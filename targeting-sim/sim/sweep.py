"""Recompute wall-clock for stored results under alternative cost assumptions."""
import json, sys, copy
from collections import defaultdict
from .scm import Meter, COST

SCENES = {
    "baseline (100 Mbit, light repos)": {},
    "real-weight repos x5": {"git_size_mult": 5},
    "heavy repos x20": {"git_size_mult": 20},
    "VPN 25 Mbit, x5": {"git_size_mult": 5, "git_bw_Bps": 3.1e6},
    "slow auth handshake 1.5s, x5": {"git_size_mult": 5, "git_handshake_s": 1.5},
    "laptop: 4 git workers, x5": {"git_size_mult": 5, "git_workers": 4},
    "long history x10, x5": {"git_size_mult": 5, "history_mult": 10},
    "worst: VPN+x5+hist x10+1.2s auth": {"git_size_mult": 5, "history_mult": 10, "git_bw_Bps": 3.1e6, "git_handshake_s": 1.2},
}


def main(files):
    rows = [r for f in files for r in json.load(open(f))]
    out = {}
    for label, ov in SCENES.items():
        c = dict(COST, **ov)
        agg = defaultdict(list)
        for r in rows:
            agg[(r["profile"], r["strategy"])].append(Meter.wall(r["phases"], c))
        out[label] = {f"{p}|{s}": round(sum(v) / len(v), 1) for (p, s), v in agg.items()}
    return out


if __name__ == "__main__":
    res = main(sys.argv[1:])
    strategies = sorted({k for d in res.values() for k in d})
    labels = list(res)
    print("mean wall seconds per rollout targeting run")
    print(f"{'profile|strategy':40}" + "".join(f"{l[:18]:>20}" for l in labels))
    for k in strategies:
        print(f"{k:40}" + "".join(f"{res[l][k]:>20}" for l in labels))
