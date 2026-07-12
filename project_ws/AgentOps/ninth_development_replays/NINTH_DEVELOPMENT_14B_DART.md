# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-12T09:36:19.889782+00:00
- Local model: `qwen2.5-coder:14b`
- Reference family: `claude-fable-5`
- Overall score: **80.0/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **80.0/100**
- Functional sealed-final solve rate: **100.0%**
- Causal-diagnosis accuracy: **0.0%**
- Exact changed-file-set accuracy: **100.0%**
- Accepted diagnostic stages: **0.0%**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **1062.3s/case**
- Maximum bounded repair rounds: **3**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| dart-profile-patch-null | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 80 | code | lib/patch.dart, lib/profile_store.dart | true | true | true | true |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
