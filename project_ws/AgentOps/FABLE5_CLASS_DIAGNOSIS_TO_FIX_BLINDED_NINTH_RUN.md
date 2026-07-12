# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-12T07:46:01.922567+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **53.8/100**
- Development-regression score: **0.0/100**
- Blinded holdout score: **53.8/100**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **blinded_evaluation_failed**
- Premium calls: **0**
- Average wall time: **212.4s/case**
- Maximum bounded repair rounds: **3**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| py-config-explicit-values | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 45 | runtime | settings/cli.py | true | true | false | false |
| py-matrix-result-attribution | python | pytest | blinded_holdout | holdout-sealed-python-multifile | 80 | runtime | harness/context.py, harness/runner.py | true | true | true | true |
| ts-workspace-state-ownership | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 65 | state | src/session.ts | true | true | false | false |
| ts-utf8-stream-boundaries | typescript | node_test | blinded_holdout | holdout-sealed-typescript-multifile | 60 | code | src/messageReader.ts, src/utf8Lines.ts | true | true | false | false |
| dart-profile-patch-null | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 30 | code | - | false | true | false | false |
| dart-equal-time-event-order | dart | dart | blinded_holdout | holdout-sealed-dart-multifile | 45 | state | lib/event_cursor.dart | true | true | false | false |
| sql-retained-payment-history | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 60 | config | sql/history_report.sql, sql/schema.sql | true | true | false | false |
| sql-fixed-width-identifiers | sql | pytest | blinded_holdout | holdout-sealed-sql-multifile | 45 | data | sql/schema.sql | true | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
