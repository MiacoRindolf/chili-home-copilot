# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T15:05:50.429252+00:00
- Local model: `qwen2.5-coder:7b`
- Local escalation model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **27.9/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **27.9/100**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **8.3%**
- Exact changed-file-set accuracy: **8.3%**
- JSON-valid diagnostic stages: **100.0%**
- Causally accepted diagnoses: **0.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **521.6s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| th14_dart_redirect_handoffs | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | config | - | false | true | false | false |
| th14_dart_semver_selection | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | code | - | false | true | false | false |
| th14_dart_websocket_fragments | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 25 | code | - | false | true | false | false |
| th14_node_esm_plugin_loading | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 40 | code | src/package-exports.mjs, src/plugin-loader.mjs | true | true | false | false |
| th14_node_http_preconditions | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 25 | code | - | false | true | false | false |
| th14_node_partition_commits | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 30 | code | src/batch-consumer.mjs | true | true | false | false |
| th14_py_context_offload | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | data | - | false | true | false | false |
| th14_py_decorated_handlers | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| th14_py_link_pagination | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| th14_sql_partner_search | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | code | - | false | true | false | false |
| th14_sql_registry_refresh | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | data | - | false | true | false | false |
| th14_sql_suppression_batches | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 40 | data | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
