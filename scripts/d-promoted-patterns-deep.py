"""Deep dive on the 2 promoted patterns — what tickers do they have, and
where do they normally fire from?"""
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


print("=== 1. The 2 promoted patterns: full record ===")
res = safe("""
    SELECT id, name, lifecycle_stage, promotion_status, active,
           win_rate, sample_size, hit_count,
           last_validated_at, last_evidence_audit_at,
           conditions, metadata, created_at, updated_at
    FROM scan_patterns
    WHERE LOWER(COALESCE(lifecycle_stage, '')) = 'promoted'
       OR LOWER(COALESCE(promotion_status, '')) = 'promoted'
    ORDER BY id DESC
    LIMIT 5
""")
if isinstance(res, list):
    for r in res:
        d = dict(r._mapping)
        print(f"\n  id={d['id']}")
        print(f"  name={d.get('name')}")
        print(f"  lifecycle_stage={d.get('lifecycle_stage')}")
        print(f"  promotion_status={d.get('promotion_status')}")
        print(f"  active={d.get('active')}")
        print(f"  win_rate={d.get('win_rate')}  sample_size={d.get('sample_size')}  hit_count={d.get('hit_count')}")
        print(f"  last_validated_at={d.get('last_validated_at')}")
        print(f"  last_evidence_audit_at={d.get('last_evidence_audit_at')}")
        print(f"  created_at={d.get('created_at')}  updated_at={d.get('updated_at')}")
        cond = d.get('conditions')
        if cond:
            print(f"  conditions: {str(cond)[:300]}...")
        md = d.get('metadata')
        if md:
            print(f"  metadata: {str(md)[:300]}...")
else:
    print(f"  ERROR: {res}")

print()
print("=== 2. Recent pattern_breakout_imminent alerts: which tickers? ===")
res = safe("""
    SELECT scan_pattern_id, ticker, count(*) AS c,
           min(created_at) AS first_seen, max(created_at) AS last_seen
    FROM trading_alerts
    WHERE alert_type = 'pattern_breakout_imminent'
      AND created_at >= now() - interval '14 days'
    GROUP BY scan_pattern_id, ticker
    ORDER BY last_seen DESC
    LIMIT 30
""")
if isinstance(res, list):
    print(f"  {len(res)} (pattern, ticker) pairs in last 14d:")
    for r in res:
        print(f"    pattern={r[0]} ticker={r[1]:10s} count={r[2]:>4} first={r[3]} last={r[4]}")

print()
print("=== 3. Recent breakout_alerts table (legacy) ===")
res = safe("""
    SELECT count(*), max(created_at) FROM breakout_alerts
""")
if isinstance(res, list):
    print(f"  total: count={res[0][0]}, last={res[0][1]}")
res = safe("""
    SELECT count(*) FROM breakout_alerts WHERE created_at >= now() - interval '7 days'
""")
if isinstance(res, list):
    print(f"  last 7d: {res[0][0]}")

print()
print("=== 4. The 2 promoted patterns: scan_results ticker history ===")
# Find scan_results rows linked to promoted patterns
promoted_ids = []
res = safe("""
    SELECT id FROM scan_patterns
    WHERE LOWER(COALESCE(lifecycle_stage, '')) = 'promoted'
       OR LOWER(COALESCE(promotion_status, '')) = 'promoted'
""")
if isinstance(res, list):
    promoted_ids = [r[0] for r in res]
    print(f"  promoted IDs: {promoted_ids}")

if promoted_ids:
    pid_list = "(" + ",".join(str(i) for i in promoted_ids) + ")"
    res = safe(f"""
        SELECT scan_pattern_id, ticker, count(*) AS c,
               max(created_at) AS last_seen
        FROM scan_results
        WHERE scan_pattern_id IN {pid_list}
        GROUP BY scan_pattern_id, ticker
        ORDER BY last_seen DESC
        LIMIT 30
    """)
    if isinstance(res, list):
        print(f"  scan_results for promoted patterns ({len(res)} rows):")
        for r in res:
            print(f"    pattern={r[0]} ticker={r[1]:10s} count={r[2]:>4} last={r[3]}")
    else:
        print(f"  ERROR: {res}")

print()
print("=== 5. Watchlist universe (currently empty per scheduler log) ===")
res = safe("""
    SELECT count(*) FROM (
        SELECT DISTINCT user_id FROM trading.watchlist_entries
    ) sub
""")
if not isinstance(res, list):
    # Different schema
    res = safe("SELECT relname FROM pg_class WHERE relkind='r' AND relname LIKE '%watchlist%'")
    if isinstance(res, list):
        print(f"  watchlist tables: {[r[0] for r in res]}")
