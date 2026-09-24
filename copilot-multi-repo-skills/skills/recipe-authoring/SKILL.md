---
name: recipe-authoring
description: Turn a change request or Jira card into a tested recipe (recipe.md + check.py + optional apply.py) that multi-repo-rollout can run across many repos. Use whenever someone wants to write, fix, trial or reuse a recipe, or says things like "make a recipe from ENG-123", "we need to change X in all our services", "turn this card into something we can roll out".
---

# Recipe authoring

**Goal:** A recipe that finds exactly the branches that need the change (no misses, no false alarms) and makes exactly the change the user wants, proven on real repos before any rollout.

**How:** [rules.md](../multi-repo-rollout/rules.md) has the overall goal, the fixed rules, and the room you have for judgement. [commands.md](../multi-repo-rollout/commands.md) says what each `mr` command does and how to check it. Use any tool for reading, editing, building and investigating. The steps below are the default path: adapt them when the situation calls for it, and say why.

Output: a folder `~/.multi-repo/recipes/<slug>/` that passes a trial on real repos, plus the request that
starts the rollout.
Template: [recipe-template.md](../multi-repo-rollout/examples/recipe-template.md). A complete example:
[disable-feature-builds](../multi-repo-rollout/examples/disable-feature-builds/recipe.md).

A recipe is reusable: the repo list, Jira key and branches belong to the run, never to the recipe.

## 1. Read the card (skip if there's no card)

Read it with your Jira tool. Extract the outcome in one sentence, the acceptance criteria as written, the
repo list if there is one, and anything that limits scope. If it lists repos, get their clone URLs from the
SCM tool (or `mr list-repos` with an API token) and write them one per line as `<name> <clone-url>` to
`~/.multi-repo/intake/<KEY>-repos.txt`. Never guess URLs. Show the user what you extracted and ask only
about gaps.

## 2. Reuse before writing

Look in `~/.multi-repo/recipes/` and in the examples folder. If something already does this, say so and
skip to step 6. If something is close, copy it and edit it.

## 3. Find out what's out there

Before writing the check, see how the repos actually differ: `mr scan` (see the `repo-scan` skill) with a
quick script that reports the relevant file or version as `value`. Pick trial repos from the report:
- one that clearly needs the change;
- one with a different layout or build tool;
- one that should *not* change.

## 4. Write the folder

`mkdir -p ~/.multi-repo/recipes/<slug>` and copy the template in as `recipe.md`. Fill every section. The
status line:
- `Mode: scripted` when `apply.py` can make the change the same way everywhere. The user approves exact
  diffs. Prefer this whenever it's possible: it's the fastest and most predictable.
- `Mode: agent` when the change needs judgement per repo (code migrations). The user approves targets and
  approach; each diff is checked, fully built and independently reviewed before its PR. A hybrid (a tool
  like OpenRewrite does most of it, an agent the rest) is `agent`, with the tool in `apply.py`.
- `Risk:` `low` (one config value), `medium` (a scripted code or dependency change), `high` (agent edits).
- `Validation:` `check` (no local build; the PR's CI runs it; fine for config flips), `compile`, or `full`.
- A `Branches:` line: ask the user which branch types matter and how recent they must be, for example
  `Branches: develop*, release/*, main, master; active within 90 days`. With no line, every branch is scanned.
  Patterns are case-insensitive globs over the full name (`*develop*` matches anywhere); prefer wide patterns,
  and check the scan's `possible_variants`.
- A `Check paths:` line naming every file `check.py` reads (globs allowed). Discovery downloads and caches
  only these, which is what makes hundreds of repos cheap.

**check.py (required).** It runs in a checkout of one commit and prints one JSON line
`{"status": "...", "evidence": "...", "value": "optional"}`.
- `compliant`: already done. `needs_change`: the recipe applies and the change is safe. `not_applicable`:
  the thing doesn't exist here by design. `unknown`: anything else, and never changed.
- `maybe` means a worker should decide (agent recipes: "uses the old client API").
- Evaluate every matching file (every module of a monorepo), not the first one found. A missing file is
  `unknown` unless it truly means "not applicable": misses are worse than questions.
- Rule repos out cheaply first (`pom.xml` doesn't mention the group ID → `not_applicable`) before anything
  slow like `dependency:tree`.
- Use real parsers, standard library only.
- It must report `compliant` after a correct change: the same check decides discovery, verifies the edit,
  and proves the destination is done.
- Environment: `TARGET_REPO`, `TARGET_BRANCH`, `RECIPE_DIR`.

**apply.py (scripted recipes).** It edits the worktree in place.
- Idempotent: a second run changes nothing and exits 0 with `{"changed": false}`.
- Smallest diff: no reformatting.
- On an unsupported layout, exit 1 with the reason in `notes`.
- Helpers can sit next to it, as `config_edit.py` does in the example.

**Agent recipes.** `## How the change is made` must be precise enough that two agents produce the same
diff. Even when the change can't be scripted, make the *outcome* checkable.

## 5. Trial on real repos

For each trial repo: `mr prepare --url <url> --path ~/.multi-repo/clones/<repo-slug>`, then
`mr trial --recipe <slug> --clone ~/.multi-repo/clones/<repo-slug> --branch <branch>`.
It checks, applies and checks again in a throwaway copy, and prints the real diff. A `warning` means
`check.py` reads a file missing from `Check paths`.

With no `apply.py`, make the edit yourself in a scratch worktree
(`git -C <clone> worktree add --detach /tmp/trial-<slug> origin/<branch>`), run
`mr check --recipe <slug> --dir /tmp/trial-<slug>`, look at `git -C /tmp/trial-<slug> diff`, then remove the worktree.

Show the user each diff and the before/after statuses, and iterate until they're right. Paste one real
diff into `## Before and after`, fill the Trial log, and set `Status: trialed`.

## 6. Hand off

Give the user the request that starts it, filled in:

> Use multi-repo-rollout with recipe `<slug>`, Jira `<KEY>`, repos from `~/.multi-repo/intake/<KEY>-repos.txt`.

Don't start the rollout yourself.
