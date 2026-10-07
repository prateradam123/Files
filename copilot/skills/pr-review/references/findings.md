# Findings

## What a reviewer returns

Put this format in every reviewer's prompt.

```
Axis: <axis id>
Takeaway: <one line: what you concluded about this change on your axis>
Findings:
- severity: <blocker | major | minor>
  where: <file>:<line>
  evidence: `<the code, quoted exactly as it is at the head commit>`
  impact: <what goes wrong in production>
  fix: <the concrete change, modeled on this repo's own code where possible>
Questions (at most two):
- <something that would be a blocker or a major if true, that you could not settle by reading, and why it matters>
Checked and fine: <what you looked at and found in order, short>
Suggestions (design axis only, at most 3): <idea | sketch | payoff>
Not checked: <what you did not get to>
```

"Findings: none" is a valid return. So is "Questions: none".

Give the severity table below to every reviewer with this format.

## Checking what comes back (step 4)

For every finding:

1. **Evidence check.** Open the file at the head commit. If the quoted code is not at that place, drop the finding and count it as dropped.
2. **In scope.** The finding must be in the diff or clearly caused by it. A problem that was already there goes under "noticed, not introduced by this change", at most two, and only if serious.
3. **Merge duplicates.** When several axes report the same thing, keep the clearest one under the axis it fits best, and add "also raised by" with the other axes. When their fixes differ, take the one closest to this repo's own code.
4. **No preference findings.** If the only argument is taste, drop it.
5. **Missing tests.** A missing test goes into the fix of the code finding it would expose. Keep a separate tests finding only for behavior that is right but not proven.
6. **Questions.** Answer the ones you can by reading the code, and drop them. A question that is left must matter and need a person.

"Dropped" in the review's count means findings and questions removed in steps 1, 4 and 6. Merged duplicates are not dropped.

## Severity, by what would happen in production

| Severity | Meaning |
|---|---|
| Blocker | It will break production, lose or corrupt data, open a security hole, or break a consumer. |
| Major | A likely bug or incident, or a real gap: new logic with no test, a config key missing for an environment. |
| Minor | A real but small risk, or an inconsistency with how the repo does things. |
| Question | It matters and could not be proven either way. |

Something with no production consequence is not a finding.

Rules for close calls:

- The consequence decides, not the example. A config key missing for one environment is a major when the fallback is harmless, and a blocker when the fallback breaks that environment.
- When the harm waits on a later card (a stand-in that a real transport will replace), rate it as if that card had landed, and say so in the finding.
- When reviewers rate the same thing differently, rate it yourself by the consequence.

## Proving a blocker with a test (on request)

Offer it after the review. Only blockers.

1. Launch one `pr-verifier` subagent per blocker, in parallel. Give it the finding, the repo path, the head commit, and the command that runs one test in this repo.
2. It works in a scratch worktree, in a temporary folder outside the repo. It writes the smallest test that asserts the behavior the card requires, so the test is red while the finding is true and goes green once it is fixed. It runs only that test.
3. Outcomes:
   - **Reproduced:** the finding stands, and the failing test is attached for the author to keep.
   - **Refuted:** drop the finding and say why.
   - **Not reproduced here:** the test needs services that are not available locally. The finding stays, marked "verified by reading".
4. Nothing is committed or pushed. The worktree is removed.
