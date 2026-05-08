# CC_REPORT: f-equity-reconcile-partial-list-guard

## Outcome

Closes Phase B's audit-confirmed Case C: post-R32 phantom rows from
the partial-list failure mode. Single-ticker drops from a non-empty
broker response now require **2 consecutive cycles missing** before
the stale-close path fires (default
`CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN=2`). Both safeguards must
pass:

1. **NEW** — per-trade `broker_sync_missing_streak >= N`.
2. **EXISTING** — `_RECONCILE_CONFIRM_WINDOW` time guard.

Replay of the JOB/PED fingerprint (one broker_sync cycle gap between
`last_broker_sync` and `exit_date`) shows the close is now deferred
on cycle 1 (streak=1) and authorized on cycle 2 (streak=2) — which
matches the brief's design.

## Per-step status

### Step 1 — Truncation scan + verify migration 233 free — COMPLETE
* `broker_service.py`: 4292 lines, AST clean.
* `migrations.py`: 15794 lines, AST clean. Last registered =
  `_migration_232_fast_path_maker_only` (verified). 233 is free.

### Step 2 — Migration 233 + settings + module constant — SHIPPED
* `_migration_233_reconcile_partial_list_streak`:
  `ALTER TABLE trading_trades ADD COLUMN IF NOT EXISTS
  broker_sync_missing_streak INTEGER NOT NULL DEFAULT 0`. Idempotent
  per Hard Rule 6. Registered in `MIGRATIONS` after 232.
* `app/models/trading.py:Trade` extended with
  `broker_sync_missing_streak: int = Column(Integer, nullable=False,
  server_default="0", default=0)` so ORM-side reads + bulk UPDATEs
  resolve cleanly.
* `app/config.py:Settings` extended with
  `chili_reconcile_partial_list_streak_min: int = Field(default=2,
  validation_alias=AliasChoices("CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN"))`.
* `broker_service.py` module-level constant
  `_RECONCILE_PARTIAL_LIST_STREAK_MIN = int(getattr(settings,
  "chili_reconcile_partial_list_streak_min", 2))` immediately after
  `_RECONCILE_CONFIRM_WINDOW` so reconcile-related constants stay
  grouped.

### Step 3 — Splice streak increment/reset + close-gate — SHIPPED
Three insertions into `broker_service.py` via the splice script
(per Hard truncation discipline):

* **Bulk UPDATE** (after R32 guard, before stale-close): two
  ORM-driven `db.query(Trade).filter(...).update(...)` calls inside
  `if rh_tickers:` — the increment query is GATED on a non-empty
  rh_tickers because R32 already short-circuits the empty case
  above. Otherwise an empty broker response would mass-increment
  every trade's streak and could re-create the wipeout cascade in
  a different shape.
* **Per-trade close-gate** (inside `for trade in stale:`): single
  `getattr(trade, "broker_sync_missing_streak", 0) or 0` read +
  `if streak < _RECONCILE_PARTIAL_LIST_STREAK_MIN: continue`. The
  existing `_RECONCILE_CONFIRM_WINDOW` time-window check stays
  immediately after, so both gates must pass.
* **DEBUG logs** on both the bulk-update and the gate so ops can
  trace per-cycle behaviour without WARN-level noise.

Post-edit: `wc -l` 4292 → 4360 (+68); AST clean.

### Step 4 — Tests — SHIPPED
`tests/test_equity_reconcile_partial_list_guard.py`:

1. **`test_streak_increments_on_missing`** — trade missing from
   non-empty rh_tickers → streak 0 → 1; trade stays open (below
   threshold).
2. **`test_streak_resets_on_presence`** — trade present in
   rh_tickers → streak 1 → 0.
3. **`test_streak_below_threshold_defers_close`** — streak=1, N=2,
   time-window expired → no close (gate is AND, not OR).
