"""Liquidity / capacity hard blocks and soft penalties for trading decisions."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...config import settings


def _sf(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def evaluate_capacity(
    db: Session,
    *,
    user_id: int | None,
    symbol: str,
    spread_bps: float,
    estimated_slippage_bps: float,
    intended_notional_usd: float,
    execution_mode: str,
    adv_usd_proxy: float | None,
    min_volume_usd_proxy: float | None,
) -> dict[str, Any]:
    """Return capacity decision; hard_block only when caller enforces with mode flags."""
    reasons: list[dict[str, Any]] = []
    soft_penalty = 0.0
    blocked_codes: list[str] = []

    max_spread = _sf(
        getattr(
            settings,
            "chili_momentum_risk_max_spread_bps_live" if execution_mode == "live" else "chili_momentum_risk_max_spread_bps_paper",
            None,
        ),
        50.0,
    )
    max_slip = _sf(getattr(settings, "chili_momentum_risk_max_estimated_slippage_bps", None), 80.0)
    adv_cap = _sf(getattr(settings, "brain_max_adv_notional_pct", None), 0.0)

    if spread_bps > max_spread:
        blocked_codes.append("spread_too_wide")
        reasons.append({"code": "spread_too_wide", "spread_bps": spread_bps, "max": max_spread})
        soft_penalty += min(0.4, (spread_bps - max_spread) / 200.0)

    if estimated_slippage_bps > max_slip:
        blocked_codes.append("slippage_estimate_exceeds_cap")
        reasons.append({"code": "slippage_estimate_exceeds_cap", "slippage_bps": estimated_slippage_bps, "max": max_slip})
        soft_penalty += 0.15

    if min_volume_usd_proxy is not None and min_volume_usd_proxy > 0:
        frac_cap = adv_cap if adv_cap > 0 else 0.25
        if intended_notional_usd > min_volume_usd_proxy * frac_cap:
            blocked_codes.append("liquidity_thin_vs_intended")
            reasons.append(
                {
                    "code": "liquidity_thin_vs_intended",
                    "notional": intended_notional_usd,
                    "volume_proxy": min_volume_usd_proxy,
                    "frac_cap": frac_cap,
                }
            )
            soft_penalty += 0.2

    if adv_cap > 0 and adv_usd_proxy and adv_usd_proxy > 0:
        frac = intended_notional_usd / adv_usd_proxy
        if frac > adv_cap:
            blocked_codes.append("adv_notional_cap")
            reasons.append({"code": "adv_notional_cap", "frac": frac, "cap": adv_cap})
            soft_penalty += min(0.35, (frac - adv_cap) * 2.0)

    hard = bool(blocked_codes)
    enforce = False
    if execution_mode == "live":
        enforce = bool(getattr(settings, "brain_capacity_hard_block_live", False))
    else:
        enforce = bool(getattr(settings, "brain_capacity_hard_block_paper", True))

    blocked = hard and enforce and bool(getattr(settings, "brain_enable_capacity_governor", True))

    return {
        "capacity_blocked": blocked,
        "capacity_hard_signals": hard,
        "capacity_reasons": reasons,
        "soft_penalty": round(min(0.85, soft_penalty), 4),
        "enforced": enforce,
    }
