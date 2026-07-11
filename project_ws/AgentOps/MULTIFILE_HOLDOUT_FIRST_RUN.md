# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-11T10:48:15.666715+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **83.3/100**
- Holdout score: **83.3/100**
- Multi-file holdout score: **83.3/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Average wall time: **104.5s/case**
- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.

| Case | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |
|---|---|---:|---|---|---:|---:|---:|
| config-chain-302 | holdout-multifile | 100 | config | flags.py, worker_mode.py | true | true | true |
| data-contract-303 | holdout-multifile | 70 | data | quote_source.py, replay_mirror.py | true | false | false |
| state-dedupe-304 | holdout-multifile | 80 | runtime | dedupe_registry.py, worker.py | true | true | true |

## Interpretation

Each repository is created from a held-out case. The model sees only the prompt, candidate source, and public tests. Oracle labels and hidden tests are loaded after the initial patch has been generated. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
