"""Authoritative emit helpers for work ledger (single boundary per event type where possible)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .ledger import enqueue_outcome_event, enqueue_work_event


def _safe_evidence_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _refresh_open_backtest_request_payload(
    db: Session,
    *,
    dedupe_key: str,
    source: str,
    payload: dict[str, Any],
) -> int | None:
    """Refresh queued duplicate backtest work with newer evidence context."""
    from ....models.trading import BrainWorkEvent

    row = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.dedupe_key == dedupe_key)
        .filter(BrainWorkEvent.status.in_(("pending", "retry_wait")))
        .order_by(BrainWorkEvent.created_at.asc())
        .first()
    )
    if row is None:
        return None

    base = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
    original_source = str(base.get("source") or source)
    merged = {**base, **payload}

    old_evidence = _safe_evidence_value(base.get("expected_evidence_value"))
    new_evidence = _safe_evidence_value(payload.get("expected_evidence_value"))
    if old_evidence is not None or new_evidence is not None:
        merged["expected_evidence_value"] = round(
            max(old_evidence or 0.0, new_evidence or 0.0),
            6,
        )

    merged["source"] = original_source
    if original_source != source:
        sources = {
            original_source,
            source,
            *[
                str(item)
                for item in (base.get("sources") or [])
                if str(item or "").strip()
            ],
        }
        merged["sources"] = sorted(sources)
        merged["latest_source"] = source

    row.payload = merged
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    return int(row.id)


def emit_backtest_requested_for_pattern(
    db: Session,
    scan_pattern_id: int,
    *,
    source: str,
    asset_class: str | None = None,
    expected_evidence_value: float | None = None,
    payload: dict[str, Any] | None = None,
) -> int | None:
    """Emit ``backtest_requested`` — authoritative when newly mined or operator-boosted."""
    dedupe_key = f"bt_req:pattern:{int(scan_pattern_id)}"
    payload_dict = dict(payload or {})
    payload_dict["scan_pattern_id"] = int(scan_pattern_id)
    payload_dict["source"] = source
    if asset_class:
        payload_dict["asset_class"] = str(asset_class)
    evidence_value = _safe_evidence_value(expected_evidence_value)
    if evidence_value is not None:
        payload_dict["expected_evidence_value"] = round(evidence_value, 6)
    event_id = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=dedupe_key,
        payload=payload_dict,
        lease_scope="backtest",
    )
    if event_id is not None:
        return event_id
    if len(payload_dict) <= 2:
        return None
    return _refresh_open_backtest_request_payload(
        db,
        dedupe_key=dedupe_key,
        source=source,
        payload=payload_dict,
    )


def emit_edge_reliability_refresh_requested(
    db: Session,
    scan_pattern_id: int,
    *,
    source: str,
    asset_class: str | None = None,
    window_days: int = 30,
    evidence_fingerprint: str | None = None,
) -> int | None:
    """Emit edge reliability work for one ScanPattern.

    The handler writes aggregate diagnostics only; it never promotes or
    submits broker orders.
    """
    from ..edge_reliability import emit_edge_reliability_refresh_requested as _emit

    return _emit(
        db,
        int(scan_pattern_id),
        source=source,
        asset_class=asset_class,
        window_days=window_days,
        evidence_fingerprint=evidence_fingerprint,
    )


def emit_profitability_followup_requested(
    db: Session,
    *,
    event_type: str,
    scan_pattern_id: int | None,
    source: str,
    asset_class: str | None = None,
    evidence_fingerprint: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int | None:
    """Emit targeted profitability work using the shared edge lease scope."""
    from ..edge_reliability import emit_targeted_profitability_work

    return emit_targeted_profitability_work(
        db,
        event_type=event_type,
        scan_pattern_id=scan_pattern_id,
        source=source,
        asset_class=asset_class,
        evidence_fingerprint=evidence_fingerprint,
        payload=payload,
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
        claimable=False,
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
    now = datetime.utcnow()
    bucket = now.strftime("%Y%m%d%H%M")
    jkey = (job_id or "").strip()
    dedupe_key = f"mkt_snap_batch:{jkey}" if jkey else f"mkt_snap_batch:{bucket}"
    coalesced_count = _retire_obsolete_market_snapshot_batches(
        db,
        now=now,
        newest_job_id=jkey or bucket,
    )
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
            "coalesced_obsolete_batches": coalesced_count,
        },
    )


def _retire_obsolete_market_snapshot_batches(
    db: Session,
    *,
    now: datetime,
    newest_job_id: str,
) -> int:
    """Mark stale pending snapshot outcomes done; newer batches supersede them."""
    try:
        from app.config import settings

        grace_seconds = int(
            getattr(settings, "brain_mine_handler_obsolete_event_grace_seconds", 900)
        )
    except Exception:
        grace_seconds = 900
    cutoff = now - timedelta(seconds=max(0, grace_seconds))
    result = db.execute(
        text(
            """
            UPDATE brain_work_events
               SET status = 'done',
                   processed_at = COALESCE(processed_at, :now),
                   lease_holder = NULL,
                   lease_expires_at = NULL,
                   updated_at = :now,
                   payload = COALESCE(payload, '{}'::jsonb)
                             || jsonb_build_object(
                                  'coalesced_by_market_snapshot_batch', true,
                                  'coalesced_by_job_id', :newest_job_id,
                                  'coalesced_at', :now_iso
                                )
             WHERE domain = 'trading'
               AND event_kind = 'outcome'
               AND event_type = 'market_snapshots_batch'
               AND status IN ('pending', 'retry_wait')
               AND created_at < :cutoff
            """
        ),
        {
            "now": now,
            "now_iso": now.isoformat(),
            "cutoff": cutoff,
            "newest_job_id": newest_job_id,
        },
    )
    return int(result.rowcount or 0)


def emit_paper_trade_closed_outcome(
    db: Session,
    *,
    paper_trade_id: int,
    user_id: int | None,
    scan_pattern_id: int | None,
    ticker: str,
    pnl: float | None,
    exit_reason: str,
    extra: Optional[dict[str, Any]] = None,
) -> int | None:
    dedupe_key = f"paper_closed:{int(paper_trade_id)}:{exit_reason[:32]}"
    payload = {
        "paper_trade_id": int(paper_trade_id),
        "user_id": user_id,
        "scan_pattern_id": scan_pattern_id,
        "ticker": ticker,
        "pnl": pnl,
        "exit_reason": exit_reason,
    }
    if extra:
        for key, value in extra.items():
            if key not in payload:
                payload[key] = value
    return enqueue_outcome_event(
        db,
        event_type="paper_trade_closed",
        dedupe_key=dedupe_key,
        payload=payload,
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
    extra: Optional[dict[str, Any]] = None,
) -> int | None:
    dedupe_key = f"broker_closed:{int(trade_id)}:{source[:40]}"
    base = {
        "trade_id": int(trade_id),
        "user_id": user_id,
        "ticker": ticker,
        "broker_source": broker_source,
        "source": source,
        "scan_pattern_id": scan_pattern_id,
    }
    return enqueue_outcome_event(
        db,
        event_type="broker_fill_closed",
        dedupe_key=dedupe_key,
        payload={**base, **(extra or {})},
    )


def emit_backtest_completed_outcome(
    db: Session,
    *,
    scan_pattern_id: int,
    user_id: int | None = None,
    backtests_run: int = 0,
    win_rate: float | None = None,
    avg_return: float | None = None,
    extra: Optional[dict[str, Any]] = None,
) -> int | None:
    """f-fix-backtest-completed-emitter (2026-05-05): emit when FIX 34's
    independent fast_backtest loop finishes a pattern's backtest.

    Subscribed by ``handlers/cpcv_gate.py::handle_backtest_completed``,
    which runs the CPCV promotion gate and sets ``lifecycle_stage``.
    Pre-fix, FIX 34's loop bypassed the event path entirely -- backtests
    ran (45k+ parity rows / 5min observed in the cycle-kill smoke), but
    cpcv_gate never got called.

    Dedup is per pattern_id + a coarse minute bucket so rapid-fire
    queue churn on the same pattern doesn't flood the event ledger
    (cpcv_gate is idempotent at the run-level so a missed dup is
    harmless).
    """
    from datetime import datetime

    bucket = datetime.utcnow().strftime("%Y%m%d%H%M")
    dedupe_key = f"bt_completed:{int(scan_pattern_id)}:{bucket}"
    payload: dict[str, Any] = {
        "scan_pattern_id": int(scan_pattern_id),
        "user_id": user_id,
        "backtests_run": int(backtests_run),
        "win_rate": win_rate,
        "avg_return": avg_return,
    }
    if extra:
        payload.update(extra)
    return enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key=dedupe_key,
        payload=payload,
    )


def emit_execution_quality_updated_outcome(
    db: Session,
    *,
    user_id: int | None,
    stats_summary: dict[str, Any],
    spread_hint: dict[str, Any],
    depromotion: dict[str, Any],
    parent_work_event_id: int | None = None,
    attribution_summary: Optional[dict[str, Any]] = None,
) -> int | None:
    from datetime import datetime

    uid = int(user_id) if user_id is not None else 0
    hour = datetime.utcnow().strftime("%Y%m%d%H")
    dedupe_key = f"exec_quality:u{uid}:{hour}"
    pl: dict[str, Any] = {
        "user_id": user_id,
        "stats_summary": stats_summary,
        "spread_hint": spread_hint,
        "depromotion": depromotion,
        "parent_work_event_id": parent_work_event_id,
    }
    if attribution_summary:
        pl["attribution_summary"] = attribution_summary
    return enqueue_outcome_event(
        db,
        event_type="execution_quality_updated",
        dedupe_key=dedupe_key,
        payload=pl,
        claimable=False,
    )
