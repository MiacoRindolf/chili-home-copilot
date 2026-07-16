"""Cost-aware viability scoring per symbol × strategy family (neural path)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc

from .context import MomentumRegimeContext, VolatilityRegime
from .features import ExecutionReadinessFeatures
from .variants import MomentumStrategyFamily

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


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
    # LEVER 1 — when an extreme-vol explosive name is admitted live, it is admitted
    # RISK-BOUNDED: the runner multiplies its risk budget by ``risk_mult`` (<= 1.0).
    # 1.0 (default) is a strict no-op for every name that isn't risk-bounded.
    risk_bounded: bool = False
    risk_mult: float = 1.0

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
            "risk_bounded": self.risk_bounded,
            "risk_mult": round(self.risk_mult, 4),
        }


def _symbol_family_memory_adjust(db: "Session", symbol: str, family_id: str) -> float:
    """Boost/penalize from recent symbol × family outcomes (Phase 4b)."""
    from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant

    sym = (symbol or "").strip().upper()
    if sym in ("", "__AGGREGATE__"):
        return 0.0
    rows = (
        db.query(MomentumAutomationOutcome.return_bps)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumAutomationOutcome.variant_id)
        .filter(
            MomentumAutomationOutcome.symbol == sym,
            MomentumStrategyVariant.family == family_id,
            MomentumAutomationOutcome.return_bps.isnot(None),
        )
        .order_by(desc(MomentumAutomationOutcome.created_at))
        .limit(10)
        .all()
    )
    vals = [float(r[0]) for r in rows if r[0] is not None]
    n = len(vals)
    if n < 3:
        return 0.0
    wins = sum(1 for v in vals if v > 0)
    wr = wins / n
    if n >= 5 and wr > 0.55:
        return min(0.08, 0.05 * (wr - 0.55))
    if wr < 0.5:
        return -max(0.0, 0.1 * (0.5 - wr))
    return 0.0


def score_viability(
    symbol: str,
    family: MomentumStrategyFamily,
    ctx: MomentumRegimeContext,
    feats: ExecutionReadinessFeatures,
    *,
    db: "Session | None" = None,
) -> ViabilityResult:
    """Heuristic score in [0,1]; tightens live eligibility on spread/vol/fees."""
    warnings: list[str] = []
    base = 0.48
    risk_bounded = False
    risk_mult = 1.0

    # Explosive-quality (Ross) percentile for THIS symbol, if the scanner bridge
    # threaded it via ctx.meta. Read up-front so the extreme-vol eligibility gate
    # (LEVER 1) can consult it. None when absent (aggregate / non-bridge callers).
    explosive_score: float | None = None
    try:
        _es = (
            ctx.meta.get("ross_scores")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if isinstance(_es, dict) and symbol in _es:
            explosive_score = float(_es[symbol])
    except (TypeError, ValueError, AttributeError):
        explosive_score = None

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

    # Rolling range / breakout continuity (persisted on regime snapshot)
    rrs = (ctx.rolling_range_state or "").lower()
    if "compress" in rrs and ("breakout" in family.family_id or "impulse" in family.family_id):
        base += 0.02
    if "extended" in rrs and ("reclaim" in family.family_id or "vwap" in family.family_id or "ema" in family.family_id):
        base += 0.015
    boc = (ctx.breakout_continuity or "").lower()
    if boc in ("holding", "strong", "intact") and ("breakout" in family.family_id or "impulse" in family.family_id):
        base += 0.02

    # Continuous chop/expansion score from context meta (Phase 6c)
    ces = ctx.meta.get("chop_expansion_score")
    try:
        ces_f = float(ces) if ces is not None else None
    except (TypeError, ValueError):
        ces_f = None
    if ces_f is not None:
        if ("reclaim" in family.family_id or "vwap" in family.family_id) and ces_f < -0.25:
            base += 0.02
        if ("breakout" in family.family_id or "impulse" in family.family_id) and ces_f > 0.35:
            base += 0.02

    spread_bps = feats.spread_bps
    fee_ratio = feats.fee_to_target_ratio
    slip_bps = feats.slippage_estimate_bps
    drift = feats.bid_ask_drift_bps
    imb = feats.book_imbalance
    tape_z = feats.tape_velocity_z

    # Microstructure features (Phase 4a)
    if drift is not None:
        ad = abs(float(drift))
        if ad > 12.0:
            base -= 0.05
            warnings.append("High bid/ask drift — execution uncertainty")
        elif ad > 6.0:
            base -= 0.02
    if imb is not None:
        try:
            im = float(imb)
            if im > 0.12:
                base += 0.02
            elif im < -0.18:
                base -= 0.03
                warnings.append("Order book imbalance against long bias")
        except (TypeError, ValueError):
            pass
    if tape_z is not None:
        try:
            tz = float(tape_z)
            if tz < -2.0:
                base -= 0.04
                warnings.append("Heavy sell-side tape velocity")
            elif tz > 1.5:
                base += 0.015
        except (TypeError, ValueError):
            pass

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
        # LEVER 1 — extreme-vol / explosive eligibility. Legacy = blanket-block.
        # When the lever is on, an extreme-vol name that ALSO clears the explosive
        # floor + is tradable + has an OK spread (live_eligible still True here,
        # i.e. spread/slip/fee/tradable gates above didn't trip) becomes live-
        # eligible under RISK-BOUNDED (size-down) admission. Otherwise it stays
        # gated. Flag-off => blanket-block parity (live_eligible = False).
        from ....config import settings as _settings
        from .extreme_explosive_eligibility import evaluate_extreme_explosive

        _ee = evaluate_extreme_explosive(
            is_extreme_vol=True,
            explosive_score=explosive_score,
            product_tradable=feats.product_tradable,
            ok_spread=bool(live_eligible),
            enabled=bool(
                getattr(_settings, "chili_momentum_extreme_explosive_eligible_enabled", False)
            ),
            explosive_floor=float(
                getattr(_settings, "chili_momentum_extreme_explosive_floor", 0.7)
            ),
            risk_mult=float(
                getattr(_settings, "chili_momentum_extreme_explosive_risk_mult", 0.5)
            ),
        )
        if _ee.eligible:
            risk_bounded = True
            risk_mult = _ee.risk_mult
            warnings.append(
                f"Extreme-vol explosive admitted RISK-BOUNDED (x{_ee.risk_mult:.2f} size)"
            )
        else:
            live_eligible = False

    if db is not None:
        try:
            base += _symbol_family_memory_adjust(db, symbol, family.family_id)
        except Exception:
            pass

    # Ross momentum-quality tilt (M2): prefer EXPLOSIVE instruments (high relative
    # volume + already-moving + low float) — the selection edge a momentum
    # day-trader relies on. ``ross_score`` is a [0,1] percentile-blend from
    # ross_momentum.score_universe(), threaded via ctx.meta by the scanner bridge
    # (which now forwards the RVOL/gap/daily-change/float signals it used to
    # discard). Centered at 0.5 so it boosts above-median momentum and discounts
    # below-median; a strict no-op when the signal is absent, so aggregate ticks
    # and non-bridge callers are unaffected.
    try:
        _ross_scores = (
            ctx.meta.get("ross_scores")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if isinstance(_ross_scores, dict) and symbol in _ross_scores:
            from .ross_momentum import ROSS_QUALITY_VIABILITY_TILT

            _rqf = float(_ross_scores[symbol])
            base += ROSS_QUALITY_VIABILITY_TILT * (_rqf - 0.5)
            if _rqf >= 0.8:
                warnings.append(f"High Ross momentum quality ({_rqf:.2f})")
            elif _rqf <= 0.2:
                warnings.append(f"Low Ross momentum quality ({_rqf:.2f}) — generic setup")
    except (TypeError, ValueError, AttributeError):
        pass

    # E5: news-catalyst tilt — a mover with a known earnings catalyst is more
    # likely a real Ross gapper than a random spike. Additive boost (never a
    # penalty); no-op when the catalyst set is absent or for crypto. (catalyst.py)
    try:
        _cat_syms = (
            ctx.meta.get("catalyst_symbols")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if _cat_syms:
            from .catalyst import catalyst_viability_delta

            _cat_delta = catalyst_viability_delta(symbol, _cat_syms)
            if _cat_delta:
                base += _cat_delta
                warnings.append("News catalyst (earnings) — Ross-style")
    except (TypeError, ValueError, AttributeError):
        pass

    viability = max(0.0, min(1.0, base))

    rationale = (
        f"{family.label}: session={ctx.session_label} vol={ctx.vol_regime.value} "
        f"chop_exp={ctx.chop_expansion.value}; spread_bps={spread_bps} "
        f"fee_to_target={fee_ratio} drift={drift} imb={imb} tape_z={tape_z}"
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
        risk_bounded=risk_bounded,
        risk_mult=risk_mult,
    )
