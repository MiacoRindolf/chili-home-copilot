# NEXT_TASK: f-execution-events-sell-side-recording

STATUS: PENDING

## Goal

Close the architectural gap surfaced by Phase 4 (commit `cdf65fe`): SELL fills are not currently recorded in `trading_execution_events`. The Phase 4 inverse-reconcile reader (`position_has_recorded_sell`) queries this table; with zero sell events in the database, flipping `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` would re-open every previously-closed position.

After this brief:
1. Every NEW sell order placement + fill writes a row to `trading_execution_events` with `status='filled'` and `payload_json->>'side' = 'sell'`.
2. Historical sells are backfilled from `trading_trades` (where `exit_date IS NOT NULL AND exit_price IS NOT NULL`) as synthetic events with `event_type='backfill_exit_fill'` for audit clarity.
3. After backfill, `SELECT COUNT(*) FROM trading_execution_events WHERE status='filled' AND LOWER(payload_json->>'side')='sell'` returns a count in the hundreds (one per closed trade with a real exit).
4. Phase 4 flag becomes safe to enable.

## Why now

Phase 4 is shipped but operator-blocked. The flag-flip is the gating step for retiring the conservative `event_count == 0` workaround at `broker_service.py:~1944`. Without sells in the events table, Phase 4 is dead code.

This brief is the smallest scope that unblocks Phase 4. Phase 5 (envelope-rename + decision-layer split) is a much larger refactor and should wait until Phase 4 is operationally enabled.

## Investigation phase (read-only, ~30 min)

1. Verify the confirmed gaps:
   - `app/services/trading/robinhood_exit_execution.py` — places SELL orders at lines 945/954 via `adapter.place_*_order(side="sell", ...)`. Confirm `record_execution_event` is not called anywhere in the post-place / sync_pending_exit_order path.
   - `app/services/broker_service.py:4310-4331` — `for trade in open_with_pending_exit` loop calls `sync_pending_exit_order` (in `robinhood_exit_execution.py`) which also doesn't record an event.
   - `app/services/coinbase_service.py` — confirm the analogous gap for Coinbase exit paths.
   - Bracket-fired stops: broker fills the resting stop order out-of-band; CHILI's poll path doesn't see it as a `record_execution_event` call. Check `bracket_reconciliation_service.py`.

2. Map the SELL fill detection flow end-to-end. For each path that detects a sell fill, identify the right place to call `record_execution_event`.

## Implementation phase

For every SELL fill detection path, add:

```python
from .execution_audit import record_execution_event

record_execution_event(
    db,
    user_id=trade.user_id,
    ticker=trade.ticker,
    trade=trade,  # so position_id resolves via the Phase 2 resolver
    scan_pattern_id=getattr(trade, "scan_pattern_id", None),
    broker_source=trade.broker_source,
    order_id=sell_order_id,
    event_type="exit_fill",  # or whatever the existing convention prefers
    status="filled",
    average_fill_price=fill_price,
    cumulative_filled_quantity=fill_qty,
    payload_json={
        "side": "sell",
        # ... other broker-payload fields
    },
)
```

The `record_execution_event` call already resolves `position_id` via the Phase 2 resolver — every new sell event will land with `position_id` populated.

## Backfill migration (254)

```python
def _migration_254_synthetic_exit_fills(conn) -> None:
    """Backfill synthetic sell-side execution events for historical
    closed trades, so Phase 4's position_has_recorded_sell helper has
    data to query.

    For every trading_trades row with status='closed' AND
    exit_date IS NOT NULL AND exit_price IS NOT NULL AND quantity > 0
    AND entry_price > 0, INSERT a row into trading_execution_events:

        event_type = 'backfill_exit_fill'
        status = 'filled'
        average_fill_price = exit_price
        cumulative_filled_quantity = quantity
        event_at = exit_date
        recorded_at = NOW()
        position_id = (resolved via natural-key join)
        payload_json = {"side": "sell", "source": "mig254_backfill",
                        "trade_id": <id>}

    Idempotent: WHERE NOT EXISTS (SELECT 1 FROM trading_execution_events
    WHERE trade_id = t.id AND event_type = 'backfill_exit_fill').
    """
```

## Brain integration (reuse, don't rewrite)

- `app/services/trading/execution_audit.record_execution_event` — the writer. Already calls the Phase 2 resolver for position_id.
- `app/services/trading/position_resolver.resolve_position_id` — gets called automatically inside `record_execution_event`.
- No new helper needed; this brief is purely "wire the missing callers".

## Constraints / do not touch

- **No live broker behavior change.** Adding execution_event rows is observability only.
- **Tests use `_test`-suffixed DB.** Standard hard rule.
- **Idempotent backfill.** Re-running mig 254 must not double-insert (guarded by NOT EXISTS).
- **Don't auto-flip the Phase 4 flag in this brief.** That's an operator decision after the backfill probe shows reasonable cardinality.
- **No magic values for `event_type`.** Pick existing conventions where they exist (e.g., `'status'` matches the buy-side polling; `'exit_fill'` is a new but reasonable label).

## Out of scope

- The Phase 4 flag flip itself (operator decision post-deploy).
- Sell-side TCA writes (`apply_tca_on_trade_close` is already wired; this brief is event-recording only).
- Coinbase-specific sell paths if they're already covered — check first.
- Phase 5 (envelope-rename) — separate refactor.

## Success criteria

1. New `record_execution_event(..., payload_json={'side': 'sell', ...})` calls added to every SELL fill detection path (Robinhood exit, Coinbase exit, bracket-fired stops).
2. Mig 254 backfills synthetic exit fills for closed trades.
3. Post-deploy probe shows hundreds of sell events:
   ```sql
   SELECT COUNT(*) FROM trading_execution_events
    WHERE status='filled' AND LOWER(payload_json->>'side')='sell';
   ```
4. `position_has_recorded_sell(db, pid)` returns True for the majority of positions with `state='closed'`.
5. Tests: ≥4 new pytest cases covering the new writers + backfill idempotency.
6. CC report documents the flag-flip checklist for the operator.

## Rollback plan

- Code revert: `git revert <commit>` removes the new writer calls. Existing call sites unchanged.
- Mig 254: synthetic rows can be deleted via `DELETE FROM trading_execution_events WHERE event_type='backfill_exit_fill'`. Idempotent backfill can be re-run safely.

## Reference

- Phase 4 CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-4.md`
- Existing buy-side writer pattern: `app/services/broker_service.py:4346`
- Phase 2 resolver: `app/services/trading/position_resolver.resolve_position_id`
- Phase 4 helper: `app/services/trading/position_resolver.position_has_recorded_sell`
