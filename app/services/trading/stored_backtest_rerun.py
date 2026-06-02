"""Re-run stored BacktestResult rows with the same window/params as the saved row."""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def rerun_stored_backtest_by_id(db: Session, bt_id: int) -> dict[str, Any]:
    """Execute one stored-row rerun (same logic as ``POST .../backtest/{id}/rerun``).

    Returns a dict suitable for JSON: ``ok``, ``error`` (if failed), or merged backtest
    fields + ``backtest_id`` / ``ran_at`` on success.
    """
    from ...config import settings
    from ...models.trading import BacktestResult, TradingInsight, ScanPattern
    from ..backtest_service import (
        backtest_pattern,
        get_brain_backtest_window,
        save_backtest,
    )

    bt_row = (
        db.query(
            BacktestResult.user_id,
            BacktestResult.ticker,
            BacktestResult.related_insight_id,
            BacktestResult.scan_pattern_id,
            BacktestResult.params,
            BacktestResult.param_set_id,
        )
        .filter(BacktestResult.id == bt_id)
        .first()
    )
    bt = _stored_backtest_rerun_values(bt_row)
    if not bt:
        return {"ok": False, "error": "Backtest not found"}

    ins = (
        db.query(TradingInsight.user_id, TradingInsight.scan_pattern_id)
        .filter(TradingInsight.id == bt.related_insight_id)
        .first()
        if bt.related_insight_id
        else None
    )
    ins = _related_insight_rerun_values(ins)
    sp_id = bt.scan_pattern_id or (getattr(ins, "scan_pattern_id", None) if ins else None)
    if not sp_id:
        return {"ok": False, "error": "No ScanPattern linked to this backtest"}
    p = (
        db.query(ScanPattern.name, ScanPattern.rules_json, ScanPattern.exit_config, ScanPattern.timeframe)
        .filter(ScanPattern.id == int(sp_id))
        .first()
    )
    p = _scan_pattern_rerun_values(p)
    if not p:
        return {"ok": False, "error": "Pattern not found"}

    tf = getattr(p, "timeframe", "1d") or "1d"
    use_period, use_interval = get_brain_backtest_window(tf)
    ohlc_start: str | None = None
    ohlc_end: str | None = None
    from .backtest_param_sets import materialize_backtest_params

    pr = materialize_backtest_params(db, bt)
    if isinstance(pr, dict):
        if pr.get("interval"):
            use_interval = str(pr["interval"]).strip()
        if pr.get("period"):
            use_period = str(pr["period"]).strip()
        ctf, ctt = pr.get("chart_time_from"), pr.get("chart_time_to")
        if ctf is not None and ctt is not None:
            try:
                from datetime import datetime, timezone

                ohlc_start = datetime.fromtimestamp(
                    int(float(ctf)), tz=timezone.utc,
                ).strftime("%Y-%m-%d")
                ohlc_end = datetime.fromtimestamp(
                    int(float(ctt)), tz=timezone.utc,
                ).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                ohlc_start, ohlc_end = None, None

    result = backtest_pattern(
        ticker=bt.ticker,
        pattern_name=p.name,
        rules_json=p.rules_json,
        interval=use_interval,
        period=use_period,
        exit_config=getattr(p, "exit_config", None),
        cash=100_000.0,
        commission=float(settings.backtest_commission),
        spread=float(settings.backtest_spread),
        ohlc_start=ohlc_start,
        ohlc_end=ohlc_end,
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error", "backtest failed"),
            "ticker": bt.ticker,
            "backtest_id": bt_id,
        }

    uid = ins.user_id if ins else bt.user_id
    rec = save_backtest(
        db, uid, result,
        insight_id=bt.related_insight_id,
        scan_pattern_id=int(sp_id),
        backtest_row_id=int(bt_id),
    )
    ran_at = getattr(rec, "ran_at", None)
    out: dict[str, Any] = {
        **result,
        "ok": True,
        "backtest_id": rec.id,
        "ran_at": ran_at.isoformat() if ran_at else None,
    }
    return out


def _row_value(row: Any, field: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, (tuple, list)):
        return row[index] if len(row) > index else None
    return getattr(row, field, None)


def _stored_backtest_rerun_values(row: Any) -> Any:
    if row is None:
        return None
    if not isinstance(row, (tuple, list)):
        return row
    return SimpleNamespace(
        user_id=_row_value(row, "user_id", 0),
        ticker=_row_value(row, "ticker", 1),
        related_insight_id=_row_value(row, "related_insight_id", 2),
        scan_pattern_id=_row_value(row, "scan_pattern_id", 3),
        params=_row_value(row, "params", 4),
        param_set_id=_row_value(row, "param_set_id", 5),
    )


def _related_insight_rerun_values(row: Any) -> Any:
    if row is None:
        return None
    if not isinstance(row, (tuple, list)):
        return row
    return SimpleNamespace(
        user_id=_row_value(row, "user_id", 0),
        scan_pattern_id=_row_value(row, "scan_pattern_id", 1),
    )


