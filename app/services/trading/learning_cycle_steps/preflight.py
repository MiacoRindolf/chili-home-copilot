"""Prescreen / scan counts / snapshot-universe hydration for ``run_learning_cycle``."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session

from ....models.trading import ScanResult


def load_prescreen_scan_and_universe(
    db: Session,
    user_id: int | None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Build initial ``report`` keys and the mining/snapshot ticker universe.

    Returns:
        report_updates: keys to merge into the cycle ``report`` dict
        top_tickers: universe for mining / downstream steps
        snap_drv: driver metadata from :func:`build_snapshot_ticker_universe`
    """
    from ..prescreen_job import count_active_global_candidates, get_latest_prescreen_summary
    from ..scanner import build_snapshot_ticker_universe

    summary = get_latest_prescreen_summary(db)
    report_updates: dict[str, Any] = {
        "prescreen_candidates": count_active_global_candidates(db),
        "prescreen_sources": summary.get("source_map") or {},
        "prescreen_snapshot_id": summary.get("snapshot_id"),
        "prescreen_fallback_inline": False,
    }
    cutoff = datetime.utcnow() - timedelta(hours=48)
    scan_n = int(
        db.query(sql_func.count(ScanResult.id))
        .filter(ScanResult.user_id == user_id)
        .filter(ScanResult.scanned_at >= cutoff)
        .scalar()
        or 0,
    )
    report_updates["tickers_scored"] = scan_n
    report_updates["tickers_scanned"] = scan_n

    top_tickers, snap_drv = build_snapshot_ticker_universe(db, user_id)
    report_updates["snapshot_driver"] = snap_drv.get("snapshot_driver")
    if snap_drv.get("fallback"):
        report_updates["snapshot_driver_fallback"] = snap_drv["fallback"]

    return report_updates, top_tickers, snap_drv
