"""Neural-owned evolution trace + closed-loop outcome ingestion (Phase 9; no learning-cycle)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, MomentumSymbolViability
from .strategy_params import params_signature, refine_strategy_params
from ..brain_neural_mesh.repository import get_or_create_state

EVOLUTION_NODE_ID = "nm_momentum_evolution_trace"
_MAX_TRACE = 24
_FEEDBACK_VERSION = 1

# Decay: weight multiplier ~= exp(-age_days / TAU_DAYS)
_TAU_DAYS = 14.0
# Max |delta| applied to viability_score per ingest (protect base engine)
_MAX_VIABILITY_DELTA = 0.03
# Minimum live samples before full live weighting kick-in
_LIVE_WEIGHT_FULL_N = 3
_REFINEMENT_MIN_OUTCOMES = 5
_REFINEMENT_LOOKBACK_DAYS = 30


def record_evolution_trace(
    db: Session,
    *,
    snapshot: dict[str, Any],
    graph_version: int = 1,
) -> None:
    """Append a compact tick snapshot to the evolution observer node's local_state."""
    _ = graph_version
    st = get_or_create_state(db, EVOLUTION_NODE_ID)
    ls = dict(st.local_state) if isinstance(st.local_state, dict) else {}
    trace = list(ls.get("trace") or [])
    trace.append(
        {
            "at_utc": datetime.utcnow().isoformat(),
            "top_family": snapshot.get("top_family_id"),
            "top_viability": snapshot.get("top_viability"),
            "regime_session": snapshot.get("session_label"),
        }
    )
    ls["trace"] = trace[-_MAX_TRACE:]
    ls["momentum_neural_version"] = 1
    st.local_state = ls
    st.updated_at = datetime.utcnow()


def _parse_terminal_at(row: MomentumAutomationOutcome) -> datetime:
    t = row.terminal_at
    if t.tzinfo is not None:
        return t.replace(tzinfo=None)
    return t


def compute_session_evidence_weight(db: Session, extracted: dict[str, Any]) -> float:
    """Simple transparent weight: recency decay + modest live boost when enough live samples exist."""
    try:
        terminal_at = datetime.fromisoformat(str(extracted["terminal_at_utc"]).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except Exception:
        terminal_at = datetime.utcnow()
    age_days = max(0.0, (datetime.utcnow() - terminal_at).total_seconds() / 86400.0)
    decay = math.exp(-age_days / _TAU_DAYS)
    base = 1.0 * decay
    mode = str(extracted.get("mode") or "paper").lower()
    vid = int(extracted.get("variant_id") or 0)
    if mode == "live" and vid > 0:
        since = datetime.utcnow() - timedelta(days=30)
        n_live = (
            db.query(func.count(MomentumAutomationOutcome.id))
            .filter(
                MomentumAutomationOutcome.variant_id == vid,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.terminal_at >= since,
            )
            .scalar()
        )
        n_live = int(n_live or 0)
        if n_live >= _LIVE_WEIGHT_FULL_N:
            base *= 1.25
        else:
            base *= min(1.0, 0.5 + 0.15 * max(0, n_live))
    # Negative live outcomes: slightly heavier penalty channel in viability apply, not global weight crush
    oc = str(extracted.get("outcome_class") or "")
    rp = extracted.get("return_bps")
    if mode == "live" and rp is not None and float(rp) < -30:
        base *= 1.1
    if oc in ("error_exit", "governance_exit", "risk_block", "stale_data_abort"):
        base *= 0.85
    return float(min(2.0, max(0.05, base)))


def ingest_session_outcome(db: Session, outcome_row: MomentumAutomationOutcome) -> None:
    """Apply one durable outcome into evolution trace + viability feedback channel."""
    record_feedback_ingestion_trace(
        db,
        {
            "session_id": outcome_row.session_id,
            "variant_id": outcome_row.variant_id,
            "symbol": outcome_row.symbol,
            "mode": outcome_row.mode,
            "outcome_class": outcome_row.outcome_class,
            "return_bps": outcome_row.return_bps,
            "realized_pnl_usd": outcome_row.realized_pnl_usd,
            "weight": outcome_row.evidence_weight,
        },
    )
    if outcome_row.contributes_to_evolution:
        apply_outcome_feedback_to_viability(db, outcome_row)
        maybe_publish_refined_variant(db, variant_id=int(outcome_row.variant_id))


def record_feedback_ingestion_trace(db: Session, payload: dict[str, Any]) -> None:
    st = get_or_create_state(db, EVOLUTION_NODE_ID)
    ls = dict(st.local_state) if isinstance(st.local_state, dict) else {}
    fb = list(ls.get("feedback_trace") or [])
    fb.append({"at_utc": datetime.utcnow().isoformat(), **payload})
    ls["feedback_trace"] = fb[-_MAX_TRACE:]
    ls["latest_feedback_at_utc"] = datetime.utcnow().isoformat()
    ls["momentum_neural_version"] = max(int(ls.get("momentum_neural_version") or 1), _FEEDBACK_VERSION)
    st.local_state = ls
    st.updated_at = datetime.utcnow()


def aggregate_recent_outcomes_for_variant(
    db: Session,
    *,
    variant_id: int,
    days: int = 14,
    mode: Optional[str] = None,
) -> dict[str, Any]:
    """Weighted aggregates for one variant (optional mode filter: paper | live)."""
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)))
    q = db.query(MomentumAutomationOutcome).filter(
        MomentumAutomationOutcome.variant_id == int(variant_id),
        MomentumAutomationOutcome.terminal_at >= since,
        MomentumAutomationOutcome.contributes_to_evolution.is_(True),
    )
    if mode:
        q = q.filter(MomentumAutomationOutcome.mode == mode.lower().strip())
    rows = q.all()
    return _aggregate_rows(rows)


