# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T04:13:00.250117+00:00
- Local model: `qwen2.5-coder:7b`
- Local escalation model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **32.9/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **32.9/100**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **41.7%**
- Exact changed-file-set accuracy: **0.0%**
- JSON-valid diagnostic stages: **100.0%**
- Causally accepted diagnoses: **0.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **341.1s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| dart_decimal_apportionment | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | code | - | false | true | false | false |
| dart_release_reader_lifecycle | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | runtime | - | false | true | false | false |
| dart_trusted_proxy_chain | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | code | - | false | true | false | false |
| node_base64url_blob_ids | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 40 | data | - | false | true | false | false |
| node_policy_reload_consistency | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 25 | config | - | false | true | false | false |
| node_tls_client_auth_config | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 45 | config | src/tls-options.mjs | true | true | false | false |
| py_config_reload | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 45 | config | gateway_runtime.py | true | true | true | false |
| py_tail_checkpoint | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| py_unordered_category_hierarchy | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 45 | data | category_loader.py | true | true | false | false |
| sql_notification_override_tristate | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | code | - | false | true | false | false |
| sql_tenant_stock_ownership | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 45 | data | sql/availability_view.sql | true | true | false | false |
| sql_ticket_archive_transitions | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | data | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
