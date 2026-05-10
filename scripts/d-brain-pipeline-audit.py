"""Architect-grade audit of the brain pattern pipeline.

Goal: find exactly why no pattern_imminent alerts are being produced.

Sections:
  S1 Pattern roster: scan_patterns by (lifecycle_stage, promotion_status, active)
  S2 pattern_imminent eligibility count (replicates scan_pattern_eligible_main_imminent)
  S3 pattern_imminent_scanner job activity (last runs, recent BreakoutAlert rows)
  S4 Recent promotion/demotion decisions
  S5 Top 20 candidate patterns by hit-rate / sample-size (not yet promoted)
  S6 pattern_survival_predictions activity
  S7 BreakoutAlert / AlertHistory recent (where pattern_imminent goes once dispatched)
  S8 Trading_alerts breakdown by alert_type with relationship to ScanPattern.id
"""
import sys
from datetime import datetime, timezone

from sqlalchemy import text


def safe(sql, params=None):
    """Each query in its own session to avoid InFailedSqlTransaction."""
    from app.db import SessionLocal
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


def section(title):
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main():
    print(f"# brain-pipeline-audit {datetime.now(timezone.utc).isoformat()}")

    # ---- S1: Pattern roster by lifecycle/promotion/active ----
    section("S1 - scan_patterns roster")
    res = safe("""
        SELECT
          COALESCE(lifecycle_stage, '<null>') AS life,
          COALESCE(promotion_status, '<null>') AS promo,
          active,
          count(*) AS c
        FROM scan_patterns
        GROUP BY life, promo, active
        ORDER BY c DESC
        LIMIT 30
    """)
    if isinstance(res, list):
        print(f"  rows: {len(res)} groups")
        for r in res:
            print(f"    life={r[0]:14s} promo={r[1]:14s} active={r[2]} count={r[3]}")
    else:
        print(f"  ERROR: {res}")

    res = safe("SELECT count(*) FROM scan_patterns")
    if isinstance(res, list):
        print(f"  scan_patterns total rows: {res[0][0]}")
    res = safe("SELECT count(*) FROM scan_patterns WHERE active = true")
    if isinstance(res, list):
        print(f"  active=true: {res[0][0]}")

    # ---- S2: pattern_imminent eligibility ----
    section("S2 - pattern_imminent ELIGIBLE roster")
    # Replicates: lifecycle_stage IN ('promoted','live') OR promotion_status='promoted'
    res = safe("""
        SELECT count(*) FROM scan_patterns
        WHERE LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
           OR LOWER(COALESCE(promotion_status, '')) = 'promoted'
    """)
    if isinstance(res, list):
        print(f"  ELIGIBLE patterns (any active): {res[0][0]}")
    res = safe("""
        SELECT count(*) FROM scan_patterns
        WHERE active = true
          AND (LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
               OR LOWER(COALESCE(promotion_status, '')) = 'promoted')
    """)
    if isinstance(res, list):
        print(f"  ELIGIBLE patterns (active=true ONLY): {res[0][0]}")

    res = safe("""
        SELECT id, ticker, lifecycle_stage, promotion_status, active,
               win_rate, sample_size, last_validated_at
        FROM scan_patterns
        WHERE LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
           OR LOWER(COALESCE(promotion_status, '')) = 'promoted'
        ORDER BY id DESC
        LIMIT 20
    """)
    if isinstance(res, list):
        print(f"  eligible patterns (top 20):")
        for r in res:
            print(f"    id={r[0]} {r[1] or '?':10s} life={(r[2] or '?')[:10]:10s} promo={(r[3] or '?')[:10]:10s} active={r[4]} wr={r[5]} n={r[6]} last_val={r[7]}")
    else:
        print(f"  ERROR: {res}")

    # ---- S3: pattern_imminent producer activity ----
    section("S3 - BreakoutAlert / AlertHistory recent rows (pattern_imminent target)")
    res = safe("SELECT count(*) FROM breakout_alerts WHERE created_at >= now() - interval '24 hours'")
    if isinstance(res, list):
        print(f"  breakout_alerts last 24h: {res[0][0]}")
    res = safe("SELECT count(*) FROM breakout_alerts WHERE created_at >= now() - interval '1 hour'")
    if isinstance(res, list):
        print(f"  breakout_alerts last 1h: {res[0][0]}")
    res = safe("""
        SELECT id, ticker, pattern_name, created_at FROM breakout_alerts
        ORDER BY created_at DESC LIMIT 5
    """)
    if isinstance(res, list):
        print(f"  most recent 5 breakout_alerts:")
        for r in res:
            print(f"    id={r[0]} {r[1]:10s} pattern={(r[2] or '?')[:30]:30s} created={r[3]}")

    res = safe("""
        SELECT alert_type, count(*), max(created_at)
        FROM alert_history
        WHERE created_at >= now() - interval '7 days'
        GROUP BY alert_type
        ORDER BY 2 DESC
        LIMIT 20
    """)
    if isinstance(res, list):
        print(f"\n  alert_history breakdown last 7d:")
        for r in res:
            print(f"    {(r[0] or '<null>')[:30]:30s} count={r[1]:>5} last={r[2]}")

    # Look for PATTERN_BREAKOUT_IMMINENT rows specifically
    res = safe("""
        SELECT count(*) FROM alert_history
        WHERE alert_type = 'PATTERN_BREAKOUT_IMMINENT'
           OR alert_type = 'pattern_breakout_imminent'
           OR alert_type = 'pattern_imminent'
    """)
    if isinstance(res, list):
        print(f"\n  alert_history PATTERN_BREAKOUT_IMMINENT total: {res[0][0]}")
    res = safe("""
        SELECT count(*) FROM alert_history
        WHERE (alert_type = 'PATTERN_BREAKOUT_IMMINENT'
            OR alert_type = 'pattern_breakout_imminent'
            OR alert_type = 'pattern_imminent')
          AND created_at >= now() - interval '24 hours'
    """)
    if isinstance(res, list):
        print(f"  alert_history PATTERN_BREAKOUT_IMMINENT last 24h: {res[0][0]}")

    # ---- S4: Recent promotion/demotion decisions ----
    section("S4 - Recent promotion / demotion / re-cert log activity")
    for tbl in ("trading_pattern_recert_log", "trading_pattern_drift_log",
                "trading_pattern_evidence_hypotheses", "pattern_evidence_corrections",
                "trading_pattern_regime_promotion_log",
                "pattern_survival_decision_log",
                "pattern_survival_promote_review_queue"):
        res = safe(f"SELECT count(*), max(created_at) FROM {tbl}")
        if isinstance(res, list) and res[0][0] is not None:
            print(f"  {tbl:42s} total={res[0][0]:>6} last={res[0][1]}")
        res = safe(f"SELECT count(*) FROM {tbl} WHERE created_at >= now() - interval '7 days'")
        if isinstance(res, list):
            print(f"    last 7d: {res[0][0]}")

    # ---- S5: Top candidate patterns (would-be promoted) ----
    section("S5 - Top candidate patterns by win_rate * sample_size")
    res = safe("""
        SELECT id, ticker, lifecycle_stage, promotion_status, active,
               win_rate, sample_size, hit_count
        FROM scan_patterns
        WHERE active = true
          AND sample_size >= 20
          AND win_rate >= 0.5
          AND NOT (LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
                   OR LOWER(COALESCE(promotion_status, '')) = 'promoted')
        ORDER BY win_rate * sample_size DESC NULLS LAST
        LIMIT 20
    """)
    if isinstance(res, list):
        print(f"  candidates with wr>=0.5, n>=20, NOT promoted (top 20):")
        for r in res:
            print(f"    id={r[0]:>5} {r[1] or '?':10s} life={(r[2] or '?')[:10]:10s} promo={(r[3] or '?')[:10]:10s} wr={r[5]} n={r[6]} hits={r[7]}")

    # ---- S6: pattern_survival_predictions activity ----
    section("S6 - pattern_survival_predictions activity")
    for tbl in ("pattern_survival_predictions", "pattern_survival_features",
                "pattern_survival_decision_log"):
        res = safe(f"SELECT count(*), max(created_at) FROM {tbl}")
        if isinstance(res, list) and res[0][0] is not None:
            print(f"  {tbl:42s} total={res[0][0]:>6} last={res[0][1]}")
        res24 = safe(f"SELECT count(*) FROM {tbl} WHERE created_at >= now() - interval '24 hours'")
        if isinstance(res24, list):
            print(f"    last 24h: {res24[0][0]}")

    # ---- S7: pattern_imminent_scanner job activity ----
    section("S7 - pattern_imminent_scanner job activity (logs)")
    # Look for scheduler heartbeat / job execution log
    for tbl in ("trading_automation_runtime_snapshots", "trading_automation_events"):
        res = safe(f"SELECT count(*), max(created_at) FROM {tbl}")
        if isinstance(res, list) and res[0][0] is not None:
            print(f"  {tbl:42s} total={res[0][0]:>6} last={res[0][1]}")

    # Look for opportunity scoring / scan results recency (these feed pattern_imminent_alerts)
    for tbl in ("scan_results", "trading_opportunity_scores"):
        res = safe(f"SELECT count(*), max(created_at) FROM {tbl}")
        if isinstance(res, list) and res[0][0] is not None:
            print(f"  {tbl:42s} total={res[0][0]:>6} last={res[0][1]}")

    # ---- S8: trading_alerts (where pattern_breakout_imminent surface) ----
    section("S8 - trading_alerts by alert_type x scan_pattern_id presence")
    res = safe("""
        SELECT alert_type,
               count(*) AS c,
               sum(CASE WHEN scan_pattern_id IS NOT NULL THEN 1 ELSE 0 END) AS with_pattern,
               max(created_at) AS last
        FROM trading_alerts
        WHERE created_at >= now() - interval '7 days'
        GROUP BY alert_type
        ORDER BY 2 DESC
        LIMIT 20
    """)
    if isinstance(res, list):
        print(f"  trading_alerts breakdown last 7d:")
        for r in res:
            print(f"    {(r[0] or '<null>')[:30]:30s} count={r[1]:>5} pattern_linked={r[2]:>5} last={r[3]}")

    print()
    print("=" * 78)
    print("  audit complete")
    print("=" * 78)


if __name__ == "__main__":
    main()
