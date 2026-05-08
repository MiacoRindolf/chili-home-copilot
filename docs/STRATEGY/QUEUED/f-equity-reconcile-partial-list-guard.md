# f-equity-reconcile-partial-list-guard

STATUS: QUEUED
SLUG: equity-reconcile-partial-list-guard
PROPOSED: 2026-05-08
SEVERITY: medium (post-R32 phantom rate is ~2/week; not a wipeout but a structural source of false-PDT-counts and learning-signal noise)

## TL;DR

Phase B audit (commit `a341dfe`'s CC report) found **2 post-R32
phantom rows in 30 days**, both single-row events, both with
`last_broker_sync` exactly 6 minutes before `exit_date`. R32 catches
empty-`rh_tickers` only; the missing-from-otherwise-non-empty-list
case is the gap. Add a **per-position consecutive-cycle confirmation
counter**: a position must be missing from `rh_tickers` for N
consecutive broker_sync cycles (default N=2) before the stale-close
path can close it. Closes Phase B's audit-deferred Case C.

## Why now

Phase B's wipeout-burst breaker (`_record_reconcile_close_burst`)
covers cardinality (≥3 closes in 5s) but does NOT fire on isolated
single-row phantoms. Audit data:

| id | ticker | exit_date | last_broker_sync | gap |
|---|---|---|---|---|
| 1819 | JOB | 2026-05-06T13:46:03 | 2026-05-06T13:40:02 | 6m 1s |
| 1820 | PED | 2026-05-08T12:58:03 | 2026-05-08T12:52:02 | 6m 1s |

Both rows have `broker_order_id=NULL`, `last_fill_at=NULL` (synthesized
closes). Both happened one broker_sync cycle after the last successful
sighting of the position. The current `_RECONCILE_CONFIRM_WINDOW`
time-based guard is global and (per these timings) had already
expired by `exit_date`. A per-position cycle-streak counter would
have shown `streak=1` at close-time and deferred to `streak>=2`,
giving the position a full 10+ minutes of "consistently missing from
broker truth" before closing.

These rows individually don't lose money (the position genuinely
went away), but each one:
- Falsely counts toward PDT until Phase A's filter excludes it
- Pollutes the brain's learning signal with synthesized fills
- Is invisible in the fast-broker-pull path (broker says qty=0
  on re-open the next day, so the position genuinely existed and
  was not just a transient API hiccup — but the close was driven
  by the local reconciler, not by a real broker fill)

## Goal

Add `trading_trades.broker_sync_missing_streak INT NOT NULL DEFAULT 0`.
On every broker_sync pass:

- For every open Trade whose `ticker NOT IN rh_tickers`: increment
  the streak. If `streak >= CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN`
  (default 2), proceed to the stale-close path. If `streak < N`,
  defer the close and continue.
- For every open Trade whose `ticker IN rh_tickers`: reset
  `broker_sync_missing_streak` to 0.

The existing `_RECONCILE_CONFIRM_WINDOW` time guard stays in place
for the **fresh-trade** case (autotrader places, RH hasn't reflected
yet); the cycle-streak guard layers ON TOP. Fresh trades have
`last_broker_sync IS NULL`; the time-window check fires first and
defers; once the first successful sighting happens, `streak` starts
counting from 0.

## Acceptance criteria

1. New column `trading_trades.broker_sync_missing_streak INT NOT NULL
   DEFAULT 0` via mig NNN (next free).
2. Settings constant `CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN`
   (default 2). Module-level constant in `broker_service.py` lifted
   from the env var.
3. `sync_positions_to_db` increments / resets the counter on every
   open trade per cycle (visible: a single `UPDATE` statement with
   CASE-when bound by `ticker IN/NOT IN rh_tickers`).
4. Stale-close path checks `broker_sync_missing_streak >= N` AND the
   existing `_RECONCILE_CONFIRM_WINDOW` time guard. Both must allow
   the close.
