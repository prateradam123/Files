---
name: pr-follow-through
description: Check and look after the PRs a rollout opened - CI status, reviews, merges, conflicts - and, only when the user asks, fix failing PRs or bring stale branches up to date. Use for "how are the ENG-123 PRs doing", "which PRs failed CI", "fix the failing ones", "update the Jira card", or any follow-up on a run's PRs.
---

# PR follow-through

**Goal:** Every PR ends merged or deliberately closed, with an accurate status on the ticket, and nothing is changed without the user asking.

**How:** [rules.md](../multi-repo-rollout/rules.md) has the overall goal, the fixed rules, and the room you have for judgement. [commands.md](../multi-repo-rollout/commands.md) says what each `mr` command does and how to check it. Use any tool for reading, editing, building and investigating. The steps below are the default path: adapt them when the situation calls for it, and say why.

Default is **read-only**; fixes
happen only when the user asks in this conversation.

**For a rollout run,** find it with `mr runs` and record what you observe (below). **For any other PRs**
(the user pastes links), do the same checks with your SCM tool and report; there's nothing to record.

## Status (read-only)

1. Record every PR's state and CI:
   - with an API token set: `mr pr-status --run <run-id>` (one pass, rate-limited);
   - otherwise, for each PR, read it with your SCM tool and record it:
     `mr record --run <run-id> --target <id> --status observed --pr-state <open|merged|closed> --ci <passed|failed|pending|none> --evidence "<what the skill returned>"`.
     For many PRs, give `repo-worker` subagents batches of them.
2. Group failures by what failed. For a group, read one CI log and classify it:
   - **caused by our change** (the failure touches what we changed);
   - **already failing** on the destination;
   - **infrastructure**: recommend a re-run;
   - **conflict or stale branch**: recommend updating the branch;
   - **changes requested**: summarize the review.
3. `mr report --run <run-id>`. Show the user a short table with your recommendations. If
   `mr gate --run <run-id> --action jira_comment` allows it, post or edit the card's table (`jira-table.md`
   in the run folder).

Never merge, reply to reviews, re-request reviewers or change Jira status during a check.

## Fixes (only when the user asks)

Once a target's PR exists, the PR is where changes are reviewed. Follow-up commits need the Jira prefix,
the target's own branch, and no force-push; they don't need a new approval. In the target's worktree:

- **Stale branch or conflict:**
  ```sh
  git -C <worktree> fetch -q origin <branch>
  git -C <worktree> merge --no-ff origin/<branch> -m "<KEY> Merge <branch> into <source-branch>"
  ```
  Resolve conflicts keeping both sides' intent. If it isn't obvious, `git -C <worktree> merge --abort`
  and ask. Never rebase pushed commits.
- **A failure caused by our change:** make the smallest fix, then check and build:
  ```sh
  mr check --recipe ~/.multi-repo/runs/<run-id>/recipe.v<N> --dir <worktree>
  mr build --command "<repo-facts build>" --dir <worktree>
  git -C <worktree> add -A && git -C <worktree> commit -m "<KEY> Fix <what>"
  ```
  If several PRs need the same fix, fix the recipe instead (rollout's Change of course). The
  `change-verification` skill explains what each kind of failure means.
- **Requested changes:** only what the user agreed to, committed the same way.

Push with `git -C <worktree> push origin <source-branch>`, then record
`mr record --run <run-id> --target <id> --status pr_updated --pr-url <url> --ci pending --evidence "<what was fixed>"`.

**Merging:** only PRs the user names, only with green checks and approvals. Record `--status merged --pr-state merged`.

**Is it really done?** A merge doesn't prove the destination is compliant. To confirm, fetch the clone
(`mr prepare --url <url> --path ~/.multi-repo/clones/<repo-slug> --branch <branch>`) and run
`mr check --recipe ~/.multi-repo/runs/<run-id>/recipe.v<N> --repo ~/.multi-repo/clones/<repo-slug> --ref origin/<branch>`.
For a whole run, `mr progress --run <run-id>` rechecks every original target at once and reports remediated,
still needs change, regressed, inaccessible or unresolved, separately from PR state.
