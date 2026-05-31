# NEXT_TASK: f-position-identity-phase-5aj-trades-api-tie-order-hardening

STATUS: QUEUED

## Goal

Remove the last Phase 5AH `/api/trading/trades` mixed/all-route caveat by
making equal-`entry_date` ordering deterministic across the legacy Trade ORM
path and the management-envelope runtime-object path.

## Current State

Phase 5AI turned on the route flag for the web container:

```text
CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true
```

Live route trial is healthy:

```text
/api/trading/trades?status=open   ok=True rows=5 suppressed=0
/api/trading/trades?status=closed ok=True rows=50 suppressed=0
```

Post-trial probes:

```text
Phase 5AH cutover probe: COMPLETE_POSITIVE
  open exact_match=true
  closed exact_match=true
  all accepted=true, tie_order_only=true

Phase 5AG: COMPLETE_POSITIVE
Phase 5K:  COMPLETE_POSITIVE
Phase 5I:  COMPLETE_POSITIVE
```

The only remaining `/trades` parity softness is the mixed/all response order
when two rows share the exact same `entry_date`. Row contents and row sets
match.

## Recommended Work Shape

1. Audit the current `ts.get_trades(...)` ordering and the
   `load_trades_api_envelope_objects(...)` / row helper ordering.
2. Add an explicit, shared secondary tie-breaker for equal `entry_date` rows.
   Prefer a stable `id` tie-breaker unless live evidence shows another
   existing semantic order is expected.
3. Update the Phase 5AH probe so mixed/all requires exact parity, not
   `tie_order_only=true`.
4. Re-run:
   - focused route/helper tests
   - `scripts/d-phase5ah-trades-api-cutover-probe.py`
   - `scripts/d-phase5ag-trades-open-runtime-adapter-probe.py`
   - `scripts/d-phase5ae-trades-api-parity-probe.py`
   - `scripts/d-phase5k-live-path-parity-probe.py`
   - `scripts/d-phase5i-post-rename-soak-probe.py`
5. Recreate only the `chili` web container from the clean merged worktree and
   re-test the live HTTP routes.

## Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not rename `Trade`, `/trades`, `trade_id`, schema classes, UI labels, or
  public response field names.
- Do not touch sell/close, monitor-run, stop execution, broker/order/reconcile,
  PDT, cash, capital, portfolio, or promotion gates.
- Do not restart Postgres.
- Do not pull into or overwrite the dirty live root.
