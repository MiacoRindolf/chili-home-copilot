# NEXT_TASK: f-phase5p-learning-reporting-lifecycle-decay-adapter-plan

STATUS: QUEUED

## Goal

Start the next post-Phase-5O lane with a narrow learning/lifecycle conversion
plan, not a broad rename. The target family is the smallest remaining
behavior-bearing group:

```text
learning_research_reporting = 5
```

## Why This Is Next

Phase 5O closed with `adapter_candidate = 0`. The remaining 48 surfaces are
explicit future rename blockers:

```text
learning_research_reporting = 5
live_action_broker_reconcile = 21
risk_capital_gate = 22
```

The live broker/reconcile and risk/capital groups should stay deferred while
runtime-source posture is still `ALERT`. The learning/lifecycle group is the
smallest next family and can be approached with helper/default-off evidence
without touching broker, order, close, reconcile, PDT, or capital paths.

## Candidate Files

```text
app/services/trading/alpha_decay.py
app/services/trading/brain_neural_mesh/plasticity.py
app/services/trading/cron_jobs/stale_promoted_sweep.py
app/services/trading/learning.py
app/services/trading/live_drift.py
```

## Recommended First Slice

Plan before converting. Choose either:

1. A lifecycle-decay helper plan for `alpha_decay.py` +
   `cron_jobs/stale_promoted_sweep.py`; or
2. A realized-evidence helper plan for the relevant read-only portions of
   `learning.py`.

Do not change demotion thresholds, promotion rules, lifecycle transitions, mesh
weights, model/capital settings, or live worker flags in this slice. If any
conversion is attempted, it must include old/new parity evidence first.

## Guardrails

- No shared-root source edits.
- No runtime refresh, Docker/Compose action, migration, DB mutation, broker/API
  call, model/capital/breaker change, or live-trading behavior change.
- No public `/trades`, `trade_id`, schema, UI label, route, or payload rename.
- Do not touch live broker/reconcile/risk/capital surfaces from this lane.
- Respect `project_ws` coordination reports; current PM/AgentOps governance
  keeps release/runtime/source-dispatch fail-closed.
- Source posture remains expected `ALERT` because shared services are mounted
  from dirty root `D:\dev\chili-home-copilot`. Document; do not fix from this
  lane.

## Exit Criteria

- A narrow Phase 5P report names the first implementation target and proves why
  it is safer than broker/reconcile/risk/capital surfaces.
- If code changes are included, focused tests and old/new parity evidence pass.
- Analyzer remains clean with no unexpected runtime readers/mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE` if probes are run.
