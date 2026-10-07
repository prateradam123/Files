# Simulation results

2730 targeting runs across 5 fleets, 21 scenarios, 13 strategies, 2 SCM profiles.

## Headline — github

| strategy | recall (mean / worst run) | precision | runs fully right | time, light repos | time, realistic x5 | time, worst case | data moved | calls |
|---|---|---|---|---|---|---|---|---|
| search-default | 37% / 0% | 56% | 1/84 | 1s | 1s | 1s | 0 MB | 1 |
| search-then-verify | 83% / 0% | 100% | 48/84 | 20s | 20s | 21s | 6 MB | 140 |
| api-guess-paths | 60% / 0% | 100% | 28/84 | 4s | 4s | 4s | 1 MB | 15 |
| api-tree-probe | 100% / 100% | 100% | 84/84 | 83s | 83s | 84s | 25 MB | 455 |
| api-tree-probe+dedupe | 100% / 100% | 100% | 84/84 | 36s | 36s | 37s | 11 MB | 268 |
| full-clone | 100% / 100% | 100% | 84/84 | 13s | 40s | 412s | 409 MB | 69 |
| shallow-all-branches | 100% / 100% | 98% | 51/84 | 11s | 28s | 73s | 264 MB | 69 |
| api-refs+shallow-fetch | 100% / 100% | 100% | 84/84 | 12s | 29s | 73s | 258 MB | 74 |
| api-refs+blobless-probe | 100% / 100% | 100% | 84/84 | 13s | 13s | 24s | 8 MB | 140 |
| blobless-full-history | 100% / 100% | 100% | 84/84 | 12s | 13s | 67s | 27 MB | 135 |
| blobless-recent-history | 100% / 100% | 100% | 84/84 | 12s | 13s | 27s | 25 MB | 135 |
| protocol | 100% / 100% | 100% | 84/84 | 12s | 13s | 27s | 25 MB | 135 |
| protocol+search-prefilter | 99% / 71% | 100% | 81/84 | 11s | 12s | 24s | 22 MB | 118 |

## Headline — bitbucket

| strategy | recall (mean / worst run) | precision | runs fully right | time, light repos | time, realistic x5 | time, worst case | data moved | calls |
|---|---|---|---|---|---|---|---|---|
| search-default | 37% / 0% | 57% | 2/84 | 1s | 1s | 1s | 0 MB | 1 |
| search-then-verify | 83% / 0% | 100% | 48/84 | 134s | 134s | 134s | 6 MB | 2120 |
| api-guess-paths | 60% / 0% | 100% | 28/84 | 32s | 32s | 32s | 2 MB | 506 |
| api-tree-probe | 100% / 100% | 100% | 84/84 | 750s | 750s | 751s | 25 MB | 11984 |
| api-tree-probe+dedupe | 100% / 100% | 100% | 84/84 | 249s | 249s | 250s | 10 MB | 3983 |
| full-clone | 100% / 100% | 100% | 84/84 | 13s | 39s | 412s | 409 MB | 69 |
| shallow-all-branches | 100% / 100% | 98% | 51/84 | 10s | 27s | 72s | 264 MB | 69 |
| api-refs+shallow-fetch | 100% / 100% | 100% | 84/84 | 18s | 35s | 79s | 259 MB | 192 |
| api-refs+blobless-probe | 100% / 100% | 100% | 84/84 | 19s | 19s | 30s | 9 MB | 259 |
| blobless-full-history | 100% / 100% | 100% | 84/84 | 11s | 13s | 67s | 27 MB | 135 |
| blobless-recent-history | 100% / 100% | 100% | 84/84 | 11s | 13s | 27s | 25 MB | 135 |
| protocol | 100% / 100% | 100% | 84/84 | 11s | 13s | 27s | 25 MB | 136 |
| protocol+search-prefilter | 99% / 71% | 100% | 81/84 | 10s | 12s | 24s | 22 MB | 119 |

## Recall by signal class (GitHub)

| strategy | manifest | config | resolution | code | cross-file |
|---|---|---|---|---|---|
| search-default | 42% | 4% | 11% | 45% | 50% |
| search-then-verify | 88% | 45% | 100% | 76% | 100% |
| api-guess-paths | 85% | 74% | 93% | 0% | 41% |
| protocol+search-prefilter | 99% | 100% | 100% | 97% | 100% |
| protocol | 100% | 100% | 100% | 100% | 100% |

