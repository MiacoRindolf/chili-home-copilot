"""Aggregate momentum outcomes by strategy family × regime (Phase 6a pre-filter support)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant


def _family_regime_key(
    out: MomentumAutomationOutcome,
    var: MomentumStrategyVariant,
) -> tuple[str, str, str]:
    entry = getattr(out, "entry_regime_snapshot_json", None)
    if not isinstance(entry, dict) or not entry:
        entry = out.regime_snapshot_json if isinstance(out.regime_snapshot_json, dict) else {}
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    vol = str(entry.get("volatility_regime") or meta.get("volatility_regime") or "unknown")
    sess_lbl = str(entry.get("session_label") or meta.get("session_label") or "unknown")
    fam = str(var.family or "unknown")
    return fam, vol, sess_lbl


def _bucket_summary(
    *,
    family_id: str,
    volatility_regime: str,
    session_label: str,
    returns: Iterable[float],
) -> dict[str, Any] | None:
    vals = list(returns)
    n = len(vals)
    if n < 1:
        return None
    wins = sum(1 for v in vals if v > 0)
    return {
        "family_id": family_id,
        "volatility_regime": volatility_regime,
        "session_label": session_label,
        "n": n,
        "win_rate": wins / n,
        "mean_return_bps": sum(vals) / n,
    }


def _target_family_regime_summary(
    rows: Iterable[tuple[MomentumAutomationOutcome, MomentumStrategyVariant]],
    *,
    family_id: str,
    volatility_regime: str,
    session_label: str,
) -> dict[str, Any] | None:
    fid = (family_id or "").strip().lower()
    vals: list[float] = []
    matched_family = family_id
    for out, var in rows:
        fam, vol, sess_lbl = _family_regime_key(out, var)
        if fam.lower() != fid:
            continue
        if vol != volatility_regime or sess_lbl != session_label:
            continue
        matched_family = fam
        vals.append(float(out.return_bps or 0.0))
    return _bucket_summary(
        family_id=matched_family,
        volatility_regime=volatility_regime,
        session_label=session_label,
        returns=vals,
    )


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
        buckets.setdefault(_family_regime_key(out, var), []).append(float(out.return_bps or 0.0))

    out_rows: list[dict[str, Any]] = []
    for (fam, vol, sess_lbl), vals in buckets.items():
        summary = _bucket_summary(
            family_id=fam,
            volatility_regime=vol,
            session_label=sess_lbl,
            returns=vals,
        )
        if summary is not None:
            out_rows.append(summary)
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
    if not fid:
        return True, "ok"
    since = datetime.utcnow() - timedelta(days=120)
    rows = (
        db.query(MomentumAutomationOutcome, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumAutomationOutcome.variant_id)
        .filter(MomentumAutomationOutcome.created_at >= since)
        .filter(MomentumAutomationOutcome.return_bps.isnot(None))
        .filter(func.lower(func.coalesce(MomentumStrategyVariant.family, "unknown")) == fid)
        .all()
    )
    row = _target_family_regime_summary(
        rows,
        family_id=fid,
        volatility_regime=vol,
        session_label=sess,
    )
    if (
        row is not None
        and row["n"] >= 5
        and row["win_rate"] < 0.4
        and row["mean_return_bps"] < -10.0
    ):
        return False, "family_regime_track_record_poor"
    return True, "ok"