def aggregate_recent_outcomes_for_symbol_variant(
    db: Session,
    *,
    symbol: str,
    variant_id: int,
    days: int = 14,
) -> dict[str, Any]:
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)))
    rows = (
        db.query(MomentumAutomationOutcome)
        .filter(
            MomentumAutomationOutcome.symbol == symbol.strip().upper(),
            MomentumAutomationOutcome.variant_id == int(variant_id),
            MomentumAutomationOutcome.terminal_at >= since,
            MomentumAutomationOutcome.contributes_to_evolution.is_(True),
        )
        .all()
    )
    return _aggregate_rows(rows)


def paper_vs_live_performance_slices(
    db: Session,
    *,
    variant_id: int,
    days: int = 14,
) -> dict[str, Any]:
    paper = aggregate_recent_outcomes_for_variant(db, variant_id=variant_id, days=days, mode="paper")
    live = aggregate_recent_outcomes_for_variant(db, variant_id=variant_id, days=days, mode="live")
    return {
        "variant_id": int(variant_id),
        "window_days": int(days),
        "paper": paper,
        "live": live,
        "live_sample_caution": live["n"] < _LIVE_WEIGHT_FULL_N,
    }


def _aggregate_rows(rows: list[MomentumAutomationOutcome]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "weighted_return_bps_sum": 0.0,
            "weighted_pnl_sum": 0.0,
            "weight_sum": 0.0,
            "mean_return_bps": None,
            "governance_or_risk_count": 0,
        }
    w_sum = 0.0
    wrb = 0.0
    wp = 0.0
    gr = 0
    for r in rows:
        w = float(r.evidence_weight or 1.0)
        w_sum += w
        rb = r.return_bps
        if rb is not None:
            wrb += float(rb) * w
        if r.realized_pnl_usd is not None:
            wp += float(r.realized_pnl_usd) * w
        oc = r.outcome_class or ""
        if oc in ("governance_exit", "risk_block", "stale_data_abort"):
            gr += 1
    mean_bps = (wrb / w_sum) if w_sum > 0 else None
    return {
        "n": len(rows),
        "weighted_return_bps_sum": round(wrb, 6),
        "weighted_pnl_sum": round(wp, 6),
        "weight_sum": round(w_sum, 6),
        "mean_return_bps": round(mean_bps, 4) if mean_bps is not None else None,
        "governance_or_risk_count": gr,
    }


