# f-autotrader-pdt-aware-exit-deferral

STATUS: QUEUED
SLUG: autotrader-pdt-aware-exit-deferral
PROPOSED: 2026-05-08
SEVERITY: high (autotrader self-locks the account out of stock entries via rapid same-day round-trips)

## TL;DR

Operator audit 2026-05-08 found 14 day-trades on the books, all from autotrader rapid-fire round-trips on Apr 29 + Apr 30 (some held <10 min). Autotrader's exit logic doesn't check whether closing a position same-day would push the day-trade count above the PDT threshold for sub-$25k accounts. Result: the system locks itself out of stock entries every time it does a couple sessions of rapid round-trips. **Add PDT-aware exit deferral**: refuse to close a stock position same-day if doing so would breach the count, except for critical-loss exits.

## Why now

Operator's account is at $12k+ equity (sub-$25k tier → PDT applies, 3-day-trades-in-5 days threshold). Autotrader did 14 same-day round-trips on Apr 29-30; PDT counter at 22 immediately after, dropping to 14 today as the window rolls. **Every stock entry since has been blocked** because the autotrader created its own ceiling.

The fix: before exiting a stock position that was opened today, check the current day-trade count + the PDT threshold. If `current_count + 1 >= threshold` AND the exit isn't a critical-loss trigger (stop-out, drawdown breaker, kill-switch), defer the exit to next session (overnight hold).

Reference:
- `app/services/trading/auto_trader_monitor.py` (exit decision lane)
- `app/services/trading/pdt_guard.py:115-160` (count function)
- Memory: `reference_diagnostic_bridge.md` (autotrader exit lanes)

## Goal

1. **New `pdt_safe_to_close_intraday` gate in pdt_guard.py.**
   - Inputs: `ticker`, `entry_date`, `account_equity_usd`, `db`.
   - Returns: `PdtCloseGateResult(allowed: bool, reason: str, detail: dict)`.
   - Logic:
     - If `asset_kind != 'stock'` → allow (crypto / options outside scope).
     - If `account_equity_usd >= 25000` → allow (no PDT).
     - If `entry_date.date() != today.date()` → allow (already overnight; not a day-trade).
     - If `(current_day_trade_count + 1) < pdt_threshold` → allow.
     - Else → DEFER with reason `'pdt_would_breach'`.

2. **Wire into autotrader_monitor's exit decision pipeline.**
   - When the monitor decides to exit a stock position, call `pdt_safe_to_close_intraday` first.
   - If DEFER:
     - Log the deferral with reason + ticker + position_id.
     - Set a flag on the trade row (e.g., `pending_exit_deferred_pdt = TRUE`) to prevent re-attempting until the next trading day.
     - Skip the broker order placement.
     - Pattern-monitor logic (the LLM-based exit reason) continues to run; only the *placement* is deferred.

3. **Critical-loss override.**
   - If exit reason is one of: `stop_loss_hit`, `drawdown_breaker_trip`, `kill_switch_active`, `forced_unwind` — bypass the PDT check.
   - Rationale: operator's risk management trumps PDT lockout. Better to take the PDT hit than blow up.
   - Add a settings flag `CHILI_AUTOTRADER_PDT_OVERRIDE_ON_CRITICAL_LOSS=1` (default on) so operator can flip if their judgment differs.

4. **Reset the deferral on next trading day.**
   - A scheduled job (or per-tick check in autotrader_monitor) clears `pending_exit_deferred_pdt` flags when the entry_date is no longer "today" — i.e., the position has rolled overnight and is now eligible for non-day-trade close.
   - Resumes normal exit flow at next-session open.

5. **Tests.**
   - `tests/test_pdt_safe_to_close_intraday.py`:
     - Crypto / options → always allow.
     - Equity ≥ $25k → always allow.
     - Position opened yesterday → always allow.
     - Same-day position, count would breach → defer.
     - Same-day position, count would NOT breach → allow.
     - Critical-loss reasons override the defer.
     - Setting flag flipped off → critical-loss reasons no longer override.

## Acceptance criteria

1. `pdt_safe_to_close_intraday` exists in `pdt_guard.py` with the documented signature.
2. `auto_trader_monitor.py` calls it before placing exit orders for stocks.
3. Deferred exits write to `trading_trades.pending_exit_deferred_pdt` (new column via mig 233).
4. Next-day reset clears the flag.
5. Critical-loss reasons bypass via the settings flag.
6. New tests pass; existing autotrader tests still pass.
7. Live verification: simulate 22 day-trades on the books, then trigger an exit on a same-day stock position. Verify it defers (autotrader_runs row with reason `pdt_would_breach`).
8. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-autotrader-pdt-aware-exit-deferral.md`.

## Brain integration (reuse, don't rewrite)

- `pdt_guard._count_day_trades_5d` — reuse for the count.
- `auto_trader_monitor`'s existing exit decision pipeline — add the new gate as one more pre-flight check.
- `trading_trades.pending_exit_*` columns already exist (`pending_exit_order_id`, `pending_exit_status`, `pending_exit_requested_at`, `pending_exit_reason`, `pending_exit_limit_price`). Add `pending_exit_deferred_pdt BOOLEAN` next to them.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Critical-loss path stays critical**: stops and breakers always exit; the PDT check defers, never blocks, those cases.
- **Migration ID**: 233 next free.
- **Tests use `_test`-suffixed DB.**
- **Edit-tool truncation discipline (HARD).** Splice pattern for `auto_trader_monitor.py` (large file).
- **No magic numbers**: PDT threshold (3) and equity floor ($25k) come from `pdt_guard` constants, not new ones in autotrader.

## Out of scope

- Crypto PDT bypass cleanup (separate brief: `f-pdt-crypto-bypass-cleanup`).
- Pattern quality demotion (separate brief: `f-pattern-demote-on-thin-evidence`).
- Adjusting the autotrader's entry pace to avoid the build-up in the first place.
- Same-day exit deferral for options (different rules; surface as open question).
- Notifying the operator via UI/email when an exit gets deferred.

## Sequencing

1. Truncation scan.
2. Mig 233 adding `trading_trades.pending_exit_deferred_pdt` column (idempotent ADD COLUMN IF NOT EXISTS).
3. `pdt_guard.pdt_safe_to_close_intraday` helper + tests.
4. `auto_trader_monitor` exit pipeline integration + tests.
5. Reset-on-next-day scheduler hook.
6. Commit + push.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker scheduler-worker`.
3. Verify mig 233 applied.
4. Watch the next stock position for deferral behavior. Should defer same-day if PDT count is at threshold.
5. Verify next-day reset clears the flag and the position closes normally.

## Rollback plan

`git revert` the commit. Mig 233 is additive (NEW column with default NULL); the rollback simply ignores the column (rows are unaffected). The new gate is a no-op if the autotrader doesn't call it.

## Open questions

1. **Options day-trade rules**. Are option round-trips counted? PDT rules technically include them. Today's `pdt_guard._count_day_trades_5d` doesn't filter by asset_kind so options ARE counted; the new PDT-crypto-cleanup brief surfaces this as an open question. Confirm with operator before this brief ships whether the same-day-defer logic should apply to options.
2. **Drawdown breaker integration.** When the breaker trips, the autotrader force-unwinds. PDT check should not apply to breaker-triggered closes. The critical-loss override list covers this; verify the breaker's exit-reason string matches `forced_unwind` exactly.
3. **Position size threshold.** Should very small positions (e.g., $25 paper notional from fast-path) be exempt from PDT-aware deferral? They're not consequential to the operator's PDT bucket but each one bumps the count. Surface the trade-off; default to "yes count them" since broker doesn't care about notional.