- **search-then-verify** missed 1388 of 9027 targets: {'not-default-branch': 1357, 'index-lag': 23, 'file-too-large': 8}
- **search-default** missed 6978 of 9027 targets: {'not-default-branch': 6452, 'query-or-resolution': 461, 'index-lag': 57, 'file-too-large': 8}
- **protocol+search-prefilter** missed 10 of 9027 targets: {'index-lag': 6, 'file-too-large': 4}

## Per scenario (GitHub, 120-repo fleets)

| scenario | policy | class | targets | search-default | search-then-verify | guess paths | protocol | protocol time |
|---|---|---|---|---|---|---|---|---|
| S01 snakeyaml < 2.0 on the classpath (direct, property, parent-managed, transitive) | active | resolution | 182 | 5% | 100% | 100% | 100% | 14s |
| S02 log4j-core < 2.17.1 declared anywhere (incl. nested modules) | active | manifest | 102 | 10% | 49% | 100% | 100% | 14s |
| S03 Java version below 21 | long_lived | manifest | 115 | 30% | 100% | 100% | 100% | 13s |
| S04 Deprecated config key acme.kafka.consumer.legacy-retry (nested YAML, flat, .properties, env-only) | active | config | 173 | 8% | 90% | 59% | 100% | 14s |
| S05 Deprecated class LegacyRetryTemplate used in production code (not tests/comments/docs) | active | code | 123 | 12% | 63% | 0% | 100% | 14s |
| S06 Kafka topic contract.status.v1 referenced by production config or code | active | code | 60 | 13% | 59% | 0% | 100% | 14s |
| S07 Has a @KafkaListener but no dead-letter config (cross-file, absence) | default | cross-file | 21 | 100% | 100% | 0% | 100% | 13s |
| S08 Dockerfile on eclipse-temurin:17 (any Dockerfile path) | release | manifest | 52 | 39% | 70% | 82% | 100% | 17s |
| S09 CI workflow pinned to actions/checkout@v3 | default | manifest | 56 | 100% | 100% | 61% | 100% | 18s |
| S10 Terraform ECS module pinned below v2 | release | manifest | 12 | 57% | 100% | 49% | 100% | 2s |
| S11 axios < 1.6.0 (direct or transitive via lockfile; lockfiles often > 384 KB) | active | manifest | 34 | 15% | 90% | 100% | 100% | 3s |
| S12 Deprecated endpoint /v1/contracts/legacy-status in resources (incl. huge OpenAPI specs) | active | code | 58 | 14% | 66% | 0% | 100% | 14s |
| S13 jackson-databind < 2.15 on supported release branches | release | manifest | 64 | 40% | 100% | 100% | 100% | 13s |
| S14 Spring Boot < 3.2 (direct parent, Gradle plugin, or via internal acme-parent) | long_lived | resolution | 93 | 18% | 100% | 85% | 100% | 13s |
| S15 Hard-coded acme.auth.client-secret (mostly appears on in-flight branches) | active | config | 34 | 0% | 0% | 84% | 100% | 14s |
| S16 Internal acme-kafka-starter on 3.x (literal, property or version catalog) | long_lived | manifest | 116 | 33% | 100% | 100% | 100% | 13s |
| S17 Kafka consumer whose application-prod.yml lacks max.poll.interval.ms (absence) | default | cross-file | 24 | 0% | 100% | 82% | 100% | 13s |
| S18 Python requests < 2.31 (requirements*.txt in any folder, or pyproject) | active | manifest | 36 | 6% | 76% | 67% | 100% | 2s |
| S19 Default branch only: deprecated endpoint in OpenAPI specs (some specs > 384 KB) | default | code | 9 | 87% | 87% | 0% | 100% | 13s |
| S20 Default branch only: axios < 1.6.0 incl. lockfile-only (big lockfiles) | default | manifest | 5 | 100% | 100% | 100% | 100% | 2s |
| S21 Default branch only: LegacyRetryTemplate in production code (some repos pushed minutes ago) | default | code | 15 | 98% | 98% | 0% | 100% | 13s |

## Scaling

