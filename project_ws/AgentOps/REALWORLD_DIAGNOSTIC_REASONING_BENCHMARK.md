# Real-World Diagnostic Reasoning Benchmark

- Run: 2026-07-11T09:24:21.519895+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **97.9/100**
- Holdout score: **98.8/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Average local model latency: **26.8s/case**
- Maximum local model latency: **37.4s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| cal-001 | calibration | 95 | clock | patch_root_cause | confirmed | 1/1 |
| cal-002 | calibration | 95 | data | patch_root_cause | confirmed | 1/1 |
| cal-003 | calibration | 100 | data | patch_root_cause | confirmed | 0/1 |
| hold-101 | holdout | 100 | runtime | patch_root_cause | confirmed | 1/1 |
| hold-102 | holdout | 100 | config | patch_root_cause | confirmed | 1/1 |
| hold-103 | holdout | 95 | state | patch_root_cause | confirmed | 1/1 |
| hold-104 | holdout | 100 | code | instrument_first | inconclusive | 1/1 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, and premium calls remain zero. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
