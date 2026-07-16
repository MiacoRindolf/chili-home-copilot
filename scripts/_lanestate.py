import os, json
from sqlalchemy import create_engine, text

eng = create_engine(os.environ["DATABASE_URL"])
ACTIVE = (
    "watching_live", "queued_live", "live_arm_pending", "live_entry_candidate",
    "live_pending_entry", "live_entered", "live_trailing", "armed_pending_runner",
)
out = {}
with eng.connect() as c:
    rows = c.execute(text(
        "select state, count(*) from trading_automation_sessions "
        "where mode='live' and state = any(:a) group by state order by 2 desc"
    ), {"a": list(ACTIVE)}).fetchall()
    out["active_by_state"] = {r[0]: int(r[1]) for r in rows}
    out["active_total"] = sum(int(r[1]) for r in rows)
    rows2 = c.execute(text(
        "select symbol, state, execution_family, "
        "round(extract(epoch from (now() - updated_at))) as age_s "
        "from trading_automation_sessions where mode='live' and state = any(:a) "
        "order by updated_at desc limit 10"
    ), {"a": list(ACTIVE)}).fetchall()
    out["active_rows"] = [
        {"sym": r[0], "state": r[1], "fam": r[2], "age_s": int(r[3] or 0)} for r in rows2
    ]
    rows3 = c.execute(text(
        "select state, count(*) from trading_automation_sessions "
        "where mode='live' and updated_at > now() - interval '50 minutes' "
        "group by state order by 2 desc"
    )).fetchall()
    out["touched_last_50min_by_state"] = {r[0]: int(r[1]) for r in rows3}
print("LANE " + json.dumps(out, default=str))
