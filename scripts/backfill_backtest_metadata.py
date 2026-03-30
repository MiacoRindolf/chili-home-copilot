"""
Backfill period/interval on stored BacktestResult rows, and optionally re-run
pattern backtests so stats match the current engine.

**Brain alignment:** ``period`` and ``interval`` always come from
``get_brain_backtest_window(ScanPattern.timeframe)``, i.e. the same
``get_backtest_params`` mapping used by ``smart_backtest_insight`` when no
custom period is passed (linked pattern + timeframe).

Usage (from project root):
  python scripts/backfill_backtest_metadata.py                    # dry-run counts
  python scripts/backfill_backtest_metadata.py --apply          # fill missing interval/period only
  python scripts/backfill_backtest_metadata.py --apply --brain  # force all rows → brain window (recommended)
  python scripts/backfill_backtest_metadata.py --rerun          # re-run backtests (slow); uses brain window

  python scripts/backfill_backtest_metadata.py --rerun --insight-id 42 --limit 20
  python scripts/backfill_backtest_metadata.py --rerun --workers 8 --limit 2000
  python scripts/backfill_backtest_metadata.py --rerun --workers 8 --offset 5000 --limit 2000  # resume next slice

Parallel reruns use a thread pool; each worker opens its own DB session (PostgreSQL via ``DATABASE_URL``).
Recompute insight win/loss once at the end.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.trading import BacktestResult, PatternTradeRow, ScanPattern, TradingInsight


def _resolve_scan_pattern_id(db: Session, bt: BacktestResult) -> int | None:
    if bt.scan_pattern_id:
        return int(bt.scan_pattern_id)
    if bt.related_insight_id:
        ins = db.query(TradingInsight).filter(TradingInsight.id == bt.related_insight_id).first()
        if ins and ins.scan_pattern_id:
            return int(ins.scan_pattern_id)
    return None


def _parse_params(bt: BacktestResult) -> dict:
    if not bt.params:
        return {}
    try:
        return json.loads(bt.params) if isinstance(bt.params, str) else dict(bt.params or {})
    except (json.JSONDecodeError, TypeError):
        return {}


def _params_json_brain_aligned(sp: ScanPattern, p: dict) -> str:
    """Stored params JSON with period/interval matching ``smart_backtest_insight``.

    Preserves ohlc_bars / chart_time_* / friction / OOS keys when present so
    we do not strip chart metadata on --apply --brain.
    """
    from app.services.backtest_service import get_brain_backtest_window

    tf = getattr(sp, "timeframe", "1d") or "1d"
    period, interval = get_brain_backtest_window(tf)
    merged = {**p, "period": period, "interval": interval}
    if "strategy_id" not in merged and p.get("strategy_id") is not None:
        merged["strategy_id"] = p.get("strategy_id")
    return json.dumps(merged)


def _params_need_brain_sync(bt: BacktestResult, sp: ScanPattern) -> bool:
    """True if stored period/interval differ from ``get_brain_backtest_window(sp.timeframe)``."""
    from app.services.backtest_service import get_brain_backtest_window

    p = _parse_params(bt)
    tf = getattr(sp, "timeframe", "1d") or "1d"
    period, interval = get_brain_backtest_window(tf)
    return p.get("period") != period or p.get("interval") != interval


def _merge_params_fill_missing(
    db: Session,
    bt: BacktestResult,
    *,
    patterns_by_id: dict[int, ScanPattern] | None = None,
) -> tuple[bool, str | None]:
    """Fill only missing interval/period from brain (legacy behavior)."""
    from app.services.backtest_service import get_brain_backtest_window

    sp_id = _resolve_scan_pattern_id(db, bt)
    if not sp_id:
        return False, None
    sp = patterns_by_id.get(sp_id) if patterns_by_id else None
    if sp is None:
        sp = db.query(ScanPattern).filter(ScanPattern.id == sp_id).first()
    if not sp:
        return False, None
    tf = getattr(sp, "timeframe", "1d") or "1d"
    period, interval = get_brain_backtest_window(tf)
    p = _parse_params(bt)
    changed = False
    if not p.get("interval"):
        p["interval"] = interval
        changed = True
    if not p.get("period"):
        p["period"] = period
        changed = True
    if not changed:
        return False, None
    return True, json.dumps(p)


def _recompute_insight_win_loss(db: Session, insight_id: int) -> None:
    """Match smart_backtest_insight recomputation from all linked rows with trades."""
    rows = (
        db.query(BacktestResult.return_pct, BacktestResult.trade_count)
        .filter(
            BacktestResult.related_insight_id == insight_id,
            BacktestResult.trade_count > 0,
        )
        .all()
    )
    if not rows:
        ins = db.query(TradingInsight).filter(TradingInsight.id == insight_id).first()
        if ins:
            ins.win_count = 0
            ins.loss_count = 0
        return
    wins = sum(1 for r, tc in rows if tc and (r or 0) > 0)
    losses = len(rows) - wins
    ins = db.query(TradingInsight).filter(TradingInsight.id == insight_id).first()
    if ins:
        ins.win_count = wins
        ins.loss_count = losses


def _rerun_one(db: Session, bt: BacktestResult) -> bool:
    from app.services.backtest_service import backtest_pattern, get_brain_backtest_window, save_backtest

    sp_id = _resolve_scan_pattern_id(db, bt)
    if not sp_id:
        logger.warning("  skip id=%s: no ScanPattern", bt.id)
        return False
    p = db.query(ScanPattern).filter(ScanPattern.id == sp_id).first()
    if not p:
        logger.warning("  skip id=%s: pattern %s missing", bt.id, sp_id)
        return False

    tf = getattr(p, "timeframe", "1d") or "1d"
    use_period, use_interval = get_brain_backtest_window(tf)

    db.query(PatternTradeRow).filter(PatternTradeRow.backtest_result_id == bt.id).delete()
    db.commit()

    result = backtest_pattern(
        ticker=bt.ticker,
        pattern_name=p.name,
        rules_json=p.rules_json,
        interval=use_interval,
        period=use_period,
        exit_config=getattr(p, "exit_config", None),
        cash=100_000.0,
        commission=0.001,
    )
    if not result.get("ok"):
        logger.warning("  id=%s backtest failed: %s", bt.id, result.get("error"))
        return False

    ins = None
    if bt.related_insight_id:
        ins = db.query(TradingInsight).filter(TradingInsight.id == bt.related_insight_id).first()
    uid = ins.user_id if ins else bt.user_id
    save_backtest(
        db,
        uid,
        result,
        insight_id=bt.related_insight_id,
        scan_pattern_id=int(sp_id),
    )
    return True


def _rerun_one_by_id(bt_id: int) -> tuple[bool, int | None]:
    """Run _rerun_one in an isolated session (safe for thread pool). Returns (ok, related_insight_id)."""
    db = SessionLocal()
    try:
        bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
        if not bt:
            logger.warning("  skip id=%s: row missing", bt_id)
            return False, None
        ok = _rerun_one(db, bt)
        iid = bt.related_insight_id if ok and bt.related_insight_id else None
        return ok, iid
    except Exception:
        logger.exception("  BacktestResult id=%s", bt_id)
        try:
            db.rollback()
        except Exception:
            pass
        return False, None
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill backtest params / optional brain-aligned rerun")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Update params JSON (use with --brain for full sync to Chili brain window)",
    )
    parser.add_argument(
        "--brain",
        action="store_true",
        help="With --apply: set period+interval from get_brain_backtest_window for every row with a ScanPattern",
    )
    parser.add_argument("--rerun", action="store_true", help="Re-run each pattern backtest (slow); brain window")
    parser.add_argument("--insight-id", type=int, default=None, help="Only BacktestResult rows for this insight")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N rows (apply/rerun only; for testing)",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        metavar="K",
        help="Skip first K rerun candidates (after --insight-id filter); use with --limit to resume batches",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="W",
        help="Rerun only: parallel threads (each uses its own DB session). Try 4–8 on PostgreSQL. Default 1.",
    )
    args = parser.parse_args()

    if args.rerun and args.apply:
        logger.error("Use either --apply or --rerun, not both.")
        sys.exit(1)

    db = SessionLocal()
    try:
        q = db.query(BacktestResult).order_by(BacktestResult.id)
        if args.insight_id is not None:
            q = q.filter(BacktestResult.related_insight_id == args.insight_id)
        rows: list[BacktestResult] = q.all()

        with_sp = [bt for bt in rows if _resolve_scan_pattern_id(db, bt)]

        need_params_fill: list[BacktestResult] = []
        for bt in rows:
            p = _parse_params(bt)
            if not p.get("interval") or not p.get("period"):
                sp_id = _resolve_scan_pattern_id(db, bt)
                if sp_id:
                    need_params_fill.append(bt)

        logger.info("Total BacktestResult rows (scope): %s", len(rows))
        logger.info("Rows with resolvable ScanPattern: %s", len(with_sp))
        logger.info("Rows missing interval and/or period (fill-only target): %s", len(need_params_fill))

        sp_ids_brain = {_resolve_scan_pattern_id(db, bt) for bt in with_sp}
        sp_ids_brain.discard(None)
        patterns_for_brain: dict[int, ScanPattern] = {}
        if sp_ids_brain:
            for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids_brain)).all():
                patterns_for_brain[int(sp.id)] = sp
        brain_drift = sum(
            1
            for bt in with_sp
            if (sp_id := _resolve_scan_pattern_id(db, bt))
            and (sp := patterns_for_brain.get(sp_id))
            and _params_need_brain_sync(bt, sp)
        )
        logger.info("Rows with period/interval drift from Chili brain window: %s", brain_drift)

        if not args.apply and not args.rerun:
            logger.info(
                "Dry run. Next: --apply (fill missing), --apply --brain (sync all to brain), or --rerun (clean stats).",
            )
            return

        if args.apply:
            if args.brain:
                n = 0
                for j, bt in enumerate(with_sp):
                    if args.limit is not None and j >= args.limit:
                        break
                    sp_id = _resolve_scan_pattern_id(db, bt)
                    if not sp_id:
                        continue
                    sp = patterns_for_brain.get(sp_id)
                    if not sp:
                        continue
                    if _params_need_brain_sync(bt, sp):
                        bt.params = _params_json_brain_aligned(sp, _parse_params(bt))
                        n += 1
                db.commit()
                logger.info("Brain-synced params on %s row(s).", n)
                return

            # fill missing only
            sp_ids = {_resolve_scan_pattern_id(db, bt) for bt in need_params_fill}
            sp_ids.discard(None)
            patterns_by_id = {}
            if sp_ids:
                for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids)).all():
                    patterns_by_id[int(sp.id)] = sp
            n = 0
            for j, bt in enumerate(need_params_fill):
                if args.limit is not None and j >= args.limit:
                    break
                changed, new_json = _merge_params_fill_missing(db, bt, patterns_by_id=patterns_by_id)
                if changed and new_json:
                    bt.params = new_json
                    n += 1
            db.commit()
            logger.info("Updated params (fill missing) on %s row(s).", n)
            return

        # --rerun
        rerun_candidates = list(with_sp)
        if args.offset:
            rerun_candidates = rerun_candidates[args.offset :]
        if args.limit is not None:
            rerun_candidates = rerun_candidates[: args.limit]
        total = len(rerun_candidates)
        ids = [bt.id for bt in rerun_candidates]
        tickers = {bt.id: bt.ticker for bt in rerun_candidates}

        if args.workers < 1:
            logger.error("--workers must be >= 1")
            sys.exit(1)

        if args.workers == 1:
            ok = 0
            fail = 0
            affected_insights: set[int] = set()
            for i, bt_id in enumerate(ids, 1):
                logger.info(
                    "[%s/%s] rerun BacktestResult id=%s %s",
                    i,
                    total,
                    bt_id,
                    tickers.get(bt_id, ""),
                )
                try:
                    success, iid = _rerun_one_by_id(bt_id)
                    if success:
                        ok += 1
                        if iid:
                            affected_insights.add(iid)
                    else:
                        fail += 1
                except Exception as e:
                    logger.exception("  error: %s", e)
                    fail += 1
        else:
            logger.info(
                "Rerunning %s rows with %s workers (offset=%s)...",
                total,
                args.workers,
                args.offset,
            )
            ok = 0
            fail = 0
            affected_insights: set[int] = set()
            lock = Lock()
            done = 0

            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(_rerun_one_by_id, bid): bid for bid in ids}
                for fut in as_completed(futures):
                    bt_id = futures[fut]
                    try:
                        success, iid = fut.result()
                        with lock:
                            done += 1
                            if success:
                                ok += 1
                                if iid:
                                    affected_insights.add(iid)
                            else:
                                fail += 1
                            if done % 25 == 0 or done == total:
                                logger.info(
                                    "Progress: %s/%s ok=%s fail=%s (last id=%s %s)",
                                    done,
                                    total,
                                    ok,
                                    fail,
                                    bt_id,
                                    tickers.get(bt_id, ""),
                                )
                    except Exception:
                        with lock:
                            done += 1
                            fail += 1
                        logger.exception("  future failed id=%s", bt_id)

        for iid in affected_insights:
            try:
                _recompute_insight_win_loss(db, iid)
            except Exception:
                pass
        db.commit()
        logger.info("Rerun finished: ok=%s fail=%s (insights recomputed: %s)", ok, fail, len(affected_insights))
    finally:
        db.close()


if __name__ == "__main__":
    main()
