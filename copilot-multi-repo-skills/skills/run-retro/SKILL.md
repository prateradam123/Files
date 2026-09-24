---
name: run-retro
description: After a rollout (or when one stalls), turn what happened into small, reviewable improvements to repo facts, the recipe, the skills or the helpers. Use when the user asks "what did we learn", "wrap up the run", "why was that slow", or after multi-repo-rollout finishes.
---

# Run retro

Follow [rules.md](../multi-repo-rollout/rules.md), which also says what `mr` means. `RUN` is
`~/.multi-repo/runs/<run-id>`. You propose changes; you apply only the ones the user approves.

## 1. Gather

```sh
mr report --run <run-id>
mr show --run <run-id> > $RUN/manifest-snapshot.json
```
From the manifest read: `lessons`, answered `questions`, results that were `failed` or `blocked`, the
`recipe.versions` list (every revision and why), and `aborted`. From the report, read the Performance
section: which stage dominated, and which limit was waited on.

## 2. Sort each finding by where it belongs

| Finding | Goes to |
|---|---|
| A fact about one repo: build command, dev branch, PR template, required label, flaky test | `mr facts merge --repo <name> --file <json>`, or the Notes section of `~/.multi-repo/repo-facts/<repo>.md` |
| A build command `discover` detected and `deliver` then ran successfully | the same, with `{"build_source": "confirmed in run <run-id>"}` |
| The check missed or misjudged a layout; apply failed on a pattern; a revision during the run | `~/.multi-repo/recipes/<slug>/` (check.py, apply.py, recipe.md), plus a Trial log row |
| An agent improvised or got stuck because a step was unclear | the skill or agent file that step lives in (in `~/.copilot`) |
| A helper bug or missing guard | `multi-repo-rollout/scripts/` (plus a test, if the developer kit is installed) |
| A limit that was the bottleneck (git, build, api) | `limits` in `~/.multi-repo/config.json` (create the file with just that key) |
| For agent recipes: how pilots and reviewers corrected the agents | examples in the recipe's `## Before and after` |
| A one-off (someone pushed to the destination mid-run) | nothing; say so |

One repo's quirk is a repo fact, not a recipe rule. Only change a recipe or skill when the evidence
covers more than one repo or would clearly recur.

## 3. Write the proposal

Write `$RUN/retro.md`:
- what happened, in 3–5 lines (what went well, what stalled, where time went);
- a table of proposed changes (file, change, evidence, patch file);
- what's not worth making permanent, and why.

Write each change as a unified diff in `$RUN/proposed/<n>-<short-name>.patch` (paths relative to the `~/.copilot` folder or to `~/.multi-repo`). Show the user the list: one line of evidence each, and the diff.

## 4. Apply what's approved

Apply only approved patches (`git apply` from the folder the paths are relative to, or make the same edit by
hand). After skill, agent or script changes, if the developer kit is installed, run its checks (see its
`DEV.md`); they must pass. Runs in progress keep their own recipe
snapshot, so they're unaffected.
