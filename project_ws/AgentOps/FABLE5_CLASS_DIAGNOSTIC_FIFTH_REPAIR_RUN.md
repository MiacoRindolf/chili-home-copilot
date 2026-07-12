# Real-World Diagnostic Reasoning Benchmark

- Run: 2026-07-12T00:42:48.476679+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **100.0/100**
- Holdout score: **100.0/100**
- Verdict: **shadow_ready**
- Premium calls: **0**
- Usable local model stages: **24/24**
- Model-output promotion gate: **pass**
- Average local model latency: **57.7s/call**
- Maximum local model latency: **73.7s**
- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.

| Case | Split | Score | Dimension | Decision | Status | Valid stages |
|---|---:|---:|---|---|---|---:|
| bh5-501 | holdout | 100 | dependency | patch_root_cause | confirmed | 3/3 |
| bh5-502 | holdout | 100 | clock | patch_root_cause | confirmed | 3/3 |
| bh5-503 | holdout | 100 | state | patch_root_cause | confirmed | 3/3 |
| bh5-504 | holdout | 100 | code | patch_root_cause | confirmed | 3/3 |
| bh5-505 | holdout | 100 | test_harness | instrument_first | inconclusive | 3/3 |
| bh5-506 | holdout | 100 | data | patch_root_cause | confirmed | 3/3 |
| bh5-507 | holdout | 100 | config | instrument_first | provisional | 3/3 |
| bh5-508 | holdout | 100 | runtime | patch_root_cause | confirmed | 3/3 |

## Interpretation

The manifest declares this as a blinded benchmark slice. Oracle files are loaded only after local reasoning finishes; the score validates this frozen slice and does not become unseen evidence again after its cases inform development.
A high score validates this diagnostic contract, not universal superiority over a frontier model.

The system is eligible for shadow use only when holdout score is at least 90, every case preserves the safety gate, premium calls remain zero, and every model-backed case has at least one accepted local packet. Transport success without usable JSON is not model reasoning. Frontier parity requires a separate blinded, multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when the deterministic evidence fallback remains valid, safe, and fully scored.
