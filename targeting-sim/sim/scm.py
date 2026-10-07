"""Snapshot of the fleet (read straight from the bare repos) plus simulated SCM front-ends
(GitHub REST/GraphQL, Bitbucket Data Center REST) and a git client whose clone costs are
measured from real clones of the bare repos.

Every simulated call is charged to a Meter: calls by type, bytes moved, and modeled
wall-clock (latency + bandwidth + rate limits + worker-pool parallelism).
"""
import json, math, os, re, subprocess, pickle, shutil, tempfile, zlib
from collections import defaultdict
from pathlib import Path

DAY = 86400

# ------------------------------------------------------------------ cost model (edit freely)
COST = dict(
    gh_rest_s=0.30,            # REST round trip
    gh_gql_s=0.90,             # GraphQL query with aliases (heavier server-side)
    gh_gql_per_node_s=0.004,   # extra per aliased node
    gh_gql_max_aliases=50,     # blob text fetches per GraphQL query
    gh_gql_max_oid_aliases=100,
    gh_search_s=0.70,
    gh_search_per_min=10,      # REST search/code: 10 req/min authenticated
    gh_search_page=100,
    gh_search_cap=1000,        # max results per query
    gh_search_max_bytes=384 * 1024,
    bb_rest_s=0.25,
    bb_search_s=0.80,
    bb_search_per_min=60,      # admin-configurable on DC; generous default
    bb_search_page=100,
    bb_search_max_bytes=512 * 1024,
    git_handshake_s=0.60,      # TLS + auth + ref advertisement + negotiation
    git_bw_Bps=12.5e6,         # 100 Mbit/s
    git_server_pack_s_per_MB=0.05,
    local_scan_s_per_ref=0.03,
    git_size_mult=1.0,
    history_mult=1.0,          # scale history-only bytes (our synthetic repos have ~5-125 commits; real ones thousands)         # scale measured git bytes (our synthetic code compresses better than real code)
    api_workers=4,
    git_workers=8,
)


class Meter:
    """Records raw work per phase so wall-clock can be recomputed under any cost model."""

    def __init__(self):
        self.calls = defaultdict(int)
        self.phases = []
        self._cur = None
        self.phase("start")

    def phase(self, name):
        self._cur = dict(name=name, api_s=0.0, api_bytes=0, git_rt=0, git_bytes=0, git_bytes_fixed=0, git_bytes_hist=0, local_s=0.0,
                         search_n=0, search_s=0.0, search_per_min=10)
        self.phases.append(self._cur)

    def api(self, kind, seconds, nbytes=0):
        self.calls[kind] += 1
        self._cur["api_s"] += seconds
        self._cur["api_bytes"] += nbytes

    def search(self, kind, seconds, per_min):
        self.calls[kind] += 1
        self._cur["search_n"] += 1
        self._cur["search_s"] += seconds
        self._cur["search_per_min"] = per_min

    def git(self, kind, nbytes, round_trips=1, local_s=0.0, scale=True, hist_bytes=0):
        """scale=False for targeted fetches (a pom.xml is the same size in a big or small repo).
        hist_bytes: the part of nbytes that only exists because of history depth."""
        self.calls[kind] += 1
        self._cur["git_rt"] += round_trips
        self._cur["git_bytes" if scale else "git_bytes_fixed"] += nbytes - hist_bytes
        self._cur["git_bytes_hist"] += hist_bytes
        self._cur["local_s"] += local_s

    def local(self, seconds):
        self._cur["local_s"] += seconds

    @staticmethod
    def wall(phases, c=None):
        c = c or COST
        total = 0.0
        for p in phases:
            n, pm = p["search_n"], p["search_per_min"]
            search_t = p["search_s"] if n <= pm else max(p["search_s"], (math.ceil(n / pm) - 1) * 60 + (n % pm or pm) * p["search_s"] / n)
            api_t = (p["api_s"] + p["api_bytes"] / c["git_bw_Bps"]) / c["api_workers"]
            gb = p["git_bytes"] * c["git_size_mult"] + p["git_bytes_fixed"] + p.get("git_bytes_hist", 0) * c["git_size_mult"] * c["history_mult"]
            git_t = (p["git_rt"] * c["git_handshake_s"] + gb / c["git_bw_Bps"] + gb / 1e6 * c["git_server_pack_s_per_MB"] + p["local_s"]) / c["git_workers"]
            total += max(api_t, git_t, search_t)
        return total

    def summary(self):
        api_b = sum(p["api_bytes"] for p in self.phases)
        git_b = sum(p["git_bytes"] * COST["git_size_mult"] + p["git_bytes_fixed"] + p["git_bytes_hist"] * COST["git_size_mult"] * COST["history_mult"] for p in self.phases)
        return dict(calls=dict(self.calls), total_calls=sum(self.calls.values()),
                    mb=round((api_b + git_b) / 1e6, 2), api_mb=round(api_b / 1e6, 2), git_mb=round(git_b / 1e6, 2),
                    wall_s=round(self.wall(self.phases), 1), phases=self.phases)


