# Kill-switch runbook

**Hard Rule 1 (CLAUDE.md):** the kill switch gates every automated trade. A tripped breaker blocks new entries but does not liquidate open positions — it is a defensive freeze, not a panic close.

Use this runbook when:

- An automated trade misbehaved (unexpected size, wrong ticker, wrong direction)
- The broker account is showing activity CHILI did not emit
- A dependency the brain relies on (market data, pattern pipeline) is visibly broken
- You are about to make a change to the live trading path and want to stop automation during the rollout

## TL;DR — manual activation

Python REPL from the repo root, conda env active:

```bash
conda run -n chili-env python -c "from app.services.trading.governance import activate_kill_switch; activate_kill_switch(reason='manual 2026-04-20 ops')"
```

The state persists via `trading_risk_state` (regime='kill_switch') and survives app restart. On startup, `_run_deferred_startup` in `app/main.py` calls `restore_kill_switch_from_db()` — if restored ACTIVE, a `[startup] Kill switch restored ACTIVE: <reason>` line is emitted at WARNING level.

## How it's enforced

- `app/services/trading/governance.py`
  - `activate_kill_switch(reason)` — sets the in-process flag and persists
  - `deactivate_kill_switch()` — clears the in-process flag and persists
  - `is_kill_switch_active()` — thread-safe read, consulted by the auto-trader
  - `get_kill_switch_status()` — returns `{active: bool, reason: str | None}`
  - `restore_kill_switch_from_db()` — called on startup so a tripped breaker does not silently disarm on redeploy
- Every `place_market_order` / `place_limit_order` path in `app/services/trading/auto_trader.py` checks `is_kill_switch_active()` before contacting the broker; a tripped switch short-circuits with an audit log and returns without placing
- Logs: `[governance] KILL SWITCH ACTIVATED: <reason>` at CRITICAL when tripped, and the startup line when restored

## Verification (after flipping)

1. Confirm state with:
   ```bash
   conda run -n chili-env python -c "from app.services.trading.governance import get_kill_switch_status; print(get_kill_switch_status())"
   ```
   Expected: `{'active': True, 'reason': '<your reason>'}`.
2. Tail the app log for one minute and confirm no new `[auto_trader] placed` events appear.
3. In the dashboard (`/chat` → trading tab), the autopilot badge should display "KILL SWITCH ACTIVE" — if it does not, the UI cache may be stale; hard-refresh.

## Reset

After the incident is resolved and you have signed off on causes + mitigations:

```bash
conda run -n chili-env python -c "from app.services.trading.governance import deactivate_kill_switch; deactivate_kill_switch()"
```

Then run verification step 1 and expect `{'active': False, 'reason': None}`.

**Never** deactivate while an open incident exists. If in doubt, leave it tripped and escalate.

## Known interactions

- **Drawdown breaker** (`check_drawdown_breaker` in `portfolio_risk.py`) is a separate mechanism with its own reason. A tripped drawdown breaker does not automatically flip the kill switch, and vice versa. If both need to be cleared, do the kill switch first (it's the wider blast radius) and then reset the breaker — see `DRAWDOWN_BREAKER_RUNBOOK.md`.
- **Prediction mirror rollout** (`app/trading_brain/`) is independent of the kill switch. Flipping the kill switch does not affect dual-write/read flags — see `PHASE_ROLLBACK_RUNBOOK.md` for that rollout dimension.
- **Dual-path broker credentials** — if `[startup] Dual-path broker credentials detected:` appears on boot, the `.env` plaintext fallback is shadowing the vault. This does not trip the kill switch but indicates a credential migration gap; see `app/main.py::_warn_dual_path_broker_credentials`.

## Audit trail

Every toggle writes to `trading_risk_state` with the reason and timestamp. Pull the last 24h of state changes:

```sql
SELECT created_at, breaker_tripped, breaker_reason
FROM trading_risk_state
WHERE regime = 'kill_switch'
ORDER BY created_at DESC
LIMIT 20;
```

## If the kill switch will not activate

Possible causes, in order:

1. App is not running — restart via `.\scripts\start-https.ps1`; on boot, activate again.
2. `trading_risk_state` table is corrupt or missing — run migrations: `conda run -n chili-env python -c "from app.migrations import run_migrations; from app.db import engine; run_migrations(engine)"`. The migration ID guard will fail fast if there is a schema issue (see header of `app/migrations.py`).
3. Broker is still receiving orders despite `is_kill_switch_active() == True` — this is a bug; do not restart. Pull the last 50 `[auto_trader]` log lines and open an incident. The check is supposed to be atomic but an error in a code path that bypasses `auto_trader` (e.g. manual order placement from a route) will not be blocked.
