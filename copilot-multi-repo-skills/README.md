# Copilot multi-repo skills

Skills and subagents for GitHub Copilot that find every branch across many repositories that needs a
change, turn the change into a recipe, and roll it out with one pull request per branch: approved up front,
pilots first, in parallel.

- `skills/`: six skills (`repo-scan`, `recipe-authoring`, `multi-repo-rollout`, `change-verification`,
  `pr-follow-through`, `run-retro`). All scripts live in `skills/multi-repo-rollout/`.
- `agents/`: two subagents (`repo-worker`, `change-verifier`).

## Install

Copy `skills/` and `agents/` into your Copilot folder (existing skills and agents are left alone):

```sh
cp -R skills agents ~/.copilot/
```

Windows: copy both folders into `%USERPROFILE%\.copilot`. Requires Python 3.10+ and Git.

Details, settings and usage: [skills/multi-repo-rollout/README.md](skills/multi-repo-rollout/README.md).
Tests, simulations and the sandbox: [skills/multi-repo-rollout/DEV.md](skills/multi-repo-rollout/DEV.md).
