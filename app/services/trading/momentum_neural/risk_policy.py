"""Momentum automation risk policy (config-backed; frozen on session snapshots — Phase 6)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ....config import settings
from ..execution_family_registry import EXECUTION_FAMILY_COINBASE_SPOT

POLICY_VERSION = 1
RISK_SNAPSHOT_KEY = "momentum_risk"
POLICY_SNAPSHOT_KEY = "momentum_risk_policy_summary"


def policy_float_cap(policy: dict[str, Any], key: str, default: float) -> float:
    raw = policy.get(key, default)
    if isinstance(raw, bool) or raw is None:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def policy_int_cap(policy: dict[str, Any], key: str, default: int) -> int:
    raw = policy.get(key, default)
    if isinstance(raw, bool) or raw is None:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def adaptive_max_spread_bps(
    base_max_spread_bps: float,
    expected_move_bps: float | None,
    ratio: float,
) -> float:
    """Volatility-relative spread tolerance (no magic fixed cap).

    The BBO/quote spread is a round-trip execution cost; we tolerate
    proportionally more of it when the instrument's expected move (realized
    volatility) is larger. This ONLY ever loosens above ``base_max_spread_bps``
    (the documented live floor) — it never tightens below it, so the change is a
    safe, monotonic relaxation for explosive momentum names while quiet/illiquid
    names keep the conservative floor. ``ratio`` is the single documented knob:
    the spread may be at most ``ratio`` x the expected per-bar move. Falls back to
    the base floor when expected move or ratio is unknown / non-finite / <= 0.
    """
    base = float(base_max_spread_bps)
    try:
        em = float(expected_move_bps) if expected_move_bps is not None else None
    except (TypeError, ValueError):
        em = None
    if em is None or not math.isfinite(em) or em <= 0:
        return base
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return base
    if not math.isfinite(r) or r <= 0:
        return base
    return max(base, r * em)


def _account_equity_usd(execution_family: str | None = None) -> float | None:
    """Best-effort account equity (USD) for equity-relative sizing, PER VENUE.

    robinhood_spot -> Robinhood account equity (equities are bought with RH buying
    power, not Coinbase crypto equity); else Coinbase portfolio equity (crypto).
    Returns None when unavailable so callers fall back to the documented fixed cap
    (never size against unknown equity). docs/DESIGN/MOMENTUM_LANE.md
    """
    from ..execution_family_registry import (
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        normalize_execution_family,
    )

    ef = normalize_execution_family(execution_family)
    try:
        if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT:
            from ...broker_service import get_portfolio as _rh_portfolio

            pf = _rh_portfolio() or {}
            eq = float(pf.get("equity") or 0.0)
            return eq if eq > 0 else None
        from ...coinbase_service import get_portfolio

        pf = get_portfolio() or {}
        eq = float(pf.get("equity") or 0.0)
        return eq if eq > 0 else None
    except Exception:
        return None


def _equity_relative_cap(
    fixed_fallback_usd: float, fraction: Any, execution_family: str | None = None
) -> float:
    """Cap = account_equity x fraction (equity-relative, not a fixed $), per venue.

    Scales UP as equity grows and DOWN in drawdown (auto-de-risk). Falls back to
    ``fixed_fallback_usd`` when equity or the fraction is unavailable (never size
    against unknown equity). A 0 / non-positive fixed cap is a deliberate operator
    disable/block and is preserved. docs/DESIGN/MOMENTUM_LANE.md
    """
    fixed = float(fixed_fallback_usd)
    if fixed <= 0:
        return fixed
    try:
        frac = float(fraction or 0.0)
    except (TypeError, ValueError):
        frac = 0.0
    if frac <= 0 or not math.isfinite(frac):
        return fixed
    eq = _account_equity_usd(execution_family)
    if eq is None or eq <= 0:
        return fixed
    return round(eq * frac, 2)


def equity_relative_notional_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Per-trade NOTIONAL cap as a fraction of account equity (documented
    per-trade SIZE knob). docs/DESIGN/MOMENTUM_LANE.md"""
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15),
        execution_family,
    )


def equity_relative_loss_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Per-trade MAX-LOSS cap as a fraction of account equity (documented
    per-trade RISK knob). docs/DESIGN/MOMENTUM_LANE.md"""
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01),
        execution_family,
    )


def equity_relative_daily_loss_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Daily-loss cap as a fraction of account equity (documented DAILY risk knob).
    Evaluated live so the daily circuit-breaker adapts to current equity.
    docs/DESIGN/MOMENTUM_LANE.md"""
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05),
        execution_family,
    )


