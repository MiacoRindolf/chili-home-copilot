# Node Validation Lane Report

**Verdict: PASS**

- Author root (read only): `D:\dev\chili-thirteenth-author-node`
- Validation root (write only): `D:\dev\chili-thirteenth-validation-lane-node`
- Runtime: `Node.js v24.15.0`
- Cases validated: `3`
- Test timeout: `10000 ms` per command
- External/model/service operations: `none`

## Gate Summary

| Case ID | Dimension | Structural | Execution | Final novelty | Overall |
| --- | --- | --- | --- | --- | --- |
| `th13_node_facility_rollup` | `data` | PASS | PASS | PASS | PASS |
| `th13_node_job_recovery` | `state` | PASS | PASS | PASS | PASS |
| `th13_node_response_compression` | `config` | PASS | PASS | PASS | PASS |

## th13_node_facility_rollup

- Dimension: `data`
- Candidates (3): `src/observation.mjs`, `src/daily-rollup.mjs`, `src/report-service.mjs`
- Expected owners (2/2 max): `src/observation.mjs`, `src/daily-rollup.mjs`
- Mechanism: null-versus-zero preservation through normalization and aggregation.
- Final-test finding: Feedback isolates normalization and rollup units; final composes both through buildDailyReport, crosses two metrics, and verifies mixed null, measured zero, and numeric-string handling end to end.
- Prohibited-overlap finding: Telemetry missing-value preservation and arithmetic rollup do not implement any prohibited mechanism.

### Required Checks

| Gate | Result | Finding |
| --- | --- | --- |
| `json_parse` | PASS | case.json, oracle.json, and final_oracle.json parsed |
| `schema_keys` | PASS | top-level bundle maps have the expected roles |
| `identity` | PASS | directory/case/oracle/final identities=th13_node_facility_rollup/th13_node_facility_rollup/th13_node_facility_rollup |
| `language` | PASS | language=typescript |
| `runner` | PASS | test_runner=node_test |
| `file_maps` | PASS | repo, feedback, and final maps contain source strings |
| `paths_relative_contained_ascii` | PASS | 6 unique declared paths checked |
| `bundle_ascii` | PASS | all bundle bytes and embedded file contents are ASCII |
| `candidate_shape_and_plausibility` | PASS | 3 source candidates are exported and exercised by public tests |
| `owner_set_within_max_files` | PASS | 2 expected owners; max_files=2 |
| `test_map_consistency` | PASS | one public, one feedback, and one final test path with no collisions |
| `public_surface_no_oracle_leak` | PASS | 8 labels, 2 hidden bodies, and 3 hidden titles checked |
| `repair_scope_matches_owners` | PASS | repair owners=src/daily-rollup.mjs, src/observation.mjs |
| `final_not_renamed_or_identical` | PASS | feedback titles=2; final titles=1; semantic review recorded separately |
| `baseline_public_passes` | PASS | tests=3, pass=3, fail=0 |
| `baseline_feedback_fails` | PASS | tests=5, pass=3, fail=2; failing=an unavailable sensor value remains missing during normalization \| a measured zero participates in metric statistics |
| `baseline_final_fails` | PASS | tests=1, pass=0, fail=1; failing=a multi-metric report distinguishes missing data from measured zero end to end |
| `repaired_public_feedback_final_pass` | PASS | tests=6, pass=6, fail=0 |
| `each_owner_is_required` | PASS | src/observation.mjs: tests=6, pass=4, fail=2; failing=an unavailable sensor value remains missing during normalization \| a multi-metric report distinguishes missing data from measured zero end to end; src/daily-rollup.mjs: tests=6, pass=4, fail=2; failing=a measured zero participates in metric statistics \| a multi-metric report distinguishes missing data from measured zero end to end |
| `all_runs_bounded_and_no_materialized_symlinks` | PASS | 6 fresh scenarios, timeout=10000ms each |
| `every_expected_owner_changed` | PASS | 2/2 owners changed |

### Exact Test Commands

