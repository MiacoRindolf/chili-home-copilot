# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-11T13:27:40.767879+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **65.0/100**
- Holdout score: **65.0/100**
- Multi-file holdout score: **65.0/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Average wall time: **147.1s/case**
- Maximum bounded repair rounds: **3**
- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.

| Case | Language | Runner | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |
|---|---|---|---|---:|---|---|---:|---:|---:|
| ts-singleflight-401 | typescript | node_test | holdout-multifile-typescript | 65 | state | src/user_service.ts | true | true | false |

## Interpretation

Each repository is created from a held-out case. The model sees only the prompt, candidate source, and public tests. Oracle labels and hidden tests are loaded after the initial patch has been generated. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