5. Tests in `tests/test_equity_reconcile_partial_list_guard.py`:
   - **streak-increments-on-missing**: position absent → streak goes
     0→1.
   - **streak-resets-on-presence**: position absent (streak=1) →
     present (streak=0).
   - **streak-below-threshold-defers-close**: `streak=1` with
     `N=2`, time guard expired → no close, no logger.warning, trade
     stays open.
   - **streak-at-threshold-allows-close**: `streak=2` with `N=2`,
     time guard expired → close fires (with the existing Phase B
     `[broker_sync] RECONCILE_CLOSE` warning).
   - **fresh-trade-time-guard-still-fires**: brand-new trade with
     `last_broker_sync IS NULL` and short `entry_date` → time
     guard defers regardless of streak.
   - **JOB / PED replay**: a position missing for 1 cycle (the
     audit shape) → streak=1 → no close. Same position missing
     again on next cycle → streak=2 → close.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-equity-reconcile-partial-list-guard.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py:2202-2290` (approximate; the
  `for trade in stale:` stale-close loop). Add the streak check
  ahead of the existing `_RECONCILE_CONFIRM_WINDOW` guard.
- `app/migrations.py` — new `_migration_NNN_*` adding the column
  idempotently.
- `app/config.py` — add `CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN`
  (default 2) to settings.
- `app/services/trading/portfolio_risk.py:1016` — already excludes
  `broker_reconcile_position_gone` from R31's PnL-based check.
  Don't touch.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. Add the column via migration; don't
  use a flag in `notes` or some other off-schema field.
- **Hard Rule 6**: migrations are sequential and idempotent. Check
  the last `_migration_NNN_` number before adding.
- **Edit-tool truncation discipline (HARD).** `broker_service.py`
  is large (>4000 lines now after Phase B). Splice pattern only.
  `wc -l + ast.parse` post-edit verification mandatory.
- **Tests use `_test`-suffixed DB.**
- **Don't touch `pdt_guard.py`** (Phase A's filter is the durable
  defence even after this brief ships).
- **Don't touch the wipeout-burst breaker** (Phase B's
  cardinality-based trip stays as-is — this brief is a different
  layer).
- **No magic numbers**: `N=2` default lifts from settings, NOT
  inlined.

## Out of scope

- Crypto reconciler.
- Options reconciler.
- The PDT count itself (Phase A territory).
- Cycle-over-cycle 50%-drop guard (parent brief option A — the
  audit data shows this would be over-broad for the observed
  failure mode; revisit only if we see a true wipeout-class event
  post-Phase-B).
- Position-snapshot history table (parent brief option C — overkill
  for this volume).
- Backfilling old phantom rows.

## Sequencing

1. Truncation scan.
2. Migration NNN: idempotent ADD COLUMN
   `broker_sync_missing_streak INT NOT NULL DEFAULT 0`.
3. Settings + module-level constant.
4. Splice-edit `sync_positions_to_db`: increment/reset block + the
   streak-check ahead of `_RECONCILE_CONFIRM_WINDOW`.
5. Tests.
6. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili broker-sync-worker
   autotrader-worker`.
3. Verify migration NNN applied:
   ```sql
   \d trading_trades
   ```
   New column should be present, default 0.
4. Watch the next `[broker_sync] RECONCILE_CLOSE` warning (if any).
   Compare exit_date to last_broker_sync; with N=2, the gap should
   be ≥ 2 cycles (~10 min) for any close to fire.
5. After 7 days: re-run the Phase B audit query; the post-R32
   phantom count should be lower (target: 0 in 7d).

## Rollback plan

`git revert` the commit. The new column is additive (default 0)
and doesn't break existing reads. The streak-check is gated on the
column existing AND the settings flag being non-zero; if the column
is absent (revert with mig still applied), `getattr(trade,
'broker_sync_missing_streak', 0)` returns 0 and the gate becomes
a no-op. Settings flag default is 2; setting to 0 disables the
guard without a code revert.

## Open questions

1. **Should `last_broker_sync` be the cycle counter instead of a
   new column?** It's tempting to compute "streak" on the fly from
   `last_broker_sync` deltas, but that's brittle (sync_interval
   changes mid-flight, cycle skipped due to API failure, etc.).
   The dedicated column is the cleaner data shape.

2. **Cycle interval drift.** The current broker_sync interval is
   ~5 min. If the operator changes the interval, N=2 has different
   wall-clock implications. Surface this in the CC report; the
   `CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN` env var already lets
   the operator tune.

3. **Streak-counter overflow.** If a position genuinely stays open
   indefinitely, the streak will never grow because every
   sighting resets it. If a position genuinely disappears (e.g.,
   broker error in our favour), the streak grows by 1 per cycle
   forever until the close fires. No overflow concern in practice;
   INT is plenty.

4. **Interaction with the `bracket_reconciliation_service` qty=0
   re-open detection.** The audit notes the broker shows qty=0 on
   the May-1 re-opens of the same tickers from the wipeout. If
   that service has its own reconcile path, surface in the CC
   report — this brief touches `sync_positions_to_db` only.
