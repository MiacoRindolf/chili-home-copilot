# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-14T02:20:31.234125+00:00
- Local model: `qwen2.5-coder:7b`
- Local reasoning model: `qwen3:8b`
- Local escalation model: `disabled`
- Reference family: `claude-fable-5`
- Overall score: **27.5/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **27.5/100**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **0.0%**
- Exact changed-file-set accuracy: **25.0%**
- JSON-parsed diagnostic stages: **68.8%**
- Accepted diagnostic stages: **62.5%**
- Causally accepted diagnoses: **0.0%**
- Live-reasoning-qualified cases: **75.0%**
- Deterministic-only cases: **25.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **487.5s/case**
- Maximum bounded repair rounds: **2**
- Escalation repair rounds: **0**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.
- Test subprocess containment: **static scan + seeded-file SHA-256 guard; not hostile-process proof**.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| node_coalesced_abort_poison | javascript | node_test | blinded_holdout | holdout_sealed_javascript_multifile | 25 | state | - | false | true | false | false |
| node_wrapped_sequence_window | javascript | node_test | blinded_holdout | holdout_sealed_javascript_multifile | 35 | state | src/replica.js, src/serialOrder.js | false | true | false | false |
| python_persisted_rate_window | python | pytest | blinded_holdout | holdout_sealed_python_multifile | 35 | runtime | quota/limiter.py, quota/snapshot.py | false | true | false | false |
| python_invoice_residual_allocation | python | pytest | blinded_holdout | holdout_sealed_python_multifile | 25 | code | - | false | true | false | false |
| dart_streaming_event_text | dart | dart | blinded_holdout | holdout_sealed_dart_multifile | 25 | runtime | - | false | true | false | false |
| dart_sensor_sentinel_signedness | dart | dart | blinded_holdout | holdout_sealed_dart_multifile | 25 | data | - | false | true | false | false |
| sqlite_effective_price_intervals | sql | pytest | blinded_holdout | holdout_sealed_sql_multifile | 25 | data | - | false | true | false | false |
| sqlite_tenant_external_identity | sql | pytest | blinded_holdout | holdout_sealed_sql_multifile | 25 | data | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication. A deterministic-only functional solve keeps its score but cannot receive shadow_ready or support a Fable 5-class reasoning claim. Test subprocesses are not OS-isolated in this runner; static screening and seeded-file mutation hashes reduce but do not eliminate hostile-process risk.
