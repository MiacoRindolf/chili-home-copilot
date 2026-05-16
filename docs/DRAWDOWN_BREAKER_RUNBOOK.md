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

## Two-tier architecture (2026-05-16, f-portfolio-vs-pattern-breaker-separation)

CHILI ships **two** drawdown breakers operating at different decision boundaries against independent distributions. They coexist; neither replaces the other.

### Tier 1 — pattern breaker (CHILI-attributed)

- **Decision boundary it gates:** the autotrader's pattern-decision path. Consulted by `auto_trader.py::_process_one_alert` before sizing a CHILI-attributed entry.
- **Distribution:** trailing 180-day **CHILI-attributed** closed-trade history (`scan_pattern_id IS NOT NULL AND != -1`). Numerator and threshold both drawn from this same population — the symmetry that `f-monthly-dd-breaker-numerator-symmetrize` (commits `fdfe15d`, `3e3253b`) restored.
- **Settings:** `chili_pattern_dd_breaker_enabled` (renamed from `chili_monthly_dd_breaker_enabled`; legacy env var honored for one release via `AliasChoices`), `chili_pattern_dd_breaker_lower_bound_sigmas` (default 2.0σ).
- **Persistence:** `trading_risk_state` rows with `regime IN ('default','risk_off','risk_on')` and `breaker_reason` containing `monthly_dd_breaker:`. (The `regime` tag stays unchanged for SQL-parser backward compat.)
- **In-process flag:** `_breaker_tripped` (module-global in `portfolio_risk.py`). Cleared by `reset_breaker()`.

### Tier 2 — portfolio breaker (account-wide)

- **Decision boundary it gates:** the **venue-adapter entry boundary**. Consulted by `_assert_portfolio_breaker_ok()` from inside `coinbase_spot.place_market_order` / `place_limit_order_gtc` / `place_stop_limit_order_gtc` and `robinhood_spot.place_market_order` / `place_limit_order_gtc` — every BUY entry regardless of source (autotrader, broker-sync reconcile, manual, no_pattern). SELL / cancel / preview paths are intentionally NOT gated; portfolio preservation is an entry-side lever.
- **Distribution:** trailing 180-day **all-closed** trades — attributed, no_pattern, manual, reconcile-inferred, every row that touches buying power. Numerator and threshold both drawn from the same all-closed population.
- **Settings:**
  - `chili_portfolio_dd_breaker_enabled` (default OFF). When True, the breaker is **active**.
  - `chili_portfolio_dd_breaker_live` (default OFF). When False AND enabled=True, the breaker is in **shadow mode** — computes the would-have-tripped decision, persists a shadow row, logs an INFO line, but does NOT block entries. The 7-day soak path operators run before flipping the live flag.
  - `chili_portfolio_dd_breaker_lower_bound_sigmas` (default 2.0σ; tunable independently of the pattern tier because the all-closed distribution is wider).
  - `chili_portfolio_dd_breaker_shadow_log_enabled` (default ON). Silences shadow-log emission if the daily volume becomes noisy.
- **Persistence:** `trading_risk_state` rows with `regime='portfolio_breaker'` (live trips) or `regime='portfolio_breaker_shadow'` (would-have-tripped in shadow mode; `breaker_reason` prefixed `SHADOW:`). No new table, no migration.
- **In-process flag:** none. Each adapter call re-checks via `_assert_portfolio_breaker_ok()`, which opens its own short-lived `SessionLocal` and fails OPEN on DB/exception. The breaker is a safety belt that goes blind rather than become a denial-of-service surface.

### The principle the two tiers encode

Each tier gates the decision boundary it can act on against a distribution drawn from the population its lever sees:

- **Pattern tier:** lever halts CHILI-attributed strategy decisions; distribution is CHILI-attributed closed history. Coherent.
- **Portfolio tier:** lever halts EVERY entry path at the venue boundary; distribution is all-closed history. Coherent.

Conflating them led to the original asymmetry the 2026-05-15 quant audit caught: a pattern-attributed numerator was being compared to an all-closed threshold, so a no_pattern bleed could trip the pattern breaker on losses the pattern breaker could not act on. Two tiers eliminates that open loop.

### How to read each state