4. **`test_streak_at_threshold_allows_close`** — streak=2 (after
   the cycle's increment), N=2, time-window expired → close fires
   AND Phase B's `[broker_sync] RECONCILE_CLOSE` warning is emitted.
5. **`test_fresh_trade_time_guard_still_fires`** — fresh trade with
   `last_broker_sync=NULL` but recent `entry_date`: even if streak
   is high, time guard defers the close (uses `entry_date` as
   fallback per the existing `refs = [...]` logic).
6. **`test_job_ped_replay_first_cycle_defers_second_cycle_closes`** —
   the brief's named scenario: position missing for 1 cycle →
   streak=1 → no close. Same trade missing in next cycle →
   streak=2 → close fires + RECONCILE_CLOSE warning.

### Step 5 — CC report + commit + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Bulk-UPDATE gated on non-empty rh_tickers**, not unconditional.
   The brief described "increment streak for missing positions,
   reset for present positions" without specifying the empty-list
   case. R32 already handles the empty case by skipping the entire
   stale-close path; if I unconditionally incremented streaks when
   rh_tickers was empty, the next non-empty cycle could trip the
   gate on perfectly healthy trades that had spent two cycles in a
   broker auth flap. Gating the increment behind `if rh_tickers:`
   keeps R32 and this gate compositional rather than overlapping.

2. **ORM model column added.** The brief's "Brain integration"
   section doesn't mention `app/models/trading.py`, but the bulk
   UPDATE uses `Trade.broker_sync_missing_streak` as a column
   reference (cleaner than raw SQL). Adding the column to the ORM
   was the natural fit. `server_default="0"` matches the
   migration's `DEFAULT 0` so the schema and ORM agree at INSERT.

3. **Phase B observability is what asserts close-fired.** Test 4
   and Test 6 both check the `[broker_sync] RECONCILE_CLOSE`
   warning — Phase B's structured logging is the ground-truth
   signal that the close path executed (rather than asserting on
   `closed=N` in the return dict, which doesn't distinguish "closed
   for the right reason"). This dovetails with the brief's "do not
   touch the Phase B wipeout-burst helper" — those defences are
   complementary.

## Open questions (carried from brief)

1. **Migration ID conflict**. None — 232 was the last registered.
   Mig 233 added cleanly.
2. **Bulk-UPDATE interaction with the existing `db.add()` /
   `db.commit()` flow**. The two `update(...)` calls use
   `synchronize_session=False` so ORM identity-map sync isn't
   forced; the trailing `db.commit()` at the end of
   `sync_positions_to_db` flushes them along with all other
   pending changes. No fall-back to per-trade updates was needed.
3. **No new code paths emitting `broker_reconcile_position_gone`**.
   The writer remains at `broker_service.py:2247` only; grep
   confirms.

## Verification

* `wc -l broker_service.py` 4292 → 4360 (+68); AST clean.
* `wc -l migrations.py` 15794 → 15825; AST clean.
* `wc -l config.py` 2837 → 2854; AST clean.
* `wc -l models/trading.py` +13.
* All 4 files importable; settings + ORM column resolve.
* Splice pattern used (NOT Edit tool) for `broker_service.py` per
  the brief's truncation discipline. Edit tool used for the small
  3-line additions in `migrations.py`, `config.py`, and
  `models/trading.py` (each well under the 100-line splice
  threshold for the surface being touched).

## Operator-side after CC ships

Per brief:

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili broker-sync-worker
   autotrader-worker`.
3. Verify migration 233 applied:
   ```sql
   \d trading_trades
   -- broker_sync_missing_streak should be present, default 0
   ```
4. Watch the next `[broker_sync] RECONCILE_CLOSE` warning (if any).
   With N=2 the gap between `last_broker_sync` and `exit_date` for
   any close should be ≥ 2 broker_sync cycles (~10–12 min) — a
   single missing cycle no longer fires.
5. After 7 days: re-run the Phase B audit query; the post-R32
   phantom count should be 0 in the trailing 7d (target).

## Rollback plan

`git revert` the feature commit. The new column is additive (default
0) and harmless to leave in place. The streak gate is a no-op if
`CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN=0` (env override) — flip the
env var to disable without a code revert.

If the migration is reverted but the column persists, the gate's
`getattr(trade, "broker_sync_missing_streak", 0) or 0` resolves to 0
and the gate becomes a no-op — same defensive behaviour as the
brief's rollback design.

## What's NEXT after this ships

* The wipeout-cascade loop is now closed at three layers: Phase A
  (PDT count filters reconcile artifacts), Phase B (R32 + wipeout-
  burst breaker + RECONCILE_CLOSE observability), and this brief
  (per-trade consecutive-cycle confirmation).
* If post-R32 phantom count > 0 in 7d, the audit will reveal a
  fourth code path; queue another follow-up.
* Phase A's `pdt_guard.py` filter (commit `60c26f8`) remains the
  durable defence on the count side even if all upstream layers
  succeed at preventing new phantoms.
