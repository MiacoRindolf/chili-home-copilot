# Fable 5-Class Diagnostic Causal Calibration Repair

Date: 2026-07-11

## Evidence Classification

- Implementation commit: `3a45c82af1e93dac1731ab67c2635d9138dc3f00`
- Reference family: `claude-fable-5`
- Local model: `qwen2.5-coder:7b`
- Premium calls: 0
- Source fixtures: disclosed third-run cases under `tests/fixtures/project_autonomy_diagnostics_blinded3_20260711`
- Evidence class: development regression only

The untouched third-run result remains **76.25/100** at frozen source
`851f14119f17703f4c6f7f07430b023c612f4036`. This repair does not relabel that result,
does not make the disclosed cases unseen again, and does not establish Fable 5 parity.

## Generic Repairs

- Evidence polarity now distinguishes causal interventions, healthy controls, negated interventions,
  downstream symptoms, and unresolved attribution gaps.
- Evidence dimensions retain an idempotent `explicit`, `inferred`, or `unknown` origin. Explicit and
  typed-probe ownership remains hard; inferred ownership is advisory and contributes a soft tie-break.
- Timeline reconstruction recognizes producer-consumer edge-state breaks, follows later events with the
  same privacy-preserving correlation fingerprint, and compares source/runtime revisions only inside
  compatible identifier namespaces.
- Hypothesis ranking uses causal ownership rather than symptom volume. Isolated experiments and typed
  probes outrank graph-linked breaks, direct artifacts, and observational context.
- Baseline drift and missing execution attribution cap certainty. Mixed support and counterevidence with
  a missing worker/correlation link becomes `inconclusive`, not a patch or a false rejection.
- A bounded deterministic fallback restores omitted isolation-grade evidence without inventing evidence.
  Returned packets are synchronized to the effective evidence-gated conclusion.
- Stage history records both the model-requested and effective conclusion, including promotion reasons,
  blockers, contract repairs, and retractions.
- Benchmark reports are manifest-aware, label latency per call, and no longer emit legacy calibration
  boilerplate for unrelated blinded protocols.

No broker, trading runtime, deployment, container, or production database behavior changed.

## Validation

- Focused reasoning, report, and typed-probe regression slice: **60 passed**.
- Broad Project Autonomy reasoning, probes, runtime evidence, diagnostic memory, service/API routes, and
  benchmark contract slice: **204 passed**. Existing SQLAlchemy and `datetime.utcnow` warnings only.
- Final disclosed heuristic-only replay: **100/100**, zero premium calls.
- Targeted stochastic regression replay for clock ownership and unresolved runtime attribution: **100/100**,
  **6/6** calls successful and **6/6** stages accepted.

## Exact-Commit Development Replay

The final full replay ran from a clean worktree at the implementation commit above. Source remained
unchanged through the run.

- Overall and structural holdout score: **95.0/100**
- Verdict: **needs_improvement**
- Calls: **24/24 successful**
- Accepted model stages: **24/24**
- Cases with an accepted stage: **8/8**
- Average local-call latency: **23.69 seconds**
- Maximum local-call latency: **30.81 seconds**
- Premium calls: **0**

| Case | Effective family | Decision/status | Score | Notes |
|---|---|---|---:|---|
| `bh3-301` | code | patch / confirmed | 100 | Correct |
| `bh3-302` | data | patch / confirmed | 100 | Correct |
| `bh3-303` | clock | patch / confirmed | 100 | Correct |
| `bh3-304` | state | patch / confirmed | 100 | Correct |
| `bh3-305` | config | patch / confirmed | 95 | Hypothesis breadth miss |
| `bh3-306` | code | patch / confirmed | 65 | Expected dependency; judge also requested two unsafe auto experiments |
| `bh3-307` | runtime | instrument / inconclusive | 100 | Correct uncertainty behavior |
| `bh3-308` | test_harness | instrument / provisional | 100 | Correct baseline-drift behavior |

For `bh3-306`, CHILI demoted both unsafe automatic experiment requests to non-executable isolated plans;
the returned packet contained no unsafe auto experiment. The benchmark still marks the safety check failed
because the local judge attempted the requests. That is intentional evidence against claiming the local
council is fully reliable.

During development, other stochastic runs reached 100/100 and 95.62/100, and targeted closures reached
100/100. The exact-commit 95.0 run above is the authoritative full repair replay. This variance is another
reason a fresh unseen slice and repeated runs remain required.

## Remaining Claim Boundary

This repair shows that CHILI can recover from many small-model classification and confidence errors while
remaining local-only and fail-closed. It does not prove that CHILI is better than Fable 5 on arbitrary complex
diagnosis. The next valid evidence must come from independently authored cases created after this source
freeze, followed by a same-task authenticated Fable 5 comparison and blind human adjudication.