def _scan_pattern_rerun_values(row: Any) -> Any:
    if row is None:
        return None
    if not isinstance(row, (tuple, list)):
        return row
    return SimpleNamespace(
        name=_row_value(row, "name", 0),
        rules_json=_row_value(row, "rules_json", 1),
        exit_config=_row_value(row, "exit_config", 2),
        timeframe=_row_value(row, "timeframe", 3),
    )


def collect_evidence_listed_backtest_ids(
    db: Session,
    insight_id: int,
    *,
    limit: int | None = None,
) -> tuple[list[int], str | None]:
    """IDs of deduped rows shown in Pattern Evidence (same filter as the panel)."""
    from ...models.trading import ScanPattern, TradingInsight
    from ...routers.trading_sub import ai as _brain_ai

    ins = (
        db.query(TradingInsight.id, TradingInsight.scan_pattern_id, TradingInsight.pattern_description)
        .filter(TradingInsight.id == int(insight_id))
        .first()
    )
    ins = _evidence_insight_values(ins)
    if not ins:
        return [], "Insight not found"
    sp_resolved = _scan_pattern_id_from_insight_row(db, ins, ScanPattern)
    desc = ins.pattern_description or ""
    univ = _brain_ai._evidence_backtest_asset_universe(
        db, desc, sp_resolved, insight_id=int(insight_id),
    )
    panel = _brain_ai._compute_deduped_backtest_win_stats(
        db,
        [int(insight_id)],
        asset_universe=univ,
        scan_pattern_id=sp_resolved,
    )
    rows = panel.get("backtests_out") or []
    ids: list[int] = []
    for b in rows:
        bid = b.get("id")
        if bid is None:
            continue
        try:
            ids.append(int(bid))
        except (TypeError, ValueError):
            continue
    if limit is not None and limit > 0:
        ids = ids[:limit]
    return ids, None


def _evidence_insight_values(row: Any) -> Any:
    if row is None:
        return None
    if not isinstance(row, (tuple, list)):
        return row
    return SimpleNamespace(
        id=_row_value(row, "id", 0),
        scan_pattern_id=_row_value(row, "scan_pattern_id", 1),
        pattern_description=_row_value(row, "pattern_description", 2),
    )


def _scan_pattern_id_from_insight_row(db: Session, insight: Any, scan_pattern_model: Any) -> int | None:
    sid = getattr(insight, "scan_pattern_id", None)
    if sid is None:
        return None
    try:
        sid_int = int(sid)
    except (TypeError, ValueError):
        return None
    row = db.query(scan_pattern_model.id).filter(scan_pattern_model.id == sid_int).first()
    resolved = _row_value(row, "id", 0)
    try:
        return int(resolved) if resolved is not None else None
    except (TypeError, ValueError):
        return None


def run_insight_stored_backtests_rerun_job(insight_id: int, *, limit: int | None = None) -> None:
    """Background thread: rerun every evidence-listed row for this insight, then sync tallies."""
    from ...db import SessionLocal
    from ...models.trading import TradingInsight
    from .insight_backtest_panel_sync import sync_insight_backtest_tallies_from_evidence_panel

    sdb = SessionLocal()
    try:
        ids, err = collect_evidence_listed_backtest_ids(sdb, insight_id, limit=limit)
        if err:
            logger.warning("[rerun_all_insight] insight=%s: %s", insight_id, err)
            return
        if not ids:
            logger.info("[rerun_all_insight] insight=%s: no rows to rerun", insight_id)
            return
        logger.info(
            "[rerun_all_insight] insight=%s starting %d stored backtest reruns",
            insight_id,
            len(ids),
        )
        ok = fail = 0
        for i, bid in enumerate(ids):
            r = rerun_stored_backtest_by_id(sdb, bid)
            if r.get("ok"):
                ok += 1
            else:
                fail += 1
                logger.warning(
                    "[rerun_all_insight] id=%s ticker=%s err=%s",
                    bid,
                    r.get("ticker"),
                    r.get("error"),
                )
            if (i + 1) % 25 == 0:
                logger.info(
                    "[rerun_all_insight] insight=%s progress %d/%d (ok=%d fail=%d)",
                    insight_id,
                    i + 1,
                    len(ids),
                    ok,
                    fail,
                )
        ins = sdb.get(TradingInsight, int(insight_id))
        if ins:
            try:
                sync_insight_backtest_tallies_from_evidence_panel(sdb, ins)
                sdb.commit()
            except Exception:
                logger.exception("[rerun_all_insight] tally sync failed insight=%s", insight_id)
                sdb.rollback()
        logger.info(
            "[rerun_all_insight] insight=%s done ok=%d fail=%d",
            insight_id,
            ok,
            fail,
        )
    except Exception:
        logger.exception("[rerun_all_insight] job failed insight=%s", insight_id)
    finally:
        # FIX 46 pattern (rollback before close).
        try:
            sdb.rollback()
        except Exception:
            pass
        sdb.close()


def spawn_insight_stored_backtests_rerun_thread(
    insight_id: int,
    *,
    limit: int | None = None,
) -> None:
    threading.Thread(
        target=run_insight_stored_backtests_rerun_job,
        args=(int(insight_id),),
        kwargs={"limit": limit},
        daemon=True,
        name=f"rerun-insight-bts-{insight_id}",
    ).start()
