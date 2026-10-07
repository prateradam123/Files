# Targeting: find every (repo, branch) that needs the change

Guidance for the multi-repo rollout skill. The numbers come from the targeting sandbox (5 synthetic fleets,
21 remediation scenarios, 13 strategies, GitHub and Bitbucket profiles, 2,730 simulated runs, each re-costed under 8 network and repo-weight conditions).

## The rule everything follows from

A target is a **(repo, branch) pair**, decided by **reading file content at that branch's tip**.
Repo-level answers and index-level answers are hints, never the target list.

## Never do these

| Don't | Why (sandbox result) |
|---|---|
| Patch the default branch of every code-search hit | 37% recall, 56% precision. Most targets live on develop, streams, releases or in-flight features, which no SCM search indexes. |
| Use code search to pick repos, then check their branches | Still only 83% recall. Problems that exist only on in-flight branches (a hard-coded secret on a feature branch) are invisible: 0% recall. |
| Probe a few guessed paths (`pom.xml`, `Dockerfile`) | 60% recall. Misses nested modules, `docker/Dockerfile`, `requirements/prod.txt`, version catalogs, env-only config. |
| Read files one by one over Bitbucket REST | About 12,000 calls per run on 120 repos (12.5 minutes). GitHub GraphQL batching makes API probing possible, but it is still 3–7× slower than a snapshot. |
| Full or shallow clone to target | Correct, but cost grows with repo weight and history. Worst case: 412 s (full) and 73 s (shallow) against 27 s for the snapshot. |
| `--depth 1` clones of all branches without merge info | Shallow history cannot tell a merged feature branch from a live one, so some merged branches get patched (precision 98%). |

## The protocol

1. **Write the check before scanning anything.** It has three parts:
   - globs: which files can prove it;
   - rule: what makes this branch a target;
   - branch policy: which branches must be checked.

   Run it on one repo you know is affected and one you know is clean. A wrong check, fanned out, is the most expensive mistake available.
2. **Inventory (API).** List the org or project repos. Drop archived repos and forks. Filter by language only when the check is language-bound; Dockerfiles and CI workflows apply everywhere.
3. **Snapshot every repo (git, 8 workers).** Run `snapshot.py snapshot-many repos.txt work/`, which does a blobless clone of all branches with recent history:
   - `git clone --no-checkout --no-single-branch --filter=blob:none --shallow-since=<120 days>`
   - Cost stays flat however heavy the repo or long its history: commits and trees only, no file contents.
4. **Branches (local).** `snapshot.py branches <repo> --policy <policy>`. Tip dates and merged status come from the local graph, so the SCM branch APIs aren't needed; this matters on Bitbucket.
5. **Evidence (local + one fetch per repo).** Paths are listed locally from trees. `snapshot.py fetch` pulls only the blobs under your globs, on the eligible branches, in a single round trip. Then apply the rule with `snapshot.py grep`, `snapshot.py show` or your own parser.
6. **Hand over a target list with evidence.** Each entry is `repo, branch, why-eligible, file, line`. The snapshot is already a working copy, so the change step checks out the target branch from it with no second clone.

This scored 100% recall and 100% precision in all 210 of its runs (21 scenarios × 5 fleets of up to 300 repos × 2 SCM profiles). Its cost stayed roughly flat across:
- light vs 20× heavier repos;
- 100 vs 25 Mbit;
- short vs 10× longer histories.

It works the same on GitHub and Bitbucket. `snapshot.py` was checked against the sandbox's ground truth:
- eligible branches matched in 109/109 repos;
- an end-to-end config-key check found 182/182 targets with no extras.

## Branch policies

| policy | branches |
|---|---|
| `default` | default branch only (CI pins, repo hygiene) |
| `release` | default + the 2 newest `release/*` (hotfix-able code) |
| `long_lived` | release + `develop` + `develop-*` streams with a commit in the last 90 days |
| `active` | long_lived + `feature/*` with a commit in the last 30 days that is ahead of its base (merged branches are skipped) |

