# The review in chat (step 5)

Most important first. Short. Each finding is ready to copy into a PR comment.

## Reviewer mode opens with a reviewer guide

```
**What changed:** <two or three plain sentences: the behavior change, not a file list>
**Where the risk is:** <file:line, one line each on why>
**Checked by this review:** <for example: criteria 1 to 4 implemented; config present for every environment>
**Worth your own eyes:** <what needs your judgement or domain knowledge>
**Reading order:** <the files, most important first>
```

## Verdict

One of `Approve`, `Approve with comments`, `Changes requested`, with one paragraph of reasons. It is a suggestion to the user. You never set it on the PR.

## Findings

Blockers first, then majors, questions, minors.

```
### B1. Blocker, failure handling (also raised by correctness). src/.../PartnerClient.java:84
**Evidence:** `catch (Exception e) { return null; }`
**Impact:** A partner outage is swallowed. Orders are marked sent but never delivered, and nothing alerts.
**Fix:** Rethrow as `PartnerUnavailableException`, as `InvoiceClient` does, so the framework's retry applies.
**Verified:** by reading
```

When there are no findings, say so in one line.

## Acceptance criteria (when there is a card)

| # | Criterion | Implemented | Test that proves it | Notes |
|---|---|---|---|---|

## Noticed, not introduced by this change

Optional, at most two, and only if serious. One line each.

## Suggestions

Optional, at most three. Each: the idea, a short sketch, the payoff.

## Coverage

| Axis | Ran | Why, or why skipped |
|---|---|---|

Then one line: what was not checked. Always include: "CI results were not used. Dependency vulnerabilities are left to the CI scanner."

## Count

`N findings: X blockers, Y majors, Z questions, W minors. Dropped: D.`

Dropped counts what the evidence check, the preference rule and your own answers to questions removed. Merged duplicates are not counted.

## Author mode ends with a fix plan

- **Trivial fixes:** one batch of diffs, one approval.
- **Substantive fixes:** one at a time, test first.
- **Not fixed by me:** suggestions, and questions that need a person's answer.