def compute_risk_first_quantity(
    *,
    entry_price: float,
    atr_pct: float,
    max_loss_usd: float,
    max_notional_ceiling_usd: float,
    base_increment: float | None = None,
    base_min_size: float | None = None,
    stop_atr_mult: float = 0.60,
) -> tuple[float, dict[str, Any]]:
    """Risk-first sizing (Ross-style): qty = max_loss_usd / stop_distance, capped at
    the notional ceiling.

    A TIGHTER stop buys MORE size at constant risk (Ross's core sizing edge) — vs
    notional-first where stop distance doesn't drive size. Stop distance uses the
    same ATR formula as ``stop_target_prices`` (max(0.003, atr_pct x stop_atr_mult)).
    Returns ``(qty, meta)``; qty=0 with a ``reason`` when inputs are unusable.
    docs/DESIGN/MOMENTUM_LANE.md
    """
    e = float(entry_price or 0.0)
    if e <= 0 or not math.isfinite(e):
        return 0.0, {"reason": "invalid_entry"}
    loss = float(max_loss_usd or 0.0)
    if loss <= 0 or not math.isfinite(loss):
        return 0.0, {"reason": "max_loss_nonpositive"}
    stop_pct = max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.60))
    stop_distance = e * stop_pct
    if stop_distance <= 0 or not math.isfinite(stop_distance):
        return 0.0, {"reason": "stop_distance_invalid"}
    qty = loss / stop_distance
    capped_by = None
    ceiling = float(max_notional_ceiling_usd or 0.0)
    if ceiling > 0 and qty * e > ceiling:
        qty = ceiling / e
        capped_by = "notional_ceiling"
    inc = float(base_increment) if base_increment and base_increment > 0 else None
    if inc:
        qty = math.floor(qty / inc) * inc
    mn = float(base_min_size) if base_min_size and base_min_size > 0 else None
    if mn and qty < mn:
        return 0.0, {"reason": "below_min_size", "stop_distance": round(stop_distance, 8)}
    return float(qty), {
        "model": "risk_first",
        "stop_distance": round(stop_distance, 8),
        "risk_usd": round(loss, 2),
        "notional_usd": round(qty * e, 2),
        "capped_by": capped_by,
    }


