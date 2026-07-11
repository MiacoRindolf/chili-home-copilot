# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-11T14:53:17.764188+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **91.7/100**
- Development-regression score: **91.7/100**
- Blinded holdout score: **not run**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **development_regression_failed**
- Premium calls: **0**
- Average wall time: **97.1s/case**
- Maximum bounded repair rounds: **3**
- Fable 5 parity claim: **No**. These are development regressions, not a blinded frontier head-to-head.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |
|---|---|---|---|---|---:|---|---|---:|---:|---:|
| ts-singleflight-401 | typescript | node_test | development_regression | holdout-multifile-typescript | 100 | state | src/inflight.ts, src/user_service.ts | true | true | true |
| ts-abort-chain-402 | typescript | node_test | development_regression | holdout-multifile-typescript | 100 | dependency | src/provider.ts, src/retry.ts | true | true | true |
| dart-cache-clock-403 | dart | dart | development_regression | holdout-multifile-dart | 100 | clock | lib/cache.dart, lib/cache_entry.dart | true | true | true |
| dart-subscription-404 | dart | dart | development_regression | holdout-multifile-dart | 100 | state | lib/subscription.dart, lib/worker.dart | true | true | true |
| sql-partial-unique-405 | sql | pytest | development_regression | holdout-sql | 100 | data | schema.sql | true | true | true |
| sql-join-aggregate-406 | sql | pytest | development_regression | holdout-sql | 50 | data | - | false | true | false |

## Interpretation

Each repository is created from a development-regression case. The live model sees only the prompt, candidate source, and public tests; oracle labels and hidden tests are loaded after the initial patch. However, these fixtures have informed system development, so they do not measure unseen generalization. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
