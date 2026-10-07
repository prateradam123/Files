# The plan file

The plan is one JSON file. You write it into the working folder (`.build-flow` in the workspace) and give it to the script with `plan-submit`. The script checks it, saves it, and writes the readable plan for the user as `<CARD>.plan.md` in the workspace folder, beside the repos. Never edit that readable file. To change the plan, edit the JSON and submit or revise.

A complete example is in `plan.example.json`.

Write the JSON with your file-editing tool, not with a PowerShell command.

## Fields

| Field | Required | What it holds |
| --- | --- | --- |
| `title` | yes | The card title, as the user would say it. |
| `what` | yes | What changes, in one or two sentences. |
| `why` | yes | Who needs it and why, in one sentence. |
| `not_included` | no | List of things a reader might expect that this card does not do. |
| `work` | yes | `"trivial"` or `"standard"`. |
| `risk` | yes | `"low"`, `"standard"` or `"high"`. |
| `risk_reason` | yes | One sentence on why the risk is what it is. |
| `branch` | yes | The feature branch, used in every repo. Form: `feature/<CARD>-<short-name>`. |
| `criteria` | yes | List of acceptance criteria. See below. |
| `decisions` | no | List of `{ "text", "by", "repo" }`. `by` is `"user"` for an answer the user gave, `"default"` for one you chose that they can veto. `repo` is optional: give it when the decision concerns one repo only, so it appears only in that repo's PR. |
| `assumptions` | no | List of things you took as given and did not check with the user. |
| `contract` | no | `{ "summary", "items": [ { "name", "kind", "change" } ] }`. Fill it whenever an API, event, topic, table or config key is added or changed. Give exact names. |
| `tests_expected_to_change` | no | List of `{ "test", "change", "repo" }` for existing test classes in which this card must change or remove lines. `test` is the class name. `repo` is the repo it lives in: give it whenever the plan has more than one repo. See "Existing tests" below. |
| `repos` | yes | List of repos in build order. The repo that owns the contract comes first. See below. |
| `rollout` | no | Ordered list of steps to switch the change on, and how to back it out. |
| `merge_order` | no | List of repo names in the order their PRs must merge. Give it when that differs from the build order, for example when the consumer must be live before the producer. It must agree with the `rollout` steps. |

### A criterion

```json
{ "id": "AC1", "text": "Approving a contract publishes a vendor status event",
  "test": { "layer": "component", "scenario": "approval publishes the event" } }
```

- `id`: `AC1`, `AC2`, and so on.
- `text`: something a person can check, stated as an outcome.
- `test.layer`: the lowest layer that can prove it: `unit`, `spring`, `component` or `contract`. When a criterion is proven at unit level in a build slice and again end to end in the closing slice, give the lowest layer here and list the criterion in both slices' `proves`.
- `test.scenario`: the scenario that proves it, in a few words.

Write a criterion for everything the user asked for, including what they added in chat that the card did not say.

### A repo

| Field | Required | What it holds |
| --- | --- | --- |
| `repo` | yes | The repo's name. |
| `path` | no | Its folder, relative to the workspace. Defaults to the repo name. Use `"."` when the workspace is the repo. |
| `base` | yes | The branch the feature branch starts from and the PR goes into. |
| `in_flight` | no | Other open branches that touch the same files, found with the commands in SKILL.md. Say which branch and which file. In-flight work in a repo that is not in the plan goes in `assumptions`. |
| `full_suite` | no | The command for the whole test suite. Defaults to `mvn verify`. |
| `suite_timeout_min` | no | Minutes before the full suite is stopped. Defaults to 30. |
| `slices` | yes | The build slices, in order. |
| `closing` | no | The closing test slice: tests only. Leave it out only for trivial work. |

### A slice

```json
{ "title": "Write status to outbox on approval",
  "done_when": "An approval writes one outbox row, and a rollback writes none",
  "approach": "Follow ApprovalAuditWriter, which already writes inside the approval transaction.",
  "proves": ["AC1", "AC2"],
  "tests_to_add": ["OutboxWriterTest", "ApprovalRollbackTest"],
  "depends_on": "Slice 1",
  "checks": [ { "cmd": "mvn -Dtest=OutboxWriterTest,ApprovalRollbackTest test", "timeout_sec": 300 } ] }
```

- `title`: what the slice delivers, under 80 characters.
- `done_when`: the observable result that makes it done.
- `approach`: the existing class or pattern to follow, by name.
- `proves`: the criteria this slice proves. May be empty for a slice that only supports others, or that carries out a decision. Say which decision in `done_when`.
- `tests_to_add`: the test classes this slice adds or extends, by class name. An existing class whose existing lines also change goes in `tests_expected_to_change` as well.
- `checks`: one or more commands the script runs to verify the slice. Each must pass.
- `timeout_sec`: seconds before a check is stopped. Defaults to 300.

## Check commands

The script runs each check itself, in the repo folder, through `cmd`, not PowerShell. Keep each one a single plain command.

- A build slice checks only what it built: `mvn -Dtest=ClassA,ClassB test`. In a multi-module repo add `-pl <module>`.
- Tests that start a Spring context, component tests and contract tests are slow. They belong in the closing slice. The one exception is a build slice added after the closing slice is done: it may run a closing test that it had to change.
- Find the component and contract test commands in the repo's CI file, pom profiles or README. Use what CI uses. If the repo has no contract tests, the closing slice holds component tests only: say so in `assumptions`.
- When the repo has `mvnw.cmd`, write `mvnw` in place of `mvn`.
- Never add `-q`. The script needs the test counts, and it trims the output for you.
- A test command that runs zero tests fails. Name test classes that exist by the end of the slice.

## What the script refuses

- A criterion that no slice proves.
- A slice without `title`, `done_when` or a check command.
- A `proves` entry that is not a criterion.
- A branch that is a base branch (`main`, `master`, `develop`, `release/...`, `hotfix/...`).
- `work`, `risk` or a decision's `by` outside the allowed values.

## Changing the plan after approval

Run `plan-revise --file <path> --summary '<what changed>'`.

- **No approval needed:** changing, adding, removing or resplitting slices that are not done.
- **The user must agree first:** any change to `what`, `why`, `not_included`, `criteria`, `contract`, `decisions`, `tests_expected_to_change`, `risk`, `rollout`, `merge_order`, `branch`, or the list of repos and their base branches. Tell the user what changes and why, wait for their answer, then add `--user-said '<their words>'`.
- **A slice that is done cannot change.** Add new work as a new slice after it. This includes a closing slice that is done: it is not run again, so a later build slice edits its tests when it has to, and the full suite proves them.
- **An answer from the user is an approval.** When a question you asked with `ask` leads to a plan change, pass their answer as `--user-said`.
- **A build slice can be added at any point**, even after the self-check, the closing slice, the full suite or the PR. The script puts that repo back in its build stage, and its self-check and full suite run again. New behavior always goes in a build slice, never in the closing slice.

## Existing tests

The script refuses a commit that changes or removes lines in a test file that exists on the base branch, unless that test class is in `tests_expected_to_change`.

- Adding new test methods to an existing class, without touching its existing lines, needs no listing.
- Changing a `setUp`, a constructor call, an assertion or a test name in an existing class needs listing. Say what changes and why in `change`.
- A test class you created earlier on this branch is not an existing test.
- When a build slice changes a signature that an existing component test uses, edit that test in the same slice, even though component tests otherwise belong to the closing slice. List it.

A failing test that is not on the list is a regression: fix the code. If the test really must change, that is a plan change the user approves.
