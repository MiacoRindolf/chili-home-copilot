"""Phase K - canonical ops health service.

Read-only aggregation of every Phase A-K substrate diagnostic summary
plus scheduler + governance state into a single snapshot dict used by
``GET /api/trading/brain/ops/health``.

Defensive: every upstream summary call is wrapped in ``try/except``
so one broken substrate does not take down the health endpoint.
Broken or disabled phases appear as ``present=False`` in the output
so operators still see the full per-phase ledger in one place.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from ...config import settings
from .ops_health_model import (
    OpsHealthSnapshotInput,
    PHASE_KEYS,
    compute_snapshot,
)

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return bool(getattr(settings, "brain_ops_health_enabled", True))


def _safe_call(
    fn: Callable[..., Mapping[str, Any]],
    *,
    key: str,
    **kwargs: Any,
) -> Mapping[str, Any] | None:
    try:
        result = fn(**kwargs)
        if isinstance(result, Mapping):
            return result
        return None
    except Exception as exc:  # noqa: BLE001 - operational tolerance
        logger.warning(
            "[ops_health_service] %s summary failed: %s", key, exc,
        )
        return None


def _gather_phase_summaries(
    db: Session,
    *,
    lookback_days: int,
) -> dict[str, Mapping[str, Any] | None]:
    """Call every phase's ``*_summary`` helper defensively."""
    lookback_hours = int(lookback_days) * 24
    summaries: dict[str, Mapping[str, Any] | None] = {}

    # Phase A - ledger
    try:
        from . import economic_ledger as _ledger
        summaries["ledger"] = _safe_call(
            _ledger.ledger_summary,
            key="ledger",
            db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["ledger"] = None

    # Phase B - exit engine (no summary function; synthesize minimal shape)
    summaries["exit_engine"] = _exit_engine_summary(
        db=db, lookback_hours=lookback_hours,
    )

    # Phase E - net edge
    try:
        from .net_edge_ranker import diagnostics as _ne_diag
        summaries["net_edge"] = _safe_call(
            _ne_diag, key="net_edge",
            db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["net_edge"] = None

    # Phase C - pit
    try:
        from . import pit_audit as _pit_audit
        summaries["pit"] = _safe_call(
            _pit_audit.audit_summary,
            key="pit", db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["pit"] = None

    # Phase D - triple barrier
    try:
        from . import triple_barrier_labeler as _tb
        summaries["triple_barrier"] = _safe_call(
            _tb.label_summary,
            key="triple_barrier", db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["triple_barrier"] = None

    # Phase F - execution cost
    try:
        from . import execution_cost_builder as _ec
        summaries["execution_cost"] = _safe_call(
            _ec.estimates_summary,
            key="execution_cost",
            db=db, stale_threshold_hours=lookback_hours,
        )
    except Exception:
        summaries["execution_cost"] = None

    # Phase F - venue truth
    try:
        from . import venue_truth as _vt
        summaries["venue_truth"] = _safe_call(
            _vt.venue_truth_summary,
            key="venue_truth",
            db=db, lookback_hours=lookback_hours, top_n=10,
        )
    except Exception:
        summaries["venue_truth"] = None

    # Phase G - bracket intent
    try:
        from .bracket_intent_writer import bracket_intent_summary
        summaries["bracket_intent"] = _safe_call(
            bracket_intent_summary,
            key="bracket_intent",
            db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["bracket_intent"] = None

    # Phase G - bracket reconciliation
    try:
        from .bracket_reconciliation_service import (
            bracket_reconciliation_summary,
        )
        summaries["bracket_reconciliation"] = _safe_call(
            bracket_reconciliation_summary,
            key="bracket_reconciliation",
            db=db, lookback_hours=lookback_hours, recent_sweeps=20,
        )
    except Exception:
        summaries["bracket_reconciliation"] = None

    # Phase H - position sizer
    try:
        from .position_sizer_writer import proposals_summary
        summaries["position_sizer"] = _safe_call(
            proposals_summary,
            key="position_sizer",
            db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["position_sizer"] = None

    # Phase I - risk dial
    try:
        from .risk_dial_service import dial_state_summary
        summaries["risk_dial"] = _safe_call(
            dial_state_summary,
            key="risk_dial",
            db=db, lookback_hours=lookback_hours,
        )
    except Exception:
        summaries["risk_dial"] = None

    # Phase I - capital reweight
    try:
        from .capital_reweight_service import sweep_summary
        summaries["capital_reweight"] = _safe_call(
            sweep_summary,
            key="capital_reweight",
            db=db, lookback_days=int(lookback_days),
        )
    except Exception:
        summaries["capital_reweight"] = None

    # Phase J - drift monitor
    try:
        from .drift_monitor_service import drift_summary
        summaries["drift_monitor"] = _safe_call(
            drift_summary,
            key="drift_monitor",
            db=db, lookback_days=int(lookback_days),
        )
    except Exception:
        summaries["drift_monitor"] = None

    # Phase J - recert queue
    try:
        from .recert_queue_service import recert_summary
        summaries["recert_queue"] = _safe_call(
            recert_summary,
            key="recert_queue",
            db=db, lookback_days=int(lookback_days),
        )
    except Exception:
        summaries["recert_queue"] = None

    # Phase K - divergence
    try:
        from .divergence_service import divergence_summary
        summaries["divergence"] = _safe_call(
            divergence_summary,
            key="divergence",
            db=db, lookback_days=int(lookback_days),
        )
    except Exception:
        summaries["divergence"] = None

    return summaries


def _exit_engine_summary(
    *, db: Session, lookback_hours: int,
) -> dict[str, Any] | None:
    """Synthesize a minimal frozen-shape summary for Phase B.

    The Phase B diagnostics endpoint inlines its query; there is no
    shared helper. We re-compute the same fields here using
    ``ExitParityLog``.
    """
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import func
        from ...models.trading import ExitParityLog

        since = datetime.utcnow() - timedelta(hours=int(lookback_hours))
        total = db.query(ExitParityLog).filter(
            ExitParityLog.created_at >= since,
        ).count()
        agree = db.query(ExitParityLog).filter(
            ExitParityLog.created_at >= since,
            ExitParityLog.agree_bool.is_(True),
        ).count()
        disagree = total - agree
        rate = 0.0 if total == 0 else disagree / total
        mode = str(getattr(settings, "brain_exit_engine_mode", "off") or "off")
        _ = func  # keep import usable if tests mock
        return {
            "mode": mode,
            "lookback_hours": int(lookback_hours),
            "total": int(total),
            "agree": int(agree),
            "disagree": int(disagree),
            "disagreement_rate": float(rate),
            "by_severity": {
                "red": 0,
                "yellow": int(disagree),
                "green": int(agree),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ops_health_service] exit_engine summary failed: %s", exc)
        return None


def _scheduler_info() -> Mapping[str, Any] | None:
    try:
        from .. import trading_scheduler as _sched
        info = _sched.get_scheduler_info()
        if isinstance(info, Mapping):
            return info
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ops_health_service] scheduler info failed: %s", exc)
    return None


def _governance_info() -> Mapping[str, Any] | None:
    try:
        from . import governance as _gov
        info = _gov.get_governance_dashboard()
        if isinstance(info, Mapping):
            return info
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ops_health_service] governance info failed: %s", exc)
    return None


def build_health_snapshot(
    db: Session,
    *,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """Top-level entry: build the full ops-health snapshot dict."""
    ld = int(
        lookback_days
        if lookback_days is not None
        else getattr(settings, "brain_ops_health_lookback_days", 14)
    )
    ld = max(1, min(180, ld))

    summaries = _gather_phase_summaries(db, lookback_days=ld)
    scheduler = _scheduler_info()
    governance = _governance_info()

    inp = OpsHealthSnapshotInput(
        phase_summaries=summaries,
        scheduler=scheduler,
        governance=governance,
        lookback_days=ld,
    )
    snap = compute_snapshot(inp)
    result = snap.to_dict()
    result["enabled"] = is_enabled()
    return result


__all__ = [
    "PHASE_KEYS",
    "build_health_snapshot",
    "is_enabled",
]
