# NEXT_TASK: f-phase5o-autotrader-desk-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/autotrader_desk.py`, one of the final Phase 5O
adapter candidates after `live_drift.py` was reclassified as a live-drift
auto-challenge future rename blocker.

## Why This Is Next

The remaining adapter-candidate pool is down to three files. `autotrader_desk.py`
is classified as private helper/type-only, but it likely backs operator-visible
desk state. It should receive source-proof or parity-proof before removal or
reclassification.

Current surface after the `live_drift.py` audit:

```text
orm_trade_symbol_compat = 65
learning_research_reporting = 6
live_action_broker_reconcile = 20
private_helper_type_only = 4
risk_capital_gate = 21
adapter_candidate = 3
future_rename_blocker = 46
unexpected runtime readers = 0
unexpected runtime mutations = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in `autotrader_desk.py`.
- Determine whether the references are private type hints/source wording,
  passive desk reporting, or behavior-bearing live-state reads.
- If false-positive or type-only, remove it from the compatibility inventory
  with a source-preservation test.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live alert cadence, order, stop, close, broker, reconcile,
  risk/capital/PDT, lifecycle, or portfolio behavior change without parity
  evidence.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.
- Respect `project_ws` coordination reports; while PM/control-plane governance
  remains frozen, push evidence branches only and do not force a merge/deploy.
- Source posture is currently ALERT because shared app services are mounted
  from dirty root `D:\dev\chili-home-copilot`. Do not restart Postgres or clean
  the dirty root as part of this slice.

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer reports no unexpected runtime readers/mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
