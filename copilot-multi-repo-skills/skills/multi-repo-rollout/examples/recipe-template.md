# <What changes, in plain words>

Status: draft | trialed · Mode: scripted | agent · Risk: low | medium | high · Validation: check | compile | full · Toolkit: 2.x
Branches: all; active within <N> days   (or name branch types: develop*, release/*, main, master; active within 90 days)
Check paths: <every file check.py reads, comma-separated; globs allowed, e.g. **/application*.yml>

Mode: scripted = apply.py makes the change and the user approves exact diffs; agent = judgement per repo:
the user approves targets and approach, and each diff is checked, built (full) and reviewed before its PR.
Risk: low = one config value; medium = script-made code or dependency change; high = agent edits or
behavior change. Rollout uses it for pilot size and independent review.
Validation: the local build before pushing. `check` = none (the PR's CI runs it); `compile`; `full` (default).
Check paths: discovery downloads, checks and caches only these files. Leave the line out if the check
needs the whole repo (it then runs on a full checkout).
Keep run details out of this file: the repo list, Jira key, and branches come from the run.

## Outcome
One paragraph: what changes, why it matters, and what "done" means for one repo branch.

## Where it applies
- Files and patterns that carry the setting/code:
- Branches: the development base, plus which other branches (e.g. `feature/*` active in the last 30 days)
- Not applicable when:
- Unknown (stop and ask) when:

## Before and after
A real example from the trial, trimmed to the relevant lines.

```diff
- "featureBuilds": true
+ "featureBuilds": false
```

Counterexample, something that looks similar but must NOT change:

## How check.py decides
- maybe (agent recipes: a worker decides):
- compliant:
- needs_change:
- not_applicable:
- unknown:

## How the change is made
apply.py (if deterministic):
What an agent does when apply.py can't handle a repo:
Stop and ask when:
Never touch:

## Validation
Commands (use the repo's build from repo-facts), and what the diff must not contain.

## Trial log
| Date | Repo @ branch | Check | Diff reviewed | Notes |
|---|---|---|---|---|
