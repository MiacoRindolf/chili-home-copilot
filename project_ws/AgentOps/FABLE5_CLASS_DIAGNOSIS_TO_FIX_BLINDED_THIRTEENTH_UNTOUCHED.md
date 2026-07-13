# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T07:55:55.121922+00:00
- Local model: `qwen2.5-coder:7b`
- Local escalation model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **40.8/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **40.8/100**
- Functional sealed-final solve rate: **16.7%**
- Causal-diagnosis accuracy: **25.0%**
- Exact changed-file-set accuracy: **25.0%**
- JSON-valid diagnostic stages: **100.0%**
- Causally accepted diagnoses: **0.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **207.6s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| th13_dart_dependency_report | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | code | - | false | true | false | false |
| th13_dart_offset_schedule | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 30 | runtime | lib/offset_schedule.dart | true | true | false | false |
| th13_dart_portable_exports | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | data | - | false | true | false | false |
| th13_node_facility_rollup | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 100 | data | src/daily-rollup.mjs, src/observation.mjs | true | true | true | true |
| th13_node_job_recovery | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 40 | code | src/job-runner.mjs, src/job-store.mjs | true | true | true | false |
| th13_node_response_compression | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 100 | config | src/compression-config.mjs, src/content-type-match.mjs | true | true | true | true |
| th13_py_factory_binding | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| th13_py_monthly_settlement | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 30 | code | settlement_runner.py | true | true | false | false |
| th13_py_task_teardown | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| th13_sql_delimited_profile | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 40 | config | - | false | true | false | false |
| th13_sql_export_job_state | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | runtime | - | false | true | false | false |
| th13_sql_package_units | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | code | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
