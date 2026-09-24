---
name: repo-scan
description: Answer a question across many repos and branches quickly and read-only - which repos have a setting, what version of a framework or dependency each is on, who still uses an API, inventories and audits. Use for "which of our services...", "list the Spring Boot version in every repo in ABC", "find repos where X is enabled", even when no change is planned. Never changes anything.
---

# Repo scan

Read-only. Nothing is committed, pushed or opened, and no run or Jira card is needed. Commands: see
[rules.md](../multi-repo-rollout/rules.md) for what `mr` means.

## 1. The scope

- **One or more projects** (needs `BITBUCKET_TOKEN` or `GITHUB_TOKEN`): `--project ABC --host bitbucket.example.com`.
  It lists every repo (all pages), skipping archived, fork and empty ones.
- **Otherwise,** a file with one `<name> <clone-url>` per line, built with your SCM tool: `--repos <file>`.

Branches: every branch of every repo, unless the recipe's `Branches:` line names branch types and/or an
activity cutoff. For a bare script: `--pattern '<glob>'` (repeatable) and `--days N`. The default branch is
exempt from the cutoff. Nothing is left out silently: the summary's `coverage` counts what was excluded by
type, age or as a rollout branch, and lists `possible_variants` (excluded names that look like an included type).

## 2. The question

- **An existing recipe** asks "does this need the change?": `--check <recipe name or folder>`.
- **Anything else,** write a small check script. Save it as `~/.multi-repo/scans/<name>.py`. It runs inside
  a checkout of one commit and prints one JSON line:
  ```
  {"status": "compliant|needs_change|not_applicable|unknown", "value": "<what you found>", "evidence": "<file and line>"}
  ```
  Put what you're inventorying in `value` (a version, a flag, a URL); the report groups repos by it. Use
  `not_applicable` when the thing doesn't exist, and `unknown` when you can't tell. Read only the files
  you need with real parsers (json, xml.etree, tomllib), not regex. For effective Maven or Gradle versions,
  call the build tool.
- List the files the script reads with `--paths`. Only those are downloaded, and identical files across
  repos and branches are checked once. Globs are allowed (`--paths pom.xml '**/pom.xml'`). Without
  `--paths`, every branch needs a full checkout, which is much slower.

## 3. Run it

```sh
mr scan --check ~/.multi-repo/scans/boot-version.py --paths pom.xml --project ABC --name boot-versions
```
Add `--via api` when an API token is set and `--paths` are exact file names (no globs, no
`--pattern`). Then only those files are read through the API, with no clone at all.

It prints a summary (`by_status`, `by_value`, `not_scanned`) and writes `report.md`, `results.csv` and
`results.json` to `~/.multi-repo/scans/<name>/`. Show the user the summary and the `By value` table; give
the paths for the rest. Repos in `not_scanned` are usually access problems: list them and say why. `audit` shows a sample of sparse verdicts
re-checked on full checkouts; if they disagreed, everything was re-checked in full (use `--full` next time,
and complete `--paths`).

**Try the script first** on one repo:
`mr check --recipe <folder with the script as check.py> --repo ~/.multi-repo/clones/<repo> --ref origin/<branch>`,
or just scan a repos file with one line in it.

**Next steps the user may want:** a recipe from the results (`recipe-authoring`), or a rollout
(`multi-repo-rollout` with the same scope).
