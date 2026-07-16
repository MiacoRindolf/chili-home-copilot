import os, json
from sqlalchemy import create_engine, text
from app.config import settings
from app.services.trading.governance import is_kill_switch_active
from app.services.trading.momentum_neural.auto_arm import _lane_execution_family
out = {}
eng = create_engine(os.environ["DATABASE_URL"])
with eng.connect() as c:
    out["DB"] = c.execute(text("select current_database()")).scalar()
    rows = c.execute(text(
        "select state, count(*) from trading_automation_sessions where mode='live' and state in "
        "('watching_live','queued_live','live_entry_candidate','live_pending_entry','live_entered','live_trailing') "
        "group by state order by 2 desc")).fetchall()
    out["open_now"] = {r[0]: r[1] for r in rows} or "none"
    out["errors_last_15m"] = c.execute(text(
        "select count(*) from trading_automation_sessions where mode='live' and state='live_error' "
        "and updated_at > now() - interval '15 min'")).scalar()
    out["reaped_last_15m"] = c.execute(text(
        "select count(*) from trading_automation_sessions where mode='live' and state='live_cancelled' "
        "and updated_at > now() - interval '15 min'")).scalar()
out["kill_switch"] = bool(is_kill_switch_active())
out["lane_family"] = _lane_execution_family()
out["crypto_only"] = bool(getattr(settings, "chili_momentum_auto_arm_crypto_only", True))
print("HEALTH " + json.dumps(out, default=str))
