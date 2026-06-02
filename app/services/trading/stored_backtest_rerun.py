"""Re-run stored BacktestResult rows with the same window/params as the saved row."""
from __future__ import annotations

import logging
import math
import threading
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_REPLAY_REQUIRED_PROVENANCE_FIELDS = (
    "provenance_status",
    "period",
    "interval",
    "ohlc_bars",
    "chart_time_from",
    "chart_time_to",
    "cash_used",
    "spread_used",
    "commission_used",
)


def _has_replay_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _finite_replay_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _stored_replay_provenance_failure(params: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(params, dict) or not params:
        return {
            "error": "stored_backtest_replay_provenance_incomplete",
            "missing_provenance_fields": ["params"],
            "provenance_status": "missing",
        }

    missing = [
        field
        for field in _REPLAY_REQUIRED_PROVENANCE_FIELDS
        if not _has_replay_value(params.get(field))
    ]
    status = str(params.get("provenance_status") or "").strip().lower()
    if status != "complete" or missing:
        return {
            "error": "stored_backtest_replay_provenance_incomplete",
            "missing_provenance_fields": missing,
            "provenance_status": status or ("incomplete" if missing else "unknown"),
        }

    numeric_fields = (
        "ohlc_bars",
        "chart_time_from",
        "chart_time_to",
        "cash_used",
        "spread_used",
        "commission_used",
    )
    numeric_values = {
        field: _finite_replay_float(params.get(field))
        for field in numeric_fields
    }
    invalid_numeric = [
        field
        for field, value in numeric_values.items()
        if value is None
    ]
    if invalid_numeric:
        return {
            "error": "stored_backtest_replay_provenance_invalid",
            "invalid_provenance_fields": invalid_numeric,
            "provenance_status": status or "invalid",
        }

    invalid_bounds: list[str] = []
    if (numeric_values["ohlc_bars"] or 0.0) < 30.0:
        invalid_bounds.append("ohlc_bars")
    if (numeric_values["cash_used"] or 0.0) <= 0.0:
        invalid_bounds.append("cash_used")
    if (numeric_values["spread_used"] or 0.0) < 0.0:
        invalid_bounds.append("spread_used")
    if (numeric_values["commission_used"] or 0.0) < 0.0:
        invalid_bounds.append("commission_used")
    if (numeric_values["chart_time_to"] or 0.0) <= (
        numeric_values["chart_time_from"] or 0.0
    ):
        invalid_bounds.extend(["chart_time_from", "chart_time_to"])
    if invalid_bounds:
        return {
            "error": "stored_backtest_replay_provenance_invalid",
            "invalid_provenance_fields": sorted(set(invalid_bounds)),
            "provenance_status": status or "invalid",
        }
    return None


def rerun_stored_backtest_by_id(db: Session, bt_id: int) -> dict[str, Any]:
    """Execute one stored-row rerun (same logic as ``POST .../backtest/{id}/rerun``).

    Returns a dict suitable for JSON: ``ok``, ``error`` (if failed), or merged backtest
    fields + ``backtest_id`` / ``ran_at`` on success.
    """
    from ...models.trading import BacktestResult, TradingInsight, ScanPattern
    from ..backtest_service import (
        backtest_pattern,
        save_backtest,
    )

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return {"ok": False, "error": "Backtest not found"}

    ins = (
        db.query(TradingInsight).filter(TradingInsight.id == bt.related_insight_id).first()
        if bt.related_insight_id
        else None
    )
    sp_id = bt.scan_pattern_id or (getattr(ins, "scan_pattern_id", None) if ins else None)
    if not sp_id:
        return {"ok": False, "error": "No ScanPattern linked to this backtest"}
    p = db.get(ScanPattern, int(sp_id))
    if not p:
        return {"ok": False, "error": "Pattern not found"}

    from .backtest_param_sets import materialize_backtest_params

    pr = materialize_backtest_params(db, bt)
    replay_failure = _stored_replay_provenance_failure(pr)
    if replay_failure:
        return {
            "ok": False,
            "ticker": bt.ticker,
            "backtest_id": bt_id,
            **replay_failure,
        }

    use_interval = str(pr["interval"]).strip()
    use_period = str(pr["period"]).strip()
    cash_used = _finite_replay_float(pr.get("cash_used"))
    spread_used = _finite_replay_float(pr.get("spread_used"))
    commission_used = _finite_replay_float(pr.get("commission_used"))
    try:
        from datetime import datetime, timezone

        ohlc_start = datetime.fromtimestamp(
            int(float(pr["chart_time_from"])), tz=timezone.utc,
        ).strftime("%Y-%m-%d")
        ohlc_end = datetime.fromtimestamp(
            int(float(pr["chart_time_to"])), tz=timezone.utc,
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return {
            "ok": False,
            "ticker": bt.ticker,
            "backtest_id": bt_id,
            "error": "stored_backtest_replay_provenance_invalid",
            "invalid_provenance_fields": ["chart_time_from", "chart_time_to"],
            "provenance_status": str(pr.get("provenance_status") or "invalid"),
        }

    result = backtest_pattern(
        ticker=bt.ticker,
        pattern_name=p.name,
        rules_json=p.rules_json,
        interval=use_interval,
        period=use_period,
        exit_config=getattr(p, "exit_config", None),
        cash=float(cash_used),
        commission=float(commission_used),
        spread=float(spread_used),
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


def collect_evidence_listed_backtest_ids(
    db: Session,
    insight_id: int,
    *,
    limit: int | None = None,
) -> tuple[list[int], str | None]:
    """IDs of deduped rows shown in Pattern Evidence (same filter as the panel)."""
    from ...models.trading import TradingInsight
    from ...routers.trading_sub import ai as _brain_ai

    ins = db.get(TradingInsight, int(insight_id))
    if not ins:
        return [], "Insight not found"
    sp_resolved = _brain_ai._resolve_scan_pattern_id_for_insight(db, ins)
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
