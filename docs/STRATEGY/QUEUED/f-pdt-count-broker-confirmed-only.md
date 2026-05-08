# f-pdt-count-broker-confirmed-only

STATUS: QUEUED
SLUG: pdt-count-broker-confirmed-only
PROPOSED: 2026-05-08
SEVERITY: HIGH (system is currently self-locked by 14 ghost day-trades that don't exist at the broker)
SUPERSEDES: `f-pdt-crypto-bypass-cleanup` (this is the cleaner fix; that one becomes a follow-up)

## TL;DR

**`pdt_guard._count_day_trades_5d` is counting reconcile artifacts as real day-trades.** Of the 14 currently-counted trades, all 14 have:
- `exit_reason: 'broker_reconcile_position_gone'`
- `broker_order_id: None` (no actual broker exit order)
- `filled_at: None` and `last_fill_at: None` (never filled at broker)
- Synthetic exit timestamps clustered at exact same second (9 trades all closed at 0:56:01.X on 2026-04-30)

These are NOT day-trades by any FINRA definition. They're chili synthesizing closes when its reconciler couldn't find the position at the broker — the same pattern R31/R32 (commits `539e1c2` + `7af3d49`, 2026-04-30) fixed for the **crypto** book. **The equity book never got the equivalent protection**, and on 2026-04-30 ~00:34-00:56 UTC, 9 equity positions got synthetically wiped out the same way.

Result: operator's account is locked out of new stock entries with `pdt_guard:pdt_limit_reached:14>=3` (or higher when the count was 22). Operator's actual broker-confirmed day-trades in the last 5 business days: **almost certainly zero**.

**Fix is one SQL change** in `_count_day_trades_5d`: only count rows where the exit was actually executed at the broker (broker_order_id + last_fill_at present, exit_reason not a reconcile artifact). This drops the count from 14 to 0 immediately and the autotrader resumes stock entries today.

## Why now

Operator audit 2026-05-08 surfaced this. The previous diagnostic incorrectly labeled the 14 rows as "autotrader rapid-fire round-trips" — they're not. Operator was right: they're ghost data. The system has been self-locked since 2026-04-30 (9 days).

References:
- `app/services/trading/pdt_guard.py:115-160` (the buggy SQL)
- Memory: `reference_r31_r32_auth_flap_cascade.md` (R31/R32 fixed crypto; equity never got the parallel fix)
- `app/services/trading/bracket_reconciliation_service.py` (current logs show ongoing `broker_qty_zero` discrepancies on May-1 re-opens — equity wipeout pattern is still happening, just not as catastrophically)

## Goal

**Phase A — Immediate fix (one SQL change, ~30 min for CC).** Filter the day-trade count to broker-confirmed exits only:

```python
sql = """
    SELECT COUNT(*) AS n
    FROM trading_trades
    WHERE status = 'closed'
      AND entry_date IS NOT NULL
      AND exit_date IS NOT NULL
      AND DATE(entry_date) = DATE(exit_date)
      AND exit_date > :cutoff
      AND ticker NOT LIKE '%-USD'

      -- 2026-05-08: PDT applies only to broker-confirmed round-trips.
      -- Reconcile artifacts (where chili synthesized a close because
      -- it couldn't find the position at the broker) are not day-trades.
      -- See R31/R32 history (crypto wipeout fix); equity book had a
      -- parallel wipeout 2026-04-30 ~00:34-00:56 UTC that wrongly
      -- inflated PDT count by 14.
      AND broker_order_id IS NOT NULL
      AND last_fill_at IS NOT NULL
      AND COALESCE(exit_reason, '') NOT IN (
          'broker_reconcile_position_gone',
          'forced_unwind_reconcile'
      )
"""
```

**Phase B — Architectural protection (separate brief: `f-equity-broker-reconcile-wipeout-protection`).** Apply R31/R32-equivalent guards to the equity reconciler. When `broker.get_positions()` returns empty/incomplete, do NOT synthetically close equity positions; wait for the next sync cycle to confirm. Track this in a follow-up brief.

This brief = Phase A only.

## Acceptance criteria

1. `_count_day_trades_5d` SQL has the four new filter clauses (`broker_order_id IS NOT NULL`, `last_fill_at IS NOT NULL`, `exit_reason NOT IN`, in addition to the existing `ticker NOT LIKE`).
2. Live count drops from 14 → 0 (or whatever the broker-confirmed total is) on the next pdt_guard call.
3. Autotrader stock entries resume firing on the next `pattern_breakout_imminent` alert that wouldn't otherwise be blocked by `projected_profit_below_min`.
4. Existing `pdt_guard` tests still pass.
5. New helper test `tests/test_pdt_guard_excludes_reconcile_artifacts.py` (4 tests):
   - Row with `broker_reconcile_position_gone` is NOT counted.
   - Row with `broker_order_id IS NULL` is NOT counted.
   - Row with `last_fill_at IS NULL` is NOT counted.
   - Row with broker-confirmed exit IS counted (control).
6. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-pdt-count-broker-confirmed-only.md`.

## Brain integration (reuse, don't rewrite)

- Existing `_count_day_trades_5d` function shape stays; just SQL body updates.
- Existing `pdt_guard` test fixtures.
- The `trading_trades.broker_order_id` and `last_fill_at` columns already exist (verified 2026-05-08).
- No migration needed.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **The crypto bypass logic stays** — `ticker NOT LIKE '%-USD'` filter is still correct (cleaner version queued separately as `f-pdt-crypto-bypass-cleanup`). The broker-confirmed filter is INDEPENDENT of and ADDITIVE to crypto bypass.
- **Edit-tool truncation discipline (HARD).** Splice pattern only for `pdt_guard.py`. `wc -l + ast.parse` post-edit verification.
- **Tests use `_test`-suffixed DB.**
- **No magic numbers** (no new constants).
- **Don't touch the 14 ghost rows.** Leave them in the DB as historical data; the SQL filter excludes them from the count. Backfilling exit_reason is a different concern.

## Out of scope (separate briefs)

- **`f-equity-broker-reconcile-wipeout-protection`** (Phase B, follow-up). Equity-book version of R31/R32. When `get_positions()` returns empty/incomplete, don't synthesize closes. Bigger scope; this brief is the immediate unblock.
- **`f-pdt-crypto-bypass-cleanup`** (already queued). Make crypto bypass explicit + equity-tier-aware. Not load-bearing for the immediate stock-entry resumption.
- **`f-autotrader-pdt-aware-exit-deferral`** (already queued). Defer same-day stock closes when count would breach. Becomes less urgent once the count is correctly counting only real broker-confirmed round-trips, but still useful for future PDT-aware exit logic.
- **`f-pattern-demote-on-thin-evidence`** (already queued). Pattern 585 demotion. Independent of PDT.
- **Backfilling exit_reason on the 14 ghost rows.** They're correctly recorded as reconcile artifacts; no need to "fix" them.

## Sequencing

1. Truncation scan.
2. Splice-rewrite `_count_day_trades_5d` SQL with the four new filter clauses.
3. Add the test file + 4 tests.
4. Run helper tests (DB-bound deferred per established pattern).
5. Verify in scheduler-worker that the live count drops post-deploy.
6. Commit + push.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker`.
3. Verify the count:
   ```powershell
   docker exec chili-home-copilot-chili-1 python -c "from app.services.trading.pdt_guard import _count_day_trades_5d; from app.db import SessionLocal; db=SessionLocal(); print('count:', _count_day_trades_5d(db, user_id=1)); db.close()"
   ```
   Expected: `count: 0` (or however many real broker-confirmed day-trades exist; should be a small number).
4. Watch the next `pattern_breakout_imminent` alert; the autotrader's PDT block should be GONE for stock entries.
5. Stock trades resume per pattern-quality availability (separate `f-pattern-demote-on-thin-evidence` issue determines which patterns fire).

## Rollback plan

`git revert` the commit. The four new filter clauses are SQL-additive (more `AND` clauses); revert restores prior count behavior. Worst case the count goes back to 14 and stock entries lock out again — but that's the current state anyway, no regression risk.

## Why this supersedes the more elaborate brief

The previous proposal (`f-pdt-crypto-bypass-cleanup`) tried to make the bypass cleaner and more explicit. That's still useful, but **it doesn't unblock the operator today**. The operator's stock-entry drought is caused by a counting bug, not by a crypto/equity policy ambiguity. This brief fixes the actual bug. The cleanup brief stays queued as a follow-up.

## Open questions

1. **Backfill the 14 ghost rows?** Could mark them with a `pdt_excluded=TRUE` flag for forensics or update `exit_reason` to be more descriptive. Not necessary — the SQL filter excludes them via `exit_reason NOT IN (...)` and the missing `broker_order_id`. Leave them as-is for historical record.
2. **Are there OTHER reconcile-artifact reasons we should exclude?** The current proposal lists `'broker_reconcile_position_gone'` and `'forced_unwind_reconcile'`. Grep `app/services/` for other strings that match the pattern (synthetic close due to broker reconcile). Surface findings in CC report.
3. **What about `pattern_exit_now` rows that DID get filled at broker?** Those have `broker_order_id`, `last_fill_at`, and a real exit_reason — they pass the filter and ARE counted as day-trades (correct). Verify against the 2 visible `pattern_exit_now` rows in section C of the audit (trades 1777, 1778, 1776, 1779). Those should still count if same-day. After deploy, verify the count is 4 (the broker-confirmed ones), not 0.

   Actually wait — looking at the audit more carefully:
   - Trade 1777 AB: filled_at=18:00:20, exit_date=18:05:46, exit_reason='pattern_exit_now', broker_order_id present → SAME-DAY day-trade, real, counts.
   - Trade 1776 COF: similar → counts.
   - Trade 1778 AB and 1779 COF: have `exit_reason='broker_reconcile_position_gone'` → DON'T count.

   So real PDT count after fix: probably 2-4 (the legit pattern_exit_now ones), still well below the 3-trade-in-5-day threshold. Operator stays unlocked.