@dataclass(frozen=True)
class MomentumAutomationRiskPolicy:
    """Conservative defaults for short-horizon crypto momentum (pre-runner gates)."""

    execution_family_default: str = EXECUTION_FAMILY_COINBASE_SPOT
    mode_scope: str = "both"  # paper | live | both (informational)
    max_daily_loss_usd: float = 250.0
    max_loss_per_trade_usd: float = 50.0
    max_concurrent_sessions: int = 10
    max_concurrent_live_sessions: int = 5
    max_concurrent_positions: int = 5
    max_notional_per_trade_usd: float = 500.0
    max_position_size_base: float = 1_000_000.0
    max_spread_bps_paper: float = 28.0
    max_spread_bps_live: float = 12.0
    # Adaptive spread tolerance. The BBO/quote spread is a round-trip cost, so we
    # gate it RELATIVE to how far the instrument actually moves (its realized 15m
    # volatility), never below the live floor above. This single documented knob is
    # the max spread as a fraction of that expected per-bar move (0.5 => the spread
    # may be at most half a typical bar's range). Lets Ross-style explosive names
    # (wide absolute spread, tiny vs. their move) trade without a magic fixed cap.
    spread_to_expected_move_ratio: float = 0.5
    max_estimated_slippage_bps: float = 18.0
    max_fee_to_target_ratio: float = 0.35
    max_hold_seconds: int = 86_400
    cooldown_after_stopout_seconds: int = 300
    cooldown_after_cancel_seconds: int = 60
    viability_max_age_seconds: float = 600.0
    stale_market_data_max_age_sec: float = 30.0
    require_live_eligible_for_live: bool = True
    require_fresh_viability: bool = True
    require_strict_coinbase_freshness: bool = False
    disable_live_if_governance_inhibit: bool = True
    block_paper_when_kill_switch: bool = False
    auto_expire_pending_live_arm_seconds: float = 900.0

    @classmethod
    def from_settings(cls) -> MomentumAutomationRiskPolicy:
        s = settings
        return cls(
            max_daily_loss_usd=float(getattr(s, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
            max_loss_per_trade_usd=float(getattr(s, "chili_momentum_risk_max_loss_per_trade_usd", 50.0)),
            max_concurrent_sessions=int(getattr(s, "chili_momentum_risk_max_concurrent_sessions", 10)),
            max_concurrent_live_sessions=int(getattr(s, "chili_momentum_risk_max_concurrent_live_sessions", 5)),
            max_concurrent_positions=int(getattr(s, "chili_momentum_risk_max_concurrent_positions", 5)),
            max_notional_per_trade_usd=float(getattr(s, "chili_momentum_risk_max_notional_per_trade_usd", 500.0)),
            max_position_size_base=float(getattr(s, "chili_momentum_risk_max_position_size_base", 1_000_000.0)),
            max_spread_bps_paper=float(getattr(s, "chili_momentum_risk_max_spread_bps_paper", 28.0)),
            max_spread_bps_live=float(getattr(s, "chili_momentum_risk_max_spread_bps_live", 12.0)),
            spread_to_expected_move_ratio=float(
                getattr(s, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
            ),
            max_estimated_slippage_bps=float(getattr(s, "chili_momentum_risk_max_estimated_slippage_bps", 18.0)),
            max_fee_to_target_ratio=float(getattr(s, "chili_momentum_risk_max_fee_to_target_ratio", 0.35)),
            max_hold_seconds=int(getattr(s, "chili_momentum_risk_max_hold_seconds", 86_400)),
            cooldown_after_stopout_seconds=int(getattr(s, "chili_momentum_risk_cooldown_after_stopout_seconds", 300)),
            cooldown_after_cancel_seconds=int(getattr(s, "chili_momentum_risk_cooldown_after_cancel_seconds", 60)),
            viability_max_age_seconds=float(getattr(s, "chili_momentum_risk_viability_max_age_seconds", 600.0)),
            stale_market_data_max_age_sec=float(
                getattr(s, "chili_momentum_risk_stale_market_data_max_age_sec", 30.0)
            ),
            require_live_eligible_for_live=bool(getattr(s, "chili_momentum_risk_require_live_eligible", True)),
            require_fresh_viability=bool(getattr(s, "chili_momentum_risk_require_fresh_viability", True)),
            require_strict_coinbase_freshness=bool(
                getattr(s, "chili_momentum_risk_require_strict_coinbase_freshness", False)
            ),
            disable_live_if_governance_inhibit=bool(
                getattr(s, "chili_momentum_risk_disable_live_if_governance_inhibit", True)
            ),
            block_paper_when_kill_switch=bool(getattr(s, "chili_momentum_risk_block_paper_when_kill_switch", False)),
            auto_expire_pending_live_arm_seconds=float(
                getattr(s, "chili_momentum_risk_auto_expire_pending_live_arm_seconds", 900.0)
            ),
        )


def resolve_effective_risk_policy() -> dict[str, Any]:
    """Full policy as JSON-safe dict (for snapshots and read APIs)."""
    p = MomentumAutomationRiskPolicy.from_settings()
    d = asdict(p)
    d["policy_version"] = POLICY_VERSION
    d["resolved_at_utc"] = datetime.now(timezone.utc).isoformat()
    return d


def effective_policy_summary() -> dict[str, Any]:
    """Compact summary for UI / automation strip."""
    p = MomentumAutomationRiskPolicy.from_settings()
    return {
        "policy_version": POLICY_VERSION,
        "max_concurrent_sessions": p.max_concurrent_sessions,
        "max_concurrent_live_sessions": p.max_concurrent_live_sessions,
        "max_spread_bps_paper": p.max_spread_bps_paper,
        "max_spread_bps_live": p.max_spread_bps_live,
        "max_estimated_slippage_bps": p.max_estimated_slippage_bps,
        "max_fee_to_target_ratio": p.max_fee_to_target_ratio,
        "viability_max_age_seconds": p.viability_max_age_seconds,
        "disable_live_if_governance_inhibit": p.disable_live_if_governance_inhibit,
    }


def build_session_risk_snapshot(
    *,
    policy_full: dict[str, Any],
    evaluation: dict[str, Any],
    viability_brief: dict[str, Any] | None,
    readiness_subset: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
    execution_family: str | None = None,
) -> dict[str, Any]:
    """Merge operator keys (e.g. arm_token) with frozen policy + evaluation."""
    snap: dict[str, Any] = dict(extra or {})
    snap[POLICY_SNAPSHOT_KEY] = effective_policy_summary()
    snap["momentum_risk_policy_resolved_utc"] = policy_full.get("resolved_at_utc")
    snap[RISK_SNAPSHOT_KEY] = {
        "policy_version": POLICY_VERSION,
        "evaluated_at_utc": evaluation.get("evaluated_at_utc"),
        "allowed": evaluation.get("allowed"),
        "severity": evaluation.get("severity"),
        "checks": evaluation.get("checks", []),
        "warnings": evaluation.get("warnings", []),
        "errors": evaluation.get("errors", []),
        "governance_state": evaluation.get("governance_state"),
        "freshness_state": evaluation.get("freshness_state"),
        "viability_state": evaluation.get("viability_state"),
    }
    if viability_brief is not None:
        snap["viability_brief"] = viability_brief
    if readiness_subset is not None:
        snap["execution_readiness_subset"] = readiness_subset
    # Frozen caps for runner enforcement (Phase 7+); do not overwrite after admission.
    snap["momentum_policy_caps"] = {
        "max_hold_seconds": int(policy_full.get("max_hold_seconds") or 86_400),
        "cooldown_after_stopout_seconds": policy_int_cap(policy_full, "cooldown_after_stopout_seconds", 300),
        # Equity-relative per-trade notional (no fixed-$ magic): a fraction of
        # account equity, frozen at admission; falls back to the fixed cap when
        # equity is unavailable. [[feedback_adaptive_no_magic]]
        "max_notional_per_trade_usd": equity_relative_notional_cap(
            policy_float_cap(policy_full, "max_notional_per_trade_usd", 500.0),
            execution_family,
        ),
        # Equity-relative per-trade max-loss (no fixed-$ magic); same fallback rules.
        "max_loss_per_trade_usd": equity_relative_loss_cap(
            policy_float_cap(policy_full, "max_loss_per_trade_usd", 50.0),
            execution_family,
        ),
    }
    return snap
