# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-12T23:42:04.804326+00:00
- Local model: `qwen2.5-coder:7b`
- Local escalation model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **32.9/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **32.9/100**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **33.3%**
- Exact changed-file-set accuracy: **8.3%**
- Accepted diagnostic stages: **100.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **1215.9s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| dart_lifecycle_handoff_epoch | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 30 | state | lib/connectivity_monitor.dart, lib/lease_pool.dart | true | true | false | false |
| dart_offline_rebase_barrier | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 40 | state | - | false | true | false | false |
| node-cache-locale-order-retry-slot | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 30 | data | src/catalog-cache-key.mjs, src/catalog-service.mjs | true | true | true | false |
| node-jsonseq-utf8-channel-sequence | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 25 | runtime | - | false | true | false | false |
| node-lifecycle-preabort-generation | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 40 | state | - | false | true | false | false |
| node-policy-empty-overlay-scope-algebra | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 40 | config | - | false | true | false | false |
| py_atomic_idempotent_transfer | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 30 | test_harness | payments/service.py | true | true | false | false |
| py_epoch_fenced_reconnect | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | state | - | false | true | false | false |
| py_layered_config_client_rotation | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 55 | dependency | delivery/client_pool.py, delivery/config.py, delivery/router.py | true | true | false | false |
| py_utf8_stream_envelope | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 30 | code | relay_protocol/codec.py, relay_protocol/gateway.py | true | true | false | false |
| sql_out_of_order_document_heads | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | clock | - | false | true | false | false |
| sql_settlement_lifecycle_reassignment | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 25 | code | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