# ------------------------------------------------------------------ snapshot of the truth
def git(repo, *args, input=None):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True, input=input).stdout


class Fleet:
    def __init__(self, root):
        self.root = Path(root).resolve()
        meta = json.loads((self.root / "fleet.json").read_text())
        self.now = meta["now"]
        self.repos = {r["name"]: r for r in meta["repos"]}
        snap = self.root / "snapshot.pkl"
        if snap.exists():
            d = pickle.loads(snap.read_bytes())
        else:
            d = self._build()
            snap.write_bytes(pickle.dumps(d))
        self.refs, self.trees, self.blob_sizes, self.index_tree, self.tags = d["refs"], d["trees"], d["blob_sizes"], d["index_tree"], d["tags"]
        self._blob_cache = {}
        bc = self.root / "blobs.pkl"
        if bc.exists():
            self._blob_cache = pickle.loads(bc.read_bytes())

    def remote(self, name):
        return self.root / "remotes" / f"{name}.git"

    def _build(self):
        refs, trees, blob_sizes, index_tree, tags = {}, {}, {}, {}, {}
        for name, meta in self.repos.items():
            rp = self.remote(name)
            out = git(rp, "for-each-ref", "--format=%(refname) %(objectname) %(committerdate:unix) %(*objectname)").decode().split("\n")
            rr, tg = {}, {}
            for line in filter(None, out):
                ref, oid, ts, peeled = (line.split(" ") + [""])[:4]
                if ref.startswith("refs/heads/"):
                    rr[ref[11:]] = dict(oid=oid, date=int(ts))
                elif ref.startswith("refs/tags/"):
                    tg[ref[10:]] = peeled or oid
            for b, info in rr.items():
                for base in ("main", "develop"):
                    if base in rr and b != base:
                        info[f"ahead_{base}"] = int(git(rp, "rev-list", "--count", f"{base}..{b}").strip())
            refs[name], tags[name] = rr, tg
            for commit in {i["oid"] for i in rr.values()} | set(tg.values()):
                trees[(name, commit)] = self._ls_tree(rp, commit)
            if meta.get("index_lag"):
                parent = git(rp, "rev-parse", "main~1").decode().strip()
                trees[(name, parent)] = self._ls_tree(rp, parent)
                index_tree[name] = parent
            else:
                index_tree[name] = rr["main"]["oid"]
            for line in git(rp, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize) %(objectsize:disk)").decode().split("\n"):
                if line:
                    o, t, s, ds = line.split()
                    if t == "blob":
                        blob_sizes[(name, o)] = (int(s), int(ds))
        return dict(refs=refs, trees=trees, blob_sizes=blob_sizes, index_tree=index_tree, tags=tags)

    @staticmethod
    def _ls_tree(rp, commit):
        res = {}
        for line in git(rp, "ls-tree", "-r", "-l", commit).decode().split("\n"):
            if line:
                info, path = line.split("\t", 1)
                _, _, oid, size = info.split()
                res[path] = (oid, int(size))
        return res

    def tree(self, repo, ref_or_oid):
        if ref_or_oid in self.refs[repo]:
            oid = self.refs[repo][ref_or_oid]["oid"]
        elif ref_or_oid in self.tags.get(repo, {}):
            oid = self.tags[repo][ref_or_oid]
        else:
            oid = ref_or_oid
        return self.trees[(repo, oid)]

    def blob(self, repo, oid):
        if oid not in self._blob_cache:
            self._blob_cache[oid] = git(self.remote(repo), "cat-file", "blob", oid)
        return self._blob_cache[oid]

    def wire(self, repo, oid):
        """Bytes on the wire for one blob (compressed, as in a pack or a gzip'd API response)."""
        k = ("wire", oid)
        if k not in self._blob_cache:
            self._blob_cache[k] = len(zlib.compress(self.blob(repo, oid), 6))
        return self._blob_cache[k]

    def save_blob_cache(self):
        (self.root / "blobs.pkl").write_bytes(pickle.dumps(self._blob_cache))


