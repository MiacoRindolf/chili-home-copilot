# Drawdown breaker runbook

**Hard Rule 2 (CLAUDE.md):** the drawdown breaker gates sizing — if it trips, trades are blocked until manual reset. It is a narrower tool than the kill switch: the breaker fires automatically on loss-pattern thresholds, while the kill switch is an explicit freeze.

Use this runbook when:

- `[circuit_breaker] TRIPPED: <reason>` appears in the app log
- The autopilot badge shows "DRAWDOWN BREAKER TRIPPED"
- A reconciliation pass finds open P&L that breaches the configured thresholds
- You need to verify or reset the breaker during an incident

## What trips the breaker

Implemented in `app/services/trading/portfolio_risk.py::check_drawdown_breaker`:

1. **Mark-to-market unrealized drawdown** exceeds the 30-day limit (`limits.max_30day_dd_pct`). Trips immediately.
2. **5-day rolling P&L** (realized + unrealized) exceeds `limits.max_5day_dd_pct`.
3. **30-day rolling P&L** (realized + unrealized) exceeds `limits.max_30day_dd_pct`.
4. **Consecutive losing trades** ≥ `limits.max_consecutive_losses` most-recent closed trades.

Limits come from `get_drawdown_limits(db=db)` in the same module; a regime-aware multiplier pulls them tighter in `risk_off` and loosens them in `risk_on` (Phase 2 of the risk-dial work).

On trip, state is persisted via `_persist_breaker_state(True, reason)` to `trading_risk_state` (same table as the kill switch; different `regime` value). It survives restart.

## TL;DR — check & reset

```bash
# Status
conda run -n chili-env python -c "from app.services.trading.portfolio_risk import get_breaker_status; print(get_breaker_status())"

# Reset (after sign-off)
conda run -n chili-env python -c "from app.services.trading.portfolio_risk import reset_breaker; reset_breaker()"
```

`reset_breaker()` clears the in-process flag, writes `breaker_tripped=False` to `trading_risk_state`, and logs `[circuit_breaker] Manually reset` at INFO.

## Incident procedure

1. **Do not reset on reflex.** The breaker trips because the thresholds agreed with you that today is a bad day. Resetting to unblock trading re-exposes you to the same regime — confirm the root cause first.

2. **Pull the reason:** `get_breaker_status()` returns a dict like `{'tripped': True, 'reason': '5-day drawdown -7.3% (realized=-450, unrealized=-280) exceeds -6.0% limit'}`. The reason text identifies which branch of the check fired.

3. **Verify the P&L numbers are real.** TCA slippage bugs have historically caused phantom drawdowns — see `services/trading/execution_audit.py::record_execution_event` + `tca_service.py`. If realized P&L is off, do NOT reset; investigate the mis-priced fill first.

4. **If real, pause.** Leave the breaker tripped. Review exposure on the affected positions. Decide: close, hedge, or accept.

5. **Only reset after you have:**
   - Confirmed the thresholds were correct (or updated them intentionally via `DrawdownLimits`)
   - Documented the decision in the incident log
   - Confirmed the kill switch is not also active (see `KILL_SWITCH_RUNBOOK.md`)

6. **After reset**, run `check_drawdown_breaker(db, user_id, capital=...)` once manually to confirm it does not immediately re-trip:

   ```bash
   conda run -n chili-env python -c "
   from app.db import SessionLocal
   from app.services.trading.portfolio_risk import check_drawdown_breaker
   with SessionLocal() as s:
       tripped, reason = check_drawdown_breaker(s, user_id=None, capital=100000.0)
       print('tripped=', tripped, 'reason=', reason)
   "
   ```

   If it trips again immediately, the underlying P&L is still beyond limits. Do not loop reset/trip — escalate.

## Audit trail

Like the kill switch, every breaker state change writes to `trading_risk_state`. Pull the last 24h:

```sql
SELECT created_at, breaker_tripped, breaker_reason, regime
FROM trading_risk_state
WHERE regime IN ('default', 'risk_off', 'risk_on')
ORDER BY created_at DESC
LIMIT 50;
```

## Known interactions

- **Kill switch** (`KILL_SWITCH_RUNBOOK.md`) — if both are active, the kill switch wins at the gate level; resetting the breaker alone will not resume trading.
- **Risk dial** — the regime multiplier on `DrawdownLimits` means a `risk_off` classification can trip the breaker at a smaller loss than `default`. Flipping dial state does not by itself reset an already-tripped breaker.
- **Auto-trader** — `auto_trader.py::_process_one_alert` consults the breaker inline before sizing. A tripped breaker returns a `decision=blocked` audit row in `trading_autotrader_runs`.

## If reset fails

1. DB write blocked — inspect `app.log` for `[circuit_breaker] Failed to persist breaker state` at DEBUG level (it is intentionally swallowed). Check DB connectivity + migration state.
2. Flag bounces back to tripped seconds after reset — the check ran and found the P&L still breached. See step 6 above; do not loop.
3. If you suspect the `trading_risk_state` row is stuck, you can override directly:

   ```sql
   INSERT INTO trading_risk_state (user_id, snapshot_date, breaker_tripped, breaker_reason, regime, capital)
   VALUES (NULL, NOW(), FALSE, 'manual reset via SQL — see incident #', 'default', 0);
   ```

   Then `reset_breaker()` in Python to clear the in-process cache. Log this in the incident record.
