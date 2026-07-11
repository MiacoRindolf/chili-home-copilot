# Real-World Diagnostic Reasoning Benchmark

- Run: 2026-07-11T20:12:41.720880+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **76.2/100**
- Holdout score: **76.2/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Usable local model stages: **22/24**
- Model-output promotion gate: **pass**
- Average local model latency: **21.3s/case**
- Maximum local model latency: **27.9s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bh3-301 | holdout | 75 | dependency | patch_root_cause | confirmed | 3/3 |
| bh3-302 | holdout | 100 | data | patch_root_cause | confirmed | 3/3 |
| bh3-303 | holdout | 100 | clock | patch_root_cause | confirmed | 2/3 |
| bh3-304 | holdout | 100 | state | patch_root_cause | confirmed | 3/3 |
| bh3-305 | holdout | 75 | data | patch_root_cause | confirmed | 2/3 |
| bh3-306 | holdout | 45 | data | instrument_first | provisional | 3/3 |
| bh3-307 | holdout | 70 | runtime | patch_root_cause | confirmed | 3/3 |
| bh3-308 | holdout | 45 | runtime | patch_root_cause | confirmed | 3/3 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, premium calls remain zero, and every model-backed case has at least one accepted local packet. Transport success without usable JSON is not model reasoning. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
