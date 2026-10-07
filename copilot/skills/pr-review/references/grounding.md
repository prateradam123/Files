# Grounding (step 1)

Gather only what this change needs. Skip a lookup when the change does not touch that area.

## The change

1. Reviewer mode: from the SCM tool, the PR's title, description, base branch, head commit, changed files and existing review comments.
2. Author mode: the current branch, and the base it will merge into. Ask the user for the base if it is not obvious. The diff is `git diff <base>...HEAD`.
3. Note the head commit. Every quoted line in the review must exist at that commit.
4. If the working tree is not at the head commit, do not check anything out. Read files with `git show <head>:<path>`, and tell every reviewer to do the same, with line numbers taken from the file at the head.
5. The base is obvious when the PR names it, or when the repo has one long-lived branch. Otherwise ask.

## The card

1. Find the key in the PR title, the branch name or the commit messages.
2. No key: ask the user for it before going on. If they say there is none, review against the PR description only and say so in the review.
3. From the Jira tool, take the scope, what must not change, any required approach, and the acceptance criteria.
4. When the card points to another card you cannot read, say so in the review and go on.

## History

1. Run `git blame` on the changed and deleted lines at the base commit.
2. For lines that came from a bug fix, a hotfix or a revert, note that commit and its card. A change that removes such a line is a risk to name.
3. Look for a reverted commit in the same area.
4. Stop after five commits. A past discussion counts as a decision only if it was acted on.
5. When the history holds nothing relevant, pass on "no relevant history" and move on.

## The repo's setup

Read what you need from the tree:

- **Test layers:** unit tests, component tests (the whole app with integrations virtualized) and contract tests, and where each lives.
- **Datastore:** the driver, the access layer, pooling and migrations, from the build file and config.
- **Framework:** the internal framework and what it already provides. When the change builds something the framework may provide, find one real use of the framework's version in this or a sibling repo. When the repo uses no framework, say "none".
- **Callers outside the repo:** when a public entry point changes, search the sibling repos in the workspace for callers yourself, and pass on what you find.
- **Config locations:** the per-environment config files, deployment definitions, and parameter or secret references.
- **House patterns:** how this repo logs, handles errors and wraps calls to other systems. Read the repo's instruction file if it has one.

## Pass on

Give each reviewer a short facts block: repo path, base and head commits, the diff command, the card's criteria, the setup facts that matter to its axis, and the history findings that matter to its axis.
