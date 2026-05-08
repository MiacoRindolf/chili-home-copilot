# NEXT_TASK: f-equity-reconcile-partial-list-guard

STATUS: DONE

## Goal

Close Phase B's audit-confirmed Case C: post-R32 phantom rows from
the **partial-list** failure mode (broker returns most positions but
truncates one). Add a per-position consecutive-cycle confirmation
counter so a position must be missing from `rh_tickers` for N
consecutive `sync_positions_to_db` cycles (default N=2) before the
stale-close path can close it.

The full brief is at
`docs/STRATEGY/QUEUED/f-equity-reconcile-partial-list-guard.md`
— read it first.

## Why now

Phase B (commit `bc1a0f3`) shipped today; the operator-run audit
returned:

| Window | Count | Notes |
|---|---|---|
| Pre-R32 (before 2026-05-01T04:08:57Z) | 31 | The Apr 29-30 cascade |
| **Post-R32** | **2** | The structural gap |

Both post-R32 phantoms have the same fingerprint:
- `id=1819 ticker=JOB exit=2026-05-06T13:46:03 last_sync=2026-05-06T13:40:02`
- `id=1820 ticker=PED exit=2026-05-08T12:58:03 last_sync=2026-05-08T12:52:02`

Both: `broker_order_id=NULL`, `last_fill_at=NULL` (synthesized
closes); `last_broker_sync` exactly 6 minutes before `exit_date`
(one broker_sync cycle); single-row events (Phase B's
wipeout-burst breaker at ≥3-in-5s correctly didn't fire).

This is **Case C** from Phase B's parent brief. R32 catches
empty-`rh_tickers` only; the missing-from-otherwise-non-empty-list
case is the gap. The fix is the per-position consecutive-cycle
counter (parent brief option B), not options A or C — the audit
data confirms the failure mode is single-ticker drops, not
50%+-drops, and not a volume that justifies a snapshot history table.

## The change

1. **Migration 233**: idempotent `ADD COLUMN IF NOT EXISTS
   trading_trades.broker_sync_missing_streak INT NOT NULL DEFAULT 0`.
2. **Settings**: `CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN` (default
   2), surfaced via `app/config.py`. Module-level constant in
   `broker_service.py` lifts from settings.
3. **`sync_positions_to_db` body** (around the existing
   `for trade in stale:` loop): increment streak for missing
   positions, reset for present positions, single bulk UPDATE.
4. **Stale-close gate**: check `streak >= N` AND existing
   `_RECONCILE_CONFIRM_WINDOW` time guard. Both must allow.
5. **Tests**: see acceptance criteria.

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py` — `sync_positions_to_db` body.
  R32 guard at 2109-2150 stays. Phase B's wipeout-burst helper
  stays. The new streak gate goes ahead of `_RECONCILE_CONFIRM_WINDOW`
  inside the stale-close loop.
- `app/migrations.py` — new `_migration_233_*` (last used: 232).
- `app/config.py` — add settings entry.
- `app/services/trading/portfolio_risk.py:1016` — already excludes
  `broker_reconcile_position_gone` from R31's PnL-based check.
  Don't touch.

## Acceptance criteria

1. Migration 233 adds the column idempotently.
2. Settings constant `CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN`
   (default 2). Lifted to a module-level constant in
   `broker_service.py`.
3. Single bulk UPDATE per cycle that increments streak for
   missing trades + resets for present trades. Visible in the
   logs under DEBUG (no extra warning per cycle).
4. Stale-close path checks streak >= N AND time-window. Both
   gates must allow before close fires.
5. Tests in `tests/test_equity_reconcile_partial_list_guard.py`:
   - **streak-increments-on-missing** (0→1).
   - **streak-resets-on-presence** (1→0).
   - **streak-below-threshold-defers-close** (streak=1, N=2,
     time guard expired → no close).
   - **streak-at-threshold-allows-close** (streak=2, N=2, time
     guard expired → close fires with Phase B's
     `[broker_sync] RECONCILE_CLOSE` warning).
   - **fresh-trade-time-guard-still-fires** (brand-new trade with
     `last_broker_sync IS NULL` → time guard defers regardless
     of streak).
   - **JOB / PED replay**: position missing for 1 cycle → streak=1
     → no close. Same position missing again next cycle → streak=2
     → close fires.
6. Existing pdt_guard / wipeout-burst tests still pass.
7. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-08_f-equity-reconcile-partial-list-guard.md`.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. Use the new column; don't smuggle
  state into `notes` or off-schema fields.
- **Hard Rule 6**: migrations are sequential and idempotent. Use
  ID 233 (last used: 232 — verified via grep). The migration must
  use `ADD COLUMN IF NOT EXISTS` so re-runs are no-ops.
- **Edit-tool truncation discipline (HARD).** `broker_service.py`
  is now >4292 lines after Phase B. Splice pattern only.
  `wc -l + ast.parse` post-edit verification mandatory. See memory
  `reference_2026_05_07_widespread_truncation.md`.
- **Tests use `_test`-suffixed DB.**
- **Don't touch `pdt_guard.py`**.
- **Don't touch the Phase B wipeout-burst helper** (different
  layer; complementary not redundant).
- **No magic numbers**: `N=2` lifts from settings, NOT inlined.
- **Testability seams**: borrow Phase B's pattern (`_now`,
  `_breaker_persister` leading-underscore kwargs) for any new
  helpers if the test suite needs to inject fake clocks /
  fake DB sessions.

## Out of scope

- Crypto reconciler.
- Options reconciler.
- The PDT count itself (Phase A territory).
- Cycle-over-cycle 50%-drop guard (parent brief option A — would
  be over-broad for the observed failure mode).
- Position-snapshot history table (parent brief option C — overkill
  for this volume).
- Backfilling old phantom rows.
- Pattern-quality demotion / autotrader exit deferral / crypto
  bypass cleanup (separate briefs already queued).

## Sequencing

1. Truncation scan on `app/services/broker_service.py` and
   `app/migrations.py`.
2. Migration 233.
3. Settings + module-level constant.
4. Splice-edit `sync_positions_to_db` increment/reset block + the
   streak-check ahead of `_RECONCILE_CONFIRM_WINDOW`.
5. Tests.
6. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili broker-sync-worker
   autotrader-worker`.
3. Verify migration 233 applied:
   ```sql
   \d trading_trades
   ```
   New column should be present, default 0.
4. Watch the next `[broker_sync] RECONCILE_CLOSE` warning (if any).
   With N=2 the gap between `last_broker_sync` and `exit_date`
   should be ≥ 2 cycles (~10 min) for any close to fire.
5. After 7 days: re-run the Phase B audit query; the post-R32
   phantom count should be lower (target: 0 in 7d).

## Rollback plan

`git revert` the commit. The new column is additive (default 0)
and doesn't break existing reads. The streak gate is gated on the
column existing AND the settings flag being non-zero; if the column
is absent (revert with mig still applied), `getattr(trade,
'broker_sync_missing_streak', 0)` returns 0 and the gate is a
no-op. Settings flag default is 2; setting to 0 disables the
guard without a code revert.

## What CC should do if it's unsure

1. If the migration ID conflicts (someone pushed a 233 between this
   brief writing and CC running), use the next free ID and surface
   the change in the CC report.
2. If the bulk-UPDATE shape interacts badly with the existing
   `db.add()` / commit flow in the cycle, fall back to per-trade
   updates inside the existing loop — surface the choice in the
   CC report.
3. If a new code path emitting `broker_reconcile_position_gone`
   shows up in grep (today the only writer is
   `broker_service.py:2247`), stop and surface — the brief expects
   the writer to be at one location.
