# Cutting slices

A slice is the smallest piece of the change that proves something on its own. The script lets the next slice start only when the current one has passed its check and been committed.

## How to cut

- **Cut by behavior, not by layer.** "Write status to outbox on approval" is a slice. "Add the repository class" is not: nothing can be checked until something uses it.
- **Each slice has a check that can fail.** If you cannot name a test that would fail without the slice, it is not a slice. Merge it into the one that uses it.
- **Migrations and schema changes go first**, as their own slice.
- **Order by dependency.** A slice may rely on the ones before it, never on one after it.
- **Keep each slice small enough to hold in your head**: usually one behavior, a handful of files, and one or two test classes.
- **Prove each criterion at the lowest layer that can prove it.** A rule about a calculation is a unit test. A rule about a transaction needs a Spring test. A rule about the whole request needs a component test.

## What goes where

| Kind of work | Where it goes |
| --- | --- |
| A behavior with its unit tests | A build slice |
| A migration or schema change | The first build slice |
| A new config key | The slice that first reads it, added to every environment file |
| Tests that start a Spring context, component tests, contract tests | The closing slice |
| Edits to existing tests | The slice that changes the behavior, and listed in `tests_expected_to_change` |

## The closing slice

Each repo ends with one closing slice. It adds the component tests that prove the criteria end to end, and contract tests when the repo has them. It runs after the self-check, so those slower tests run once, on code that has already been cleaned up.

The closing slice holds tests only. The self-check has already run by then, so production code added here would never be checked. Behavior that turns up late goes in a new build slice: `plan-revise` can add one at any point, and the script runs the self-check again.

## Trivial work

When `work` is `trivial`, use one slice and no closing slice. The check, the self-check and the full suite still run. The slice still needs a check that can fail, so add or extend a test even for a one-line fix.

## At the end of every slice

- No stubs, no TODOs, no commented-out code.
- The code compiles and the slice's tests pass through `check`.
- Only this slice's files are in the commit list that `check` prints.

## Changing the cut during the build

You may resplit slices that are not done whenever the code shows the cut was wrong. Edit the plan JSON and run `plan-revise`. This needs no approval as long as the criteria, the contract and the scope stay the same. Say what changed in `--summary`.

## Multi-repo cards

List the repos in build order, with the repo that owns the contract first. Each repo has its own slices and its own closing slice. When a repo is done, the script asks for a handoff note for the next one: the names, paths and details that were actually built.