| Scenario | CWD | Command | Exit | Concise output |
| --- | --- | --- | ---: | --- |
| `01-baseline-public` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_facility_rollup\01-baseline-public` | `node --test --test-reporter=tap tests/public.test.mjs` | 0 | tests=3, pass=3, fail=0 |
| `02-baseline-feedback` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_facility_rollup\02-baseline-feedback` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs` | 1 | tests=5, pass=3, fail=2; failing=an unavailable sensor value remains missing during normalization \| a measured zero participates in metric statistics |
| `03-baseline-final` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_facility_rollup\03-baseline-final` | `node --test --test-reporter=tap tests/final.test.mjs` | 1 | tests=1, pass=0, fail=1; failing=a multi-metric report distinguishes missing data from measured zero end to end |
| `04-repaired-all` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_facility_rollup\04-repaired-all` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 0 | tests=6, pass=6, fail=0 |
| `05-omit-observation` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_facility_rollup\05-omit-observation` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 1 | tests=6, pass=4, fail=2; failing=an unavailable sensor value remains missing during normalization \| a multi-metric report distinguishes missing data from measured zero end to end |
| `06-omit-daily-rollup` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_facility_rollup\06-omit-daily-rollup` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 1 | tests=6, pass=4, fail=2; failing=a measured zero participates in metric statistics \| a multi-metric report distinguishes missing data from measured zero end to end |

### Repair Ownership Hashes

| Owner | Baseline SHA-256 | Repaired SHA-256 | Changed |
| --- | --- | --- | --- |
| `src/observation.mjs` | `2a6302f5c7c9186d18616224605ffa7a93482c2534318840281e5f477a6260a2` | `164acd0f45aeeeb3fbf40af1c9587494c50acfaecbbc2de780a5e0aa024c2563` | yes |
| `src/daily-rollup.mjs` | `69e039391a365eec1a5503b5c06a26d66f619511146e4d9501c7a4dc696153a2` | `e22fc8e5cb8775d95c5bb7975b603541773b92592f1972f526fc1e7e15417715` | yes |

## th13_node_job_recovery

- Dimension: `state`
- Candidates (3): `src/job-store.mjs`, `src/job-runner.mjs`, `src/job-events.mjs`
- Expected owners (2/2 max): `src/job-store.mjs`, `src/job-runner.mjs`
- Mechanism: attempt-token fencing for late asynchronous job settlement.
- Final-test finding: Feedback covers stale completion and the runner success path; final adds the distinct stale-failure transition and verifies Error identity plus attempt forwarding on the runner failure path.
- Prohibited-overlap finding: Attempt fencing for watchdog-requeued jobs is not release-reader retirement, configuration replacement/reload, or another prohibited mechanism.

### Required Checks

| Gate | Result | Finding |
| --- | --- | --- |
| `json_parse` | PASS | case.json, oracle.json, and final_oracle.json parsed |
| `schema_keys` | PASS | top-level bundle maps have the expected roles |
| `identity` | PASS | directory/case/oracle/final identities=th13_node_job_recovery/th13_node_job_recovery/th13_node_job_recovery |
| `language` | PASS | language=typescript |
| `runner` | PASS | test_runner=node_test |
| `file_maps` | PASS | repo, feedback, and final maps contain source strings |
| `paths_relative_contained_ascii` | PASS | 6 unique declared paths checked |
| `bundle_ascii` | PASS | all bundle bytes and embedded file contents are ASCII |
| `candidate_shape_and_plausibility` | PASS | 3 source candidates are exported and exercised by public tests |
| `owner_set_within_max_files` | PASS | 2 expected owners; max_files=2 |
| `test_map_consistency` | PASS | one public, one feedback, and one final test path with no collisions |
| `public_surface_no_oracle_leak` | PASS | 8 labels, 2 hidden bodies, and 4 hidden titles checked |
| `repair_scope_matches_owners` | PASS | repair owners=src/job-runner.mjs, src/job-store.mjs |
| `final_not_renamed_or_identical` | PASS | feedback titles=2; final titles=2; semantic review recorded separately |
| `baseline_public_passes` | PASS | tests=3, pass=3, fail=0 |
| `baseline_feedback_fails` | PASS | tests=5, pass=3, fail=2; failing=the store rejects completion from an attempt the watchdog replaced \| the runner identifies the claim when reporting successful work |
| `baseline_final_fails` | PASS | tests=2, pass=0, fail=2; failing=a late failure cannot send a replacement attempt back to the queue \| the runner identifies the claim when reporting failed work |
| `repaired_public_feedback_final_pass` | PASS | tests=7, pass=7, fail=0 |
| `each_owner_is_required` | PASS | src/job-store.mjs: tests=7, pass=4, fail=3; failing=the store rejects completion from an attempt the watchdog replaced \| a late failure cannot send a replacement attempt back to the queue \| a failed export returns to the queue and can be retried; src/job-runner.mjs: tests=7, pass=3, fail=4; failing=the runner identifies the claim when reporting successful work \| the runner identifies the claim when reporting failed work \| a claimed export completes normally |
| `all_runs_bounded_and_no_materialized_symlinks` | PASS | 6 fresh scenarios, timeout=10000ms each |
| `every_expected_owner_changed` | PASS | 2/2 owners changed |

### Exact Test Commands

| Scenario | CWD | Command | Exit | Concise output |
| --- | --- | --- | ---: | --- |
| `01-baseline-public` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_job_recovery\01-baseline-public` | `node --test --test-reporter=tap tests/public.test.mjs` | 0 | tests=3, pass=3, fail=0 |
| `02-baseline-feedback` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_job_recovery\02-baseline-feedback` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs` | 1 | tests=5, pass=3, fail=2; failing=the store rejects completion from an attempt the watchdog replaced \| the runner identifies the claim when reporting successful work |
| `03-baseline-final` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_job_recovery\03-baseline-final` | `node --test --test-reporter=tap tests/final.test.mjs` | 1 | tests=2, pass=0, fail=2; failing=a late failure cannot send a replacement attempt back to the queue \| the runner identifies the claim when reporting failed work |
| `04-repaired-all` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_job_recovery\04-repaired-all` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 0 | tests=7, pass=7, fail=0 |
| `05-omit-job-store` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_job_recovery\05-omit-job-store` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 1 | tests=7, pass=4, fail=3; failing=the store rejects completion from an attempt the watchdog replaced \| a late failure cannot send a replacement attempt back to the queue \| a failed export returns to the queue and can be retried |
| `06-omit-job-runner` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_job_recovery\06-omit-job-runner` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 1 | tests=7, pass=3, fail=4; failing=the runner identifies the claim when reporting successful work \| the runner identifies the claim when reporting failed work \| a claimed export completes normally |

