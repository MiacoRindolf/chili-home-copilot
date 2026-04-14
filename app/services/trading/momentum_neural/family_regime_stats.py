"""Aggregate momentum outcomes by strategy family × regime (Phase 6a pre-filter support)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant


def aggregate_family_regime_performance(db: Session, *, days: int = 90) -> list[dict[str, Any]]:
    """Rollup (family, volatility_regime, session_label) → n, win_rate, mean_return_bps."""
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)))
    rows = (
        db.query(MomentumAutomationOutcome, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumAutomationOutcome.variant_id)
        .filter(MomentumAutomationOutcome.created_at >= since)
        .filter(MomentumAutomationOutcome.return_bps.isnot(None))
        .all()
    )
    buckets: dict[tuple[str, str, str], list[float]] = {}
    for out, var in rows:
        entry = getattr(out, "entry_regime_snapshot_json", None)
        if not isinstance(entry, dict) or not entry:
            entry = out.regime_snapshot_json if isinstance(out.regime_snapshot_json, dict) else {}
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        vol = str(entry.get("volatility_regime") or meta.get("volatility_regime") or "unknown")
        sess_lbl = str(entry.get("session_label") or meta.get("session_label") or "unknown")
        fam = str(var.family or "unknown")
        key = (fam, vol, sess_lbl)
        buckets.setdefault(key, []).append(float(out.return_bps or 0.0))

    out_rows: list[dict[str, Any]] = []
    for (fam, vol, sess_lbl), vals in buckets.items():
        n = len(vals)
        if n < 1:
            continue
        wins = sum(1 for v in vals if v > 0)
        out_rows.append(
            {
                "family_id": fam,
                "volatility_regime": vol,
                "session_label": sess_lbl,
                "n": n,
                "win_rate": wins / n,
                "mean_return_bps": sum(vals) / n,
            }
        )
    out_rows.sort(key=lambda r: r["n"], reverse=True)
    return out_rows


def family_regime_prefilter_allows(
    db: Session,
    *,
    family_id: str,
    regime_snapshot: dict[str, Any],
) -> tuple[bool, str]:
    """Optional block when historical bucket is clearly toxic (config-gated)."""
    from ....config import settings

    if not bool(getattr(settings, "chili_momentum_family_regime_prefilter_enabled", False)):
        return True, "prefilter_off"
    vol = str(regime_snapshot.get("volatility_regime") or "unknown")
    sess = str(regime_snapshot.get("session_label") or "unknown")
    fid = (family_id or "").strip().lower()
    stats = aggregate_family_regime_performance(db, days=120)
    for row in stats:
        if row["family_id"].lower() != fid:
            continue
        if row["volatility_regime"] != vol or row["session_label"] != sess:
            continue
        if row["n"] >= 5 and row["win_rate"] < 0.4 and row["mean_return_bps"] < -10.0:
            return False, "family_regime_track_record_poor"
    return True, "ok"