Security fixes and deprecations default to `active`. Pick narrower only on purpose, and say so in the run log.

## Writing the check, by kind of signal

- **Manifest** (versions, images, pins):
  - Read every build file. Look at `**/pom.xml` (modules!), not just the root, as well as `build.gradle` and `gradle/libs.versions.toml`.
  - Resolve `${property}` indirection.
  - Check `**/Dockerfile`, all `.github/workflows/*.yml`, and every `*.tf` in env folders.
- **Config keys**:
  - YAML is nested, so `acme.kafka.consumer.legacy-retry` is never on one line. Flatten it, or match the leaf key under its parents.
  - Also check `.properties` and env-only files (`application-dev.yml`).
- **Resolution** (effective versions):
  - A parent POM, a BOM or an internal starter decides the version, and the repo may never mention the library.
  - Read the internal lib at the referenced tag (`git show <tag>:pom.xml` in a snapshot of that lib). In the sandbox, 65% of the vulnerable snakeyaml targets had no "snakeyaml" text anywhere in their build files.
  - When transitive resolution really matters, run the build tool (`mvn -q dependency:tree`) on the *targets only*, never as the scan.
- **Code usage**:
  - Restrict to production sources (`src/main/**`) and strip comments.
  - Docs, tests and comments mentioning the name are not usages.
- **Cross-file and absence** ("has a listener but no DLQ config", "prod yml lacks X"):
  - These need file reads. Search cannot find something that is absent, and searching for the fix finds the repos that are already fine.

## When code search is still useful

- **While writing the check.** Discover spellings, file locations and variants on default branches before you fix the globs and rule.
- **As a cross-check on the check.** Every default-branch search hit that your rule rejected is either a true negative you can explain, or a bug in the rule. It's cheap: one query.
- **Never as a filter.** "Search to pick repos, snapshot only those" saved about 10% time. It still lost 20 targets in 10 of its 210 runs, mostly to index lag: a repo pushed minutes ago is searched as of its previous commit. The 384 KB (GitHub) and 512 KiB (Bitbucket DC) file limits drop big lockfiles and OpenAPI specs.

## Gotchas (all hit in the sandbox or verified in docs)

- **Dormant repos:** `--shallow-since` fails with *"no commits selected for shallow requests"* when a repo has no commit in the window. Retry with `--depth 1` (4 of 109 repos). `snapshot.py` does this.
- **No partial-clone support:** a server prints *"filtering not recognized by server"* and silently sends the full repo. Detect it and fall back to `--depth 1`.
  - Bitbucket Data Center supports `--filter` from 7.13 on (default on, admin can disable).
  - Bitbucket Cloud support is unconfirmed.
- **Lazy fetching:** reading blobs one by one in a blobless clone fetches each in its own round trip. Batch them: `snapshot.py fetch` sends all missing blob ids in one `git fetch --stdin`.
- **GitHub code search:**
  - default branch only, files under 384 KB;
  - 10 requests per minute, at most 1,000 results per query.
  - GraphQL has no code search at all.
- **Bitbucket DC code search:** default branch only, files under 512 KiB, archived repos excluded by default.

## What it costs (120-repo fleets, mean per rollout)

| approach | recall | realistic repos | worst case (VPN, heavy, long history, slow auth) |
|---|---|---|---|
| protocol (blobless + recent history) | 100% | 13 s | 27 s |
| API refs + blobless fetch | 100% | 13 s (GH) / 19 s (BB) | 24 s / 30 s |
| API refs + shallow fetch | 100% | 29 s / 35 s | 73 s / 79 s |
| full clone | 100% | 40 s | 412 s |
| GitHub GraphQL tree probe (deduped) | 100% | 36 s | 37 s |
| Bitbucket REST tree probe (deduped) | 100% | 249 s | 250 s |
| search, then verify branches | 83% | 20 s (GH) / 134 s (BB) | same |
| search, patch default | 38% | 1 s | 1 s |
