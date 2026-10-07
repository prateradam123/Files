---
name: build-flow
description: Take a Jira card to pull requests through a planned, sliced and verified build. Use when the user gives a Jira card key and asks to build or implement it, or to continue or change a card already in progress.
---

# Build Flow

You take one Jira card from its description to draft pull requests that have passed their tests. A script holds the state of the run, runs every test, and makes every commit. You do the thinking and the coding. Subagents do the reading that would fill your context and the reviewing that must not be done by the author. The user approves the plan in chat and reviews the pull requests.

## The script decides where you are

Run every step through the flow script. Every reply ends with a `NEXT:` line. Do what that line says, one command at a time, and read the reply before the next command. If you lose your place, run `status`. Do not work out the next step from memory. Do not put two flow commands on one line, with `;` or any other way.

- `OK` means the step is recorded.
- `REFUSED` means nothing changed. The `DO:` line under it says what to do. Never work around a refusal.
- A failed check prints the first errors and the path of the full output. Read the full output only when the first errors are not enough.

## Running the script

The terminal is PowerShell. In this file `flow` stands for:

```
python "$HOME\.copilot\skills\build-flow\flow.py"
```

So `flow ABC-123 status` is typed as `python "$HOME\.copilot\skills\build-flow\flow.py" ABC-123 status`.

Rules for text values on the command line:

- Wrap each text value in single quotes: `--why 'Vendors need the status without polling.'`
- Write an apostrophe inside a value twice: `'the user''s answer'`
- Never put a double quote inside a value. Reword it. When you quote the user, keep their words but write contractions out if that is easier: `we are` for `we're`.
- Anything long or structured goes in a file. The plan and the self-check report are JSON files you write into the working folder that `start` prints (`.build-flow` in the workspace). Name them `<CARD>.plan.json` and `<CARD>.selfcheck-<repo>.json`.
- A text over its limit is refused, with the limit in the reply. Shorten it and run the command again. Questions, recommendations, change requests and takeaways may be up to 300 characters; notes, tasks and reasons up to 160; a commit subject line up to 72. `--found` and `--options` have no limit, but keep them to a few lines.

Run the script from the workspace folder, the one that contains the repos.

## Subagents

You launch three kinds of subagent. None of them edits files, runs the flow script or talks to the user. You do those.

| Agent | When | What it does |
|---|---|---|
| `flow-scout` | Planning, for anything but a trivial card. | Answers one question about the code and returns facts with file references. |
| `flow-plan-reviewer` | After the plan is submitted, before the user sees it. | Looks for what would make the build go wrong. |
| `pr-reviewer` | The review phase of each repo, through the `pr-review` skill. | Reviews the change on one axis. |

Rules for every launch:

1. A subagent knows only what you put in its prompt. Give it the paths, the question and what the change is about.
2. Launch independent subagents at the same time, not one after another.
3. If a named agent is not installed, launch a plain subagent and tell it to read that agent's file in the Copilot `agents` folder before it starts. If the `pr-review` skill is not installed, stop and tell the user.
4. What a subagent returns is evidence, not truth. Check a fact against the code before the plan or a fix depends on it.

## Starting or continuing a card

Run `flow <CARD> status` first, every time. Then take the one case that fits.

- **No run yet.** Read the card with the Jira tool in this session. If there is none, ask the user to paste the card's title, description and acceptance criteria. Run `flow <CARD> start --title '<card title>' --url '<card link>'`. `NEXT:` then sends you to `flow <CARD> monitor`, which opens the page where the user can follow the run. In your first chat message, tell the user what the script said about the page: that it is open, or where the file is if it could not be opened.
- **A run exists and is not finished.** Run `flow <CARD> brief` and read the plan file it names before you touch code. `status` tells you whether a repo has uncommitted changes left by an earlier session. If it has, look at them before you go on, and never discard them. Then continue from the `NEXT:` line.
- **A run exists and is finished.** Anything more the user wants on this card is a change request. Run `brief`, read the plan file, then follow "When the user asks for a change".

