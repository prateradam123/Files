# Build Flow set for GitHub Copilot

Two skills and four agents. Copy `skills` and `agents` into `%USERPROFILE%\.copilot`.

```
skills\build-flow\     takes a Jira card to draft pull requests
skills\pr-review\      reviews a PR or your branch; Build Flow calls it for its review phase
agents\
  flow-scout.agent.md           answers one question about the code during planning
  flow-plan-reviewer.agent.md   stress-tests a plan before you are asked to approve it
  pr-reviewer.agent.md          reviews a change on one axis
  pr-verifier.agent.md          proves a blocker with a failing test, on request
```

## What needs what

| Piece | Needs |
|---|---|
| `build-flow` | `pr-review`, all four agents, a Jira tool, `gh` or your Bitbucket skill, Python 3.8+, `git`, Maven |
| `pr-review` on its own | `pr-reviewer`, `pr-verifier`, a Jira tool, `gh` or your Bitbucket skill |

## Using them

- `/build-flow ABC-123` builds a card. Details and install check: `skills\build-flow\README.md`.
- `/pr-review review PR 812` reviews someone's PR. It answers in chat, and posts to the PR only when you say which findings to post.
- `/pr-review review my branch` reviews your own branch before you open a PR, and applies fixes when you ask. It never commits.

## The agents

All four are hidden from the agent dropdown (`user-invocable: false`), so they do not clutter it. The skills launch them as subagents.

Each agent file names a model. If an agent fails to start because that model is not in your picker, change or delete the `model:` line in its file. `skills\pr-review\references\models.md` lists the choices and why.

## When subagents run, and how many

| When | Subagents | Skipped for |
|---|---|---|
| Planning | `flow-scout`, usually 3 or 4, in parallel | A card where the one place to change is already known |
| After the plan is written | 1 `flow-plan-reviewer` | Trivial work |
| After each repo's closing tests | 1 `pr-reviewer` per review axis the change needs: usually 3 for a small change, up to 9, plus 1 cross-repo reviewer on the last repo of a multi-repo card | Trivial work |
| When you ask `pr-review` to verify | 1 `pr-verifier` per blocker | |
