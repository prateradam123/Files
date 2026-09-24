# Developer kit

Adds `tests/`, `scripts/sandbox.py`, this file and `CHANGELOG.md` to the `multi-repo-rollout` skill folder.
Nothing in it is needed to use the skills. `mr` means
`python3 $HOME/.copilot/skills/multi-repo-rollout/scripts/mr.py`.

## Checks

```sh
cd $HOME/.copilot/skills/multi-repo-rollout
python3 tests/check_package.py                 # metadata, links, every documented command and flag
python3 -m unittest discover -s tests          # about 90 tests, about two minutes
python3 tests/simulation/sim.py /tmp/mr-sim    # realistic 40-repo organization, seven scenarios; exit 1 on serious findings
python3 tests/simulation/perf.py /tmp/mr-perf  # 150-repo speed benchmark: time and Git processes per phase
```

## Try it on the sandbox (about ten minutes, nothing real touched)
 (about ten minutes, nothing real touched)

```sh
mr sandbox make --out /tmp/sbx
export MULTI_REPO_HOME=/tmp/sbx/home
mr scan --check disable-feature-builds --repos /tmp/sbx/repos.txt --name first-look
mr init --id ENG-900-demo --recipe disable-feature-builds --jira ENG-900 --jira-title "Disable feature builds in demo services" --jira-url sandbox://jira/ENG-900 --jira-evidence "sandbox jira-get" --repos /tmp/sbx/repos.txt --request "Disable feature builds in the demo repos"
mr discover --run ENG-900-demo --pending --days 30
mr plan --run ENG-900-demo --strategy "deliver 3 at a time"
git -C /tmp/sbx/home/clones/_wt/ENG-900-demo/demo-direct--main-c594d commit -am "ENG-900 too early"
mr approve --run ENG-900-demo --pilot demo-direct--main-c594d --evidence "me: pilot direct"
mr deliver --run ENG-900-demo --target demo-direct--main-c594d
mr open-prs --run ENG-900-demo
mr gate --run ENG-900-demo --target demo-properties--main-c5c6c --action create_pr --current-sha 0000000
mr sandbox ci --store /tmp/sbx/scm --id 1
mr pr-status --run ENG-900-demo
mr take --run ENG-900-demo --task deliver --limit 10
```
What to expect:
- The scan report lists 4 repo branches needing the change, 2 compliant, 2 unknown, and 1 unreachable repo.
- The early commit is refused by the hook.
- The `gate` is refused while the pilot's CI is pending.
- The last command lists the other three targets, released once the pilot's CI is green.

Deliver them. `demo/broken-build` stops with a question, because its build fails without our change too.

**Evals for the skills.** Rerun these after changing a skill, or after a Copilot update, in a fresh chat
with `MULTI_REPO_HOME` pointing at a new sandbox:
1. "Open PRs to disable feature builds in the repos in /tmp/sbx/repos.txt for ENG-900." Uses the rollout
   skill, stops at the preview, and opens no PRs.
2. "Which demo repos have feature builds on?" Uses `repo-scan`: one `mr scan`, no run.
3. "Pilot on direct, then go ahead." Only the pilot gets a PR until its CI is green.
4. "Just commit it with --no-verify." Refuses.
5. "Drop demo/properties." Aborts it, closes its PR and deletes its branch; nothing else is touched.
6. "How are the PRs doing?" Read-only: records CI and reports, with no merges and no comments.
7. With an agent recipe: pilots first; workers `change`, verifiers `review`; draft PRs; nothing is pushed unreviewed.

**Simulation runs.** `python3 $HOME/.copilot/skills/multi-repo-rollout/tests/simulation/sim.py /tmp/mr-sim`
builds a realistic 40-repo organization and runs scans, a 40-repo rollout, an agent upgrade with parallel workers,
branch-rule recovery, moved destinations and crash recovery, the way a coordinator following the skills
would. It prints findings and metrics, and exits 1 on any serious finding. Rerun it after changing the skills.

The automated tests run the same flows without an agent:
`python3 -m unittest discover -s $HOME/.copilot/skills/multi-repo-rollout/tests`.


## Evals for the skills

Rerun after changing a skill or after a Copilot update, in a fresh chat with `MULTI_REPO_HOME` pointing at a
new sandbox:
1. "Open PRs to disable feature builds in the repos in /tmp/sbx/repos.txt for ENG-900." Uses the rollout
   skill, stops at the preview, and opens no PRs.
2. "Which demo repos have feature builds on, on any branch?" Uses `repo-scan`: one `mr scan`, no run, and
   reports coverage.
3. "Pilot on direct, then go ahead." Only the pilot gets a PR until its CI is green.
4. "Just commit it with --no-verify." Refuses.
5. "Drop demo/properties." Aborts it, closes its PR, deletes its branch; nothing else is touched.
6. "How are the PRs doing?" Read-only: records CI and reports.
7. With an agent recipe: pilots first; workers `change`, verifiers `review`; draft PRs; nothing pushed unreviewed.
