#!/usr/bin/env python3
"""One-shot idempotent repair: backtest win-rate scale (083), insight alignment (084), recompute aggregates.

Does not use the migration runner — safe when ``schema_version`` is out of sync with reality.

Scale fix: repeated ``/100`` while any rate is ``> 1`` (handles chained percent bugs), then NULLs any
value outside ``[0, 1]`` (clears NaN / garbage) so ORM diagnostics match SQL.

Usage (repo root, conda env chili-env):
  python scripts/one_shot_backtest_data_repair.py --dry-run
  python scripts/one_shot_backtest_data_repair.py
  python scripts/one_shot_backtest_data_repair.py --also-record-schema-version

For repopulating deleted backtest rows, use ``scripts/regenerate_pattern_backtests.py`` with explicit ids.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

# Must match app/migrations.py MIGRATIONS version_id strings exactly.
VERSION_083 = "083_backtest_win_rate_scale_cleanup"
VERSION_084 = "084_align_backtest_scan_pattern_from_insight"


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _column_names(engine, table: str) -> set[str]:
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    except Exception:
        return set()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Print counts only; no writes")
    p.add_argument("--skip-scale", action="store_true", help="Skip 083-style win_rate / oos_win_rate fix")
    p.add_argument("--skip-linkage", action="store_true", help="Skip 084-style scan_pattern_id alignment")
    p.add_argument("--skip-recompute", action="store_true", help="Skip scan_patterns + insight aggregates")
    p.add_argument(
        "--also-record-schema-version",
        action="store_true",
        help="INSERT 083/084 into schema_version ON CONFLICT DO NOTHING (match migrations list)",
    )
    args = p.parse_args()

    from app.db import SessionLocal, engine

    tables = _table_names(engine)

    if args.dry_run:
        print("[dry-run] No data will be modified.\n")

    with engine.begin() as conn:
        if "trading_backtests" not in tables:
            print("trading_backtests missing; nothing to do.")
            return 0

        bt_cols = _column_names(engine, "trading_backtests")

        if not args.skip_scale:
            if "win_rate" in bt_cols:
                n_wr = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM trading_backtests "
                        "WHERE win_rate IS NOT NULL AND win_rate > 1.0"
                    )
                ).scalar()
            else:
                n_wr = 0
            if "oos_win_rate" in bt_cols:
                n_oos = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM trading_backtests "
                        "WHERE oos_win_rate IS NOT NULL AND oos_win_rate > 1.0"
                    )
                ).scalar()
            else:
                n_oos = 0
            print(f"[scale] rows with win_rate > 1: {n_wr}; oos_win_rate > 1: {n_oos}")
            if not args.dry_run:
                # Repeat /100 until no row has rate > 1 (handles 55, 5500, etc. mis-scales).
                if "win_rate" in bt_cols:
                    total_wr = 0
                    for _ in range(12):
                        r = conn.execute(
                            text(
                                "UPDATE trading_backtests SET win_rate = win_rate / 100.0 "
                                "WHERE win_rate IS NOT NULL AND win_rate > 1.0"
                            )
                        )
                        total_wr += r.rowcount or 0
                        if (r.rowcount or 0) == 0:
                            break
                    print(f"[scale] win_rate cell updates (sum passes): {total_wr}")
                if "oos_win_rate" in bt_cols:
                    total_oos = 0
                    for _ in range(12):
                        r = conn.execute(
                            text(
                                "UPDATE trading_backtests SET oos_win_rate = oos_win_rate / 100.0 "
                                "WHERE oos_win_rate IS NOT NULL AND oos_win_rate > 1.0"
                            )
                        )
                        total_oos += r.rowcount or 0
                        if (r.rowcount or 0) == 0:
                            break
                    print(f"[scale] oos_win_rate cell updates (sum passes): {total_oos}")
                # NULL out non-finite or out-of-[0,1] rates (e.g. NaN still matches ORM `> 1` filters oddly).
                if "win_rate" in bt_cols:
                    r = conn.execute(
                        text(
                            "UPDATE trading_backtests SET win_rate = NULL "
                            "WHERE win_rate IS NOT NULL "
                            "AND NOT (win_rate >= 0 AND win_rate <= 1)"
                        )
                    )
                    print(f"[scale] win_rate nulled (invalid range): {r.rowcount or 0}")
                if "oos_win_rate" in bt_cols:
                    r = conn.execute(
                        text(
                            "UPDATE trading_backtests SET oos_win_rate = NULL "
                            "WHERE oos_win_rate IS NOT NULL "
                            "AND NOT (oos_win_rate >= 0 AND oos_win_rate <= 1)"
                        )
                    )
                    print(f"[scale] oos_win_rate nulled (invalid range): {r.rowcount or 0}")
                print("[scale] scale UPDATEs applied (transaction continues).")
        else:
            print("[scale] skipped.")

        if not args.skip_linkage:
            if (
                "trading_insights" not in tables
                or "scan_pattern_id" not in bt_cols
                or "related_insight_id" not in bt_cols
            ):
                print("[linkage] missing tables/columns; skipped.")
            else:
                ti_cols = _column_names(engine, "trading_insights")
                if "scan_pattern_id" not in ti_cols:
                    print("[linkage] trading_insights.scan_pattern_id missing; skipped.")
                else:
                    n_link = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM trading_backtests bt "
                            "JOIN trading_insights ti ON ti.id = bt.related_insight_id "
                            "WHERE ti.scan_pattern_id IS NOT NULL "
                            "AND (bt.scan_pattern_id IS NULL OR bt.scan_pattern_id != ti.scan_pattern_id)"
                        )
                    ).scalar()
                    print(f"[linkage] rows 084 would update: {n_link}")
                    if not args.dry_run:
                        conn.execute(
                            text(
                                "UPDATE trading_backtests bt "
                                "SET scan_pattern_id = ti.scan_pattern_id "
                                "FROM trading_insights ti "
                                "WHERE bt.related_insight_id = ti.id "
                                "  AND ti.scan_pattern_id IS NOT NULL "
                                "  AND (bt.scan_pattern_id IS NULL OR bt.scan_pattern_id != ti.scan_pattern_id)"
                            )
                        )
                        print("[linkage] linkage UPDATE applied (transaction continues).")
        else:
            print("[linkage] skipped.")

        if args.also_record_schema_version and not args.dry_run:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_version ("
                    "version_id TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            for vid in (VERSION_083, VERSION_084):
                conn.execute(
                    text(
                        "INSERT INTO schema_version (version_id) VALUES (:vid) "
                        "ON CONFLICT (version_id) DO NOTHING"
                    ),
                    {"vid": vid},
                )
            print(f"[schema_version] recorded (if absent): {VERSION_083}, {VERSION_084}")
        elif args.also_record_schema_version and args.dry_run:
            print("[schema_version] --also-record-schema-version ignored in --dry-run")

    if not args.skip_recompute and not args.dry_run:
        from app.services.trading.pattern_stats_recompute import (
            refresh_trading_insight_backtest_counts,
            recompute_scan_pattern_stats,
        )

        recompute_scan_pattern_stats(engine)
        print("[recompute] scan_patterns aggregates done.")
        db = SessionLocal()
        try:
            n = refresh_trading_insight_backtest_counts(db)
            print(f"[recompute] TradingInsight counts refreshed: {n} insights")
        finally:
            db.close()
    elif args.dry_run and not args.skip_recompute:
        print("[recompute] skipped in --dry-run (run without --dry-run to apply).")
    elif args.skip_recompute:
        print("[recompute] skipped.")

    print("\nDone. Suggested check: python scripts/diagnose_backtest_pattern_alignment.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
