# NEXT_TASK: f-bracket-fired-stop-recording

STATUS: PENDING

## Goal

Close the last sell-event gap in Phase 4's data plumbing: when a resting BRACKET STOP order fires at the broker (the most common exit path for protected positions), CHILI's order-state polling detects the state change but does NOT yet write a sell `execution_event` for the FILL itself. After this brief:

1. Every broker-fired bracket stop fill writes a row to `trading_execution_events` with `status='filled'`, `payload_json->>'side'='sell'`, `event_type='bracket_stop_fired'` (or similar).
2. The new event resolves `position_id` via the Phase 2 resolver automatically (free via the existing `record_execution_event` integration).
3. Phase 4's `position_has_recorded_sell` helper sees these fills going forward.
4. Combined with mig 254 (historical synthetic backfill) and the previously-shipped exit-monitor writer + Coinbase sync-gone writer, the **entire** universe of sell events is now captured in `trading_execution_events`.

## Why now

Coinbase + Robinhood-crypto exit recording shipped 2026-05-19. The remaining gap is the broker-fired stop path: when the brain places a stop order at the broker, then later the broker fires that order autonomously when price hits the stop level. Today the order's filled-state change is detected by `broker_service.py`'s order polling loop, but the resulting fill is not written as a sell `execution_event`.

This is the last writer-side gap before Phase 5 can begin safely.

## Investigation phase (~30 min, read-only)

1. Find the path that detects bracket-stop fills. Likely candidates:
   - `app/services/broker_service.py:4310-4331` (open_with_pending_exit loop) — calls `sync_pending_exit_order`. Check if THAT function writes events.
   - `app/services/trading/bracket_reconciliation_service.py` — reconciles bracket state against broker. May detect broker-fired stops.
   - `app/services/trading/robinhood_exit_execution.py:sync_pending_exit_order` — already known to not call `record_execution_event` for fills.
2. Cross-check by querying `trading_execution_events` over the last 30d for ANY rows with `event_type` referencing stop-fill or bracket-fire. If none, the gap is real.

## Implementation phase

For each detection path identified in investigation:

```python
from .execution_audit import record_execution_event

record_execution_event(
    db,
    user_id=trade.user_id,
    ticker=trade.ticker,
    trade=trade,  # so position_id resolves via Phase 2 resolver
    scan_pattern_id=getattr(trade, "scan_pattern_id", None),
    broker_source=trade.broker_source,
    order_id=stop_order_id,
    event_type="bracket_stop_fired",
    status="filled",
    average_fill_price=fill_price,
    cumulative_filled_quantity=fill_qty,
    payload_json={
        "side": "sell",
        "source": "bracket_stop_broker_fire",
        "trade_id": int(trade.id),
        "stop_intent_id": getattr(bracket_intent, "id", None),
    },
)
```

Wrap in try/except — never block the existing close flow.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/execution_audit.record_execution_event` — same writer used by Coinbase exit + Robinhood-crypto-exit hooks.
- `app/services/trading/position_resolver.resolve_position_id` — auto-resolves position_id inside record_execution_event. No new lookups needed in this brief.

## Constraints / do not touch

- **No live behavior change.** Observability only. The bracket reconciler / exit poller already manages the Trade row state; this brief just adds the audit-event row.
- **Tests use `_test`-suffixed DB.** Standard rule.
- **Wrap in try/except.** Mandatory; failure must NEVER block the close path.
- **Don't backfill historical broker-fired stops via a new mig.** Mig 254 covered the cohort by synthesizing from `trading_trades.exit_*`. Backfilling stop fills SPECIFICALLY would require digging into broker order-history APIs which is out of scope.

## Out of scope

- Phase 5 envelope-rename — separate refactor; waits for at least one real `[phase4_*]` log line in production.
- Any RH session restoration (operator action, separate).
- The known-quiet `chk_trades_entry_price_positive` phantom row #404 (already-protected by phantom-row guard in mig 253 backfill).

## Success criteria

1. Investigation enumerates every bracket-stop-fill detection site.
2. Each gets a `record_execution_event(payload.side='sell')` writer hook wrapped in try/except.
3. Post-deploy probe (run after at least one bracket stop fires) shows a new `event_type='bracket_stop_fired'` row in `trading_execution_events`.
4. Tests (≥3): static-grep for the writer call sites + try/except wrappers, mirror the pattern from `tests/test_coinbase_exit_side_recording.py`.
5. CC_REPORT documents the writer-event-type label (so future audit queries know what to look for).

## Rollback plan

- `git revert <commit>` removes the writer calls. Existing close paths unchanged.
- Any new events already written stay; harmless audit rows.

## Reference

- Coinbase exit-recording CC report: `docs/STRATEGY/CC_REPORTS/2026-05-19_f-coinbase-exit-side-recording.md`
- Phase 4 helper: `app/services/trading/position_resolver.position_has_recorded_sell`
- Phase 4 CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-4.md`
- Mig 254 (synthetic backfill): `app/migrations.py:_migration_254_synthetic_exit_fill_events`
