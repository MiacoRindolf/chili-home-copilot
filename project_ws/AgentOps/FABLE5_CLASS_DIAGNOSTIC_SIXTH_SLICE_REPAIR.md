# Fable 5-Class Diagnostic Sixth-Slice Repair

Date: 2026-07-11

## Evidence Classification

- Repair implementation: `43f6d3e60725370eacfea968046325b1550bbca9`
- Frozen pre-repair implementation: `aa2821db9c67444bb6d3ce5cc63c71bdbfe1756c`
- Fixture commit: `90e88dbd1eeca7612ba9e9db2949e19d5a18bae8`
- Reference family: `claude-fable-5`
- Local model: `qwen2.5-coder:7b`
- Disclosed development fixture: `tests/fixtures/project_autonomy_diagnostics_blinded6_20260712`
- Premium calls: 0

The authoritative untouched sixth-run result remains **67.5/100** at implementation
`aa2821db9c67444bb6d3ce5cc63c71bdbfe1756c`. The cases were disclosed before this repair and are now
development regressions. Neither replay below rewrites the untouched score or proves Fable 5 parity.

## Generic Repairs

- Evidence-family inference now recognizes bounded operational fingerprints for duplicated or selected data
  rows, effective environment and settings entries, shared-library/package compatibility, and matcher or release
  artifact behavior. These are generic mechanism families rather than fixture IDs or oracle answers.
- Retained before/after metadata can establish baseline drift without requiring the author to use one exact
  vocabulary for the comparison.
- Event-level comparability gaps are distinct from baseline-only uncertainty. A coarse worker recycle or fresh
  worker control may localize a state/runtime family, but cannot by itself identify the owning mechanism.
- Mechanism-attribution gaps block sparse experiments that merely repeat the same broad reset. Attribution is
  resolving only when evidence names a typed probe, isolates a changed factor, or links the causal graph.
- Under unresolved drift, an untested harness explanation remains eligible for an inconclusive
  `instrument_first` decision instead of being over-promoted to a confirmed patch.
- The benchmark contract now freezes and validates this sixth fixture family without exposing oracle content to
  the reasoning stages.

No case ID, oracle value, expected score, premium-model output, trading rule, broker behavior, deployment action,
or production mutation path is encoded in the repair.

## Validation

- Focused diagnostic reasoning and benchmark-contract tests: **76 passed**.
- Broad reasoning, typed probes, runtime evidence, diagnostic memory, Project Autonomy service/API routes, and
  benchmark contracts: **226 passed**. Only existing warnings remained.
- Disclosed heuristic-only sixth-slice replay: **100/100**, all eight cases exact.
- Disclosed full local-council sixth-slice replay: **92.5/100**, verdict `needs_improvement`.
- Local calls: **24/24 successful** and **24/24 accepted**; all three stages were usable in every case.
- Final ownership: five conclusions remained model-selected and three were selected by stronger deterministic
  evidence-gate hypotheses.
- Average local-call latency: **52.2 seconds**; maximum: **61.1 seconds**.
- Full-run wall time: **1,258.6 seconds**.
- Unsafe final automatic experiments: **0**.
- Premium calls: **0**.
- The per-case checkpoint was removed only after both final artifacts were written. The branch remained clean at
  the repair commit, and the three implementation/test sources had zero post-run diff.

| Case | Effective family | Decision/status | Final owner | Score |
|---|---|---|---|---:|
| `bh6-601` | data | patch / confirmed | local council | 100 |
| `bh6-602` | clock | patch / confirmed | deterministic evidence gate | 100 |
| `bh6-603` | state | patch / confirmed | deterministic evidence gate | 100 |
| `bh6-604` | code | patch / confirmed | local council | 65 |
| `bh6-605` | dependency | patch / confirmed | deterministic evidence gate | 100 |
| `bh6-606` | code | patch / confirmed | local council | 100 |
| `bh6-607` | state | instrument / provisional | local council | 75 |
| `bh6-608` | test_harness | instrument / inconclusive | local council | 100 |

The two remaining misses are causal-boundary errors. Case `bh6-604` expects `config`, but the council selected a
downstream code explanation. Case `bh6-607` expects `runtime`, but the council selected the adjacent `state`
family while preserving the correct provisional instrumentation posture. The run therefore stays below the
development shadow threshold despite closing all decision/status and safety errors.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_SIXTH_REPAIR_RUN.md` and
`fable5_class_diagnostic_sixth_repair_run.json`. Their SHA-256 hashes are
`d3974f6e7b58a6f8ab466fdb8658789db8bb1c17bc2576646d14ea88039e2e7f` and
`c34f896fa651728b00a1ce8852358f1519f7b331be6c8a123d545d28316acc34`.

## Claim Boundary

This proves that the generic repair closed most disclosed sixth-slice failure modes without premium calls while
retaining conservative safety behavior. It does not establish unseen generalization, direct Fable 5
non-inferiority, or broad coding superiority. The next authoritative evidence must come from a newly authored,
post-repair untouched slice. Diagnosis-to-fix evaluation must then expand to multi-file, mixed-language,
multi-repository tasks with blind human adjudication against authenticated Fable 5 outputs.
