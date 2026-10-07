# Review axes

Nine axes. Each brief is one question, answered with a senior engineer's judgement and measured against how this repo already does things. The example line shows the kind of thing meant. It is not a checklist.

Copy an axis's brief into its reviewer's prompt, then add your focus lines for this change.

| Id | Axis | When it runs |
|---|---|---|
| `requirements` | Requirements | Always when there is a card or a plan. |
| `correctness` | Correctness | Always when logic changes. |
| `tests` | Test coverage | Always when behavior changes. |
| `failure_handling` | Failure handling | When the change adds a call that can fail, a consumer, or error handling of its own. A new `throw` beside existing ones belongs to `correctness`. |
| `security` | Security and data | When the change takes new input from outside, adds or changes an access check, does new external I/O, handles personal or financial data in a new way, or changes dependencies. |
| `compatibility` | Compatibility, dependencies and rollout | When a contract other code relies on, a config key, a build file, a pipeline or an infrastructure file changes, or deploy order could matter. |
| `design` | Design and implementation quality | When new structure is added: a class, an abstraction, a new way of doing something. |
| `data_access` | Data access and persistence | Only when entities, repositories, queries, migrations, transaction boundaries or datastore config change. |
| `messaging` | Messaging and events | Only when real producers, consumers, topics or event schemas change. An in-process stand-in is not one. |

A change can match a "when it runs" line loosely and still not need the axis. Ask what that reviewer would read that no other running reviewer reads. If the answer is nothing, write a focus line for a neighbouring axis and skip it.

## requirements

**Does the change do what the card asks: all of it, only it, and without breaking what must not change?**
Check each acceptance criterion and each decision against the code, and look for changes the card did not ask for. Example: the card says "refuse approval by the creator", and the code compares ids but lets a null creator through. Also return one line per criterion: implemented, partly, or not, with the place in the code.

## correctness

**Is the logic right on every path, including the ones the author did not think about?**
Example: boundaries, nulls and empties, duplicates, out-of-order input, state combinations, concurrency, and framework behavior that silently does nothing.

## tests

**Is each new or changed behavior proven, at the lowest test level that can prove it?**
Map each new branch to the test that exercises it, by reading the tests. Example: a new `REJECTED` path with no test that reaches it, or a bug fix with no test that fails without the fix. Also return one line per criterion: the test that proves it, or "none".

## failure_handling

**When this goes wrong in production, is it handled on purpose, and can the person on call tell what happened and why?**
Example: a swallowed exception, a call to another system with no timeout and no outcome logged, or an error logged twice with no context.

## security

**Can this be abused, or leak something it should not?**
Example: input used without checking, an access check that can be skipped, personal or financial data written to a log or an event. Say plainly that dependency vulnerabilities are left to the CI scanner.

## compatibility

**Will this deploy safely, and keep everything around it working while old and new versions run side by side?**
Example: a config key the code reads that is missing for one environment, a field removed from an event that a consumer still reads, or a deploy order nobody wrote down.

## design

**Is it built the way this codebase builds things, using what the codebase and its framework already provide, and no more complicated than it needs to be?**
Example: hand-written retry where the framework has one, or an interface with a single implementation. Report a finding only where there is a consequence. You may add at most three optional suggestions, each with a sketch and its payoff.

## data_access

**Is data read and written correctly, efficiently and safely, for this datastore at this scale?**
Name the datastore and access layer first, then judge by their practices. Example: a query in a loop, a new filter with no index, a migration that locks a large table.

## messaging

**Will events be produced and consumed correctly when messages arrive twice, late, out of order or in an old format?**
Example: a consumer that is not safe to run twice on the same message, a database write and a publish that can succeed separately, or a schema change old consumers cannot read.

## The cross-repo axis (only when build-flow asks for it)

**Do the repos in this card agree with each other?**
Read the diffs of every repo together. Example: the producer names a field `occurredAt` and the consumer reads `occurred_at`, the topic or config key is spelled differently, or the merge order would deploy the producer before its consumer exists.
