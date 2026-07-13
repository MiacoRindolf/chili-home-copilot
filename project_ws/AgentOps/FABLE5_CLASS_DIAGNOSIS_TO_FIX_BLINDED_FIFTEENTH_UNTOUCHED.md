# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T19:35:47.054797+00:00
- Local model: `qwen2.5-coder:7b`
- Local reasoning model: `qwen3:8b`
- Local escalation model: `disabled`
- Reference family: `claude-fable-5`
- Overall score: **41.9/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **41.9/100**
- Functional sealed-final solve rate: **12.5%**
- Causal-diagnosis accuracy: **25.0%**
- Exact changed-file-set accuracy: **50.0%**
- JSON-valid diagnostic stages: **100.0%**
- Causally accepted diagnoses: **12.5%**
- Live-reasoning-qualified cases: **100.0%**
- Deterministic-only cases: **0.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **278.8s/case**
- Maximum bounded repair rounds: **2**
- Escalation repair rounds: **0**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.
- Test subprocess containment: **static scan + seeded-file SHA-256 guard; not hostile-process proof**.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| th15_d01 | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 40 | code | lib/envelope_codec.dart, lib/varint.dart | true | true | true | false |
| th15_d02 | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 100 | state | lib/asset_cache.dart, lib/recency_index.dart | true | true | true | true |
| th15_n01 | javascript | node_test | blinded_holdout | holdout-sealed-javascript-multifile | 40 | data | src/cookie-jar.mjs, src/set-cookie.mjs | true | true | false | false |
| th15_n02 | javascript | node_test | blinded_holdout | holdout-sealed-javascript-multifile | 25 | data | - | false | true | false | false |
| th15_p01 | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| th15_p02 | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 25 | code | - | false | true | false | false |
| th15_s01 | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 40 | data | - | false | true | false | false |
| th15_s02 | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 40 | code | sql/explode_work_order.sql, sql/required_materials.sql | true | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication. A deterministic-only functional solve keeps its score but cannot receive shadow_ready or support a Fable 5-class reasoning claim. Test subprocesses are not OS-isolated in this runner; static screening and seeded-file mutation hashes reduce but do not eliminate hostile-process risk.
