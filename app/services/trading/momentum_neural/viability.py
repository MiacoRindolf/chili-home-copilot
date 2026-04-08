"""Cost-aware viability scoring per symbol × strategy family (neural path)."""

from __future__ import annotations

from dataclasses import dataclass

from .context import MomentumRegimeContext, VolatilityRegime
from .features import ExecutionReadinessFeatures
from .variants import MomentumStrategyFamily


@dataclass(frozen=True)
class ViabilityResult:
    symbol: str
    family_id: str
    family_version: int
    viability: float
    paper_eligible: bool
    live_eligible: bool
    freshness_hint: str
    regime_fit: str
    rationale: str
    warnings: tuple[str, ...]

    def to_public_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "family_id": self.family_id,
            "family_version": self.family_version,
            "viability": round(self.viability, 4),
            "paper_eligible": self.paper_eligible,
            "live_eligible": self.live_eligible,
            "freshness_hint": self.freshness_hint,
            "regime_fit": self.regime_fit,
            "rationale": self.rationale,
            "warnings": list(self.warnings),
        }


def score_viability(
    symbol: str,
    family: MomentumStrategyFamily,
    ctx: MomentumRegimeContext,
    feats: ExecutionReadinessFeatures,
) -> ViabilityResult:
    """Heuristic Phase-1 score in [0,1]; tightens live eligibility on spread/vol/fees."""
    warnings: list[str] = []
    base = 0.48

    # Session tilt (crypto liquidity clusters)
    if ctx.session_label in ("us", "europe"):
        base += 0.04
    if ctx.session_label == "asia":
        base += 0.02

    # Volatility: momentum scalps prefer normal/high, not extreme chop
    if ctx.vol_regime == VolatilityRegime.normal:
        base += 0.06
        regime_fit = "normal_vol_momentum_friendly"
    elif ctx.vol_regime == VolatilityRegime.high:
        base += 0.04
        regime_fit = "high_vol_faster_stops"
    elif ctx.vol_regime == VolatilityRegime.low:
        base -= 0.02
        regime_fit = "low_vol_tight_ranges"
        warnings.append("Low volatility — range chop risk for breakout families")
    else:
        base -= 0.08
        regime_fit = "extreme_vol_size_down"
        warnings.append("Extreme volatility — live size and slippage risk")

    # Family-specific nudges
    if "reclaim" in family.family_id or "vwap" in family.family_id or "ema" in family.family_id:
        if ctx.chop_expansion.value == "chop":
            base += 0.03
    if "breakout" in family.family_id or "impulse" in family.family_id:
        if ctx.chop_expansion.value == "expansion":
            base += 0.04

    spread_bps = feats.spread_bps
    fee_ratio = feats.fee_to_target_ratio
    slip_bps = feats.slippage_estimate_bps

    paper_eligible = True
    live_eligible = True

    if spread_bps is not None:
        if spread_bps > 25:
            base -= 0.12
            warnings.append("Wide spread — edge vs fees doubtful")
            live_eligible = False
        elif spread_bps > 12:
            base -= 0.05
            warnings.append("Elevated spread — caution for live scalps")
            live_eligible = False

    if slip_bps is not None and slip_bps > 15:
        base -= 0.06
        warnings.append("High slippage estimate")
        live_eligible = False

    if fee_ratio is not None and fee_ratio > 0.35:
        base -= 0.1
        warnings.append("Fee burden high vs target move")
        live_eligible = False

    if feats.product_tradable is False:
        live_eligible = False
        warnings.append("Product not tradable / metadata missing")

    if ctx.vol_regime == VolatilityRegime.extreme:
        live_eligible = False

    viability = max(0.0, min(1.0, base))

    rationale = (
        f"{family.label}: session={ctx.session_label} vol={ctx.vol_regime.value} "
        f"chop_exp={ctx.chop_expansion.value}; spread_bps={spread_bps} "
        f"fee_to_target={fee_ratio}"
    )

    return ViabilityResult(
        symbol=symbol,
        family_id=family.family_id,
        family_version=family.version,
        viability=viability,
        paper_eligible=paper_eligible,
        live_eligible=live_eligible and viability >= 0.42,
        freshness_hint="mesh_tick",
        regime_fit=regime_fit,
        rationale=rationale,
        warnings=tuple(warnings),
    )
