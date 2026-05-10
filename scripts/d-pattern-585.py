"""What is pattern 585's current state? And what changed around 23:56 UTC May 8?"""
from sqlalchemy import text
from app.db import SessionLocal


def safe(sql, params=None):
    db = SessionLocal()
    try:
        rows = db.execute(text(sql), params or {}).fetchall()
        db.rollback()
        return rows
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return e
    finally:
        db.close()


print("=== 1. Discover scan_patterns columns ===")
res = safe("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name = 'scan_patterns' ORDER BY ordinal_position
""")
if isinstance(res, list):
    cols = [r[0] for r in res]
    print(f"  columns: {cols}")

print()
print("=== 2. Pattern 585 full record ===")
res = safe("SELECT * FROM scan_patterns WHERE id = 585")
if isinstance(res, list) and res:
    row = res[0]
    d = dict(row._mapping)
    for k, v in d.items():
        s = str(v)
        if len(s) > 200:
            s = s[:200] + "..."
        print(f"  {k} = {s}")

print()
print("=== 3. Pattern 1011 + 1016 (currently promoted) full records ===")
for pid in (1011, 1016):
    res = safe(f"SELECT * FROM scan_patterns WHERE id = {pid}")
    if isinstance(res, list) and res:
        d = dict(res[0]._mapping)
        print(f"\n  pattern id={pid}:")
        for k, v in d.items():
            s = str(v)
            if len(s) > 200:
                s = s[:200] + "..."
            print(f"    {k} = {s}")

print()
print("=== 4. Pattern 585 promotion/demotion history ===")
# Try various log tables
for tbl in ("trading_pattern_recert_log", "trading_pattern_drift_log",
            "pattern_evidence_corrections", "pattern_survival_decision_log",
            "trading_pattern_regime_promotion_log"):
    res = safe(f"""
        SELECT * FROM {tbl}
        WHERE pattern_id = 585
        ORDER BY created_at DESC
        LIMIT 5
    """)
    if isinstance(res, list) and res:
        print(f"\n  {tbl} (pattern_id=585):")
        for r in res:
            d = dict(r._mapping)
            print(f"    {d}")

print()
print("=== 5. Anything changed in scan_patterns around May 8 23:56 UTC ===")
res = safe("""
    SELECT id, lifecycle_stage, promotion_status, active, updated_at
    FROM scan_patterns
    WHERE updated_at BETWEEN '2026-05-08 23:00:00' AND '2026-05-09 02:00:00'
    ORDER BY updated_at DESC
    LIMIT 20
""")
if isinstance(res, list):
    print(f"  rows updated in cliff window: {len(res)}")
    for r in res:
        print(f"    id={r[0]} life={r[1]} promo={r[2]} active={r[3]} updated={r[4]}")

print()
print("=== 6. ALL ACTIVE patterns that might be eligible producers ===")
# Anything with promotion_status='promoted' OR lifecycle in (promoted,live) AND active
res = safe("""
    SELECT id, lifecycle_stage, promotion_status, active, updated_at, last_validated_at
    FROM scan_patterns
    WHERE active = true
      AND (LOWER(COALESCE(lifecycle_stage,'')) IN ('promoted','live')
           OR LOWER(COALESCE(promotion_status,'')) = 'promoted')
    ORDER BY id
""")
if isinstance(res, list):
    print(f"  eligible (active+promoted): {len(res)}")
    for r in res:
        print(f"    id={r[0]} life={r[1]} promo={r[2]} active={r[3]} updated={r[4]} validated={r[5]}")
