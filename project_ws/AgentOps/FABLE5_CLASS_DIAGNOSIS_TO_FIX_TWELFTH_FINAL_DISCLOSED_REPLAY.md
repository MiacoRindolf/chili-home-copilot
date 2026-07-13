# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T05:30:36.517491+00:00
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
- Average wall time: **38.3s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| dart_decimal_apportionment | dart | dart | development_regression | disclosed_replay | 100 | data | lib/minor_units.dart, lib/proportional_allocator.dart | true | true | true | true |
| dart_release_reader_lifecycle | dart | dart | development_regression | disclosed_replay | 100 | state | lib/release_handle.dart, lib/release_manager.dart | true | true | true | true |
| dart_trusted_proxy_chain | dart | dart | development_regression | disclosed_replay | 100 | config | lib/proxy_policy.dart, lib/request_origin.dart | true | true | true | true |
| node_base64url_blob_ids | typescript | node_test | development_regression | disclosed_replay | 100 | data | src/base64url.mjs, src/blob-locator.mjs | true | true | true | true |
| node_policy_reload_consistency | typescript | node_test | development_regression | disclosed_replay | 100 | state | src/audit-log.mjs, src/authorizer.mjs, src/policy-store.mjs | true | true | true | true |
| node_tls_client_auth_config | typescript | node_test | development_regression | disclosed_replay | 100 | config | src/tls-config.mjs, src/tls-options.mjs | true | true | true | true |
| py_config_reload | python | pytest | development_regression | disclosed_replay | 100 | config | gateway_runtime.py, settings_store.py | true | true | true | true |
| py_tail_checkpoint | python | pytest | development_regression | disclosed_replay | 100 | state | file_tailer.py, tail_checkpoint.py | true | true | true | true |
| py_unordered_category_hierarchy | python | pytest | development_regression | disclosed_replay | 100 | data | category_loader.py, category_paths.py | true | true | true | true |
| sql_notification_override_tristate | sql | pytest | development_regression | disclosed_replay | 100 | config | sql/notification_config.sql, sql/notification_resolution.sql | true | true | true | true |
| sql_tenant_stock_ownership | sql | pytest | development_regression | disclosed_replay | 100 | data | sql/availability_view.sql, sql/inventory_catalog.sql | true | true | true | true |
| sql_ticket_archive_transitions | sql | pytest | development_regression | disclosed_replay | 100 | state | sql/project_dashboard.sql, sql/ticket_lifecycle.sql | true | true | true | true |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
