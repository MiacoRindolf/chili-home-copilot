"""Execution robustness / readiness realism for repeatable-edge ScanPatterns (Phase 4 v1).

Aggregates **real** ``Trade`` rows linked by ``scan_pattern_id`` (TCA slippage, fill/miss proxies).
Does not invent microstructure; null-safe fields when data is missing.

Operator readiness merge: ``merge_repeatable_edge_robustness_into_readiness`` plugs summaries into
the existing momentum ``build_momentum_operator_readiness`` dict when a session variant links a pattern.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)

ROBUSTNESS_VERSION = 1

REPEATABLE_EDGE_ORIGINS = frozenset({"web_discovered", "brain_discovered"})

APPROXIMATION_NOTE = (
    "CHILI v4 execution robustness v1: derived from linked Trade rows only (entry_date window); "
    "partial fills inferred from broker_status text; no order-book microstructure; latency not "
    "stored on Trade — left null. Provider truth is config-inferred, not exchange order audit."
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _infer_market_data_source() -> str:
    if getattr(settings, "use_polygon", False):
        return "polygon"
    if (getattr(settings, "massive_api_key", None) or "").strip():
        return "massive"
    return "unknown"


def _infer_provider_truth_mode() -> str:
    """Config-level seam (not per-fill exchange metadata on Trade)."""
    if getattr(settings, "chili_coinbase_spot_adapter_enabled", False):
        return "exchange_aware"
    if getattr(settings, "use_polygon", False) or (getattr(settings, "massive_api_key", None) or "").strip():
        return "aggregated"
    return "unknown"


def _source_truth_tier_from_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m == "exchange_aware":
        return "strong"
    if m == "aggregated":
        return "medium"
    return "unknown"


def build_skip_contract(
    *,
    skip_reason: str,
    evaluation_window_days: int | None = None,
    execution_family: str | None = None,
) -> dict[str, Any]:
    return {
        "robustness_version": ROBUSTNESS_VERSION,
        "execution_family": execution_family,
        "venue": None,
        "broker_adapter": None,
        "market_data_source": _infer_market_data_source(),
        "provider_truth_mode": _infer_provider_truth_mode(),
        "primary_runtime_source": None,
        "sample_count_orders": 0,
        "sample_count_fills": 0,
        "fill_rate": None,
        "partial_fill_rate": None,
        "miss_rate": None,
        "avg_expected_slippage_bps": None,
        "avg_realized_slippage_bps": None,
        "slippage_gap_bps": None,
        "avg_spread_bps": None,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "execution_cost_tier": "n/a",
        "source_truth_tier": _source_truth_tier_from_mode(_infer_provider_truth_mode()),
        "robustness_tier": "n/a",
        "robustness_flags": [],
        "readiness_impact_flags": [],
        "evaluation_window": {"days": int(evaluation_window_days or 0)},
        "last_evaluated_at": _utc_iso(),
        "approximation_note": APPROXIMATION_NOTE,
        "skip_reason": skip_reason,
    }


def aggregate_trade_execution_for_pattern(
    db: Session,
    *,
    scan_pattern_id: int,
    user_id: int,
    window_days: int,
) -> dict[str, Any]:
    """Rollups from ``trading_trades`` for one pattern + user."""
    from ...models.trading import Trade

    since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    rows = (
        db.query(Trade)
        .filter(
            Trade.scan_pattern_id == int(scan_pattern_id),
            Trade.user_id == int(user_id),
            Trade.entry_date >= since,
        )
        .all()
    )
    n_orders = len(rows)
    n_filled = sum(1 for t in rows if t.filled_at is not None or t.avg_fill_price is not None)
    n_partial = sum(
        1
        for t in rows
        if t.broker_status and "partial" in (t.broker_status or "").lower()
    )
    n_miss = sum(
        1
        for t in rows
        if (t.status or "").lower() in ("cancelled", "rejected")
        and t.filled_at is None
        and t.avg_fill_price is None
    )
    slips: list[float] = []
    for t in rows:
        for col in (t.tca_entry_slippage_bps, t.tca_exit_slippage_bps):
            if col is not None:
                try:
                    slips.append(abs(float(col)))
                except (TypeError, ValueError):
                    pass
    brokers = [((t.broker_source or "manual") or "manual").strip().lower() for t in rows]
    broker_mode = max(set(brokers), key=brokers.count) if brokers else None

    return {
        "n_orders": n_orders,
        "n_filled": n_filled,
        "n_partial": n_partial,
        "n_miss": n_miss,
        "slippages_abs_bps": slips,
        "dominant_broker_source": broker_mode,
    }


def compute_execution_robustness_contract(
    *,
    pattern: Any,
    stats: dict[str, Any],
    settings_mod: Any,
) -> dict[str, Any]:
    window_days = int(getattr(settings_mod, "brain_execution_robustness_window_days", 120) or 120)
    min_orders = int(getattr(settings_mod, "brain_execution_robustness_min_orders", 5) or 5)
    warn_fill = float(getattr(settings_mod, "brain_execution_robustness_warn_fill_rate", 0.65) or 0.65)
    crit_fill = float(getattr(settings_mod, "brain_execution_robustness_critical_fill_rate", 0.45) or 0.45)
    warn_slip = float(getattr(settings_mod, "brain_execution_robustness_warn_slippage_bps", 35.0) or 35.0)
    crit_slip = float(getattr(settings_mod, "brain_execution_robustness_critical_slippage_bps", 65.0) or 65.0)

    origin = (getattr(pattern, "origin", "") or "").strip().lower()
    if origin not in REPEATABLE_EDGE_ORIGINS:
        return build_skip_contract(
            skip_reason="not_repeatable_edge_origin",
            evaluation_window_days=window_days,
            execution_family=None,
        )

    n_orders = int(stats.get("n_orders") or 0)
    if n_orders < min_orders:
        return build_skip_contract(
            skip_reason="insufficient_trade_sample",
            evaluation_window_days=window_days,
            execution_family="discretionary_linked_trades",
        )

    n_filled = int(stats.get("n_filled") or 0)
    n_partial = int(stats.get("n_partial") or 0)
    n_miss = int(stats.get("n_miss") or 0)
    fill_rate = round(n_filled / max(1, n_orders), 4)
    miss_rate = round(n_miss / max(1, n_orders), 4)
    partial_fill_rate = round(n_partial / max(1, n_filled), 4) if n_filled else 0.0

    slips = list(stats.get("slippages_abs_bps") or [])
    avg_realized = round(sum(slips) / len(slips), 2) if slips else None

    prov_mode = _infer_provider_truth_mode()
    src_tier = _source_truth_tier_from_mode(prov_mode)
    mds = _infer_market_data_source()

    flags: list[str] = []
    impact: list[str] = []
    if src_tier == "unknown":
        flags.append("weak_provider_truth_inferred")
        impact.append("weak_provider_truth")
    if fill_rate < warn_fill:
        flags.append("low_fill_rate")
        impact.append("poor_fill_rate")
    if avg_realized is not None and avg_realized > warn_slip:
        flags.append("elevated_slippage")
        impact.append("high_slippage")

    robustness_tier = "healthy"
    exec_cost_tier = "low"
    if fill_rate <= crit_fill or (avg_realized is not None and avg_realized >= crit_slip):
        robustness_tier = "critical"
        exec_cost_tier = "high"
        impact.append("review_required")
    elif fill_rate <= warn_fill or (avg_realized is not None and avg_realized >= warn_slip):
        robustness_tier = "warning"
        exec_cost_tier = "medium"

    if src_tier == "unknown" and robustness_tier == "healthy":
        robustness_tier = "warning"
        impact.append("paper_only_recommended")

    broker = stats.get("dominant_broker_source")

    return {
        "robustness_version": ROBUSTNESS_VERSION,
        "execution_family": "discretionary_linked_trades",
        "venue": None,
        "broker_adapter": broker,
        "market_data_source": mds,
        "provider_truth_mode": prov_mode,
        "primary_runtime_source": "live" if n_filled else "unknown",
        "sample_count_orders": n_orders,
        "sample_count_fills": n_filled,
        "fill_rate": fill_rate,
        "partial_fill_rate": partial_fill_rate,
        "miss_rate": miss_rate,
        "avg_expected_slippage_bps": None,
        "avg_realized_slippage_bps": avg_realized,
        "slippage_gap_bps": None,
        "avg_spread_bps": None,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "execution_cost_tier": exec_cost_tier,
        "source_truth_tier": src_tier,
        "robustness_tier": robustness_tier,
        "robustness_flags": flags,
        "readiness_impact_flags": sorted(set(impact)),
        "evaluation_window": {"days": window_days},
        "last_evaluated_at": _utc_iso(),
        "approximation_note": APPROXIMATION_NOTE,
        "skip_reason": None,
    }


def apply_execution_robustness_to_pattern(
    db: Session,
    pattern: Any,
    contract: dict[str, Any],
    settings_mod: Any,
) -> None:
    ov = dict(pattern.oos_validation_json or {}) if isinstance(pattern.oos_validation_json, dict) else {}
    ov["execution_robustness"] = contract
    pattern.oos_validation_json = ov


def run_execution_robustness_refresh(db: Session) -> dict[str, Any]:
    from ...models.trading import ScanPattern

    if not getattr(settings, "brain_execution_robustness_enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled", "updated": 0}
    uid = getattr(settings, "brain_default_user_id", None)
    if uid is None:
        return {"ok": True, "skipped": True, "reason": "no_brain_default_user_id", "updated": 0}

    window = int(getattr(settings, "brain_execution_robustness_window_days", 120) or 120)
    q = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.origin.in_(tuple(REPEATABLE_EDGE_ORIGINS)),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
    )
    rows = q.all()
    updated = 0
    for pattern in rows:
        try:
            stats = aggregate_trade_execution_for_pattern(
                db,
                scan_pattern_id=int(pattern.id),
                user_id=int(uid),
                window_days=window,
            )
            contract = compute_execution_robustness_contract(
                pattern=pattern,
                stats=stats,
                settings_mod=settings,
            )
            apply_execution_robustness_to_pattern(db, pattern, contract, settings)
            updated += 1
        except Exception as e:
            logger.warning("[execution_robustness] pattern id=%s: %s", getattr(pattern, "id", "?"), e)
    if updated:
        try:
            db.commit()
        except Exception as e:
            logger.warning("[execution_robustness] commit failed: %s", e)
            db.rollback()
            return {"ok": False, "error": str(e), "updated": 0}
    return {"ok": True, "updated": updated, "candidates": len(rows)}


def execution_robustness_summary(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not contract or not isinstance(contract, dict):
        return None
    return {
        "robustness_tier": contract.get("robustness_tier"),
        "fill_rate": contract.get("fill_rate"),
        "miss_rate": contract.get("miss_rate"),
        "avg_realized_slippage_bps": contract.get("avg_realized_slippage_bps"),
        "provider_truth_mode": contract.get("provider_truth_mode"),
        "source_truth_tier": contract.get("source_truth_tier"),
        "readiness_impact_flags": contract.get("readiness_impact_flags"),
        "skip_reason": contract.get("skip_reason"),
    }


def merge_repeatable_edge_robustness_into_readiness(
    readiness: dict[str, Any],
    db: Session,
    *,
    scan_pattern_id: int | None,
) -> dict[str, Any]:
    """Augment existing operator readiness (mutates and returns same dict)."""
    out = dict(readiness or {})
    out.pop("_repeatable_edge_block_live", None)
    if not scan_pattern_id:
        out["repeatable_edge_execution_robustness"] = None
        return out

    from ...models.trading import ScanPattern

    p = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).first()
    if not p:
        out["repeatable_edge_execution_robustness"] = None
        return out
    ov = p.oos_validation_json or {}
    er = ov.get("execution_robustness") if isinstance(ov.get("execution_robustness"), dict) else None
    out["repeatable_edge_execution_robustness"] = execution_robustness_summary(er)
    if er and not er.get("skip_reason"):
        tier = (er.get("robustness_tier") or "").strip().lower()
        flags = er.get("readiness_impact_flags") or []
        if tier == "critical" and bool(getattr(settings, "brain_execution_robustness_live_not_recommended", True)):
            out["repeatable_edge_live_not_recommended"] = True
            out["repeatable_edge_live_not_recommended_reason"] = "execution_robustness_critical"
        if "weak_provider_truth" in flags and bool(
            getattr(settings, "brain_execution_robustness_flag_weak_truth_live", True)
        ):
            out["repeatable_edge_live_not_recommended"] = True
            out["repeatable_edge_live_not_recommended_reason"] = "weak_provider_truth"
        hard = bool(getattr(settings, "brain_execution_robustness_hard_block_live_enabled", False))
        min_o = int(getattr(settings, "brain_execution_robustness_min_orders", 5) or 5)
        n_o = int(er.get("sample_count_orders") or 0)
        if hard and tier == "critical" and n_o >= min_o:
            out["_repeatable_edge_block_live"] = "execution_robustness_critical"
    return out
