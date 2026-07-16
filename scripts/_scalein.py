import os, json
from sqlalchemy import create_engine, text
from app.config import settings
c = create_engine(os.environ["DATABASE_URL"]).connect()

print("=== scale-in / pyramid + scale-out config ===")
for k in dir(settings):
    if any(t in k for t in ("pyramid", "scale_in", "scale_out", "scalein", "add_on", "scaling")):
        try:
            print(f"  {k} = {getattr(settings, k)!r}")
        except Exception:
            pass

print("=== pyramid / scale-in / scale-out events (today, by type) ===")
rows = c.execute(text(
    "select event_type, count(*) n from trading_automation_events "
    "where ts >= date_trunc('day', now()) "
    "and (event_type ilike '%pyramid%' or event_type ilike '%scale%' or event_type ilike '%add%') "
    "group by event_type order by n desc limit 20")).fetchall()
for et, n in rows:
    print(f"  {et}: {n}")
if not rows:
    print("  (none today)")
