# CC_REPORT: f-position-identity-phase-4-inverse-reconcile-position-history

**Session type:** Cowork-direct execution (operator said "continue with the next phase, use claude/daemon" after the Phase 3 + TCA push).

## What shipped

**Commit `cdf65fe` on `main`**, 6 files / ~250 LOC:

- `app/config.py` — new flag `chili_position_identity_phase4_authority_enabled` (default `False`)
- `app/services/trading/position_resolver.py` — new helper `position_has_recorded_sell(db, position_id) -> bool`
- `app/services/broker_service.py` — inverse-reconcile branch (~line 1944) gains the flag-gated alternate path; logger tagged with phase
- `tests/test_position_identity_phase2.py` + `test_position_identity_phase3.py` — reader canaries updated to allow the intentional Phase 4 reader in `broker_service.py`
- `tests/test_position_identity_phase4.py` — 7 new pinned tests

**No migration.** Phase 4 is pure code; the schema and data from Phase 2 (mig 248) + Phase 3 (mig 249) provide everything needed.

## Verification

**Tests.** 27/27 PASS (10 Phase 2 + 10 Phase 3 + 7 Phase 4). Compile clean across 6 files.

**Deploy.** All 5 containers force-recreated. Helper importable. Flag confirmed `False` by default. Existing inverse-reconcile behavior unchanged on deploy (the legacy `event_count == 0` path is still authoritative).

**Live DB observations (Phase 4 readiness):**

| Check | Result |
|---|---|
| `schema_version` tip | unchanged at `253_tca_backfill_guard_phantom_rows` (Phase 4 has no migration) |
| Sell events in `trading_execution_events` | **0** ← BLOCKER |
| Buy events in `trading_execution_events` | 8,352 |
| Positions with at least one recorded sell | 0 |
| 201 trading_positions rows | all `has_sell = False` |

## CRITICAL FINDING — Phase 4 cannot be flipped on yet

**Zero sell events are recorded in `trading_execution_events`.** Investigation:

- `robinhood_exit_execution.py` places SELL orders at the broker via `adapter.place_*_order(side="sell", ...)` but **does not call `record_execution_event` for sell submissions or sell fills.**
- `broker_service.py:4346` calls `record_execution_event` for trades with `broker_order_id` — but `broker_order_id` is the BUY order's id. Once the buy is filled and the position is open, subsequent SELL orders (placed by the exit path or by bracket fires) have their own broker_order_ids that aren't tracked by `trade.broker_order_id`.
- `bracket_writer_g2.py:594` records `g2_place_missing_stop_*` events — but these are stop-placement *attempts*, not actual sell fills. Status of those is always `rejected`/`submitting`/`submitted`, never `filled`.

**Consequence:** if the operator flips `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` today, every closed Trade would look like "no sell recorded → bookkeeping-only close → eligible for re-open". The flag would catastrophically re-open every previously-closed position that broker_sync sees as alive on the broker side.

**Phase 4 ships in shadow-correct state.** The code is right; the data is incomplete. The flag stays OFF until sell-side event recording is fixed — that's Phase 4.5 / the new NEXT_TASK.

## Surprises / deviations

1. **Sell-side recording gap not called out in the Phase 4 brief.** The brief assumed `position_has_recorded_sell` would just work with existing data. The Phase 1 author's note in `broker_service.py:1965-1968` literally says "there is no SELL discriminator on the events table" — that comment turned out to mean *the events table doesn't reliably get SELL events written* (not just "the events table has no field for it"). I should have probed sells earlier.

2. **Helper is robust against position_id=0.** Python's truthiness on 0 was a possible bug surface (an `if not position_id` check would mis-fire on id 0). I used `is None` explicitly and pinned this with a test.

3. **Logger tags now Phase-4-aware.** Re-open logs include `[phase4_no_sell]` or `[phase1_event_count_0]` so post-flip audits can distinguish which path made the call. Contradiction logs similarly tagged.

## Deferred → new NEXT_TASK

**`f-execution-events-sell-side-recording`** is the necessary prerequisite for the Phase 4 flag-flip. Brief sketch:

- Audit every code path that places a SELL order. Confirmed gaps:
  - `robinhood_exit_execution.py` exit submissions (entry-time + stop-fired)
  - Bracket stop fills (broker fires the resting stop; CHILI's polling path doesn't record the resulting fill as a sell event)
- For each gap, add a `record_execution_event(..., side='sell', ...)` call after the order acknowledgment / fill detection.
- Ensure `payload_json['side']='sell'` is set (the Phase 4 helper queries on this).
- Backfill historical sells from `trading_trades.exit_date / exit_price` where possible: for every closed trade with `exit_price IS NOT NULL`, write a synthetic event with `status='filled'`, `side='sell'`, `average_fill_price=exit_price`, `event_at=exit_date`, `event_type='backfill_exit_fill'`. One-shot migration.
- After backfill, re-probe: `positions_with_sell` should be in the hundreds. Then Phase 4 flag can be enabled with confidence.

## Rollback plan

- Flag stays off (default) — Phase 4 path is dormant.
- `git revert cdf65fe` removes the helper + reader; legacy `event_count == 0` continues unchanged.
- Reader canary allowlist updates can stay; they're harmless if the readers don't exist.

## Status

Phase 4 code shipped. Operator action: do NOT flip the flag until `f-execution-events-sell-side-recording` lands and backfills historical sells. NEXT_TASK set to that brief.

CC_REPORT = this file. Plan + memory updated separately.
