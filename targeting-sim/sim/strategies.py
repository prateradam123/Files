"""Targeting strategies. Each takes a scenario and returns the (repo, branch) pairs it would
remediate, while the Meter records what it cost to get there."""
import re
from collections import defaultdict
from .scm import SCM, Meter, GitCosts, COST, DAY
from .scenarios import Resolver, V

LIBS = {"acme-kafka-starter", "acme-parent"}


# ------------------------------------------------------------------ shared rules
def eligible_repo(meta):
    return not meta["archived"] and not meta["fork"] and meta["kind"] != "internal-lib"


def lang_ok(meta, scn):
    return scn.langs is None or meta["language"] in scn.langs


def supported_releases(names):
    rel = sorted([b for b in names if b.startswith("release/")], key=lambda b: V(b.split("/", 1)[1]), reverse=True)
    return rel[:2]


def eligible_branches(refs, policy, now, know_ahead=True):
    """refs: {branch: {date, ahead_main?, ahead_develop?}}"""
    out = {"main"}
    if policy == "default":
        return out
    out |= set(supported_releases(refs))
    if policy == "release":
        return out
    for b, i in refs.items():
        if b == "develop":
            out.add(b)
        elif b.startswith("develop-") and now - i["date"] <= 90 * DAY:
            out.add(b)
    if policy == "long_lived":
        return out
    for b, i in refs.items():
        if b.startswith("feature/") and now - i["date"] <= 30 * DAY:
            if not know_ahead:
                out.add(b)
                continue
            base = "develop" if "develop" in refs else "main"
            if i.get(f"ahead_{base}", 1) > 0:
                out.add(b)
    return out


def ground_truth(fleet, scn):
    R = Resolver().load(lambda repo, ref, path: fleet.blob(repo, fleet.tree(repo, ref)[path][0]).decode())
    targets = set()
    for name, meta in fleet.repos.items():
        m = dict(meta, language=None)
        if meta.get("archived") or meta.get("fork") or meta["kind"] == "internal-lib":
            continue
        refs = fleet.refs[name]
        for b in eligible_branches(refs, scn.policy, fleet.now):
            if b not in refs:
                continue
            tree = fleet.tree(name, b)
            files = {p: fleet.blob(name, tree[p][0]).decode("utf-8", "replace") for p in scn.select(list(tree))}
            if scn.evaluate(files, R):
                targets.add((name, b))
    return targets


# ------------------------------------------------------------------ strategy base
class Strategy:
    name = "base"
    note = ""

    def __init__(self, fleet, profile, gitcosts):
        self.f, self.profile, self.gc = fleet, profile, gitcosts

    def run(self, scn):
        self.m = Meter()
        self.scm = SCM(self.f, self.m, self.profile)
        targets = self.target(scn)
        return targets, self.m.summary()

    # helpers ---------------------------------------------------------------------------
    def inventory(self, scn, use_lang=True):
        self.m.phase("inventory")
        repos = self.scm.list_repos()
        return [r["name"] for r in repos if eligible_repo(r) and (not use_lang or lang_ok(r, scn))]

    def branches(self, repos, scn):
        self.m.phase("branches")
        refs = self.scm.list_refs(repos, with_compare=scn.policy == "active")
        return {r: [b for b in eligible_branches(refs[r], scn.policy, self.f.now) if b in refs[r]] for r in repos}

    def api_resolver(self, scn):
        if not scn.needs_resolver:
            return None
        items = [("acme-kafka-starter", t, "pom.xml") for t in ["v3.1.0", "v3.2.0", "v3.4.0", "v4.0.1"]] + \
                [("acme-parent", t, "pom.xml") for t in ["v5.0.0", "v5.1.0", "v5.2.0"]]
        got = self.scm.read_files(items)
        return Resolver().load(lambda r, t, p: got[(r, t, p)].decode())

    def git_resolver(self, scn):
        if not scn.needs_resolver:
            return None
        for lib in LIBS:
            self.m.git("git:clone_lib", self.gc.full(lib), scale=False)
        return Resolver().load(lambda repo, ref, path: self.f.blob(repo, self.f.tree(repo, ref)[path][0]).decode())

    def local_eval(self, repo, branch, scn, R):
        tree = self.f.tree(repo, branch)
        files = {p: self.f.blob(repo, tree[p][0]).decode("utf-8", "replace") for p in scn.select(list(tree))}
        self.m.local(COST["local_scan_s_per_ref"])
        return scn.evaluate(files, R)


