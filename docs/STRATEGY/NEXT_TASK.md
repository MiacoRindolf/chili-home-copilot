# NEXT_TASK: f-phase5o-paper-trading-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/paper_trading.py`, one of the final two Phase 5O
adapter candidates after `autotrader_desk.py` was reclassified as an
operator-visible live desk future rename blocker.

## Why This Is Next

The remaining adapter-candidate pool is down to:

```text
app/services/trading/paper_trading.py
app/services/trading/position_plan_generator.py
```

`paper_trading.py` is not live broker execution, but it is not harmless either:
paper outcomes feed learning, drift comparisons, UI/reporting evidence, and
promotion/validation interpretation. Treat it as a potential learning evidence
surface until source and parity proof say otherwise.

Current surface after the AutoTrader desk audit:

```text
orm_trade_symbol_compat = 65
learning_research_reporting = 6
live_action_broker_reconcile = 21
private_helper_type_only = 3
risk_capital_gate = 21
adapter_candidate = 2
future_rename_blocker = 47
unexpected runtime readers = 0
unexpected runtime mutations = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in `paper_trading.py`.
- Determine whether the references are false-positive/type-only, passive
  paper-reporting reads, or learning-evidence inputs.
- If false-positive or type-only, remove it from the compatibility inventory
  with a source-preservation test.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live broker/order/stop/close/reconcile/PDT/capital behavior change.
- No public `/trades`, `trade_id`, schema, UI label, or paper-trade payload
  rename.
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
