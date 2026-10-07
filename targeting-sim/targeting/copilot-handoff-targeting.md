# Task: upgrade how the multi-repo rollout skill finds targets

You are working on our multi-repo rollout skill. Given a card describing a change, it:
- writes a recipe;
- finds every repo and branch that needs the change;
- applies the change, commits, pushes and opens PRs.

This task is only about the **finding** part (targeting). The goal is zero silent misses (false negatives) at reasonable speed. Follow every rule below exactly. Where a rule says "script", the step must be enforced in code. Everything else is guidance you apply with judgement.

## How to work

1. **Read the current skill first.** Read its SKILL.md, prompt files, scripts and helpers. Write a short map of how it finds targets today:
   - which SCM calls it makes, which branches it looks at, and how it decides a match;
   - what happens on API errors, rate limits and paging.
2. **Write a gap report.** For each rule below, say: already met / partly met / not met, with the file and line that shows it.
3. **Propose the smallest plan that closes the gaps.** Keep what already works, especially the fast API path. Do not rewrite the skill.
   - Supporting code must stay under about 1,000 lines total.
   - Only the steps marked "script" are forced in code; the rest stays as markdown guidance for the agent.
4. **Stop and wait for my approval of the plan** before changing anything.
5. After implementing, run the acceptance tests at the bottom and report the results.

## Rule 1: a target is a (repo, branch) pair decided from file content

- A target is a specific branch of a specific repo. It is decided by reading the files at that branch's tip, never by repo name or a search hit alone.
- Every target in the output carries evidence: `repo · branch · why the branch is eligible · file · line · matched text`.

## Rule 2: what SCM code search can and cannot do

Facts. Do not rely on memory that says otherwise:
- **GitHub code search** (REST `search/code` and `gh search code`):
  - searches the default branch only;
  - skips files over 350–384 KB, vendored and generated code, and some very large repos;
  - allows 10 requests per minute and returns at most 1,000 results per query;
  - can return `incomplete_results: true`.
- **GitHub GraphQL has no code search.** Its search covers repositories, issues, users and discussions only.
- **Bitbucket Data Center code search:** default branch only, files under 512 KiB, archived repos excluded by default.
- **Index lag:** a repo pushed minutes ago is searched as of its previous commit.

Therefore:
- Search may **add** candidates, never **remove** them. Never treat "no search hit" as "not affected".
- Good uses of search:
  - discovery while writing the check (spellings, file locations, variants);
  - a fast first pass that finds repos to check;
  - a cross-check (see Rule 8).
- For a rollout whose policy is `default` only, search hits may serve as the candidate list if I explicitly accept that. Even then:
  - also check every repo pushed in the last 24 hours (index lag);
  - also check repos whose relevant files are over the size limit.

## Rule 3: write the check before scanning (agent, then a gate)

Before any fan-out, write down a check spec with these fields:

| Field | Meaning |
| --- | --- |
| `globs` | Every file pattern that could prove the change, e.g. `**/pom.xml`, `**/build.gradle`, `gradle/libs.versions.toml`, `**/application*.yml`, `**/application*.properties`, `**/Dockerfile`, `.github/workflows/*.yml`, `**/*.tf`. Prefer wide over clever. |
| `strict` | The exact rule that makes a branch a target. A regex is often not enough; use a small parser when needed (see Rule 6). |
| `loose` | Just the distinctive token, such as `snakeyaml` or `legacy-retry`. It is used to catch spellings the strict rule misses (Rule 8). |
| `policy` | Which branches must be checked (Rule 4). |
| `canary` | One repo/branch known to be affected and one known to be clean. |

**Gate (script):** run the strict check on both canaries before fanning out. If the affected canary isn't flagged, or the clean one is, stop and fix the check.

## Rule 4: branch policies

Pick one per rollout and state it in the output:

| Policy | Branches checked |
| --- | --- |
| `default` | the default branch only |
| `release` | default + the 2 newest `release/*` branches |
| `long_lived` | release + `develop` + every `develop-*` stream with a commit in the last 90 days |
| `active` | long_lived + every `feature/*` with a commit in the last 30 days that is ahead of its base (develop, else default). Branches with nothing ahead are already merged: skip them. |

- Security fixes and deprecations default to `active`.
- A branch outside the policy is never silently dropped. It appears in the ledger as `skipped: out of policy`.

## Rule 5: three ways to read files — choose per rollout

**Lane A — API reads (fastest when you know which files).** Use it when the globs resolve to known paths, on GitHub.
- Read files on any branch with GraphQL `repository { object(expression: "<branch>:<path>") { ... on Blob { text } } }`. Batch about 50 objects per query across repos and branches.
- If paths vary per branch (multi-module POMs, `docker/Dockerfile`), list each **unique commit's** tree with REST `git/trees/<sha>?recursive=1`, then read only the paths matching the globs. Never guess paths: guessed paths missed about 40% of targets in testing.

**Lane B — snapshot clone (when location is unknown, on Bitbucket, or when many files are needed).** Script; the commands and fallbacks must be exact:
- Per repo, 8 in parallel:
  ```
  git clone --no-checkout --no-single-branch --filter=blob:none --shallow-since=<today-120d> <url> <dir>
  ```
  This downloads commits and trees but no file contents and no old history, and writes no working files (important on Windows).
- If it fails with `no commits selected for shallow requests` (a dormant repo), retry with `--depth 1` instead of `--shallow-since`.
- If stderr contains `filtering not recognized by server`, the server sent the full repo. Delete it and re-clone with `--depth 1` (no filter).
- An existing snapshot is refreshed with `git fetch --prune` using the same flags, not re-cloned.
- Branch dates and merged status come from the local graph:
  - `git for-each-ref --format="%(refname:short) %(objectname) %(committerdate:unix)" refs/remotes/origin`
  - `git rev-list --count origin/<base>..origin/<branch>`
