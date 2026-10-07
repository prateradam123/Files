# The two reviews

Both reviews are done by subagents that did not write what they review. You launch them, check what they return, act on it, and record the result with the script.

## The plan review

It runs after `plan-submit`, before the user sees the plan, for everything but trivial work. `NEXT:` tells you when, and at which depth: `light` when the plan's risk is low, `full` otherwise.

1. Launch one `flow-plan-reviewer` subagent. Do not record the launch with `agent-start`: `plan-reviewed` is its record. Put in its prompt:
   - the path of your plan JSON file;
   - the card's title, description and acceptance criteria;
   - each question you asked the user, with its options and their answer, and the defaults they accepted;
   - the workspace folder, and the instruction file paths for the repos in the plan;
   - the path of `reference/plan-format.md`;
   - the depth.
2. For each issue it returns, decide:
   - **It stands:** fix the plan JSON.
   - **It is wrong, or the plan is right as it is:** keep the plan, and write down why in one line.
   - **It is really a choice for the user:** record it with `ask`, write your recommended option into the plan as a default, and put the question to the user with the plan. Its outcome is `asked`.
3. If you changed the JSON, run `plan-submit` again. `NEXT:` will still mention the reviewer, because the script cannot see that it ran. It does not need to run again.
4. Write the result as a JSON file in the working folder, as `<CARD>.plan-review.json`:

```json
{
  "issues": [
    { "text": "Slice 2 has no check that can fail: OutboxWriterTest is added in slice 3.", "outcome": "fixed" },
    { "text": "The rollout does not say how to back out.", "outcome": "kept", "note": "The switch in step 1 is the way back, and step 3 says so." }
  ]
}
```

5. Run `flow <CARD> plan-reviewed --file <path>`. Then show the user the plan.

Rules the script enforces:

- `issues` is a list. Use an empty list when the reviewer found nothing.
- Each issue has `text` and an `outcome` of `fixed`, `kept` or `asked`. `kept` needs a `note`. `asked` needs an open question recorded with `ask`.
- An issue marked `fixed` requires that the plan was submitted again after the first `plan-submit`.
- The user cannot be recorded as approving a plan that was due a review and did not get one.

One review is enough. If the user then asks for changes to the plan, submit it again and ask them again. It is not reviewed a second time.

## The code review

It runs once per repo, after the closing tests and before the full suite, for everything but trivial work. When a build slice is added later, it runs again on the new code only.

### Steps

1. Run `flow <CARD> review-start`. It prints the diff to review, the plan file, the instruction files and the axis ids.
2. Open the `pr-review` skill and follow its section "When build-flow calls you". You already know this change, so skip the card lookup and the history lookup. Write down your focus lines: where the risk is in this change, by file and name.
3. Decide for each axis whether this change needs it, and write the decisions to a JSON file in the working folder, as `<CARD>.review-plan-<repo>.json`:

```json
{
  "axes": {
    "requirements":     { "run": true,  "why": "Four criteria and two decisions to check against the code." },
    "correctness":      { "run": true,  "why": "New branch on approver id, with a null case." },
    "tests":            { "run": true,  "why": "Two new behaviors, each needs a test that reaches it." },
    "failure_handling": { "run": true,  "why": "New publish call that can fail." },
    "security":         { "run": false, "why": "No input, access check or external I/O changes." },
    "compatibility":    { "run": true,  "why": "New config key and a new event." },
    "design":           { "run": false, "why": "No new structure. Follows AuditWriter." },
    "data_access":      { "run": false, "why": "No entity, query or migration changes." },
    "messaging":        { "run": true,  "why": "New event on contract.vendor-status.v1." }
  }
}
```

4. Run `flow <CARD> review-plan --file <path>`. That records the reviewers and shows them on the monitor page. Do not record them with `agent-start` as well.
5. Launch one `pr-reviewer` subagent per axis that runs, all at once. Write each prompt as step 3 of the pr-review skill says. The facts to give are the ones `review-start` printed: the repo path, the diff command, the readable plan file (it holds the criteria and decisions), and the instruction file paths. Add the head commit and one line on the repo's test layers.
6. Check what comes back, as the pr-review skill's `references/findings.md` says: confirm each quoted line exists, merge duplicates, drop findings that are only preference.
7. Deal with the reviewers' questions before the findings. A question is yours to answer first:
   - **You can answer it by reading the code or the plan:** answer it, and drop it. If the answer shows a real problem, it is now a finding.
   - **The plan already decided it:** drop it, unless the reviewer shows a consequence the user was not shown when they decided. Then it goes to the user.
   - **Only the user can answer it, and it matters for this card:** it becomes a finding with severity `question` and outcome `asked`.
   - **It is real but belongs to a later card:** it becomes a finding with severity `question` and outcome `left`, with the reason. The script lists it for the user at the end.
