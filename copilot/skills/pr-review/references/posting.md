# Posting to the PR (reviewer mode, only when asked)

1. The user chooses: "post all", "post 1, 3 and 5", or "post the summary only".
2. Show exactly what will be posted: each comment with its file and line, and the summary. Post only after the user confirms.
3. Inline comments go on the exact lines. Each starts with its severity ("Major: ..."), then the evidence, the impact and the fix. A finding on a line outside the diff goes into the summary, because it cannot be anchored.
4. The summary comment holds the verdict in plain words as the user's view ("Changes needed before this merges"), the condensed reviewer guide, and the list of the findings being posted. When the user chose only some findings, the summary lists only those.
5. A blocker that was reproduced carries its failing test in its inline comment.
6. A finding with one place inside the diff and one outside is anchored on the line inside the diff, and names the other file in its text.
7. On GitHub, post everything as one review of type "comment". On Bitbucket, post inline comments and one summary comment. If the user asks, post blockers as Bitbucket tasks.
8. Everything is posted as the user, through their SCM tool. Never approve, request changes or merge.

## Posting again after a re-review

1. Keep a small file per PR in `~/.copilot/pr-review/sessions/<repo>-<pr>.json`. It holds the head commit that was reviewed, the summary comment's id, and every finding of the review: its id, axis, severity, file and line, one line of text, and its comment id if it was posted. A later chat has no memory, so this file is what a re-review starts from.
2. Never post the same finding twice.
3. For an earlier finding that is now fixed, reply "Resolved in `<commit>`" on its thread only if the user asks.
