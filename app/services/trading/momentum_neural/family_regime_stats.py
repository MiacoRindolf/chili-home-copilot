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
    return _family_regime_key_from_values(
        entry_regime_snapshot_json=getattr(out, "entry_regime_snapshot_json", None),
        regime_snapshot_json=getattr(out, "regime_snapshot_json", None),
        family=getattr(var, "family", None),
    )


def _family_regime_key_from_values(
    *,
    entry_regime_snapshot_json: Any,
    regime_snapshot_json: Any,
    family: Any,
) -> tuple[str, str, str]:
    entry = entry_regime_snapshot_json
    if not isinstance(entry, dict) or not entry:
        entry = regime_snapshot_json if isinstance(regime_snapshot_json, dict) else {}
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    vol = str(entry.get("volatility_regime") or meta.get("volatility_regime") or "unknown")
    sess_lbl = str(entry.get("session_label") or meta.get("session_label") or "unknown")
    fam = str(family or "unknown")
    return fam, vol, sess_lbl


def _outcome_family_columns(row: Any) -> tuple[float, str, str, str]:
    if isinstance(row, (tuple, list)):
        return_bps, entry_snapshot, regime_snapshot, family = row
    else:
        return_bps = getattr(row, "return_bps", None)
        entry_snapshot = getattr(row, "entry_regime_snapshot_json", None)
        regime_snapshot = getattr(row, "regime_snapshot_json", None)
        family = getattr(row, "family", None)
    fam, vol, sess_lbl = _family_regime_key_from_values(
        entry_regime_snapshot_json=entry_snapshot,
        regime_snapshot_json=regime_snapshot,
        family=family,
    )
    return float(return_bps or 0.0), fam, vol, sess_lbl


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
    return _bucket_summary_from_stats(
        family_id=family_id,
        volatility_regime=volatility_regime,
        session_label=session_label,
        n=n,
        wins=wins,
        total=sum(vals),
    )


def _bucket_summary_from_stats(
    *,
    family_id: str,
    volatility_regime: str,
    session_label: str,
    n: int,
    wins: int,
    total: float,
) -> dict[str, Any] | None:
    if n < 1:
        return None
    return {
        "family_id": family_id,
        "volatility_regime": volatility_regime,
        "session_label": session_label,
        "n": n,
        "win_rate": wins / n,
        "mean_return_bps": total / n,
    }


def _target_family_regime_summary(
    rows: Iterable[tuple[MomentumAutomationOutcome, MomentumStrategyVariant]],
    *,
    family_id: str,
    volatility_regime: str,
    session_label: str,
) -> dict[str, Any] | None:
    fid = (family_id or "").strip().lower()
    n = 0
    wins = 0
    total = 0.0
    matched_family = family_id
    for out, var in rows:
        fam, vol, sess_lbl = _family_regime_key(out, var)
        if fam.lower() != fid:
            continue
        if vol != volatility_regime or sess_lbl != session_label:
            continue
        matched_family = fam
        value = float(out.return_bps or 0.0)
        n += 1
        total += value
        if value > 0:
            wins += 1
    return _bucket_summary_from_stats(
        family_id=matched_family,
        volatility_regime=volatility_regime,
        session_label=session_label,
        n=n,
        wins=wins,
        total=total,
    )


def _target_family_regime_summary_from_column_rows(
    rows: Iterable[Any],
    *,
    family_id: str,
    volatility_regime: str,
    session_label: str,
) -> dict[str, Any] | None:
    fid = (family_id or "").strip().lower()
    n = 0
    wins = 0
    total = 0.0
    matched_family = family_id
    for raw in rows:
        value, fam, vol, sess_lbl = _outcome_family_columns(raw)
        if fam.lower() != fid:
            continue
        if vol != volatility_regime or sess_lbl != session_label:
            continue
        matched_family = fam
        n += 1
        total += value
        if value > 0:
            wins += 1
    return _bucket_summary_from_stats(
        family_id=matched_family,
        volatility_regime=volatility_regime,
        session_label=session_label,
        n=n,
        wins=wins,
        total=total,
    )


def aggregate_family_regime_performance(db: Session, *, days: int = 90) -> list[dict[str, Any]]:
    """Rollup (family, volatility_regime, session_label) → n, win_rate, mean_return_bps."""
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)))
    rows = (
        db.query(
            MomentumAutomationOutcome.return_bps,
            MomentumAutomationOutcome.entry_regime_snapshot_json,
            MomentumAutomationOutcome.regime_snapshot_json,
            MomentumStrategyVariant.family,
        )
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumAutomationOutcome.variant_id)
        .filter(MomentumAutomationOutcome.created_at >= since)
        .filter(MomentumAutomationOutcome.return_bps.isnot(None))
        .all()
    )
    buckets: dict[tuple[str, str, str], list[float]] = {}
    for row in rows:
        value, fam, vol, sess_lbl = _outcome_family_columns(row)
        buckets.setdefault((fam, vol, sess_lbl), []).append(value)

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
        db.query(
            MomentumAutomationOutcome.return_bps,
            MomentumAutomationOutcome.entry_regime_snapshot_json,
            MomentumAutomationOutcome.regime_snapshot_json,
            MomentumStrategyVariant.family,
        )
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumAutomationOutcome.variant_id)
        .filter(MomentumAutomationOutcome.created_at >= since)
        .filter(MomentumAutomationOutcome.return_bps.isnot(None))
        .filter(func.lower(func.coalesce(MomentumStrategyVariant.family, "unknown")) == fid)
        .all()
    )
    row = _target_family_regime_summary_from_column_rows(
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
