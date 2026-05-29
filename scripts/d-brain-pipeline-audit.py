"""Architect-grade audit of the brain pattern and learning-cycle pipeline.

Goal: prove whether live brain-worker activity, pattern-imminent production,
and durable learning-cycle lineage are all present.

Sections:
  S0 Brain-worker liveness vs durable learning-cycle lineage
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
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def safe(sql, params=None):
    """Each query in its own session to avoid InFailedSqlTransaction."""
    db = None
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '8000ms'"))
        rows = db.execute(text(sql), params or {}).fetchall()
        db.rollback()
        return rows
    except Exception as e:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        return e
    finally:
        if db is not None:
            db.close()


def table_columns(table_name):
    res = safe("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name
    """, {"table_name": table_name})
    if not isinstance(res, list):
        return set()
    return {r[0] for r in res}


def table_exists(table_name):
    res = safe("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :table_name
        )
    """, {"table_name": table_name})
    return isinstance(res, list) and bool(res[0][0])


def first_present(columns, names, fallback=None):
    for name in names:
        if name in columns:
            return name
    return fallback


def coalesce_expr(columns, names, default):
    present = [name for name in names if name in columns]
    if not present:
        return default
    return "COALESCE(" + ", ".join(present + [default]) + ")"


def scan_pattern_projection(columns):
    return {
        "name_expr": "name" if "name" in columns else "id::text",
        "scope_expr": first_present(
            columns,
            ("ticker_scope", "asset_class", "timeframe"),
            "'?'",
        ),
        "sample_expr": coalesce_expr(
            columns,
            ("evidence_count", "trade_count", "backtest_count"),
            "0",
        ),
        "last_expr": first_present(
            columns,
            ("last_backtest_at", "updated_at", "created_at"),
            "NULL",
        ),
        "avg_return_expr": "avg_return_pct" if "avg_return_pct" in columns else "NULL",
    }


def activity_time_column(table_name, candidates):
    return first_present(table_columns(table_name), candidates)


def section(title):
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def print_learning_cycle_lineage():
    section("S0 - brain-worker liveness vs durable learning-cycle lineage")

    worker_activity_observed = False
    legacy_cycle_count_24h = None
    durable_cycle_rows = None

    if table_exists("brain_worker_control"):
        cols = table_columns("brain_worker_control")
        if {"id", "last_heartbeat_at", "updated_at"}.issubset(cols):
            live_expr = (
                "left(coalesce(learning_live_json,''), 260)"
                if "learning_live_json" in cols
                else "NULL"
            )
            digest_expr = (
                "left(coalesce(last_cycle_digest_json,''), 260)"
                if "last_cycle_digest_json" in cols
                else "NULL"
            )
            res = safe(f"""
                SELECT id,
                       {('wake_requested' if 'wake_requested' in cols else 'NULL')} AS wake_requested,
                       {('stop_requested' if 'stop_requested' in cols else 'NULL')} AS stop_requested,
                       last_heartbeat_at,
                       updated_at,
                       CASE
                         WHEN last_heartbeat_at IS NULL THEN NULL
                         ELSE extract(epoch from (now() - last_heartbeat_at))::int
                       END AS heartbeat_age_seconds,
                       {live_expr} AS learning_live_prefix,
                       {digest_expr} AS last_cycle_digest_prefix
                FROM brain_worker_control
                ORDER BY id
            """)
            if isinstance(res, list) and res:
                for r in res:
                    worker_activity_observed = r.heartbeat_age_seconds is not None
                    print(
                        "  brain_worker_control "
                        f"id={r.id} heartbeat_age_s={r.heartbeat_age_seconds} "
                        f"wake={r.wake_requested} stop={r.stop_requested}"
                    )
                    print(f"    learning_live_prefix={r.learning_live_prefix}")
                    print(f"    last_cycle_digest_prefix={r.last_cycle_digest_prefix}")
            else:
                print(f"  brain_worker_control unavailable: {res}")
    else:
        print("  brain_worker_control table absent")

    if table_exists("brain_batch_jobs"):
        cols = table_columns("brain_batch_jobs")
        if {"job_type", "status", "started_at"}.issubset(cols):
            ended_expr = "max(ended_at)" if "ended_at" in cols else "NULL"
            res = safe(f"""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE started_at >= now() - interval '24 hours') AS count_24h,
                       count(*) FILTER (WHERE status = 'running') AS running_total,
                       max(started_at) AS latest_started_at,
                       {ended_expr} AS latest_ended_at
                FROM brain_batch_jobs
                WHERE job_type = 'learning_cycle'
            """)
            if isinstance(res, list) and res:
                r = res[0]
                legacy_cycle_count_24h = int(r.count_24h or 0)
                print(
                    "  legacy learning_cycle ledger "
                    f"total={r.total} count_24h={r.count_24h} running={r.running_total} "
                    f"latest_started={r.latest_started_at} latest_ended={r.latest_ended_at}"
                )

            res = safe("""
                SELECT job_type, status, count(*) AS count, max(started_at) AS latest_started_at
                FROM brain_batch_jobs
                WHERE started_at >= now() - interval '2 hours'
                  AND (
                    job_type ILIKE '%brain%' OR job_type ILIKE '%neural%'
                    OR job_type ILIKE '%pattern%' OR job_type ILIKE '%learning%'
                  )
                GROUP BY job_type, status
                ORDER BY latest_started_at DESC
                LIMIT 25
            """)
            if isinstance(res, list):
                print("  recent brain/pattern job activity:")
                if not res:
                    print("    none in last 2h")
                else:
                    worker_activity_observed = True
                for r in res:
                    print(
                        f"    {r.job_type:36s} status={r.status:10s} "
                        f"count={r.count:>5} latest_started={r.latest_started_at}"
                    )
    else:
        print("  brain_batch_jobs table absent")

    durable_specs = {
        "brain_learning_cycle_run": ("started_at", "created_at"),
        "brain_stage_job": ("started_at", "finished_at"),
        "brain_cycle_lease": ("acquired_at", "expires_at"),
        "brain_prediction_snapshot": ("as_of_ts",),
        "brain_prediction_line": ("created_at",),
    }
    print("  durable lineage tables:")
    for table_name, candidates in durable_specs.items():
        if not table_exists(table_name):
            print(f"    {table_name:30s} absent")
            continue
        time_col = activity_time_column(table_name, candidates)
        if time_col:
            res = safe(f"""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE {time_col} >= now() - interval '24 hours') AS count_24h,
                       max({time_col}) AS latest_at
                FROM {table_name}
            """)
        else:
            res = safe(f"SELECT count(*) AS total, NULL AS count_24h, NULL AS latest_at FROM {table_name}")
        if isinstance(res, list) and res:
            r = res[0]
            if table_name == "brain_learning_cycle_run":
                durable_cycle_rows = int(r.total or 0)
            print(
                f"    {table_name:30s} total={r.total} count_24h={r.count_24h} "
                f"latest={r.latest_at} via={time_col or '<none>'}"
            )

    if worker_activity_observed and legacy_cycle_count_24h == 0 and durable_cycle_rows == 0:
        print(
            "  READINESS_CLASSIFICATION=WORKER_ACTIVITY_WITHOUT_DURABLE_LEARNING_CYCLE_LINEAGE"
        )
        print(
            "  meaning: heartbeat/event activity is present, but persisted cycle/stage "
            "lineage is not observed; treat learning_live_json as volatile UI state."
        )
    elif worker_activity_observed:
        print("  READINESS_CLASSIFICATION=WORKER_ACTIVITY_WITH_LINEAGE_PARTIAL_OR_PRESENT")
    else:
        print("  READINESS_CLASSIFICATION=WORKER_ACTIVITY_NOT_OBSERVED")


def main():
    print(f"# brain-pipeline-audit {datetime.now(timezone.utc).isoformat()}")

    print_learning_cycle_lineage()

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

    scan_cols = table_columns("scan_patterns")
    projection = scan_pattern_projection(scan_cols)
    name_expr = projection["name_expr"]
    scope_expr = projection["scope_expr"]
    sample_expr = projection["sample_expr"]
    last_expr = projection["last_expr"]
    avg_return_expr = projection["avg_return_expr"]

    res = safe(f"""
        SELECT id, {name_expr} AS pattern_name, {scope_expr} AS scope_hint,
               lifecycle_stage, promotion_status, active,
               win_rate, {sample_expr} AS sample_size, {last_expr} AS last_activity_at
        FROM scan_patterns
        WHERE LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
           OR LOWER(COALESCE(promotion_status, '')) = 'promoted'
        ORDER BY id DESC
        LIMIT 20
    """)
    if isinstance(res, list):
        print(f"  eligible patterns (top 20):")
        for r in res:
            print(
                f"    id={r[0]} scope={(r[2] or '?')[:12]:12s} "
                f"life={(r[3] or '?')[:10]:10s} promo={(r[4] or '?')[:18]:18s} "
                f"active={r[5]} wr={r[6]} n={r[7]} last={r[8]} "
                f"name={(r[1] or '?')[:46]}"
            )
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
    activity_time_candidates = {
        "trading_pattern_recert_log": ("created_at", "observed_at", "as_of_date"),
        "trading_pattern_drift_log": ("created_at", "observed_at", "sweep_at"),
        "trading_pattern_evidence_hypotheses": ("created_at", "observed_at"),
        "pattern_evidence_corrections": ("created_at", "updated_at"),
        "trading_pattern_regime_promotion_log": ("created_at", "computed_at", "as_of_date"),
        "pattern_survival_decision_log": ("created_at", "decided_at"),
        "pattern_survival_promote_review_queue": ("created_at", "updated_at"),
    }
    for tbl in ("trading_pattern_recert_log", "trading_pattern_drift_log",
                "trading_pattern_evidence_hypotheses", "pattern_evidence_corrections",
                "trading_pattern_regime_promotion_log",
                "pattern_survival_decision_log",
                "pattern_survival_promote_review_queue"):
        cols = table_columns(tbl)
        if not cols:
            continue
        time_col = first_present(cols, activity_time_candidates.get(tbl, ("created_at",)))
        if time_col:
            res = safe(f"SELECT count(*), max({time_col}) FROM {tbl}")
        else:
            res = safe(f"SELECT count(*), NULL FROM {tbl}")
        if isinstance(res, list) and res[0][0] is not None:
            print(f"  {tbl:42s} total={res[0][0]:>6} last={res[0][1]} via={time_col or '<none>'}")
        if time_col:
            res = safe(f"SELECT count(*) FROM {tbl} WHERE {time_col} >= now() - interval '7 days'")
            if isinstance(res, list):
                print(f"    last 7d: {res[0][0]}")

    # ---- S5: Top candidate patterns (would-be promoted) ----
    section("S5 - Top candidate patterns by win_rate * sample_size")
    res = safe(f"""
        SELECT id, {name_expr} AS pattern_name, {scope_expr} AS scope_hint,
               lifecycle_stage, promotion_status, active,
               win_rate, {sample_expr} AS sample_size,
               {avg_return_expr} AS avg_return_pct, {last_expr} AS last_activity_at
        FROM scan_patterns
        WHERE active = true
          AND {sample_expr} >= 20
          AND win_rate >= 0.5
          AND NOT (LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
                   OR LOWER(COALESCE(promotion_status, '')) = 'promoted')
        ORDER BY COALESCE(win_rate, 0) * COALESCE({sample_expr}, 0) DESC NULLS LAST
        LIMIT 20
    """)
    if isinstance(res, list):
        print(f"  candidates with wr>=0.5, n>=20, NOT promoted (top 20):")
        for r in res:
            print(
                f"    id={r[0]:>5} scope={(r[2] or '?')[:12]:12s} "
                f"life={(r[3] or '?')[:10]:10s} promo={(r[4] or '?')[:18]:18s} "
                f"wr={r[6]} n={r[7]} avg_ret={r[8]} last={r[9]} "
                f"name={(r[1] or '?')[:42]}"
            )

    # ---- S6: pattern_survival_predictions activity ----
    section("S6 - pattern_survival_predictions activity")
    survival_time_candidates = {
        "pattern_survival_predictions": ("created_at", "trained_at", "snapshot_date"),
        "pattern_survival_features": ("created_at", "snapshot_date", "promoted_at"),
        "pattern_survival_decision_log": ("created_at", "decided_at"),
    }
    for tbl in ("pattern_survival_predictions", "pattern_survival_features",
                "pattern_survival_decision_log"):
        cols = table_columns(tbl)
        if not cols:
            continue
        time_col = first_present(cols, survival_time_candidates.get(tbl, ("created_at",)))
        if time_col:
            res = safe(f"SELECT count(*), max({time_col}) FROM {tbl}")
        else:
            res = safe(f"SELECT count(*), NULL FROM {tbl}")
        if isinstance(res, list) and res[0][0] is not None:
            print(f"  {tbl:42s} total={res[0][0]:>6} last={res[0][1]} via={time_col or '<none>'}")
        if time_col:
            res24 = safe(f"SELECT count(*) FROM {tbl} WHERE {time_col} >= now() - interval '24 hours'")
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
