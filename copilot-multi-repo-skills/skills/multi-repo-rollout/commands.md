# What the `mr` commands do

A command is a tool, not a black box. Each card below says what it does, what it guarantees, what it
doesn't do, how to check its result yourself, and when your own method fits better. `mr <command> --help`
lists every option. For how `mr` fits in beside your other tools, see [rules.md](rules.md).

## `scan`: answer one question for many repos, read-only

**Use it for:** "which branches have X / what version of Y is each branch on", with no run and no changes.

**Does:**
1. Lists every branch of every repo with `git ls-remote` (no clone).
2. Keeps the branches that match the recipe's `Branches:` types (all, if none) and activity cutoff. The
   default branch is exempt from the cutoff.
3. Fetches only branches that changed since last time.
4. Runs the check script on each branch, seeing only the `Check paths` files, and caches results by file
   content.

**Guarantees:**
- every excluded branch is counted, with its reason (type, age, rollout branch) plus possible variants;
- a sample of "compliant/not applicable" verdicts is re-checked on full checkouts, and everything is
  redone in full on any disagreement;
- unreachable repos are listed, never dropped.

**Doesn't:**
- see files outside `Check paths` (unless the audit or `--full` switches to full checkouts);
- look inside submodules or at LFS content (both come back `unknown`);
- look at history, only each branch's latest commit;
- see anything outside Git.

**Check it:** `~/.multi-repo/scans/<name>/results.json` has the verdict and evidence per branch. Reproduce
one with `git -C ~/.multi-repo/clones/<repo-slug> show origin/<branch>:<file>`, or run
`mr check --recipe <recipe> --repo ~/.multi-repo/clones/<repo-slug> --ref origin/<branch>`.

**Your own method instead:** questions about history ("when was this introduced?"), comparing files
across repos, or data outside Git. Use `git log`, the shell and your SCM tools, and say so.

## `discover` + `plan`: what a rollout would change

**Use it for:** the start of a rollout, after `init`.

**Does:**
- the same branch selection, checks, coverage and audit as `scan`, on the run's repos;
- where a change is needed, creates a worktree on its own source branch (default
  `feature/<KEY>-<recipe>`, suffixed for non-dev branches);
- for scripted recipes, runs `apply.py` and captures the exact diff;
- `plan` builds the plan and `preview.md`, and merges repo facts.

**Guarantees:**
- nothing leaves the machine;
- worktrees exist only for targets needing the change;
- an uncommitted edit on a moved destination is replayed or redone, never silently lost;
- targets whose branch left the selection are retired, unless they already have work.

**Doesn't:** decide `maybe`/`unknown` targets (that's workers' `decide` task), or make agent edits.

**Check it:** `~/.multi-repo/runs/<run>/discovery/<repo>.json` per repo; `preview.md` for the diffs;
`git -C <worktree> diff` for any single target.

**Your own method instead:** investigating one odd repo. Read it with Git; fix a wrong verdict with
`mr decide`, or ask the user for an `mr override`.

## `approve`, `gate`: the approval boundary

**Does:**
- `approve` records the user's words for targets (all that need it, or `--targets`), plus pilots, PR mode
  and CI window;
- `gate` answers "may this happen now?" and never changes anything.

**Guarantees:** the Git hooks call the gate on every commit and push, whether it's `mr deliver` or plain `git`.

**Doesn't:** create PRs or post to Jira. Call `gate` before you do those with your tools.

## `take`, `record`, `ask`, `answer`: shared state

**Does:**
- `take` hands out the next task per target: `decide`, `edit`, `change`, `review`, `deliver`, `open_pr`.
  With `--worker` it claims them; claims expire after 2 hours, and `--renew` extends yours.
- `record` stores a result, reading hashes from the worktree itself.
- `ask` stops one target with a question; `answer` records the user's reply and puts the target back in
  the queue.

**Guarantees:** recording a PR re-checks the pushed commits (ticket key, approved content); a violation
blocks the target with a question.

**Check it:** `mr show --run <run>` (the full ledger) or `mr report --run <run>` (the summary).

## `deliver`: check, build, commit, push many targets

**Does**, per target:
1. Checks the gate.
2. Runs the recipe check and the build. On failure it retries the build on the unchanged branch, and tries
   the repo's CI command before blaming the branch.
3. Commits with the ticket key and pushes the source branch.

`--dry` stops after the build (validation only); `--ready` picks every target ready now, within the CI
window. For agent recipes, a target must be validated and reviewed on its current base before it can push.

**Guarantees:**
- "not ready" returns without recording anything;
- real problems are recorded with their reason;
- the rollout's hooks are put back if a build switched them off.

**Doesn't:** open PRs (`open-prs` or your SCM tool does), or fix a failing change.

**Check it:** `git -C <worktree> log`, the build log path given in the result, `mr report`.

**Your own method instead:** a repo that needs a special build. Run it yourself, and record the command with
`mr facts set --repo <repo> build='...'`.

## `open-prs`, `pr-status`, `close-prs`: PRs through the API (needs a token)

**Does:** finds or opens each pushed target's PR, spacing requests out; records PR state and CI for every
PR; closes PRs of aborted targets.

**Guarantees:**
- looks for an existing PR before creating one, so reruns never duplicate;
- a server's rate limit pauses every process;
- exits 1 if any PR couldn't be handled.

**Your own method instead:** without a token, use your SCM tool, then record the result with
`mr record ... --status pr_opened --pr-url <url>`.

## `progress`, `diagnose`, `clean`, `report`: status and recovery

- **`progress`** rechecks every original target on the server now: remediated, still needs change,
  regressed. This is separate from PR state; a merged PR isn't proof.
- **`diagnose`** lists stuck or stranded work (missing worktrees, pushes without PRs, unowned failures),
  with a fix for each.
- **`clean`** removes worktrees of finished targets and clones unused for N days.
- **`report`** writes `report.md` and `jira-table.md`, and prints a short summary with the next steps.

## Adjusting a run

- **`scope --add <file>`:** add repos to a running rollout. They're discovered with `discover --pending`,
  and their targets need approval like any other.
- **`override`:** correct the check's verdict for one target, **only with the user's approval**
  (`--evidence` = their words). It always shows in the report.
- **`branches --format`:** rename every unpushed source branch, e.g. when the server's branch rules reject
  the naming.
- **`revise`:** a corrected recipe mid-run. Unchanged scripted diffs stay approved.
- **`retry`, `abort`:** put a stopped target back in the queue, or stop targets for good.
- **`facts show|set|merge|list`:** per-repo facts (dev branch, build command, PR conventions) that every run reuses.
