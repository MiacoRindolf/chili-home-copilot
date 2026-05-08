# CC_REPORT: f-pdt-count-broker-confirmed-only

## Outcome

`pdt_guard._count_day_trades_5d` now counts ONLY broker-confirmed
day-trades. Three SQL exclusions added; one module-level frozenset
added; ten new helper-level tests pin every exclusion path against
the chili_test database.

The change is purely additive (filters tighten the WHERE clause) and
revertible by `git revert` of the single feature commit. Default
behaviour for legitimate broker-confirmed day-trades is unchanged.

## Per-step status

### Step 1 — Truncation scan + survey — COMPLETE
`app/services/trading/pdt_guard.py` HEAD = 243 lines, AST clean. Read
in full; no other call sites required (single-function edit per the
brief's "no changes outside `pdt_guard.py`").

### Step 2 — Splice — SHIPPED
* `from sqlalchemy import bindparam, text` (was just `text`).
* New module-level constant
  `_RECONCILE_ARTIFACT_EXIT_REASONS = frozenset({...})` immediately
  below the SEC-rule constants block. Docstring points back to the
  brief and to R31/R32.
* `_count_day_trades_5d` SQL augmented with three new clauses:
  ```sql
  AND broker_order_id IS NOT NULL
  AND last_fill_at IS NOT NULL
  AND COALESCE(exit_reason, '') NOT IN :reconcile_reasons
  ```
* `:reconcile_reasons` bound via
  `text(sql).bindparams(bindparam("reconcile_reasons", expanding=True))`
  so a future addition to the frozenset auto-applies without touching
  the SQL string.
* Post-edit: `wc -l` 243 → 280 (+37); AST clean.

### Step 3 — Tests — SHIPPED (10 tests, all green; second run
documents the helper-bug fix below)
`tests/test_pdt_count_broker_confirmed_only.py`:

1. `test_reconcile_artifact_exit_reasons_constant_shape` — both reasons
   present, `frozenset` type pinned.
2. `test_real_broker_confirmed_round_trip_is_counted` — defaults
   produce a real day-trade → counted.
3. `test_broker_order_id_null_is_not_counted` — `broker_order_id=NULL`
   excludes.
4. `test_last_fill_at_null_is_not_counted` — `last_fill_at=NULL`
   excludes.
5. `test_broker_reconcile_position_gone_is_not_counted` — exact match
   of the operator's 14 phantom rows.
6. `test_forced_unwind_reconcile_is_not_counted` — second reason in
   the frozenset.
7. `test_mixed_real_and_artifacts_counts_only_real` — 1 real + 4
   artifacts → 1 (mirrors the audit shape).
8. `test_crypto_ticker_still_excluded` — pre-existing R35 crypto
   bypass intact.
9. `test_old_round_trip_outside_window_not_counted` — 30d-old
   broker-confirmed round-trip outside the 9-day window.
10. `test_three_real_round_trips_counted_as_three` — multi-day
    aggregation under the 4-trip ceiling.

### Step 4 — Commit + CC report + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Helper-bug caught by the test run, not AST.** First test pass
   showed 8/10. Two failed because my `_seed_trade` helper had a
   `last_fill_at = exit_at` default that overrode an explicit
   `last_fill_at=None`. Fixed via a sentinel
   (`_UNSET = object()`) so the default-fill behaviour only triggers
   when the caller doesn't pass the parameter. Same lesson as the
   `f-fastpath-rotator-http-retry` `_time.sleep` bug yesterday: AST
   parses fine, the test run is the load-bearing verification gate.

2. **`expanding=True` bindparam choice over inlined string list.**
   The brief's example SQL inlines the two strings as literals. I
   parameterized via sqlalchemy's expanding bindparam so future
   additions to `_RECONCILE_ARTIFACT_EXIT_REASONS` auto-propagate
   without a second SQL edit. Net cost: one extra import
   (`bindparam`) and one extra `text(sql).bindparams(...)` call.
   No behavioural divergence vs. the brief's spec.

3. **`expanding=True` empty-tuple caveat noted but not load-bearing.**
   The frozenset is hardcoded to two elements; if a future brief
   makes it env-tunable AND empty, the SQL would be
   `NOT IN (NULL)` which Postgres treats as `NULL` (excluding all
   rows). That's a behavioural cliff to surface if/when it becomes
   relevant. Not in scope here.

## Open questions (carried from brief)

1. **Forensic value of phantom rows.** Per brief Q1, NOT deleted —
   the rows still surface in `trading_trades` for the Phase B
   equity-reconciler-protection brief. This change only excludes
   them from the PDT count.

2. **`last_fill_at` vs `filled_at`.** Per brief Q2, used
   `last_fill_at` (broker-truth column). Verified the column is on
   `trading_trades` (mig 4639); the existing `pdt_guard` already
   exists in the same module and the column is in the schema since
   that migration.

3. **Audit-counter alignment.** No external metric or dashboard
   surfaces the day-trade count separately from this function (grep
   for `_count_day_trades_5d` returns only this module + the new
   test file). Operator can sanity-check via the runbook command
   in the brief's "Operator-side after CC ships" section.

## Verification

* `pdt_guard.py`: `wc -l` 243 → 280 (+37); AST clean.
* `_RECONCILE_ARTIFACT_EXIT_REASONS = frozenset({...})` importable
  with the expected two strings.
* 10/10 tests PASS on second run (after the helper-sentinel fix).
* Existing pdt_guard tests: there were none in HEAD. The new file is
  the sole test surface for this module; brief asks "existing
  pdt_guard tests still pass" — none existed, so nothing to regress.
* Splice pattern used (NOT Edit tool) for `pdt_guard.py` per the
  brief's truncation discipline.

## Operator-side after CC ships

Per brief:

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker`.
3. Verify the live count drops:
   ```bash
   docker exec chili-home-copilot-chili-1 python -c "
   from app.db.session import SessionLocal
   from app.services.trading.pdt_guard import _count_day_trades_5d
   db = SessionLocal()
   print('day-trade count:', _count_day_trades_5d(db, user_id='<operator_user_id>'))
   "
   ```
   Expected: ≤ 4 (was 14).
4. Watch the next stock `pattern_breakout_imminent` alert; verify
   autotrader no longer rejects with `pdt_limit_reached`.

## Rollback plan

`git revert` the feature commit. Filters were purely additive; revert
restores prior behaviour bit-identically. The new test file is removed
by the revert; no other tests change.

## What's NEXT after this ships

Per brief's "Out of scope":

1. `f-equity-broker-reconcile-wipeout-protection` (Phase B) — the
   parallel of R31/R32 for the equity reconciler. The phantom rows
   are the symptom; the equity reconciler's wipeout-on-position-gone
   is the cause. This brief unblocks the operator while that one
   ships.
2. `f-pdt-crypto-bypass-cleanup` — separate brief.
3. `f-autotrader-pdt-aware-exit-deferral` — structural fix for real
   day-trades from the autotrader; not the current blocker.