### Repair Ownership Hashes

| Owner | Baseline SHA-256 | Repaired SHA-256 | Changed |
| --- | --- | --- | --- |
| `src/job-store.mjs` | `5848323b96477bd4a4eb0551d00e070a894c0d2b4205830520b835da9ad4596f` | `397c550fb3fce516f395a9881ec73f4cae43a1d465276a61ecbbc87f3b35cf7f` | yes |
| `src/job-runner.mjs` | `1b91feac757f1f7b01144f02f9a4a31079545610626493bbf0e0de659bb7d42f` | `95b8f46b3b9e7c4aecdafcb6b0dcc979ce6d1e97198f8ee21fa8434f7bd8a813` | yes |

## th13_node_response_compression

- Dimension: `config`
- Candidates (3): `src/compression-config.mjs`, `src/content-type-match.mjs`, `src/compress-response.mjs`
- Expected owners (2/2 max): `src/compression-config.mjs`, `src/content-type-match.mjs`
- Mechanism: comma-list token normalization combined with media-type parameter matching.
- Final-test finding: Feedback tests parser and matcher separately; final composes them through createCompressionDecider and adds case normalization, an unlisted binary guard, and the non-string response boundary.
- Prohibited-overlap finding: Static startup parsing and response matching perform no live replacement configuration reload and no other prohibited mechanism.

