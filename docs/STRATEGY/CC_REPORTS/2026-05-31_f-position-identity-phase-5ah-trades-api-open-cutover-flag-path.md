# Phase 5AH - Trades API Open Cutover Flag Path

Date: 2026-05-31

## Summary

Phase 5AH expands the existing default-off
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES` route flag so
`/api/trading/trades` can render open and mixed responses from
management-envelope runtime objects when the flag is explicitly enabled.

Default behavior is unchanged. The flag remains default `false`.

## What changed

- Added `load_trades_api_envelope_objects(...)` to load
  `trading_management_envelopes` rows as read-only Trade-like runtime objects.
- Refactored `/api/trading/trades` row rendering into a shared
  `_trade_like_public_response(...)` helper.
- The legacy compatibility path and the envelope runtime-object path now use
  the same broker-truth overlay and stale-open suppression chain:
  - `filter_broker_stale_open_trades(...)`
  - `broker_position_display_metrics(...)`
- When the flag is enabled:
  - `status=closed` keeps the simple envelope row renderer.
  - `status=open` uses runtime objects from `trading_management_envelopes`.
  - no status filter uses runtime objects, preserving row content and accepting
    only tie-order drift among identical `entry_date` timestamps.
- Added `scripts/d-phase5ah-trades-api-cutover-probe.py`, a read-only old-vs-new
  route-path probe for `all`, `open`, and `closed`.

## Live evidence

`scripts/d-phase5ah-trades-api-cutover-probe.py` against live DB:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
status=open: exact_match=true, old_rows=5, new_rows=5, suppressed=0
status=closed: exact_match=true, old_rows=50, new_rows=50, suppressed=0
status=None: accepted=true, exact_match=false, tie_order_only=true
```

The `status=None` difference is not a row-content mismatch. The row set and
each row payload match; only the relative order of rows sharing identical
`entry_date` timestamps differs. The current legacy route does not define a
secondary tie-breaker, so this is acceptable for a default-off flag path but
should be watched during the route trial.

Other gates:

```text
Phase 5AG open runtime adapter probe: COMPLETE_POSITIVE
Phase 5AE /trades base parity probe: COMPLETE_POSITIVE
Phase 5K live-path parity probe: COMPLETE_POSITIVE
Phase 5I post-rename soak probe: COMPLETE_POSITIVE
```

Focused test run:

```text
19 passed
```

Classifier:

```text
0 unexpected runtime readers
0 unexpected runtime mutations
0 unclassified entries
```

## Guardrails preserved

- No public rename.
- No `Trade`, `/trades`, `trade_id`, schema, UI-label, or response-field rename.
- No sell/close/monitor-run/stop execution/broker/order/reconcile/PDT/capital
  gate changes.
- No live `.env` flip for `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES`.

## Architect verdict

This is the right next slice: the route can now be tried behind the existing
flag without making the full public rename. The only caveat is the mixed/all
response tie-order drift for rows with identical timestamps; that is not a data
or field mismatch, but the Phase 5AI trial should inspect the live UI/API
behavior before leaving the flag on permanently.