# ------------------------------------------------------------------ simulated front-ends
TOKEN = re.compile(r"[A-Za-z0-9_]+")


def tokens(text):
    return [t.lower() for t in TOKEN.findall(text)]


def contains_token_seq(hay, needle):
    n = len(needle)
    if n == 0:
        return False
    first = needle[0]
    for i, t in enumerate(hay):
        if t == first and hay[i:i + n] == needle:
            return True
    return False


_TOK_CACHE = {}
_INDEX_CACHE = {}


class SCM:
    """Common shape. `profile` is 'github' or 'bitbucket'."""

    def __init__(self, fleet: Fleet, meter: Meter, profile="github"):
        self.f, self.m, self.profile = fleet, meter, profile
        self._search_index = None

    # ---- inventory
    def list_repos(self):
        names = sorted(self.f.repos)
        if self.profile == "github":
            for _ in range(math.ceil(len(names) / 100)):
                self.m.api("gql:repos", COST["gh_gql_s"], 30_000)
        else:
            for _ in range(math.ceil(len(names) / 100)):
                self.m.api("bb:repos", COST["bb_rest_s"], 40_000)
        return [dict(name=n, archived=self.f.repos[n].get("archived", False), fork=self.f.repos[n].get("fork", False),
                     default_branch="main", kind=self.f.repos[n]["kind"], language=self._lang(n)) for n in names]

    def _lang(self, n):
        k = self.f.repos[n]["kind"]
        return {"node": "JavaScript", "python": "Python", "terraform": "HCL"}.get(k, "Java")

    def list_refs(self, repos, with_compare=True):
        """Branch tips with commit date, plus ahead-of-default/develop counts.
        GitHub: one GraphQL query per 10 repos (refs + compare fields).
        Bitbucket DC: branches?details=true gives ahead/behind vs default in one call per repo;
        ahead-of-develop needs an extra commits call per branch."""
        out = {}
        if self.profile == "github":
            for _ in range(math.ceil(len(repos) / (10 if with_compare else 25))):
                self.m.api("gql:refs", COST["gh_gql_s"] + 0.2, 60_000)
        for r in repos:
            if self.profile == "bitbucket":
                self.m.api("bb:branches", COST["bb_rest_s"], 15_000)
                if with_compare and "develop" in self.f.refs[r]:
                    for b, i in self.f.refs[r].items():
                        if b.startswith("feature/") and self.f.now - i["date"] <= 30 * DAY:
                            self.m.api("bb:ahead_develop", COST["bb_rest_s"], 2_000)
            out[r] = {b: dict(i) for b, i in self.f.refs[r].items()}
        return out

    # ---- file access
    def tree(self, repo, ref):
        """GitHub: REST git/trees?recursive=1. Bitbucket: /files?at=ref (paths only, 1000/page)."""
        t = self.f.tree(repo, ref)
        if self.profile == "github":
            self.m.api("rest:tree", COST["gh_rest_s"], 80 * len(t))
        else:
            for _ in range(max(1, math.ceil(len(t) / 1000))):
                self.m.api("bb:files", COST["bb_rest_s"], 60 * len(t))
        return t

    def read_files(self, items):
        """items: list of (repo, ref, path). GitHub batches into GraphQL aliases;
        Bitbucket DC has no batch read, one /raw call per file."""
        res = {}
        if self.profile == "github":
            for i in range(0, len(items), COST["gh_gql_max_aliases"]):
                chunk = items[i:i + COST["gh_gql_max_aliases"]]
                nbytes = 0
                for repo, ref, path in chunk:
                    ent = self.f.tree(repo, ref).get(path)
                    res[(repo, ref, path)] = self.f.blob(repo, ent[0]) if ent else None
                    nbytes += self.f.wire(repo, ent[0]) if ent else 50
                self.m.api("gql:blobs", COST["gh_gql_s"] + COST["gh_gql_per_node_s"] * len(chunk), nbytes)
        else:
            for repo, ref, path in items:
                ent = self.f.tree(repo, ref).get(path)
                res[(repo, ref, path)] = self.f.blob(repo, ent[0]) if ent else None
                self.m.api("bb:raw", COST["bb_rest_s"], self.f.wire(repo, ent[0]) if ent else 200)
        return res

    def read_oids(self, items):
        """GitHub GraphQL object(expression:"ref:path"){oid} — cheap, 100 per query."""
        res = {}
        for i in range(0, len(items), COST["gh_gql_max_oid_aliases"]):
            chunk = items[i:i + COST["gh_gql_max_oid_aliases"]]
            for repo, ref, path in chunk:
                ent = self.f.tree(repo, ref).get(path)
                res[(repo, ref, path)] = ent[0] if ent else None
            self.m.api("gql:oids", COST["gh_gql_s"] + 0.002 * len(chunk), 120 * len(chunk))
        return res

    def read_blobs_by_oid(self, repo_oids):
        """GitHub GraphQL object(oid:){... on Blob {text}} batched."""
        res = {}
        for i in range(0, len(repo_oids), COST["gh_gql_max_aliases"]):
            chunk = repo_oids[i:i + COST["gh_gql_max_aliases"]]
            nbytes = 0
            for repo, oid in chunk:
                res[oid] = self.f.blob(repo, oid)
                nbytes += self.f.wire(repo, oid)
            self.m.api("gql:blobs", COST["gh_gql_s"] + COST["gh_gql_per_node_s"] * len(chunk), nbytes)
        return res

    # ---- code search (default branch only, size-limited, token based, capped)
    def _index(self):
        ck = (str(self.f.root), self.profile)
        if ck in _INDEX_CACHE:
            self._search_index, self._tok_cache = _INDEX_CACHE[ck], _TOK_CACHE
        if self._search_index is None:
            idx = []
            limit = COST["gh_search_max_bytes"] if self.profile == "github" else COST["bb_search_max_bytes"]
            for name, meta in self.f.repos.items():
                if self.profile == "github" and meta.get("fork"):
                    continue  # forks not indexed unless more stars than parent
                if self.profile == "bitbucket" and meta.get("archived"):
                    continue  # archived excluded by default
                tree = self.f.trees[(name, self.f.index_tree[name])]
                for path, (oid, size) in tree.items():
                    if size >= limit or path.endswith(".bin"):
                        continue
                    idx.append((name, path, oid))
            self._search_index = idx
            self._tok_cache = _TOK_CACHE
            _INDEX_CACHE[ck] = idx
        return self._search_index

    def code_search(self, query, path_contains=None, filename=None, extension=None):
        idx = self._index()
        needles = [tokens(q) for q in query.split()]
        hits = []
        for name, path, oid in idx:
            if path_contains and path_contains not in path:
                continue
            if filename and not path.endswith("/" + filename) and path != filename:
                continue
            if extension and not path.endswith("." + extension):
                continue
            if oid not in self._tok_cache:
                self._tok_cache[oid] = tokens(self.f.blob(name, oid).decode("utf-8", "replace"))
            toks = self._tok_cache[oid]
            if all(contains_token_seq(toks, n) for n in needles):
                hits.append((name, path))
        page = COST["gh_search_page"] if self.profile == "github" else COST["bb_search_page"]
        cap = COST["gh_search_cap"] if self.profile == "github" else 10_000
        returned = hits[:cap]
        pages = max(1, math.ceil(len(returned) / page))
        for _ in range(pages):
            if self.profile == "github":
                self.m.search("rest:code_search", COST["gh_search_s"], COST["gh_search_per_min"])
            else:
                self.m.search("bb:code_search", COST["bb_search_s"], COST["bb_search_per_min"])
        return dict(hits=returned, total=len(hits), truncated=len(hits) > cap)


