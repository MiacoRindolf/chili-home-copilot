# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-11T13:09:57.252152+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **73.3/100**
- Holdout score: **73.3/100**
- Multi-file holdout score: **70.0/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Average wall time: **242.1s/case**
- Maximum bounded repair rounds: **5**
- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.

| Case | Language | Runner | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |
|---|---|---|---|---:|---|---|---:|---:|---:|
| ts-singleflight-401 | typescript | node_test | holdout-multifile-typescript | 60 | dependency | src/inflight.ts, src/user_service.ts | true | true | false |
| ts-abort-chain-402 | typescript | node_test | holdout-multifile-typescript | 70 | dependency | src/provider.ts, src/retry.ts | true | false | false |
| dart-cache-clock-403 | dart | dart | holdout-multifile-dart | 70 | clock | lib/cache.dart, lib/cache_entry.dart | true | false | false |
| dart-subscription-404 | dart | dart | holdout-multifile-dart | 80 | code | lib/subscription.dart, lib/worker.dart | true | true | true |
| sql-partial-unique-405 | sql | pytest | holdout-sql | 80 | unknown | schema.sql | true | true | true |
| sql-join-aggregate-406 | sql | pytest | holdout-sql | 80 | unknown | report.sql | true | true | true |

## Interpretation

Each repository is created from a held-out case. The model sees only the prompt, candidate source, and public tests. Oracle labels and hidden tests are loaded after the initial patch has been generated. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