### Required Checks

| Gate | Result | Finding |
| --- | --- | --- |
| `json_parse` | PASS | case.json, oracle.json, and final_oracle.json parsed |
| `schema_keys` | PASS | top-level bundle maps have the expected roles |
| `identity` | PASS | directory/case/oracle/final identities=th13_node_response_compression/th13_node_response_compression/th13_node_response_compression |
| `language` | PASS | language=typescript |
| `runner` | PASS | test_runner=node_test |
| `file_maps` | PASS | repo, feedback, and final maps contain source strings |
| `paths_relative_contained_ascii` | PASS | 6 unique declared paths checked |
| `bundle_ascii` | PASS | all bundle bytes and embedded file contents are ASCII |
| `candidate_shape_and_plausibility` | PASS | 3 source candidates are exported and exercised by public tests |
| `owner_set_within_max_files` | PASS | 2 expected owners; max_files=2 |
| `test_map_consistency` | PASS | one public, one feedback, and one final test path with no collisions |
| `public_surface_no_oracle_leak` | PASS | 8 labels, 2 hidden bodies, and 3 hidden titles checked |
| `repair_scope_matches_owners` | PASS | repair owners=src/compression-config.mjs, src/content-type-match.mjs |
| `final_not_renamed_or_identical` | PASS | feedback titles=2; final titles=1; semantic review recorded separately |
| `baseline_public_passes` | PASS | tests=3, pass=3, fail=0 |
| `baseline_feedback_fails` | PASS | tests=5, pass=3, fail=2; failing=comma-separated deployment values ignore surrounding whitespace \| Content-Type parameters do not change the configured media type match |
| `baseline_final_fails` | PASS | tests=1, pass=0, fail=1; failing=spaced deployment configuration matches parameterized handler responses end to end |
| `repaired_public_feedback_final_pass` | PASS | tests=6, pass=6, fail=0 |
| `each_owner_is_required` | PASS | src/compression-config.mjs: tests=6, pass=4, fail=2; failing=comma-separated deployment values ignore surrounding whitespace \| spaced deployment configuration matches parameterized handler responses end to end; src/content-type-match.mjs: tests=6, pass=4, fail=2; failing=Content-Type parameters do not change the configured media type match \| spaced deployment configuration matches parameterized handler responses end to end |
| `all_runs_bounded_and_no_materialized_symlinks` | PASS | 6 fresh scenarios, timeout=10000ms each |
| `every_expected_owner_changed` | PASS | 2/2 owners changed |

### Exact Test Commands

| Scenario | CWD | Command | Exit | Concise output |
| --- | --- | --- | ---: | --- |
| `01-baseline-public` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_response_compression\01-baseline-public` | `node --test --test-reporter=tap tests/public.test.mjs` | 0 | tests=3, pass=3, fail=0 |
| `02-baseline-feedback` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_response_compression\02-baseline-feedback` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs` | 1 | tests=5, pass=3, fail=2; failing=comma-separated deployment values ignore surrounding whitespace \| Content-Type parameters do not change the configured media type match |
| `03-baseline-final` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_response_compression\03-baseline-final` | `node --test --test-reporter=tap tests/final.test.mjs` | 1 | tests=1, pass=0, fail=1; failing=spaced deployment configuration matches parameterized handler responses end to end |
| `04-repaired-all` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_response_compression\04-repaired-all` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 0 | tests=6, pass=6, fail=0 |
| `05-omit-compression-config` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_response_compression\05-omit-compression-config` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 1 | tests=6, pass=4, fail=2; failing=comma-separated deployment values ignore surrounding whitespace \| spaced deployment configuration matches parameterized handler responses end to end |
| `06-omit-content-type-match` | `D:\dev\chili-thirteenth-validation-lane-node\_tmp\th13_node_response_compression\06-omit-content-type-match` | `node --test --test-reporter=tap tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs` | 1 | tests=6, pass=4, fail=2; failing=Content-Type parameters do not change the configured media type match \| spaced deployment configuration matches parameterized handler responses end to end |

