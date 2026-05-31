# NEXT_TASK: f-phase5o-position-plan-generator-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/position_plan_generator.py`, the final Phase 5O
adapter candidate after `paper_trading.py` was removed as a false-positive
legacy `Trade` source hit.

## Why This Is Next

The remaining adapter-candidate pool is down to one file:

```text
app/services/trading/position_plan_generator.py
```

It is currently classified as private helper/type-only, but plan-generation
helpers often sit close to stop/target sizing and operator-facing trade plans.
Close it with source proof if harmless, or add parity evidence and reclassify it
if it feeds behavior-bearing live plan decisions.

Current surface after the paper-trading closeout:

```text
orm_trade_symbol_compat = 64
learning_research_reporting = 5
live_action_broker_reconcile = 21
private_helper_type_only = 3
risk_capital_gate = 21
adapter_candidate = 1
future_rename_blocker = 47
unexpected runtime readers = 0
unexpected runtime mutations = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `position_plan_generator.py`.
- Determine whether the references are false-positive/type-only, passive plan
  formatting, or live/risk-relevant plan behavior.
- If false-positive or type-only, remove it from the compatibility inventory
  with a source-preservation test.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live broker/order/stop/close/reconcile/PDT/capital behavior change.
- No public `/trades`, `trade_id`, schema, UI label, or plan payload rename.
- Do not touch the dirty root checkout.
- Respect `project_ws` coordination reports; while PM/control-plane governance
  remains frozen, push evidence branches only and do not force a merge/deploy.
- Source posture is currently ALERT because shared app services are mounted
  from dirty root `D:\dev\chili-home-copilot`. Do not restart Postgres, refresh
  runtime, clean the dirty root, or mutate DB/live state as part of this slice.

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer reports no unexpected runtime readers/mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
