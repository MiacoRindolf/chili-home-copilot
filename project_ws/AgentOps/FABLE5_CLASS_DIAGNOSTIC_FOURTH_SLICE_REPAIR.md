# Fable 5-Class Diagnostic Fourth-Slice Repair

Date: 2026-07-11

## Evidence Classification

- Repair implementation: `8628ddea503814dced347792aaf1c56c0d67c243`
- Reference family: `claude-fable-5`
- Local model: `qwen2.5-coder:7b`
- Disclosed development fixture: `tests/fixtures/project_autonomy_diagnostics_blinded4_20260711`
- Premium calls: 0

The authoritative untouched fourth-run result remains **69.38/100** at implementation
`b8616f6273480ca892c229d74021a6ef0c3c411f`. The cases were disclosed before this repair and are now
development regressions. No replay in this document rewrites the untouched score or proves Fable 5 parity.

## Generic Repairs

- Stronger isolated evidence can no longer be overwritten by a weaker earliest-timeline or provenance-break
  candidate later in conclusion selection.
- Mechanism vocabulary now covers memory boundaries, orphaned workflow transitions, transitive parser locks,
  cursor paging after filtering, browser-profile/service-worker test state, rendered topic filters, canonical
  identifiers and joins, and offset-aware clock evidence.
- Ambiguous experiments that depend on an assumed interpretation or leave mutually incompatible explanations
  unresolved are context, not confirmatory support.
- One high-reliability decisive attribution gap can force `inconclusive`; two redundant gap statements are no
  longer required.
- Missing or compositionally different baselines are represented as `baseline_comparability_gap` findings even
  when no exact same-input fingerprint pair survived retention.
- Non-code direct evidence under an unreproducible baseline is capped at provisional rather than being forced
  to either confirmation or complete rejection. Code attribution remains blocked without isolation.
- The heuristic fallback always emits the requested minimum number of known competing dimensions; added
  alternatives are untested and cannot win by evidence volume.

No case IDs, oracle values, scores, broker behavior, trading runtime behavior, deployment behavior, or
production mutation path is encoded in the repair.

## Validation

- Focused reasoning and fixture-protocol slice: **64 passed**.
- Broad reasoning, probes, runtime evidence, diagnostic memory, Project Autonomy service/API routes, and
  benchmark contract slice: **208 passed**. Existing SQLAlchemy and `datetime.utcnow` warnings only.
- Disclosed heuristic-only replay: **100/100**, all eight dimensions, decisions, statuses, baseline labels,
  breadth checks, safety checks, and premium-independence checks passed.
- Disclosed full local-council replay: **100/100**.
- Local calls: **24/24 successful**.
- Accepted model stages: **24/24**; every case accepted all three stages.
- Average local-call latency: **21.82 seconds**; maximum: **26.35 seconds**.
- Unsafe automatic experiments: **0**.
- Premium calls: **0**.

| Case | Effective family | Decision/status | Score |
|---|---|---|---:|
| `bh4-401` | runtime | patch / confirmed | 100 |
| `bh4-402` | dependency | patch / confirmed | 100 |
| `bh4-403` | state | patch / confirmed | 100 |
| `bh4-404` | code | patch / confirmed | 100 |
| `bh4-405` | test_harness | instrument / inconclusive | 100 |
| `bh4-406` | data | patch / confirmed | 100 |
| `bh4-407` | config | patch / confirmed | 100 |
| `bh4-408` | clock | instrument / provisional | 100 |

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_FOURTH_REPAIR_RUN.md` and
`fable5_class_diagnostic_fourth_repair_run.json`.

## Claim Boundary

This run proves that the observed fourth-slice failure mechanisms are closed as development regressions. It is
not unseen generalization evidence. A fifth independently authored post-freeze slice is required before any
new quality claim, and authenticated same-task Fable 5 output plus blind human adjudication remains missing.