### Repair Ownership Hashes

| Owner | Baseline SHA-256 | Repaired SHA-256 | Changed |
| --- | --- | --- | --- |
| `src/compression-config.mjs` | `84e2dd45575d3d8e1048dddc9a375c1fcfdf42e3036e435adc5ea9c8fa789f46` | `b2cc4222a9dce6b41b641a83ce963d064d523ca718c505ef43d828407d2c714d` | yes |
| `src/content-type-match.mjs` | `eddf382ba9274103f6be253210824b7b5b52085a653adf31d714d0446a99c887` | `5bab579168a5949312d1ff80eca60710b0d4f6b58ce8408ef3efdee3d891497e` | yes |

## Cross-Case Review

- Unique dimensions: `config`, `data`, `state`.
- Mechanisms: null-versus-zero preservation through normalization and aggregation; attempt-token fencing for late asynchronous job settlement; comma-list token normalization combined with media-type parameter matching.
- Source skeletons: pure normalization and statistical rollup functions composed by a report service; mutable class-backed job store coordinated by an asynchronous runner; configuration parser and pure predicate composed by a decision factory.
- Assertion families: aggregate object and numeric-statistic invariants; state-transition rejection and claim-identity argument invariants; normalized token-list equality and Boolean decision-matrix invariants.
- Final boundaries: multi-metric report-service composition across null, zero, and numeric-string values; stale failure rejection plus runner failure-token forwarding; factory-decider integration with spaced config, parameterized types, binary exclusion, and undefined input.
- Duplicate review: No mechanism, source skeleton, semantic assertion family, or final boundary is duplicated across the three cases.
- Prohibited mechanisms reviewed: `fixed-point apportionment`, `release-reader retirement`, `trusted proxy CIDR chains`, `canonical base64url`, `request policy snapshots`, `TLS client authentication`, `replacement config reload`, `source-aware tail checkpoints`, `unordered category hierarchy`, `tri-state override SQL`, `composite tenant stock ownership`, `ticket archive/move accounting`.
- Prohibited-overlap result: No exact prohibited phrase occurs, and manual semantic review found no material overlap. The compression case uses static parsing only, not replacement/reload behavior.

## Frozen Author Inventory

Canonical digest format: UTF-8 lines sorted by relative path: <lowercase-file-sha256><two spaces><forward-slash-path><LF>.
- Before sorted inventory SHA-256: `789542593f8fafa5fbd323aaabf56ad99cb3ea733344ebb84a2f2049e59953c7`
- After sorted inventory SHA-256: `789542593f8fafa5fbd323aaabf56ad99cb3ea733344ebb84a2f2049e59953c7`
- Physical symlinks before/after: `0/0`
- Author tree unchanged: `true`

