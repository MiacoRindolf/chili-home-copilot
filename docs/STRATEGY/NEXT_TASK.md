# NEXT_TASK: f-position-identity-phase-5ah-trades-api-open-cutover-flag-path

STATUS: IN_FLIGHT

## Goal

Expand the existing default-off `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES`
route flag so `/api/trading/trades` can use management-envelope runtime objects
for open/all responses when explicitly enabled.

## Current State

Phase 5AF added the default-off flag but intentionally fell back whenever open
rows were requested or present. Phase 5AG has now proven the open-row runtime
adapter:

```text
Phase 5AG live probe: COMPLETE_POSITIVE
old_trades=5
new_trades=5
old_suppressed=0
new_suppressed=0
matched=true
```

The probe path uses `trading_management_envelopes` runtime objects and runs the
same helper chain as the current route:

- `filter_broker_stale_open_trades(...)`
- `broker_position_display_metrics(...)`
- public `/trades` field rendering

## Recommended Work Shape

1. Add or reuse a helper that loads open management envelopes as read-only
   runtime objects.
2. Refactor the `/api/trading/trades` route serializer so the compatibility
   path and envelope path share the same open-row rendering logic.
3. When `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true`:
   - `status=closed` can keep using the simple envelope row renderer
   - `status=open` should use the proven runtime-object path
   - no status filter should use envelope runtime objects for open rows plus
     envelope row rendering for closed rows, preserving sort/limit semantics
     carefully
4. Keep the flag default off.
5. Run:
   - focused route/helper tests
   - Phase 5AG live probe
   - Phase 5AE parity probe
   - Phase 5K live-path parity probe
   - Phase 5I post-rename soak probe

## Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not rename `Trade`, `/trades`, `trade_id`, schema classes, UI labels, or
  public response field names.
- Do not touch sell/close, monitor-run, stop execution, broker/order/reconcile,
  PDT, cash, capital, portfolio, or promotion gates.
- Leave `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=false` in live runtime unless
  running an explicit short operator-approved route trial.
