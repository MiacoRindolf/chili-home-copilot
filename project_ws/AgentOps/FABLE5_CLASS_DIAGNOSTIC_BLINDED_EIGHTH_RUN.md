# Real-World Diagnostic Reasoning Benchmark

- Run: 2026-07-12T06:22:56.234794+00:00
- Local model: `qwen2.5-coder:7b`
- Model thinking: **model-default**
- Reference family: `claude-fable-5`
- Overall score: **83.1/100**
- Holdout score: **83.1/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Usable local model stages: **24/24**
- Model-output promotion gate: **pass**
- Average local model latency: **33.5s/call**
- Maximum local model latency: **46.5s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bh8-801 | holdout | 65 | test_harness | patch_root_cause | confirmed | 3/3 |
| bh8-802 | holdout | 100 | code | patch_root_cause | confirmed | 3/3 |
| bh8-803 | holdout | 90 | dependency | patch_root_cause | confirmed | 3/3 |
| bh8-804 | holdout | 100 | dependency | patch_root_cause | confirmed | 3/3 |
| bh8-805 | holdout | 70 | state | instrument_first | provisional | 3/3 |
| bh8-806 | holdout | 60 | runtime | instrument_first | provisional | 3/3 |
| bh8-807 | holdout | 80 | code | instrument_first | inconclusive | 3/3 |
| bh8-808 | holdout | 100 | code | instrument_first | inconclusive | 3/3 |

## Interpretation

The manifest declares this as a blinded benchmark slice. Oracle files are loaded only after local reasoning finishes; the score validates this frozen slice and does not become unseen evidence again after its cases inform development.
A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, premium calls remain zero, and every model-backed case has at least one accepted local packet. Transport success without usable JSON is not model reasoning. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