| Author file | Before SHA-256 | After SHA-256 | Unchanged |
| --- | --- | --- | --- |
| `AUTHOR_RECEIPT.md` | `701dd6714520577479134476b555d68029a1ddebfa7df2a041e75ccd95b460b0` | `701dd6714520577479134476b555d68029a1ddebfa7df2a041e75ccd95b460b0` | yes |
| `th13_node_facility_rollup/case.json` | `0ed58bccf859151c4fbc461c8afe793f70fc05050172de4175011457ecad951f` | `0ed58bccf859151c4fbc461c8afe793f70fc05050172de4175011457ecad951f` | yes |
| `th13_node_facility_rollup/final_oracle.json` | `0d368ec47b9165462815dde4b2fcea52a011387e93b23956c05b89388b650ff7` | `0d368ec47b9165462815dde4b2fcea52a011387e93b23956c05b89388b650ff7` | yes |
| `th13_node_facility_rollup/oracle.json` | `2f67641daf429c8a969ec5ab4efe5439a4b173ec445c2c493d3e0e1acf8c9bdc` | `2f67641daf429c8a969ec5ab4efe5439a4b173ec445c2c493d3e0e1acf8c9bdc` | yes |
| `th13_node_job_recovery/case.json` | `bb32a60140b3b3863fcd3b9f8467b22f2f033594d51c12177baf297d76fac8d1` | `bb32a60140b3b3863fcd3b9f8467b22f2f033594d51c12177baf297d76fac8d1` | yes |
| `th13_node_job_recovery/final_oracle.json` | `ba14e700a88f25ce0655010d84719cab6a96d4a09030fd01c223f9828eeb5910` | `ba14e700a88f25ce0655010d84719cab6a96d4a09030fd01c223f9828eeb5910` | yes |
| `th13_node_job_recovery/oracle.json` | `012d157421fa001f74465a0f887ae1ed156bf05b4111623b7a826bfb4a096eba` | `012d157421fa001f74465a0f887ae1ed156bf05b4111623b7a826bfb4a096eba` | yes |
| `th13_node_response_compression/case.json` | `7eb9ff54c94516b078dc221c623b39d34c6ee132aa7384e11e377cd49295a2ac` | `7eb9ff54c94516b078dc221c623b39d34c6ee132aa7384e11e377cd49295a2ac` | yes |
| `th13_node_response_compression/final_oracle.json` | `2162a2880c77ec92f44238360eba2f7fec6933201e66b6dfc8c3fff6f9db0bc1` | `2162a2880c77ec92f44238360eba2f7fec6933201e66b6dfc8c3fff6f9db0bc1` | yes |
| `th13_node_response_compression/oracle.json` | `b4a511a77692333bb0f3882f7189aff5af282fbac97f6bdd005303c42ce10cbf` | `b4a511a77692333bb0f3882f7189aff5af282fbac97f6bdd005303c42ce10cbf` | yes |

## Findings

- No rejecting findings. Every required gate was directly verified.
- The author bundles were not modified; repairs existed only in fresh validation copies under the temporary output directory.

## Supplemental Characterization

This supplement preserves the prior PASS verdict, gate findings, and all 18 recorded test commands above.

### Source Skeleton Normalization

Normalization ID: `js-ts-source-skeleton-v1`.

- Input is each baseline candidate source string from `case.json` `repo_files`, interpreted as UTF-8.
- Remove line and block comments.
- Replace single-quoted, double-quoted, and template string literals (including payloads) with `STR`.
- Replace decimal, binary, octal, hexadecimal, exponent, and bigint numeric literal payloads with `NUM`.
- Preserve ECMAScript control, declaration, and literal keywords; collapse other ASCII identifiers to `ID`.
- Preserve operators and punctuation, discard original whitespace, then join tokens with one ASCII space and no trailing newline.
- `source_skeleton_sha256` is lowercase SHA-256 of the normalized UTF-8 bytes.
- For `combined_source_skeleton_sha256`, sort candidates by ASCII path and concatenate `<path><LF><normalized-text><LF>` for each before hashing the UTF-8 bytes.

### th13_node_facility_rollup

- `dimension`: `data`
- `mechanism`: null-versus-zero preservation through normalization and aggregation.
- `assertion_family`: aggregate object and numeric-statistic invariants.
- `feedback_boundary`: Unit boundary: null preservation in normalizeObservation and measured-zero inclusion in summarizeMetric are asserted independently.
- `final_boundary`: multi-metric report-service composition across null, zero, and numeric-string values.
- `final_novelty`: Feedback isolates normalization and rollup units; final composes both through buildDailyReport, crosses two metrics, and verifies mixed null, measured zero, and numeric-string handling end to end.

| Candidate path | source_skeleton_sha256 | Normalized tokens |
| --- | --- | ---: |
| `src/observation.mjs` | `d95434cd39d9ff6016b5097df79477b40fac25975d6c048b3b81454f09de25d2` | 134 |
| `src/daily-rollup.mjs` | `c93f95276e5e97b51c991ad99507b21068fd31c97875a26e62b014e35ffa8900` | 133 |
| `src/report-service.mjs` | `daa4cc3be43963ffdb5f6ccdbf934a2947865467174a4d15d4072c6974008e5b` | 67 |