When the user's message asks for a change as well as asking you to continue, record the change first, as described under "When the user asks for a change". `NEXT:` keeps pointing at the change until it is settled.

## Planning

Write no code before the plan is approved. Read `reference/plan-format.md` before you read the code: its fields tell you what to look for.

1. **Understand the ask.** Restate it to yourself in one sentence. A card with no acceptance criteria is normal: you write them in the plan and the user approves them.
2. **Find out what the plan depends on, and no more.** Read the instruction files `start` listed for the repos you will change yourself. They shape the design. For the rest:
   - **A card where you can already name the one place to change** from the card and the instruction files (a typo, a wording, a constant): read the code yourself.
   - **Any other card:** launch `flow-scout` subagents, all at once, one question each. The usual scouts:
     1. One per repo you will change: where does the change enter, what existing code does the same kind of thing, and which tests cover it?
     2. One for the rest of the workspace: what else reads, calls or consumes what will change? This covers repos you will not change.
     3. One for all repos together: is other work in flight on the same files? (`git fetch`, then `git branch -r --no-merged origin/<base>`, then `git diff --name-only origin/<base>...<branch>` for a branch that looks related.) Record what is found in the repo's `in_flight`. For a trivial card, run these three commands yourself.
     4. Only when the card points to documents and says what to do but not how: what do those documents say?
   - **A scout's prompt holds three things:** the one question; the folder or folders to look in, as full paths; and what the change is about, in a few lines from the card. Add the specific facts you need answered.
   - **Record each scout:** at launch, `flow <CARD> agent-start --role scout --task '<the question, short>' --why '<why the plan needs it>'`, one command per scout. When it returns, `flow <CARD> agent-done <number> --takeaway '<what it found, in one line>'`. The takeaway is for the monitor page. The scout's full answer stays with you.
   - **The open decisions scouts list are raw material.** Merge the ones that say the same thing, then sort each with the test in step 4. Most become defaults.
   - Then read for yourself the one or two files the scouts named as the pattern to follow. You will build from them.
   - When code and documents disagree, the code wins. Note the file for every fact you rely on.
3. **Make two calls and put them in the plan.** `work`: `trivial` (one small obvious change) or `standard`. `risk`: `low`, `standard` or `high`, with the reason. A new or changed contract that another repo or team will rely on, a migration, and anything costly to get wrong are high.
4. **Sort every unknown into one of three kinds.**
   - The card or the code answers it. It is settled. Do not ask.
   - There is a clear precedent or an obvious default. Put it in the plan's `decisions` with `"by": "default"`. The user can veto it when they read the plan.
   - There are two or more reasonable options that lead to different outcomes the user would care about. Ask.
   The test for asking: would the user be surprised or annoyed to find your choice in the PR? "Which HTTP status for a refused approval, when the controller already returns 409 for every other refusal" is a default. "Should an admin be exempt from the rule" is a question: the card does not say, and the two answers are different products.
   On a high-risk change, a default you are not sure of becomes a question. Risk does not turn a default you are sure of into a question.
5. **Ask all the questions at once.** Record each one with `flow <CARD> ask --question '...' --recommend '...' --found '...' --options 'A: ... | B: ...'`. When every one is recorded, put them to the user in a single chat message: for each, what you found, the options, and the one you recommend. Wait. Record the reply with `answered --user-said '<their words>'`. When the reply answers only some of them, close those with `--question <number>` and ask again about the rest. If an answer opens a new fork, ask once more. Questions you need answered before you can even plan a thin card go the same way. Keep `--question` and `--recommend` to a sentence or two each (300 characters), and give the reason for the recommendation in `--found`.
6. **Write the plan as JSON** in the working folder, as `<CARD>.plan.json`. The fields and rules are in `reference/plan-format.md`, with an example beside it. How to cut slices is in `reference/slicing.md`. Anything the user asked for that the card did not say becomes a criterion as well as a decision.
7. **Submit it:** `flow <CARD> plan-submit --file <path>`. Fix whatever it refuses. For trivial work the script skips the plan review and the code review on purpose: say so when you show the user the plan.
8. **Have the plan reviewed** when `NEXT:` says so. It does for everything but trivial work. Follow "The plan review" in `reference/review.md`: launch one `flow-plan-reviewer`, fix what stands, and record the result with `plan-reviewed`. The user does not see the plan before this.
9. **Show the user the plan file** the script wrote and ask for approval in chat. Summarize it in a few lines: what changes, the slices, the decisions that are theirs to veto.
10. **Record the approval** with `plan-approve --user-said '<their words>'`. Never approve on the user's behalf. A reply that asks for any change is not an approval, even if it sounds positive: edit the JSON, submit again, and ask again. A reply that answers an open question and approves is both: record `answered`, and if the plan already says what they answered, record `plan-approve` with the same words.

