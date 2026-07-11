# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-11T13:23:17.195528+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **93.3/100**
- Holdout score: **93.3/100**
- Multi-file holdout score: **93.3/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Average wall time: **119.7s/case**
- Maximum bounded repair rounds: **3**
- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.

| Case | Language | Runner | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |
|---|---|---|---|---:|---|---|---:|---:|---:|
| ts-singleflight-401 | typescript | node_test | holdout-multifile-typescript | 80 | dependency | src/inflight.ts, src/user_service.ts | true | true | true |
| ts-abort-chain-402 | typescript | node_test | holdout-multifile-typescript | 100 | dependency | src/provider.ts, src/retry.ts | true | true | true |
| dart-cache-clock-403 | dart | dart | holdout-multifile-dart | 100 | clock | lib/cache.dart, lib/cache_entry.dart | true | true | true |

## Interpretation

Each repository is created from a held-out case. The model sees only the prompt, candidate source, and public tests. Oracle labels and hidden tests are loaded after the initial patch has been generated. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
