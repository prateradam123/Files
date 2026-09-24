---
name: multi-repo-rollout
description: Run a recipe across many repos in parallel and open one PR per repo branch, with an approval step, a pilot, Jira-prefixed commits and a status table on the Jira card. Use to start, resume, change course on, or stop a rollout, and for ANY request to change code or open PRs across several repos ("do this in all our services", "open PRs for these repos", "continue ENG-123"), even if no recipe is mentioned.
---

# Multi-repo rollout

**Goal:** Every branch in scope that needs the change gets exactly the approved change as its own PR, pilots first. Nothing is missed silently, and nothing is pushed unapproved.

**How:** [rules.md](rules.md) has the overall goal, the fixed rules, and the room you have for judgement. [commands.md](commands.md) says what each `mr` command does and how to check it. Use any tool for reading, editing, building and investigating. The steps below are the default path: adapt them when the situation calls for it, and say why.

You're the coordinator: you talk to the user, run the commands, and launch workers. 

## 1. Start (or resume)

`mr runs` lists runs. If one exists for this Jira key, run `mr report --run <run-id>` and `mr diagnose --run <run-id>`
(what's stuck or stranded, with a fix for each), then continue at the step it's in. On long runs, start a fresh
chat and resume this way: everything is on disk.

- **Recipe:** a name in `~/.multi-repo/recipes/` or [examples/](examples/). With no recipe, use
  `recipe-authoring`. Read its status line for `Mode`, `Risk` and `Validation`.
- **Jira:** read the card with your Jira tool. Permission denied is not "doesn't exist".
- **Repos:** a file of `<name> <clone-url>` lines: from recipe-authoring intake, the user, your SCM tool, or
  `mr list-repos --host <host> --project <KEY> --out <file>` (with an API token).

```sh
mr init --id <KEY>-<slug>-<yyyymmdd> --recipe <name> --jira <KEY> --jira-title "<title>" \
  --jira-url "<url>" --jira-evidence "<skill> returned <KEY>" --repos <file> --request "<the user's words>"
```

## 2. Discover (one command, many repos at once)

```sh
mr discover --run <run-id> --pending --workers 8
mr plan --run <run-id> --strategy "<how you'll run it>"
```
`discover` prints a summary (counts, problems, and a file with the details). It scans every repo the cheap way (branch tips, then only the branches and files the recipe needs,
cached). Only where a change is needed does it build a local worktree, and for scripted recipes a diff.
Which branches: the recipe's `Branches:` line (branch types and an activity cutoff). With no line, every branch
in every repo is in scope. The default branch is exempt from the cutoff. `--pattern`/`--days` override the
recipe for one run. Add `--via api` (with an API token)
and exact `Check paths`.

`plan` writes `~/.multi-repo/runs/<run-id>/preview.md` and lists:
- `pending_repos`: discover again;
- `undecided` and `needs_edit`: work for `repo-worker`s (step 3);
- `needs_approval`: ready for the user.

**More than about 50 repos:** run `discover ... --limit 30`, then `plan ... --allow-missing`, and start
step 4 with those while discovery continues.

**Review the coverage with the user.** The summary's `coverage` shows the branch types and cutoff used, and
how many branches were scanned or excluded by type, by age, or as our own rollout branches. Look closely at
`possible_variants`: branches excluded by type whose names look like an included type (`dev`, `devel`,
`feature/develop-sync`). If any should be in scope, widen the recipe's `Branches:` line, `revise`, and rediscover.
`audit` reports a sample of sparse verdicts re-checked on full checkouts. If they disagreed, everything was
already re-checked in full; tell the user the recipe's `Check paths` are incomplete.

## 3. Workers for judgement (in parallel)

For `undecided` and `needs_edit` items, and later for agent-recipe `change` and `review` tasks, launch
`repo-worker` subagents (and `change-verifier` for `review`), 4–6 at once. Tell each:
- the run ID;
- which tasks to take (`decide`, `edit`, `change`, `open_pr`; or `review`);
- how many items to do before returning: 1 for heavy edits, 3–5 for light, similar ones;
- any answers or examples that apply to everyone.

They pull their own work with `mr take`, so nothing needs splitting up front. Run `mr plan` again when they return.

## 4. Approval

Show the user `preview.md`: counts, each diff and its targets, what's unresolved and why, and open
questions with your recommendation (`mr questions --run <run-id>`). Record answers with
`mr answer --run <run-id> --question <qN> --answer "..." --evidence "<their words>"`. `mr questions` groups similar
questions. Answer a whole group at once with `--question <all its ids>`. If the question lists
`choices`, add `--choice <one>`: `draft` opens that one target as a draft despite the recorded failure (and says
so in the report and PR), `skip` leaves it out, `retry` tries again after a fix.

Offer to validate while they read. Builds run repo code, so ask first. Then
`mr deliver --run <run-id> --dry --target <ids...>` runs checks and builds with nothing committed, and
surfaces repos whose build is already broken.

Ask for:
- **Pilots:** `plan` prints `suggested_pilots`: one per kind of diff, preferring targets that already
  validated. The rest wait until the pilots' PRs have green CI.
- **Pause after the pilot?** Then approve only the pilots now.
- **PRs:** ready or draft (agent recipes default to draft), and whether to post the Jira table.

```sh
mr approve --run <run-id> --pilot <id> <id> --evidence "<their reply, verbatim>"
```
Other options: `--targets <ids>` for some only, `--pr-mode draft|ready`, `--no-jira`, and
`--max-pending-ci <N>`: at most N targets in flight toward CI at once (pushed, awaiting a PR, or waiting on
CI), enforced even when several callers deliver at the same time. Approve again
whenever `plan` or `report` shows new `needs_approval`.

## 5. Run

**Scripted recipes:**
```sh
mr take --run <run-id> --worker coordinator --task deliver --limit 50   # claims what's ready (within the CI window)
mr deliver --run <run-id> --worker coordinator --target <ids from above> --workers 3
mr open-prs --run <run-id>          # with an API token; otherwise repo-workers take the open_pr tasks
```
**Agent recipes:** `repo-worker`s take `change` tasks and `change-verifier`s take `review` tasks, in parallel.
Then deliver and open PRs as above. Nothing is pushed until a diff is both validated and reviewed.

Refresh CI with `mr pr-status --run <run-id>` (or your SCM tool plus
`mr record --run <run-id> --target <id> --status observed --ci <state> --evidence "..."`).
A delivery that doesn't fit the CI window returns `not_ready` and waits for the next round.

After each round, `mr report --run <run-id>` lists stopped targets, open questions and `needs_approval`:
- **"Destination moved":** `mr discover --run <run-id> --repo <name>`, then `mr plan`. An identical diff keeps
  its approval (`mr retry --run <run-id> --target <id> --evidence "rediscovered"`); the build always reruns on the
  new base. An uncommitted agent edit is replayed; if it conflicts, nothing is discarded. The target stops
  with a question and a checkpoint file.
- **Questions:** ask the user in one batch; `mr answer` puts the target back in the queue.
- **`pushed_without_pr`:** run `mr open-prs --run <run-id>` (it exits 1 if any PR could not be opened; read why).
- **"The server refused branch …":** your Bitbucket's branch rules. Change the run's format once:
  `mr branches --run <run-id> --format 'feature/{key}-{recipe}'`, then `mr plan` and `mr retry` those targets.
- **Two failures of the same kind** (`report` groups stopped targets by reason): stop and fix the recipe
  (below), not one repo at a time.

**Pilots:** when their CI is green the rest unlock by themselves. Show the pilot PR links. For agent recipes,
add the pilot diffs to the recipe as examples first. A repo with no CI: `mr approve --waive-ci "<why>"`,
with the user's agreement.

## 6. Report and Jira

`mr report --run <run-id>` writes `report.md` and `jira-table.md`. If
`mr gate --run <run-id> --action jira_comment` allows it, post or edit the table comment with your Jira tool:
after the pilot, when all PRs are open, and on request. Tell the user:
- the PR links;
- what's unresolved and why;
- the Performance section's bottleneck, if a limit should change;
- what's next: `pr-follow-through`, then `mr progress --run <run-id>` (every original target rechecked on the
  server: remediated, still needs change, regressed), then `run-retro`. `mr clean --run <run-id>` frees the
  worktrees of finished targets.

