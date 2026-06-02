"""A/B helpers for parallel refined vs parent momentum variants (Phase 6b)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant


def _return_slices_from_peer_rows(
    rows: list[Any],
    *,
    variant_a_id: int,
    variant_b_id: int,
) -> tuple[list[float], list[float]]:
    a: list[float] = []
    b: list[float] = []
    aid = int(variant_a_id)
    bid = int(variant_b_id)
    for row in rows:
        if isinstance(row, (tuple, list)):
            variant_id, return_bps = row
        else:
            variant_id = row.variant_id
            return_bps = row.return_bps
        value = float(return_bps)
        vid = int(variant_id)
        if vid == aid:
            a.append(value)
        if vid == bid:
            b.append(value)
    return a, b


def _return_slices_by_variant_from_rows(rows: list[Any], *, variant_ids: list[int]) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {int(variant_id): [] for variant_id in variant_ids}
    for row in rows:
        if isinstance(row, (tuple, list)):
            variant_id, return_bps = row
        else:
            variant_id = row.variant_id
            return_bps = row.return_bps
        vid = int(variant_id)
        if vid in out:
            out[vid].append(float(return_bps))
    return out


def _comparison_from_return_slices(
    *,
    variant_a_id: int,
    variant_b_id: int,
    a: list[float],
    b: list[float],
    min_sessions: int,
) -> dict[str, Any]:
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


def _ab_pair_row_fields(row: Any) -> tuple[int, str, dict[str, Any]]:
    if isinstance(row, (tuple, list)):
        variant_id, label, raw_meta = row
    else:
        variant_id = row.id
        label = row.label
        raw_meta = row.refinement_meta_json
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    return int(variant_id), str(label or ""), meta


def _latest_return_slices_by_variant(
    db: Session,
    *,
    variant_ids: list[int],
    min_sessions: int = 5,
    days: int = 30,
) -> dict[int, list[float]]:
    ids = sorted({int(variant_id) for variant_id in variant_ids})
    if not ids:
        return {}
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)))
    lim = max(int(min_sessions), 5)
    ranked = (
        db.query(
            MomentumAutomationOutcome.variant_id.label("variant_id"),
            MomentumAutomationOutcome.return_bps.label("return_bps"),
            func.row_number()
            .over(
                partition_by=MomentumAutomationOutcome.variant_id,
                order_by=MomentumAutomationOutcome.created_at.desc(),
            )
            .label("rn"),
        )
        .filter(
            MomentumAutomationOutcome.variant_id.in_(ids),
            MomentumAutomationOutcome.created_at >= since,
            MomentumAutomationOutcome.return_bps.isnot(None),
        )
        .subquery()
    )
    rows = db.query(ranked.c.variant_id, ranked.c.return_bps).filter(ranked.c.rn <= lim).all()
    return _return_slices_by_variant_from_rows(rows, variant_ids=ids)


def compare_peer_variants(
    db: Session,
    *,
    variant_a_id: int,
    variant_b_id: int,
    min_sessions: int = 5,
    days: int = 30,
) -> dict[str, Any]:
    """Compare mean return_bps over recent terminal outcomes per variant (paper+live)."""
    by_variant = _latest_return_slices_by_variant(
        db,
        variant_ids=[int(variant_a_id), int(variant_b_id)],
        min_sessions=min_sessions,
        days=days,
    )
    a = by_variant.get(int(variant_a_id), [])
    b = by_variant.get(int(variant_b_id), [])
    return _comparison_from_return_slices(
        variant_a_id=variant_a_id,
        variant_b_id=variant_b_id,
        a=a,
        b=b,
        min_sessions=min_sessions,
    )


def list_ab_pairs(db: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    """Variants with refinement_meta ab_peer_variant_id (operator desk)."""
    rows = (
        db.query(
            MomentumStrategyVariant.id,
            MomentumStrategyVariant.label,
            MomentumStrategyVariant.refinement_meta_json,
        )
        .filter(MomentumStrategyVariant.refinement_meta_json.isnot(None))
        .order_by(desc(MomentumStrategyVariant.updated_at))
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    out: list[dict[str, Any]] = []
    pairs: list[tuple[Any, dict[str, Any], int, int]] = []
    variant_ids: list[int] = []
    for row in rows:
        variant_id, label, meta = _ab_pair_row_fields(row)
        peer = meta.get("ab_peer_variant_id")
        if peer is None:
            continue
        peer_id = int(peer)
        pairs.append((label, meta, variant_id, peer_id))
        variant_ids.extend([variant_id, peer_id])

    by_variant = _latest_return_slices_by_variant(db, variant_ids=variant_ids)
    for label, meta, variant_id, peer_id in pairs:
        out.append(
            {
                "variant_id": variant_id,
                "label": label,
                "ab_peer_variant_id": peer_id,
                "ab_role": meta.get("ab_role"),
                "comparison": _comparison_from_return_slices(
                    variant_a_id=variant_id,
                    variant_b_id=peer_id,
                    a=by_variant.get(variant_id, []),
                    b=by_variant.get(peer_id, []),
                    min_sessions=5,
                ),
            }
        )
    return out[:limit]
