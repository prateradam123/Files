# Targeting sandbox

Simulates multi-repo rollout targeting: which (repo, branch) pairs need a change, and which way of finding
them is complete and cheap. See the report doc for results; `targeting/` holds the outputs for the skill.

- `sim/fleet.py`       generate a fleet of real git repos (bare remotes) with realistic branching and traps
- `sim/scm.py`         fleet snapshot, simulated GitHub / Bitbucket front-ends, measured git costs, cost model
- `sim/scenarios.py`   21 remediation scenarios (check code + branch policy + search queries)
- `sim/strategies.py`  13 targeting strategies, incl. the recommended `protocol`
- `sim/run.py`         run strategies x scenarios x SCM profiles on a fleet -> results JSON
- `sim/sweep.py`       re-cost results under other network / repo-weight / history assumptions
- `sim/analysis.py`    tables used in the report
- `targeting/snapshot.py`            the protocol's building block (stdlib Python, works on Windows)
- `targeting/TARGETING.md`           agent guidance for the rollout skill
- `targeting/validate_on_sandbox.py` checks snapshot.py against the simulator's ground truth

Quick start (Python 3.10+, git 2.43+):

    python -m sim.fleet --out fleets/f1 --repos 120 --seed 101
    python -m sim.run --fleet fleets/f1 --out results/f1.json
    python -m sim.sweep results/f1.json
    python -m sim.analysis results/f1.json
    python targeting/validate_on_sandbox.py fleets/f1 work

Fleets are not included (regenerate them from the seeds: f1=101, f2=202, f3=303, f4=505, big=404 with --repos 300);
note f1-f3 and big were generated before the oversized-OpenAPI fix, f4 after it.
Results for all five fleets are in `results/`.
