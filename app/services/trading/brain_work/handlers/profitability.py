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


def _recert_reason_set(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


def _recert_reason_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hard_reason_still_unresolved(pattern: Any, reason: str, config: Any) -> bool:
    reason = str(reason or "").strip()
    if reason == "negative_oos_recert":
        oos_avg = _safe_float(getattr(pattern, "oos_avg_return_pct", None))
        if oos_avg is None:
            return True
        return oos_avg < float(getattr(config, "min_oos_avg_return_pct", 0.0) or 0.0)
    if reason == "negative_realized_ev":
        raw_avg = _safe_float(getattr(pattern, "raw_realized_avg_return_pct", None))
        if raw_avg is None:
            return True
        raw_n = _safe_int(getattr(pattern, "raw_realized_trade_count", None)) or 0
        payoff = _safe_float(getattr(pattern, "payoff_ratio", None))
        payoff_n = _safe_int(getattr(pattern, "payoff_ratio_n", None)) or 0
        payoff_protected = (
            payoff is not None
            and payoff >= 1.5
            and payoff_n >= int(getattr(config, "min_realized_trades", 5) or 5)
        )
        return raw_avg < 0.0 and not payoff_protected and raw_n > 0
    if reason in {
        "promotion_gate_not_currently_passed",
        "promotion_gate_not_passed",
        "promotion_gate_failed",
        "cpcv_promotion_gate_failed",
    }:
        return getattr(pattern, "promotion_gate_passed", None) is not True
    return True


def _refresh_pattern_recert_state(
    pattern: Any,
    hard_recert_reasons: set[str],
) -> dict[str, Any]:
    """Reconcile stale persisted recert flags with current evidence, without bypassing hard debt."""
    from app.config import settings
    from app.services.trading.alpha_portfolio_gate import (
        config_from_settings,
        recert_reasons_for_pattern,
    )

    config = config_from_settings(settings)
    previous = _recert_reason_list(getattr(pattern, "recert_reason", None))
    previous_set = set(previous)
    current = recert_reasons_for_pattern(pattern, config=config)
    current_set = set(current)

    if previous:
        refreshed: list[str] = []
        for reason in previous:
            if reason in current_set:
                refreshed.append(reason)
                continue
            if (
                reason in hard_recert_reasons
                and _hard_reason_still_unresolved(pattern, reason, config)
            ):
                refreshed.append(reason)

        for reason in current:
            if reason in hard_recert_reasons and reason not in refreshed:
                refreshed.append(reason)
        if not refreshed and current:
            refreshed = list(current)
    else:
        refreshed = list(current)

    changed = previous != refreshed
    pattern.recert_required = bool(refreshed)
    pattern.recert_reason = ",".join(refreshed) if refreshed else None
    return {
        "previous_recert_reasons": previous,
        "current_recert_reasons": current,
        "persisted_recert_reasons": refreshed,
        "cleared_recert_reasons": [r for r in previous if r not in set(refreshed)],
        "added_recert_reasons": [r for r in refreshed if r not in previous_set],
        "changed": changed,
    }


def _recert_rescue_diagnostic_status(
    *,
    recert_required: bool,
    recert_reason: Any,
    graduation_blocker: Any,
    hard_recert_reasons: set[str],
) -> tuple[str, str]:
    if not recert_required:
        return "not_recert_required", "no_recert_action_needed"

    blocker = str(graduation_blocker or "").strip().lower()
    reasons = _recert_reason_set(recert_reason)
    if reasons & hard_recert_reasons or blocker == "hard_recert_blocked":
        return "hard_blocked", "keep_live_blocked_until_hard_recert_clears"
    if blocker == "recert_blocked":
        return "soft_blocked", "complete_oos_recert_and_quality_refresh"
    return "needs_review", "inspect_recert_diagnostic"


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
        HARD_RECERT_REASONS,
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

    recert_reconcile = _refresh_pattern_recert_state(pattern, set(HARD_RECERT_REASONS))
    db.flush()

    reliability = compute_pattern_edge_reliability(
        db,
        pid,
        asset_class=_payload(ev).get("asset_class"),
        window_days=_window_days(ev),
    )
    reasons = reliability.get("recert_reason")
    reason_set = _recert_reason_set(reasons)
    hard_reasons = sorted(reason_set & HARD_RECERT_REASONS)
    soft_reasons = sorted(reason_set - set(hard_reasons))
    recert_required = bool(getattr(pattern, "recert_required", False))
    rescue_status, next_action = _recert_rescue_diagnostic_status(
        recert_required=recert_required,
        recert_reason=reasons,
        graduation_blocker=reliability.get("graduation_blocker"),
        hard_recert_reasons=set(HARD_RECERT_REASONS),
    )
    payload = {
        "scan_pattern_id": pid,
        "source": "recert_rescue_refresh",
        "recert_required": recert_required,
        "recert_reason": reasons,
        "recert_rescue_status": rescue_status,
        "hard_recert_reasons": hard_reasons,
        "soft_recert_reasons": soft_reasons,
        "recommended_next_action": next_action,
        "graduation_blocker": reliability.get("graduation_blocker"),
        "quality_recomputed": quality_recomputed,
        "recert_reconcile": recert_reconcile,
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
    variant_diag: dict[str, Any] = {}
    created_ids = fork_edge_learned_exit_variants(
        db,
        pid,
        edge_loss_report=report,
        diagnostics=variant_diag,
    )
    payload = {
        "scan_pattern_id": pid,
        "source": "exit_variant_refresh",
        "asset_class": _payload(ev).get("asset_class"),
        "cash_deployment_category": _payload(ev).get("cash_deployment_category"),
        "evidence_fingerprint": _payload(ev).get("evidence_fingerprint"),
        "graduation_blocker": _payload(ev).get("graduation_blocker"),
        "skip_reason": variant_diag.get("skip_reason"),
        "variant_label": variant_diag.get("variant_label"),
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
