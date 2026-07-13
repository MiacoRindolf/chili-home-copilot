# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T10:31:38.782293+00:00
- Local model: `qwen2.5-coder:7b`
- Local escalation model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Development-regression score: **100.0/100**
- Blinded holdout score: **not run**
- Functional sealed-final solve rate: **100.0%**
- Causal-diagnosis accuracy: **100.0%**
- Exact changed-file-set accuracy: **100.0%**
- JSON-valid diagnostic stages: **100.0%**
- Causally accepted diagnoses: **100.0%**
- Autonomy verdict: **shadow_ready**
- Comparison verdict: **disclosed_replay_passed**
- Premium calls: **0**
- Average wall time: **159.7s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| th13_dart_dependency_report | dart | dart | development_regression | disclosed_replay | 100 | dependency | lib/license_gate.dart, lib/scan_report_adapter.dart | true | true | true | true |
| th13_dart_offset_schedule | dart | dart | development_regression | disclosed_replay | 100 | clock | lib/daily_window.dart, lib/offset_schedule.dart | true | true | true | true |
| th13_dart_portable_exports | dart | dart | development_regression | disclosed_replay | 100 | code | lib/export_name.dart, lib/report_bundle.dart | true | true | true | true |
| th13_node_facility_rollup | typescript | node_test | development_regression | disclosed_replay | 100 | data | src/daily-rollup.mjs, src/observation.mjs | true | true | true | true |
| th13_node_job_recovery | typescript | node_test | development_regression | disclosed_replay | 100 | state | src/job-runner.mjs, src/job-store.mjs | true | true | true | true |
| th13_node_response_compression | typescript | node_test | development_regression | disclosed_replay | 100 | config | src/compression-config.mjs, src/content-type-match.mjs | true | true | true | true |
| th13_py_factory_binding | python | pytest | development_regression | disclosed_replay | 100 | dependency | dependency_plan.py, service_container.py | true | true | true | true |
| th13_py_monthly_settlement | python | pytest | development_regression | disclosed_replay | 100 | clock | billing_clock.py, settlement_runner.py | true | true | true | true |
| th13_py_task_teardown | python | pytest | development_regression | disclosed_replay | 100 | runtime | task_runtime.py, teardown_stack.py | true | true | true | true |
| th13_sql_delimited_profile | sql | pytest | development_regression | disclosed_replay | 100 | config | sql/render_customer_row.sql, sql/render_order_row.sql | true | true | true | true |
| th13_sql_export_job_state | sql | pytest | development_regression | disclosed_replay | 100 | state | sql/claim_export_job.sql, sql/finish_export_job.sql | true | true | true | true |
| th13_sql_package_units | sql | pytest | development_regression | disclosed_replay | 100 | data | sql/oversize_queue.sql, sql/package_volume.sql | true | true | true | true |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
