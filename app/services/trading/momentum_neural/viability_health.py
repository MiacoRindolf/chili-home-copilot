"""Autopilot-facing health for the momentum viability pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainActivationEvent, BrainNodeState
from .pipeline import VIABILITY_NODE_ID
from .viability_scope import AGGREGATE_SYMBOL, VIABILITY_SCOPE_AGGREGATE, VIABILITY_SCOPE_SYMBOL


def _parse_iso_utc(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _last_tick_scope(local_state: dict[str, Any]) -> str | None:
    symbols = local_state.get("symbols_evaluated")
    if isinstance(symbols, list) and symbols:
        if len(symbols) == 1 and str(symbols[0]).strip().upper() == AGGREGATE_SYMBOL:
            return VIABILITY_SCOPE_AGGREGATE
        return VIABILITY_SCOPE_SYMBOL
    rows = local_state.get("viability_rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("scope"):
                return str(row.get("scope"))
    return None


def viability_pipeline_stale_after_seconds() -> float:
    return max(180.0, float(settings.chili_momentum_risk_viability_max_age_seconds) / 2.0)


def get_viability_pipeline_health(
    db: Session,
    *,
    stale_after_seconds: float | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    stale_after = float(stale_after_seconds or viability_pipeline_stale_after_seconds())
    try:
        pending_q = db.query(BrainActivationEvent).filter(
            BrainActivationEvent.cause == "momentum_context_refresh",
            BrainActivationEvent.status == "pending",
        )
        pending_refresh_count = int(pending_q.count())
        oldest_pending_refresh_at = pending_q.with_entities(func.min(BrainActivationEvent.created_at)).scalar()
    except Exception:
        pending_refresh_count = 0
        oldest_pending_refresh_at = None
    oldest_pending_refresh_age_seconds = None
    if oldest_pending_refresh_at is not None:
        oldest_pending_refresh_age_seconds = max(0.0, float((now - oldest_pending_refresh_at).total_seconds()))

    try:
        node = db.query(BrainNodeState).filter(BrainNodeState.node_id == VIABILITY_NODE_ID).one_or_none()
        local_state = node.local_state if node is not None and isinstance(node.local_state, dict) else {}
    except Exception:
        local_state = {}
    last_tick_utc = local_state.get("last_tick_utc") if isinstance(local_state.get("last_tick_utc"), str) else None
    last_tick_dt = _parse_iso_utc(last_tick_utc)
    last_tick_age_seconds = None
    if last_tick_dt is not None:
        ref = last_tick_dt.replace(tzinfo=None) if last_tick_dt.tzinfo else last_tick_dt
        last_tick_age_seconds = max(0.0, float((now - ref).total_seconds()))

    viability_pipeline_stale = bool(
        pending_refresh_count > 0
        and oldest_pending_refresh_age_seconds is not None
        and oldest_pending_refresh_age_seconds >= stale_after
    )

    return {
        "pending_refresh_count": pending_refresh_count,
        "oldest_pending_refresh_utc": oldest_pending_refresh_at.isoformat() if oldest_pending_refresh_at else None,
        "oldest_pending_refresh_age_seconds": oldest_pending_refresh_age_seconds,
        "last_viability_tick_utc": last_tick_utc,
        "last_viability_tick_age_seconds": last_tick_age_seconds,
        "last_viability_tick_scope": _last_tick_scope(local_state),
        "viability_pipeline_stale": viability_pipeline_stale,
        "stale_after_seconds": stale_after,
    }