def apply_outcome_feedback_to_viability(db: Session, outcome_row: MomentumAutomationOutcome) -> None:
    """
    Patch evidence_window_json.neural_feedback_v1 with separate paper/live tallies;
    nudge viability_score slightly (capped) — does not replace core viability engine.
    """
    sym = outcome_row.symbol.strip().upper()
    vid = int(outcome_row.variant_id)
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == vid)
        .one_or_none()
    )
    if not via:
        return

    ev = dict(via.evidence_window_json) if isinstance(via.evidence_window_json, dict) else {}
    fb = dict(ev.get("neural_feedback_v1") or {})
    fb["version"] = _FEEDBACK_VERSION
    fb["updated_at_utc"] = datetime.utcnow().isoformat()

    mode = (outcome_row.mode or "paper").lower()
    bucket = "paper" if mode == "paper" else "live"
    side = dict(fb.get(bucket) or {})
    n = int(side.get("n") or 0) + 1
    wsum = float(side.get("weight_sum") or 0.0) + float(outcome_row.evidence_weight or 1.0)
    wrb = float(side.get("weighted_return_bps_sum") or 0.0)
    if outcome_row.return_bps is not None:
        wrb += float(outcome_row.return_bps) * float(outcome_row.evidence_weight or 1.0)
    side.update(
        {
            "n": n,
            "weight_sum": round(wsum, 6),
            "weighted_return_bps_sum": round(wrb, 6),
            "last_outcome_class": outcome_row.outcome_class,
            "last_session_id": outcome_row.session_id,
            "last_terminal_at_utc": outcome_row.terminal_at.isoformat() if outcome_row.terminal_at else None,
        }
    )
    if n < _LIVE_WEIGHT_FULL_N and bucket == "live":
        side["hint"] = "caution_tiny_live_sample"
    else:
        side.pop("hint", None)
    fb[bucket] = side
    ev["neural_feedback_v1"] = fb
    via.evidence_window_json = ev

    # Capped viability nudge from **separate** paper vs live means (no blind merge)
    paper_stats = aggregate_recent_outcomes_for_variant(db, variant_id=vid, days=30, mode="paper")
    live_stats = aggregate_recent_outcomes_for_variant(db, variant_id=vid, days=30, mode="live")
    delta = _viability_delta_from_slices(paper_stats, live_stats)
    if delta != 0.0:
        new_score = float(via.viability_score) + delta
        new_score = max(0.0, min(1.0, new_score))
        via.viability_score = new_score
        ex = dict(via.explain_json) if isinstance(via.explain_json, dict) else {}
        ex["neural_feedback_nudge"] = {
            "delta": round(delta, 5),
            "at_utc": datetime.utcnow().isoformat(),
            "session_id": outcome_row.session_id,
        }
        via.explain_json = ex

    via.updated_at = datetime.utcnow()


def _recent_outcomes_for_variant(db: Session, *, variant_id: int, days: int = _REFINEMENT_LOOKBACK_DAYS) -> list[MomentumAutomationOutcome]:
    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    return (
        db.query(MomentumAutomationOutcome)
        .filter(
            MomentumAutomationOutcome.variant_id == int(variant_id),
            MomentumAutomationOutcome.contributes_to_evolution.is_(True),
            MomentumAutomationOutcome.created_at >= since,
        )
        .order_by(MomentumAutomationOutcome.created_at.desc())
        .all()
    )


