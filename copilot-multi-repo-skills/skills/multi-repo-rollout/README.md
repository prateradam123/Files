# Multi-repo skills for Copilot

Find every branch across many repos that needs a change, turn the change into a recipe, and roll it out
with one PR per branch: approved up front, pilots first, in parallel, without overloading your SCM.

| Skill | Use it to… |
|---|---|
| `repo-scan` | answer a question across many repos, read-only ("which Spring Boot version is each branch on?") |
| `recipe-authoring` | turn a Jira card or request into a tested recipe |
| `multi-repo-rollout` | run a recipe: preview, approve, pilot, PRs, Jira status table |
| `change-verification` | tell "the change broke the build" from "it was already broken" |
| `pr-follow-through` | check CI and reviews on a set of PRs; fix them when asked |
| `run-retro` | turn what a run taught into small, reviewable improvements |

Subagents: `repo-worker` (parallel repo work) and `change-verifier` (independent review of agent-made diffs).

## Install

Unzip into `~/.copilot` (Windows: `%USERPROFILE%\.copilot`): six folders under `skills/`, two files under
`agents/`. Nothing else to set up. The skills use the Jira and Bitbucket/GitHub tools your Copilot already has.
`mr` means `python3 $HOME/.copilot/skills/multi-repo-rollout/scripts/mr.py`; `mr doctor` checks Python 3.10+
and Git, and `mr help` lists the commands.

**Optional speed-up:** with `BITBUCKET_TOKEN` or `GITHUB_TOKEN` in your environment, scripts list repos and
open or check PRs through the API directly: seconds per PR instead of an agent step. The server and its
API address come from each repo's clone URL. A token is only sent to a host that identified itself as that
kind of server. This API code hasn't run against a real server yet, so start with `mr list-repos` (read-only).

**Copilot settings that matter**
- **Auto-approve** the `mr.py` and `git` commands. Otherwise every command every worker runs waits for a click.
- **Check subagents run in parallel:** launch two `repo-worker`s and compare the times of their records. If
  they don't overlap, use the Copilot CLI agent from the agent picker for rollouts.
- **Business/Enterprise:** preview features (agent hooks, the CLI agent) need the "Editor preview features" policy.

## How it works

- **Every branch counts.** Every branch of every repo is scanned, unless the recipe's `Branches:` line
  names branch types and an activity cutoff (`Branches: develop*, release/*; active within 90 days`). Each
  run reports what was excluded by type, age, or as its own PR branches, plus names that look like a missed variant.
- **Cheap first.** Branch tips come from `ls-remote`. Only branches that changed are fetched. The check reads
  only the recipe's `Check paths` files, cached by content. A full worktree is made only where a change is
  needed. A sample of verdicts is re-checked on full checkouts; on any disagreement, everything is.
- **Approval is enforced by Git hooks.** Scripted recipes: you approve exact diffs. Agent recipes: you approve
  targets and approach; each diff is validated, reviewed, and opened as a draft PR. Every commit carries the
  Jira key; no force-push; pilots must go green before the rest unlock.
- **Parallel without overload.** Scripts fan out, and subagents only do judgement. Git, API and build work
  share one set of limits across all processes; a server's "slow down" (HTTP 429) pauses everyone.
- **Recoverable.** `mr diagnose` shows stuck work, `mr progress` rechecks every target on the server, and
  `mr clean` frees disk space.

## Your data

Everything the skills write goes to `~/.multi-repo` (override with `MULTI_REPO_HOME`), created on first use:
`recipes/`, `repo-facts/`, `scans/`, `runs/`, `clones/`, `cache/`, `locks/`. Nothing goes into your projects.
An optional `config.json` there changes limits (`{"limits": {"git": 8, "build": 4}}`) or points
`recipes_dir` at a shared Git checkout so your team shares recipes.

## Update and remove

Replace the six skill folders and two agent files; `~/.multi-repo` is kept. Finish or pause runs first,
because the Git hooks in existing clones call these scripts. To remove, delete those plus `~/.multi-repo`.

## Developer kit

The separate developer kit adds the sandbox (fake repos, Jira and PR server), the tests, two realistic
simulations (a 40-repo organization, and a 150-repo speed benchmark) and the changelog. Unzip it into
`~/.copilot` over this. See its `DEV.md`.
