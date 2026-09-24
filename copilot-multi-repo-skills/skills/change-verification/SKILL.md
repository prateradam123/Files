---
name: change-verification
description: Decide whether a change is really verified in a repo - run the relevant check and build on it, and when the build fails, run the same build on the unchanged commit to tell "our change broke it" from "it was already broken". Use for "did my change break the build?", "is this failure pre-existing?", or before trusting any automated change; rollout workers use it for every target.
---

# Change verification

**Goal:** A verdict the user can trust: does the change work, and if something fails, is it the change or was it already broken?

**How:** [rules.md](../multi-repo-rollout/rules.md) has the overall goal, the fixed rules, and the room you have for judgement. [commands.md](../multi-repo-rollout/commands.md) says what each `mr` command does and how to check it. Use any tool for reading, editing, building and investigating. The steps below are the default path: adapt them when the situation calls for it, and say why.

Verification never
commits or pushes anything.

## Verified means all of these, on the exact change

1. The relevant check passes: the recipe's `check.py`, or the check the user names.
2. The repo's build command exits 0 on the change. Take it from `mr facts show --repo <name>`, the repo's
   README or CONTRIBUTING, or its wrapper (`./mvnw -q -B verify`, `./gradlew build`).
3. In a rollout: the change is exactly the one that was previewed and approved (the tools compare hashes).

A skipped or timed-out check is not a pass.

## Commands

```sh
mr check --recipe <name or folder> --dir <folder with the change>
mr build --command "<build command>" --dir <folder with the change> --log /tmp/<name>.build.log
mr build --command "<build command>" --clone <repo> --ref <commit without the change> --log /tmp/<name>.baseline.log
```
`build` exits 0 when the build passed and 1 when it failed. In a rollout, when a build fails on the unchanged
branch too, `deliver` first tries the command from the repo's CI config (Jenkinsfile, Bitbucket Pipelines,
GitLab CI, GitHub Actions). If that passes, it becomes the repo's build command automatically. The `--clone/--ref` form runs it on the
unchanged commit in a throwaway worktree, which is removed afterwards. In a rollout,
`mr deliver --run <run-id> --target <id> --dry` does all of this and records the result.

## What a failure means

| What you see | Meaning | What to do |
|---|---|---|
| The check isn't `compliant` after the change | The change doesn't do what it should | Stop; report the check's evidence |
| The build fails with the change, passes without it | The change broke it | Stop; give the failing test or compile error (a few lines) and the likely cause |
| The build fails both ways | Pre-existing failure | Report it as not caused by the change; a rollout asks the user what to do |
| Timeout, network, registry or disk error | Infrastructure | Run once more; if it fails again, report it |
| "command not found", exit 126/127 | The build command is wrong for this machine | Find the right one (the repo's CI config usually shows it); record it with `mr facts set --repo <repo> build='<command>'` |

Report the exact commands, their exit codes and the log paths. Never "fix" a failure by changing what's
being verified unless the user asked for a fix.
