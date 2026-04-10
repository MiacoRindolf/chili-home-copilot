#!/usr/bin/env python3
"""Delete stored backtest rows for a Brain pattern/insight, then re-run fresh backtests.

Wipes ``trading_backtests`` for the chosen scope so the next run uses current rules,
OHLCV, and engine logic — then repopulates via ``smart_backtest_insight`` (insight path)
or ``execute_queue_backtest_for_pattern`` (pattern-only / all-patterns path).

**Default delete scope** when the insight has ``scan_pattern_id``: all rows with that
``scan_pattern_id`` (same pool Pattern Evidence aggregates across sibling insights).

Usage (repo root, conda env chili-env):

  python scripts/reset_insight_backtests_and_regenerate.py --insight-id 42 --dry-run
  python scripts/reset_insight_backtests_and_regenerate.py --insight-id 42 --tickers 60
  python scripts/reset_insight_backtests_and_regenerate.py --insight-id 42 --insight-rows-only
  python scripts/reset_insight_backtests_and_regenerate.py --pattern-id 12 --user-id 1

  python scripts/reset_insight_backtests_and_regenerate.py --all-patterns --dry-run
  python scripts/reset_insight_backtests_and_regenerate.py --all-patterns --user-id 1 --limit 20

Optional: drop simulated trade analytics rows for the pattern (no FK to backtests, but
stale ``backtest_result_id`` pointers otherwise):

  python scripts/reset_insight_backtests_and_regenerate.py --insight-id 42 --purge-pattern-trades
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("reset_insight_backtests")


def _pattern_ids_to_process(db, *, include_inactive: bool, limit: int | None) -> list[int]:
    from app.models.trading import ScanPattern

    q = db.query(ScanPattern.id).order_by(ScanPattern.id)
    if not include_inactive:
        q = q.filter(ScanPattern.active.is_(True))
    ids = [int(r[0]) for r in q.all()]
    if limit is not None and limit > 0:
        ids = ids[:limit]
    return ids


def _run_all_patterns(args, db) -> int:
    from app.models.trading import BacktestResult, PatternTradeRow, ScanPattern
    from app.services.trading.backtest_queue_worker import execute_queue_backtest_for_pattern

    _repo_root = Path(__file__).resolve().parents[1]
    _progress_log = _repo_root / "reset_all_patterns_bt.log"
    _log_fp = None

    def _both(line: str) -> None:
        print(line, flush=True)
        if _log_fp is not None:
            _log_fp.write(line + "\n")
            _log_fp.flush()

    ids = _pattern_ids_to_process(
        db, include_inactive=bool(args.include_inactive_patterns), limit=args.limit,
    )
    if not ids:
        print("No scan_patterns matched (check --include-inactive-patterns).", file=sys.stderr)
        return 1

    per_pattern_rows: list[tuple[int, int, int]] = []
    total_bt = 0
    total_pt = 0
    for pid in ids:
        n_bt = db.query(BacktestResult).filter(BacktestResult.scan_pattern_id == pid).count()
        n_pt = (
            db.query(PatternTradeRow)
            .filter(PatternTradeRow.scan_pattern_id == pid)
            .count()
            if args.purge_pattern_trades
            else 0
        )
        per_pattern_rows.append((pid, n_bt, n_pt))
        total_bt += n_bt
        total_pt += n_pt

    summary_line = (
        f"patterns={len(ids)} active_only={not args.include_inactive_patterns} "
        f"total_backtest_rows={total_bt}"
        + (f" total_pattern_trade_rows={total_pt}" if args.purge_pattern_trades else "")
    )
    if args.dry_run:
        print(summary_line, flush=True)
        for pid, n_bt, n_pt in per_pattern_rows:
            if n_bt or n_pt:
                extra = f" pattern_trades={n_pt}" if args.purge_pattern_trades else ""
                print(f"  pattern_id={pid} backtests={n_bt}{extra}")
        print("dry-run: no changes")
        return 0

    try:
        _log_fp = open(_progress_log, "a", encoding="utf-8")
        _both("--- reset_all_patterns start ---")
    except OSError as exc:
        logger.warning("Could not open %s: %s", _progress_log, exc)
    _both(summary_line)

    ok = 0
    fail = 0
    ran_sum = 0
    for i, pid in enumerate(ids, 1):
        pat = db.get(ScanPattern, pid)
        name = (getattr(pat, "name", None) or "")[:60] if pat else ""
        n_bt = db.query(BacktestResult).filter(BacktestResult.scan_pattern_id == pid).count()
        try:
            if n_bt:
                db.query(BacktestResult).filter(
                    BacktestResult.scan_pattern_id == pid
                ).delete(synchronize_session=False)
            if args.purge_pattern_trades:
                db.query(PatternTradeRow).filter(
                    PatternTradeRow.scan_pattern_id == pid
                ).delete(synchronize_session=False)
            db.commit()
            if args.no_regenerate:
                logger.info("[%s/%s] pattern_id=%s deleted_bt=%s (no regenerate)", i, len(ids), pid, n_bt)
                _both(f"[{i}/{len(ids)}] pattern_id={pid} deleted_bt={n_bt} skip_regen")
                ok += 1
                continue
            ran, _proc = execute_queue_backtest_for_pattern(pid, args.user_id)
            ran_sum += int(ran)
            logger.info(
                "[%s/%s] pattern_id=%s name=%r deleted_bt=%s backtests_run≈%s",
                i,
                len(ids),
                pid,
                name,
                n_bt,
                ran,
            )
            _both(f"[{i}/{len(ids)}] pattern_id={pid} deleted_bt={n_bt} backtests_run≈{ran}")
            ok += 1
        except Exception as exc:
            fail += 1
            db.rollback()
            logger.exception("[%s/%s] pattern_id=%s failed: %s", i, len(ids), pid, exc)
            err_line = f"[{i}/{len(ids)}] pattern_id={pid} ERROR: {exc}"
            print(err_line, file=sys.stderr)
            if _log_fp is not None:
                _log_fp.write(err_line + "\n")
                _log_fp.flush()

    _both(f"done ok={ok} fail={fail} sum_backtests_run≈{ran_sum}")
    if _log_fp is not None:
        try:
            _log_fp.close()
        except OSError:
            pass
    return 0 if fail == 0 else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--insight-id", type=int, metavar="N", help="TradingInsight.id (Brain card)")
    g.add_argument("--pattern-id", type=int, metavar="N", help="scan_patterns.id")
    g.add_argument(
        "--all-patterns",
        action="store_true",
        help="Every ScanPattern (default: active only): wipe + queue backtest each",
    )
    p.add_argument(
        "--include-inactive-patterns",
        action="store_true",
        help="With --all-patterns: include ScanPattern rows where active=False",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="With --all-patterns: process at most N patterns (by id, ascending)",
    )
    p.add_argument(
        "--insight-rows-only",
        action="store_true",
        help="With --insight-id: delete only rows with related_insight_id=N (not whole pattern)",
    )
    p.add_argument(
        "--tickers",
        type=int,
        default=40,
        metavar="N",
        help="Target tickers for smart_backtest_insight (default: 40)",
    )
    p.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Optional user id (pattern / all-patterns path; passed to queue executor)",
    )
    p.add_argument(
        "--no-regenerate",
        action="store_true",
        help="Only delete; do not run backtests",
    )
    p.add_argument(
        "--purge-pattern-trades",
        action="store_true",
        help="Also DELETE FROM trading_pattern_trades for this scan_pattern_id",
    )
    p.add_argument("--dry-run", action="store_true", help="Print counts only; no DB writes")
    args = p.parse_args()

    if args.insight_rows_only and args.pattern_id is not None:
        print("--insight-rows-only only applies with --insight-id", file=sys.stderr)
        return 2
    if args.insight_rows_only and args.all_patterns:
        print("--insight-rows-only does not apply with --all-patterns", file=sys.stderr)
        return 2
    if args.limit is not None and not args.all_patterns:
        print("--limit only applies with --all-patterns", file=sys.stderr)
        return 2
    if args.include_inactive_patterns and not args.all_patterns:
        print("--include-inactive-patterns only applies with --all-patterns", file=sys.stderr)
        return 2

    from app.db import SessionLocal
    from app.models.trading import BacktestResult, PatternTradeRow, ScanPattern, TradingInsight
    from app.services.trading.backtest_engine import hydrate_scan_pattern_rules_json, smart_backtest_insight
    from app.services.trading.insight_backtest_panel_sync import sync_insight_backtest_tallies_from_evidence_panel

    db = SessionLocal()
    try:
        if args.all_patterns:
            return _run_all_patterns(args, db)

        sp_id: int | None = None
        insight: TradingInsight | None = None

        if args.insight_id is not None:
            insight = db.get(TradingInsight, int(args.insight_id))
            if not insight:
                print(f"TradingInsight id={args.insight_id} not found", file=sys.stderr)
                return 1
            sp_id = getattr(insight, "scan_pattern_id", None)

        if args.pattern_id is not None:
            sp_id = int(args.pattern_id)
            pat = db.get(ScanPattern, sp_id)
            if not pat:
                print(f"ScanPattern id={sp_id} not found", file=sys.stderr)
                return 1

        # --- count / delete backtests ---
        if args.insight_id is not None and args.insight_rows_only:
            filt = BacktestResult.related_insight_id == int(args.insight_id)
            scope_desc = f"related_insight_id={args.insight_id}"
        elif sp_id is not None:
            filt = BacktestResult.scan_pattern_id == int(sp_id)
            scope_desc = f"scan_pattern_id={sp_id}"
        else:
            filt = BacktestResult.related_insight_id == int(args.insight_id)
            scope_desc = (
                f"related_insight_id={args.insight_id} (no scan_pattern_id on insight)"
            )

        n_bt = db.query(BacktestResult).filter(filt).count()
        n_pt = 0
        if args.purge_pattern_trades and sp_id is not None:
            n_pt = (
                db.query(PatternTradeRow)
                .filter(PatternTradeRow.scan_pattern_id == int(sp_id))
                .count()
            )

        print(f"BacktestResult rows matching {scope_desc}: {n_bt}")
        if args.purge_pattern_trades and sp_id is not None:
            print(f"PatternTradeRow rows for scan_pattern_id={sp_id}: {n_pt}")

        if args.dry_run:
            print("dry-run: no changes")
            return 0

        if n_bt:
            db.query(BacktestResult).filter(filt).delete(synchronize_session=False)
        if args.purge_pattern_trades and sp_id is not None and n_pt:
            db.query(PatternTradeRow).filter(
                PatternTradeRow.scan_pattern_id == int(sp_id)
            ).delete(synchronize_session=False)
        db.commit()
        print(f"Deleted {n_bt} backtest row(s)" + (f"; {n_pt} pattern trade row(s)" if n_pt else ""))

        if args.no_regenerate:
            print("--no-regenerate: skipping backtest run")
            return 0

        # --- regenerate ---
        if args.insight_id is not None:
            assert insight is not None
            db.refresh(insight)
            if sp_id is not None:
                pat = db.get(ScanPattern, int(sp_id))
                if pat:
                    hydrate_scan_pattern_rules_json(db, pat, insight)
                    db.refresh(pat)
                    db.commit()
            res = smart_backtest_insight(
                db,
                insight,
                target_tickers=max(1, int(args.tickers)),
                update_confidence=True,
            )
            try:
                sync_insight_backtest_tallies_from_evidence_panel(db, insight)
            except Exception as exc:
                print(f"warning: tally sync failed: {exc}", file=sys.stderr)
            db.commit()
            print(
                "smart_backtest_insight done: "
                f"backtests_run={res.get('backtests_run')} "
                f"wins={res.get('wins')} losses={res.get('losses')}"
            )
        else:
            from app.services.trading.backtest_queue_worker import execute_queue_backtest_for_pattern

            ran, _proc = execute_queue_backtest_for_pattern(int(sp_id), args.user_id)
            print(f"execute_queue_backtest_for_pattern done: backtests_run≈{ran}")

        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