- `combined_source_skeleton_sha256`: `01945435a9bb47029ff2a21284dd404ad55727fec521eab8c0450743e81d696b`

### th13_node_job_recovery

- `dimension`: `state`
- `mechanism`: attempt-token fencing for late asynchronous job settlement.
- `assertion_family`: state-transition rejection and claim-identity argument invariants.
- `feedback_boundary`: Store/runner API boundary: stale completion rejection and successful runner forwarding of the claim attempt are asserted independently.
- `final_boundary`: stale failure rejection plus runner failure-token forwarding.
- `final_novelty`: Feedback covers stale completion and the runner success path; final adds the distinct stale-failure transition and verifies Error identity plus attempt forwarding on the runner failure path.

| Candidate path | source_skeleton_sha256 | Normalized tokens |
| --- | --- | ---: |
| `src/job-store.mjs` | `128697f0a5869cae3bf8ea3c4c95f9233aab6756a4e609400deecda4b3a6efcb` | 360 |
| `src/job-runner.mjs` | `0c9cdbfc5dbf660f4505742f946401b6081f81b6fd80340cfc2dd9a4130c70c7` | 161 |
| `src/job-events.mjs` | `168e916664279cdf06b2bcc01c18114c0d53bd06799e18b387826bd71981f99b` | 21 |

- `combined_source_skeleton_sha256`: `8e43d8921c0c0f74a0e01605d343ec5cb4dc7097b72cb442b84f5e424a11a719`

### th13_node_response_compression

- `dimension`: `config`
- `mechanism`: comma-list token normalization combined with media-type parameter matching.
- `assertion_family`: normalized token-list equality and Boolean decision-matrix invariants.
- `feedback_boundary`: Unit boundary: comma-list whitespace normalization and parameter-insensitive Content-Type matching are asserted independently.
- `final_boundary`: factory-decider integration with spaced config, parameterized types, binary exclusion, and undefined input.
- `final_novelty`: Feedback tests parser and matcher separately; final composes them through createCompressionDecider and adds case normalization, an unlisted binary guard, and the non-string response boundary.

| Candidate path | source_skeleton_sha256 | Normalized tokens |
| --- | --- | ---: |
| `src/compression-config.mjs` | `1ccf07f070bce89911f739bbfef7d3cf7809a3e5bd4789fb088a95b3b44a3094` | 68 |
| `src/content-type-match.mjs` | `cf1bd8879325354c06ffb4be0e139716aca5a21fefa60b34a9fa621b7d74567a` | 34 |
| `src/compress-response.mjs` | `5673cd00dc2063147cb251793d5f46c175c19a1ecbac5d172933f1d862c75179` | 47 |

- `combined_source_skeleton_sha256`: `fc645df81cb3ff9cc6903470a8d68c0fe0943e0ca4d498ef2ff22551dd18bd35`

## Current Complete Author Inventory

- Current file count: `10`.
- Prior before sorted inventory SHA-256: `789542593f8fafa5fbd323aaabf56ad99cb3ea733344ebb84a2f2049e59953c7`.
- Prior after sorted inventory SHA-256: `789542593f8fafa5fbd323aaabf56ad99cb3ea733344ebb84a2f2049e59953c7`.
- Current sorted inventory SHA-256: `789542593f8fafa5fbd323aaabf56ad99cb3ea733344ebb84a2f2049e59953c7`.
- Current physical symlinks: `0`.
- Current matches prior before inventory: `true`.
- Current matches prior after inventory: `true`.
- Every current per-file hash matches both prior columns: `true`.

