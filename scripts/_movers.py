import os, json
from sqlalchemy import create_engine, text

eng = create_engine(os.environ["DATABASE_URL"])
with eng.connect() as c:
    cols = [r[0] for r in c.execute(text(
        "select column_name from information_schema.columns "
        "where table_name='momentum_symbol_viability' order by ordinal_position"
    )).fetchall()]
    # identity + the signals that decide an arm
    pref = ["symbol", "ross_score", "viability_score", "rvol", "relative_volume",
            "gap_pct", "gap", "pct_change", "change_pct", "last_price", "price",
            "is_eligible", "eligible", "discarded", "is_discarded", "updated_at"]
    want = [x for x in pref if x in cols]
    if "symbol" not in want:
        want = cols[: min(8, len(cols))]
    order = next((x for x in ["ross_score", "viability_score", "rvol", "relative_volume"] if x in cols), want[0])
    has_upd = "updated_at" in cols
    where = "where updated_at > now() - interval '30 minutes'" if has_upd else ""
    sel = ", ".join(f'"{x}"' for x in want)
    rows = c.execute(text(
        f"select {sel} from momentum_symbol_viability {where} "
        f'order by "{order}" desc nulls last limit 15'
    )).fetchall()
    fresh = 0
    if has_upd:
        fresh = c.execute(text(
            "select count(*) from momentum_symbol_viability where updated_at > now() - interval '30 minutes'"
        )).scalar()
    print("COLS " + json.dumps(cols))
    print("FRESH_30m " + str(fresh))
    print("TOP " + json.dumps([{want[i]: str(r[i]) for i in range(len(want))} for r in rows]))
