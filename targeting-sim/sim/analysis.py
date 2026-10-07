"""Aggregate all result files into the tables used in the report.

python -m sim.analysis results/f1.json results/f2.json ... > results/analysis.md
"""
import json, sys, statistics as st
from collections import defaultdict
from .scm import Meter, COST
from .sweep import SCENES

ORDER = ["search-default", "search-then-verify", "api-guess-paths", "api-tree-probe", "api-tree-probe+dedupe",
         "full-clone", "shallow-all-branches", "api-refs+shallow-fetch", "api-refs+blobless-probe",
         "blobless-full-history", "blobless-recent-history", "protocol", "protocol+search-prefilter"]


def load(files):
    rows = []
    for f in files:
        for r in json.load(open(f)):
            r["_file"] = f
            rows.append(r)
    return rows


def headline(rows, profile):
    worst = dict(COST, **SCENES["worst: VPN+x5+hist x10+1.2s auth"])
    real = dict(COST, **SCENES["real-weight repos x5"])
    out = ["| strategy | recall (mean / worst run) | precision | runs fully right | time, light repos | time, realistic x5 | time, worst case | data moved | calls |",
           "|---|---|---|---|---|---|---|---|---|"]
    by = defaultdict(list)
    for r in rows:
        if r["profile"] == profile and "big" not in r["_file"]:
            by[r["strategy"]].append(r)
    for s in ORDER:
        rs = by.get(s)
        if not rs:
            continue
        n = len(rs)
        rec = st.mean(r["recall"] for r in rs)
        out.append("| {} | {:.0%} / {:.0%} | {:.0%} | {}/{} | {:.0f}s | {:.0f}s | {:.0f}s | {:.0f} MB | {:.0f} |".format(
            s, rec, min(r["recall"] for r in rs), st.mean(r["precision"] for r in rs),
            sum(1 for r in rs if r["recall"] == 1 and r["precision"] == 1), n,
            st.mean(r["wall_s"] for r in rs), st.mean(Meter.wall(r["phases"], real) for r in rs),
            st.mean(Meter.wall(r["phases"], worst) for r in rs), st.mean(r["mb"] for r in rs), st.mean(r["total_calls"] for r in rs)))
    return "\n".join(out)


def recall_by_class(rows, profile="github"):
    strategies = ["search-default", "search-then-verify", "api-guess-paths", "protocol+search-prefilter", "protocol"]
    classes = ["manifest", "config", "resolution", "code", "cross-file"]
    agg = defaultdict(list)
    for r in rows:
        if r["profile"] == profile and r["strategy"] in strategies:
            agg[(r["strategy"], r["cls"])].append(r["recall"])
    out = ["| strategy | " + " | ".join(classes) + " |", "|---" * (len(classes) + 1) + "|"]
    for s in strategies:
        out.append(f"| {s} | " + " | ".join(f"{st.mean(agg[(s, c)]):.0%}" if agg[(s, c)] else "-" for c in classes) + " |")
    return "\n".join(out)


def miss_reasons(rows, strategy, profile="github"):
    tot = defaultdict(int)
    missed = 0
    truth = 0
    for r in rows:
        if r["profile"] == profile and r["strategy"] == strategy:
            truth += r["truth"]
            missed += r["fn"]
            for k, v in r["miss_reasons"].items():
                tot[k] += v
    return truth, missed, dict(sorted(tot.items(), key=lambda kv: -kv[1]))


def per_scenario(rows, profile="github"):
    keys = sorted({r["scenario"] for r in rows})
    out = ["| scenario | policy | class | targets | search-default | search-then-verify | guess paths | protocol | protocol time |",
           "|---|---|---|---|---|---|---|---|---|"]
    for k in keys:
        rs = [r for r in rows if r["scenario"] == k and r["profile"] == profile and "big" not in r["_file"]]
        g = lambda s, f="recall": st.mean(r[f] for r in rs if r["strategy"] == s)
        t = rs[0]
        out.append(f"| {k} {t['title']} | {t['policy']} | {t['cls']} | {st.mean(r['truth'] for r in rs if r['strategy'] == 'protocol'):.0f} | "
                   f"{g('search-default'):.0%} | {g('search-then-verify'):.0%} | {g('api-guess-paths'):.0%} | {g('protocol'):.0%} | {g('protocol', 'wall_s'):.0f}s |")
    return "\n".join(out)


def scaling(rows):
    out = ["| fleet | repos | strategy | profile | mean time (light) | mean time (realistic x5) | calls |", "|---|---|---|---|---|---|---|"]
    real = dict(COST, **SCENES["real-weight repos x5"])
    for f in sorted({r["_file"] for r in rows}):
        n = 300 if "big" in f else 120
        for s in ["protocol", "api-refs+blobless-probe", "shallow-all-branches", "full-clone", "search-then-verify"]:
            for p in ["github", "bitbucket"]:
                rs = [r for r in rows if r["_file"] == f and r["strategy"] == s and r["profile"] == p]
                if rs:
                    out.append(f"| {f.split('/')[-1]} | {n} | {s} | {p} | {st.mean(r['wall_s'] for r in rs):.0f}s | "
                               f"{st.mean(Meter.wall(r['phases'], real) for r in rs):.0f}s | {st.mean(r['total_calls'] for r in rs):.0f} |")
    return "\n".join(out)


def main(files):
    rows = load(files)
    n_runs = len(rows)
    print(f"# Simulation results\n\n{n_runs} targeting runs across {len(set(r['_file'] for r in rows))} fleets, "
          f"{len(set(r['scenario'] for r in rows))} scenarios, {len(set(r['strategy'] for r in rows))} strategies, 2 SCM profiles.\n")
    for p in ["github", "bitbucket"]:
        print(f"## Headline — {p}\n\n" + headline(rows, p) + "\n")
    print("## Recall by signal class (GitHub)\n\n" + recall_by_class(rows) + "\n")
    for s in ["search-then-verify", "search-default", "protocol+search-prefilter"]:
        t, m, why = miss_reasons(rows, s)
        print(f"- **{s}** missed {m} of {t} targets: {why}")
    print("\n## Per scenario (GitHub, 120-repo fleets)\n\n" + per_scenario(rows) + "\n")
    print("## Scaling\n\n" + scaling(rows) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
