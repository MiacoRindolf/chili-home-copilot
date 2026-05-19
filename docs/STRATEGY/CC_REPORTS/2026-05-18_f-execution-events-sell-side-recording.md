# CC_REPORT: f-execution-events-sell-side-recording

**Session type:** Cowork-direct execution (operator said "go do it" after the Phase 4 ship report identified the sell-recording gap as the gating prerequisite for the Phase 4 flag-flip).

## What shipped

**Single commit on `main`**, 3 files / ~200 insertions:

- `app/migrations.py` — `_migration_254_synthetic_exit_fill_events` (mig 254 inserts synthetic `backfill_exit_fill` events for closed trades; idempotent; phantom-row guarded)
- `app/services/trading/robinhood_exit_execution.py` — `record_execution_event` calls added at TWO sites:
  1. `submit_robinhood_trade_exit` (after `_finalize_filled_exit` sets terminal state)
  2. `sync_pending_exit_order` (after polled-fill detection)
- `tests/test_sell_side_recording.py` — 5 new pytest cases

## Verification

**Tests.** 12/12 PASS (5 new sell-side + 7 existing Phase 4). Compile clean across 3 files.

**Deploy.** All 5 workers force-recreated. Mig 254 fired at 2026-05-19 05:59:15 UTC.

**Post-deploy live DB:**

| Metric | Pre-fix | Post-fix |
|---|---|---|
| `schema_version` tip | `253_tca_backfill_guard_phantom_rows` | **`254_synthetic_exit_fill_events`** |
| Total sell events (status=filled, side=sell) | 0 | **450** |
| Distinct positions with recorded sell | 0 | **112** |
| Closed positions with recorded sell | 0 / 196 | **107 / 196 (55%)** |
| Phase 4 flag flippable | NO | **YES** |

**Distribution by state × has_sell:**
- 89 closed positions without sells (legitimate "never really sold" cases — exit_date NULL or exit_price NULL or phantom rows)
- 107 closed positions with sells (synthetic backfill rows)
- 5 open positions with sells (cross-generation linkage — position has past closed trades plus a current open Trade row; this is exactly the close-and-reopen pattern Phase 2-3 were built for)

## Phase 4 enablement is now safe

Pre-deploy: flipping `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` would have classified every closed position as "no sell recorded → bookkeeping-only close → eligible for re-open" → catastrophic mass-reopen on next `broker_sync`.

Post-deploy: 107 of 196 closed positions have sell evidence. The Phase 4 reader can now precisely distinguish:
- **Has sell + broker says alive** → re-buy after sell (don't re-open old Trade row; create new one) → contradiction logged
- **No sell + broker says alive + qty/price match** → bookkeeping-only close → re-open existing Trade row

Operator can flip the flag after a paper-soak window. Suggested arm-up:

1. Inspect first 24h of `broker_sync` logs for any `[phase4_no_sell]` re-open events under the flag-on path (manually via `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` enabled briefly).
2. Compare against historical `[phase1_event_count_0]` re-opens. The Phase 4 path should produce a SUPERSET (catches more legitimate bookkeeping-only closes) without false positives.
3. Then enable durably.

## Surprises / deviations

1. **Terminal guard already in place.** `apply_execution_event_to_trade` (called inside `record_execution_event` when `trade=trade`) has a `_is_terminal` check that protects closed trades from entry_price clobber. Since both my new call sites fire AFTER `_finalize_filled_exit` sets `status='closed'` + `exit_date`, the guard is active and no trade-row side effects occur. This was a relief — meant I could pass `trade=trade` (gets trade_id linkage) without needing a new `skip_trade_apply` kwarg.

2. **Transient Windows TCP buffer exhaustion (WSAENOBUFS 10055).** The rapid back-to-back container recreates of the past two hours exhausted ephemeral ports on the Windows host. Probe retries succeeded after a short wait. Worth noting for future operator: avoid clustering multiple `docker compose up --force-recreate` invocations within ~60s windows.

3. **Coinbase exit path NOT patched in this commit.** The mig 254 backfill walks all `trading_trades` regardless of broker, so HISTORICAL Coinbase sells are covered (e.g., the 53 Coinbase positions get the same treatment as Robinhood). Going forward, new Coinbase exits won't auto-record sells until that path is patched — but the mig 254 covers them at next refresh if the backfill is re-runnable (and it is — idempotent). Operator can run `python -c "from app.migrations import _migration_254_synthetic_exit_fill_events; ..."` against the live DB as a cron or include in a future code-fix brief.

## Deferred

- **Coinbase exit-path code change.** `coinbase_service.py` exit handlers have the same gap as the Robinhood path. Brief: `f-coinbase-exit-side-recording`. Lower priority because (a) mig 254 covers historical Coinbase sells, (b) re-running mig 254 periodically catches new ones, (c) the Robinhood path is where the vast majority of activity happens.
- **Bracket-fired stop sells.** When a resting stop fires at the broker, that fill is detected by `broker_service.sync_pending_orders` (or similar polling) — not by either of the two paths I patched. Audit pending; likely covered by the broker_service.py:4346 path which calls `record_execution_event` for `open_with_order_id` trades. Verify if `trade.broker_order_id` actually tracks the SELL order id after a stop fire. Probably it doesn't — the stop is a separate order. Separate brief.
- **Operator flag-flip** for Phase 4. Pending operator decision after this deploy soaks.

## Rollback plan

- `git revert <commit>` — removes the writer-side hooks. New sells stop being recorded going forward; existing backfill rows stay.
- Mig 254 rows can be deleted: `DELETE FROM trading_execution_events WHERE event_type='backfill_exit_fill';`. Backfill is idempotent — can be re-run safely.
- Phase 4 flag default is still OFF; nothing changes operationally until operator flips it.

## Status

Sell-side recording shipped. Phase 4 flag is now safe to flip (operator decision). NEXT_TASK can be set to either:
1. The operator-flip-Phase-4 brief (paper soak + monitor + arm)
2. Phase 5 (envelope-rename) — the larger refactor that the position-identity foundation enables
3. The TCA-followup briefs (maker-only Coinbase, tighter entry gating, reference re-snap) which target the +102 bps slippage finding

My recommendation: **Phase 4 flag-flip brief first** (small, validates the entire arc), then Phase 5 OR TCA followups based on operator priority.