- Paths come from the local graph: `git ls-tree -r -l origin/<branch>` (path, blob ID, size; no network).
- Fetch file contents in **one batch** per repo:
  1. Find which blobs are missing with `GIT_NO_LAZY_FETCH=1 git cat-file --batch-check`.
  2. Fetch them all at once:
     ```
     git -c fetch.negotiationAlgorithm=noop fetch origin --no-tags --no-write-fetch-head --recurse-submodules=no --filter=blob:none --stdin
     ```
     with the missing IDs on stdin.
  - Never read blobs one by one before this fetch; each one would be its own network round trip.
- Then search locally with `git grep -E <regex> origin/<branch> -- <pathspecs>`, or read with `git show origin/<branch>:<path>`.
- The snapshot doubles as the working copy for making the change.

**Lane C — the build tool (only for effective versions).** Use it when a parent POM, a BOM or an internal starter decides a version, so the repo may never mention the library.
- Fetch only `**/pom.xml`, lay them out in a temp folder, and run `mvn -q dependency:tree -Dincludes=<group>:<artifact>`. No compile and no sources.
- Use a shared, warmed local Maven repo.
- Run Maven once per unique POM signature (Rule 6), not once per branch.
- If resolution fails (for example, sibling modules not built), mark the branch `error` and run a real build for that one. Never mark it clean.
- Text-level shortcuts may only *add* branches to the Maven list (e.g. the token appears directly). They may never remove one unless that is provable (e.g. no `pom.xml` and no Gradle files at all).

| Situation | Lane |
| --- | --- |
| Known files, GitHub | A |
| Files vary per branch, GitHub, few repos | A with tree listing, or B |
| Match could be in any file | B |
| Bitbucket (no batched reads, one call per file) | B |
| Effective or transitive dependency version | B to find the POMs, then C |

## Rule 6: decide once per unique input (script)

- A branch's verdict depends only on the files under the globs. Build a **signature** per branch: the sorted list of `(path, blob ID)` for those files. Evaluate each signature once and copy the verdict to every branch that shares it. Branches pointing at the same commit share everything.
- For wide greps on big repos:
  1. grep the default branch fully;
  2. for each other branch, use `git diff --name-only origin/<default> origin/<branch>` (local, free);
  3. fetch and grep only the changed files.

  A branch matches if the default branch matches in files the branch didn't change, or the branch's changed files match.
- The check must be a pure function of the files it was given. If it needs another file, that file goes in `globs`.

## Rule 7: coverage ledger (script, hard gate)

Every repo × eligible branch gets exactly one row:

| Status | Meaning |
| --- | --- |
| `matched` | Needs the change; has evidence. |
| `clean` | Checked; does not need it. |
| `skipped:<reason>` | archived, fork, out-of-policy, binary-over-size-cap, language-not-applicable, and so on. |
| `error:<what>` | Anything that failed: timeouts, rate limits, clone failures, Maven failures, access denied. |

Hard rules:
- The ledger total must equal the inventory (all repos) × the branches considered. If it doesn't reconcile, the run fails.
- An `error` is never treated as `clean`. Retry errors once with backoff; anything still failing is listed for me.
- Repos the token cannot see are omitted by the API without an error. Report the inventory count so I can compare it with what I expect.

## Rule 8: loose-vs-strict review list and cross-check (script)

- Run the `loose` token over the same files. Every (repo, branch) the loose token hits but the strict check rejects goes on a **review list** with the matched lines. That is where spelling variants and check bugs show up. I review it before PRs go out.
- If code search was used, every search hit must be either in the target list or explained on the review list. An unexplained hit means the check is wrong.

## Rule 9: plumbing that must not fail silently (script)

- Handle paging fully: GitHub `Link` headers and GraphQL `pageInfo`; Bitbucket `isLastPage` and `nextPageStart`.
- On HTTP 403 or 429, or secondary rate limits, back off and retry. Never record "no match".
- Check `incomplete_results` and the 1,000-result cap. When hit, split the query (by path, language or repo set) or switch that part to Lane B.
- Note: Bitbucket Data Center supports `--filter` (partial clone) from 7.13, if not disabled by an admin. Bitbucket Cloud may not support it; the Rule 5 fallback covers that.

## Rule 10: outputs

1. **Target list.** One line per target, with evidence (Rule 1).
2. **Ledger summary.** Counts per status, plus the full ledger as a file.
3. **Review list** (Rule 8).
4. **One line on what was skipped and why** (policy, archived, forks, size caps).

## Rule 11: sweep after the rollout

After the PRs merge, rerun the `loose` token across the same scope. It should come back empty except for known review-list items. Report anything new.

## Acceptance tests (run after implementing; report results)

1. **Blind-spot probe.** On a test repo, commit a unique string like `targetingprobe` to a non-default branch only. Confirm the skill finds it under policy `active`. Confirm plain code search does not.
2. **Past rollout replay.** Rerun one completed rollout's check with the new targeting. Diff the new target list against what the old approach found, and explain every difference.
3. **Ledger reconciliation.** Show that the ledger total equals inventory × branches considered, and that an injected failure (a bad repo URL) shows up as `error`, not `clean`.
4. **Fallbacks.** Show the `--depth 1` fallback on a repo with no commits in the last 120 days.
5. **Dedupe.** Report how many branches were checked versus how many unique signatures were evaluated.
