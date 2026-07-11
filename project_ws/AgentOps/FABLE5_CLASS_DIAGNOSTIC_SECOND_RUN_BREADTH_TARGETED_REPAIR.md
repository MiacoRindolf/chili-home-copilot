# Real-World Diagnostic Reasoning Benchmark

> Evidence classification: **targeted development repair after disclosure**, not an unseen holdout. This one-case rerun closes the final breadth regression from the 99.38/100 full development replay.

- Run: 2026-07-11T18:43:15.243296+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Holdout score: **100.0/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Usable local model stages: **2/3**
- Model-output promotion gate: **pass**
- Average local model latency: **13.1s/case**
- Maximum local model latency: **14.6s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bh2-203 | holdout | 100 | data | patch_root_cause | confirmed | 2/3 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, and premium calls remain zero. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