# ------------------------------------------------------------------ git client (measured)
class GitCosts:
    """Measures real clone sizes against the bare remotes once, caches them."""

    def __init__(self, fleet: Fleet):
        self.f = fleet
        self.path = fleet.root / "clone_costs.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def _measure(self, key, cmds):
        if key in self.data:
            return self.data[key]
        tmp = Path(tempfile.mkdtemp(dir=os.environ.get("SIM_TMP")))
        try:
            for c in cmds:
                subprocess.run(c, cwd=tmp, capture_output=True, check=True)
            size = 0
            for dp, _, fs in os.walk(tmp / "r" / ".git" / "objects"):
                for fn in fs:
                    size += os.path.getsize(os.path.join(dp, fn))
            self.data[key] = size
            return size
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def full(self, repo):
        url = "file://" + str(self.f.remote(repo))
        return self._measure(f"full|{repo}", [["git", "clone", "-q", "--no-checkout", url, "r"]])

    def shallow_all(self, repo):
        url = "file://" + str(self.f.remote(repo))
        return self._measure(f"shallow_all|{repo}", [["git", "clone", "-q", "--no-checkout", "--depth", "1", "--no-single-branch", url, "r"]])

    def _fetch_refs(self, repo, refs, extra):
        url = "file://" + str(self.f.remote(repo))
        specs = [f"+refs/heads/{r}:refs/remotes/origin/{r}" for r in sorted(refs)]
        return [["git", "init", "-q", "r"],
                ["git", "-C", "r", "remote", "add", "origin", url],
                ["git", "-C", "r", "fetch", "-q", *extra, "origin", *specs]]

    def shallow_refs(self, repo, refs):
        key = f"shallow_refs|{repo}|{','.join(sorted(refs))}"
        return self._measure(key, self._fetch_refs(repo, refs, ["--depth", "1"]))

    def blobless_refs(self, repo, refs):
        key = f"blobless_refs|{repo}|{','.join(sorted(refs))}"
        cmds = self._fetch_refs(repo, refs, ["--depth", "1", "--filter=blob:none"])
        cmds.insert(2, ["git", "-C", "r", "config", "remote.origin.promisor", "true"])
        cmds.insert(3, ["git", "-C", "r", "config", "remote.origin.partialclonefilter", "blob:none"])
        return self._measure(key, cmds)

    def blobless_full(self, repo):
        url = "file://" + str(self.f.remote(repo))
        return self._measure(f"blobless_full|{repo}", [["git", "clone", "-q", "--no-checkout", "--filter=blob:none", url, "r"]])

    def blobless_tips(self, repo):
        url = "file://" + str(self.f.remote(repo))
        return self._measure(f"blobless_tips|{repo}", [["git", "clone", "-q", "--no-checkout", "--filter=blob:none", "--depth", "1", "--no-single-branch", url, "r"]])

    def blobless_since(self, repo, days=120):
        """Returns (bytes, round_trips). Real gotcha: --shallow-since fails with 'no commits selected for
        shallow requests' when a repo has no commit inside the window; fall back to --depth 1."""
        import datetime
        since = datetime.datetime.utcfromtimestamp(self.f.now - days * DAY).strftime("%Y-%m-%d")
        url = "file://" + str(self.f.remote(repo))
        key = f"blobless_since{days}|{repo}"
        if key + "|rt" not in self.data:
            try:
                b = self._measure(key, [["git", "clone", "-q", "--no-checkout", "--filter=blob:none",
                                         f"--shallow-since={since}", "--no-single-branch", url, "r"]])
                self.data[key + "|rt"] = 1
            except subprocess.CalledProcessError:
                b = self._measure(key, [["git", "clone", "-q", "--no-checkout", "--filter=blob:none",
                                         "--depth", "1", "--no-single-branch", url, "r"]])
                self.data[key + "|rt"] = 2
        return self.data[key], self.data[key + "|rt"]

    def treeless_full(self, repo):
        url = "file://" + str(self.f.remote(repo))
        return self._measure(f"treeless|{repo}", [["git", "clone", "-q", "--no-checkout", "--filter=tree:0", url, "r"]])

    def blob_fetch_bytes(self, repo, oids):
        # each blob travels whole (no delta base on the client): zlib size + pack overhead
        return sum(self.f.wire(repo, o) for o in oids) + 200 + 30 * len(oids)