## Building

Follow `NEXT:` through each repo: `repo-start`, then for each slice `slice-start`, build, `check`, `slice-done`.

- Build only the slice in progress. Leave no stubs and no TODOs at its end.
- Never run the tests yourself to decide a slice is done. `check` runs them and records the result. A slice is done when `check` has passed and `slice-done` has committed it.
- The script makes the commit. You supply the message parts. The format is in `reference/commit-and-pr.md`.
- `check` lists the files that will be committed. If one does not belong to the slice, remove or revert it and run `check` again.
- When a check fails, read the first errors, fix the cause, and run `check` again. Change code, not an existing test, unless that test is on the plan's expected-to-change list.
- When a check times out, follow the steps the script prints before trying again.
- When a slice turns out wrong or too big, resplit it: edit the plan JSON and run `plan-revise --file <path> --summary '<what changed>'`. Slices that are not done can change without approval.
- When you hit a choice the plan did not settle and the options behave differently, use `ask` as in planning. Do not pick silently. If the answer adds or changes anything in the plan, including a decision, run `plan-revise` with the answer as `--user-said`. Their answer is the approval, so you do not ask a second time. If the answer leaves the plan as it is, go on.
- A test this run created in an earlier slice is yours to change when a later slice changes what it should assert. The rule against changing existing tests is about tests that were on the base branch.
- When you notice a problem that is outside this card (a typo, a rule the old code breaks), do not fix it. Record it with `flow <CARD> noticed --text '...'`. The script lists these for the user at the end.
- When you start something slow that is not a slice start (chasing a failing check, waiting on CI), say so with `flow <CARD> now '<one line>'`. The user sees it on the monitor page.
- When you learn something a later run should know (a faster command, a trap in this repo, a correction from the user), record it: `learn --kind <insight|issue_fix|command|correction|went_well> --text '...'`.

## When the user asks for a change

This applies at any point after the plan is approved, including after the run has finished.

1. Run `flow <CARD> change-request --text '<what they asked>'`. The script checks whether a feature branch has already been merged and refuses if one has. It cannot see a squash merge, so when PRs are open, look at them yourself as well. If one is merged, stop and tell the user: the change needs a new branch. Until the change is settled, `NEXT:` points at it and nothing else. The script says when a commit or a plan change has settled it.
2. A question is not a change. When the user asks "should we also ...?", answer in chat with what you found and what you recommend. Record a change request only when they ask for the change.
3. Size it.
   - **It stays inside the approved plan** (a rename, a log line, a small fix): make it. In a slice that is in progress it is part of that slice: `check`, `slice-done`. Otherwise `verify --repo <name>`, then `commit --repo <name> ...`.
   - **It changes the criteria, the contract, the scope, the decisions, the expected test changes, the rollout, the merge order, the branch or the repos:** it is a plan change. Tell the user exactly what changes in the plan and wait for their yes. Then `plan-revise --file <path> --summary '...' --user-said '<their words>'`. If you need a fact from them first, record it with `ask`. The question and the proposed plan change may go in the same chat message.
   - **The user drops it:** `change-request --withdrawn '<their words>'`.
   - **The user already said yes to the exact plan change** before you recorded it: that yes is the approval. Pass it as `--user-said`.
   - **The user asks you to fix something you had recorded with `noticed`:** their request is enough. A line or two with no change in behavior is inside the plan. Right after the commit, run `noticed --resolved <number>` so the list stays true. `status` shows the numbers.
   - **The fix needs an existing test edited, or changes what the feature does:** it is a plan change, however small it looks.
   - **The fix touches code the slices' tests do not cover:** use `verify --full`, which runs the whole suite.
