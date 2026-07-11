# Fable 5-Class Diagnostic Second-Run Repair Receipt

Date: 2026-07-11

## Evidence Classification

- Untouched second holdout: **87.5/100**, `needs_improvement`.
- Frozen baseline: `e1bc5538c1cfac65ec992bed6be1d296b603acc4`.
- Untouched local calls: **24/24 transport-successful**, but **0/24 usable packets**; every response reached the 900-token cap.
- Untouched failures: four root-cause dimension checks.
- Untouched premium calls: **0**; safety violations: **0**.
- The frozen result remains valid negative evidence. Every rerun below is a disclosed development replay, not unseen capability evidence.

## Generic Repairs

1. Dimension terms now use lexical boundaries, so `release` no longer implies `lease` state and `timeout` no longer implies clock time.
2. Operational vocabulary covers manifests and shards, cursors, source hunks, configuration-only value changes, MTU/underlay failures, and deployment ownership.
3. Evidence polarity separates causal support, ruled-out controls, and context. Strong independent A/B support can outweigh weaker same-family controls.
4. Hypotheses without explicit evidence links are no longer silently supported by every record in the same family.
5. Dense local prompts use compact JSON, short fields, and empty experiment lists unless a typed probe is essential.
6. Common small-model contract slips are repaired fail-closed: mismatched support is dropped only when prior grounded support exists, missing falsification fields are restored from the prior packet, and malformed automatic experiments are demoted to non-executable plans. Every repair is recorded.
7. The benchmark promotion gate now requires at least one accepted local packet per model-backed case; successful transport alone cannot qualify.
8. Fresh typed-probe evidence is retained when a case reaches the 40-observation cap.
9. Deployment/infrastructure and concurrency/lifecycle aliases map to runtime and state while preserving the stable scoring taxonomy.

## Development Verification

### Full Local Council

- Result: **99.38/100**, `shadow_ready` on the disclosed development replay.
- Model: `qwen2.5-coder:7b` through local Ollama.
- Calls: **24/24 successful**.
- Accepted packets: **21/24**; every one of the eight cases had at least one accepted packet.
- Model-output promotion gate: **pass**.
- Average call latency: **15,749.42 ms**; maximum: **23,954 ms**.
- Premium calls: **0**; safety violations: **0**.
- Remaining weighted miss: one hypothesis-breadth check on `bh2-203`.
- Artifacts: `FABLE5_CLASS_DIAGNOSTIC_SECOND_RUN_DEVELOPMENT_REPAIR.md` and `fable5_class_diagnostic_second_run_development_repair.json`.

### Final Breadth Repair

- Heuristic replay: **100/100** across all eight disclosed cases.
- Targeted local replay of `bh2-203`: **100/100**.
- Targeted calls: **3/3 successful**, **2/3 accepted**, promotion gate **pass**.
- Targeted hypothesis families: code, data, dependency, and state.
- Average targeted call latency: **13,108 ms**; maximum: **14,641 ms**.
- Broad routing, evidence, repair, identity, and autonomy regression suite: **297 passed**.
- Artifacts: `FABLE5_CLASS_DIAGNOSTIC_SECOND_RUN_BREADTH_TARGETED_REPAIR.md` and `fable5_class_diagnostic_second_run_breadth_targeted_repair.json`.

## Honest Verdict

The disclosed failures are repaired and the local council is now materially faster and structurally usable. This does not rewrite the untouched 87.5/100 score and does not prove Fable 5 parity. A third independently authored post-repair holdout is required to measure generalization.
