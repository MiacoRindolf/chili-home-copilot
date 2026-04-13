"""Authoritative emit helpers for work ledger (single boundary per event type where possible)."""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from sqlalchemy.orm import Session

from .ledger import enqueue_outcome_event, enqueue_work_event


def emit_backtest_requested_for_pattern(
    db: Session,
    scan_pattern_id: int,
    *,
    source: str,
) -> int | None:
    """Emit ``backtest_requested`` — authoritative when newly mined or operator-boosted."""
    dedupe_key = f"bt_req:pattern:{int(scan_pattern_id)}"
    return enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=dedupe_key,
        payload={"scan_pattern_id": int(scan_pattern_id), "source": source},
        lease_scope="backtest",
    )


def emit_promotion_changed_outcome(
    db: Session,
    *,
    scan_pattern_id: int,
    old_promotion_status: str,
    new_promotion_status: str,
    old_lifecycle_stage: str,
    new_lifecycle_stage: str,
    source: str,
    extra: Optional[dict[str, Any]] = None,
) -> int | None:
    """Emit ``promotion_changed`` outcome (audit)."""
    op = (old_promotion_status or "").strip() or "none"
    np = (new_promotion_status or "").strip() or "none"
    ol = (old_lifecycle_stage or "").strip() or "none"
    nl = (new_lifecycle_stage or "").strip() or "none"
    h = hashlib.sha256(f"{scan_pattern_id}|{op}|{np}|{ol}|{nl}".encode()).hexdigest()[:24]
    dedupe_key = f"promo:p{int(scan_pattern_id)}:{h}"
    payload = {
        "scan_pattern_id": int(scan_pattern_id),
        "old_promotion_status": op,
        "new_promotion_status": np,
        "old_lifecycle_stage": ol,
        "new_lifecycle_stage": nl,
        "source": source,
        **(extra or {}),
    }
    return enqueue_outcome_event(
        db,
        event_type="promotion_changed",
        dedupe_key=dedupe_key,
        payload=payload,
    )


def emit_market_snapshots_batch_outcome(
    db: Session,
    *,
    daily: int,
    intraday: int,
    universe_size: int,
    job_id: str | None = None,
    snapshot_driver: str | None = None,
) -> int | None:
    """Scheduler-owned snapshot batch completion (edge / source refresh)."""
    from datetime import datetime

    bucket = datetime.utcnow().strftime("%Y%m%d%H%M")
    jkey = (job_id or "").strip()
    dedupe_key = f"mkt_snap_batch:{jkey}" if jkey else f"mkt_snap_batch:{bucket}"
    return enqueue_outcome_event(
        db,
        event_type="market_snapshots_batch",
        dedupe_key=dedupe_key,
        payload={
            "snapshots_taken_daily": int(daily),
            "intraday_snapshots_taken": int(intraday),
            "universe_size": int(universe_size),
            "job_id": job_id,
            "snapshot_driver": snapshot_driver,
        },
    )


def emit_paper_trade_closed_outcome(
    db: Session,
    *,
    paper_trade_id: int,
    user_id: int | None,
    scan_pattern_id: int | None,
    ticker: str,
    pnl: float | None,
    exit_reason: str,
) -> int | None:
    dedupe_key = f"paper_closed:{int(paper_trade_id)}:{exit_reason[:32]}"
    return enqueue_outcome_event(
        db,
        event_type="paper_trade_closed",
        dedupe_key=dedupe_key,
        payload={
            "paper_trade_id": int(paper_trade_id),
            "user_id": user_id,
            "scan_pattern_id": scan_pattern_id,
            "ticker": ticker,
            "pnl": pnl,
            "exit_reason": exit_reason,
        },
    )


def emit_live_trade_closed_outcome(
    db: Session,
    *,
    trade_id: int,
    user_id: int | None,
    ticker: str,
    source: str,
    scan_pattern_id: int | None = None,
    extra: Optional[dict[str, Any]] = None,
) -> int | None:
    dedupe_key = f"live_closed:{int(trade_id)}:{source[:48]}"
    return enqueue_outcome_event(
        db,
        event_type="live_trade_closed",
        dedupe_key=dedupe_key,
        payload={
            "trade_id": int(trade_id),
            "user_id": user_id,
            "ticker": ticker,
            "source": source,
            "scan_pattern_id": scan_pattern_id,
            **(extra or {}),
        },
    )


def emit_broker_fill_closed_outcome(
    db: Session,
    *,
    trade_id: int,
    user_id: int | None,
    ticker: str,
    broker_source: str,
    source: str,
    scan_pattern_id: int | None = None,
) -> int | None:
    dedupe_key = f"broker_closed:{int(trade_id)}:{source[:40]}"
    return enqueue_outcome_event(
        db,
        event_type="broker_fill_closed",
        dedupe_key=dedupe_key,
        payload={
            "trade_id": int(trade_id),
            "user_id": user_id,
            "ticker": ticker,
            "broker_source": broker_source,
            "source": source,
            "scan_pattern_id": scan_pattern_id,
        },
    )


def emit_execution_quality_updated_outcome(
    db: Session,
    *,
    user_id: int | None,
    stats_summary: dict[str, Any],
    spread_hint: dict[str, Any],
    depromotion: dict[str, Any],
    parent_work_event_id: int | None = None,
) -> int | None:
    from datetime import datetime

    uid = int(user_id) if user_id is not None else 0
    hour = datetime.utcnow().strftime("%Y%m%d%H")
    dedupe_key = f"exec_quality:u{uid}:{hour}"
    return enqueue_outcome_event(
        db,
        event_type="execution_quality_updated",
        dedupe_key=dedupe_key,
        payload={
            "user_id": user_id,
            "stats_summary": stats_summary,
            "spread_hint": spread_hint,
            "depromotion": depromotion,
            "parent_work_event_id": parent_work_event_id,
        },
    )
