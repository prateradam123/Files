#!/usr/bin/env python3
"""Targeting building blocks for multi-repo rollouts (stdlib only, Windows-friendly).

Finds every (repo, branch) that needs a change by reading branch tips locally from a
light snapshot clone. Commands compose; the agent decides globs, policy and the check.

  snapshot   clone light: blobless + recent history, all branches (with the fallbacks that matter)
  branches   eligible branches for a policy, with tip date and merged status, as JSON
  fetch      one batched fetch of only the blobs under the given globs, on the given branches
  grep       regex over those files per branch (after fetch); exit code 0 = any match
  show       print one file at one branch (no checkout)

Examples
  python snapshot.py snapshot https://github.com/acme/contract-svc.git work/contract-svc
  python snapshot.py branches work/contract-svc --policy active
  python snapshot.py fetch work/contract-svc --policy active --glob "**/pom.xml" --glob "**/build.gradle"
  python snapshot.py grep work/contract-svc --policy active --glob "**/application*.yml" --regex "legacy-retry"
  python snapshot.py snapshot-many repos.txt work --workers 8

Needs Git 2.43+ on the client (GIT_NO_LAZY_FETCH, partial clone). Server: GitHub, or Bitbucket Data Center 7.13+
with Git 2.18+; servers without partial clone fall back to shallow snapshots automatically.
"""
import argparse, fnmatch, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

DAY = 86400


