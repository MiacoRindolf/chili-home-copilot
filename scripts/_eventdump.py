import os, json
from sqlalchemy import create_engine, text, inspect
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()
insp = inspect(e)
cols = [col["name"] for col in insp.get_columns("trading_automation_events")]
print("events cols:", cols)
# pick the session-id, type, payload, ts columns by best-guess match
sidc = next((x for x in cols if "session" in x.lower()), None)
typc = next((x for x in cols if x.lower() in ("event_type","type","kind","name","event")), None)
payc = next((x for x in cols if x.lower() in ("payload","data","detail","details","meta","payload_json")), None)
tsc = next((x for x in cols if "creat" in x.lower() or x.lower() in ("ts","at","occurred_at")), None)
print(f"using sid={sidc} type={typc} pay={payc} ts={tsc}")
for sid in (7578, 7573, 7567, 7560):
    rows = c.execute(text(
        f"select {typc}, {payc} from trading_automation_events where {sidc}=:s order by {tsc} desc limit 4"
    ), {"s": sid}).fetchall()
    print(f"=== session {sid} ===")
    for t, p in rows:
        ps = json.dumps(p) if not isinstance(p, str) else p
        print(f"  {t}: {str(ps)[:240]}")
