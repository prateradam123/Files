"""Run every strategy against every scenario on a fleet, for GitHub and Bitbucket profiles.

python -m sim.run --fleet fleets/f1 --out results/f1.json
"""
import argparse, json, time
from pathlib import Path
from .scm import Fleet, GitCosts, COST
from .scenarios import SCENARIOS
from .strategies import ALL, ground_truth


def miss_reason(fleet, scn, repo, branch, profile):
    if branch != "main":
        return "not-default-branch"
    meta = fleet.repos[repo]
    tree = fleet.tree(repo, "main")
    paths = scn.select(list(tree))
    limit = COST["gh_search_max_bytes"] if profile == "github" else COST["bb_search_max_bytes"]
    if meta.get("index_lag"):
        return "index-lag"
    if any(tree[p][1] >= limit for p in paths):
        return "file-too-large"
    return "query-or-resolution"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fleet", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profiles", default="github,bitbucket")
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--cost", default="{}", help="JSON overrides for the cost model")
    ap.add_argument("--strategies", default="", help="comma list of strategy names (default all)")
    a = ap.parse_args()
    COST.update(json.loads(a.cost))
    fleet = Fleet(a.fleet)
    gc = GitCosts(fleet)
    rows = []
    keys = [k for k in SCENARIOS if not a.scenarios or k in a.scenarios.split(",")]
    for k in keys:
        scn = SCENARIOS[k]
        truth = ground_truth(fleet, scn)
        for profile in a.profiles.split(","):
            for S in [S for S in ALL if not a.strategies or S.name in a.strategies.split(",")]:
                t0 = time.time()
                got, cost = S(fleet, profile, gc).run(scn)
                tp, fp, fn = got & truth, got - truth, truth - got
                reasons = {}
                for r, b in fn:
                    reasons.setdefault(miss_reason(fleet, scn, r, b, profile), 0)
                    reasons[miss_reason(fleet, scn, r, b, profile)] += 1
                rows.append(dict(fleet=a.fleet, cost_overrides=a.cost, scenario=k, title=scn.title, cls=scn.cls, policy=scn.policy, profile=profile,
                                 strategy=S.name, truth=len(truth), found=len(got), tp=len(tp), fp=len(fp), fn=len(fn),
                                 recall=round(len(tp) / len(truth), 3) if truth else 1.0,
                                 precision=round(len(tp) / len(got), 3) if got else 1.0,
                                 miss_reasons=reasons, **cost, sim_runtime_s=round(time.time() - t0, 2)))
            gc.save()
        print(f"{k} truth={len(truth)} done", flush=True)
    fleet.save_blob_cache()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rows, indent=1))
    print("wrote", a.out, len(rows), "rows")


if __name__ == "__main__":
    main()