| Author file | Bytes | Prior before SHA-256 | Prior after SHA-256 | Current SHA-256 | Match |
| --- | ---: | --- | --- | --- | --- |
| `AUTHOR_RECEIPT.md` | 2619 | `701dd6714520577479134476b555d68029a1ddebfa7df2a041e75ccd95b460b0` | `701dd6714520577479134476b555d68029a1ddebfa7df2a041e75ccd95b460b0` | `701dd6714520577479134476b555d68029a1ddebfa7df2a041e75ccd95b460b0` | yes |
| `th13_node_facility_rollup/case.json` | 3982 | `0ed58bccf859151c4fbc461c8afe793f70fc05050172de4175011457ecad951f` | `0ed58bccf859151c4fbc461c8afe793f70fc05050172de4175011457ecad951f` | `0ed58bccf859151c4fbc461c8afe793f70fc05050172de4175011457ecad951f` | yes |
| `th13_node_facility_rollup/final_oracle.json` | 1071 | `0d368ec47b9165462815dde4b2fcea52a011387e93b23956c05b89388b650ff7` | `0d368ec47b9165462815dde4b2fcea52a011387e93b23956c05b89388b650ff7` | `0d368ec47b9165462815dde4b2fcea52a011387e93b23956c05b89388b650ff7` | yes |
| `th13_node_facility_rollup/oracle.json` | 1216 | `2f67641daf429c8a969ec5ab4efe5439a4b173ec445c2c493d3e0e1acf8c9bdc` | `2f67641daf429c8a969ec5ab4efe5439a4b173ec445c2c493d3e0e1acf8c9bdc` | `2f67641daf429c8a969ec5ab4efe5439a4b173ec445c2c493d3e0e1acf8c9bdc` | yes |
| `th13_node_job_recovery/case.json` | 4721 | `bb32a60140b3b3863fcd3b9f8467b22f2f033594d51c12177baf297d76fac8d1` | `bb32a60140b3b3863fcd3b9f8467b22f2f033594d51c12177baf297d76fac8d1` | `bb32a60140b3b3863fcd3b9f8467b22f2f033594d51c12177baf297d76fac8d1` | yes |
| `th13_node_job_recovery/final_oracle.json` | 1776 | `ba14e700a88f25ce0655010d84719cab6a96d4a09030fd01c223f9828eeb5910` | `ba14e700a88f25ce0655010d84719cab6a96d4a09030fd01c223f9828eeb5910` | `ba14e700a88f25ce0655010d84719cab6a96d4a09030fd01c223f9828eeb5910` | yes |
| `th13_node_job_recovery/oracle.json` | 1736 | `012d157421fa001f74465a0f887ae1ed156bf05b4111623b7a826bfb4a096eba` | `012d157421fa001f74465a0f887ae1ed156bf05b4111623b7a826bfb4a096eba` | `012d157421fa001f74465a0f887ae1ed156bf05b4111623b7a826bfb4a096eba` | yes |
| `th13_node_response_compression/case.json` | 2820 | `7eb9ff54c94516b078dc221c623b39d34c6ee132aa7384e11e377cd49295a2ac` | `7eb9ff54c94516b078dc221c623b39d34c6ee132aa7384e11e377cd49295a2ac` | `7eb9ff54c94516b078dc221c623b39d34c6ee132aa7384e11e377cd49295a2ac` | yes |
| `th13_node_response_compression/final_oracle.json` | 738 | `2162a2880c77ec92f44238360eba2f7fec6933201e66b6dfc8c3fff6f9db0bc1` | `2162a2880c77ec92f44238360eba2f7fec6933201e66b6dfc8c3fff6f9db0bc1` | `2162a2880c77ec92f44238360eba2f7fec6933201e66b6dfc8c3fff6f9db0bc1` | yes |
| `th13_node_response_compression/oracle.json` | 1130 | `b4a511a77692333bb0f3882f7189aff5af282fbac97f6bdd005303c42ce10cbf` | `b4a511a77692333bb0f3882f7189aff5af282fbac97f6bdd005303c42ce10cbf` | `b4a511a77692333bb0f3882f7189aff5af282fbac97f6bdd005303c42ce10cbf` | yes |

