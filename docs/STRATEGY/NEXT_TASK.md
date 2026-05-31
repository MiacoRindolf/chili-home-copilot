# NEXT_TASK: f-phase5o-zero-adapter-candidates-closeout

STATUS: QUEUED

## Goal

Close the Phase 5O adapter-candidate audit lane now that
`adapter_candidate = 0`, then sequence the remaining future rename blockers by
behavioral risk before any broad `Trade`/`trade` naming conversion.

## Why This Is Next

The last adapter candidate, `app/services/trading/position_plan_generator.py`,
has been reclassified as a behavior-bearing future rename blocker with live
plan-input parity evidence. That means Phase 5O has achieved its first major
objective: there are no remaining "maybe harmless" adapter candidates in the
runtime compatibility map.

Current surface after the position-plan-generator audit:

```text
orm_trade_symbol_compat = 64
learning_research_reporting = 5
live_action_broker_reconcile = 21
private_helper_type_only = 2
risk_capital_gate = 22
adapter_candidate = 0
future_rename_blocker = 48
unexpected runtime readers = 0
unexpected runtime mutations = 0
```

This is not a green light for a blind rename. It is a green light for a
controlled closeout/sequencing pass: group the 48 behavior-bearing blockers,
name the safest conversion order, and keep each future conversion backed by
focused parity evidence.

## Scope

- Confirm the analyzer, compatibility map, and Phase 5O reports agree on
  `adapter_candidate = 0`.
- Group the 48 future rename blockers by conversion class:
  learning/reporting, live broker/reconcile, risk/capital gates, public schema
  surfaces, and private helper surfaces.
- Identify which blockers can be converted behind adapters first, and which
  must remain deferred until the live runtime source posture is clean.
- Produce a closeout report that says explicitly what Phase 5O did and did not
  authorize.

## Guardrails

- No shared-root source edits.
- No runtime refresh, Docker/Compose action, migration, DB mutation, broker/API
  call, model/capital/breaker change, or live-trading behavior change.
- No public `/trades`, `trade_id`, schema, UI label, or payload rename in this
  closeout slice.
- Respect `project_ws` coordination reports. Current PM/AgentOps governance
  keeps release/runtime/source-dispatch fail-closed; push evidence branches only
  and do not force merge/deploy.
- Source posture remains expected `ALERT` because shared services are mounted
  from dirty root `D:\dev\chili-home-copilot`. Document that posture; do not fix
  it from this lane.

## Exit Criteria

- Phase 5O closeout report committed on a clean evidence branch.
- Compatibility map remains analyzer-clean with no unexpected runtime
  readers/mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
- The next implementation lane is explicit and narrow, not a broad rename.
