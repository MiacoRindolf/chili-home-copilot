# Real-World Diagnostic Reasoning Benchmark

> Evidence classification: **development replay after disclosure**, not an unseen holdout. This deterministic rerun verifies the repaired evidence gate; it is not local-model or Fable 5 parity evidence.

- Run: 2026-07-11T18:42:31.399963+00:00
- Local model: `heuristic-only`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Holdout score: **100.0/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Usable local model stages: **0/0**
- Model-output promotion gate: **pass**
- Average local model latency: **0.0s/case**
- Maximum local model latency: **0.0s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bh2-201 | holdout | 100 | code | patch_root_cause | confirmed | 0/3 |
| bh2-202 | holdout | 100 | config | patch_root_cause | confirmed | 0/3 |
| bh2-203 | holdout | 100 | data | patch_root_cause | confirmed | 0/3 |
| bh2-204 | holdout | 100 | state | patch_root_cause | confirmed | 0/3 |
| bh2-205 | holdout | 100 | dependency | patch_root_cause | confirmed | 0/3 |
| bh2-206 | holdout | 100 | runtime | patch_root_cause | confirmed | 0/3 |
| bh2-207 | holdout | 100 | state | patch_root_cause | confirmed | 0/3 |
| bh2-208 | holdout | 100 | runtime | patch_root_cause | confirmed | 0/3 |

## Interpretation

Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. Holdout cases are sealed variants whose oracle labels are loaded only after local reasoning finishes. A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, and premium calls remain zero. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
