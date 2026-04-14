"""One-shot data repair based on diagnose_all_tables.py findings.

Fixes:
  1. NULL out dangling trading_pattern_trades.backtest_result_id (1.07M rows)
  2. NULL out dangling trading_breakout_alerts.scan_cycle_id (2,654 rows)
  3. NULL out dangling chat_messages.conversation_id (192 rows)
  4. NULL out dangling trading_learning_events.related_insight_id (148 rows)
  5. DELETE orphan pair_codes with user_id not in users (1 row)
  6. Fix scan_patterns.win_rate > 1 -- divide by 100 (59 rows)
  7. Fix scan_patterns.oos_win_rate > 1 -- divide by 100 (65 rows)
  8. Deduplicate trading_backtests by (strategy_name, ticker, scan_pattern_id) keeping newest (4,657 groups)

Usage:
  python scripts/repair_all_domains.py --dry-run   # preview counts only
  python scripts/repair_all_domains.py              # apply all fixes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ.setdefault("CHILI_MP_BACKTEST_CHILD", "")

from sqlalchemy import text
from app.db import engine


def repair_orphan_pattern_trades_backtest(conn, dry_run: bool) -> int:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM trading_pattern_trades t "
            "WHERE t.backtest_result_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM trading_backtests b WHERE b.id = t.backtest_result_id)"
        )
    ).scalar() or 0
    print(f"  [1] trading_pattern_trades.backtest_result_id orphans: {count}")
    if count and not dry_run:
        conn.execute(
            text(
                "UPDATE trading_pattern_trades SET backtest_result_id = NULL "
                "WHERE backtest_result_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM trading_backtests b WHERE b.id = trading_pattern_trades.backtest_result_id)"
            )
        )
        print(f"      -> NULLed {count} rows")
    return count


def repair_orphan_breakout_scan_cycle(conn, dry_run: bool) -> int:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM trading_breakout_alerts t "
            "WHERE t.scan_cycle_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM brain_batch_jobs b WHERE b.id = t.scan_cycle_id)"
        )
    ).scalar() or 0
    print(f"  [2] trading_breakout_alerts.scan_cycle_id orphans: {count}")
    if count and not dry_run:
        conn.execute(
            text(
                "UPDATE trading_breakout_alerts SET scan_cycle_id = NULL "
                "WHERE scan_cycle_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM brain_batch_jobs b WHERE b.id = trading_breakout_alerts.scan_cycle_id)"
            )
        )
        print(f"      -> NULLed {count} rows")
    return count


def repair_orphan_chat_messages_convo(conn, dry_run: bool) -> int:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM chat_messages m "
            "WHERE m.conversation_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = m.conversation_id)"
        )
    ).scalar() or 0
    print(f"  [3] chat_messages.conversation_id orphans: {count}")
    if count and not dry_run:
        conn.execute(
            text(
                "UPDATE chat_messages SET conversation_id = NULL "
                "WHERE conversation_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = chat_messages.conversation_id)"
            )
        )
        print(f"      -> NULLed {count} rows")
    return count


def repair_orphan_learning_events_insight(conn, dry_run: bool) -> int:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM trading_learning_events e "
            "WHERE e.related_insight_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM trading_insights i WHERE i.id = e.related_insight_id)"
        )
    ).scalar() or 0
    print(f"  [4] trading_learning_events.related_insight_id orphans: {count}")
    if count and not dry_run:
        conn.execute(
            text(
                "UPDATE trading_learning_events SET related_insight_id = NULL "
                "WHERE related_insight_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM trading_insights i WHERE i.id = trading_learning_events.related_insight_id)"
            )
        )
        print(f"      -> NULLed {count} rows")
    return count


def repair_orphan_pair_codes(conn, dry_run: bool) -> int:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM pair_codes p "
            "WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)"
        )
    ).scalar() or 0
    print(f"  [5] pair_codes with non-existent user_id: {count}")
    if count and not dry_run:
        conn.execute(
            text(
                "DELETE FROM pair_codes "
                "WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = pair_codes.user_id)"
            )
        )
        print(f"      -> DELETED {count} rows")
    return count


def repair_win_rate_scale(conn, dry_run: bool) -> int:
    wr = conn.execute(
        text("SELECT COUNT(*) FROM scan_patterns WHERE win_rate > 1")
    ).scalar() or 0
    print(f"  [6] scan_patterns.win_rate > 1 (percent scale): {wr}")
    if wr and not dry_run:
        conn.execute(text("UPDATE scan_patterns SET win_rate = win_rate / 100.0 WHERE win_rate > 1"))
        print(f"      -> Fixed {wr} rows (divided by 100)")

    oos = conn.execute(
        text("SELECT COUNT(*) FROM scan_patterns WHERE oos_win_rate > 1")
    ).scalar() or 0
    print(f"  [7] scan_patterns.oos_win_rate > 1 (percent scale): {oos}")
    if oos and not dry_run:
        conn.execute(text("UPDATE scan_patterns SET oos_win_rate = oos_win_rate / 100.0 WHERE oos_win_rate > 1"))
        print(f"      -> Fixed {oos} rows (divided by 100)")
    return wr + oos


def repair_duplicate_backtests(conn, dry_run: bool) -> int:
    dup_groups = conn.execute(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT strategy_name, ticker, scan_pattern_id "
            "  FROM trading_backtests "
            "  WHERE scan_pattern_id IS NOT NULL "
            "  GROUP BY strategy_name, ticker, scan_pattern_id HAVING COUNT(*) > 1"
            ") sub"
        )
    ).scalar() or 0

    total_excess = conn.execute(
        text(
            "SELECT COALESCE(SUM(c - 1), 0) FROM ("
            "  SELECT strategy_name, ticker, scan_pattern_id, COUNT(*) c "
            "  FROM trading_backtests "
            "  WHERE scan_pattern_id IS NOT NULL "
            "  GROUP BY strategy_name, ticker, scan_pattern_id HAVING COUNT(*) > 1"
            ") sub"
        )
    ).scalar() or 0

    print(f"  [8] Duplicate backtest groups: {dup_groups} ({total_excess} excess rows to remove)")
    if total_excess and not dry_run:
        conn.execute(
            text(
                "DELETE FROM trading_backtests WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY strategy_name, ticker, scan_pattern_id "
                "      ORDER BY ran_at DESC NULLS LAST, id DESC"
                "    ) rn "
                "    FROM trading_backtests WHERE scan_pattern_id IS NOT NULL"
                "  ) ranked WHERE rn > 1"
                ")"
            )
        )
        print(f"      -> DELETED {total_excess} duplicate rows (kept newest per group)")
    return total_excess


def main() -> int:
    parser = argparse.ArgumentParser(description="CHILI DB data repair")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    args = parser.parse_args()

    mode = "DRY RUN" if args.dry_run else "LIVE REPAIR"
    print(f"{'=' * 60}")
    print(f"  CHILI DB Repair — {mode}")
    print(f"{'=' * 60}")

    total_fixed = 0
    with engine.connect() as conn:
        total_fixed += repair_orphan_pattern_trades_backtest(conn, args.dry_run)
        total_fixed += repair_orphan_breakout_scan_cycle(conn, args.dry_run)
        total_fixed += repair_orphan_chat_messages_convo(conn, args.dry_run)
        total_fixed += repair_orphan_learning_events_insight(conn, args.dry_run)
        total_fixed += repair_orphan_pair_codes(conn, args.dry_run)
        total_fixed += repair_win_rate_scale(conn, args.dry_run)
        total_fixed += repair_duplicate_backtests(conn, args.dry_run)

        if not args.dry_run:
            conn.commit()
            print(f"\n  All changes committed.")
        else:
            conn.rollback()
            print(f"\n  Dry run — no changes made.")

    print(f"\n{'=' * 60}")
    print(f"  Total rows affected: {total_fixed}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