4. New behavior always goes in a new build slice, added after the slices that are done. Never put it in the closing slice. The script reopens the build for that repo, and the self-check, the review and the full suite run again on the new code. `repo-start` is not needed again.
5. The closing slice does not run again after a reopened build. If the new slice breaks a closing test, or should extend one, edit that test in the new build slice and give the slice a second check that runs it. List the new criterion in the new slice's `proves`.
6. When pull requests are already open, the new commits go to the same branches and the same PRs. `NEXT:` takes you through `push`, `pr-body` and CI again, for the changed repos only. For an open PR, `pr-body` leaves your description file alone and prints the lines to add. Leave an open PR open: do not turn it back into a draft. In your closing message, say that CI ran again and what it showed.

## Self-check, closing tests, review and the full suite

After the last build slice, run `selfcheck-start` and follow `reference/self-check.md`. Each fix is its own commit: edit, `verify`, `commit`. `verify` runs the check commands of every slice that is done in that repo, so it is quick and covers what you built. `verify --full` runs the whole suite. A fix that changes behavior goes to the user first. Then write the report file and run `selfcheck-report`.

The closing slice comes next, when the plan has one. It adds the component tests, and contract tests when the repo has them. It holds tests only.

The review comes next, for everything but trivial work. You wrote this code, so you do not review it. Run `review-start` and follow "The code review" in `reference/review.md`: the `pr-review` skill picks the review axes, `pr-reviewer` subagents read the change, you check what they return, and each finding that stands is fixed, put to the user, or rejected with a reason. "No findings" is a good result.

Then run `suite`. If it fails, fix the code and run it again. When it passes, run `repo-done`. When another repo follows, `repo-done` needs a handoff note: what was actually built that the next repo relies on.

## Pull requests

When every repo is built, follow `NEXT:` through `push`, `pr-body`, opening the draft PR, and `pr-opened`. The script takes the repos in order: all pushes first, then the PRs. The PRs merge in build order unless the plan gives a `merge_order`.

- `pr-body` writes the description to a file in the working folder. That file is yours to edit.
- On GitHub, open the PR with the `gh` command the script prints.
- On Bitbucket, use your Bitbucket skill with the title and description file the script prints.
- Always open the PR as a draft, from the feature branch into the base branch in the plan.
- Check CI with `gh pr checks` or your Bitbucket skill. If it is still running, check again after a minute. Fix each failure in the repo: edit, `verify --repo <name>`, `commit --repo <name> ...`, `push <name>`.
- When every check has finished green, mark the PR ready for review and run `pr-ready <name> --ci-result '<what ran and passed>'`. Never mark a PR ready on a red build.
- Run `finish`, then give the user the PR links, the merge order, and everything the script lists: review findings left as they are, and what was noticed outside the card. Their review, and an approval from someone else, come next. You do not merge.

## Rules that never bend

1. Every step goes through the script. Never edit its state, its log or the readable plan file by hand. The files in the working folder are yours.
2. Never report a test result you did not get from the script.
3. Work only on the feature branch. Never force-push, never push to a base branch, never merge a PR.
4. Never stash, reset or discard changes you did not make. Tell the user.
5. Every question to the user comes with what you found and what you recommend.
6. Keep what you send the script short. It enforces limits. Shorten, do not pad.
7. Never review your own plan or your own code in place of the reviewer subagents. Never record a review that did not happen.

## What must be installed

- This skill, in the Copilot `skills` folder.
- The `pr-review` skill, in the same folder. The review phase uses it.
- The agents `flow-scout`, `flow-plan-reviewer`, `pr-reviewer` and `pr-verifier`, in the Copilot `agents` folder.
- A Jira tool, and `gh` or the Bitbucket skill for pull requests.
