"""Check snapshot.py against the simulator's ground truth on a sandbox fleet:
branch eligibility (policy=active) and one end-to-end check (S04 legacy-retry key)."""
import json, os, subprocess, sys, time, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sim.scm import Fleet
from sim.strategies import eligible_branches, ground_truth
from sim.scenarios import SCENARIOS

fleet_dir, work = sys.argv[1], sys.argv[2]
fl = Fleet(fleet_dir)
repos = [n for n, m in fl.repos.items() if not m.get("archived") and not m.get("fork") and m["kind"] != "internal-lib"]
shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
lst = os.path.join(work, "repos.txt")
open(lst, "w").write("\n".join("file://" + str(fl.remote(r)) for r in repos))
S = [sys.executable, os.path.join(os.path.dirname(__file__), "snapshot.py")]
t0 = time.time()
out = subprocess.run(S + ["snapshot-many", lst, work, "--workers", "8"], capture_output=True, text=True).stdout
modes = {}
for line in out.splitlines():
    r = json.loads(line); modes[r.get("mode", "ERROR " + r.get("error", ""))] = modes.get(r.get("mode", "ERROR"), 0) + 1
print("snapshots", len(repos), "in", round(time.time() - t0, 1), "s; modes:", modes)

elig_mismatch = 0
for r in repos:
    got = {e["branch"] for e in json.loads(subprocess.run(S + ["branches", os.path.join(work, r), "--policy", "active"], capture_output=True, text=True).stdout)}
    want = {b for b in eligible_branches(fl.refs[r], "active", fl.now) if b in fl.refs[r]}
    if got != want:
        elig_mismatch += 1
        print("  eligibility differs", r, "only-snapshot:", got - want, "only-truth:", want - got)
print("eligibility: repos matching truth", len(repos) - elig_mismatch, "/", len(repos))

truth = ground_truth(fl, SCENARIOS["S04"])
found = set()
for r in repos:
    p = subprocess.run(S + ["grep", os.path.join(work, r), "--policy", "active", "--glob", "**/application*.yml",
                            "--glob", "**/application*.yaml", "--glob", "**/application*.properties", "--regex", r"legacy-retry\s*[:=]"],
                       capture_output=True, text=True)
    for h in json.loads(p.stdout or "[]"):
        found.add((r, h["branch"]))
print("S04 legacy-retry via snapshot.py grep: truth", len(truth), "found", len(found), "missed", len(truth - found), "extra", len(found - truth))
du = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(work) for f in fs)
print("disk used by all snapshots: %.1f MB" % (du / 1e6))