def maybe_publish_refined_variant(db: Session, *, variant_id: int) -> dict[str, Any]:
    variant = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id == int(variant_id))
        .one_or_none()
    )
    if variant is None:
        return {"ok": False, "error": "variant_not_found"}

    outcomes = _recent_outcomes_for_variant(db, variant_id=int(variant.id))
    if len(outcomes) < _REFINEMENT_MIN_OUTCOMES:
        return {"ok": True, "skipped": "insufficient_outcomes", "sample_size": len(outcomes)}

    existing_child = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.parent_variant_id == int(variant.id))
        .order_by(MomentumStrategyVariant.version.desc())
        .first()
    )
    if existing_child is not None:
        return {"ok": True, "skipped": "child_already_exists", "variant_id": int(existing_child.id)}

    refined_params, meta = refine_strategy_params(variant.params_json, outcomes)
    if not meta.get("eligible"):
        return {"ok": True, "skipped": meta.get("reason") or "not_eligible", "meta": meta}
    if params_signature(refined_params) == params_signature(variant.params_json):
        return {"ok": True, "skipped": "params_unchanged", "meta": meta}

    next_version = int(variant.version) + 1
    child = MomentumStrategyVariant(
        family=variant.family,
        variant_key=variant.variant_key,
        version=next_version,
        label=f"{variant.label} [Brain refined v{next_version}]",
        params_json=refined_params,
        is_active=True,
        execution_family=variant.execution_family,
        parent_variant_id=int(variant.id),
        refinement_meta_json={
            "created_at_utc": datetime.utcnow().isoformat(),
            "source_variant_id": int(variant.id),
            "source_variant_version": int(variant.version),
            "source_outcome_count": len(outcomes),
            **meta,
        },
        scan_pattern_id=variant.scan_pattern_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(child)
    db.flush()

    variant.is_active = False
    variant.updated_at = datetime.utcnow()

    source_rows = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.variant_id == int(variant.id))
        .all()
    )
    for row in source_rows:
        explain = dict(row.explain_json or {})
        explain["refined_from_variant_id"] = int(variant.id)
        evidence_window = dict(row.evidence_window_json or {})
        evidence_window["refined_clone_from_variant_id"] = int(variant.id)
        evidence_window["refined_clone_at_utc"] = datetime.utcnow().isoformat()
        db.add(
            MomentumSymbolViability(
                symbol=row.symbol,
                scope=getattr(row, "scope", "symbol"),
                variant_id=int(child.id),
                viability_score=row.viability_score,
                paper_eligible=row.paper_eligible,
                live_eligible=row.live_eligible,
                freshness_ts=row.freshness_ts,
                regime_snapshot_json=dict(row.regime_snapshot_json or {}),
                execution_readiness_json=dict(row.execution_readiness_json or {}),
                explain_json=explain,
                evidence_window_json=evidence_window,
                source_node_id=row.source_node_id,
                correlation_id=row.correlation_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )

    record_feedback_ingestion_trace(
        db,
        {
            "variant_id": int(variant.id),
            "refined_variant_id": int(child.id),
            "refined_version": next_version,
            "sample_size": len(outcomes),
            "mean_return_bps": meta.get("mean_return_bps"),
            "win_rate": meta.get("win_rate"),
        },
    )
    return {
        "ok": True,
        "created": True,
        "variant_id": int(child.id),
        "source_variant_id": int(variant.id),
        "version": next_version,
    }


def _viability_delta_from_slices(paper: dict[str, Any], live: dict[str, Any]) -> float:
    """Small capped delta; live dominates only when sample size sufficient."""
    p_n = int(paper.get("n") or 0)
    l_n = int(live.get("n") or 0)
    p_mean = paper.get("mean_return_bps")
    l_mean = live.get("mean_return_bps")

    # Paper-only channel
    delta = 0.0
    if p_n >= 2 and p_mean is not None:
        delta += 0.01 * math.tanh(float(p_mean) / 120.0) * min(1.0, p_n / 10.0)
    # Live channel (stronger when n>=3)
    if l_n >= _LIVE_WEIGHT_FULL_N and l_mean is not None:
        delta += 0.015 * math.tanh(float(l_mean) / 100.0) * min(1.0, l_n / 8.0)
    elif l_n > 0 and l_mean is not None and l_mean < -50:
        # Single/two harsh live losses: small degradation only
        delta += -0.008 * min(1.0, abs(float(l_mean)) / 200.0) * (0.4 if l_n < _LIVE_WEIGHT_FULL_N else 1.0)
    elif p_n < 2 and l_n == 1 and l_mean is not None and float(l_mean) < -40:
        delta += -0.005

    if delta > _MAX_VIABILITY_DELTA:
        return _MAX_VIABILITY_DELTA
    if delta < -_MAX_VIABILITY_DELTA:
        return -_MAX_VIABILITY_DELTA
    return float(delta)


def evolution_summary_for_operator(db: Session, *, variant_id: Optional[int] = None) -> dict[str, Any]:
    """Compact read-model for automation / trading surfaces."""
    st = get_or_create_state(db, EVOLUTION_NODE_ID)
    ls = dict(st.local_state) if isinstance(st.local_state, dict) else {}
    out: dict[str, Any] = {
        "latest_feedback_at_utc": ls.get("latest_feedback_at_utc"),
        "feedback_trace_tail": list(ls.get("feedback_trace") or [])[-5:],
    }
    if variant_id:
        out["paper_vs_live"] = paper_vs_live_performance_slices(db, variant_id=int(variant_id), days=14)
    return out
