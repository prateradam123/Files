# Learning

## When the user reacts to a finding

When the user says a finding was useful or noise ("finding 3 is noise, an interceptor already logs that"), add one line to `~/.copilot/pr-review/feedback.md`:

```
| date | repo | PR or branch | axis | kind of finding | useful or noise | the user's reason |
```

Record the pattern, not the code. Use the user's own words as the reason. Ask nothing.

A decision is not feedback. "Leave Q1" or "not for this card" answers a question. Record a line only when the user says a finding was good, wrong, or not worth raising.

## The retro, when the user asks for one ("pr-review retro")

1. Read the feedback file.
2. Per axis: how many findings, and how many were useful.
3. For a pattern that was noise more than once, propose the exact wording change to that axis's brief in `axes.md`.
4. For something reviewers missed more than once in this codebase, propose one line for `known-traps.md`.
5. These are proposals. Change the files only when the user says so.

## Known traps

`known-traps.md` starts empty on purpose. The reviewers work from judgement. A trap is added only after real reviews missed it more than once.
