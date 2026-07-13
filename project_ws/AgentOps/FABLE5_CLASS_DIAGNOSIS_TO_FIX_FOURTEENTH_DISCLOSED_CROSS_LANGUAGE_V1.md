# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-13T17:31:45.807478+00:00
- Local model: `qwen2.5-coder:7b`
- Local reasoning model: `qwen3:8b`
- Local escalation model: `disabled`
- Reference family: `claude-fable-5`
- Overall score: **35.0/100**
- Development-regression score: **35.0/100**
- Blinded holdout score: **not run**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **66.7%**
- Exact changed-file-set accuracy: **0.0%**
- JSON-valid diagnostic stages: **100.0%**
- Causally accepted diagnoses: **0.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **disclosed_replay_failed**
- Premium calls: **0**
- Average wall time: **251.8s/case**
- Maximum bounded repair rounds: **2**
- Escalation repair rounds: **0**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| th14_dart_semver_selection | dart | dart | development_regression | disclosed_replay | 25 | code | - | false | true | false | false |
| th14_py_decorated_handlers | python | pytest | development_regression | disclosed_replay | 40 | runtime | - | false | true | false | false |
| th14_sql_suppression_batches | sql | pytest | development_regression | disclosed_replay | 40 | data | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