# ------------------------------------------------------------------ code search family
class CodeSearchDefault(Strategy):
    name = "search-default"
    note = "Naive: one code-search query, patch the default branch of every hit."

    def target(self, scn):
        self.m.phase("search")
        q, quals = scn.naive
        res = self.scm.code_search(q, **quals)
        return {(r, "main") for r, _ in res["hits"]}


class CodeSearchVerify(Strategy):
    name = "search-then-verify"
    note = "Careful: several queries to find candidate repos, then list branches and verify each eligible branch via API."

    def target(self, scn):
        repos = {r for r in self.inventory(scn)}
        self.m.phase("search")
        cand = set()
        for q, quals in scn.search:
            res = self.scm.code_search(q, **quals)
            cand |= {r for r, _ in res["hits"] if r in repos}
        cand = sorted(cand)
        br = self.branches(cand, scn)
        self.m.phase("verify")
        R = self.api_resolver(scn)
        return api_verify(self, scn, br, R, dedupe=True)


# ------------------------------------------------------------------ API probing family
def api_verify(st, scn, br, R, dedupe):
    trees, seen_commit = {}, {}
    for repo, bs in br.items():
        for b in bs:
            oid = st.f.refs[repo][b]["oid"]
            if dedupe and (repo, oid) in seen_commit:
                trees[(repo, b)] = seen_commit[(repo, oid)]
                continue
            t = st.scm.tree(repo, b)
            trees[(repo, b)] = t
            seen_commit[(repo, oid)] = t
    need = {k: scn.select(list(t)) for k, t in trees.items()}
    contents = {}
    if dedupe:
        uniq = {}
        for (repo, b), paths in need.items():
            for p in paths:
                uniq.setdefault(trees[(repo, b)][p][0], repo)
        got = st.scm.read_blobs_by_oid([(r, o) for o, r in uniq.items()]) if st.profile == "github" else None
        if got is None:  # bitbucket: one raw call per unique blob (need any ref holding it)
            got = {}
            where = {}
            for (repo, b), paths in need.items():
                for p in paths:
                    where.setdefault(trees[(repo, b)][p][0], (repo, b, p))
            res = st.scm.read_files(list(where.values()))
            for oid, k in where.items():
                got[oid] = res[k]
        for (repo, b), paths in need.items():
            contents[(repo, b)] = {p: got[trees[(repo, b)][p][0]].decode("utf-8", "replace") for p in paths}
    else:
        items = [(repo, b, p) for (repo, b), paths in need.items() for p in paths]
        res = st.scm.read_files(items)
        for (repo, b), paths in need.items():
            contents[(repo, b)] = {p: res[(repo, b, p)].decode("utf-8", "replace") for p in paths}
    return {k for k, files in contents.items() if scn.evaluate(files, R)}


class ApiTreeProbe(Strategy):
    name = "api-tree-probe"
    note = "No clones: list every eligible branch, fetch its tree, read only the files the check needs (no dedupe)."
    dedupe = False

    def target(self, scn):
        repos = self.inventory(scn)
        br = self.branches(repos, scn)
        self.m.phase("probe")
        R = self.api_resolver(scn)
        return api_verify(self, scn, br, R, dedupe=self.dedupe)


class ApiTreeProbeDedupe(ApiTreeProbe):
    name = "api-tree-probe+dedupe"
    note = "As api-tree-probe, but identical commits share one tree and each unique blob is read once."
    dedupe = True


