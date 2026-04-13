"""Net expectancy composition for allocator (research prior + realism + penalties)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern


def _sf(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def compute_expectancy_edges(
    db: Session,
    *,
    scan_pattern_id: int | None,
    viability_score: float,
    viability_eligible: bool,
    regime_multiplier: float,
    uncertainty_haircut: float,
    execution_penalty: float,
    capacity_soft_penalty: float,
    correlation_penalty: float,
) -> dict[str, Any]:
    """Return gross/net expectancy-like scores in fraction-of-notional space (approximate)."""
    research_prior = max(0.0, min(1.0, _sf(viability_score, 0.0)))
    if scan_pattern_id:
        p = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).one_or_none()
        if p:
            oos_ret = _sf(getattr(p, "oos_avg_return_pct", None), 0.0)
            if oos_ret == 0:
                oos_ret = _sf(getattr(p, "avg_return_pct", None), 0.0)
            research_prior = max(research_prior, min(0.08, abs(oos_ret) / 100.0 * 0.6))

    gross = research_prior * max(0.5, min(1.5, regime_multiplier))
    if not viability_eligible:
        gross *= 0.35

    hair = max(0.0, min(0.95, uncertainty_haircut))
    exec_p = max(0.0, min(0.95, execution_penalty))
    cap_p = max(0.0, min(0.95, capacity_soft_penalty))
    corr_p = max(0.0, min(0.95, correlation_penalty))

    net = gross * (1.0 - hair) - exec_p * 0.12 - cap_p * 0.1 - corr_p * 0.08
    net = max(-0.5, min(0.5, net))

    return {
        "expected_edge_gross": round(gross, 6),
        "expected_edge_net": round(net, 6),
        "research_prior": round(research_prior, 6),
        "uncertainty_haircut": round(hair, 4),
    }
