# Real-World Diagnostic Reasoning Benchmark

- Run: 2026-07-11T16:44:12.971467+00:00
- Local model: `heuristic-only`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Holdout score: **100.0/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Average local model latency: **0.0s/case**
- Maximum local model latency: **0.0s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bhfr-20260711-01 | holdout | 100 | code | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-02 | holdout | 100 | data | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-03 | holdout | 100 | clock | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-04 | holdout | 100 | state | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-05 | holdout | 100 | config | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-06 | holdout | 100 | dependency | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-07 | holdout | 100 | runtime | patch_root_cause | confirmed | 0/1 |
| bhfr-20260711-08 | holdout | 100 | test_harness | patch_root_cause | confirmed | 0/1 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, and premium calls remain zero. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
