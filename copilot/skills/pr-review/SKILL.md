---
name: pr-review
description: Reviews a pull request (reviewer mode) or your own branch before a PR exists (author mode). Grounds itself in the Jira card and the history of the touched code, runs one reviewer subagent per review axis the change needs, checks every finding against the code, and returns an evidence-backed review. On request it proves blockers with tests, posts feedback to the PR, or fixes what it found on your branch. Use it when the user asks to review, check or look over a PR, a branch or a diff, gives a PR link or number, asks for a pre-PR check, or asks to re-review after new commits. The build-flow skill also calls it for its review phase.
---

# PR review

You coordinate a review. You ground yourself in the change, decide which review axes it needs, launch one `pr-reviewer` subagent per axis, check what they return against the code, and deliver one review.

The review must be consistent (every change judged on the same axes with the same evidence rules) and right (it finds what would hurt in production and nothing else). Move with purpose: no padding and no re-reading for its own sake, but never skip something the change needs just to finish sooner.

A review with no findings is a valid result. Do not look for things to say.

## Modes

| Mode | When | What comes out |
|---|---|---|
| Reviewer | The user gives a PR link or number. | The review in chat. Posting to the PR only when the user asks. |
| Author | "Review my branch", or there is no PR yet. | The review in chat, with a fix plan. Fixes only when the user asks. |
| Called | Another skill (build-flow) calls you for its review phase. | The checked findings, returned to that skill. See "When build-flow calls you". |

## Tools you use

- Your SCM tool (`gh` on GitHub, the Bitbucket skill on Bitbucket): read the PR, its diff and its comments. Post only on request.
- Git, read-only: the diff, blame and history.
- The Jira tool: the card.
- The build tool: only to verify a blocker or to make an author-mode fix.

Never wait for CI and never use its results (Sonar, coverage, scanners). The review stands on the code.

## 1. Ground

Follow `references/grounding.md`. Run the lookups in parallel where you can. Gather:

1. The change: the PR or branch, the base it goes into, the diff, the head commit.
2. The Jira card: find the key in the PR title, the branch name or the commits. If there is none, ask the user for the key before you go on. If they say there is no card, review against the PR description and say so in the review.
3. The history of the touched lines: who changed them last and why, especially bug fixes and reverts.
4. The repo's setup: test layers, datastore, framework, config locations, and how this repo logs and handles errors.

## 2. Triage

Decide for each of the nine axes in `references/axes.md` whether this change needs it. Keep one line per axis: why it runs, or why it is skipped. The lines go in the Coverage table of the review.

- **Run an axis only when the change has ground that only that axis covers.** Each reviewer is a full read of the change, and two reviewers on the same ground return the same finding twice. A ten-line rule change usually needs `requirements`, `correctness` and `tests`. A new Kafka consumer with a migration may need all nine.
- **A small concern that sits next to another axis becomes a focus line for that axis.** One new log line with an amount in it is a focus line for `requirements`, not a `security` reviewer. An in-process stand-in for a later transport is ground for `correctness` and `failure_handling`, not for `messaging`.
- There is no cap. If the change needs all nine, run all nine.
- Write focus lines for this change: where the risk is, by file and name. Example: "New consumer in `SettlementListener` and a migration on `contract_event`. Look there first." Name the place and what to compare. Do not state the verdict.
- Read `references/known-traps.md` and pass on any trap that applies.

## 3. Review

Launch one `pr-reviewer` subagent per chosen axis, all at once. Give each one, in its prompt:

1. Its axis and that axis's brief, copied from `references/axes.md`.
2. The other axes that are running, so it leaves their ground to them.
3. Your focus lines and any known trap that applies.
4. The grounding facts: repo path, base and head commits, the command that shows the diff, how to read a file at the head commit, the card's criteria, the repo's setup and instruction file, the relevant history.
5. The return format and the severity table from `references/findings.md`.

A subagent knows only what you put in its prompt. If your Copilot runs subagents one after another instead of in parallel, combine related axes into fewer launches and keep the same coverage.

If the `pr-reviewer` agent is not installed, launch a plain subagent and tell it to read `agents/pr-reviewer.agent.md` in the Copilot folder before it starts.

## 4. Check the findings

Follow `references/findings.md`:

1. Evidence check: open the file and confirm the quoted code is there at the head commit. Drop any finding that fails.
2. Merge duplicates across axes.
3. Set each severity by what would happen in production.
4. Go through the reviewers' questions. Answer the ones you can by reading the code, and drop them. Keep a question only when it matters and only a person can answer it.

## 5. Deliver

Write the review in chat in the format of `references/output.md`. Then offer the next steps that apply. Do none of them unasked.

- **Verify the blockers with tests.** Launch one `pr-verifier` subagent per blocker. See `references/findings.md`.
- **Reviewer mode: post to the PR.** Follow `references/posting.md`.
- **Author mode: apply fixes.** The user saying "apply the fixes" after seeing the fix plan is the go-ahead for all of it.
  1. Trivial fixes change no logic: a config value, a missing key, a wording, a rename. Apply them as one batch and show the diff.
  2. Substantive fixes go one at a time: write the test, run it and see it fail, make the fix, run it and see it pass. Do not stop between fixes unless one would change behavior beyond its finding.
  3. A question the user answered with an instruction ("drop the amount from the log") becomes a fix.
  4. Never apply the optional suggestions.
  5. When the fixes are in, read your own diff once against the findings, and run the repo's whole test suite once. Do not launch reviewers again for this.
  6. Never commit and never push.

## Re-review

After every reviewer-mode review, posted or not, write the session file described in `references/posting.md`. A later chat has no memory of this one.

When the user asks again after new commits, read that file and review only what changed since the head commit it names. For each earlier finding, say whether it was addressed. Findings already posted are not posted again.

## Learning

When the user says a finding was useful or noise, record it as `references/learning.md` says. Do not ask them for more.

## When build-flow calls you

Build Flow owns the run, the commits and the user conversation. You supply the review.

1. Do steps 1 to 4 only. In step 1, skip the card and the history: the plan file Build Flow names holds the criteria and decisions, and the calling agent wrote this code. The diff is the range Build Flow gives you.
2. Triage all nine axes and give Build Flow the list with a reason each, before you launch anything. It records the list.
3. Launch the reviewers and check their findings.
4. Return to Build Flow: for each axis that ran, a one-line takeaway; the checked findings, each with axis, severity, file and line, what is wrong and why it matters, and the fix; and the questions that are left, each with why it matters.
5. Do not write the chat review, do not offer next steps, do not fix anything and do not talk to the user. Build Flow fixes findings through its own script.

## Models

The agent files name their models. See `references/models.md`.

## Rules

1. Never approve, request changes or merge. Post only on explicit request, after showing exactly what will be posted.
2. Every finding has real evidence from the change, its production impact and a concrete fix.
3. How this repo already does things is the baseline. A preference is not a finding.
4. Never repeat secrets or personal data that appear in the diff.
5. Say what was not checked.
