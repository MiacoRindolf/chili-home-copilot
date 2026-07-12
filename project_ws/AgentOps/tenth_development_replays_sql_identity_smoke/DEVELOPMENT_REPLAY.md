# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-12T15:32:19.939611+00:00
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
- Average wall time: **217.8s/case**
- Maximum bounded repair rounds: **3**
- Escalation repair rounds: **1**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| sql_tenant_grant_intervals | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 100 | data | sql/effective_access.sql, sql/schema.sql, sql/upsert_grant.sql | true | true | true | true |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
