import os, json
from datetime import datetime as dt, timedelta as td
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session
from app.config import settings
from app.models.trading import MomentumAutomationOutcome, TradingAutomationSession
from app.services.trading.momentum_neural.risk_policy import _account_equity_usd

out = {}
try:
    out["account_equity_usd"] = round(float(_account_equity_usd()), 2)
except Exception as e:
    out["equity_err"] = repr(e)[:160]
out["pct_cap"] = getattr(settings, "chili_global_max_daily_loss_pct_of_equity", None)
out["usd_cap"] = getattr(settings, "chili_global_max_daily_loss_usd", None)

et = ZoneInfo("America/New_York")
now_et = dt.now(et)
start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
end_utc = (start_et + td(days=1)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
eng = create_engine(os.environ["DATABASE_URL"])
with Session(eng) as db:
    rows = (
        db.query(
            TradingAutomationSession.execution_family,
            MomentumAutomationOutcome.symbol,
            func.count(),
            func.sum(MomentumAutomationOutcome.realized_pnl_usd),
        )
        .join(MomentumAutomationOutcome, MomentumAutomationOutcome.session_id == TradingAutomationSession.id)
        .filter(
            MomentumAutomationOutcome.terminal_at >= start_utc,
            MomentumAutomationOutcome.terminal_at < end_utc,
            TradingAutomationSession.execution_family != "alpaca_spot",
        )
        .group_by(TradingAutomationSession.execution_family, MomentumAutomationOutcome.symbol)
        .order_by(func.sum(MomentumAutomationOutcome.realized_pnl_usd).asc())
        .limit(15)
        .all()
    )
    out["today_by_sym"] = [
        {"fam": r[0], "sym": r[1], "n": int(r[2]), "pnl": round(float(r[3] or 0), 2)} for r in rows
    ]
print("BASIS " + json.dumps(out, default=str))
