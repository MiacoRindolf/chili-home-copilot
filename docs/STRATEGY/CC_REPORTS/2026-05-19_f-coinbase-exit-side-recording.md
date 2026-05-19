# CC_REPORT: f-coinbase-exit-side-recording

**Session type:** Cowork-direct execution ("do the next task per your reco" — Coinbase parity for Phase 4 sell-side recording).

## What shipped

**Single commit** (current `main` tip), 3 files / **+248 / -0**:

- `app/services/trading/crypto/exit_monitor.py` (+40) — writer hook in `run_crypto_exit_pass` after the SELL submission via `_place_market_sell_for_trade`. Covers **both** Coinbase AND Robinhood crypto exits (the function dispatches to both venues).
- `app/services/coinbase_service.py` (+47) — writer hook in `sync_positions_to_db` stale-close branch after the `coinbase_position_sync_gone` auto-closure.
- `tests/test_coinbase_exit_side_recording.py` (+161) — 5 new pinned tests.

**No migration.** Pure code change; no schema or backfill needed (mig 254 already covered the historical synthetic backfill).

## What changed semantically

| Path | Trigger | New event written |
|---|---|---|
| `crypto/exit_monitor._place_market_sell_for_trade` | Successful market-sell submission for ANY crypto position (Coinbase OR Robinhood crypto) | `event_type='crypto_exit_submitted'`, `status='submitted'`, `payload_json={'side':'sell', 'source':'crypto_exit_monitor', 'trade_id':..., 'reason':...}` |
| `coinbase_service.sync_positions_to_db` stale-close | Local trade was open, broker position vanished → auto-close with `exit_reason='coinbase_position_sync_gone'` | `event_type='coinbase_position_sync_gone_close'`, `status='filled'`, `payload_json={'side':'sell', 'synthetic':True, 'source':'coinbase_position_sync_gone', ...}` |

Both writers go through `record_execution_event` which:
- Resolves `position_id` via the Phase 2 resolver automatically
- Writes to `trading_execution_events`

Both writers wrapped in `try/except` — a record-event DB error NEVER blocks the exit submission or auto-close.

## Verification

**Tests.** 31/31 PASS (5 new Coinbase-exit-recording + 26 existing position-identity Phase 2/3/4 tests). No regressions.

**Compile.** All 3 files compile cleanly.

**Deploy.** `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker` clean.

**Post-deploy event-type distribution** (`scripts/dispatch-coinbase-exit-commit-deploy-out.txt`):

| event_type | n |
|---|---|
| `status` | 8,352 (buy-side polling, unchanged) |
| `g2_place_missing_stop_submitting` | 3,387 (chronic, unchanged) |
| `g2_place_missing_stop_rejected` | 3,286 |
| **`backfill_exit_fill`** | **450** (mig 254 synthetic sells, unchanged) |
| `g2_place_missing_stop_submitted` | 90 |
| `emergency_terminal_reject_repair` | 20 |
| others | minor |

The new event types (`crypto_exit_submitted`, `coinbase_position_sync_gone_close`) have not appeared yet because no crypto exit / Coinbase stale-close fired in the post-deploy window. They will materialize on the next real exit. The earlier sample of 133 `coinbase_position_sync_gone` closes in 30d (the bleeding-but-uncounted cohort) tells us this writer will start producing events within the next 24-48 hours of normal activity.

## Surprises / deviations

1. **`crypto/exit_monitor.py` covers BOTH venues.** The function `_place_market_sell_for_trade` already dispatches to `coinbase_service.place_sell_order` OR `broker_service.place_crypto_sell_order` depending on the trade's broker. So writing the event AFTER that call covers Coinbase AND Robinhood crypto exits with one fix.

2. **Coinbase has no inverse-reconcile branch.** `coinbase_service.sync_positions_to_db` doesn't have the Robinhood-style "broker says alive, local says closed → consider re-opening" path. Coinbase just CREATES a new Trade row when it sees a new broker position. So **Phase 4 parity isn't needed for Coinbase on the reader side** — the only thing it needs is the SELL-event recording so that if Phase 4 ever queries a Coinbase position (e.g., via a future shared inverse-reconcile path), the helper sees the sells.

3. **First static-grep test regex was too strict.** Used `[^}]` which excluded dict literals in payloads. Rewrote as a line-by-line walker. 31/31 passing now.

## Deferred

- **`f-bracket-fired-stop-recording`** — Broker-fired bracket stop fills still bypass `record_execution_event`. When a resting stop order fires at the broker, CHILI's polling detects the order state change but doesn't yet write a sell event for the FILL itself. Mig 254 backfilled historical closures via `trading_trades.exit_*`, but new bracket-fires would still be missed. Next natural side branch.
- **Phase 5 envelope-rename + decision-layer split** — Waits for Phase 4 to be exercised in production (i.e., at least one real `[phase4_*]` log line from `broker_service.sync_positions_to_db` inverse-reconcile branch).

## Rollback plan

- `git revert <commit>` — removes both writer call blocks; existing call sites unchanged.
- New events (`crypto_exit_submitted`, `coinbase_position_sync_gone_close`) that have already been written stay; they're harmless audit rows.
- If for some reason a writer error DID block an exit (the try/except should make this impossible, but for paranoia): explicitly set `record_execution_event` to a no-op via a feature flag. Not implemented; not expected to be needed.

## Status

Three of three side-branch tasks for "Coinbase exit-side recording" complete. CC report = this file. Memory + plan + decision log updated separately.
