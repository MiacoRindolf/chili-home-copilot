import os, json
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models.trading import TradingAutomationSession

eng = create_engine(os.environ["DATABASE_URL"])
out = {}
with Session(eng) as db:
    rows = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == "live_arm_pending",
    ).all()
    out["orphans"] = [
        {"id": s.id, "sym": s.symbol, "user_id": s.user_id, "started_at": str(s.started_at)}
        for s in rows
    ]
    try:
        from app.services.trading.momentum_neural.auto_arm import _auto_arm_user_id
        out["auto_arm_uid"] = _auto_arm_user_id()
    except Exception as e:
        out["uid_err"] = repr(e)[:140]
    # Clean via the FIXED canonical function (proves the reaper fix expires no-expiry stale rows)
    from app.services.trading.momentum_neural.automation_query import expire_stale_live_arm_sessions
    uids = sorted({s.user_id for s in rows if s.user_id is not None})
    out["expire_by_user"] = {}
    for u in uids:
        try:
            out["expire_by_user"][u] = expire_stale_live_arm_sessions(db, user_id=int(u))
        except Exception as e:
            out["expire_by_user"][u] = "ERR:" + repr(e)[:100]
    db.commit()
    out["remaining_arm_pending"] = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == "live_arm_pending",
    ).count()
print("CLEAN " + json.dumps(out, default=str))