8. Give every finding that is left one outcome:

| Outcome | When | What you do |
|---|---|---|
| `fixed` | The finding stands and the fix does not change what the feature does. | Edit, `verify`, `commit`. One commit per fix. |
| `asked` | The fix would change behavior, the contract or the plan, or the finding is a question only the user can answer. | Record it with `ask`, put it to the user, act on the answer. A plan change goes through `plan-revise`. |
| `rejected` | The finding is wrong. | Say why in one line. A reason is required. |
| `left` | It is a minor or a question, and not for a change now. | Say why in one line. A blocker or a major cannot be left. |

   A finding has one `axis`. When several axes reported it, name the one it fits best. A problem that was there before this change is not a finding: record it with `noticed`. A reviewer's optional suggestion is not recorded: mention it to the user at the end if it is worth their time. A finding whose fix is plan text only (the rollout, a risk line) is `asked`: the user agrees, then `plan-revise`.

9. Write the report as a JSON file in the working folder, as `<CARD>.review-<repo>.json`:

```json
{
  "axes": {
    "requirements": "All four criteria are implemented and both decisions hold.",
    "correctness": "One real gap: a null approver passes the creator check.",
    "tests": "Each new behavior has a test that reaches it.",
    "failure_handling": "A failed publish is logged once with the contract id.",
    "compatibility": "The config key is in all three environment files.",
    "messaging": "The event is written in the approval transaction."
  },
  "findings": [
    { "axis": "correctness", "severity": "major", "where": "src/main/java/com/acme/approval/ApprovalService.java:41",
      "text": "A null approver id passes the creator check, so an unauthenticated call can approve.",
      "outcome": "fixed" },
    { "axis": "design", "severity": "minor", "where": "src/main/java/com/acme/approval/VendorStatusPublisher.java:12",
      "text": "The publisher builds the event inline where AuditWriter uses a factory method.",
      "outcome": "left", "note": "One call site. A factory would be abstraction without a second case." }
  ],
  "dropped": 1
}
```

   `dropped` counts the findings and questions you dropped in steps 6 and 7. Merged duplicates do not count.

10. Run `flow <CARD> review-report --file <path>`.

### When the user's decision on a finding changes the plan

Finish the review first, then change the plan.

1. Record the finding as `asked`, with what the user decided in the `note`.
2. Run `review-report`. The script refuses to add a build slice while a review is open.
3. Run `plan-revise` with the new slice and the user's words. The repo goes back to its build stage.
4. The new slice then gets its own self-check and its own review, on the new code only. In that second review, run only the axes the new code needs. An axis that ran the first time does not run again unless the new code touches its ground.

When a decision reverses a criterion that a done slice proves, reword the criterion, take its id out of the done slice's `proves`, and put it in the new slice's `proves`. That is the one thing a done slice may change.

### Rules the script enforces

- `review-plan` needs all nine axes, each with `run` and a one-line `why`. At least one must run.
- `review-report` needs a one-line takeaway for every axis that ran.
- `findings` is a list. Use an empty list when no finding stood.
- Each finding has `axis`, `severity` (`blocker`, `major`, `minor` or `question`), `text` of at most 300 characters saying what is wrong and why it matters, and an `outcome`. `where` is the file and line. For a finding about plan text, leave `where` empty.
- Every outcome but `fixed` needs a `note`.
- A finding marked `fixed` requires a fix commit made since the review started.
- Nothing is left uncommitted.

### More than one repo

For the last repo of a card with several repos, `review-start` also asks for the axis `cross_repo`. Add it to the axes file as a tenth key with `"run": true`, and launch it in the same batch as the other reviewers. Give that reviewer the path, head commit, diff command and instruction file of every repo, and the cross-repo brief at the end of the pr-review skill's `references/axes.md`. Its findings go in this last repo's report under the axis `cross_repo`. A finding there may need a fix in a repo that is already built: make it with `verify --repo <name>` and `commit --repo <name>`.

### What the review is not

- It is not a second self-check. The self-check is you against a fixed list. The review is other readers against the change.
- It does not wait for CI and does not use CI results.
- It does not replace the user's review of the PR.
