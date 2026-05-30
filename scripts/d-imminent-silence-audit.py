"""8-layer audit: where did the pattern_imminent alerts go?

Read-only Python diagnostic. Each query runs in its own session/transaction
to avoid InFailedSqlTransaction cascading on missing-table errors.
"""
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import SessionLocal


def now_utc():
    return datetime.now(timezone.utc)


def safe_query(sql, params=None):
    """Run a query in its own session. Returns rows or None on failure."""
    db = SessionLocal()
    try:
        rows = db.execute(text(sql), params or {}).fetchall()
        db.rollback()  # read-only, but be explicit
        return rows
    except Exception as e:
        db.rollback()
        return e
    finally:
        db.close()


def print_section(title):
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def fmt_age(ts, now):
    if ts is None:
        return "NULL"
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        delta = now.replace(tzinfo=None) - ts
    else:
        delta = now - ts
    mins = delta.total_seconds() / 60.0
    if mins < 60:
        return f"{mins:.1f}m"
    if mins < 1440:
        return f"{mins/60:.1f}h"
    return f"{mins/1440:.1f}d"


def main():
    now = now_utc()
    print(f"# audit @ {now.isoformat()}")

    # ----- L0: discover relevant tables -----
    print_section("L0 — Table discovery (heartbeat-like + brain-like + autotrader-like)")
    res = safe_query("""
        SELECT relname FROM pg_class
        WHERE relkind='r' AND (
            relname LIKE '%heartbeat%' OR
            relname LIKE '%brain%' OR
            relname LIKE '%event%' OR
            relname LIKE '%alert%' OR
            relname LIKE '%pattern%' OR
            relname LIKE '%snapshot%' OR
            relname LIKE '%autotrader%' OR
            relname LIKE '%trade%' OR
            relname LIKE '%fast_%' OR
            relname LIKE '%bracket%'
        )
        ORDER BY relname
    """)
    if isinstance(res, list):
        names = [r[0] for r in res]
        print(f"  found {len(names)} candidate tables")
        for n in names:
            print(f"    {n}")
    else:
        print(f"  ERROR: {res}")

    # ----- L1: Worker heartbeats -----
    print_section("L1 — Worker heartbeats")
    for table in ("worker_heartbeats", "scheduler_worker_heartbeat",
                  "brain_worker_heartbeat", "trading_worker_heartbeat",
                  "worker_heartbeat", "heartbeats"):
        res = safe_query(f"""
            SELECT * FROM {table} ORDER BY 1 DESC LIMIT 5
        """)
        if isinstance(res, list) and res:
            print(f"  table={table} top 5:")
            for r in res:
                print(f"    {dict(r._mapping)}")

    # ----- L2: Brain event log -----
    print_section("L2 — pattern_imminent event recency")
    for tbl in ("trading_brain_events", "brain_events", "pattern_events",
                "pattern_imminent_alerts", "trading_alerts",
                "trading_pattern_imminent", "pattern_imminent_log"):
        res = safe_query(f"SELECT count(*) FROM {tbl}")
        if isinstance(res, list):
            total = res[0][0]
            r1h = safe_query(f"SELECT count(*) FROM {tbl} WHERE created_at >= now() - interval '1 hour'")
            r24h = safe_query(f"SELECT count(*) FROM {tbl} WHERE created_at >= now() - interval '24 hours'")
            r7d = safe_query(f"SELECT count(*) FROM {tbl} WHERE created_at >= now() - interval '7 days'")
            last = safe_query(f"SELECT max(created_at) FROM {tbl}")
            last_ts = last[0][0] if isinstance(last, list) and last and last[0][0] else None
            print(f"  {tbl:35s} total={total:>8} 1h={r1h[0][0] if isinstance(r1h,list) else '?':>5} 24h={r24h[0][0] if isinstance(r24h,list) else '?':>5} 7d={r7d[0][0] if isinstance(r7d,list) else '?':>5} last={fmt_age(last_ts, now)}")

            # event_type breakdown
            bd = safe_query(f"""
                SELECT event_type, count(*), max(created_at)
                FROM {tbl}
                WHERE created_at >= now() - interval '7 days'
                GROUP BY event_type
                ORDER BY count(*) DESC
                LIMIT 10
            """)
            if isinstance(bd, list) and bd:
                print(f"  -> {tbl} event_type breakdown (last 7d):")
                for r in bd:
                    print(f"    {(r[0] or '<null>')[:35]:35s} count={r[1]:>6} last={fmt_age(r[2], now)}")

    # ----- L3: Pattern population -----
    print_section("L3 — Pattern population")
    res = safe_query("""
        SELECT status, count(*) FROM trading_patterns GROUP BY status ORDER BY 2 DESC
    """)
    if isinstance(res, list):
        for r in res:
            print(f"  status={(r[0] or '<null>'):20s} count={r[1]}")
    else:
        print(f"  trading_patterns query failed: {res}")

    res = safe_query("""
        SELECT id, status, hit_count, win_rate, last_validated_at
        FROM trading_patterns
        WHERE status = 'promoted'
        ORDER BY id DESC
        LIMIT 10
    """)
    if isinstance(res, list):
        print(f"\n  promoted patterns (top 10 by id):")
        for r in res:
            print(f"    id={r[0]:>5} status={r[1]:12s} hits={r[2]} wr={r[3]} last_val={fmt_age(r[4], now) if r[4] else 'NULL'}")

    # last_validated breakdown
    res = safe_query("""
        SELECT date_trunc('day', last_validated_at) AS d, count(*)
        FROM trading_patterns
        WHERE status = 'promoted' AND last_validated_at IS NOT NULL
        GROUP BY d
        ORDER BY d DESC
        LIMIT 10
    """)
    if isinstance(res, list) and res:
        print(f"\n  promoted patterns by last_validated_at day:")
        for r in res:
            print(f"    {r[0]} count={r[1]}")

    # ----- L4: Snapshot freshness -----
    print_section("L4 — trading_snapshots freshness")
    res = safe_query("SELECT count(*), max(created_at) FROM trading_snapshots")
    if isinstance(res, list) and res:
        last_ts = res[0][1]
        print(f"  total={res[0][0]} last={last_ts} age={fmt_age(last_ts, now)}")
    r1h = safe_query("SELECT count(*) FROM trading_snapshots WHERE created_at >= now() - interval '1 hour'")
    r24h = safe_query("SELECT count(*) FROM trading_snapshots WHERE created_at >= now() - interval '24 hours'")
    if isinstance(r1h, list):
        print(f"  last 1h: {r1h[0][0]}")
    if isinstance(r24h, list):
        print(f"  last 24h: {r24h[0][0]}")

    res = safe_query("""
        SELECT ticker, max(created_at)
        FROM trading_snapshots
        GROUP BY ticker
        ORDER BY max(created_at) DESC
        LIMIT 10
    """)
    if isinstance(res, list):
        print(f"\n  top 10 freshest tickers:")
        for r in res:
            print(f"    {r[0]:10s} last={r[1]} age={fmt_age(r[1], now)}")

    # ----- L5: Autotrader cycle activity -----
    print_section("L5 — Autotrader cycle activity")
    res = safe_query("SELECT count(*), max(created_at) FROM trading_autotrader_runs")
    if isinstance(res, list) and res:
        last_ts = res[0][1]
        print(f"  total={res[0][0]} last={last_ts} age={fmt_age(last_ts, now)}")
    r1h = safe_query("SELECT count(*) FROM trading_autotrader_runs WHERE created_at >= now() - interval '1 hour'")
    r24h = safe_query("SELECT count(*) FROM trading_autotrader_runs WHERE created_at >= now() - interval '24 hours'")
    r7d = safe_query("SELECT count(*) FROM trading_autotrader_runs WHERE created_at >= now() - interval '7 days'")
    if isinstance(r1h, list):
        print(f"  last 1h:  {r1h[0][0]}")
    if isinstance(r24h, list):
        print(f"  last 24h: {r24h[0][0]}")
    if isinstance(r7d, list):
        print(f"  last 7d:  {r7d[0][0]}")

    # ----- L6: Gate distribution -----
    print_section("L6 — Gate decision/reason distribution (last 7d)")
    for sql_attempt in [
        """SELECT decision, reason, count(*) FROM trading_autotrader_runs
           WHERE created_at >= now() - interval '7 days'
           GROUP BY decision, reason ORDER BY 3 DESC LIMIT 30""",
        """SELECT reason, count(*) FROM trading_autotrader_runs
           WHERE created_at >= now() - interval '7 days'
           GROUP BY reason ORDER BY 2 DESC LIMIT 30""",
    ]:
        res = safe_query(sql_attempt)
        if isinstance(res, list) and res:
            for r in res:
                if len(r) == 3:
                    print(f"  decision={(r[0] or '<null>')[:18]:18s} reason={(r[1] or '<null>')[:38]:38s} count={r[2]}")
                else:
                    print(f"  reason={(r[0] or '<null>')[:50]:50s} count={r[1]}")
            break

    # ----- L7: Recent Trade rows -----
    print_section("L7 — Recent Trade rows")
    res = safe_query("SELECT count(*) FROM trading_management_envelopes WHERE entry_date >= now() - interval '24 hours'")
    if isinstance(res, list):
        print(f"  Trades placed last 24h: {res[0][0]}")
    res = safe_query("SELECT count(*) FROM trading_management_envelopes WHERE entry_date >= now() - interval '7 days'")
    if isinstance(res, list):
        print(f"  Trades placed last 7d:  {res[0][0]}")
    res = safe_query("""
        SELECT id, ticker, broker_source, status, entry_date
        FROM trading_management_envelopes
        ORDER BY entry_date DESC
        LIMIT 10
    """)
    if isinstance(res, list):
        print(f"\n  most recent 10 Trade rows:")
        for r in res:
            print(f"    id={r[0]:>5} {r[1]:10s} broker={r[2] or '<null>':12s} status={r[3]:12s} entry={r[4]}")

    # ----- L8: Market data freshness -----
    print_section("L8 — Market data table freshness")
    for tbl in ("market_data", "ticker_quotes", "trading_quotes",
                "polygon_quotes", "massive_quotes", "fast_snapshots",
                "fast_orderbook", "fast_alerts", "regime_snapshot",
                "ticker_regime", "breadth_snapshot",
                "fast_path_universe", "trading_universe"):
        res = safe_query(f"SELECT count(*), max(created_at) FROM {tbl}")
        if isinstance(res, list) and res and res[0][0]:
            last_ts = res[0][1]
            print(f"  {tbl:25s} count={res[0][0]:>10} last={last_ts} age={fmt_age(last_ts, now)}")

    print()
    print("=" * 78)
    print("  audit complete")
    print("=" * 78)


if __name__ == "__main__":
    main()
