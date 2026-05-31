# NEXT_TASK: f-phase5ab-c-pattern-monitor-runtime-object-probe

STATUS: QUEUED

## Goal

Build a read-only runtime-object parity probe for
`trading_scheduler.trigger_pattern_monitor_for_tickers(...)` before converting
its remaining `Trade` ORM object handoff.

## Why This Is Next

Phase 5AB-B converted the scheduler's proven user/ticker/count selection
queries to management-envelope helpers. The only intentional scheduler
`Trade` ORM surface left is the event-driven pattern monitor handoff:

`trigger_pattern_monitor_for_tickers(...) -> run_pattern_position_monitor_for_trades(...)`

That path passes actual SQLAlchemy `Trade` objects into live pattern-position
monitor logic. The Phase 5AB scope probe proves selected ids match, but it
does not prove that envelope-shaped runtime objects are behaviorally equivalent
inside the downstream monitor.

## Scope

- Add a read-only probe that loads the current `Trade` ORM objects and
  candidate runtime objects from `trading_management_envelopes` for the same
  ticker set.
- Compare object-visible fields used by `pattern_position_monitor`.
- Include broker-stale suppression and position-identity behavior only if the
  downstream monitor observes those fields directly.
- Do not execute monitor side effects, emit alerts, close positions, place
  orders, or write monitor decisions.

## Guardrails

- No scheduler cadence changes.
- No stop evaluation or dispatch behavior changes.
- No broker/order/close/reconcile changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No conversion of `trigger_pattern_monitor_for_tickers(...)` in this slice
  unless the probe proves runtime-object parity first.

## Exit Criteria

- New probe emits `COMPLETE_POSITIVE` against live data.
- Focused tests pin the probe and make sure it is read-only/live-opt-in.
- Phase 5AB scheduler scope probe remains `COMPLETE_POSITIVE`.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
