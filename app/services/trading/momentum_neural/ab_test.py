"""A/B helpers for parallel refined vs parent momentum variants (Phase 6b)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant


def compare_peer_variants(
    db: Session,
    *,
    variant_a_id: int,
    variant_b_id: int,
    min_sessions: int = 5,
    days: int = 30,
) -> dict[str, Any]:
    """Compare mean return_bps over recent terminal outcomes per variant (paper+live)."""
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)))
    lim = max(int(min_sessions), 5)

    def _slice(vid: int) -> list[float]:
        rows = (
            db.query(MomentumAutomationOutcome.return_bps)
            .filter(
                MomentumAutomationOutcome.variant_id == int(vid),
                MomentumAutomationOutcome.created_at >= since,
                MomentumAutomationOutcome.return_bps.isnot(None),
            )
            .order_by(desc(MomentumAutomationOutcome.created_at))
            .limit(lim)
            .all()
        )
        return [float(r[0]) for r in rows]

    a = _slice(variant_a_id)
    b = _slice(variant_b_id)
    out: dict[str, Any] = {
        "variant_a_id": int(variant_a_id),
        "variant_b_id": int(variant_b_id),
        "a_n": len(a),
        "b_n": len(b),
        "a_mean_bps": sum(a) / len(a) if a else None,
        "b_mean_bps": sum(b) / len(b) if b else None,
        "winner": None,
        "ready": len(a) >= min_sessions and len(b) >= min_sessions,
    }
    if not out["ready"] or not a or not b:
        return out
    ma = out["a_mean_bps"] or 0.0
    mb = out["b_mean_bps"] or 0.0
    if ma > mb:
        out["winner"] = "a"
    elif mb > ma:
        out["winner"] = "b"
    else:
        out["winner"] = "tie"
    return out


def list_ab_pairs(db: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    """Variants with refinement_meta ab_peer_variant_id (operator desk)."""
    rows = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.refinement_meta_json.isnot(None))
        .order_by(desc(MomentumStrategyVariant.updated_at))
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    out: list[dict[str, Any]] = []
    for v in rows:
        meta = v.refinement_meta_json if isinstance(v.refinement_meta_json, dict) else {}
        peer = meta.get("ab_peer_variant_id")
        if peer is None:
            continue
        out.append(
            {
                "variant_id": int(v.id),
                "label": v.label,
                "ab_peer_variant_id": int(peer),
                "ab_role": meta.get("ab_role"),
                "comparison": compare_peer_variants(db, variant_a_id=int(v.id), variant_b_id=int(peer)),
            }
        )
    return out[:limit]
