# The self-check

The self-check runs once per repo, after the last build slice and before the closing slice. You walk the repo's whole diff once and judge it against a fixed list. It is a check of your own work, so do it as if someone else wrote the code.

## Steps

1. Run `selfcheck-start`. It prints the diff command, the list, and the instruction files to re-read. When the build was reopened for a late slice, the diff covers only what was added since the last self-check.
2. Read the instruction files it lists now. Do not rely on having read them earlier. If it lists none, apply the coding instructions you were given at the start of the session.
3. Read the whole diff.
4. Judge every item below against the diff. Decide `ok`, `fixed`, `na` or `accepted` for each.
5. Fix what you find. Each fix is its own commit: edit, `verify`, then `commit --subject '...' --why '...'`.
6. Write the report file as `<CARD>.selfcheck-<repo>.json` and run `selfcheck-report --file <path>`. For a second self-check after a reopened build, write a new file (`...-2.json`) that judges only the new diff.

## What goes to the user first

- A fix that would change how the feature behaves.
- A pattern the repo is missing altogether, for example no logging convention at all.

Use `ask`, with what you found and what you recommend. Everything else you fix directly. Adding or correcting a log line is not a change in behavior.

## Judgment calls

- **The pattern you followed does not meet the list.** Your new code meets the list. Leave the old code alone and record it with `noticed`.
- **An instruction file disagrees with the approved plan.** The plan wins, because the user approved it. Say so in the item's `note` and tell the user at the end.
- **You find a problem outside the diff.** Do not fix it. Record it with `noticed`.
- **A stand-in for another system** (an in-memory publisher, a fake client) is not a call to another system. Mark `integrations` as `na` and say so.
- **An item is about something the diff does not have** (no consumer, no call to another system, no new config key). Mark it `na` and say what is absent.
- **The closing slice comes after the self-check.** It holds tests only, so nothing in it needs this list. If production code ends up in it anyway, the script tells you, and you apply this list to those lines by hand.

## The items

| Id | What must be true |
| --- | --- |
| `entry_logged` | Each new entry point logs once on the way in, with the ids needed to trace it. |
| `branches_logged` | Each branch or early exit that changes the outcome logs its reason. |
| `state_logged` | Each state change is logged with before and after, or the new value. |
| `integrations` | Each call to another system logs attempt, success and failure, and has a timeout. |
| `errors` | Each error is handled or passed on deliberately, and logged once with context. |
| `no_pii` | No personal data or secrets are written to logs. |
| `null_checks` | Data from outside (requests, events, other services, the database) is checked for missing values. |
| `duplicate_safe` | Each Kafka consumer gives the same result when the same message arrives twice. |
| `config_all_envs` | Each new config key exists in every environment file. |
| `no_leftovers` | No debug code, TODOs, commented-out code or unrelated changes are left. |
| `instructions` | The diff follows the instruction files. Compare it rule by rule: function length, class size, log format, no abstraction without more than one real case, and whatever else they say. |

## The report file

Write it as JSON in the working folder.

```json
{
  "items": {
    "entry_logged":    { "result": "fixed", "note": "Added the contract id to the relay's entry log." },
    "branches_logged": { "result": "ok" },
    "state_logged":    { "result": "ok" },
    "integrations":    { "result": "ok" },
    "errors":          { "result": "ok" },
    "no_pii":          { "result": "ok" },
    "null_checks":     { "result": "ok" },
    "duplicate_safe":  { "result": "na", "note": "This repo publishes. It has no consumer in this change." },
    "config_all_envs": { "result": "ok" },
    "no_leftovers":    { "result": "ok" },
    "instructions":    { "result": "ok" }
  },
  "log_success": "INFO OutboxRelay published contractId=C-1042 vendorId=V-77 status=APPROVED",
  "log_failure": "WARN OutboxRelay publish failed contractId=C-1042 attempt=2 reason=timeout"
}
```

Rules the script enforces:

- Every item is present, with `ok`, `fixed`, `na` or `accepted`.
- `accepted` is for a gap you put to the user and they chose to leave. It needs a `note` saying what the gap is and what they said. Never use it without asking.
- `fixed` needs a `note` saying what was fixed, in one line.
- `na` needs a `note` saying why the item does not apply. "Not applicable" is not a reason. When the plan's `work` is `trivial`, the note may be left out.
- `log_success` and `log_failure` are one log line each, as the new code writes them: one for the path that works and one for a path that fails or is skipped. Read the line off the code if you cannot run it. They are required whenever a logging item is `ok` or `fixed`.
- When the change writes no such line, write `"none: <why>"`, for example `"log_failure": "none: this change adds no failing or skipped path"`.
- When any item is `fixed`, at least one fix commit must exist since the self-check started.
- Nothing is left uncommitted.

Mark an item `ok` only after you have looked for it in the diff. If you did not look, look.