## Change of course

**Fix the recipe mid-run:** edit it in `~/.multi-repo/recipes/<name>`, trial it (recipe-authoring), then
`mr revise --run <run-id> --recipe <name> --evidence "<what was wrong>"`, `mr discover --run <run-id> --pending ...`
and `mr plan`. Unchanged scripted diffs stay approved; changed ones need approval, and those with a PR get
a follow-up commit.

**More repos:** `mr scope --run <run-id> --add <file> --evidence "<their words>"`, then `mr discover --run <run-id> --pending`
and `mr plan`. The new targets need approval like any other.

**The check got one target wrong** (you're sure after reading the repo): show the user the evidence. Only
with their agreement run `mr override --run <run-id> --target <id> --status <needs_change|compliant|not_applicable>
--evidence "<their words>"`, then `mr plan`. It always shows in the report. If several targets are wrong,
fix the recipe instead (above).

**Something the rollout can't do** (create a branch, change repo or CI settings, land several repos at
once): tell the user in one line and ask whether they'll do it or you should, with your tools.

**Stop targets (only when the user asks):**
`mr abort --run <run-id> --targets <ids> --evidence "<their words>"`. Then
`mr close-prs --run <run-id> --target <ids> --comment "<reason>"`, or close them with your SCM tool after
`mr gate --run <run-id> --target <id> --action close_pr`. Ask first if a PR has human reviews.
