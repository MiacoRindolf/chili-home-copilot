"""Cost-aware viability scoring per symbol × strategy family (neural path)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc

from ....config import settings
from .context import MomentumRegimeContext, VolatilityRegime
from .features import ExecutionReadinessFeatures
from .leveraged_etf import symbol_is_leveraged_etf
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
    if bool(getattr(settings, "chili_momentum_exclude_leveraged_etfs", True)) and symbol_is_leveraged_etf(symbol):
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

            _meta = ctx.meta if isinstance(getattr(ctx, "meta", None), dict) else {}
            _hot = bool(_meta.get("hot_tape"))
            _ctry = (_meta.get("symbol_countries") or {}).get(symbol)
            _theme_syms = _meta.get("theme_symbols")
            _weak_syms = _meta.get("weak_catalyst_symbols")
            _cat_delta = catalyst_viability_delta(
                symbol, _cat_syms, hot_tape=_hot, hq_country=_ctry,
                theme_symbols=set(_theme_syms) if _theme_syms else None,
                weak_symbols=set(_weak_syms) if _weak_syms else None,
            )
            if _cat_delta:
                base += _cat_delta
                warnings.append(
                    "Hot tape — no-news speculation room (Ross-style)"
                    if _hot else "News catalyst (earnings) — Ross-style"
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
            if _weak_g or _strong_g or _fake_g:
                from .catalyst import catalyst_grade_selection_delta

                _grade_delta = catalyst_grade_selection_delta(
                    symbol,
                    weak_symbols=set(_weak_g) if _weak_g else None,
                    strong_symbols=set(_strong_g) if _strong_g else None,
                    fake_symbols=set(_fake_g) if _fake_g else None,
                )
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
            from .catalyst import fake_catalyst_viability_delta

            _fake_delta = fake_catalyst_viability_delta(symbol, set(_fake_syms))
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
            from .catalyst import sympathy_viability_delta

            _symp_delta = sympathy_viability_delta(symbol, set(_symp))
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
                from .theme_detector import theme_sympathy_viability_delta

                _ts_delta = theme_sympathy_viability_delta(symbol, set(_theme_symp))
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
            from .catalyst import close_strength_viability_delta

            _csp_delta = close_strength_viability_delta(symbol, _csp)
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
                from .distribution_filters import thick_tape_discount

                _tt = thick_tape_discount(symbol, _rsig, atr_pct=getattr(ctx, "atr_pct", None))
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
                from .distribution_filters import nonmonotonic_volume_rolloff

                _nm = nonmonotonic_volume_rolloff(symbol, _rsig2)
                if _nm < 0.0:
                    base += _nm
                    warnings.append("Extreme RVOL tail — choppy/crowded (inverted-U roll-off)")
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
    )
