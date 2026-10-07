# Models

Each agent file names its model in its frontmatter (`model:`). The coordinator is your chat session, so pick its model in the chat's model picker.

| Who | Model | If it is not in your picker | Why |
|---|---|---|---|
| Coordinator (the chat session) | Claude Opus 5.5 | Claude Opus 5 | Triage, merging and severity calls shape the whole review. |
| `pr-reviewer` (every axis) | Claude Opus 5.5 | Claude Opus 5 | Subtle logic, edge cases and rollout problems are where bugs hide. One strong model for every axis keeps judgement consistent. |
| `pr-verifier` | Claude Sonnet 5 | Any strong coding model | Writing and running one focused test is a coding task. |

If an agent fails to start because its model is not available to you, change the `model:` line in that agent file, or delete the line so the agent uses the session's model.

JetBrains has no reasoning-level setting, so each model runs at its default.