def git(*args, cwd=None, input=None, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


# ------------------------------------------------------------------ snapshot
def snapshot(url, dest, window_days=120):
    """Blobless + shallow-since clone of every branch. Returns the mode actually used.

    Gotchas handled:
      * --shallow-since fails ("no commits selected for shallow requests") when the repo has no
        commit inside the window -> retry with --depth 1 (dormant repo: tips are all that matter).
      * A server without partial-clone support prints "filtering not recognized by server" and
        silently sends EVERYTHING -> detect it, re-clone shallow instead.
      * Existing snapshot -> fetch instead of re-cloning (same flags), so reruns are cheap.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    base = ["--no-checkout", "--no-single-branch", "--filter=blob:none"]
    if os.path.isdir(os.path.join(dest, ".git")):
        p = git("fetch", "--prune", "--filter=blob:none", f"--shallow-since={since}", "origin", cwd=dest, check=False)
        if p.returncode != 0:
            git("fetch", "--prune", "--filter=blob:none", "--depth", "1", "origin", cwd=dest)
        return "refreshed"
    p = git("clone", "-q", *base, f"--shallow-since={since}", url, dest, check=False)
    mode = "blobless+since"
    if p.returncode != 0 and "no commits selected" in p.stderr:
        p = git("clone", "-q", *base, "--depth", "1", url, dest, check=False)
        mode = "blobless+depth1"
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    if "filtering not recognized" in p.stderr:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        git("clone", "-q", "--no-checkout", "--no-single-branch", "--depth", "1", url, dest)
        mode = "shallow (server has no partial clone)"
    return mode


# ------------------------------------------------------------------ branches
def _refs(repo):
    out = git("for-each-ref", "--format=%(refname:short)\t%(objectname)\t%(committerdate:unix)", "refs/remotes/origin", cwd=repo).stdout
    refs = {}
    for line in out.splitlines():
        name, oid, ts = line.split("\t")
        name = name.split("/", 1)[1] if name.startswith("origin/") else name
        if name in ("HEAD", "origin"):
            continue
        refs[name] = dict(oid=oid, date=int(ts))
    return refs


def default_branch(repo):
    p = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
    return p.stdout.strip().split("/", 1)[1] if p.returncode == 0 else "main"


def _ver(name):
    return tuple(int(x) for x in re.findall(r"\d+", name.split("/", 1)[-1])[:4] or [0])


def eligible(repo, policy, feature_days=30, stream_days=90, releases=2, now=None):
    """Policies: default | release | long_lived | active (see TARGETING.md)."""
    now = now or time.time()
    refs, dflt = _refs(repo), default_branch(repo)
    pick = {dflt: "default"}
    if policy != "default":
        rel = sorted([b for b in refs if b.startswith("release/")], key=_ver, reverse=True)[:releases]
        pick.update({b: "supported-release" for b in rel})
    if policy in ("long_lived", "active"):
        for b, i in refs.items():
            if b == "develop":
                pick[b] = "develop"
            elif b.startswith("develop-") and now - i["date"] <= stream_days * DAY:
                pick[b] = "active-stream"
    if policy == "active":
        base = "develop" if "develop" in refs else dflt
        for b, i in refs.items():
            if b.startswith("feature/") and now - i["date"] <= feature_days * DAY:
                ahead = git("rev-list", "--count", f"origin/{base}..origin/{b}", cwd=repo, check=False)
                n = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 1
                if n > 0:  # merged branches carry nothing new: skip them
                    pick[b] = f"active-feature (+{n} vs {base})"
    return [dict(branch=b, why=w, oid=refs[b]["oid"], date=datetime.fromtimestamp(refs[b]["date"], timezone.utc).date().isoformat())
            for b, w in pick.items() if b in refs]


# ------------------------------------------------------------------ files
def _match(path, globs):
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", "")) for g in globs)


def paths(repo, branch, globs):
    out = git("ls-tree", "-r", f"origin/{branch}", cwd=repo).stdout  # trees are local: no network
    res = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _, typ, oid = meta.split()
        if typ == "blob" and _match(path, globs):
            res.append((path, oid))
    return res


def fetch(repo, branches, globs):
    """One round trip for every needed blob across all branches (git's own promisor fetch)."""
    oids = sorted({oid for b in branches for _, oid in paths(repo, b, globs)})
    if not oids:
        return 0
    if snapshot_mode(repo) == "shallow":
        return 0  # shallow (non-partial) snapshots already hold every blob at the tips
    # Which blobs are not here yet? (GIT_NO_LAZY_FETCH stops cat-file fetching them one by one.)
    chk = subprocess.run(["git", "cat-file", "--batch-check"], cwd=repo, input="\n".join(oids) + "\n",
                         capture_output=True, text=True, env=dict(os.environ, GIT_NO_LAZY_FETCH="1"))
    missing = [ln.split()[0] for ln in chk.stdout.splitlines() if ln.endswith(" missing")]
    if not missing:
        return 0
    # One round trip for all of them: the same fetch git runs internally for promisor objects.
    p = subprocess.run(["git", "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q", "origin", "--no-tags",
                        "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin"],
                       cwd=repo, input="\n".join(missing) + "\n", capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return len(missing)


def snapshot_mode(repo):
    p = git("config", "--get", "remote.origin.partialclonefilter", cwd=repo, check=False)
    return "blobless" if p.returncode == 0 else "shallow"


def show(repo, branch, path):
    return git("show", f"origin/{branch}:{path}", cwd=repo).stdout


def grep(repo, branches, globs, regex):
    rx = re.compile(regex)
    hits = []
    for b in branches:
        for path, _ in paths(repo, b, globs):
            for n, line in enumerate(show(repo, b, path).splitlines(), 1):
                if rx.search(line):
                    hits.append(dict(branch=b, path=path, line=n, text=line.strip()[:200]))
    return hits


# ------------------------------------------------------------------ cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("snapshot"); a.add_argument("url"); a.add_argument("dest"); a.add_argument("--window-days", type=int, default=120)
    a = sp.add_parser("snapshot-many"); a.add_argument("list", help="file: one clone URL per line"); a.add_argument("root")
    a.add_argument("--workers", type=int, default=8); a.add_argument("--window-days", type=int, default=120)
    for name in ("branches", "fetch", "grep"):
        a = sp.add_parser(name); a.add_argument("repo")
        a.add_argument("--policy", default="active", choices=["default", "release", "long_lived", "active"])
        a.add_argument("--branch", action="append", help="explicit branches instead of a policy")
        if name != "branches":
            a.add_argument("--glob", action="append", required=True)
        if name == "grep":
            a.add_argument("--regex", required=True)
    a = sp.add_parser("show"); a.add_argument("repo"); a.add_argument("branch"); a.add_argument("path")
    a = ap.parse_args()

    if a.cmd == "snapshot":
        print(json.dumps(dict(dest=a.dest, mode=snapshot(a.url, a.dest, a.window_days))))
    elif a.cmd == "snapshot-many":
        urls = [u.strip() for u in open(a.list) if u.strip() and not u.startswith("#")]
        def one(u):
            dest = os.path.join(a.root, re.sub(r"\.git$", "", u.rstrip("/").split("/")[-1]))
            try:
                return dict(url=u, dest=dest, mode=snapshot(u, dest, a.window_days))
            except Exception as e:  # report and keep going; the agent decides what to do with failures
                return dict(url=u, dest=dest, error=str(e)[:300])
        with ThreadPoolExecutor(a.workers) as ex:
            for r in ex.map(one, urls):
                print(json.dumps(r), flush=True)
    elif a.cmd == "show":
        sys.stdout.write(show(a.repo, a.branch, a.path))
    else:
        bs = a.branch or [e["branch"] for e in eligible(a.repo, a.policy)]
        if a.cmd == "branches":
            print(json.dumps(eligible(a.repo, a.policy) if not a.branch else bs, indent=1))
        elif a.cmd == "fetch":
            print(json.dumps(dict(branches=bs, blobs=fetch(a.repo, bs, a.glob))))
        elif a.cmd == "grep":
            fetch(a.repo, bs, a.glob)
            hits = grep(a.repo, bs, a.glob, a.regex)
            print(json.dumps(hits, indent=1))
            sys.exit(0 if hits else 1)


if __name__ == "__main__":
    main()
