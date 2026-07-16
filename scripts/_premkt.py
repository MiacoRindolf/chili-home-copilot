import os, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, text
from app.config import settings as s
from app.services.trading import governance as g

out = {}
out["et"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %a")
eng = create_engine(os.environ["DATABASE_URL"])
ACTIVE = ["watching_live", "queued_live", "live_arm_pending", "live_entry_candidate",
          "live_pending_entry", "live_entered", "live_trailing"]
with eng.connect() as c:
    out["db"] = c.execute(text("select current_database()")).scalar()
    rows = c.execute(text(
        "select state,count(*) from trading_automation_sessions where mode='live' and state = any(:a) group by state"
    ), {"a": ACTIVE}).fetchall()
    out["lane"] = {r[0]: int(r[1]) for r in rows}
    rec = c.execute(text(
        "select symbol,state from trading_automation_sessions where mode='live' "
        "and state in ('live_entered','live_trailing') and updated_at > now() - interval '7 minutes'"
    )).fetchall()
    out["recent_fills_7m"] = [{"sym": r[0], "state": r[1]} for r in rec]
out["kill_switch"] = bool(g.is_kill_switch_active())
try:
    from app.services.trading.momentum_neural.auto_arm import _lane_execution_family
    out["lane_family"] = _lane_execution_family()
except Exception as e:
    out["fam_err"] = repr(e)[:100]
from app.services.trading.momentum_neural import risk_policy as rp
fam = out.get("lane_family", "robinhood_agentic_mcp")
try:
    out["daily_loss_cap"] = round(rp.equity_relative_daily_loss_cap(
        getattr(s, "chili_momentum_risk_max_daily_loss_usd", 250.0), fam), 2)
except Exception as e:
    out["cap_err"] = repr(e)[:100]
try:
    out["broker_block"] = g.is_broker_daily_loss_blocked(fam)
except Exception as e:
    out["broker_block_err"] = repr(e)[:80]
try:
    tok = json.load(open("/app/secrets/rh_agentic/token.json"))
    exp = tok.get("expires_at") or tok.get("expires") or tok.get("expiry") or tok.get("expires_at_utc")
    out["token_expires"] = exp
    out["token_keys"] = list(tok.keys())
except Exception as e:
    out["token_err"] = repr(e)[:100]
print("CHK " + json.dumps(out, default=str))
