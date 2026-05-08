# NEXT_TASK: f-pdt-count-broker-confirmed-only

STATUS: DONE

## Goal

Patch `pdt_guard._count_day_trades_5d` so it counts ONLY broker-confirmed
day-trades — not reconcile artifacts. Drops the live PDT count from 14 to
~0–4 immediately on deploy. Stock entries resume on the next
`pattern_breakout_imminent` alert (pattern-quality permitting).

The full brief is at `docs/STRATEGY/QUEUED/f-pdt-count-broker-confirmed-only.md`
— read it first.

## Why now

Operator audit 2026-05-08 caught my earlier diagnosis (autotrader rapid-fire
round-trips) as wrong. Row-detail re-pull on the 14 PDT-counted trades:

- All 14 have `exit_reason = 'broker_reconcile_position_gone'`
- All 14 have `broker_order_id IS NULL`
- All 14 have `filled_at IS NULL` and `last_fill_at IS NULL`
- 9 of them "exited" at exact same second `00:56:01` on 2026-04-30
- Broker now reports `qty=0` for the May-1 re-opens of the same tickers
  (visible in `bracket_reconciliation_service` ongoing logs)

These are NOT day-trades by any FINRA definition. They're chili synthesizing
closes when its reconciler couldn't find positions at the broker — the same
wipeout pattern that R31/R32 (commits `539e1c2` + `7af3d49`, 2026-04-30)
fixed for the **crypto** book. The equity book never got the parallel fix,
so the operator's account self-locked from these phantom rows.

## The change

Single SQL change to the day-trade count query. Add three exclusions:

```sql
WHERE status = 'closed'
  AND DATE(entry_date) = DATE(exit_date)
  AND exit_date > :cutoff
  AND ticker NOT LIKE '%-USD'                  -- existing crypto bypass (R35)
  AND broker_order_id IS NOT NULL              -- NEW: exit was a real broker order
  AND last_fill_at IS NOT NULL                 -- NEW: broker actually filled it
  AND COALESCE(exit_reason, '') NOT IN (       -- NEW: exclude reconcile artifacts
      'broker_reconcile_position_gone',
      'forced_unwind_reconcile'
  )
```

This is the shortest path to unblock stock entries today.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/pdt_guard.py:115-160` — `_count_day_trades_5d`. The
  only function that needs editing.
- Existing `can_open_intraday_round_trip` short-circuits stay as-is.
- Existing R35 crypto bypass (ticker pattern) stays as-is.
- The migration framework is NOT needed — this is a SQL filter change in
  Python, no schema delta.

## Acceptance criteria

1. `_count_day_trades_5d` SQL adds the three new clauses (broker_order_id,
   last_fill_at, exit_reason NOT IN reconcile-set).
2. The three exclusion strings (`'broker_reconcile_position_gone'`,
   `'forced_unwind_reconcile'`) are module-level constants with a docstring
   pointing back to this brief and to R31/R32.
3. New helper-level tests in `tests/test_pdt_count_broker_confirmed_only.py`
   pinning each exclusion path:
   - Trade with `broker_order_id=NULL` → not counted.
   - Trade with `last_fill_at=NULL` → not counted.
   - Trade with `exit_reason='broker_reconcile_position_gone'` → not counted.
   - Trade with `exit_reason='forced_unwind_reconcile'` → not counted.
   - Real broker-confirmed same-day round-trip → still counted.
4. Existing `pdt_guard` tests still pass.
5. Live verification (post-deploy): `_count_day_trades_5d` returns ≤ 4 for
   the operator's account (was 14). Capture the value in the CC report.
6. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-08_f-pdt-count-broker-confirmed-only.md`.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. Real day-trades
  still count. Threshold stays 3-in-5 for sub-$25k accounts.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Edit-tool truncation discipline (HARD).** Six rounds yesterday across
  the codebase. Splice pattern only. `wc -l + ast.parse` post-edit
  verification mandatory. See memory `reference_2026_05_07_widespread_truncation.md`.
- **Tests use `_test`-suffixed DB.**
- **No magic strings**: the two reconcile exit-reasons go into a module-level
  constant `_RECONCILE_ARTIFACT_EXIT_REASONS = frozenset({...})`.
- **No changes outside `pdt_guard.py` + the new test file.** Particularly:
  do NOT touch `auto_trader.py`, `auto_trader_monitor.py`, `bracket_*.py`,
  or anything related to the equity reconciler in this brief — those are
  the Phase B follow-up brief (`f-equity-broker-reconcile-wipeout-protection`).

## Out of scope

- The crypto bypass cleanup (separate brief: `f-pdt-crypto-bypass-cleanup`).
  That now becomes a follow-up; this brief unblocks the operator first.
- The PDT-aware exit deferral (separate brief:
  `f-autotrader-pdt-aware-exit-deferral`). That's a structural fix for a
  different problem (real day-trades from the autotrader); it's not the
  current blocker.
- The pattern-demote-on-thin-evidence work (separate brief:
  `f-pattern-demote-on-thin-evidence`).
- Equity reconciler R31/R32 parallel (separate Phase B brief, to be written
  after this ships).
- Backfilling old phantom rows. They'll roll out of the 5-day window
  naturally; the SQL filter handles them while they're in the window.

## Sequencing

1. Truncation scan on `app/services/trading/pdt_guard.py` (read full file +
   `wc -l` + `ast.parse`).
2. Splice-edit: add the constant + the three SQL clauses.
3. Post-edit: `wc -l + ast.parse` verify.
4. Add tests in `tests/test_pdt_count_broker_confirmed_only.py`.
5. Run `pytest tests/test_pdt_count_broker_confirmed_only.py -v` — must pass.
6. Run existing `pdt_guard` tests if any — must still pass.
7. Commit with message referencing the slug + R31/R32 commits.
8. Write CC report.
9. Mark this NEXT_TASK as `STATUS: DONE`.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker`
3. Verify the live count:
   ```bash
   docker exec chili-home-copilot-chili-1 python -c "
   from app.db.session import SessionLocal
   from app.services.trading.pdt_guard import _count_day_trades_5d
   db = SessionLocal()
   print('day-trade count:', _count_day_trades_5d(db, '<operator_user_id>'))
   "
   ```
   Expected: ≤ 4 (was 14).
4. Watch for the next stock `pattern_breakout_imminent` alert; verify
   autotrader no longer rejects with `pdt_limit_reached`.

## Rollback plan

`git revert` the commit. The change is purely additive SQL filters; revert
restores prior behavior bit-identically. The new test file is removed by
the revert, so existing tests stay green.

## Open questions

1. **What about old-phantom forensic value?** The phantom rows with
   `broker_reconcile_position_gone` are diagnostically valuable — they tell
   us the equity reconciler had a wipeout event. Do NOT delete them; just
   exclude from the PDT count. They'll surface again when we write the
   Phase B equity-reconciler-protection brief.
2. **`last_fill_at` vs `filled_at`.** The audit shows BOTH are NULL on the
   phantom rows. Use `last_fill_at` (broker-truth column) for the filter;
   `filled_at` is the older entry-side timestamp and may be set on
   non-fill paths.
3. **Audit-counter alignment.** If there's an external metric or dashboard
   that surfaces the day-trade count separately from this function, surface
   it in the CC report so the operator can sanity-check. Don't fix it in
   this brief.

## What CC should do if it's unsure

Flag in the CC report and ask. Specifically: if the SQL change ends up
touching more than `_count_day_trades_5d`, stop and surface — the brief
expects a single-function edit.