| fleet | repos | strategy | profile | mean time (light) | mean time (realistic x5) | calls |
|---|---|---|---|---|---|---|
| big.json | 300 | protocol | github | 30s | 34s | 341 |
| big.json | 300 | protocol | bitbucket | 29s | 34s | 341 |
| big.json | 300 | api-refs+blobless-probe | github | 32s | 33s | 352 |
| big.json | 300 | api-refs+blobless-probe | bitbucket | 49s | 50s | 674 |
| big.json | 300 | shallow-all-branches | github | 28s | 79s | 173 |
| big.json | 300 | shallow-all-branches | bitbucket | 28s | 78s | 173 |
| big.json | 300 | full-clone | github | 34s | 107s | 173 |
| big.json | 300 | full-clone | bitbucket | 33s | 106s | 173 |
| big.json | 300 | search-then-verify | github | 50s | 50s | 353 |
| big.json | 300 | search-then-verify | bitbucket | 349s | 349s | 5558 |
| f1.json | 120 | protocol | github | 11s | 13s | 130 |
| f1.json | 120 | protocol | bitbucket | 11s | 13s | 130 |
| f1.json | 120 | api-refs+blobless-probe | github | 12s | 13s | 134 |
| f1.json | 120 | api-refs+blobless-probe | bitbucket | 18s | 19s | 252 |
| f1.json | 120 | shallow-all-branches | github | 10s | 26s | 66 |
| f1.json | 120 | shallow-all-branches | bitbucket | 10s | 25s | 66 |
| f1.json | 120 | full-clone | github | 13s | 40s | 66 |
| f1.json | 120 | full-clone | bitbucket | 13s | 40s | 66 |
| f1.json | 120 | search-then-verify | github | 20s | 20s | 138 |
| f1.json | 120 | search-then-verify | bitbucket | 136s | 136s | 2152 |
| f2.json | 120 | protocol | github | 13s | 14s | 147 |
| f2.json | 120 | protocol | bitbucket | 12s | 14s | 148 |
| f2.json | 120 | api-refs+blobless-probe | github | 14s | 14s | 153 |
| f2.json | 120 | api-refs+blobless-probe | bitbucket | 20s | 21s | 280 |
| f2.json | 120 | shallow-all-branches | github | 12s | 33s | 75 |
| f2.json | 120 | shallow-all-branches | bitbucket | 12s | 33s | 75 |
| f2.json | 120 | full-clone | github | 14s | 45s | 75 |
| f2.json | 120 | full-clone | bitbucket | 14s | 45s | 75 |
| f2.json | 120 | search-then-verify | github | 22s | 22s | 153 |
| f2.json | 120 | search-then-verify | bitbucket | 149s | 149s | 2369 |
| f3.json | 120 | protocol | github | 12s | 13s | 134 |
| f3.json | 120 | protocol | bitbucket | 11s | 13s | 134 |
| f3.json | 120 | api-refs+blobless-probe | github | 13s | 13s | 139 |
| f3.json | 120 | api-refs+blobless-probe | bitbucket | 19s | 19s | 260 |
| f3.json | 120 | shallow-all-branches | github | 10s | 25s | 68 |
| f3.json | 120 | shallow-all-branches | bitbucket | 10s | 25s | 68 |
| f3.json | 120 | full-clone | github | 12s | 34s | 68 |
| f3.json | 120 | full-clone | bitbucket | 12s | 34s | 68 |
| f3.json | 120 | search-then-verify | github | 20s | 20s | 139 |
| f3.json | 120 | search-then-verify | bitbucket | 134s | 134s | 2126 |
| f4.json | 120 | protocol | github | 11s | 13s | 130 |
| f4.json | 120 | protocol | bitbucket | 11s | 12s | 130 |
| f4.json | 120 | api-refs+blobless-probe | github | 12s | 13s | 134 |
| f4.json | 120 | api-refs+blobless-probe | bitbucket | 18s | 18s | 242 |
| f4.json | 120 | shallow-all-branches | github | 10s | 27s | 66 |
| f4.json | 120 | shallow-all-branches | bitbucket | 10s | 26s | 66 |
| f4.json | 120 | full-clone | github | 13s | 39s | 66 |
| f4.json | 120 | full-clone | bitbucket | 12s | 38s | 66 |
| f4.json | 120 | search-then-verify | github | 18s | 18s | 128 |
| f4.json | 120 | search-then-verify | bitbucket | 116s | 116s | 1834 |

