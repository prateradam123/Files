# Commits and pull requests

## Commits

The script makes every commit. You supply the parts with `slice-done` (for a slice) or `commit` (for a fix).

```
flow ABC-123 slice-done --subject 'Publish vendor status event on contract approval' --why 'Vendors need approval status without polling.' --what 'Approval writes to the outbox in the same transaction. The relay publishes to contract.vendor-status.v1.' --touches 'contract.vendor-status.v1, outbox_event, vendor.status.enabled' --note 'Event order follows outbox id, not timestamp.'
```

That produces:

```
ABC-123 Publish vendor status event on contract approval

Why: Vendors need approval status without polling.
What: Approval writes to the outbox in the same transaction. The relay publishes to contract.vendor-status.v1.

Touches: contract.vendor-status.v1, outbox_event, vendor.status.enabled
Note: Event order follows outbox id, not timestamp.
```

| Part | Rule |
| --- | --- |
| `--subject` | What the commit does, in the imperative. The script adds the card key. The whole line is at most 72 characters. |
| `--why` | The reason the change is needed. Required on every commit that changes behavior. |
| `--what` | How it works. Only when the subject does not already say it. |
| `--touches` | Exact names someone in another repo or team would search for: topics, tables, config keys, endpoints, schema names. Comma separated. Leave it out when the commit has none. Class names do not belong here. |
| `--note` | One catch that is not obvious from the code. Optional. |
| `--mechanical` | Use in place of `--why` for a commit that changes no behavior: a rename or formatting. Subject only. A comment-only change is mechanical. A change to the text of a log line or a message is not, because someone may search for it: give `--why`. |

For the closing slice, give `--why` as the criteria it proves end to end: `--why 'Proves AC1 and AC2 through the wired service.'`

Do not restate the diff. Do not list files. Do not write "as requested" or name the tool that wrote the code.

The script refuses the commit when:

- the check has not passed, or files changed after it passed
- the commit changes or removes lines in an existing test that is not on the plan's expected-to-change list (new test methods are fine)
- the repo is not on the feature branch

## Pull requests

One pull request per repo, opened as a draft, from the feature branch into the base branch in the plan. Follow the script's `NEXT:` line for the order. It pushes every repo first, then takes the PRs one at a time. The repo name is optional on these commands: without it the script picks the next repo that needs the step.

1. `push` pushes the feature branch. It never forces.
2. `pr-body` writes the description to a file in the working folder and prints the title.
3. Read the description. It is your file to edit: tighten the wording where a commit subject reads badly as a bullet, give a fix commit its reason, remove a note that a later slice made untrue, and keep the four sections. Until the PR is open, running `pr-body` again rewrites the file.
4. Open the draft PR with that title and file.
5. `pr-opened --url <the PR link>`.

The title is the card key and the plan title: `ABC-123 Publish vendor status event on contract approval`.

The description has four sections:

| Section | What it holds |
| --- | --- |
| Summary | What changed, for whom and why. What is not included. |
| Changes | One bullet per commit, with its note when it has one. Then the decisions, marked as the developer's or a default. |
| Testing and rollout | The criteria this repo proves, the tests it adds, the full suite result, existing tests that changed, the merge order, and the rollout steps including how to back out. |
| Touches | The exact names from the commits, the card, and links to the sibling PRs. |

When a card has more than one repo, add each sibling PR's link under Touches once it exists. The script tells you which link goes where.

After a change request on a card whose PRs are open, `NEXT:` sends you to `pr-body` for each repo whose description is out of date. For an open PR the script does not touch your description file. It prints the bullets to add for the new commits and writes a freshly generated description beside it, as `pr-<repo>.generated.md`, to copy lines from. Edit your file, update the open PR with it, and leave the PR open. Do not turn it back into a draft.

## After the PR is open

- Check CI: the build, SonarQube, coverage, and the dependency and security scans. If a check is still running, look again after a minute. Do not go on until every check has finished.
- Fix each failure in the repo: edit, `verify --repo <name>`, `commit --repo <name> ...`, `push <name>`.
- Never lower a threshold, exclude a file from a scan, or skip a test to get a green build. If a check cannot pass honestly, tell the user.
- When CI is green, mark the PR ready for review, then `pr-ready <name> --ci-result '<what ran and passed>'`.
- You never merge. The user reviews, and someone else must approve.
