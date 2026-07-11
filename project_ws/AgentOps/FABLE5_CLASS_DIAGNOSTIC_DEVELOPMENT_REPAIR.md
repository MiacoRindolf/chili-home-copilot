# Real-World Diagnostic Reasoning Benchmark

- Run: 2026-07-11T17:03:47.969534+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Holdout score: **100.0/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Average local model latency: **48.4s/case**
- Maximum local model latency: **55.3s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bhfr-20260711-01 | holdout | 100 | code | patch_root_cause | confirmed | 2/3 |
| bhfr-20260711-02 | holdout | 100 | data | patch_root_cause | confirmed | 0/3 |
| bhfr-20260711-03 | holdout | 100 | clock | patch_root_cause | confirmed | 2/3 |
| bhfr-20260711-04 | holdout | 100 | state | patch_root_cause | confirmed | 2/3 |
| bhfr-20260711-05 | holdout | 100 | config | patch_root_cause | confirmed | 1/3 |
| bhfr-20260711-06 | holdout | 100 | dependency | patch_root_cause | confirmed | 2/3 |
| bhfr-20260711-07 | holdout | 100 | runtime | patch_root_cause | confirmed | 2/3 |
| bhfr-20260711-08 | holdout | 100 | test_harness | patch_root_cause | confirmed | 2/3 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, and premium calls remain zero. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
