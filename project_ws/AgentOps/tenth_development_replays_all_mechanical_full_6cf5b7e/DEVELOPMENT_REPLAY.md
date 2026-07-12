# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-12T18:57:19.023739+00:00
- Local model: `qwen2.5-coder:7b`
- Local escalation model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **100.0/100**
- Functional sealed-final solve rate: **100.0%**
- Causal-diagnosis accuracy: **100.0%**
- Exact changed-file-set accuracy: **100.0%**
- Accepted diagnostic stages: **100.0%**
- Autonomy verdict: **shadow_ready**
- Comparison verdict: **blinded_evaluation_passed**
- Premium calls: **0**
- Average wall time: **75.7s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| py_relay_rotation_window | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 100 | dependency | relay/canonical.py, relay/keys.py, relay/verifier.py | true | true | true | true |
| py_reservation_retry_scope | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 100 | state | inventory/events.py, inventory/ledger.py, inventory/service.py | true | true | true | true |
| ts_http_vary_isolation | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 100 | config | src/cache.mjs, src/key.mjs, src/vary.mjs | true | true | true | true |
| ts_retry_budget_clock | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 100 | clock | src/budget.mjs, src/retry_after.mjs, src/scheduler.mjs | true | true | true | true |
| dart_offline_tombstone_join | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 100 | state | lib/merge_policy.dart, lib/sync_engine.dart, lib/vector_clock.dart | true | true | true | true |
| dart_resumable_chunk_boundaries | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 100 | data | lib/assembler.dart, lib/chunk_ledger.dart, lib/content_range.dart | true | true | true | true |
| sql_tenant_grant_intervals | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 100 | data | sql/effective_access.sql, sql/schema.sql, sql/upsert_grant.sql | true | true | true | true |
| sql_telemetry_correction_rollup | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 100 | data | sql/hourly_rollup.sql, sql/telemetry_schema.sql, sql/upsert_reading.sql | true | true | true | true |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
