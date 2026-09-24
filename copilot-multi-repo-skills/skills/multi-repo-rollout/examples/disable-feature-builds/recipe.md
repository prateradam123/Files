# Disable feature builds

Status: trialed (sandbox) · Mode: scripted · Risk: low · Validation: full · Toolkit: 2.x
Branches: all
Check paths: config.json, build.properties

## Outcome
Feature-branch builds burn CI minutes. Set the `featureBuilds` flag to `false` wherever it is explicitly
`true`. Done means the check reports `compliant` on the destination branch.

## Where it applies
- Files: `config.json` (top-level `"featureBuilds"`) or `build.properties` (`featureBuilds=`).
- Branches: the development base plus `feature/*` branches with commits in the last 30 days.
  A compliant `main` does not exempt an active feature branch.
- Not applicable: never returned by this check; a repo with no build config at all still needs a person
  to confirm the flag isn't inherited.
- Unknown: the setting is missing, duplicated, not a literal boolean, or the file doesn't parse.

## Before and after

```diff
   "name": "direct",
-  "featureBuilds": true
+  "featureBuilds": false
```

```diff
 app=props
-featureBuilds=true
+featureBuilds=false
```

Counterexample: `"featureBuildsEnabledBy": "ops"` is a different key and must not change.

## How check.py decides
- compliant: every file that has the flag has it literally `false`.
- needs_change: at least one file has it literally `true` and nothing is ambiguous.
- unknown: missing everywhere, duplicated, non-boolean, or unparseable (evidence says which).

## How the change is made
`apply.py` flips the literal `true` to `false` using `scripts/config_edit.py`, preserving every other byte.
It refuses YAML, duplicates, and expressions. For those, an agent may make the equivalent edit in the
target worktree and run `recipe_run.py capture`; ask first if the flag is inherited from a parent config.
Never add the flag where it is missing, and never change other keys.

## Validation
Run the repo's build (repo-facts `build`). The diff must touch only the flag line.

## Trial log
| Date | Repo @ branch | Check | Diff reviewed | Notes |
|---|---|---|---|---|
| 2026-09-23 | sandbox demo/direct @ main | needs_change → compliant | yes | JSON |
| 2026-09-23 | sandbox demo/properties @ main | needs_change → compliant | yes | properties |
| 2026-09-23 | sandbox demo/duplicate @ main | unknown | n/a | duplicate key refused |
