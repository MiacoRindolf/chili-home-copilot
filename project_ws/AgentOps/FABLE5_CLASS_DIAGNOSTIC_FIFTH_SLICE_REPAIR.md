# Fable 5-Class Diagnostic Fifth-Slice Repair

Date: 2026-07-11

## Evidence Classification

- Repair implementation: `aa2821db9c67444bb6d3ce5cc63c71bdbfe1756c`
- Reference family: `claude-fable-5`
- Local model: `qwen2.5-coder:7b`
- Disclosed development fixture: `tests/fixtures/project_autonomy_diagnostics_blinded5_20260711`
- Premium calls: 0

The authoritative untouched fifth-run result remains **74.4/100** at implementation
`8628ddea503814dced347792aaf1c56c0d67c243` and fixture commit
`2d75f05a0c9e70538d249f99d07a4ef10dc9fb52`. The cases were disclosed before this repair and are now
development regressions. Neither replay below rewrites the untouched score or proves Fable 5 parity.

## Generic Repairs

- Evidence-family inference now gives additional weight to the variable manipulated by an intervention and
  discounts long held-constant tails. This prevents unchanged code, settings, data, or runtime names from
  outranking the component that actually reproduced or restored the outcome.
- Bounded structured evidence metadata is flattened deterministically into a 700-character local-only context.
  Recognized fingerprints, state comparisons, experiment IDs, and artifact hashes are also lifted into the
  typed evidence record. Normalization remains idempotent.
- Generic mechanism vocabulary now distinguishes dependency components and parser locks, interval predicates,
  identifier collisions, rendered listener configuration, harness instrumentation, offset-aware clock evidence,
  and runtime resource ceilings. The ambiguous bare word `endpoint` no longer implies a network dependency.
- Hypotheses labeled `unknown` recover a known family only when their own claim yields a unique taxonomy match.
- Non-isolating experiments that preserve neither timing nor environment, or allow two mechanisms to fit the
  same trace, remain context rather than confirmatory proof.
- Semantic good/bad, prior/post-maintenance, and prior/new-host comparisons can produce a bounded baseline-drift
  finding even when the author did not provide exact structured fingerprints. Explicitly incomparable retained
  baselines produce `baseline_comparability_gap`.
- The benchmark runner now writes an atomic checkpoint after every completed case. A checkpoint is bound to
  the runner, diagnostic implementation, manifest, public case hashes, model, stages, and inference parameters.
  Compatible resumes skip only completed cases; incompatible resumes fail closed. Final report and JSON writes
  are atomic, and the checkpoint is removed only after both complete.

No case ID, oracle value, expected score, premium model output, trading rule, broker behavior, deployment action,
or production mutation path is encoded in the repair.

## Validation

- Focused diagnostic reasoning and benchmark-contract tests: **71 passed**.
- Broad reasoning, typed probes, runtime evidence, diagnostic memory, Project Autonomy service/API routes, and
  benchmark contracts: **221 passed**. Only existing SQLAlchemy and `datetime.utcnow` warnings remained.
- Checkpoint test interrupted a two-case run after case one, verified the atomic checkpoint, rejected a resume
  after changing the context-window contract, resumed only case two under the original contract, produced final
  artifacts, and removed the checkpoint.
- Disclosed heuristic-only fifth-slice replay: **100/100**, all eight cases exact.
- Disclosed full local-council fifth-slice replay: **100/100**.
- Local calls: **24/24 successful**.
- Accepted model stages: **24/24**; all three stages were accepted in every case.
- Final ownership: five conclusions remained model-selected and three were selected by a stronger deterministic
  evidence-gate hypothesis.
- Average local-call latency: **57.66 seconds**; maximum: **73.74 seconds**.
- Valid full-run wall time: **1,389.2 seconds**.
- Unsafe final automatic experiments: **0**.
- Premium calls: **0**.

| Case | Effective family | Decision/status | Final owner | Score |
|---|---|---|---|---:|
| `bh5-501` | dependency | patch / confirmed | local council | 100 |
| `bh5-502` | clock | patch / confirmed | deterministic evidence gate | 100 |
| `bh5-503` | state | patch / confirmed | deterministic evidence gate | 100 |
| `bh5-504` | code | patch / confirmed | local council | 100 |
| `bh5-505` | test_harness | instrument / inconclusive | local council | 100 |
| `bh5-506` | data | patch / confirmed | local council | 100 |
| `bh5-507` | config | instrument / provisional | local council | 100 |
| `bh5-508` | runtime | patch / confirmed | deterministic evidence gate | 100 |

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_FIFTH_REPAIR_RUN.md` and
`fable5_class_diagnostic_fifth_repair_run.json`. Their SHA-256 hashes are
`c16d90e7b96593285edb62d9251f77306ab1e0f86e44ba594b0f669af8a4cbd2` and
`677109b517581f0aa4c41f5dd92ed7d83e3f542b5de8b0381bd084bf74c05ee2`.

## Claim Boundary

This proves that the observed fifth-slice mechanisms are closed as development regressions and that the local
council can cooperate with deterministic causal controls without premium calls. It does not establish unseen
generalization, direct Fable 5 non-inferiority, or broad coding superiority. A new independently authored
post-repair slice is required, followed by multi-repository coding holdouts and an authenticated same-task Fable 5
comparison with blind human adjudication.
