# Author Receipt

Authoring root: `D:\dev\chili-thirteenth-author-node`

Runtime: Node.js `v24.15.0`

Test framework: built-in `node:test`

## Bundle inventory

| Case ID | Dimension | Expected source owners |
| --- | --- | --- |
| `th13_node_job_recovery` | `state` | `src/job-store.mjs`, `src/job-runner.mjs` |
| `th13_node_facility_rollup` | `data` | `src/observation.mjs`, `src/daily-rollup.mjs` |
| `th13_node_response_compression` | `config` | `src/compression-config.mjs`, `src/content-type-match.mjs` |

Each run below used a separately materialized repository inside the authoring root. Public runs contained only `case.json` `repo_files`. Feedback runs added only `oracle.json` `feedback_files`. Final runs started from a fresh baseline and added only `final_oracle.json` `final_files`.

## Baseline results

### th13_node_job_recovery

- Public command: `node --test tests/public.test.mjs`
  Result: exit `0`; 3 passed, 0 failed.
- Feedback command: `node --test tests/public.test.mjs tests/feedback.test.mjs`
  Result: exit `1`; 3 passed, 2 failed.
- Final command in a fresh baseline: `node --test tests/final.test.mjs`
  Result: exit `1`; 0 passed, 2 failed.

### th13_node_facility_rollup

- Public command: `node --test tests/public.test.mjs`
  Result: exit `0`; 3 passed, 0 failed.
- Feedback command: `node --test tests/public.test.mjs tests/feedback.test.mjs`
  Result: exit `1`; 3 passed, 2 failed.
- Final command in a fresh baseline: `node --test tests/final.test.mjs`
  Result: exit `1`; 0 passed, 1 failed.

### th13_node_response_compression

- Public command: `node --test tests/public.test.mjs`
  Result: exit `0`; 3 passed, 0 failed.
- Feedback command: `node --test tests/public.test.mjs tests/feedback.test.mjs`
  Result: exit `1`; 3 passed, 2 failed.
- Final command in a fresh baseline: `node --test tests/final.test.mjs`
  Result: exit `1`; 0 passed, 1 failed.

## Coordinated-edit audit

For each case, an author-only temporary repair modified exactly the two `expected_files`, then ran:

`node --test tests/public.test.mjs tests/feedback.test.mjs tests/final.test.mjs`

| Case ID | Full repair | First owner only | Second owner only |
| --- | --- | --- | --- |
| `th13_node_job_recovery` | exit 0; 7 passed | exit 1 | exit 1 |
| `th13_node_facility_rollup` | exit 0; 6 passed | exit 1 | exit 1 |
| `th13_node_response_compression` | exit 0; 6 passed | exit 1 | exit 1 |

All bundle files are self-contained and ASCII. The cases and tests use only standard Node.js APIs. No network, service, credential, destructive, trading, external dependency, or external model operation was used.
