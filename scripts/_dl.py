import os, json
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.trading import governance as g

out = {}
# broker daily-loss block registry (the stale-block suspect)
for fn in ("is_broker_daily_loss_blocked", "broker_daily_loss_breached"):
    if hasattr(g, fn):
        try:
            out[fn] = getattr(g, fn)("robinhood_agentic_mcp")
        except Exception as e:
            out[fn + "_err"] = repr(e)[:140]
if hasattr(g, "get_broker_daily_loss_block"):
    try:
        out["block_detail"] = g.get_broker_daily_loss_block("robinhood_agentic_mcp")
    except Exception as e:
        out["block_err"] = repr(e)[:140]
# global breach + realized today (need a db)
eng = create_engine(os.environ["DATABASE_URL"])
with Session(eng) as db:
    for fn, args in [("check_daily_loss_breach", (db,)),
                     ("realized_pnl_today_by_broker", (db, "robinhood_agentic_mcp"))]:
        if hasattr(g, fn):
            try:
                out[fn] = getattr(g, fn)(*args)
            except Exception as e:
                out[fn + "_err"] = repr(e)[:140]
# execution-family asset-class gate (the other uniform-block suspect)
try:
    from app.services.trading.execution_family_registry import execution_family_supports_asset_class
    out["agentic_supports_equity"] = execution_family_supports_asset_class("robinhood_agentic_mcp", "equity")
except Exception as e:
    out["efclass_err"] = repr(e)[:140]
print("DL " + json.dumps(out, default=str))
