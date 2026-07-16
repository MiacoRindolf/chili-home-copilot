import json
from app.config import settings
from app.services.trading import governance as g

out = {}
try:
    out["kill_switch_active"] = bool(g.is_kill_switch_active())
except Exception as e:
    out["ks_err"] = repr(e)[:120]
# kill-switch reason (try common names)
for fn in ("kill_switch_reason", "get_kill_switch_reason", "active_kill_switch_reason", "kill_switch_status"):
    if hasattr(g, fn):
        try:
            out["ks_reason"] = getattr(g, fn)()
        except Exception as e:
            out["ks_reason_err"] = repr(e)[:120]
        break
out["per_broker_daily_loss_enabled"] = getattr(settings, "chili_per_broker_daily_loss_enabled", "MISSING")
# realized today + cap (per-broker agentic + global)
for fn in ("realized_pnl_today_by_broker", "realized_pnl_today"):
    if hasattr(g, fn):
        try:
            out[f"{fn}_agentic"] = getattr(g, fn)("robinhood_agentic_mcp")
        except Exception:
            try:
                out[fn] = getattr(g, fn)()
            except Exception as e:
                out[f"{fn}_err"] = repr(e)[:120]
from app.services.trading.momentum_neural import risk_policy
out["daily_loss_cap_agentic"] = risk_policy.equity_relative_daily_loss_cap(
    getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0), "robinhood_agentic_mcp")
# dump governance fns mentioning daily/broker/breaker for discovery
out["gov_fns"] = [n for n in dir(g) if any(t in n.lower() for t in ("daily", "broker", "breaker", "drawdown", "halt"))][:20]
print("RISKWHY " + json.dumps(out, default=str))
