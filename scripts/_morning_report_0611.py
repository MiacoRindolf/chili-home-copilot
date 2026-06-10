"""06-11 live-run morning report — fill-rate evidence vs the 06-10 replay benchmark.

Post-mortem follow-up #3 (#570 fill-rate) + the live-run scorecard in ONE command:

    docker exec chili-clean-recovery-scheduler python /app/scripts/_morning_report_0611.py
    (or from the host with PYTHONPATH + DATABASE_URL set)

Prints, for today (ET):
  * armed sessions (distinct symbols, venue split)
  * entry funnel: pending_place -> submits -> FILLS (adopted) + late-fill repoints
  * block breakdown (wide_bbo_spread / stale_bbo / halt_resume_cooldown / unresolved)
  * halt events (suspected/resumed) + any position_halted alarms
  * realized P/L today per symbol from the lane's OWN exits (managed, not manual)
  * the 06-10 replay benchmark to compare against:
      proxy +$1,669 | 3 fills (DOGZ, BATL, CNET) | 7 trades | 4W/3L
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402

BENCHMARK = "06-10 replay benchmark: +$1,669 proxy | 3 fills (DOGZ, BATL, CNET) | 7 trades | 4W/3L"


def _today_utc_bounds() -> tuple[str, str]:
    now_et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    day = now_et.strftime("%Y-%m-%d")
    return f"{day} 04:00:00-04", f"{day} 20:30:00-04"


def main() -> None:
    lo, hi = _today_utc_bounds()
    db = SessionLocal()
    try:
        def q(sql: str) -> list:
            return db.execute(text(sql), {"lo": lo, "hi": hi}).fetchall()

        print("=" * 72)
        print("MORNING LIVE-RUN REPORT —", datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET"))
        print(BENCHMARK)
        print("=" * 72)

        rows = q("""
            SELECT execution_family, count(DISTINCT symbol) AS syms, count(*) AS sessions
            FROM trading_automation_sessions
            WHERE mode='live' AND created_at BETWEEN :lo AND :hi
            GROUP BY 1 ORDER BY 3 DESC""")
        print("\n-- ARMED today --")
        for r in rows:
            print(f"  {r[0]}: {r[1]} symbols / {r[2]} sessions")

        rows = q("""
            SELECT e.event_type, count(*),
                   string_agg(DISTINCT s.symbol, ',' ORDER BY s.symbol) FILTER (WHERE e.event_type IN ('live_entry_filled','entry_late_fill_repointed'))
            FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id = e.session_id
            WHERE e.ts BETWEEN :lo AND :hi
              AND e.event_type IN ('live_entry_pending_place','live_entry_submitted','live_entry_filled',
                                   'entry_late_fill_repointed','entry_ack_timeout','live_entry_blocked_unresolved_orders')
            GROUP BY 1 ORDER BY 2 DESC""")
        print("\n-- ENTRY FUNNEL (pending_place -> submit -> FILL) --")
        for r in rows:
            extra = f"  [{r[2]}]" if r[2] else ""
            print(f"  {r[0]}: {r[1]}{extra}")

        rows = q("""
            SELECT coalesce(payload_json->>'reason','(other)'), count(*)
            FROM trading_automation_events
            WHERE ts BETWEEN :lo AND :hi AND event_type='live_blocked_by_risk'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
        print("\n-- BLOCKS --")
        for r in rows:
            print(f"  {r[0]}: {r[1]}")

        rows = q("""
            SELECT e.event_type, count(*), string_agg(DISTINCT s.symbol, ',')
            FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id = e.session_id
            WHERE e.ts BETWEEN :lo AND :hi
              AND e.event_type IN ('suspected_halt_detected','halt_resumed','position_halted')
            GROUP BY 1""")
        print("\n-- HALT AWARENESS --")
        for r in rows or [("(none)", 0, "")]:
            print(f"  {r[0]}: {r[1]} {('[' + (r[2] or '') + ']') if r[1] else ''}")

        rows = q("""
            SELECT s.symbol,
                   sum(((e.payload_json->>'return_bps')::float / 10000.0) * (e.payload_json->>'notional_basis_usd')::float)
            FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id = e.session_id
            WHERE e.ts BETWEEN :lo AND :hi
              AND e.event_type IN ('live_exit_filled','live_scaleout_filled')
              AND e.payload_json->>'return_bps' IS NOT NULL
              AND e.payload_json->>'notional_basis_usd' IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC NULLS LAST""")
        print("\n-- LANE-MANAGED realized P/L today (per symbol) --")
        total = 0.0
        for r in rows:
            v = float(r[1] or 0)
            total += v
            print(f"  {r[0]}: ${v:+,.0f}")
        print(f"  TOTAL: ${total:+,.0f}")
        print()
        print("Compare vs benchmark above. A/B reminder: scripts/_ab_helper.py SYMBOL VARIANT [--rh]")
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