class ApiGuessProbe(Strategy):
    name = "api-guess-paths"
    note = "No trees: read a handful of guessed exact paths on every eligible branch."

    def target(self, scn):
        repos = self.inventory(scn)
        br = self.branches(repos, scn)
        self.m.phase("probe")
        R = self.api_resolver(scn)
        if not scn.guess:
            return set()
        items = [(r, b, p) for r, bs in br.items() for b in bs for p in scn.guess]
        res = self.scm.read_files(items)
        out = set()
        for r, bs in br.items():
            for b in bs:
                files = {p: res[(r, b, p)].decode("utf-8", "replace") for p in scn.guess if res[(r, b, p)] is not None}
                if files and scn.evaluate(files, R):
                    out.add((r, b))
        return out


# ------------------------------------------------------------------ clone family
class FullClone(Strategy):
    name = "full-clone"
    note = "Clone every candidate repo with full history and all branches, decide everything locally."

    def target(self, scn):
        repos = self.inventory(scn)
        self.m.phase("clone")
        R = self.git_resolver(scn)
        out = set()
        for r in repos:
            full, tips = self.gc.full(r), self.gc.shallow_all(r)
            self.m.git("git:clone_full", full, hist_bytes=max(0, full - tips))
            for b in eligible_branches(self.f.refs[r], scn.policy, self.f.now):
                if b in self.f.refs[r] and self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class ShallowAll(Strategy):
    name = "shallow-all-branches"
    note = "git clone --depth 1 --no-single-branch per repo; no API. Shallow history cannot tell merged branches apart."

    def target(self, scn):
        repos = self.inventory(scn)
        self.m.phase("clone")
        R = self.git_resolver(scn)
        out = set()
        for r in repos:
            self.m.git("git:clone_shallow_all", self.gc.shallow_all(r))
            refs = {b: dict(date=i["date"]) for b, i in self.f.refs[r].items()}
            for b in eligible_branches(refs, scn.policy, self.f.now, know_ahead=False):
                if b in refs and self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class ShallowEligible(Strategy):
    name = "api-refs+shallow-fetch"
    note = "API decides eligible branches, then one depth-1 fetch of exactly those branches per repo; grep locally."

    def target(self, scn):
        repos = self.inventory(scn)
        br = self.branches(repos, scn)
        self.m.phase("clone")
        R = self.git_resolver(scn)
        out = set()
        for r, bs in br.items():
            self.m.git("git:fetch_shallow_refs", self.gc.shallow_refs(r, bs))
            for b in bs:
                if self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class BloblessEligible(Strategy):
    name = "api-refs+blobless-probe"
    note = "API decides eligible branches; depth-1 blobless fetch (commits+trees only); list paths locally; fetch only the needed blobs in one batch per repo."

    def target(self, scn):
        repos = self.inventory(scn)
        br = self.branches(repos, scn)
        self.m.phase("clone")
        R = self.git_resolver(scn)
        out = set()
        for r, bs in br.items():
            self.m.git("git:fetch_blobless_refs", self.gc.blobless_refs(r, bs))
            need = {}
            for b in bs:
                t = self.f.tree(r, b)
                for p in scn.select(list(t)):
                    need[t[p][0]] = 1
            if need:
                self.m.git("git:fetch_blobs", self.gc.blob_fetch_bytes(r, list(need)), scale=scn.cls in ("code", "cross-file"))
            for b in bs:
                if self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class BloblessFullHistory(Strategy):
    name = "blobless-full-history"
    note = "No API beyond the repo list: git clone --filter=blob:none (all commits+trees, no blobs) gives exact branch dates and merge status locally; fetch only needed blobs."

    def target(self, scn):
        repos = self.inventory(scn)
        self.m.phase("clone")
        R = self.git_resolver(scn)
        out = set()
        for r in repos:
            full, tips = self.gc.blobless_full(r), self.gc.blobless_tips(r)
            self.m.git("git:clone_blobless_full", full, hist_bytes=max(0, full - tips))
            bs = [b for b in eligible_branches(self.f.refs[r], scn.policy, self.f.now) if b in self.f.refs[r]]
            need = {}
            for b in bs:
                t = self.f.tree(r, b)
                for p in scn.select(list(t)):
                    need[t[p][0]] = 1
            if need:
                self.m.git("git:fetch_blobs", self.gc.blob_fetch_bytes(r, list(need)), scale=scn.cls in ("code", "cross-file"))
            for b in bs:
                if self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class BloblessRecentHistory(BloblessFullHistory):
    name = "blobless-recent-history"
    note = ("No branch API: git clone --filter=blob:none --shallow-since=<120 days> --no-single-branch. Recent commit graph "
            "+ trees give branch dates and merged status locally; fetch only needed blobs.")

    def target(self, scn):
        repos = self.inventory(scn)
        self.m.phase("clone")
        R = self.git_resolver(scn)
        out = set()
        for r in repos:
            nbytes, rt = self.gc.blobless_since(r)
            self.m.git("git:clone_blobless_recent", nbytes, round_trips=rt)
            bs = [b for b in eligible_branches(self.f.refs[r], scn.policy, self.f.now) if b in self.f.refs[r]]
            need = {}
            for b in bs:
                t = self.f.tree(r, b)
                for p in scn.select(list(t)):
                    need[t[p][0]] = 1
            if need:
                self.m.git("git:fetch_blobs", self.gc.blob_fetch_bytes(r, list(need)), scale=scn.cls in ("code", "cross-file"))
            for b in bs:
                if self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class Protocol(Strategy):
    name = "protocol"
    note = ("Recommended: API only for the repo inventory (+ language filter) -> per repo one "
            "`git clone --filter=blob:none --shallow-since=<window> --no-single-branch` (fallback --depth 1 when the repo "
            "has no commit in the window) -> branch eligibility from the local graph (dates, merged status) -> list paths "
            "locally -> one batched fetch of only the blobs the check needs -> decide locally. Internal-lib facts via API. "
            "Same mechanism on GitHub and Bitbucket, and the clone doubles as the working copy for the change.")
    search_prefilter = False

    def target(self, scn):
        repos = self.inventory(scn)
        if self.search_prefilter and scn.policy == "default":
            self.m.phase("search")
            cand = set()
            for q, quals in scn.search:
                cand |= {r for r, _ in self.scm.code_search(q, **quals)["hits"]}
            repos = [r for r in repos if r in cand]
        self.m.phase("evidence")
        R = self.api_resolver(scn)
        out = set()
        for r in repos:
            nbytes, rt = self.gc.blobless_since(r)
            self.m.git("git:clone_blobless_recent", nbytes, round_trips=rt)
            bs = [b for b in eligible_branches(self.f.refs[r], scn.policy, self.f.now) if b in self.f.refs[r]]
            need = {}
            for b in bs:
                t = self.f.tree(r, b)
                for p in scn.select(list(t)):
                    need[t[p][0]] = 1
            if need:
                self.m.git("git:fetch_blobs", self.gc.blob_fetch_bytes(r, list(need)), scale=scn.cls in ("code", "cross-file"))
            for b in bs:
                if self.local_eval(r, b, scn, R):
                    out.add((r, b))
        return out


class ProtocolSearchPrefilter(Protocol):
    name = "protocol+search-prefilter"
    note = "Protocol, but for default-branch-only changes it trusts code search to pick the repos first."
    search_prefilter = True


ALL = [CodeSearchDefault, CodeSearchVerify, ApiGuessProbe, ApiTreeProbe, ApiTreeProbeDedupe,
       FullClone, ShallowAll, ShallowEligible, BloblessEligible, BloblessFullHistory, BloblessRecentHistory, Protocol, ProtocolSearchPrefilter]