```sql
-- Pattern tier (live trips + heartbeat):
SELECT created_at, breaker_tripped, breaker_reason, regime
FROM trading_risk_state
WHERE regime IN ('default','risk_off','risk_on','breaker_heartbeat')
  AND breaker_reason LIKE 'monthly_dd_breaker%'
ORDER BY created_at DESC LIMIT 50;

-- Portfolio tier (shadow + live):
SELECT created_at, regime, breaker_tripped, breaker_reason
FROM trading_risk_state
WHERE regime LIKE 'portfolio_breaker%'
ORDER BY created_at DESC LIMIT 50;

-- Operator counts "would-have-tripped" days during the 7-day soak:
SELECT DATE_TRUNC('day', created_at) AS d, COUNT(*) AS would_have_tripped_events
FROM trading_risk_state
WHERE regime = 'portfolio_breaker_shadow'
  AND breaker_reason LIKE 'SHADOW:%'
GROUP BY 1 ORDER BY 1 DESC;
```

### Resetting the portfolio tier (absorbing-state caveat)

This is the most operationally consequential failure mode of the live mode and the reason the 7-day shadow soak exists.

> **Absorbing state under sustained bleed.** Once the portfolio breaker trips, the threshold doesn't recover until the trailing-30-day sum recovers. The breaker is a safety belt, not an auto-recovering throttle. If the cumulative bleed stays below the threshold for many days, the breaker stays tripped — all BUY entries halted permanently — until the operator either (a) waits for older losing days to roll out of the trailing-30d window or (b) manually resets.

Manual reset procedure (use deliberately — the breaker tripped because the all-closed distribution agreed the account is shrinking):

```bash
# 1. Inspect the most recent trip.
conda run -n chili-env python -c "
from sqlalchemy import text
from app.db import SessionLocal
with SessionLocal() as s:
    row = s.execute(text('''
        SELECT created_at, breaker_tripped, breaker_reason
        FROM trading_risk_state
        WHERE regime = 'portfolio_breaker'
        ORDER BY created_at DESC LIMIT 1
    ''')).fetchone()
    print(row)
"

# 2. Confirm the numerator/threshold to know how far below the line you are.
conda run -n chili-env python -c "
from app.db import SessionLocal
from app.services.trading.portfolio_risk import (
    _portfolio_dd_threshold, _monthly_total_pnl, check_portfolio_drawdown_breaker
)
with SessionLocal() as s:
    threshold, n_obs = _portfolio_dd_threshold(s, user_id=None)
    monthly = _monthly_total_pnl(s, user_id=None)
    print(f'monthly_total_pnl=\${monthly:.2f}  threshold=\${threshold:.2f}  n_obs={n_obs}')
    tripped, reason = check_portfolio_drawdown_breaker(s, user_id=None)
    print(f'still_tripped={tripped}  reason={reason}')
"

# 3. If you decide to reset (after sign-off, incident log entry):
conda run -n chili-env python -c "
from app.services.trading.portfolio_risk import _persist_portfolio_breaker_state
_persist_portfolio_breaker_state(
    tripped=False, reason='manual reset — see incident #', regime='portfolio_breaker'
)
"
```

The reset writes a fresh `breaker_tripped=False` row but does **not** lower the threshold or alter the trailing-30d numerator — the next venue-adapter call will recompute and trip again if the underlying P&L is still beyond limits. Loop reset/trip is a signal to escalate, not to keep resetting.

To stop trips during an active incident without unwinding the safety belt, flip `chili_portfolio_dd_breaker_live=False` (drops the tier into shadow mode — still logs but stops blocking) instead of resetting state.

### When each tier is expected to trip

| Scenario | Pattern tier | Portfolio tier |
|---|---|---|
| CHILI-attributed losses run heavy; no_pattern flat | Trips | Likely also trips (all-closed sees CHILI loss) |
| no_pattern losses run heavy; CHILI flat | Does NOT trip (no_pattern outside its distribution) | Trips |
| CHILI loses big, no_pattern offsets at account level | Trips | Does NOT trip (lever-alignment crossover; only manual / no_pattern entries should proceed) |
| Both tiers green | Allow | Allow |
| n<30 history on either tier | Skip (logged WARNING), tier is dormant | Independent — one can be active while the other is dormant |
