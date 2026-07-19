"""Cost-aware viability scoring per symbol × strategy family (neural path)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc

from ....config import settings
from .context import MomentumRegimeContext, VolatilityRegime
from .features import ExecutionReadinessFeatures
from .leveraged_etf import symbol_is_excluded_fund, symbol_is_leveraged_etf
from .variants import MomentumStrategyFamily

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ViabilitySettingsProjection:
    """Exact settings values consulted by the viability arithmetic.

    Values intentionally retain their runtime representation.  The core keeps
    the existing local ``bool``/``float`` coercion and fail-open behavior, while
    PAPER/Replay can content-bind this projection instead of reading the process
    settings singleton.
    """

    chili_momentum_ofi_tilt_weight: Any
    chili_momentum_ofi_threshold: Any
    chili_momentum_trade_flow_agreement_gain: Any
    chili_momentum_trade_flow_threshold: Any
    chili_momentum_a_setup_quality_floor_float_ceiling_shares: Any
    chili_momentum_explosive_rvol_floor: Any
    chili_momentum_a_setup_quality_floor_change_pct_min: Any
    chili_momentum_exclude_leveraged_etfs: Any
    chili_momentum_exclude_fund_structures_enabled: Any
    chili_momentum_live_eligible_max_spread_bps: Any
    chili_momentum_thin_spread_squeeze_lane_enabled: Any
    chili_momentum_thin_spread_squeeze_top_pctl: Any
    chili_momentum_thin_spread_ceiling_squeeze_slope: Any
    chili_momentum_risk_max_spread_bps_abs_cap: Any
    chili_momentum_live_eligible_allow_extreme_explosive: Any
    chili_momentum_a_setup_quality_floor_enabled: Any
    chili_momentum_no_signal_derank_enabled: Any
    chili_momentum_no_signal_derank_fraction: Any
    chili_momentum_catalyst_grade_gate_enabled: Any
    chili_momentum_dilution_history_derate_enabled: Any
    chili_momentum_theme_sympathy_enabled: Any
    chili_momentum_thick_tape_veto_enabled: Any
    chili_momentum_nonmonotonic_volume_enabled: Any
    chili_momentum_explosive_prequal_floor_enabled: Any
    chili_momentum_explosive_prequal_bar_ref: Any
    chili_momentum_explosive_prequal_margin: Any

    @classmethod
    def from_runtime(cls, source: Any) -> "ViabilitySettingsProjection":
        defaults = {
            "chili_momentum_ofi_tilt_weight": 0.015,
            "chili_momentum_ofi_threshold": 0.25,
            "chili_momentum_trade_flow_agreement_gain": 0.5,
            "chili_momentum_trade_flow_threshold": 0.25,
            "chili_momentum_a_setup_quality_floor_float_ceiling_shares": 20_000_000.0,
            "chili_momentum_explosive_rvol_floor": 3.0,
            "chili_momentum_a_setup_quality_floor_change_pct_min": 10.0,
            "chili_momentum_exclude_leveraged_etfs": True,
            "chili_momentum_exclude_fund_structures_enabled": True,
            "chili_momentum_live_eligible_max_spread_bps": 0.0,
            "chili_momentum_thin_spread_squeeze_lane_enabled": True,
            "chili_momentum_thin_spread_squeeze_top_pctl": 0.80,
            "chili_momentum_thin_spread_ceiling_squeeze_slope": 1.0,
            "chili_momentum_risk_max_spread_bps_abs_cap": 1500.0,
            "chili_momentum_live_eligible_allow_extreme_explosive": True,
            "chili_momentum_a_setup_quality_floor_enabled": False,
            "chili_momentum_no_signal_derank_enabled": False,
            "chili_momentum_no_signal_derank_fraction": 1.0,
            "chili_momentum_catalyst_grade_gate_enabled": True,
            "chili_momentum_dilution_history_derate_enabled": True,
            "chili_momentum_theme_sympathy_enabled": True,
            "chili_momentum_thick_tape_veto_enabled": True,
            "chili_momentum_nonmonotonic_volume_enabled": True,
            "chili_momentum_explosive_prequal_floor_enabled": True,
            "chili_momentum_explosive_prequal_bar_ref": 0.56,
            "chili_momentum_explosive_prequal_margin": 0.02,
        }
        values = {name: getattr(source, name, default) for name, default in defaults.items()}
        # The legacy trade-flow threshold defaults dynamically to the resolved
        # OFI threshold when absent.  Pydantic settings normally provides both,
        # but preserve the old fallback for arbitrary test/runtime sources.
        if not hasattr(source, "chili_momentum_trade_flow_threshold"):
            values["chili_momentum_trade_flow_threshold"] = values[
                "chili_momentum_ofi_threshold"
            ]
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class ViabilityExternalInputs:
    """All results previously obtained through classifiers, DB reads, or helpers
    whose own implementation reads global settings.

    The explicit core only consumes these scalar facts.  The default live
    wrapper below resolves them exactly as before; a sealed consumer must record
    them and their upstream capture provenance instead of re-running the
    resolver against current state.
    """

    leveraged_etf: bool
    excluded_fund: bool
    symbol_family_memory_adjust: float
    dilution_history_derate: float
    ross_rvol: float | None
    ross_change_pct: float | None
    ross_float_shares: float | None
    squeeze_fuel_rank_pct: float | None
    below_explosive_floor: bool
    catalyst_delta: float
    catalyst_grade_delta: float
    fake_catalyst_delta: float
    sympathy_delta: float
    theme_sympathy_delta: float
    close_strength_delta: float
    thick_tape_delta: float
    nonmonotonic_volume_delta: float
    ross_quality_viability_tilt: float

    def to_dict(self) -> dict[str, Any]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }

    @classmethod
    def neutral(
        cls,
        *,
        ross_quality_viability_tilt: float = 0.0,
        leveraged_etf: bool = False,
        excluded_fund: bool = False,
    ) -> "ViabilityExternalInputs":
        return cls(
            leveraged_etf=leveraged_etf,
            excluded_fund=excluded_fund,
            symbol_family_memory_adjust=0.0,
            dilution_history_derate=0.0,
            ross_rvol=None,
            ross_change_pct=None,
            ross_float_shares=None,
            squeeze_fuel_rank_pct=None,
            below_explosive_floor=False,
            catalyst_delta=0.0,
            catalyst_grade_delta=0.0,
            fake_catalyst_delta=0.0,
            sympathy_delta=0.0,
            theme_sympathy_delta=0.0,
            close_strength_delta=0.0,
            thick_tape_delta=0.0,
            nonmonotonic_volume_delta=0.0,
            ross_quality_viability_tilt=float(ross_quality_viability_tilt),
        )


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
    # LEVER 1 win-win: True when this name is admitted LIVE despite extreme-vol /
    # missing-rvol ONLY because it is a GENUINE explosive Ross-class mover — it must
    # then be sized RISK-BOUNDED (the live_runner extreme-vol size-down lever reads
    # this so the worst-case loss is bounded the same as a normal trade). Default
    # False everywhere else (byte-identical when the lever is OFF or N/A).
    extreme_vol_risk_bounded: bool = False

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
            "extreme_vol_risk_bounded": self.extreme_vol_risk_bounded,
        }


def _symbol_family_memory_adjust(db: "Session", symbol: str, family_id: str) -> float:
    """Boost/penalize from recent symbol × family outcomes (Phase 4b)."""
    from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant

    # Broker-truth label switch (mig309). Flag-OFF: the accessor returns the legacy
    # return_bps (is_reconciled=True) so this viability nudge is byte-identical. Flag-ON:
    # reconciled-live rows use the broker-true return_bps and unreconciled rows (incl. all
    # paper) are EXCLUDED — the symbol×family memory boost/penalty then reflects only
    # broker-true track record. Full ORM rows loaded so the accessor can read broker_*
    # columns. Mirrors the meta_label/risk_evaluator routing pattern.
    from .outcome_reconcile import authoritative_label_for_outcome

    sym = (symbol or "").strip().upper()
    if sym in ("", "__AGGREGATE__"):
        return 0.0
    rows = (
        db.query(MomentumAutomationOutcome)
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
    vals: list[float] = []
    for o in rows:
        _pnl, rb, _win, is_rec = authoritative_label_for_outcome(o)
        if not is_rec or rb is None:
            continue
        vals.append(float(rb))
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


def _resolve_viability_external_inputs(
    symbol: str,
    family: MomentumStrategyFamily,
    ctx: MomentumRegimeContext,
    feats: ExecutionReadinessFeatures,
    *,
    db: "Session | None",
    settings_projection: ViabilitySettingsProjection,
    captured_leveraged_etf: bool | None = None,
    captured_excluded_fund: bool | None = None,
    decision_as_of: "datetime | None" = None,
) -> ViabilityExternalInputs:
    """Resolve helper/DB facts for the live wrapper or an explicit capture.

    ``captured_*`` values deliberately bypass the process-global fundamentals
    caches.  The capture producer resolves and records the exact instrument
    metadata once, then supplies the two pure classifier results here.  The
    default live wrapper leaves them as ``None`` and preserves the historical
    lookup path byte-for-byte.
    """

    if captured_leveraged_etf is not None and type(captured_leveraged_etf) is not bool:
        raise TypeError("captured_leveraged_etf must be bool or None")
    if captured_excluded_fund is not None and type(captured_excluded_fund) is not bool:
        raise TypeError("captured_excluded_fund must be bool or None")
    leveraged = False
    if bool(settings_projection.chili_momentum_exclude_leveraged_etfs):
        leveraged = (
            captured_leveraged_etf
            if captured_leveraged_etf is not None
            else bool(symbol_is_leveraged_etf(symbol))
        )
    if leveraged:
        # Preserve the original hard-veto short circuit: no fund classifier,
        # DB memory, catalyst helper, or other external read occurs afterward.
        return ViabilityExternalInputs.neutral(leveraged_etf=True)
    excluded_fund = False
    if bool(settings_projection.chili_momentum_exclude_fund_structures_enabled):
        excluded_fund = (
            captured_excluded_fund
            if captured_excluded_fund is not None
            else bool(symbol_is_excluded_fund(symbol))
        )
    memory_adjust = 0.0
    if db is not None:
        try:
            memory_adjust = float(
                _symbol_family_memory_adjust(db, symbol, family.family_id)
            )
        except Exception:
            memory_adjust = 0.0

    signal = None
    try:
        raw_signals = (
            feats.meta.get("ross_signals")
            if isinstance(getattr(feats, "meta", None), dict)
            else None
        )
        signal = raw_signals.get(symbol) if isinstance(raw_signals, dict) else None
    except (TypeError, AttributeError):
        signal = None
    rvol = change = float_shares = squeeze_rank = None
    below_floor = False
    if isinstance(signal, dict) and signal:
        try:
            from .ross_momentum import (
                _extract_pillars,
                _first_float,
                _to_float,
                below_explosive_floor,
            )

            rvol, change, _liquidity, _tradeable_liquidity = _extract_pillars(
                signal
            )
            float_shares = _first_float(signal, "float_shares")
            squeeze_rank = _to_float(signal.get("squeeze_fuel_rank_pct"))
            below_floor = bool(below_explosive_floor(signal))
        except (TypeError, ValueError, AttributeError, ImportError):
            rvol = change = float_shares = squeeze_rank = None
            below_floor = False

    ross_quality_tilt = 0.0
    try:
        ross_scores = (
            ctx.meta.get("ross_scores")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        tilt_is_used = isinstance(ross_scores, dict) and (
            symbol in ross_scores
            or (
                bool(ross_scores)
                and bool(
                    settings_projection.chili_momentum_no_signal_derank_enabled
                )
            )
        )
        if tilt_is_used:
            # Match the legacy scorer's lazy dependency: do not import the Ross
            # module for names whose scoring path never consults this constant.
            from .ross_momentum import ROSS_QUALITY_VIABILITY_TILT

            ross_quality_tilt = float(ROSS_QUALITY_VIABILITY_TILT)
    except (TypeError, ValueError, AttributeError):
        ross_quality_tilt = 0.0

    catalyst_delta = catalyst_grade_delta = fake_delta = 0.0
    sympathy_delta = theme_delta = close_delta = 0.0
    meta = ctx.meta if isinstance(getattr(ctx, "meta", None), dict) else {}
    try:
        catalyst_symbols = meta.get("catalyst_symbols")
        if catalyst_symbols:
            from .catalyst import catalyst_viability_delta

            catalyst_delta = float(
                catalyst_viability_delta(
                    symbol,
                    catalyst_symbols,
                    hot_tape=bool(meta.get("hot_tape")),
                    hq_country=(meta.get("symbol_countries") or {}).get(symbol),
                    theme_symbols=(
                        set(meta.get("theme_symbols"))
                        if meta.get("theme_symbols")
                        else None
                    ),
                    weak_symbols=(
                        set(meta.get("weak_catalyst_symbols"))
                        if meta.get("weak_catalyst_symbols")
                        else None
                    ),
                )
            )
    except (TypeError, ValueError, AttributeError):
        catalyst_delta = 0.0
    try:
        if bool(settings_projection.chili_momentum_catalyst_grade_gate_enabled):
            weak = meta.get("weak_catalyst_symbols")
            strong = meta.get("strong_catalyst_symbols")
            fake = meta.get("fake_catalyst_symbols")
            actions = meta.get("catalyst_action_deltas")
            if weak or strong or fake:
                from .catalyst import catalyst_grade_selection_delta

                catalyst_grade_delta = float(
                    catalyst_grade_selection_delta(
                        symbol,
                        weak_symbols=set(weak) if weak else None,
                        strong_symbols=set(strong) if strong else None,
                        fake_symbols=set(fake) if fake else None,
                        action_deltas=(
                            dict(actions)
                            if isinstance(actions, dict) and actions
                            else None
                        ),
                    )
                )
    except (TypeError, ValueError, AttributeError):
        catalyst_grade_delta = 0.0
    try:
        fake = meta.get("fake_catalyst_symbols")
        if fake:
            from .catalyst import fake_catalyst_viability_delta

            fake_delta = float(fake_catalyst_viability_delta(symbol, set(fake)))
    except (TypeError, ValueError, AttributeError):
        fake_delta = 0.0
    try:
        sympathy = meta.get("sympathy_symbols")
        if sympathy:
            from .catalyst import sympathy_viability_delta

            sympathy_delta = float(sympathy_viability_delta(symbol, set(sympathy)))
    except (TypeError, ValueError, AttributeError):
        sympathy_delta = 0.0
    try:
        if bool(settings_projection.chili_momentum_theme_sympathy_enabled):
            theme = meta.get("theme_sympathy_symbols")
            if theme:
                from .theme_detector import theme_sympathy_viability_delta

                theme_delta = float(
                    theme_sympathy_viability_delta(symbol, set(theme))
                )
    except (TypeError, ValueError, AttributeError):
        theme_delta = 0.0
    try:
        priors = meta.get("close_strength_priors")
        if priors:
            from .catalyst import close_strength_viability_delta

            close_delta = float(close_strength_viability_delta(symbol, priors))
    except (TypeError, ValueError, AttributeError):
        close_delta = 0.0

    dilution_derate = 0.0
    try:
        if bool(settings_projection.chili_momentum_dilution_history_derate_enabled):
            strong = meta.get("strong_catalyst_symbols")
            fresh_squeeze = bool(
                strong and str(symbol or "").strip().upper() in set(strong)
            )
            if not fresh_squeeze:
                from .dilution_history import dilution_history_derate

                dilution_derate = float(
                    dilution_history_derate(
                        db,
                        symbol,
                        now_utc=decision_as_of,
                    )
                )
    except (TypeError, ValueError, AttributeError):
        dilution_derate = 0.0

    thick_tape_delta = nonmonotonic_delta = 0.0
    try:
        if (
            bool(settings_projection.chili_momentum_thick_tape_veto_enabled)
            and "-USD" not in str(symbol or "").upper()
        ):
            signals = (
                feats.meta.get("ross_signals")
                if isinstance(getattr(feats, "meta", None), dict)
                else None
            )
            if signals:
                from .distribution_filters import thick_tape_discount

                thick_tape_delta = float(
                    thick_tape_discount(
                        symbol, signals, atr_pct=getattr(ctx, "atr_pct", None)
                    )
                )
    except (TypeError, ValueError, AttributeError):
        thick_tape_delta = 0.0
    try:
        if (
            bool(settings_projection.chili_momentum_nonmonotonic_volume_enabled)
            and "-USD" not in str(symbol or "").upper()
        ):
            signals = (
                feats.meta.get("ross_signals")
                if isinstance(getattr(feats, "meta", None), dict)
                else None
            )
            if signals:
                from .distribution_filters import nonmonotonic_volume_rolloff

                nonmonotonic_delta = float(
                    nonmonotonic_volume_rolloff(symbol, signals)
                )
    except (TypeError, ValueError, AttributeError):
        nonmonotonic_delta = 0.0

    return ViabilityExternalInputs(
        leveraged_etf=leveraged,
        excluded_fund=excluded_fund,
        symbol_family_memory_adjust=memory_adjust,
        dilution_history_derate=dilution_derate,
        ross_rvol=rvol,
        ross_change_pct=change,
        ross_float_shares=float_shares,
        squeeze_fuel_rank_pct=squeeze_rank,
        below_explosive_floor=below_floor,
        catalyst_delta=catalyst_delta,
        catalyst_grade_delta=catalyst_grade_delta,
        fake_catalyst_delta=fake_delta,
        sympathy_delta=sympathy_delta,
        theme_sympathy_delta=theme_delta,
        close_strength_delta=close_delta,
        thick_tape_delta=thick_tape_delta,
        nonmonotonic_volume_delta=nonmonotonic_delta,
        ross_quality_viability_tilt=ross_quality_tilt,
    )


def score_viability_explicit(
    symbol: str,
    family: MomentumStrategyFamily,
    ctx: MomentumRegimeContext,
    feats: ExecutionReadinessFeatures,
    *,
    settings: ViabilitySettingsProjection,
    external: ViabilityExternalInputs,
) -> ViabilityResult:
    """Pure existing viability arithmetic over fully explicit inputs."""
    if type(settings) is not ViabilitySettingsProjection:
        raise TypeError("settings must be ViabilitySettingsProjection")
    if type(external) is not ViabilityExternalInputs:
        raise TypeError("external must be ViabilityExternalInputs")
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
    # Ross-style EXTREME-MOVER override (operator 2026-06-16, the TDIC/SUGP/OBAI miss):
    # the biggest explosive movers Ross TRADES are often dilution-prone low-floats with
    # choppy/selling L2 — CHILI's dilution-fade + L2-imbalance SELECTION de-rates were
    # cancelling their Ross-quality boost and keeping them OUT of the armed set (TDIC
    # +103%, Ross +$3,701 in it, NEVER armed). For an EXTREME Ross-quality name (>= the
    # existing 0.8 "High" threshold) those de-rates are SWING/overnight concerns, not
    # intraday-momentum vetoes (Ross is flat by close) — so SUPPRESS them at SELECTION and
    # let the name arm; the dilution risk is reframed to an EXIT/no-overnight constraint.
    # FAVORABLE (long-side) boosts are kept; only the de-rates skip. Non-extreme names
    # (rqf < 0.8 or no ross score) are BYTE-IDENTICAL.
    _extreme_mover = False
    try:
        _rs = ctx.meta.get("ross_scores") if isinstance(getattr(ctx, "meta", None), dict) else None
        if isinstance(_rs, dict) and symbol in _rs:
            _extreme_mover = float(_rs[symbol]) >= 0.8
    except (TypeError, ValueError, AttributeError):
        pass
    if imb is not None:
        try:
            im = float(imb)
            if im > 0.12:
                base += 0.02
            elif im < -0.18 and not _extreme_mover:
                base -= 0.03
                warnings.append("Order book imbalance against long bias")
        except (TypeError, ValueError):
            pass
    # Order-flow imbalance (OFI) + micro-price agreement tilt. Research's top L2
    # short-horizon predictor (Cont/Kukanov/Stoikov) used as a SMALL long-bias
    # SELECTION tilt — fires only when OFI and micro-price AGREE (guards thin-book
    # / flicker / spoof). Weight is env-tunable (set 0 to disable without redeploy);
    # validated by live A/B + instant rollback, since the literature edge is
    # contemporaneous and may sit near Coinbase fees.
    ofi = feats.ofi
    mpe = feats.micro_price_edge
    if ofi is not None and mpe is not None:
        try:
            w = float(getattr(settings, "chili_momentum_ofi_tilt_weight", 0.015) or 0.0)
            thr = float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25)
            if w > 0.0:
                o = float(ofi)
                m = float(mpe)
                # trade_flow (executed-tape aggressor imbalance [-1,1]; Ross's "ask getting eaten")
                # CONFIRMS the book signal — it NEVER fires a tilt alone; it SCALES the magnitude of
                # the already-OFI+micro-confirmed tilt by a bounded premium (1+g), and ONLY when its
                # sign AGREES with OFI + clears the threshold. None / contra / below-threshold ->
                # mult==1.0 (byte-identical to the bare OFI tilt -> no regression when the tape is
                # absent, the common case). g<=1 hard-caps the 3-way tilt at w*(1+g)<2w (never
                # double-counts the correlated buying pressure). g=0 -> trade_flow inert (kill-switch).
                tf = feats.trade_flow
                g = float(getattr(settings, "chili_momentum_trade_flow_agreement_gain", 0.5) or 0.0)
                tf_thr = float(getattr(settings, "chili_momentum_trade_flow_threshold", thr) or thr)
                if o > thr and m > 0:
                    base += w * (1.0 + g if (tf is not None and tf > tf_thr) else 1.0)
                elif o < -thr and m < 0 and not _extreme_mover:
                    base -= w * (1.0 + g if (tf is not None and tf < -tf_thr) else 1.0)
                    warnings.append("Order-flow imbalance against long bias (OFI+micro)")
        except (TypeError, ValueError):
            pass
    if tape_z is not None:
        try:
            tz = float(tape_z)
            if tz < -2.0 and not _extreme_mover:
                base -= 0.04
                warnings.append("Heavy sell-side tape velocity")
            elif tz > 1.5:
                base += 0.015
        except (TypeError, ValueError):
            pass

    paper_eligible = True
    live_eligible = True
    # RISK-BOUNDED size-down marker (LEVER-1 extreme-vol relax AND the thin-spread
    # squeeze carve-out both set this True). Initialized ONCE here — above the spread
    # block (line ~296) — so the thin-spread carve-out's assignment isn't clobbered by
    # a later re-init. Plumbed to the ViabilityResult field + the live_runner size-down.
    _extreme_vol_risk_bounded = False

    # HOIST for the explosive-prequal score floor (applied at viability=max(...) below).
    # The LEVER-1 explosive block (~line 440) extracts these INSIDE a nested
    # equity-only / non-empty-signal try-block, so they are NOT in scope on the crypto /
    # flag-off / empty-signal paths. Hoist them here with SAFE DEFAULTS (values None;
    # ceil/floors = the documented Ross floors) so the prequal-floor block below NEVER
    # NameErrors. The LEVER-1 block REBINDS these from the live signal when present; when
    # it does not run, the prequal A-setup conjunction fails CLOSED (None values), so the
    # floor is a strict no-op — byte-identical to the pre-floor path.
    _float_x: float | None = None
    _rvol_x: float | None = None
    _chg_x: float | None = None
    _float_ceil_x: float = float(
        getattr(
            settings,
            "chili_momentum_a_setup_quality_floor_float_ceiling_shares",
            20_000_000.0,
        )
        or 20_000_000.0
    )
    _rvol_floor_x: float = float(
        getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 3.0
    )
    _chg_floor_x: float = float(
        getattr(
            settings,
            "chili_momentum_a_setup_quality_floor_change_pct_min",
            10.0,
        )
        or 10.0
    )

    # Ross lane = low-float small-cap COMMON stock. HARD-VETO leveraged/inverse ETPs
    # (SOXS/SQQQ/SOXL + the Tradr/Defiance/T-REX "2X Short XXX" single-stock wave that
    # flooded the lane 2026-06-23: 11 of 18 eligible names were these). They are geared
    # index/single-name trackers, not the low-float squeezes the lane trades. This
    # forces BOTH eligibility flags False at the single authoritative producer, so they
    # cannot arm in live OR paper — upgrading the prior soft arm-queue down-weight
    # (#790), which LEAKED (a fresh-ross ETF at ×0.5 still outranked stale-ross real
    # companies, so SOXS armed + traded breakeven). Reuses the adaptive name-based
    # classifier (no hardcoded list); fail-open there means a fundamentals miss does
    # not veto a real mover — the arm-queue quality tier is the backstop. Default-ON;
    # kill-switch CHILI_MOMENTUM_EXCLUDE_LEVERAGED_ETFS=0.
    if bool(getattr(settings, "chili_momentum_exclude_leveraged_etfs", True)) and external.leveraged_etf:
        return ViabilityResult(
            symbol=symbol,
            family_id=family.family_id,
            family_version=family.version,
            viability=0.0,
            paper_eligible=False,
            live_eligible=False,
            freshness_hint="mesh_tick",
            regime_fit="leveraged_inverse_etf_vetoed",
            rationale=f"{symbol}: leveraged/inverse ETF — excluded from Ross momentum lane (low-float common only)",
            warnings=("Leveraged/inverse ETF vetoed from momentum lane",),
        )

    # A8 (Ross CLRO-lesson 2026-07-02): REIT / closed-end-fund NAME structures. Unlike
    # the leveraged-ETF HARD veto above, this is a soft DOWN-WEIGHT (score derate) — a
    # real low-float mover still outranks it, but a fund/trust vehicle no longer wastes a
    # watch slot (Ross passed WHLR "Wheeler Real Estate Investment Trust" at a glance;
    # CHILI armed it at 5:50 ET). Reuses the adaptive name classifier (no hardcoded list);
    # fail-open there (a fundamentals miss classifies False) so a real mover is never
    # wrongly demoted. Default-ON; kill-switch CHILI_MOMENTUM_EXCLUDE_FUND_STRUCTURES=0.
    if bool(getattr(settings, "chili_momentum_exclude_fund_structures_enabled", True)) and external.excluded_fund:
        base -= 0.12
        warnings.append("REIT / closed-end fund structure — down-weighted from Ross momentum lane")

    if spread_bps is not None:
        # DERATE the score for wider spreads (tighter books rank higher) but do NOT
        # disqualify the wide-spread EXPLOSIVE low-float movers the Ross lane EXISTS to
        # trade — they run ~40-90bps and are entered with marketable-limit / maker orders
        # that cross the spread; the liquidity floor + risk-first sizing already bound
        # tradeability/cost. The old hard `live_eligible=False` at 12/25bps SILENTLY
        # disqualified every squeeze (1,495 "Not live-eligible" entry blocks 2026-06-25 —
        # ILLR 38-91bps, FCUV 70-87bps — the exact names the lane targets). Disqualify ONLY
        # a TRULY toxic spread, via ONE documented ceiling (default 300bps = broken/halted
        # quote), not "elevated".
        if spread_bps > 25:
            base -= 0.12
            warnings.append("Wide spread — edge vs fees doubtful")
        elif spread_bps > 12:
            base -= 0.05
            warnings.append("Elevated spread — caution for live scalps")
        _max_spread_bps = float(getattr(settings, "chili_momentum_live_eligible_max_spread_bps", 0.0) or 0.0)
        if _max_spread_bps > 0.0 and spread_bps > _max_spread_bps:
            # THIN/TOXIC-SPREAD SQUEEZE CARVE-OUT (default ON; flag-off => byte-identical
            # binary decline). A genuine TOP squeeze-fuel + high-RVOL mover is the exact
            # name the lane exists to trade; the marketable-LIMIT entry + notional guard +
            # risk-first sizing already bound the toxic-fill downside the zero-fills fix
            # solved. So instead of a flat decline at _max_spread_bps, raise the ceiling
            # EM/squeeze-percentile-scaled for ONLY the top-percentile squeeze names, and
            # mark them for RISK-BOUNDED size-down (reuses the LEVER-1 extreme_vol path).
            # ORDINARY names keep the flat decline (zero-fills protection intact).
            _thin_ok = False
            if (
                bool(getattr(settings, "chili_momentum_thin_spread_squeeze_lane_enabled", True))
                and "-USD" not in str(symbol or "").upper()
            ):
                try:
                    _rvol_th = external.ross_rvol
                    _sq_rank_th = external.squeeze_fuel_rank_pct
                    if _rvol_th is not None or _sq_rank_th is not None:
                        _rvol_floor_th = float(
                            getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 3.0
                        )
                        _sq_top_th = float(
                            getattr(settings, "chili_momentum_thin_spread_squeeze_top_pctl", 0.80) or 0.80
                        )
                        # TRIPLE gate (all from REAL batch data, no magic absolute): top
                        # within-batch squeeze percentile + present RVOL at/above the explosive
                        # floor + the lane's own affirmative explosiveness (not below the floor).
                        _rvol_ok_th = _rvol_th is not None and _rvol_th >= _rvol_floor_th
                        _sq_ok_th = _sq_rank_th is not None and _sq_rank_th >= _sq_top_th
                        if _rvol_ok_th and _sq_ok_th and not external.below_explosive_floor:
                            # EM/squeeze-scaled ceiling: base ceiling * (1 + slope*squeeze_excess),
                            # hard-capped by the abs broken-quote ceiling. squeeze_excess in [0,1]
                            # is how far past the top percentile this name sits, so only the most
                            # squeeze-prone names get the widest tolerance — no flat relax.
                            _slope_th = float(
                                getattr(settings, "chili_momentum_thin_spread_ceiling_squeeze_slope", 1.0) or 0.0
                            )
                            _abs_cap_th = float(
                                getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 1500.0) or 1500.0
                            )
                            _excess_th = max(0.0, min(1.0, (float(_sq_rank_th) - _sq_top_th) / max(1e-6, 1.0 - _sq_top_th)))
                            _ceil_th = _max_spread_bps * (1.0 + max(0.0, _slope_th) * _excess_th)
                            # HARD broken-quote backstop. The squeeze formula's OWN maximum is
                            # base*(1+slope) (@rank=1.0); the abs broken-quote cap is a FLOOR for
                            # that backstop, never a clip BELOW the documented adaptive range — so
                            # in this branch where the abs_cap == the base live ceiling (300bps)
                            # the carve-out is not silently neutralized. A spread beyond the
                            # formula max (a genuine broken/halted book) still declines.
                            _formula_max_th = _max_spread_bps * (1.0 + max(0.0, _slope_th))
                            _ceil_th = min(_ceil_th, max(_max_spread_bps, _abs_cap_th, _formula_max_th))
                            if spread_bps <= _ceil_th:
                                _thin_ok = True
                                _extreme_vol_risk_bounded = True
                                warnings.append(
                                    f"Thin-spread squeeze carve-out: {spread_bps:.0f}bps within "
                                    f"squeeze ceiling {_ceil_th:.0f}bps (sq_rank={_sq_rank_th:.2f}, "
                                    f"rvol={_rvol_th:g}) — LIVE with risk-bounded sizing"
                                )
                except (TypeError, ValueError, AttributeError, ImportError):
                    _thin_ok = False
            if not _thin_ok:
                warnings.append(f"Spread {spread_bps:.0f}bps exceeds live ceiling {_max_spread_bps:.0f}bps — untradeable")
                live_eligible = False

    if slip_bps is not None and slip_bps > 15:
        base -= 0.06
        warnings.append("High slippage estimate")  # derate only — do NOT disqualify (handled by entry method + sizing)

    if fee_ratio is not None and fee_ratio > 0.35:
        base -= 0.1
        warnings.append("Fee burden high vs target move")  # derate only — do NOT disqualify the lane's target movers

    if feats.product_tradable is False:
        live_eligible = False
        warnings.append("Product not tradable / metadata missing")

    # LEVER 1 — WIN-WIN gate predicate. Compute ONCE: is this a GENUINE explosive
    # Ross-class mover that is also tradable and within the live spread ceiling? It
    # reuses the lane's EXISTING explosiveness floor (ross_momentum.below_explosive_floor
    # — low-float + change, FAIL-OPEN on absent rvol, same fields _extract_pillars reads)
    # and the SAME product_tradable + win-win spread cap already evaluated above. EQUITY
    # only (crypto 24h RVOL/change semantics differ). Used by (a) the extreme-vol relax
    # and (b) the A-setup None-rvol fail-open below. Absent signal / crypto / any error =>
    # _is_genuine_explosive stays False => byte-identical. The win-win INVARIANT lives
    # here: a name AFFIRMATIVELY below the floor, untradeable, or with a toxic spread can
    # NEVER set this True, so it is still gated.
    _allow_extreme_explosive = bool(
        getattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    )
    _is_genuine_explosive = False
    # NOTE: _extreme_vol_risk_bounded is initialized once near the top (after
    # live_eligible) so the thin-spread carve-out (above) is not clobbered here.
    if _allow_extreme_explosive and "-USD" not in str(symbol or "").upper():
        try:
            if any(
                value is not None
                for value in (
                    external.ross_rvol,
                    external.ross_change_pct,
                    external.ross_float_shares,
                )
            ):
                _spread_ok = True
                _max_sp = float(
                    getattr(settings, "chili_momentum_live_eligible_max_spread_bps", 0.0) or 0.0
                )
                if _max_sp > 0.0 and spread_bps is not None and spread_bps > _max_sp:
                    _spread_ok = False  # toxic/broken spread — never a win-win mover
                # AFFIRMATIVE explosiveness confirmation (does NOT depend on the
                # default-OFF A-setup quality floor). below_explosive_floor() FAILS OPEN
                # on absent rvol/change (ross_momentum.py:598-600) and never checks float,
                # so a non-empty-but-junk signal (e.g. {'price': 1.0} with no float / no
                # rvol / no change) would otherwise clear it and be admitted LIVE on the
                # extreme-vol relax path. Require at least ONE datum that AFFIRMATIVELY
                # shows the name is explosive: float-confirmed low float (<= the A-setup
                # float ceiling), OR a present rvol at/above the explosive floor, OR a
                # present change at/above the change floor. A name with no such datum is a
                # selection-quality reject — _is_genuine_explosive stays False and the
                # prior blanket extreme-vol block applies (byte-identical when the signal
                # carries no explosiveness data).
                # Rebind the hoisted VALUES from the live signal (the floors
                # _float_ceil_x/_rvol_floor_x/_chg_floor_x are already computed at the
                # hoist near the top from the SAME settings — single source of truth).
                _rvol_x = external.ross_rvol
                _chg_x = external.ross_change_pct
                _float_x = external.ross_float_shares
                _affirm_explosive = (
                    (
                        _float_x is not None
                        and _float_x > 0
                        and (_float_ceil_x <= 0 or _float_x <= _float_ceil_x)
                    )
                    or (_rvol_x is not None and _rvol_x >= _rvol_floor_x)
                    or (_chg_x is not None and _chg_x >= _chg_floor_x)
                )
                if (
                    feats.product_tradable is not False
                    and _spread_ok
                    and _affirm_explosive
                    and not external.below_explosive_floor
                ):
                    _is_genuine_explosive = True
        except (TypeError, ValueError, AttributeError):
            _is_genuine_explosive = False

    if ctx.vol_regime == VolatilityRegime.extreme:
        # LEVER 1: a GENUINE explosive Ross-class mover (clears the floor + tradable +
        # spread-OK) is NOT blanket-blocked on extreme-vol ALONE — it stays live-eligible
        # but is flagged for RISK-BOUNDED sizing (live_runner sizes it DOWN so worst-case
        # loss is bounded the same as a normal trade). A non-genuine extreme-vol name is
        # STILL blocked. Flag OFF / not-genuine => the prior blanket block (byte-identical).
        if _is_genuine_explosive:
            _extreme_vol_risk_bounded = True
            warnings.append(
                "Extreme vol on a genuine explosive mover — LIVE with risk-bounded sizing"
            )
        else:
            live_eligible = False

    # Ross gap #3: absolute explosiveness FLOOR (videos 01/05/17/29/36). The pipeline
    # marked the EQUITY symbols whose raw RVOL/change fall below Ross's hard floors
    # (~5x / ~10%); a name below them is not a live setup no matter its within-batch
    # percentile rank, so it is dropped from LIVE eligibility only (pool membership +
    # paper scoring untouched). Crypto is never in the list (different 24h semantics);
    # absent list -> no-op (fail-open).
    try:
        _below = (
            ctx.meta.get("ross_below_floor")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if _below and symbol in _below:
            live_eligible = False
            warnings.append("Below Ross explosiveness floor (RVOL/change) — not a live setup")
    except (TypeError, AttributeError):
        pass

    # A-SETUP QUALITY FLOOR (LIVE eligibility only; PAPER untouched). The 'puro talo'
    # root: the lane had no quality floor, so it armed/traded ANYTHING that fired a
    # trigger -> B/C junk + small losses (AREC: 107M float, rvol 5.9, +12.9% armed+lost;
    # CODI: float=None/rvol 0/+0% queued on a bare pullback). Ross trades A-setups ONLY:
    # LOW-FLOAT EXPLOSIVE names (UPC 648K/+227%, SDOT 744K/+84%, WSHP ~11M/+47%). This
    # gate keeps a name LIVE-tradeable ONLY if ALL hold: (1) low float (<= ceiling — THE
    # primary discriminator), (2) real RVOL >= the explosive-rvol floor, (3) meaningful
    # change >= the change floor, (4) FLOAT-CONFIRMED (fail-CLOSED on missing/None/0 —
    # cannot confirm low-float => reject; this also rejects empty-signal scanner names).
    # RESTRICT-only: it can ONLY set live_eligible False, never newly True. Reads the
    # SAME float/rvol/change the scorer uses (ross_momentum._extract_pillars over
    # feats.meta["ross_signals"][symbol]). EQUITY-only (crypto 24h RVOL/change semantics
    # differ). Default-OFF / absent signal -> byte-identical. Each rejection is logged
    # with its reason so over-tightening is observable. Kill-switch
    # CHILI_MOMENTUM_A_SETUP_QUALITY_FLOOR_ENABLED.
    try:
        if (
            live_eligible
            and bool(getattr(settings, "chili_momentum_a_setup_quality_floor_enabled", False))
            and "-USD" not in str(symbol or "").upper()
        ):
            if any(
                value is not None
                for value in (
                    external.ross_rvol,
                    external.ross_change_pct,
                    external.ross_float_shares,
                )
            ):
                _rvol_a = external.ross_rvol
                _chg_a = external.ross_change_pct
                _float_a = external.ross_float_shares
                _ceil = float(
                    getattr(
                        settings,
                        "chili_momentum_a_setup_quality_floor_float_ceiling_shares",
                        20_000_000.0,
                    )
                    or 20_000_000.0
                )
                _rvol_min = float(
                    getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 3.0
                )
                _chg_min = float(
                    getattr(
                        settings,
                        "chili_momentum_a_setup_quality_floor_change_pct_min",
                        10.0,
                    )
                    or 10.0
                )
                _reason: str | None = None
                # (4) FLOAT-CONFIRMED — fail-CLOSED. Missing/None/0 float => cannot
                # confirm low-float => not an A-setup (catches CODI + empty signals).
                if _float_a is None or not (_float_a > 0):
                    _reason = "no-float"
                # (1) LOW FLOAT — the primary discriminator (AREC 107M fails).
                elif _ceil > 0 and _float_a > _ceil:
                    _reason = f"float {_float_a:,.0f} > ceiling {_ceil:,.0f}"
                # (2) RVOL. LEVER 1 win-win: distinguish AFFIRMATIVELY-LOW rvol (a real
                # non-mover -> reject) from a MERELY-MISSING rvol datum (None). The old
                # code failed CLOSED on None, blanket-blocking the day-monster (UPC live
                # 2026-06-29: float=563K low-float OK, change=+10% OK, but rvol=None ->
                # 1,520 rejects, $0 vs Ross +$35k). Align with the lane's own
                # below_explosive_floor, which FAILS OPEN on absent rvol (benches a name
                # only on data that AFFIRMATIVELY shows it is NOT explosive). When the
                # name is otherwise a genuine mover (float-confirmed low-float + change
                # already cleared above), a MISSING rvol no longer rejects — it is admitted
                # RISK-BOUNDED (sized DOWN, same as the extreme-vol path) so the missing
                # confirmation never costs more than a normal trade. An affirmatively-low
                # rvol (present and < floor) STILL rejects. Flag OFF => the prior
                # fail-CLOSED-on-None is byte-identical.
                elif _rvol_a is not None and _rvol_a < _rvol_min:
                    _reason = f"rvol {_rvol_a:g} < {_rvol_min:g}"
                elif _rvol_a is None and not (
                    _allow_extreme_explosive and _is_genuine_explosive
                ):
                    _reason = "rvol none"
                # (3) meaningful change/move (absolute — magnitude is what matters).
                elif _chg_a is None or abs(_chg_a) < _chg_min:
                    _reason = f"change {_chg_a if _chg_a is not None else 'none'} < {_chg_min:g}%"
                # MISSING-rvol genuine mover admitted -> risk-bounded sizing (size DOWN).
                if (
                    _reason is None
                    and _rvol_a is None
                    and _allow_extreme_explosive
                    and _is_genuine_explosive
                ):
                    _extreme_vol_risk_bounded = True
                    warnings.append(
                        "A-setup floor: rvol datum missing on a genuine mover — "
                        "LIVE with risk-bounded sizing"
                    )
                if _reason is not None:
                    live_eligible = False
                    warnings.append(f"Below A-setup quality floor ({_reason}) — not a live setup")
    except (TypeError, ValueError, AttributeError):
        pass

    base += external.symbol_family_memory_adjust

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
            _rqf = float(_ross_scores[symbol])
            base += external.ross_quality_viability_tilt * (_rqf - 0.5)
            if _rqf >= 0.8:
                warnings.append(f"High Ross momentum quality ({_rqf:.2f})")
            elif _rqf <= 0.2:
                warnings.append(f"Low Ross momentum quality ({_rqf:.2f}) — generic setup")
        elif (
            isinstance(_ross_scores, dict)
            and _ross_scores
            and bool(getattr(settings, "chili_momentum_no_signal_derank_enabled", False))
        ):
            # FIX 2 — EMPTY-SIGNAL DE-RANK. 40/50 live-eligible names carry EMPTY ross_signals
            # (GALT/PYXS/ANGI — no momentum/velocity data) yet sit eligible at base ~0.6 via the
            # fail-OPEN absent-signal no-op above; GALT was ENTERED over the real movers. When the
            # batch DID score SOME names (_ross_scores non-empty) but THIS symbol has NO ross_score
            # (a real-momentum signal was absent for it), DE-RANK it so ANY scored real mover
            # (base + tilt ~0.7+) outranks it for the slots. DE-RANK, not hard-exclude: the name
            # stays eligible and trades if nothing better is up. ONE documented adaptive setting —
            # the penalty is sized as a fraction of the SAME ROSS_QUALITY_VIABILITY_TILT magnitude
            # so it scales with the tilt and is not a scattered magic number; the default pushes an
            # empty-signal name clearly below a scored mover. A scored real mover (symbol IN
            # _ross_scores) takes the IF branch above and is NEVER touched by this penalty. OFF
            # (default) / no scored names ⇒ this branch is skipped ⇒ byte-identical.
            _derank_frac = float(
                getattr(settings, "chili_momentum_no_signal_derank_fraction", 1.0) or 1.0
            )
            _penalty = (
                external.ross_quality_viability_tilt
                * 0.5
                * max(0.0, _derank_frac)
            )
            base -= _penalty
            warnings.append("No Ross momentum signal — de-ranked below scored movers")
    except (TypeError, ValueError, AttributeError):
        pass

    # E5: news-catalyst tilt — a mover with a known earnings catalyst is more
    # likely a real Ross gapper than a random spike. Additive boost (never a
    # penalty); no-op when the catalyst set is absent or for crypto. (catalyst.py)
    try:
        _cat_delta = external.catalyst_delta
        if _cat_delta:
            base += _cat_delta
            _meta = ctx.meta if isinstance(getattr(ctx, "meta", None), dict) else {}
            warnings.append(
                "Hot tape — no-news speculation room (Ross-style)"
                if bool(_meta.get("hot_tape"))
                else "News catalyst (earnings) — Ross-style"
            )
    except (TypeError, ValueError, AttributeError):
        pass

    # E2: CATALYST GRADING + WEAK HARD GATE (Ross course study, build_order #3). The
    # existing catalyst tilt above boosts ANY catalyst; this GRADES the type. A WEAK
    # catalyst (dilution/compliance/legal — Ross's fade predictors) is SUPPRESSED: a
    # negative tilt AND dropped from LIVE eligibility (the hard gate — a diluting low-float
    # is not a live Ross long no matter how it ranks). A STRONG catalyst (FDA/trial/
    # partnership/contract/M&A/beat) is BOOSTED. MEDIUM / crypto / absent feed -> 0 (no
    # change). Flag OFF -> the whole block is skipped -> byte-identical (the weak set still
    # feeds the soft catalyst_viability_delta de-boost above, unchanged).
    # docs/STRATEGY/CC_REPORTS/2026-06-24_ross-course-study.md
    try:
        if bool(getattr(settings, "chili_momentum_catalyst_grade_gate_enabled", True)):
            _meta2 = ctx.meta if isinstance(getattr(ctx, "meta", None), dict) else {}
            _weak_g = _meta2.get("weak_catalyst_symbols")
            _strong_g = _meta2.get("strong_catalyst_symbols")
            _fake_g = _meta2.get("fake_catalyst_symbols")
            # Ross-batch2 QUCY-vs-ILLR: {ticker: action/dollar delta} refines the STRONG boost
            # (completed-action/big-dollar +, tentative/pursuit -). Absent/flag-OFF -> None ->
            # byte-identical strong boost.
            _action_g = _meta2.get("catalyst_action_deltas")
            if _weak_g or _strong_g or _fake_g:
                _grade_delta = external.catalyst_grade_delta
                if _grade_delta < 0:
                    base += _grade_delta
                    live_eligible = False
                    warnings.append(
                        "Weak catalyst (dilution/compliance/legal) — not a live Ross setup"
                    )
                elif _grade_delta > 0:
                    base += _grade_delta
                    warnings.append("Strong catalyst (FDA/M&A/contract) — Ross-style")
    except (TypeError, ValueError, AttributeError):
        pass

    # A10 (Ross CLRO-lesson 2026-07-02): OWN-HEADLINE DILUTION-HISTORY DERATE. A symbol our own
    # catalyst headlines have flagged as a diluter on >= adaptive-K distinct days in the trailing
    # window (persisted to momentum_dilution_history) is a WHLR-class serial diluter Ross has
    # "written off" — a DECAYING soft selection derate, NEVER a hard ban. THE FRESH REVERSE-SPLIT-
    # SQUEEZE CARVE-OUT MUST STILL WIN: a symbol in TODAY's strong-catalyst set (which folds in the
    # recent-reverse-split squeeze, pipeline.py) is EXEMPT — a live squeeze overrides the stale
    # memory. Runs AFTER the strong-catalyst boost above so a real setup always outranks the
    # memory. No history / read error / flag OFF -> 0.0 (byte-identical). Equity-only.
    try:
        if bool(getattr(settings, "chili_momentum_dilution_history_derate_enabled", True)):
            _meta_a10 = ctx.meta if isinstance(getattr(ctx, "meta", None), dict) else {}
            _strong_a10 = _meta_a10.get("strong_catalyst_symbols")
            _is_fresh_squeeze = bool(_strong_a10 and str(symbol or "").strip().upper() in set(_strong_a10))
            if not _is_fresh_squeeze:  # carve-out: a fresh squeeze / strong catalyst today wins
                _dil_derate = external.dilution_history_derate
                if _dil_derate > 0:
                    base -= _dil_derate
                    warnings.append(
                        "Serial-diluter history (own dilution headlines) — soft de-ranked (decaying)"
                    )
    except (TypeError, ValueError, AttributeError):
        pass

    # FAKE-CATALYST GUARD (Ross AS101/HVM101): a fresh headline that reads as UNVERIFIED /
    # hacked-PR / unsolicited-buyout / rumor / pump earns a SOFT credibility DOWN-WEIGHT (not a
    # hard veto — these names can still run, they just round-trip, so we de-prioritize rather
    # than block; conservative, low over-veto). Negative half-tilt, the boost side's magnitude.
    # Flag OFF / crypto / absent set -> 0 (byte-identical). Distinct from the WEAK/STRONG TYPE
    # grade above (this is credibility, not type). docs/STRATEGY/CC_REPORTS/...
    try:
        _meta3 = ctx.meta if isinstance(getattr(ctx, "meta", None), dict) else {}
        _fake_syms = _meta3.get("fake_catalyst_symbols")
        if _fake_syms:
            _fake_delta = external.fake_catalyst_delta
            if _fake_delta:
                base += _fake_delta
                warnings.append(
                    "Fake/unverified catalyst (rumor/hacked-PR/unsolicited) — Ross distrust"
                )
    except (TypeError, ValueError, AttributeError):
        pass

    # Ross gap #4: sympathy/theme tilt — a SYMPATHY peer of a hot sector cluster (same SIC
    # sector as a strong leader) gets an additive boost (the "hot potato" sympathy run
    # Ross trades). Additive, never penalizes; no-op when the set is absent / for crypto.
    try:
        _symp = (
            ctx.meta.get("sympathy_symbols")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if _symp:
            _symp_delta = external.sympathy_delta
            if _symp_delta:
                base += _symp_delta
                warnings.append("Sector sympathy peer (Ross hot-potato) — Ross-style")
    except (TypeError, ValueError, AttributeError):
        pass

    # E7: THEME / SYMPATHY tilt (the 1000%-mover lever). Complements the SIC-sector
    # sympathy above with a SHARED-CATALYST-KEYWORD axis: a name whose fresh headline
    # shares a salient keyword with a hot LEADER (STI -> ASTC) gets a SMALL additive
    # boost (it runs in sympathy). Soft, additive, never a penalty; equity-only; no-op
    # when the set is absent / for crypto. Flag OFF -> the block is skipped -> the
    # theme_sympathy_symbols key is never written either (pipeline) -> byte-identical.
    try:
        if bool(getattr(settings, "chili_momentum_theme_sympathy_enabled", True)):
            _theme_symp = (
                ctx.meta.get("theme_sympathy_symbols")
                if isinstance(getattr(ctx, "meta", None), dict)
                else None
            )
            if _theme_symp:
                _ts_delta = external.theme_sympathy_delta
                if _ts_delta:
                    base += _ts_delta
                    warnings.append("Theme sympathy peer (shared-catalyst leader) — Ross-style")
    except (TypeError, ValueError, AttributeError):
        pass

    # Ross gap #6: market-wide leading-gainer boost — the day's top-N % gainers get the
    # eyes/hot-lists that make patterns resolve. Small additive tilt (orders WITHIN the
    # eligible set; #3 gates membership). Equity-only; no-op when the set is absent.
    try:
        _topg = (
            ctx.meta.get("top_market_gainers")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if _topg and "-USD" not in str(symbol or "").upper() and str(symbol or "").upper() in _topg:
            base += 0.03  # small confirming boost (vs Ross's 0.20 selection / 0.10 catalyst)
            warnings.append("Top market gainer (broker hot-list) — Ross-style")
    except (TypeError, ValueError, AttributeError):
        pass

    # Ross gap #16: dilution-risk PENALTY — a recent S-1/424B* offering means the low-float
    # will issue shares and fade despite good news (CTNT vs SNTI). Subtract a catalyst-scale
    # penalty (offsets a news boost). Not a hard veto; equity-only; no-op when absent.
    try:
        _dil = (
            ctx.meta.get("dilution_symbols")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if _dil and "-USD" not in str(symbol or "").upper() and str(symbol or "").upper() in _dil:
            if not _extreme_mover:
                base -= 0.10
                warnings.append("Recent dilution filing (S-1/424B*) — fade risk")
            else:
                # Extreme Ross-quality mover with a dilution filing: Ross TRADES these
                # intraday — the dilution fade is an OVERNIGHT/swing risk, not an entry veto.
                # Keep the boost (no -0.10) so it arms; flag it for the EXIT/no-overnight guard.
                warnings.append("Dilution filing present — Ross-style intraday entry (no overnight; tighten exit)")
    except (TypeError, ValueError, AttributeError):
        pass

    # Re-analysis survivor S1: cross-day close-strength prior — a strong power-hour close
    # predicts next-day gap-continuation, so warm the lane on it early. Additive tilt
    # centered at neutral (boost strong-close, slightly discount weak-close). Equity-only.
    try:
        _csp = (
            ctx.meta.get("close_strength_priors")
            if isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if _csp:
            _csp_delta = external.close_strength_delta
            if _csp_delta:
                base += _csp_delta
                if _csp_delta > 0:
                    warnings.append("Strong prior-day close (continuation prior) — Ross-style")
    except (TypeError, ValueError, AttributeError):
        pass

    # HVM101: thick-tape / distribution veto. A name printing HIGH relative volume with
    # ~NO net price progress (low |change%| per unit RVOL) is churning into supply
    # (distribution / rejection at a level), not a clean Ross break. Apply a SOFT,
    # adaptive (batch-percentile) discount — never a hard cut, low over-veto. Reads the
    # raw RVOL/change batch from feats.meta["ross_signals"] (carried through from_meta).
    # EQUITY-ONLY (crypto 24h RVOL/change semantics differ); absent signal / thin batch /
    # flag-off -> 0.0 -> BYTE-IDENTICAL. Kill-switch CHILI_MOMENTUM_THICK_TAPE_VETO_ENABLED.
    try:
        if (
            bool(getattr(settings, "chili_momentum_thick_tape_veto_enabled", True))
            and "-USD" not in str(symbol or "").upper()
        ):
            _rsig = (
                feats.meta.get("ross_signals")
                if isinstance(getattr(feats, "meta", None), dict)
                else None
            )
            if _rsig:
                _tt = external.thick_tape_delta
                if _tt < 0.0 and not _extreme_mover:
                    base += _tt
                    warnings.append("Thick tape — high volume, no net progress (distribution)")
    except (TypeError, ValueError, AttributeError):
        pass

    # SCAL101: non-monotonic (inverted-U) volume preference. Viability rewards RVOL
    # MONOTONICALLY (the Ross tilt above + microstructure boosts); the "most obvious"
    # name has the highest volume, but EXTREME volume is choppy/late/crowded. Apply a
    # mild PEAKED roll-off that bites ONLY the extreme upper RVOL tail (batch-percentile)
    # and grows quadratically toward the very top — softening the over-rewarded outlier
    # without ever inverting the existing signal (the body is untouched; the roll-off is
    # capped well under the per-percentile reward slope). EQUITY-ONLY; absent / flag-off
    # -> 0.0 -> BYTE-IDENTICAL. Kill-switch CHILI_MOMENTUM_NONMONOTONIC_VOLUME_ENABLED.
    try:
        if (
            bool(getattr(settings, "chili_momentum_nonmonotonic_volume_enabled", True))
            and "-USD" not in str(symbol or "").upper()
        ):
            _rsig2 = (
                feats.meta.get("ross_signals")
                if isinstance(getattr(feats, "meta", None), dict)
                else None
            )
            if _rsig2:
                _nm = external.nonmonotonic_volume_delta
                if _nm < 0.0:
                    base += _nm
                    warnings.append("Extreme RVOL tail — choppy/crowded (inverted-U roll-off)")
    except (TypeError, ValueError, AttributeError):
        pass

    # EXPLOSIVE-PREQUAL SCORE FLOOR (the UPC blocker fix). A +500% low-float mover scored
    # viability 0.55 — just BELOW the impulse_breakout entry bar (0.56,
    # strategy_params.py:35) — so the score arithmetic vetoed the exact name the lane
    # exists to trade while a generic 0.56 bar cleared. This is a bar-relative RAISE-ONLY
    # floor (never lowers) that lifts the score of a GENUINE Ross A-setup just OVER the
    # default bar. It is gated by a HARDENED signed A-setup conjunction so junk cannot ride
    # it: low-float (float-confirmed, <= ceiling) AND SIGNED up-change >= the change floor
    # (NOT abs — a low-float CRASHER with extreme rvol fails) AND rvol ok (present >= floor,
    # OR fail-OPEN only when the up-change already confirmed the mover). It also requires the
    # SAME _is_genuine_explosive conjunction the extreme-vol relax uses (tradable + spread-ok
    # + affirm-explosive + not-below-floor) and that the name is STILL live_eligible (never
    # lift one an upstream HARD gate already rejected). A floored name is coupled to
    # RISK-BOUNDED sizing (sized DOWN). EQUITY-only; crypto / flag-off / missing-change =>
    # no-op (byte-identical). RISK #1: the LIVE entry binding can be RAISED above B_ref
    # (midday-lull +0.05, run-R breaker, families that bind higher), so floor+margin clears
    # the bar in the DEFAULT / no-bump impulse_breakout regime, NOT unconditionally — which
    # is acceptable (Ross sits out the midday lull anyway). Kill-switch
    # CHILI_MOMENTUM_EXPLOSIVE_PREQUAL_FLOOR_ENABLED.
    _floored = False
    try:
        if (
            bool(getattr(settings, "chili_momentum_explosive_prequal_floor_enabled", True))
            and _is_genuine_explosive  # tradable + spread-ok + affirm-explosive + not below_explosive_floor
            and live_eligible  # never lift a name an upstream HARD gate already rejected
            and "-USD" not in str(symbol or "").upper()
        ):
            _lowfloat_ok = (
                _float_x is not None
                and _float_x > 0
                and (_float_ceil_x <= 0 or _float_x <= _float_ceil_x)
            )
            _up_ok = (_chg_x is not None and _chg_x >= _chg_floor_x)  # SIGNED up-change (NOT abs)
            _rvol_ok = (_rvol_x is not None and _rvol_x >= _rvol_floor_x) or (
                _rvol_x is None and _up_ok
            )
            _a_setup = _lowfloat_ok and _up_ok and _rvol_ok  # fail-CLOSED on missing change
            if _a_setup:
                B_ref = float(
                    getattr(settings, "chili_momentum_explosive_prequal_bar_ref", 0.56) or 0.56
                )
                m = float(
                    getattr(settings, "chili_momentum_explosive_prequal_margin", 0.02) or 0.02
                )
                _floor_v = min(0.95, B_ref + m)
                if base < _floor_v:
                    base = _floor_v  # raise-only; never lowers
                    _floored = True
    except (TypeError, ValueError, AttributeError):
        pass
    if _floored:
        _extreme_vol_risk_bounded = True  # couple to size-DOWN

    viability = max(0.0, min(1.0, base))

    rationale = (
        f"{family.label}: session={ctx.session_label} vol={ctx.vol_regime.value} "
        f"chop_exp={ctx.chop_expansion.value}; spread_bps={spread_bps} "
        f"fee_to_target={fee_ratio} drift={drift} imb={imb} tape_z={tape_z}"
    )

    _final_live_eligible = live_eligible and viability >= 0.42
    return ViabilityResult(
        symbol=symbol,
        family_id=family.family_id,
        family_version=family.version,
        viability=viability,
        paper_eligible=paper_eligible,
        live_eligible=_final_live_eligible,
        freshness_hint="mesh_tick",
        regime_fit=regime_fit,
        rationale=rationale,
        warnings=tuple(warnings),
        # LEVER 1: only meaningful when the name actually stays LIVE-eligible (a
        # blocked name needs no sizing signal). Keeps the field a clean "this LIVE
        # entry must be risk-bounded" marker for the live_runner size-down lever.
        extreme_vol_risk_bounded=bool(_extreme_vol_risk_bounded and _final_live_eligible),
    )


def score_viability(
    symbol: str,
    family: MomentumStrategyFamily,
    ctx: MomentumRegimeContext,
    feats: ExecutionReadinessFeatures,
    *,
    db: "Session | None" = None,
) -> ViabilityResult:
    """Backward-compatible live wrapper around the explicit pure core."""

    projection = ViabilitySettingsProjection.from_runtime(settings)
    external = _resolve_viability_external_inputs(
        symbol,
        family,
        ctx,
        feats,
        db=db,
        settings_projection=projection,
    )
    return score_viability_explicit(
        symbol,
        family,
        ctx,
        feats,
        settings=projection,
        external=external,
    )


def resolve_viability_external_inputs_for_capture(
    symbol: str,
    family: MomentumStrategyFamily,
    ctx: MomentumRegimeContext,
    feats: ExecutionReadinessFeatures,
    *,
    db: "Session | None",
    settings_projection: ViabilitySettingsProjection,
    leveraged_etf: bool,
    excluded_fund: bool,
    decision_as_of: "datetime",
) -> ViabilityExternalInputs:
    """Resolve the legacy helper/DB facts once so a capture can seal them.

    This is intentionally separate from :func:`score_viability_explicit`.
    PAPER/Replay consumers must use the sealed ``ViabilityExternalInputs`` and
    never call this resolver.  The production capture source may call it inside
    its read-only repeatable-read snapshot, then persist every returned scalar
    with the source evidence used by the hermetic scorer.
    """

    if type(settings_projection) is not ViabilitySettingsProjection:
        raise TypeError("settings_projection must be ViabilitySettingsProjection")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol is required")
    if type(family) is not MomentumStrategyFamily:
        raise TypeError("family must be MomentumStrategyFamily")
    if type(ctx) is not MomentumRegimeContext:
        raise TypeError("ctx must be MomentumRegimeContext")
    if type(feats) is not ExecutionReadinessFeatures:
        raise TypeError("feats must be ExecutionReadinessFeatures")
    if type(leveraged_etf) is not bool or type(excluded_fund) is not bool:
        raise TypeError("captured instrument classifications must be booleans")
    if not isinstance(decision_as_of, datetime):
        raise TypeError("decision_as_of must be a datetime")
    if decision_as_of.tzinfo is None or decision_as_of.utcoffset() is None:
        raise ValueError("decision_as_of must be timezone-aware")
    return _resolve_viability_external_inputs(
        symbol,
        family,
        ctx,
        feats,
        db=db,
        settings_projection=settings_projection,
        captured_leveraged_etf=leveraged_etf,
        captured_excluded_fund=excluded_fund,
        decision_as_of=decision_as_of,
    )
