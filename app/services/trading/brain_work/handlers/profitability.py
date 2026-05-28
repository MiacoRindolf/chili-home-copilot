"""Profitability evidence handlers for pilot-first candidate improvement.

Handlers here are deliberately conservative:
* edge reliability writes aggregate outcome snapshots only;
* recert rescue recomputes existing quality evidence but never clears recert;
* exit variant refresh delegates to existing ScanPattern evolution gates;
* provenance backfill writes diagnostics only.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:profitability]"


def _payload(ev: Any) -> dict[str, Any]:
    raw = getattr(ev, "payload", None)
    return raw if isinstance(raw, dict) else {}


def _pattern_id(ev: Any) -> int | None:
    raw = _payload(ev).get("scan_pattern_id")
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _window_days(ev: Any) -> int:
    try:
        return max(1, int(_payload(ev).get("window_days") or 30))
    except (TypeError, ValueError):
        return 30


def handle_edge_reliability_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Persist a rolling reliability snapshot and enqueue the next safe work item."""
    from app.services.trading.edge_reliability import (
        EDGE_RELIABILITY_REFRESH,
        emit_targeted_profitability_work,
        persist_edge_reliability_snapshot,
    )

    pid = _pattern_id(ev)
    if pid is None:
        raise ValueError("edge_reliability_refresh missing scan_pattern_id")
    row = persist_edge_reliability_snapshot(
        db,
        pid,
        asset_class=_payload(ev).get("asset_class"),
        window_days=_window_days(ev),
        source=str(_payload(ev).get("source") or EDGE_RELIABILITY_REFRESH),
        parent_event_id=int(getattr(ev, "id", 0) or 0),
    )
    recommended = str(row.get("recommended_work_event") or "")
    if recommended and recommended != EDGE_RELIABILITY_REFRESH:
        calibrated_ev = row.get("calibrated_ev_pct")
        try:
            evidence_value = max(0.0, float(calibrated_ev or 0.0)) * math.log1p(
                int(row.get("edge_eval_count") or 0)
                + int(row.get("closed_evidence_count") or 0)
            )
        except (TypeError, ValueError):
            evidence_value = 0.0
        emit_targeted_profitability_work(
            db,
            event_type=recommended,
            scan_pattern_id=pid,
            source="edge_reliability_snapshot",
            asset_class=row.get("slice_asset_class"),
            evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
            payload={
                "edge_snapshot_event_id": row.get("snapshot_event_id"),
                "asset_class": row.get("slice_asset_class"),
                "graduation_blocker": row.get("graduation_blocker"),
                "calibrated_ev_pct": row.get("calibrated_ev_pct"),
                "expected_evidence_value": round(evidence_value, 6),
            },
        )
    logger.info(
        "%s edge_reliability_refresh ev_id=%s pattern_id=%s blocker=%s",
        LOG_PREFIX,
        getattr(ev, "id", None),
        pid,
        row.get("graduation_blocker"),
    )


def handle_recert_rescue_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Refresh quality evidence for a recert-blocked pattern; never bypass recert."""
    from app.models.trading import ScanPattern
    from app.services.trading.edge_reliability import (
        RECERT_RESCUE_DIAGNOSTIC,
        compute_pattern_edge_reliability,
    )
    from app.services.trading.brain_work.ledger import enqueue_outcome_event

    pid = _pattern_id(ev)
    if pid is None:
        raise ValueError("recert_rescue_refresh missing scan_pattern_id")
    pattern = db.get(ScanPattern, pid)
    if pattern is None:
        raise ValueError(f"scan_pattern_id={pid} not found")

    quality_recomputed = False
    try:
        from app.services.trading.brain_work.handlers.quality_score import (
            _recompute_for_pattern,
        )

        quality_recomputed = _recompute_for_pattern(
            db,
            pid,
            source="recert_rescue_refresh",
            ev=ev,
        ) is not None
    except Exception:
        logger.debug("%s quality recompute skipped pattern_id=%s", LOG_PREFIX, pid, exc_info=True)

    reliability = compute_pattern_edge_reliability(
        db,
        pid,
        asset_class=_payload(ev).get("asset_class"),
        window_days=_window_days(ev),
    )
    reasons = reliability.get("recert_reason")
    payload = {
        "scan_pattern_id": pid,
        "source": "recert_rescue_refresh",
        "recert_required": bool(getattr(pattern, "recert_required", False)),
        "recert_reason": reasons,
        "graduation_blocker": reliability.get("graduation_blocker"),
        "quality_recomputed": quality_recomputed,
        "safe_to_bypass_live": False,
        "uses_existing_probation_only": True,
        "calibrated_ev_pct": reliability.get("calibrated_ev_pct"),
        "realized_ev_pct": reliability.get("realized_ev_pct"),
        "observed_at": datetime.utcnow().isoformat(),
    }
    enqueue_outcome_event(
        db,
        event_type=RECERT_RESCUE_DIAGNOSTIC,
        dedupe_key=f"{RECERT_RESCUE_DIAGNOSTIC}:p{pid}:{reliability.get('evidence_fingerprint')}",
        payload=payload,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )


def handle_exit_variant_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Ask existing edge-aware ScanPattern evolution for learned exit children."""
    from app.services.trading.brain_work.ledger import enqueue_outcome_event
    from app.services.trading.edge_reliability import EXIT_VARIANT_DIAGNOSTIC
    from app.services.trading.learning import (
        _edge_debt_loss_reports,
        fork_edge_learned_exit_variants,
    )

    pid = _pattern_id(ev)
    if pid is None:
        raise ValueError("exit_variant_refresh missing scan_pattern_id")
    report = _edge_debt_loss_reports(db, lookback_days=_window_days(ev)).get(pid)
    created_ids = fork_edge_learned_exit_variants(
        db,
        pid,
        edge_loss_report=report,
    )
    payload = {
        "scan_pattern_id": pid,
        "source": "exit_variant_refresh",
        "created_child_ids": created_ids,
        "created_count": len(created_ids),
        "loss_report": report,
        "shadow_only": True,
        "observed_at": datetime.utcnow().isoformat(),
    }
    key_basis = (report or {}).get("avg_expected_net_pct")
    enqueue_outcome_event(
        db,
        event_type=EXIT_VARIANT_DIAGNOSTIC,
        dedupe_key=f"{EXIT_VARIANT_DIAGNOSTIC}:p{pid}:{key_basis}:{len(created_ids)}",
        payload=payload,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )


def handle_provenance_backfill(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Record null-lineage short paper candidates for later provenance work."""
    from app.services.trading.brain_work.ledger import enqueue_outcome_event
    from app.services.trading.edge_reliability import (
        PROVENANCE_BACKFILL_DIAGNOSTIC,
        null_lineage_short_paper_candidates,
    )

    candidates = null_lineage_short_paper_candidates(
        db,
        window_days=_window_days(ev),
    )
    key = "none"
    if candidates:
        key = str(candidates[0].get("evidence_fingerprint") or "none")
    enqueue_outcome_event(
        db,
        event_type=PROVENANCE_BACKFILL_DIAGNOSTIC,
        dedupe_key=f"{PROVENANCE_BACKFILL_DIAGNOSTIC}:{key}",
        payload={
            "source": "provenance_backfill",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "research_only": True,
            "observed_at": datetime.utcnow().isoformat(),
        },
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )
