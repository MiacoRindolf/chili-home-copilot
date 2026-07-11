# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-11T10:04:58.070146+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Average wall time: **56.2s/case**
- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.

| Case | Score | Diagnosis | Selected file | Patch | Public | Hidden |
|---|---:|---|---|---:|---:|---:|
| clock-201 | 100 | clock | session_gate.py | true | true | true |
| config-202 | 100 | config | feature_gate.py | true | true | true |
| data-203 | 100 | data | replay_copy.py | true | true | true |

## Interpretation

Each repository is created from a held-out case. The model sees only the prompt, candidate source, and public tests. Oracle labels and hidden tests are loaded after the patch has been generated. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication.
