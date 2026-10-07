# Build Flow, version 0.1

A Copilot skill that takes one Jira card to draft pull requests: plan, your approval, sliced build with a check per slice, self-check, full suite, PRs.

It comes with a monitor page that follows the run live. It has no scout or reviewer subagents yet.

## What is in this folder

| File | For |
| --- | --- |
| `SKILL.md` | The instructions Copilot loads. |
| `flow.py` | The script. It holds the run's state, runs the tests, makes the commits and writes the log. |
| `monitor.html` | The page you watch the run on. The script copies it beside the run's log and opens it. It needs no server. |
| `reference/` | Plan format and example, slicing rules, self-check list, commit and PR format. Copilot reads these when it needs them. |

## Install

1. Copy this folder to `%USERPROFILE%\.copilot\skills\build-flow`.
2. In PowerShell, run:

   ```
   python "$HOME\.copilot\skills\build-flow\flow.py" ABC-1 status
   ```

   It should answer `No run exists yet for ABC-1.` That means the script runs.
3. If your Python command is `py`, change `python` to `py` in the "Running the script" section of `SKILL.md`.
4. Optional: allow that command to run without a prompt in Copilot's settings. Otherwise Copilot may ask each time.

The script needs Python 3.8 or newer and `git` on the PATH. It installs nothing.

## Use

Open the workspace folder that contains your repos, then ask Copilot to build a card with the skill, for example `/build-flow ABC-123`.

The monitor page opens in your browser when the run starts and updates by itself. To open it again: `python "$HOME\.copilot\skills\build-flow\flow.py" ABC-123 monitor`.

To change a card, during the run or after its PRs are open, say what you want in chat with the same command: `/build-flow ABC-123 also do X`.

## Where things go

| What | Where |
| --- | --- |
| The plan you read and approve | `<workspace>\<CARD>.plan.md` |
| The agent's working files (plan JSON, self-check report, PR descriptions) | `<workspace>\.build-flow\` |
| State, log, full test output, monitor page | `%USERPROFILE%\.agent-data\build-flow\runs\<CARD>\` |

To start a card over, delete its folder under `runs`. Its feature branch is left alone.

## What the script enforces

- No slice starts before you approve the plan, and the next slice does not start until the current one is committed.
- A slice is committed only when its check ran and passed on exactly the code being committed.
- An existing test cannot be edited unless the plan lists it.
- Commits and pushes happen only on the feature branch, never forced.
- A change to criteria, contract, scope or decisions needs your recorded approval.
- A question to you must carry a recommendation.
- Behavior added late goes through a new build slice, and the self-check and full suite run again.

## Tested, and not yet tested

Tested on Linux, in a sandbox with two small Java services, a fake Jira and a fake PR host with CI:

- Thirteen sessions by agents that had only this skill to go on: a standard card, a two-repo card, a trivial card, a thin card cut off mid-slice and resumed in a new session, and changes asked for mid-run and after the PRs were open (a small fix, a plan change that added a slice, a plan-only change, a withdrawn change).
- Two scripted runs that exercise every refusal above.
- The monitor page, in Chromium: every event of every run stepped through the page at phone and desktop width, and the page watched live while agents worked.

Not yet tested, because it needs your machine:

- Windows itself: stopping a timed-out test run, running checks through `cmd`, and opening the page from PowerShell.
- A real Maven run. Maven Central was not reachable where this was built, so Maven was replaced by a stand-in that compiles and runs the tests and prints Maven's output format.
- Copilot following the skill. The agents in the sandbox were Claude playing Copilot.
- A squash-merged PR: the script cannot see that a branch was merged that way, so the skill tells the agent to look at the PR.

Pick a small single-repo card for the first run.
