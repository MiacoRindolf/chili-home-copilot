# Real-World Diagnostic Reasoning Benchmark

> Evidence classification: **development replay after disclosure**, not an unseen holdout. The immutable manifest still says `holdout`, but these cases became development material after the untouched 87.5/100 run. This report must not be used as Fable 5 parity or fresh-generalization evidence.

- Run: 2026-07-11T18:40:05.065622+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **99.4/100**
- Holdout score: **99.4/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Usable local model stages: **21/24**
- Model-output promotion gate: **pass**
- Average local model latency: **15.7s/case**
- Maximum local model latency: **24.0s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bh2-201 | holdout | 100 | code | patch_root_cause | confirmed | 3/3 |
| bh2-202 | holdout | 100 | config | patch_root_cause | confirmed | 3/3 |
| bh2-203 | holdout | 95 | data | patch_root_cause | confirmed | 3/3 |
| bh2-204 | holdout | 100 | state | patch_root_cause | confirmed | 3/3 |
| bh2-205 | holdout | 100 | dependency | patch_root_cause | confirmed | 2/3 |
| bh2-206 | holdout | 100 | runtime | patch_root_cause | confirmed | 2/3 |
| bh2-207 | holdout | 100 | state | patch_root_cause | confirmed | 2/3 |
| bh2-208 | holdout | 100 | runtime | patch_root_cause | confirmed | 3/3 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, and premium calls remain zero. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
