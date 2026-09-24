# Changelog

## 4.0.0 (final)
Runtime and developer kit split; `mr help` lists only the commands the skills use; example scaffold removed.
Everything below is included.

## 3.x highlights
- 3.4: every branch is scanned unless the recipe's `Branches:` line narrows it (types and an activity
  cutoff); coverage and possible variants are always reported; a sparse-versus-full audit rechecks everything
  on disagreement; LFS pointers are `unknown`. Git processes −55–70%, ledger halved.
- 3.3: from realistic simulations: CI-config build commands, husky-safe hooks, `feature/…` branch names and
  `mr branches`, scripted redo on moved destinations, grouped questions, suggested pilots, compact output.
- 3.2: reliability: validation bound to its base, replay never loses work, locked discovery, retry keeps
  claims, draft exceptions, lazy clone on decide, CI window reservations, token-to-host verification,
  `diagnose`, `progress`, `clean`.
- 3.1/3.0: drop-in `~/.copilot` layout, one `mr` command, no setup, six skills, two agents.
