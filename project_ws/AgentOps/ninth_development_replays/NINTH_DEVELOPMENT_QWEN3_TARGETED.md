# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-12T09:00:56.535383+00:00
- Local model: `qwen3:8b`
- Reference family: `claude-fable-5`
- Overall score: **47.5/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **47.5/100**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **50.0%**
- Exact changed-file-set accuracy: **0.0%**
- Accepted diagnostic stages: **100.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **640.1s/case**
- Maximum bounded repair rounds: **3**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| py-config-explicit-values | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 65 | config | settings/cli.py | true | true | false | false |
| dart-profile-patch-null | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 30 | state | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
