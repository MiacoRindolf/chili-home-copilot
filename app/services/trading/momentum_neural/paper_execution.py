"""Simulated paper fills: spread, slippage, fee estimates (Phase 7)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ....config import settings


@dataclass
class SyntheticQuote:
    mid: float
    bid: float
    ask: float
    source: str


def regime_atr_pct(regime_json: dict[str, Any]) -> float:
    """Resolve ATR as fraction of price from regime snapshot (top-level or nested meta)."""
    raw = regime_json.get("atr_pct")
    if raw is None and isinstance(regime_json.get("meta"), dict):
        raw = regime_json["meta"].get("atr_pct")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.015
    return max(0.004, min(v, 0.12))


def effective_stop_atr_pct(
    regime_atr_pct_val: float,
    expected_move_bps: float | None,
    *,
    stop_atr_mult: float,
    vol_floor_mult: float = 0.5,
) -> float:
    """Floor the stop's ATR-pct so the stop sits OUTSIDE the live intraday noise.

    The regime ATR is a slow measure; on a coin whose LIVE 15m volatility
    (``expected_move_bps``, the same one the adaptive spread gate uses) is much
    larger, a regime-sized stop lands INSIDE the noise and gets shaken out
    (KAIO: 72bps stop vs 400bps move -> stopped, then it ran to target). Floor the
    stop distance at ``vol_floor_mult x expected_move``; risk-first sizing then
    trims qty to keep $risk constant (wider stop -> smaller size). No new
    volatility magic — reuses the live expected_move. docs/DESIGN/MOMENTUM_LANE.md
    """
    base = max(0.004, float(regime_atr_pct_val or 0.0))
    try:
        em_pct = float(expected_move_bps or 0.0) / 10_000.0
        mult = float(stop_atr_mult or 0.0)
        floor = float(vol_floor_mult or 0.0)
    except (TypeError, ValueError):
        return base
    if em_pct <= 0 or mult <= 0 or floor <= 0:
        return base
    # stop_distance = entry x atr_pct x stop_atr_mult; we want it >= floor x em_pct.
    floor_atr_pct = (floor * em_pct) / mult
    return max(0.004, min(max(base, floor_atr_pct), 0.15))


def structural_or_vol_floored_atr_pct(
    *,
    vol_floored_atr_pct: float,
    structural_stop_price: float | None,
    entry_price: float,
    stop_atr_mult: float,
) -> tuple[float, str]:
    """Ross structural stop vs the vol floor — take whichever sits FURTHER from entry.

    The pullback-break entry yields a structural stop: the pullback low. Ross stops
    just under that structure, giving the trade room to breathe WITHIN the pattern
    instead of at a noise-tight ATR (the lane's all-stop-out streak: every trade
    flagged ``stop_too_tight`` then ran 3-13%). But a very shallow pullback can put
    that level inside intraday noise and re-create the shake-out — so never go
    TIGHTER than the vol floor. Returns the effective stop ATR-pct (so the existing
    risk-first sizing + 2:1-target machinery is reused unchanged) and the model tag.
    Same 0.15 sanity cap as the vol floor. (docs/DESIGN/MOMENTUM_LANE.md)
    """
    eff = float(vol_floored_atr_pct)
    model = "vol_floored_atr"
    try:
        sp = float(structural_stop_price) if structural_stop_price is not None else 0.0
        ep = float(entry_price)
        mult = float(stop_atr_mult)
    except (TypeError, ValueError):
        return eff, model
    if sp > 0.0 and ep > 0.0 and sp < ep and mult > 0.0:
        struct_atr_pct = (ep - sp) / ep / mult
        struct_atr_pct = min(struct_atr_pct, 0.15)  # same sanity cap as the vol floor
        if struct_atr_pct > eff:
            eff = struct_atr_pct
            model = "structural_pullback"
    return eff, model


def default_reference_mid(
    *,
    viability_score: float,
    symbol: str,
    quote_mid: Optional[float],
) -> float:
    if quote_mid is not None and quote_mid > 0:
        return float(quote_mid)
    # Deterministic stub for tests / offline (not a price oracle).
    h = abs(hash(symbol)) % 1000
    return 50.0 + float(h) / 10.0 + float(viability_score)


def build_synthetic_quote(
    mid: float,
    spread_bps: float,
    *,
    source: str = "synthetic",
) -> SyntheticQuote:
    half = mid * (float(spread_bps) / 2.0) / 10_000.0
    return SyntheticQuote(mid=mid, bid=mid - half, ask=mid + half, source=source)


def long_entry_fill_price(ask: float, mid: float, slippage_bps: float) -> float:
    slip = mid * float(slippage_bps) / 10_000.0
    return ask + slip


def long_exit_fill_price(bid: float, mid: float, slippage_bps: float) -> float:
    slip = mid * float(slippage_bps) / 10_000.0
    return max(1e-12, bid - slip)


def roundtrip_fee_usd(
    notional: float,
    fee_to_target_ratio: float,
    *,
    entry: float = 0.0,
    target: float = 0.0,
    venue_rt_bps: float | None = None,
) -> float:
    """Estimate round-trip fees for a paper trade.

    ``venue_rt_bps`` is the VENUE-TRUTH path: the broker's actual round-trip
    commission in bps of notional (e.g. Coinbase taker 153 bps). When given it
    overrides the ratio model entirely — the 2026-06-13 crypto forensics found
    the ratio model booked ~1/7th of real Coinbase fees, hiding that every
    crypto round trip started ~1.5 % underwater.

    ``fee_to_target_ratio`` is the legacy fraction of *expected target profit*
    consumed by fees (e.g. 0.08 = 8 % of target PnL), kept for venues without
    a measured commission schedule. When ``entry`` and ``target`` are supplied
    we compute fees from the target P&L; otherwise fall back to a conservative
    0.5 % per-side exchange rate.
    """
    if venue_rt_bps is not None and math.isfinite(float(venue_rt_bps)) and float(venue_rt_bps) >= 0.0:
        return abs(notional) * float(venue_rt_bps) / 10_000.0
    r = float(fee_to_target_ratio)
    if entry > 0 and target > 0 and entry != target:
        qty = abs(notional) / entry if entry else 0.0
        expected_target_pnl = abs(target - entry) * qty
        return max(0.0, expected_target_pnl * r)
    # Conservative per-side estimate when target unknown (tiered venues ~0.04–0.6%).
    return abs(notional) * 0.0025 * 2.0


def crypto_paper_roundtrip_bps() -> float:
    """Round-trip commission (bps) a crypto PAPER trade should be charged.

    Maker round-trip (post-only entries never pay taker) when the crypto lane
    is configured maker-only (A3); otherwise the taker round-trip. The soak
    must measure the cost structure the live lane will actually run with, so
    flipping maker-only flips the paper fee with it (parity by construction)."""
    if bool(getattr(settings, "chili_coinbase_maker_only_enabled", False)):
        return float(getattr(settings, "chili_coinbase_maker_fee_bps_round_trip", 80) or 80)
    return float(getattr(settings, "chili_coinbase_taker_fee_bps_round_trip", 120) or 120)


# Never pull the first-scale target CLOSER than this many R from entry — selling a partial
# at a sub-1R round number is the "sold a tiny gain" failure mode. One documented base.
_FIRST_SCALE_MIN_R = 1.0


def round_numbers_above(price: float) -> list[float]:
    """Ascending psych levels strictly ABOVE ``price`` where sellers stack — Ross scales
    into these (gap #2, videos 37/03/12/14/20/24/25). A MULTI-SCALE grid (decade, half-
    decade, dollar, half-dollar relative to the price's magnitude) so a $12 name gets
    $12.50 / $13 / $15, not just the far $20, and a $0.12 crypto gets $0.125 / $0.13.
    Clamped exponent so sub-cent and 5-digit names compute an aligned grid. Pure."""
    if price is None or price <= 0 or not math.isfinite(price):
        return []
    try:
        exp = max(-4, min(6, math.floor(math.log10(price))))
        step = 10.0 ** exp
        levels: set[float] = set()
        for s in (step, step * 0.5, step * 0.1, step * 0.05):
            if s <= 0:
                continue
            lvl = (math.floor(price / s) + 1) * s  # smallest multiple of s strictly above
            # strictly above with a relative margin so float noise (lvl == price at the
            # 17th digit) can't admit the entry price itself as a "level above".
            if math.isfinite(lvl) and lvl > price * (1.0 + 1e-9):
                levels.add(round(lvl, 10))
        return sorted(levels)
    except (ValueError, OverflowError):
        return []


def round_number_first_scale_target(
    entry: float, stop: float, rr_target: float, *, side_long: bool = True
) -> float:
    """First-scale target = the NEAREST round/half-dollar above entry that clears the 1R
    floor and sits BELOW the R:R target — else the R:R target unchanged (gap #2). Ross
    sells half into the round number where sellers stack rather than waiting for a far
    fixed R:R that may never print and trails back (the MEGA give-back). The 1R floor
    (``_FIRST_SCALE_MIN_R``) avoids selling a tiny gain; the < rr_target bound keeps this a
    no-op (byte-identical) whenever no qualifying level exists. Long-only; the RUNNER
    (balance) still trails up from the partial exactly as before — only the FIRST-scale
    level moves."""
    if not side_long:
        return rr_target
    try:
        risk = float(entry) - float(stop)
        if risk <= 0 or not math.isfinite(risk):
            return rr_target
        floor_px = float(entry) + _FIRST_SCALE_MIN_R * risk
        for rn in round_numbers_above(float(entry)):  # ascending -> nearest qualifying
            if rn >= floor_px and rn < float(rr_target):
                return rn
    except (TypeError, ValueError):
        pass
    return rr_target


def stop_target_prices(
    entry: float,
    *,
    atr_pct: float,
    side_long: bool = True,
    stop_atr_mult: float = 0.60,
    target_atr_mult: float = 0.90,  # legacy; superseded by reward_risk below
    reward_risk: float | None = None,
    realized_high: float | None = None,
) -> tuple[float, float]:
    """ATR-scaled STOP + a reward:risk-anchored TARGET (Ross-style, >= 2:1).

    The TARGET is derived from the ACTUAL stop distance x a reward:risk multiple
    (not an independent ATR mult), so R:R is explicit and at least the documented
    floor — fixing the old ~1.3-1.5:1 (target_atr 0.90 vs stop_atr 0.60) that sat
    below Ross's strict 2:1. ``reward_risk`` defaults to
    chili_momentum_risk_reward_risk_ratio (2.0) — the single documented, learnable
    R:R knob (Ross = floor, the learner can raise it). docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        rr = float(reward_risk) if reward_risk is not None else float(
            getattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0) or 2.0
        )
    except (TypeError, ValueError):
        rr = 2.0
    if not math.isfinite(rr) or rr <= 0:
        rr = 2.0
    if side_long:
        stop = entry * (1.0 - max(0.003, atr_pct * float(stop_atr_mult)))
        # DESIGN #3: lift the R:R toward the name's realized HOD room (in R), capped.
        # No-op (rr unchanged) when flag-off / no realized_high / no proven room.
        rr, _adaptive_meta = adaptive_first_target_reward_risk(
            base_reward_risk=rr,
            entry=entry,
            stop=stop,
            realized_high=realized_high,
            side_long=True,
        )
        rr_target = entry + rr * (entry - stop)  # reward = rr x risk(stop distance)
        # Gap #2: pull the FIRST-scale target in to the next round number above entry when
        # one sits between 1R and the R:R target — Ross sells half into the level where
        # sellers stack rather than waiting for a far fixed R:R that trails back. No-op
        # (rr_target) when no round number qualifies; the runner trails from the partial.
        target = round_number_first_scale_target(entry, stop, rr_target, side_long=True)
    else:
        stop = entry * (1.0 + max(0.003, atr_pct * float(stop_atr_mult)))
        target = entry - rr * (stop - entry)
    return stop, target


# ── Ross asymmetric exit (scale-out + breakeven + runner trail) ───────────────
# Shared by BOTH runners (paper_runner + live_runner) so backtest and live take
# the IDENTICAL structural decision (parity contract): sell ``scale_out_fraction``
# of the original size into the FIRST (2:1) target, move the balance stop to
# BREAKEVEN, then HOLD the runner and trail it up. Ross's edge is the asymmetry
# (avg winner ~4.4x avg loser) — a 2:1-then-flat exit caps the upside and forgoes
# the tail. The fraction is the ONE documented knob; breakeven (= entry) and the
# trail (chandelier off the frozen entry ATR) are DERIVED. docs/DESIGN/MOMENTUM_LANE.md


def _is_crypto_symbol(symbol: str | None) -> bool:
    return bool(symbol) and str(symbol).upper().endswith("-USD")


def class_aware_reward_risk(symbol: str | None = None) -> float:
    """Reward:risk multiple for a symbol's asset class (2026-06-13, A4).

    Equity uses the global ``chili_momentum_risk_reward_risk_ratio`` (2:1
    floor). Crypto's fatter-tail moves take a wider target via
    ``chili_momentum_crypto_reward_risk_ratio`` when set; left None it falls
    back to the global so equity is never affected. Ross's R:R is a FLOOR, so
    a misconfig below the equity floor is clamped up to it."""
    try:
        g = float(getattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0) or 2.0)
    except (TypeError, ValueError):
        g = 2.0
    if not math.isfinite(g) or g <= 0:
        g = 2.0
    if _is_crypto_symbol(symbol):
        ov = getattr(settings, "chili_momentum_crypto_reward_risk_ratio", None)
        if ov is not None:
            try:
                ovf = float(ov)
                if math.isfinite(ovf) and ovf > 0:
                    return max(g, ovf)  # crypto override, never below the equity floor
            except (TypeError, ValueError):
                pass
    return g


# ── DESIGN #3: ADAPTIVE PROFIT TARGET (realized-range-aware R:R) ──────────────
# A fixed 2:1 caps a +400% low-float monster at 2R when its realized intraday
# room (ATR-to-HOD) is 6-10R. Lift the first-target R:R toward the name's OWN
# realized headroom-in-R, bounded by ONE documented cap above the base floor.
# SELF-CORRECTING vs the vol-floored stop (literature: a wider/vol-floored stop
# implies a lower optimal take-profit): room_R = headroom / stop_distance, so a
# wider stop GROWS the denominator and SHRINKS room_R -> rr_eff falls with no
# extra term. The base R:R (class_aware_reward_risk) stays the FLOOR; we only
# ever RAISE the target, never below the floor. Pure; parity-testable.


def adaptive_first_target_reward_risk(
    *,
    base_reward_risk: float,
    entry: float,
    stop: float,
    realized_high: float | None,
    side_long: bool = True,
) -> tuple[float, dict[str, Any]]:
    """Effective first-target R:R = clamp(max(base, room_R_capture * room_R), base, rr_cap).

    ``room_R`` is the name's realized HEADROOM expressed in R-units:
    ``room_R = max(0, realized_high - entry) / (entry - stop)`` — the ACTUAL
    distance to the session high the name has already proven it can travel,
    divided by the placed stop distance (so it is R-normalized and self-scales
    against a wider vol-floored stop). ``realized_high`` is the recent session
    high (HOD proxy from the entry 15m frame); when it is None / <= entry (no
    proven room) the result is the base R:R (byte-identical). ``room_R_capture``
    (the ONE documented base, default 0.5) is the fraction of the realized room
    we aim the first target at — a partial of the proven travel, not the whole
    leg (the RUNNER captures the tail above). The cap
    (chili_momentum_adaptive_target_rr_cap) bounds run-away on a vertical
    blow-off. Long-only; shorts / bad inputs / flag-off -> base. Pure."""
    meta: dict[str, Any] = {"adaptive": False, "base_rr": float(base_reward_risk)}
    if not side_long or not adaptive_target_enabled():
        return float(base_reward_risk), meta
    try:
        e = float(entry)
        s = float(stop)
        base = float(base_reward_risk)
    except (TypeError, ValueError):
        return float(base_reward_risk), meta
    risk = e - s
    if not (math.isfinite(e) and math.isfinite(s) and math.isfinite(base)) or risk <= 0 or base <= 0:
        return base, meta
    rh = None
    try:
        if realized_high is not None and math.isfinite(float(realized_high)):
            rh = float(realized_high)
    except (TypeError, ValueError):
        rh = None
    if rh is None or rh <= e:
        return base, meta  # no proven headroom -> base (byte-identical)
    room_r = (rh - e) / risk
    try:
        cap = float(getattr(settings, "chili_momentum_adaptive_target_rr_cap", 6.0) or 6.0)
        capture = float(getattr(settings, "chili_momentum_adaptive_target_room_capture", 0.5) or 0.5)
    except (TypeError, ValueError):
        cap, capture = 6.0, 0.5
    if not (math.isfinite(cap) and cap > 0):
        cap = 6.0
    cap = max(base, cap)  # the cap can never sit below the documented floor
    if not (math.isfinite(capture) and capture > 0):
        capture = 0.5
    rr_eff = max(base, capture * room_r)
    rr_eff = max(base, min(rr_eff, cap))
    if not math.isfinite(rr_eff) or rr_eff <= 0:
        return base, meta
    meta = {
        "adaptive": rr_eff > base + 1e-9,
        "base_rr": base,
        "room_r": round(room_r, 4),
        "capture": capture,
        "rr_eff": round(rr_eff, 4),
        "rr_cap": cap,
        "realized_high": rh,
    }
    return float(rr_eff), meta


def adaptive_target_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_adaptive_target_enabled", True))


def scale_out_fraction(
    default: float = 0.5,
    symbol: str | None = None,
    *,
    vol_pctl: float | None = None,
) -> float:
    """Fraction of the ORIGINAL position sold into the first target.

    Ross "sell 1/2 into strength" (up to 0.75 on the micro-pullback). ONE
    documented knob (``chili_momentum_scale_out_fraction``); the breakeven move
    and runner trail are derived. Crypto takes a heavier first de-risk via
    ``chili_momentum_crypto_scale_out_fraction`` when set (A4); left None it
    falls back to the global so equity is never affected. Bounded to the open
    interval (0, 1) so a misconfig can never sell 0% (no de-risk) or 100% (no
    runner)."""
    try:
        v = float(getattr(settings, "chili_momentum_scale_out_fraction", default))
    except (TypeError, ValueError):
        v = default
    if _is_crypto_symbol(symbol):
        ov = getattr(settings, "chili_momentum_crypto_scale_out_fraction", None)
        if ov is not None:
            try:
                ovf = float(ov)
                if math.isfinite(ovf):
                    v = ovf
            except (TypeError, ValueError):
                pass
    if not math.isfinite(v):
        v = default
    # DESIGN #3: tilt the partial by the name's realized-vol percentile within the
    # batch — sell LESS into the first target when realized vol is HIGH (keep a
    # bigger runner for the tail) and MORE when vol compresses. Centered at the
    # median (0.5) so a typical-vol name is byte-identical; the tilt magnitude is
    # ONE documented base (chili_momentum_adaptive_scale_vol_tilt). Flag-gated.
    if vol_pctl is not None and adaptive_target_enabled():
        try:
            p = float(vol_pctl)
            tilt = float(getattr(settings, "chili_momentum_adaptive_scale_vol_tilt", 0.5) or 0.0)
            if math.isfinite(p) and math.isfinite(tilt) and tilt > 0.0:
                p = max(0.0, min(1.0, p))
                # p>0.5 (high vol) -> reduce v; p<0.5 (low vol) -> raise v.
                v = v * (1.0 - tilt * (p - 0.5) * 2.0)
        except (TypeError, ValueError):
            pass
    return max(0.05, min(0.95, v))


def breakeven_stop_after_partial(
    entry_price: float, current_stop: float, *, side_long: bool = True
) -> float:
    """Move the RUNNER's stop to breakeven (entry) after the first-target partial.

    Ross "I then adjust my stop to my entry price on the balance of my position."
    Ratchet only — never loosen a stop that already sits tighter than entry.
    Derived from entry; no knob. Pure for parity testing."""
    try:
        e = float(entry_price)
        s = float(current_stop)
    except (TypeError, ValueError):
        return current_stop
    if not (math.isfinite(e) and math.isfinite(s)):
        return current_stop
    return max(s, e) if side_long else min(s, e)


def _floor_to_increment(qty: float, increment: float | None) -> float:
    if increment and increment > 0:
        return math.floor(qty / increment) * increment
    return qty


def scale_out_quantity(
    *,
    current_qty: float,
    original_qty: float,
    fraction: float,
    base_increment: float | None = None,
    base_min_size: float | None = None,
) -> tuple[float, float, bool]:
    """Split a held position for the Ross first-target scale-out.

    Returns ``(scale_qty, remainder_qty, can_split)``. ``scale_qty`` is ``fraction``
    of the ORIGINAL position (so re-evaluating a later tick can never double-count),
    floored to the venue base increment and clamped to what is still held.
    ``can_split`` is False when either leg would round to zero OR fall below the
    venue minimum sell size — the caller then flattens at target (the old flat
    behavior) so a tiny position is never stranded as un-sellable dust. Pure +
    side-effect-free for parity testing. (docs/DESIGN/MOMENTUM_LANE.md)"""
    try:
        cur = float(current_qty)
        orig = float(original_qty)
        frac = float(fraction)
    except (TypeError, ValueError):
        return 0.0, max(0.0, float(current_qty or 0.0)), False
    if not (math.isfinite(cur) and math.isfinite(orig) and math.isfinite(frac)):
        return 0.0, max(0.0, cur), False
    if cur <= 0.0 or orig <= 0.0 or frac <= 0.0 or frac >= 1.0:
        return 0.0, max(0.0, cur), False
    raw_scale = min(orig * frac, cur)
    scale_qty = _floor_to_increment(raw_scale, base_increment)
    if scale_qty <= 0.0:
        return 0.0, cur, False
    remainder = cur - scale_qty
    # Both legs must be independently sellable; otherwise don't split (flat exit).
    min_sz = float(base_min_size) if base_min_size else 0.0
    eps = max(min_sz, 1e-12)
    if scale_qty + 1e-12 < eps or remainder < eps:
        return 0.0, cur, False
    return float(scale_qty), float(remainder), True


def _parse_csv_floats(raw: str | None) -> list[float]:
    """Parse a comma-separated float list (the scale-grid knobs). Pure; skips junk."""
    out: list[float] = []
    if not raw:
        return out
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = float(tok)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def scale_grid_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_scale_grid_enabled", False))


# The cumulative scale-out fraction must stay strictly below 1.0 so a RUNNER always
# remains (the asymmetric Ross tail). One documented invariant constant.
_GRID_MAX_CUMULATIVE = 0.9


def scale_grid_levels(
    entry: float,
    stop: float,
    *,
    side_long: bool = True,
    symbol: str | None = None,
    rr_target: float | None = None,
) -> list[tuple[float, float]]:
    """Build the MULTI-LEVEL scale-out ladder: ``[(target_price, fraction), ...]``.

    The LEVELS are R-multiples (reward = R x stop-distance) off the configured
    ``chili_momentum_scale_grid_r_multiples``; where a round/half-dollar level above
    entry sits BELOW the next R level, that tranche's target is pulled IN to the round
    number (Ross sells into where sellers stack — reuses ``round_numbers_above`` so
    there is ONE round-number grid). The FRACTIONS are the ONE documented base
    (``chili_momentum_scale_grid_fractions``), each a fraction of the ORIGINAL position.

    INVARIANTS (so the caller can never oversell or strand 0 shares):
      * fractions are paired POSITIONALLY with R-multiples; extra of either is dropped;
      * the cumulative fraction SUM is clamped to < 1.0 (``_GRID_MAX_CUMULATIVE``) so a
        RUNNER always remains — the last tranche is trimmed if the configured sum would
        reach/exceed 1.0;
      * targets are strictly ascending and strictly above entry (a degenerate config
        collapses to an empty ladder -> the caller falls back to the single scale-out).

    Long-only (the lane is long-only); returns ``[]`` for shorts / bad inputs /
    flag-off so the caller is byte-identical. Pure + side-effect-free for parity
    testing. (docs/DESIGN/MOMENTUM_LANE.md)"""
    if not side_long or not scale_grid_enabled():
        return []
    try:
        e = float(entry)
        s = float(stop)
    except (TypeError, ValueError):
        return []
    risk = e - s
    if not (math.isfinite(e) and math.isfinite(s)) or risk <= 0 or e <= 0:
        return []
    r_mults = _parse_csv_floats(getattr(settings, "chili_momentum_scale_grid_r_multiples", "1.0,2.0"))
    fracs = _parse_csv_floats(getattr(settings, "chili_momentum_scale_grid_fractions", "0.5,0.25"))
    # Pair positionally; drop any non-positive / non-finite leg.
    pairs: list[tuple[float, float]] = []
    for rm, fr in zip(r_mults, fracs):
        if rm > 0 and fr > 0 and math.isfinite(rm) and math.isfinite(fr):
            pairs.append((rm, fr))
    if not pairs:
        return []
    rns = round_numbers_above(e)  # ascending psych levels above entry (Ross)
    levels: list[tuple[float, float]] = []
    cum = 0.0
    prev_px = e
    for rm, fr in pairs:
        r_px = e + rm * risk
        # Pull this tranche IN to a round number that sits at/above the prior level and
        # at/below this R target (Ross sells into the level where sellers stack).
        tgt = r_px
        for rn in rns:
            if rn > prev_px * (1.0 + 1e-9) and rn <= r_px * (1.0 + 1e-9):
                tgt = rn  # nearest qualifying round number (ascending -> first wins is lowest)
                break
        if tgt <= prev_px * (1.0 + 1e-9):  # not strictly ascending -> skip this rung
            continue
        # Clamp the cumulative fraction so a runner always remains (< _GRID_MAX_CUMULATIVE).
        room = _GRID_MAX_CUMULATIVE - cum
        if room <= 1e-9:
            break  # no fraction left without eating the runner
        take = min(fr, room)
        if take <= 1e-9:
            continue
        levels.append((float(tgt), float(take)))
        cum += take
        prev_px = tgt
    return levels


def iceberg_seller_score(ask_series: list[tuple[float, float]] | None) -> float | None:
    """Hidden-seller / iceberg detector for the per-add probe (pure, no I/O, unit-testable).

    Ross SS101 #038: at a level, if the DISPLAYED ask DISAPPEARS when it is hit (the offer
    lifts and price advances with no refill) there is NO hidden seller => OK to add. If the
    displayed ask REFILLS / PERSISTS at the same price after being eaten, an iceberg /
    absorbing seller is soaking the buying => STOP adding.

    ``ask_series`` is the short-window top-of-book ASK time series as ``(ask_px, ask_size)``
    tuples in ascending time (e.g. ``iqfeed_depth_snapshots`` best-ask price+size). Returns a
    refill-vs-advance ratio in [0, +inf): HIGH => hidden supply replenishing the same offer
    (iceberg); ~0 => the offer lifts cleanly as price advances (no hidden seller). Mirrors the
    crypto ``fast_path._hidden_seller`` shape so equity + crypto share ONE definition.

    Fail-OPEN: ``None``/<2 samples/unusable basis => ``None`` (the caller then ALLOWS the add —
    never blocks on absent or stale L2). This NEVER touches the initial entry — add-path only.
    """
    if not ask_series or len(ask_series) < 2:
        return None
    refill = 0.0
    price_adv_bps = 0.0
    usable = 0
    for prev, cur in zip(ask_series, ask_series[1:]):
        try:
            p_px, p_sz = float(prev[0]), float(prev[1])
            c_px, c_sz = float(cur[0]), float(cur[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (p_px > 0 and c_px > 0):
            continue
        usable += 1
        # Same touch, MORE size than before => the seller replenished the offer (absorption).
        if c_px == p_px and c_sz > p_sz:
            refill += c_sz - p_sz
        # Touch lifted up => price advanced through the offer (the offer disappeared, no refill).
        elif c_px > p_px:
            price_adv_bps += (c_px - p_px) / p_px * 1e4
    if usable < 1:
        return None
    return refill / (price_adv_bps + 1.0)


def discrete_pullback_add_trigger(
    closes: list[float] | None,
    *,
    ema: list[float] | None = None,
    vwap: float | None = None,
    live_price: float | None = None,
    ema_band: float = 0.003,
) -> tuple[bool, dict[str, Any]]:
    """Fresh DISCRETE higher-low bounce off the rising EMA/VWAP (GAP 3 add trigger; pure).

    Returns ``(fired, debug)``. ``fired=True`` ONLY when a distinct pullback-and-bounce
    sub-pattern is present in the recent bar window — i.e. the lane EARNED a NEW entry-shaped
    setup, not merely continuous green:

      (1) a recent DIP toward/just under the (rising) EMA — a local pullback low in the window
          sat within ``ema_band`` of the EMA (or below it), AND
      (2) a HIGHER LOW — the pullback low is ABOVE the prior pullback low (the dip held), AND
      (3) a BOUNCE — the current price has turned back UP off that low and is back at/above the
          EMA (close >= ema*(1-ema_band)), AND
      (4) the EMA is RISING over the window (trend intact), AND (when ``vwap`` given) price is
          at/above VWAP (Ross never adds below VWAP).

    The live tick (when ABOVE the last close) is used as the current price so a fast turn is
    seen sub-bar; it can only make the check STRICTER, never manufacture a bounce. PROTECTIVE /
    FAIL-CLOSED: a thin/degenerate window, a flat/falling EMA, a broken structure, or any error
    => ``(False, ...)`` so the add is BLOCKED (the GAP-3 guard then turns a would-fire into a
    no-fire — the safe direction). This is a SIZE-DOWN/STRICTER-ADD gate; a False can never
    increase risk. Pure; no I/O or mutation. docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"reason": "discrete_wait"}
    try:
        if not closes or len(closes) < 6:
            debug["reason"] = "insufficient_bars"
            return False, debug
        cl = [float(c) for c in closes if c is not None and math.isfinite(float(c))]
        if len(cl) < 6:
            debug["reason"] = "insufficient_bars"
            return False, debug
        cur = cl[-1]
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur = max(cl[-1], lp)
            except (TypeError, ValueError):
                pass
        if cur <= 0:
            debug["reason"] = "no_price"
            return False, debug
        # Rising-EMA basis: use the supplied EMA when present, else a short SMA proxy.
        if ema and len(ema) >= len(cl):
            ema_now = float(ema[-1])
            ema_prev = float(ema[max(0, len(ema) - 5)])
        else:
            ema_now = sum(cl[-3:]) / 3.0
            ema_prev = sum(cl[-6:-3]) / 3.0 if len(cl) >= 6 else ema_now
        if not (ema_now > 0 and math.isfinite(ema_now)):
            debug["reason"] = "no_ema"
            return False, debug
        if ema_now <= ema_prev * (1.0 + 1e-9):  # EMA must be RISING (trend intact)
            debug["reason"] = "ema_not_rising"
            return False, debug
        # Two most-recent local lows in the window (prior vs latest pullback low).
        window = cl[-6:]
        mid = len(window) // 2
        prior_low = min(window[:mid]) if window[:mid] else min(window)
        latest_low = min(window[mid:]) if window[mid:] else min(window)
        if latest_low <= prior_low * (1.0 + 1e-9):  # HIGHER LOW required (the dip held)
            debug["reason"] = "not_higher_low"
            return False, debug
        # (1) the latest pullback dipped to/under the EMA (a real pullback, not a runaway).
        dipped = latest_low <= ema_now * (1.0 + float(ema_band))
        # (3) the bounce: current price turned back UP to/above the EMA hold band.
        bounced = cur >= ema_now * (1.0 - float(ema_band)) and cur > latest_low * (1.0 + 1e-9)
        if not (dipped and bounced):
            debug["reason"] = "no_dip_or_bounce"
            return False, debug
        # (4) Ross never adds below VWAP.
        if vwap is not None:
            try:
                vw = float(vwap)
                if vw > 0 and cur < vw * (1.0 - 1e-9):
                    debug["reason"] = "below_vwap"
                    return False, debug
            except (TypeError, ValueError):
                pass
        debug.update({
            "reason": "discrete_bounce",
            "ema_now": round(ema_now, 6),
            "prior_low": round(prior_low, 6),
            "latest_low": round(latest_low, 6),
            "cur": round(cur, 6),
        })
        return True, debug
    except Exception:
        return False, {"reason": "error_fail_closed"}


def pyramid_add_decision(
    *,
    enabled: bool,
    is_equity: bool,
    add_count: int,
    max_adds: int,
    in_flight: bool,
    a0: float,
    q0: float,
    d0: float | None,
    bid: float,
    stop_px: float,
    entry_stop_ref: float | None,
    high_water_mark: float | None,
    ofi: float | None,
    ofi_threshold: float,
    min_cushion_r: float,
    midday_lull: bool,
    iceberg_score: float | None = None,
    iceberg_threshold: float | None = None,
    discrete_entry_trigger_fired: bool | None = None,
) -> dict[str, Any]:
    """Pure gate for the risk-neutral confirmation-pyramid ADD (no I/O, unit-testable).

    Decides whether to FIRE a single add to an already-winning runner. Returns a dict:
    ``{"fire": bool, "reason": str, "R0": float|None, "cushion_r": float|None,
       "cushion_usd": float|None}``. The caller (live_runner / replay) then sizes via
    ``compute_risk_first_quantity`` (never a hardcoded block), routes GUARD #4 admission,
    and submits — this helper owns ONLY the cushion+confirmation predicate so the live
    path and the replay A/B share ONE source of truth.

    R0 = d0 * q0 is the STARTER's original structural risk. The add fires iff ALL hold:
      * flag ON, EQUITY (crypto deferred — partial L2/OFI), under the max-adds cap, and
        no add currently in flight (idempotency);
      * GUARD #2 cushion BANKED: (bid - a0)*q0 >= min_cushion_r * R0 AND stop_px >= a0
        (the starter stop already ratcheted to >= breakeven);
      * CONFIRMATION (all AND): new-HOD proxy (bid >= high_water_mark), OFI thrust
        (ofi >= ofi_threshold), and non-decreasing trail headroom (stop_px ratcheted up
        since the add was first considered, entry_stop_ref);
      * NOT inside the equity midday lull (anti-Ross midday);
      * NO hidden seller at the level (the iceberg probe): when ``iceberg_score`` and
        ``iceberg_threshold`` are BOTH supplied, an iceberg_score >= threshold means the
        displayed ask is REFILLING (absorbing seller) => STOP adding (Ross SS101 #038).
    Fail-CLOSED: a missing/zero R0, a None OFI, or any unusable basis => no fire.
    Fail-OPEN (iceberg only): ``iceberg_score=None`` or ``iceberg_threshold=None`` (flag off
    or absent/stale L2) => the probe is INERT and the add proceeds — byte-identical to before.
    docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {"fire": False, "reason": "", "R0": None, "cushion_r": None, "cushion_usd": None}
    if not enabled:
        out["reason"] = "flag_off"
        return out
    if not is_equity:
        out["reason"] = "crypto_deferred"
        return out
    if in_flight:
        out["reason"] = "add_in_flight"
        return out
    if int(add_count) >= int(max_adds):
        out["reason"] = "max_adds_reached"
        return out
    try:
        _d0 = float(d0) if d0 is not None else 0.0
        _q0 = float(q0)
        _a0 = float(a0)
        _bid = float(bid)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (_d0 > 0 and math.isfinite(_d0) and _q0 > 0 and math.isfinite(_q0) and _a0 > 0):
        out["reason"] = "bad_basis"
        return out
    R0 = _d0 * _q0
    out["R0"] = R0
    cushion_usd = (_bid - _a0) * _q0
    out["cushion_usd"] = cushion_usd
    out["cushion_r"] = (cushion_usd / R0) if R0 > 0 else None
    # GUARD #2 — cushion banked + starter stop at/above breakeven.
    if not (cushion_usd >= float(min_cushion_r) * R0 and float(stop_px) >= _a0):
        out["reason"] = "cushion_not_banked"
        return out
    # CONFIRMATION — new-HOD AND OFI thrust AND non-decreasing trail headroom.
    if high_water_mark is None or _bid < float(high_water_mark) - 1e-9:
        out["reason"] = "not_new_hod"
        return out
    if ofi is None or float(ofi) < float(ofi_threshold):
        out["reason"] = "ofi_below_threshold"
        return out
    _ref = float(entry_stop_ref) if entry_stop_ref is not None else float(stop_px)
    if float(stop_px) < _ref - 1e-9:
        out["reason"] = "trail_not_ratcheted"
        return out
    if midday_lull:
        out["reason"] = "midday_lull"
        return out
    # ICEBERG / HIDDEN-SELLER probe (Ross SS101 #038) — add-path ONLY, fail-OPEN.
    # Both inputs present (flag on AND fresh L2) is the ONLY condition under which the
    # probe can block; a refilling displayed ask (score >= threshold) means an absorbing
    # seller is soaking the buying at the level => do not add into it. A None on either
    # input (flag off, absent/stale L2) leaves the add UNCHANGED.
    out["iceberg_score"] = iceberg_score
    if (
        iceberg_score is not None
        and iceberg_threshold is not None
        and float(iceberg_score) >= float(iceberg_threshold)
    ):
        out["reason"] = "iceberg_hidden_seller"
        return out
    # GAP 3 (HVM101) — require a FRESH DISCRETE entry sub-pattern for the add, not merely
    # CONTINUOUS green. ``discrete_entry_trigger_fired`` is the result of an entry-trigger
    # check at the add tick (a new higher-low bounce off the rising EMA/VWAP after a dip).
    # ADDITIVE / fail-OPEN: ``None`` (the flag is OFF, or the trigger could not be evaluated)
    # leaves the add UNCHANGED — byte-identical to before. Only an explicit ``False`` (flag
    # ON, evaluated, NO fresh discrete trigger) BLOCKS the add. It can NEVER fire an add the
    # existing cushion/HOD/OFI/iceberg guards blocked (those already returned above), so it
    # ONLY tightens — it cannot increase risk.
    out["discrete_trigger"] = discrete_entry_trigger_fired
    if discrete_entry_trigger_fired is False:
        out["reason"] = "discrete_entry_trigger_not_fired"
        return out
    out["fire"] = True
    out["reason"] = "confirmed"
    return out


def pyramid_blend_on_fill(
    *,
    q0: float,
    a0: float,
    qa_f: float,
    Pa_f: float,
    stop_px: float,
    original_quantity: float | None = None,
) -> dict[str, float]:
    """Pure blend math for a CONFIRMED pyramid add fill (no I/O, unit-testable).

    Given the held starter (q0, a0), the filled add (qa_f at Pa_f), and the freshly-
    ratcheted live stop (stop_px), returns the ENLARGED position:
      q1 = q0 + qa_f
      a1 = (a0*q0 + Pa_f*qa_f) / q1                 (blended VWAP)
      s1 = max(stop_px, a1)                          (INVARIANT-A: ratchet to blended
                                                      breakeven, tighten-ONLY)
      original_quantity grows by qa_f                (so the Ross scale-out de-risks the
                                                      ENLARGED position)
    Asserts s1 >= stop_px (the stop can only tighten). A partial add blends ONLY the
    filled qty. Pure + side-effect-free. docs/DESIGN/MOMENTUM_LANE.md"""
    _q0 = float(q0)
    _a0 = float(a0)
    _qa = float(qa_f)
    _Pa = float(Pa_f)
    q1 = _q0 + _qa
    a1 = (_a0 * _q0 + _Pa * _qa) / q1 if q1 > 0 else _a0
    s1 = max(float(stop_px), a1)
    assert s1 >= float(stop_px) - 1e-9, "INVARIANT-A violated: pyramid stop loosened"
    orig = (float(original_quantity) if original_quantity is not None else _q0) + _qa
    return {"q1": q1, "a1": a1, "s1": s1, "original_quantity": orig}


def pullback_add_decision(
    *,
    enabled: bool,
    is_equity: bool,
    add_count: int,
    max_adds: int,
    in_flight: bool,
    other_add_in_flight: bool,
    a0: float,
    q0: float,
    d0: float | None,
    bid: float,
    stop_px: float,
    high_water_mark: float | None,
    support_level: float | None,
    pullback_low: float | None,
    prior_pullback_low: float | None,
    move_range: float | None,
    pullback_depth_lo_frac: float,
    pullback_depth_hi_frac: float,
    bounced: bool,
    front_side_strength: float | None,
    strength_floor: float,
    above_vwap_or_reclaiming: bool,
    ofi_level: float | None,
    ofi_slope: float | None,
    midday_lull: bool,
    cooldown_active: bool,
) -> dict[str, Any]:
    """Pure gate for the Ross BUY-THE-DIP / pullback ADD (no I/O, unit-testable).

    The existing pyramid (``pyramid_add_decision``) adds on CONTINUATION — a new HOD +
    an OFI thrust (it pyramids UP on strength). Ross ALSO buys the PULLBACK: while a held
    winner's uptrend is INTACT, he re-loads on a controlled dip BACK to support (a higher-
    low / the breakout shelf / VWAP / a short MA) that then BOUNCES. This predicate owns
    that distinct trigger; the caller (live_runner / replay) sizes via
    ``compute_risk_first_quantity`` (the add's stop sits just below the pullback's higher-
    low), routes the SAME GUARD #4 admission, blends via ``pyramid_blend_on_fill`` (so the
    #769 max-loss circuit re-bases to the STARTER R0), and submits.

    Returns ``{"fire": bool, "reason": str, "R0": float|None, "cushion_r": float|None,
    "cushion_usd": float|None, "pullback_depth_frac": float|None, "add_stop": float|None}``.
    R0 = d0 * q0 is the STARTER's original structural risk; ``add_stop`` is the proposed
    add stop = just below the pullback's higher-low (``pullback_low``).

    The add fires iff ALL hold:
      * flag ON, EQUITY (crypto deferred — partial L2/OFI), under the max-adds cap, no add
        of EITHER kind (this one OR the UP-pyramid / micro-pullback) currently in flight
        (composes with the existing pyramid; never two adds on one tick), cooldown elapsed;
      * a SUPPORT zone is known and the price PULLED BACK toward it but is HOLDING ABOVE the
        structural stop (``pullback_low >= stop_px`` — never a collapse below the stop);
      * the pullback depth is CONTROLLED: the dip from the HWM is within the adaptive band
        [``pullback_depth_lo_frac``, ``pullback_depth_hi_frac``] of the move's range (a
        healthy pullback, not a 1-tick wiggle and not a deep rollover);
      * a HIGHER LOW held: ``pullback_low > prior_pullback_low`` (the dip made a higher low
        than the prior — NOT a lower-low breakdown);
      * a BOUNCE / hold / reclaim is in progress (``bounced`` — price turning back up off
        support: a green re-load tick / reclaim / firming bid; the caller computes it);
      * ⭐ FALLING-KNIFE GUARD (the E1/CTNT lesson): the uptrend is INTACT, measured by the
        JUST-shipped front-side strength — ``front_side_strength >= strength_floor`` (an
        adaptive floor; reuse the regime-adaptive p-thresholds) AND OFI is NOT collapsing
        (``ofi_level > 0`` and ``ofi_slope >= 0`` — the RIDE definition) AND price is above
        VWAP or cleanly reclaiming (``above_vwap_or_reclaiming``). If ANY fail ⇒ NO add
        (it's a knife, not a dip);
      * NOT inside the equity midday lull (anti-Ross midday, parity with the pyramid).

    FAIL-CLOSED — this is an EXTRA discretionary BUY, so it requires PROOF: a missing/zero
    R0, a ``front_side_strength`` of None (stale/absent strength ⇒ cannot prove the trend is
    intact ⇒ no add — the opposite of the entry-side tilt, which fails OPEN to full size),
    a None OFI, a missing support / higher-low / move-range, or any unusable basis ⇒ no
    fire. Bias toward NOT adding when uncertain (a missed add is fine; adding into a
    breakdown is not). This is an ADD lever — it can only ever ADD position on a healthy
    dip; it NEVER vetoes or touches an exit. docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {
        "fire": False, "reason": "", "R0": None, "cushion_r": None, "cushion_usd": None,
        "pullback_depth_frac": None, "add_stop": None,
    }
    if not enabled:
        out["reason"] = "flag_off"
        return out
    if not is_equity:
        out["reason"] = "crypto_deferred"
        return out
    if in_flight or other_add_in_flight:
        out["reason"] = "add_in_flight"
        return out
    if int(add_count) >= int(max_adds):
        out["reason"] = "max_adds_reached"
        return out
    if cooldown_active:
        out["reason"] = "cooldown"
        return out
    if midday_lull:
        out["reason"] = "midday_lull"
        return out
    try:
        _d0 = float(d0) if d0 is not None else 0.0
        _q0 = float(q0)
        _a0 = float(a0)
        _bid = float(bid)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (_d0 > 0 and math.isfinite(_d0) and _q0 > 0 and math.isfinite(_q0) and _a0 > 0):
        out["reason"] = "bad_basis"
        return out
    R0 = _d0 * _q0
    out["R0"] = R0
    cushion_usd = (_bid - _a0) * _q0
    out["cushion_usd"] = cushion_usd
    out["cushion_r"] = (cushion_usd / R0) if R0 > 0 else None
    # SUPPORT + structural-stop floor: a pullback that breaks the structural stop is a
    # COLLAPSE, never a buyable dip (a too-deep pullback is refused here, never sold).
    if support_level is None or pullback_low is None or prior_pullback_low is None:
        out["reason"] = "no_support_structure"
        return out
    try:
        _support = float(support_level)
        _pb_low = float(pullback_low)
        _prior_low = float(prior_pullback_low)
        _stop = float(stop_px)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    out["add_stop"] = _pb_low
    if not (_pb_low >= _stop - 1e-9):
        out["reason"] = "pullback_below_stop"      # too deep — below the structural stop
        return out
    # HIGHER LOW: the dip made a HIGHER low than the prior pullback (not a lower-low break).
    if not (_pb_low > _prior_low + 1e-9):
        out["reason"] = "not_higher_low"
        return out
    # CONTROLLED pullback DEPTH from the HWM, as a fraction of the move's range. Too shallow
    # (a 1-tick wiggle) and too deep (a rollover) are BOTH refused — the band is adaptive.
    if high_water_mark is None or move_range is None:
        out["reason"] = "no_move_range"
        return out
    try:
        _hwm = float(high_water_mark)
        _rng = float(move_range)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (_rng > 0 and math.isfinite(_rng) and _hwm > 0):
        out["reason"] = "no_move_range"
        return out
    depth_frac = (_hwm - _pb_low) / _rng
    out["pullback_depth_frac"] = depth_frac
    if depth_frac < float(pullback_depth_lo_frac) - 1e-12:
        out["reason"] = "pullback_too_shallow"
        return out
    if depth_frac > float(pullback_depth_hi_frac) + 1e-12:
        out["reason"] = "pullback_too_deep"
        return out
    # BOUNCE / hold / reclaim off support (the dip turned back up — the caller computes it).
    if not bool(bounced):
        out["reason"] = "no_bounce"
        return out
    # ⭐ FALLING-KNIFE GUARD #1 — front-side strength INTACT (FAIL-CLOSED on None: an extra
    # discretionary BUY into a dip needs PROOF the trend is alive; stale strength ⇒ no add).
    if front_side_strength is None:
        out["reason"] = "no_strength"
        return out
    try:
        _strength = float(front_side_strength)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    out["front_side_strength"] = _strength
    if not (math.isfinite(_strength) and _strength >= float(strength_floor) - 1e-12):
        out["reason"] = "weak_front_side"
        return out
    # ⭐ FALLING-KNIFE GUARD #2 — OFI NOT collapsing (the RIDE definition: level > 0 AND a
    # non-negative slope). FAIL-CLOSED on a None read (no proof the book is holding up).
    if ofi_level is None or ofi_slope is None:
        out["reason"] = "ofi_unknown"
        return out
    try:
        _ofi_lvl = float(ofi_level)
        _ofi_slp = float(ofi_slope)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (_ofi_lvl > 0.0 and _ofi_slp >= 0.0):
        out["reason"] = "ofi_collapsing"
        return out
    # ⭐ FALLING-KNIFE GUARD #3 — above VWAP or cleanly reclaiming (Ross never adds below
    # VWAP unless it is being reclaimed turning up — the E1-killed winner shape).
    if not bool(above_vwap_or_reclaiming):
        out["reason"] = "below_vwap"
        return out
    out["fire"] = True
    out["reason"] = "pullback_confirmed"
    return out


def flag_breakout_add_decision(
    *,
    enabled: bool,
    is_equity: bool,
    add_count: int,
    max_adds: int,
    in_flight: bool,
    other_add_in_flight: bool,
    a0: float,
    q0: float,
    d0: float | None,
    bid: float,
    stop_px: float,
    flag_confirmed: bool,
    flag_high: float | None,
    flag_low: float | None,
    prior_flag_high: float | None,
    breakout_margin_frac: float,
    front_side_strength: float | None,
    strength_floor: float,
    above_vwap_or_reclaiming: bool,
    ofi_level: float | None,
    ofi_slope: float | None,
    midday_lull: bool,
    cooldown_active: bool,
) -> dict[str, Any]:
    """Pure gate for the Ross ADD-ON-FLAG-BREAKOUT (no I/O, unit-testable).

    The THREE existing held-position adds each own a DISTINCT trigger geometry:
      * ``pyramid_add_decision`` — adds on a NEW HOD + an OFI thrust (pyramid UP on a fresh
        session high; continuation up the right side of the move).
      * the micro-pullback re-load — adds on a shallow dip-and-curl back to the moving avg.
      * ``pullback_add_decision`` (BUY-THE-DIP) — adds on a CONTROLLED pullback to support
        that BOUNCES (a higher-low to the shelf / VWAP), an INTACT-uptrend re-load.

    Ross ALSO adds a FOURTH, distinct way: while HOLDING a winner, the name consolidates
    into a tight BULL FLAG (a base after the impulse) and he buys the BREAK of the flag's
    swing high — a CONTINUATION add at the breakout. This is neither a new-HOD pyramid (the
    flag-break may be the FIRST new high after a base, not a fresh day high) nor a dip-buy
    bounce (the trigger is the BREAK of the flag top, not a bounce off support). This
    predicate owns that distinct trigger. The CALLER detects the flag geometry + the
    confirmed break (reuse ``bull_flag_confirmation`` on the held position's recent bars:
    ``flag_high`` = its ``pullback_high`` break level, ``flag_low`` = its ``pullback_low``
    stop, ``flag_confirmed`` = its ``ok``) and passes them in; the caller then sizes via
    ``compute_risk_first_quantity`` (the add's stop sits just below the flag low), routes
    the SAME GUARD #4 admission, blends via ``pyramid_blend_on_fill`` (so the #769 max-loss
    circuit re-bases to the STARTER R0), and submits.

    Returns ``{"fire": bool, "reason": str, "R0": float|None, "cushion_r": float|None,
    "cushion_usd": float|None, "breakout_frac": float|None, "add_stop": float|None}``.
    R0 = d0 * q0 is the STARTER's original structural risk; ``add_stop`` is the proposed
    add stop = just below the flag low (``flag_low``).

    The add fires iff ALL hold:
      * flag ON, EQUITY (crypto deferred — partial L2/OFI), under the max-adds cap, no add
        of ANY kind (this one OR the UP-pyramid / micro-pullback / dip-add) currently in
        flight (composes with the other 3; never two adds on one tick), cooldown elapsed;
      * a valid BULL FLAG was detected and CONFIRMED-BROKEN (``flag_confirmed`` — the caller
        ran the full ``bull_flag_confirmation`` ladder: tight consolidation, depth band,
        EMA-9 hold, light-pullback volume, anti-chase / backside / L2 vetoes, NOT-extended,
        and a genuine swing-high break on thrust with tape). A False ⇒ no real flag-break;
      * the break is GENUINE (not an already-extended chase): the live bid is ABOVE the flag
        high by at least ``breakout_margin_frac`` of the flag range (``flag_high - flag_low``)
        — a clean take-out, not a wick poking the level. (The caller's bull_flag NOT-PARABOLIC
        extension guard already rejects a vertical blow-off INTO the level; this confirms the
        break itself cleared the top by a real margin rather than a 1-tick touch);
      * the flag is a HIGHER base: ``flag_high > prior_flag_high`` (the new flag built ABOVE
        the prior flag/add level — each flag-add steps the structure UP, never re-adds at the
        same shelf), and the flag low HELD above the structural stop (``flag_low >= stop_px``);
      * ⭐ FALLING-KNIFE / QUALITY GUARD (the E1/CTNT lesson, identical discipline to the
        dip-add): the uptrend is INTACT — ``front_side_strength >= strength_floor`` (an
        adaptive floor; reuse the regime-adaptive p-thresholds) AND OFI is NOT collapsing
        (``ofi_level > 0`` and ``ofi_slope >= 0`` — the RIDE definition) AND price is above
        VWAP or cleanly reclaiming (``above_vwap_or_reclaiming``). If ANY fail ⇒ NO add (a
        sloppy break / breakdown dressed as a flag is a knife, not a continuation);
      * NOT inside the equity midday lull (anti-Ross midday, parity with the pyramid).

    FAIL-CLOSED — this is an EXTRA discretionary BUY, so it requires PROOF: a missing/zero
    R0, ``flag_confirmed`` False, a ``front_side_strength`` of None (stale/absent strength
    ⇒ cannot prove the trend is intact ⇒ no add — the opposite of the entry-side tilt, which
    fails OPEN to full size), a None OFI, a missing flag level / higher-base / range, or any
    unusable basis ⇒ no fire. Bias toward NOT adding when uncertain (a missed add is fine;
    adding into a fake-out is not). This is an ADD lever — it can only ever ADD position on a
    confirmed healthy flag-break; it NEVER vetoes or touches an exit.
    docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {
        "fire": False, "reason": "", "R0": None, "cushion_r": None, "cushion_usd": None,
        "breakout_frac": None, "add_stop": None,
    }
    if not enabled:
        out["reason"] = "flag_off"
        return out
    if not is_equity:
        out["reason"] = "crypto_deferred"
        return out
    if in_flight or other_add_in_flight:
        out["reason"] = "add_in_flight"
        return out
    if int(add_count) >= int(max_adds):
        out["reason"] = "max_adds_reached"
        return out
    if cooldown_active:
        out["reason"] = "cooldown"
        return out
    if midday_lull:
        out["reason"] = "midday_lull"
        return out
    try:
        _d0 = float(d0) if d0 is not None else 0.0
        _q0 = float(q0)
        _a0 = float(a0)
        _bid = float(bid)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (_d0 > 0 and math.isfinite(_d0) and _q0 > 0 and math.isfinite(_q0) and _a0 > 0):
        out["reason"] = "bad_basis"
        return out
    R0 = _d0 * _q0
    out["R0"] = R0
    cushion_usd = (_bid - _a0) * _q0
    out["cushion_usd"] = cushion_usd
    out["cushion_r"] = (cushion_usd / R0) if R0 > 0 else None
    # FLAG GEOMETRY: the caller must have detected + CONFIRMED a real bull-flag BREAK
    # (the full bull_flag_confirmation ladder). A False ⇒ no flag-break ⇒ never an add.
    if not bool(flag_confirmed):
        out["reason"] = "no_flag_break"
        return out
    if flag_high is None or flag_low is None or prior_flag_high is None:
        out["reason"] = "no_flag_structure"
        return out
    try:
        _fhi = float(flag_high)
        _flo = float(flag_low)
        _prior_hi = float(prior_flag_high)
        _stop = float(stop_px)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (0.0 < _flo < _fhi):
        out["reason"] = "bad_flag_levels"
        return out
    out["add_stop"] = _flo
    # The flag low must hold ABOVE the structural stop — a flag whose base undercut the stop
    # is a breakdown, never a buyable continuation (refused here, never sold).
    if not (_flo >= _stop - 1e-9):
        out["reason"] = "flag_below_stop"
        return out
    # HIGHER BASE: the new flag built ABOVE the prior flag/add level (steps the structure UP,
    # never re-adds at the same shelf — the analogue of the dip-add's higher-low).
    if not (_fhi > _prior_hi + 1e-9):
        out["reason"] = "not_higher_base"
        return out
    # GENUINE BREAK (not a 1-tick wick / not an extended chase): the live bid cleared the flag
    # high by >= breakout_margin_frac of the flag RANGE. Range-relative (no fixed-price magic);
    # the bull_flag NOT-PARABOLIC extension guard upstream already rejects a vertical blow-off
    # INTO the level, so this only confirms the take-out cleared the top by a real margin.
    _flag_range = _fhi - _flo
    if not (_flag_range > 0 and math.isfinite(_flag_range)):
        out["reason"] = "bad_flag_levels"
        return out
    breakout_frac = (_bid - _fhi) / _flag_range
    out["breakout_frac"] = breakout_frac
    if breakout_frac < float(breakout_margin_frac) - 1e-12:
        out["reason"] = "break_not_clear"      # wick / not yet a confirmed clear of the top
        return out
    # ⭐ FALLING-KNIFE GUARD #1 — front-side strength INTACT (FAIL-CLOSED on None: an extra
    # discretionary BUY into a break needs PROOF the trend is alive; stale strength ⇒ no add).
    if front_side_strength is None:
        out["reason"] = "no_strength"
        return out
    try:
        _strength = float(front_side_strength)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    out["front_side_strength"] = _strength
    if not (math.isfinite(_strength) and _strength >= float(strength_floor) - 1e-12):
        out["reason"] = "weak_front_side"
        return out
    # ⭐ FALLING-KNIFE GUARD #2 — OFI NOT collapsing (the RIDE definition: level > 0 AND a
    # non-negative slope). FAIL-CLOSED on a None read (no proof the book is holding up).
    if ofi_level is None or ofi_slope is None:
        out["reason"] = "ofi_unknown"
        return out
    try:
        _ofi_lvl = float(ofi_level)
        _ofi_slp = float(ofi_slope)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (_ofi_lvl > 0.0 and _ofi_slp >= 0.0):
        out["reason"] = "ofi_collapsing"
        return out
    # ⭐ FALLING-KNIFE GUARD #3 — above VWAP or cleanly reclaiming (Ross never adds below
    # VWAP unless it is being reclaimed turning up — the E1-killed winner shape).
    if not bool(above_vwap_or_reclaiming):
        out["reason"] = "below_vwap"
        return out
    out["fire"] = True
    out["reason"] = "flag_break_confirmed"
    return out


def runner_trail_stop(
    *,
    high_water_mark: float,
    atr_pct: float,
    stop_atr_mult: float,
    breakeven_floor: float,
    current_stop: float,
    side_long: bool = True,
) -> float:
    """Chandelier ATR trail for the held RUNNER — ratchets the stop up only.

    Ross holds the runner "for the next breakout level" and trails it up. Trail the
    stop the SAME ATR distance below the high-water mark that the initial stop sat
    below entry (``atr_pct x stop_atr_mult``) — fully derived from values frozen at
    entry, no new magic number. Never loosens (``max`` with the current stop) and
    never falls below ``breakeven_floor`` (the first-target partial already de-risked
    the runner). Pure for parity testing. docs/DESIGN/MOMENTUM_LANE.md"""
    try:
        hwm = float(high_water_mark)
        ap = float(atr_pct)
        mult = float(stop_atr_mult)
        be = float(breakeven_floor)
        cs = float(current_stop)
    except (TypeError, ValueError):
        return current_stop
    if not (math.isfinite(hwm) and math.isfinite(ap) and math.isfinite(mult) and math.isfinite(cs)):
        return current_stop
    dist = max(0.0, ap * mult)
    if side_long:
        chandelier = hwm * (1.0 - dist)
        floors = [c for c in (cs, be, chandelier) if math.isfinite(c)]
        return max(floors) if floors else cs
    chandelier = hwm * (1.0 + dist)
    return min(cs, chandelier)


# ---------------------------------------------------------------------------
# LEVER 2A — MATH-VERIFIED adaptive vol-normalized runner trail (CORE).
#
# The frozen-ATR trail (runner_trail_stop / cushion_adaptive_trail_stop) snapshots
# entry_stop_atr_pct AT ENTRY and never refreshes it, so a runner whose realized
# vol COLLAPSES after the breakout keeps an entry-sized (often too-wide) trail and
# bleeds back the move — exactly the ASTC/DCOY/LI/AMPX/TMC "breaks don't hold,
# -$17 total" leak. The vol-norm trail re-derives the trail WIDTH from LIVE realized
# vol scaled to the holding horizon, floored so the stop sits OUTSIDE the bid/ask
# bounce. Every output composes through INVARIANT-A (ratchet-only) at the call site —
# this core only ever produces a candidate WIDTH/STOP; it never loosens a live stop.
#
# All functions here are PURE (no DB, no clock) for replay/live parity + unit tests.
# docs/DESIGN/MOMENTUM_LANE.md
# ---------------------------------------------------------------------------


def denoised_rv_ewma(grid_log_returns: list[float], *, half_life: float) -> float | None:
    """Denoised realized-vol (per-grid-step stdev) from EVENT-GRID log returns via
    an EWMA of squared returns. ``grid_log_returns`` are r_i = ln(p_i / p_{i-1})
    computed on a SUB-SAMPLED grid (every ~1-5s or every-k-th tick) — sub-sampling is
    what denoises microstructure/bid-ask-bounce noise out of the per-tick series
    (the verification's correction: do NOT EWMA per-tick returns directly).

    ``half_life`` is in GRID-STEP count (not seconds): the EWMA decay
    lambda = exp(ln(0.5)/half_life), so the most recent steps dominate. Returns the
    per-grid-step stdev (sqrt of the EWMA variance), or None when fewer than 2 grid
    returns exist (caller falls back to expected_move). Pure."""
    rs = [r for r in (grid_log_returns or []) if isinstance(r, (int, float)) and math.isfinite(r)]
    if len(rs) < 2:
        return None
    try:
        hl = float(half_life)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(hl) and hl > 0):
        return None
    lam = math.exp(math.log(0.5) / hl)  # decay per step, in (0, 1)
    # EWMA of squared returns, oldest-first; mean ~0 over a short event grid so we
    # use E[r^2] as the variance estimator (standard short-horizon RV).
    ewma_var = rs[0] * rs[0]
    for r in rs[1:]:
        ewma_var = lam * ewma_var + (1.0 - lam) * (r * r)
    if not (math.isfinite(ewma_var) and ewma_var >= 0.0):
        return None
    return math.sqrt(ewma_var)


def roll_effective_spread_pct(grid_log_returns: list[float]) -> float | None:
    """Roll (1984) effective-spread estimator as a FRACTION of price, from the same
    EVENT-GRID returns. Roll: spread = 2 * sqrt(-Cov(r_t, r_{t-1})) when the lag-1
    autocovariance of returns is NEGATIVE (the bid-ask bounce signature). Returns the
    HALF-spread fraction (spread/2 ≈ the one-sided distance the stop must clear to sit
    outside the bounce), or None when the autocovariance is non-negative (no bounce
    signature ⇒ no Roll estimate; caller uses the vol floor alone). Pure — used to
    push the trail floor OUTSIDE the round-trip noise so a healthy pullback isn't
    shaken out."""
    rs = [r for r in (grid_log_returns or []) if isinstance(r, (int, float)) and math.isfinite(r)]
    if len(rs) < 3:
        return None
    n = len(rs) - 1
    mean = sum(rs) / len(rs)
    cov = 0.0
    for i in range(1, len(rs)):
        cov += (rs[i] - mean) * (rs[i - 1] - mean)
    cov /= n
    if not math.isfinite(cov) or cov >= 0.0:
        return None
    spread_frac = 2.0 * math.sqrt(-cov)  # full effective spread as a return-fraction
    if not (math.isfinite(spread_frac) and spread_frac > 0.0):
        return None
    return spread_frac / 2.0  # half-spread (one-sided)


def volnorm_trail_dist_pct(
    *,
    rv_live: float,
    expected_hold_s: float,
    grid_secs: float,
    k: float,
    vol_floor_pct: float,
    effective_spread_pct: float | None = None,
    spread_floor_mult: float = 1.5,
    max_dist_pct: float = 0.15,
) -> float:
    """MATH-VERIFIED vol-normalized trail DISTANCE as a fraction of price.

      rv_hold      = rv_live * sqrt(N),  N = expected_hold_s / grid_secs
                     (both LIVE-derived: expected_hold from the median realized hold of
                     recent scalps, grid_secs from the realized-vol event grid — no magic
                     horizon).
      candidate    = k * rv_hold                          (k in [1.1, 1.7], default 1.3)
      floor_pct    = max(vol_floor_pct, spread_floor_mult * effective_spread_pct)
                     (the stop sits OUTSIDE both the live vol-floor AND the bounce)
      trail_dist   = clamp(candidate, floor_pct, max_dist_pct)

    ``rv_live`` is the per-grid-step stdev (denoised_rv_ewma) and the sqrt-of-time rule
    scales it to the holding horizon in the SAME grid units (N = expected_hold_s /
    grid_secs = the number of GRID STEPS over the hold, NOT the number of ticks). Using
    a tick-count here would over-scale by sqrt(tick_rate * grid_secs) and widen the band
    well past the literature-justified k*rv_hold. ``vol_floor_pct`` is the existing entry
    vol-floor (reuse, not a new magic number). ``max_dist_pct`` mirrors the existing 0.15
    ATR-pct clamp. Pure; deterministic; INVARIANT-A is enforced by the caller
    (ratchet-only)."""
    try:
        rv = float(rv_live)
        hold = float(expected_hold_s)
        gs = float(grid_secs)
        kk = float(k)
        floor = max(0.0, float(vol_floor_pct or 0.0))
        cap = float(max_dist_pct)
    except (TypeError, ValueError):
        return max(0.0, float(vol_floor_pct or 0.0))
    if not (math.isfinite(rv) and rv >= 0.0):
        return floor
    n = (
        max(1.0, hold / max(gs, 1e-9))
        if (math.isfinite(hold) and math.isfinite(gs))
        else 1.0
    )
    rv_hold = rv * math.sqrt(n)
    candidate = max(0.0, kk) * rv_hold
    # Push the floor outside the bid/ask bounce when a Roll/Corwin-Schultz estimate exists.
    if effective_spread_pct is not None:
        try:
            es = float(effective_spread_pct)
            sm = float(spread_floor_mult)
            if math.isfinite(es) and es > 0.0 and math.isfinite(sm) and sm > 0.0:
                floor = max(floor, sm * es)
        except (TypeError, ValueError):
            pass
    if not (math.isfinite(cap) and cap > 0.0):
        cap = 0.15
    lo = min(floor, cap)
    return max(lo, min(candidate, cap))


def trail_width_maturity_factor(
    *,
    rv_live: float | None,
    vol_floor_pct: float | None,
    ofi_level: float | None,
    ofi_slope: float | None,
    max_widen: float = 2.0,
    vol_regime_pivot: float = 1.0,
) -> float:
    """DESIGN#2 — adaptive WIDEN factor in [1.0, ``max_widen``] applied to the 2A trail
    ``k`` so a FRESH, vol-rich runner trails near the chandelier-literature optimum
    (PF peaks ~3x ATR; 2x over-tightens) while a MATURING/exhausting runner decays back
    to 1.0 so the existing RIDE-LOCK LOCK/HARD bands tighten unimpeded.

    Two REAL signals, both already computed live (no new datum, no magic absolute):
      vol_regime = rv_live / max(vol_floor_pct, eps) — how energetic the LIVE tape is vs
                   the entry vol-floor. >pivot ⇒ vol-rich (room to run); <=pivot ⇒ calm.
      maturity   = fresh trend (ofi_level > 0 ∧ ofi_slope >= 0) ⇒ flow still being fed
                   ⇒ permit the widen; slope rolling over (< 0) ⇒ exhaustion ⇒ factor
                   DECAYS to 1.0 (let LOCK/HARD do the tightening).

    factor = 1.0 + (max_widen - 1.0) * vol_gate * maturity_gate
      vol_gate      = clamp((vol_regime - vol_regime_pivot) / vol_regime_pivot, 0, 1)
      maturity_gate = 1.0 if (ofi_level>0 ∧ ofi_slope>=0); 0.0 if ofi_slope<0; 0.5 if
                      flow read missing/flat (neutral half-widen, fail-toward-mild).

    Fail-NEUTRAL to 1.0 on any missing/degenerate input. Pure; deterministic; INVARIANT-A
    is enforced by the caller (a wider band only lowers the candidate, composed via max())."""
    try:
        mw = float(max_widen)
        piv = float(vol_regime_pivot)
    except (TypeError, ValueError):
        return 1.0
    if not (math.isfinite(mw) and mw > 1.0) or not (math.isfinite(piv) and piv > 0.0):
        return 1.0
    try:
        rv = float(rv_live)
        vf = float(vol_floor_pct)
    except (TypeError, ValueError):
        return 1.0
    if not (math.isfinite(rv) and rv >= 0.0 and math.isfinite(vf) and vf > 0.0):
        return 1.0
    vol_regime = rv / vf
    if not math.isfinite(vol_regime):
        return 1.0
    vol_gate = max(0.0, min(1.0, (vol_regime - piv) / piv))
    lvl = ofi_level if (ofi_level is not None and math.isfinite(float(ofi_level))) else None
    slp = ofi_slope if (ofi_slope is not None and math.isfinite(float(ofi_slope))) else None
    if lvl is None or slp is None:
        maturity_gate = 0.5
    elif float(slp) < 0.0:
        maturity_gate = 0.0
    elif float(lvl) > 0.0 and float(slp) >= 0.0:
        maturity_gate = 1.0
    else:
        maturity_gate = 0.5
    factor = 1.0 + (mw - 1.0) * vol_gate * maturity_gate
    if not math.isfinite(factor):
        return 1.0
    return max(1.0, min(mw, factor))


def volnorm_runner_trail_stop(
    *,
    high_water_mark: float,
    trail_dist_pct: float,
    breakeven_floor: float,
    current_stop: float,
    side_long: bool = True,
) -> float:
    """Apply a vol-normalized trail DISTANCE (from ``volnorm_trail_dist_pct``) to the
    high-water mark and compose it through INVARIANT-A.

      candidate = HWM * (1 - trail_dist_pct)              (long)
      new_stop  = max(current_stop, breakeven_floor, candidate)   ← INVARIANT-A

    INVARIANT-A (ratchet-only): the returned stop is NEVER below ``current_stop`` and
    NEVER below ``breakeven_floor`` — a smaller live vol (narrower candidate) declines
    to TIGHTEN but can never LOOSEN or null the structural/breakeven stop. The HWM is
    expected to be the MICRO-PRICE high (passed in by the caller), not the bid. Pure."""
    try:
        hwm = float(high_water_mark)
        dist = float(trail_dist_pct)
        be = float(breakeven_floor)
        cs = float(current_stop)
    except (TypeError, ValueError):
        return current_stop
    if not (math.isfinite(hwm) and math.isfinite(dist) and math.isfinite(cs)):
        return current_stop
    dist = max(0.0, dist)
    if side_long:
        candidate = hwm * (1.0 - dist)
        floors = [c for c in (cs, be, candidate) if math.isfinite(c)]
        return max(floors) if floors else cs  # INVARIANT-A: ratchet-only
    candidate = hwm * (1.0 + dist)
    caps = [c for c in (cs, be, candidate) if math.isfinite(c)]
    return min(caps) if caps else cs


def micro_price(bid: float, bid_size: float, ask: float, ask_size: float) -> float | None:
    """Stoikov micro-price: (bid*ask_size + ask*bid_size) / (bid_size + ask_size).

    The size-weighted fair value — leans toward the side with MORE depth (the side
    less likely to move), a less-noisy HWM reference than the raw bid for the trail.
    Returns None on degenerate inputs. Pure."""
    try:
        b, bs, a, as_ = float(bid), float(bid_size), float(ask), float(ask_size)
    except (TypeError, ValueError):
        return None
    denom = bs + as_
    if not (math.isfinite(b) and math.isfinite(a) and math.isfinite(denom)) or denom <= 0:
        return None
    mp = (b * as_ + a * bs) / denom
    return mp if math.isfinite(mp) else None


# ---------------------------------------------------------------------------
# LEVER 2B — VELOCITY/PERSISTENCE RIDE-LOCK (CORE) on top of the 2A vol-norm trail.
#
# The 2A vol-norm trail correctly SIZES the band, but it is still MECHANICAL: it will
# tighten a runner out on a healthy mid-thrust pullback, and a true climax tops a full
# candle before the candle-shaped exits print (the ASTC/DCOY/LI/AMPX/TMC "breaks don't
# hold, -$17" leak). 2B reads the DENOISED order flow and decides a REGIME:
#
#   RIDE   — denoised signed-flow / OFI-SLOPE > 0 AND tick_rate persists ⇒ the thrust is
#            still being fed; hold the band WIDE (return the 2A width unchanged) so the
#            runner extends. NEVER loosens an existing stop (INVARIANT-A at the call site).
#   LOCK   — the OFI-SLOPE / flow ROLLS OVER (turns negative) while price is NEAR the HWM ⇒
#            climax; COLLAPSE the band to a tight giveback (sell into strength BEFORE a full
#            candle prints — faster than the topping-tail).
#   HARD   — strong-negative flow WITH sellers lifting THROUGH the micro-price ⇒ the most
#            decisive distribution read; an even tighter climax band.
#
# The SLOPE is the 1st derivative of the DENOISED (EWMA) OFI LEVEL — NOT the raw
# 2nd-derivative signed_accel (noise-amplifying, per the math verification). All functions
# here are PURE (no DB, no clock) for replay/live parity + unit tests. Every output is a
# candidate WIDTH/STOP composed through INVARIANT-A by the caller (ratchet-only). docs/DESIGN/MOMENTUM_LANE.md
# ---------------------------------------------------------------------------


def ewma_series(values: list[float], *, half_life: float) -> list[float] | None:
    """Causal EWMA of ``values`` (oldest-first), one output per input. ``half_life`` is
    in STEP count: lambda = exp(ln(0.5)/half_life). Returns the smoothed series, or None
    on < 1 finite value / bad half_life. Pure — the DENOISING step that turns the noisy
    per-grid OFI LEVEL into the smooth level whose 1st difference is the rollover signal."""
    vs = [v for v in (values or []) if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vs:
        return None
    try:
        hl = float(half_life)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(hl) and hl > 0):
        return None
    lam = math.exp(math.log(0.5) / hl)  # decay per step, in (0, 1)
    out: list[float] = [vs[0]]
    for v in vs[1:]:
        out.append(lam * out[-1] + (1.0 - lam) * v)
    return out


def ofi_level_and_slope(
    ofi_level_series: list[float], *, half_life: float
) -> tuple[float | None, float | None]:
    """DENOISED OFI LEVEL + its EWMA SLOPE (the 1st derivative on the event-time series).

    ``ofi_level_series`` is the per-event-grid-bucket aggressor imbalance in [-1, 1]
    (oldest-first; Lee-Ready signed). We EWMA-smooth it (``ewma_series``) and return
    ``(level, slope)`` where ``level`` is the most-recent smoothed OFI and ``slope`` is the
    LAST consecutive EWMA difference (smoothed[-1] - smoothed[-2]) — the denoised 1st
    derivative. ``slope > 0`` ⇒ flow building (RIDE); ``slope < 0`` ⇒ flow rolling over
    (LOCK candidate). Returns ``(None, None)`` on < 2 grid buckets (caller falls back to
    the 2A trail). Using the 1st derivative of the DENOISED level — NOT the raw 2nd-
    derivative signed_accel — is the verified, noise-suppressing choice. Pure."""
    sm = ewma_series(ofi_level_series, half_life=half_life)
    if sm is None or len(sm) < 2:
        # a single smoothed value still gives a level (no slope yet)
        if sm and len(sm) == 1:
            return float(sm[0]), None
        return None, None
    return float(sm[-1]), float(sm[-1] - sm[-2])


def velocity_persistence_ride_lock(
    *,
    high_water_mark: float,
    entry_price: float,
    bid: float,
    base_trail_dist_pct: float,
    ofi_level: float | None,
    ofi_slope: float | None,
    tick_rate_per_s: float | None,
    entry_tick_rate_per_s: float | None,
    persist_frac: float,
    breakeven_floor: float,
    current_stop: float,
    micro_price_ref: float | None = None,
    last_trade_px: float | None = None,
    ofi_threshold: float = 0.25,
    lock_band_pct: float | None = None,
    side_long: bool = True,
) -> dict[str, Any]:
    """Velocity/persistence RIDE-LOCK regime decision on top of the 2A vol-norm trail.

    Given the 2A trail width (``base_trail_dist_pct``) and the DENOISED live flow
    (``ofi_level`` + ``ofi_slope`` from ``ofi_level_and_slope``) + the live ``tick_rate``,
    pick a regime and return a band WIDTH and a ratchet-only stop CANDIDATE:

      RIDE  — flow positive (level > 0 ∧ slope >= 0) AND the pace persists
              (tick_rate >= entry_tick_rate * persist_frac): keep ``base_trail_dist_pct``
              (the move is still being fed — do NOT mechanically tighten).
      LOCK  — flow rolls over (slope < 0) while NEAR the high (giveback small): COLLAPSE
              to a tight giveback band (``lock_band_pct``, default = half the 2A width,
              floored small) so the next tick exits near the top. Sell into strength.
      HARD  — strong-negative flow (level <= -ofi_threshold ∧ slope < 0) AND sellers
              lifting THROUGH the micro-price (the last trade prints AT/BELOW the fair
              value ``micro_price_ref`` — sellers hitting the bid down): an even tighter
              band (half the LOCK band).
      else  — NEUTRAL: keep ``base_trail_dist_pct`` (defer to the 2A trail).

    The returned ``new_stop_floor`` is ALWAYS ``max(current_stop, breakeven_floor,
    HWM*(1-width))`` (long) — INVARIANT-A, ratchet-only: a RIDE regime never loosens the
    live stop (a wider band simply declines to tighten further), and only a LOCK/HARD
    regime that lands ABOVE the current stop actually moves it. Pure; fail-safe (any
    missing/NaN flow input ⇒ NEUTRAL, candidate == the 2A-width stop, no behavior change)."""
    out: dict[str, Any] = {
        "regime": "neutral",
        "band_pct": base_trail_dist_pct,
        "new_stop_floor": current_stop,
        "fired": False,
        "ride": False,
        "ofi_level": ofi_level,
        "ofi_slope": ofi_slope,
        "tick_rate": tick_rate_per_s,
        "persist_ok": None,
    }
    if not side_long:
        return out
    try:
        hwm = float(high_water_mark)
        entry = float(entry_price)
        b = float(bid)
        cs = float(current_stop)
        be = float(breakeven_floor)
        base_w = max(0.0, float(base_trail_dist_pct))
    except (TypeError, ValueError):
        return out
    if not (math.isfinite(hwm) and math.isfinite(entry) and math.isfinite(b) and math.isfinite(cs)):
        return out
    if entry <= 0 or hwm <= 0 or b <= 0:
        return out

    def _stop_from_width(width: float) -> float:
        cand = hwm * (1.0 - max(0.0, width))
        floors = [c for c in (cs, be, cand) if math.isfinite(c)]
        return max(floors) if floors else cs  # INVARIANT-A: ratchet-only

    # ---- denoised flow reads (fail-safe to None) ----
    lvl = ofi_level if (ofi_level is not None and math.isfinite(float(ofi_level))) else None
    slope = ofi_slope if (ofi_slope is not None and math.isfinite(float(ofi_slope))) else None
    if lvl is None or slope is None:
        # no usable flow read ⇒ NEUTRAL, defer entirely to the 2A trail (byte-identical).
        out["new_stop_floor"] = _stop_from_width(base_w)
        out["band_pct"] = base_w
        return out

    try:
        thr = abs(float(ofi_threshold))
    except (TypeError, ValueError):
        thr = 0.25

    # ---- persistence: is the pace still being fed? (live tick_rate vs entry pace) ----
    persist_ok = None
    try:
        tr = float(tick_rate_per_s) if tick_rate_per_s is not None else None
        etr = float(entry_tick_rate_per_s) if entry_tick_rate_per_s is not None else None
        pf = max(0.0, float(persist_frac))
        if tr is not None and etr is not None and math.isfinite(tr) and math.isfinite(etr) and etr > 0:
            persist_ok = tr >= etr * pf
    except (TypeError, ValueError):
        persist_ok = None
    out["persist_ok"] = persist_ok

    # ---- giveback off the high (near-high test, position-relative as a fraction of width)
    # near the high == within the 2A band (the runner has NOT already pulled back past it).
    giveback_frac = (hwm - b) / hwm if hwm > 0 else 1.0
    near_high = giveback_frac <= base_w if base_w > 0 else (giveback_frac <= 0.0)

    # ---- LOCK band: tight giveback. Default = half the 2A width, floored small so it is a
    # genuine climax-lock (the 2A floor already keeps it outside the bounce). One derived
    # number, no fresh magic. HARD band: half the LOCK band (the most decisive read).
    try:
        lock_w = float(lock_band_pct) if lock_band_pct is not None else max(0.001, 0.5 * base_w)
        if not (math.isfinite(lock_w) and lock_w > 0):
            lock_w = max(0.001, 0.5 * base_w)
    except (TypeError, ValueError):
        lock_w = max(0.001, 0.5 * base_w)
    hard_w = max(0.0005, 0.5 * lock_w)

    # ---- sellers lifting THROUGH the micro-price: the LAST executed trade prints AT/BELOW
    # the fair value (micro_price_ref ≈ the mid on an L1 tape) — aggressive sellers hitting
    # the bid down through fair value (distribution). Fail-safe: missing ref/print ⇒ False
    # (then HARD cannot fire and the regime degrades to LOCK on the same rollover).
    sellers_through = False
    if micro_price_ref is not None and last_trade_px is not None:
        try:
            mp = float(micro_price_ref)
            lp = float(last_trade_px)
            if math.isfinite(mp) and mp > 0 and math.isfinite(lp) and lp > 0:
                sellers_through = lp <= mp  # last print at/below fair value ⇒ hit down
        except (TypeError, ValueError):
            sellers_through = False

    # ---- regime decision ----
    flow_positive = lvl > 0.0 and slope >= 0.0
    rolling_over = slope < 0.0
    strong_negative = lvl <= -thr and slope < 0.0

    if strong_negative and sellers_through and near_high:
        out["regime"] = "hard"
        width = hard_w
    elif rolling_over and near_high:
        out["regime"] = "lock"
        width = lock_w
    elif flow_positive and (persist_ok is None or persist_ok):
        # RIDE: still being fed (or no pace datum) ⇒ hold the 2A band WIDE; do not tighten.
        out["regime"] = "ride"
        out["ride"] = True
        width = base_w
    else:
        out["regime"] = "neutral"
        width = base_w

    out["band_pct"] = width
    new_floor = _stop_from_width(width)
    out["new_stop_floor"] = new_floor
    out["fired"] = bool(new_floor > cs)
    return out


def cushion_adaptive_trail_stop(
    *,
    high_water_mark: float,
    entry_price: float,
    atr_pct: float,
    stop_atr_mult: float,
    day_realized_usd: float,
    position_risk_usd: float,
    breakeven_floor: float,
    current_stop: float,
    side_long: bool = True,
    ema_5m: float | None = None,
    regime_band_mult: float = 1.0,
) -> float:
    """Cushion-adaptive runner trail (Ross day-4, 2026-06-11): exit patience is
    NOT one number — it scales with the CUSHION. "In the small account, the
    second I see an exit indicator I sell. In my big account I can hold through
    a couple of those." Encoded: with no cushion the trail hugs the floor width
    (protect the round-trip); as this position's unrealized R plus the day's
    banked R approach the trade's own reward:risk plan (2R), the trail widens
    to the ceiling (let the runner run).

    Width band floor/ceiling are the two documented knobs (defaults 500/1000
    bps — the two-day exit-capture study band: <=400 proved whipsaw-negative,
    BATL capture 0.44->0.71 at 500; refit from live capture ratios weekly).
    Everything between is derived: cushion_r = unrealized R + max(0, day R);
    patience = cushion_r / reward_risk, clamped [0, 1].

    Ratchet-only (never loosens), never below ``breakeven_floor``. Pure for
    replay/live parity."""
    try:
        hwm = float(high_water_mark)
        entry = float(entry_price)
        be = float(breakeven_floor)
        cs = float(current_stop)
    except (TypeError, ValueError):
        return current_stop
    if not (math.isfinite(hwm) and math.isfinite(entry) and math.isfinite(cs)) or entry <= 0:
        return current_stop
    try:
        floor_bps = float(getattr(settings, "chili_momentum_trail_floor_bps", 500.0) or 500.0)
        ceil_bps = float(getattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0) or 1000.0)
    except (TypeError, ValueError):
        floor_bps, ceil_bps = 500.0, 1000.0
    ceil_bps = max(ceil_bps, floor_bps)
    try:
        rr = float(getattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0) or 2.0)
    except (TypeError, ValueError):
        rr = 2.0
    # The trade's own risk unit, frozen at entry (same formula the stop used).
    risk_dist = entry * max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.0))
    unrealized_r = max(0.0, (hwm - entry) / risk_dist) if (side_long and risk_dist > 0) else 0.0
    day_r = 0.0
    try:
        pr = float(position_risk_usd)
        if pr > 0 and math.isfinite(float(day_realized_usd)):
            day_r = max(0.0, float(day_realized_usd) / pr)
    except (TypeError, ValueError):
        day_r = 0.0
    patience = min(1.0, (unrealized_r + day_r) / max(rr, 1e-9))
    trail_bps = floor_bps + (ceil_bps - floor_bps) * patience
    # GAP3 (regime-conditioned hold-time, Warrior re-audit 2026-06-26): scale the
    # give-back band by the ENTRY regime. HOT/explosive ⇒ mult > 1 (wider band ⇒ a
    # LOWER trailed candidate ⇒ the runner is held through red longer); COLD ⇒
    # mult < 1 (tighter band ⇒ a HIGHER trailed candidate ⇒ chop is cut quicker).
    # Default 1.0 ⇒ byte-identical. This only ever moves the trailed CANDIDATE; the
    # ratchet-only max(cs, be, trailed) below means an existing stop is NEVER widened
    # (a hot mult cannot loosen the live stop — it just declines to tighten it).
    try:
        _rbm = float(regime_band_mult)
        if math.isfinite(_rbm) and _rbm > 0:
            trail_bps = trail_bps * _rbm
    except (TypeError, ValueError):
        pass
    if side_long:
        trailed = hwm * (1.0 - trail_bps / 10_000.0)
        # 5m-EMA structural runner anchor (2026-06-12 exit study: the bps band
        # captured only 39% of BATL's MFE — Ross trails the 5m 9EMA on a
        # trending runner and exits when the STRUCTURE breaks, not when an
        # arbitrary band is grazed). When the runner is >= 1R in profit and
        # the 5m EMA sits below the high-water mark (healthy uptrend), the
        # structure replaces the band: stop = ema_5m − ATR-scaled wick buffer.
        # Ratchet-only is preserved by the max() below.
        if ema_5m is not None and unrealized_r >= 1.0:
            try:
                _e5 = float(ema_5m)
                if math.isfinite(_e5) and 0.0 < _e5 < hwm:
                    _buf = entry * max(0.001, float(atr_pct or 0.0) * 0.25)
                    trailed = max(be, _e5 - _buf)
            except (TypeError, ValueError):
                pass
        floors = [c for c in (cs, be, trailed) if math.isfinite(c)]
        return max(floors) if floors else cs
    trailed = hwm * (1.0 + trail_bps / 10_000.0)
    return min(cs, trailed)


def ofi_exhaustion_lock(
    *,
    high_water_mark: float,
    entry_price: float,
    bid: float,
    atr_pct: float,
    stop_atr_mult: float,
    ofi: float | None,
    micro_edge: float | None,
    hidden_seller: float | None,
    reward_risk: float,
    current_stop: float,
    breakeven_floor: float,
    current_band_bps: float,
    candle_exhaustion: bool | None = None,
    candle_gate_live: bool = False,
    side_long: bool = True,
) -> dict[str, Any]:
    """Adaptive order-flow exhaustion lock for the crypto momentum runner.

    The cushion trail band (``cushion_adaptive_trail_stop``) is loose by design
    on an extended runner (≈800bps at +1.9R into a 3R plan). MEGA-USD peaked at
    +1.9R, never reached the 3R partial, so ``partial_taken`` stayed False, the
    runner floor stayed at the loss-side stop, and the +1.9R peak bled back
    inside the band that never triggered. This helper fires an adaptive, flow-
    CONFIRMED tighten (and optionally arms the partial) the moment live order
    flow says the thrust is exhausting — BEFORE the fixed target.

    Mirror of the entry tilt (``viability``: boost on ``OFI > +T ∧ micro > 0``).
    Confluence-AND so a single noisy OFI blip never sells a winner:

      1. profit-arm   peak_r ≥ arm_r (= ``arm_frac · rr``)  — only ever lock a winner
      2. micro rollover  micro_edge < 0  (the spoof-resistant state anchor)
      3. OFI flip       ofi < −T  (windowed confirmation, never alone)
      4. giveback       (hwm − bid) ≥ k · risk_dist  (extension/deceleration check)

    Accelerant (OR-bypass of 3+4): hidden-seller absorption ≥ threshold arms on
    1+2 alone — distribution is the one LEADING signal. Off by default.

    1m-CANDLE CONFIRMER (``candle_exhaustion``, 2026-06-16): one MORE AND-gate on
    the FLOW confluence — the live entry trigger runs on 1m, but the lock's only
    candle read upstream is the coarse 15m bar. A 1m topping-tail / MACD-hist
    rollover corroborates the flow rollover. AND-gated ⇒ it can only SUPPRESS a flow
    fire whose 1m candle shows no exhaustion (a noisy-OFI early-sell); it never
    causes a new fire. Fail-OPEN: ``candle_exhaustion=None`` ⇒ ``candle_ok=True`` ⇒
    no restriction (existing captures preserved). OBSERVE-FIRST: when
    ``candle_gate_live`` is False the LIVE decision is byte-identical and only
    ``candle_would_suppress`` (the A/B) is populated; when True the gate applies to
    the confluence path (the absorption OR-bypass is intentionally never candle-
    gated). INVARIANT A is preserved either way (the gate only ever blocks a fire,
    never lowers a stop).

    ADAPTIVE & single-knob: ``base_lock_bps`` is the only irreducible number.
    ``arm_r`` derives from the plan's own ``rr``; the giveback arm derives from
    the position's own ``risk_dist`` (ATR); lock tightness scales with the move's
    percentile (``peak_r/rr``) and the flow magnitude. Thresholds reuse the
    entry's tuned ``chili_momentum_ofi_threshold``.

    RATCHET-ONLY / NEVER-LOOSEN (Invariant A): ``new_stop_floor`` is
    unconditionally ``max(current_stop, breakeven_floor, candidate)`` — it can
    only raise, never null, never write below the structural stop. The caller
    additionally re-applies its own ``> stop_px`` ratchet guard (belt-and-
    suspenders). The candidate lock is also clamped no looser than the cushion
    band already produced this tick (``current_band_bps``), so the lock can only
    EQUAL or TIGHTEN the trail, never widen it.

    Pure (no I/O) for replay/live parity. Fail-safe: missing/NaN signals →
    no-op (``new_stop_floor == current_stop``, ``partial_arm == False``). The
    returned dict ALSO carries the A/B counterfactual (the fixed-R:R candidate
    stop, lock OFF) so realized PnL can be measured against the baseline live.
    """
    out: dict[str, Any] = {
        "new_stop_floor": current_stop,
        "partial_arm": False,
        "armed": False,
        "fired": False,
        "trigger": None,
        "peak_r": None,
        "lock_bps": None,
        "counterfactual_fixed_stop": current_stop,  # band-only stop, lock OFF
        # 1m-candle confirmer A/B (observe-first); see the candle paragraph above.
        "candle_exhaustion": candle_exhaustion,
        "candle_ok": True,
        "candle_gate_live": bool(candle_gate_live),
        "candle_would_suppress": False,
    }
    if not side_long:
        return out
    try:
        hwm = float(high_water_mark)
        entry = float(entry_price)
        b = float(bid)
        cs = float(current_stop)
        be = float(breakeven_floor)
        band_bps = float(current_band_bps)
    except (TypeError, ValueError):
        return out
    if not (math.isfinite(hwm) and math.isfinite(entry) and math.isfinite(b) and math.isfinite(cs)):
        return out
    if entry <= 0 or hwm <= 0:
        return out

    # The trade's own risk unit, frozen at entry (same formula the stop used).
    risk_dist = entry * max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.0))
    if not (math.isfinite(risk_dist) and risk_dist > 0):
        return out
    peak_r = max(0.0, (hwm - entry) / risk_dist)
    out["peak_r"] = round(peak_r, 4)

    # ---- knobs (single irreducible base; everything else derived/reused) ----
    try:
        rr = float(reward_risk) if math.isfinite(float(reward_risk)) and float(reward_risk) > 0 else 2.0
    except (TypeError, ValueError):
        rr = 2.0
    try:
        thr = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
    except (TypeError, ValueError):
        thr = 0.25
    try:
        base_lock_bps = float(getattr(settings, "chili_momentum_exit_ofi_base_lock_bps", 120.0) or 120.0)
    except (TypeError, ValueError):
        base_lock_bps = 120.0
    base_lock_bps = max(1.0, base_lock_bps)
    try:
        arm_frac = float(getattr(settings, "chili_momentum_exit_ofi_arm_frac", 0.5) or 0.5)
    except (TypeError, ValueError):
        arm_frac = 0.5
    arm_frac = min(max(arm_frac, 0.0), 1.0)
    # arm_r derives from the plan's OWN reward:risk (no fixed-R magic): half the
    # planned reward, floored at 0.5R so a sub-1R plan still arms a winner.
    arm_r = max(0.5, arm_frac * rr)
    # The giveback corroborant derives from the position's OWN risk unit (ATR),
    # not a fixed bps: require the pullback off the high to exceed a fraction of
    # 1R, scaled DOWN as the move extends (a 2.5R move needs less confirmation
    # than a 1R move). k ∈ [0.15, 0.5] of risk_dist.
    giveback_k = max(0.15, 0.5 - 0.15 * max(0.0, peak_r - arm_r))
    giveback_dist = giveback_k * risk_dist

    # ---- live reads (fail-safe to no-op) ----
    o = None
    m = None
    hs = None
    try:
        if ofi is not None and math.isfinite(float(ofi)):
            o = float(ofi)
    except (TypeError, ValueError):
        o = None
    try:
        if micro_edge is not None and math.isfinite(float(micro_edge)):
            m = float(micro_edge)
    except (TypeError, ValueError):
        m = None
    try:
        if hidden_seller is not None and math.isfinite(float(hidden_seller)):
            hs = float(hidden_seller)
    except (TypeError, ValueError):
        hs = None

    # ---- counterfactual: fixed-R:R baseline stop this tick (lock OFF) ----
    # = exactly what the cushion band would have left as the floor (no lock).
    cf_band = hwm * (1.0 - max(0.0, band_bps) / 10_000.0) if math.isfinite(band_bps) else cs
    out["counterfactual_fixed_stop"] = max(cs, be, cf_band) if math.isfinite(cf_band) else max(cs, be)

    # ---- gate 1: profit-arm (only ever lock a winner) ----
    if peak_r < arm_r:
        return out
    out["armed"] = True

    # ---- accelerant: hidden-seller absorption at the highs (1+2 only) ----
    try:
        hs_enabled = bool(getattr(settings, "chili_momentum_exit_ofi_hidden_seller_enabled", False))
    except (TypeError, ValueError):
        hs_enabled = False
    # Hidden-seller score is a ratio (refill / price-advance); "strong" absorption
    # is score >= 1.0 (refill at least matches advance). Derived, not a new knob.
    absorption = hs_enabled and hs is not None and hs >= 1.0 and (m is not None and m < 0.0)

    # ---- confluence-AND (the normal path) ----
    micro_roll = m is not None and m < 0.0
    ofi_flip = o is not None and o < -thr
    giveback = (hwm - b) >= giveback_dist
    confluence = micro_roll and ofi_flip and giveback

    # ---- 1m candle confirmer: one MORE AND-gate on the FLOW path ----
    # Fail-OPEN (None ⇒ ok). OBSERVE-FIRST: when candle_gate_live is False the live
    # fire decision is UNCHANGED (byte-identical) and only the would-suppress A/B is
    # recorded; when True the flow confluence additionally requires the candle. The
    # absorption OR-bypass (leading distribution signal) is never candle-gated.
    candle_ok = (candle_exhaustion is None) or bool(candle_exhaustion)
    out["candle_ok"] = candle_ok
    # A pure-confluence fire the candle gate would block (regardless of whether the
    # gate is live this tick): the operator counts these vs subsequent price to prove
    # they are early-sells (recoveries) before flipping candle_gate_live on.
    out["candle_would_suppress"] = bool(confluence and not candle_ok and not absorption)
    confluence_effective = (confluence and candle_ok) if candle_gate_live else confluence

    if not (confluence_effective or absorption):
        return out

    out["fired"] = True
    out["trigger"] = "absorption" if (absorption and not confluence_effective) else "ofi_micro_confluence"

    # ---- adaptive lock tightness (tighten with strength + flow) ----
    # strength_scale: stronger move (higher peak_r within its rr plan) ⇒ tighter
    # lock (more to protect, less expected continuation). 1.0 at the arm, →~0.4
    # as the move reaches the full plan.
    strength_scale = 1.0 / (1.0 + 0.6 * max(0.0, peak_r - arm_r))
    # flow_scale: harder OFI flip / deeper micro rollover ⇒ tighter. Bounded.
    flow_excess = (abs(o) - thr) if o is not None else 0.0
    micro_mag = (abs(m) / 50.0) if m is not None else 0.0  # ~50bps micro = full unit
    flow_scale = 1.0 / (1.0 + max(0.0, flow_excess) + min(1.0, micro_mag))
    if absorption and hs is not None:
        flow_scale = min(flow_scale, 1.0 / (1.0 + min(2.0, hs)))
    lock_bps = base_lock_bps * strength_scale * flow_scale
    # Bounds: never wider than the cushion band already is (so the lock only ever
    # tightens vs the realized trail); never below a small floor of the base.
    lock_floor_bps = 0.25 * base_lock_bps
    ceil_bps = band_bps if (math.isfinite(band_bps) and band_bps > 0) else base_lock_bps
    lock_bps = min(max(lock_bps, lock_floor_bps), ceil_bps)
    out["lock_bps"] = round(lock_bps, 2)

    candidate = hwm * (1.0 - lock_bps / 10_000.0)
    # INVARIANT A: unconditional ratchet floor — never below current stop or BE.
    floors = [c for c in (cs, be, candidate) if math.isfinite(c)]
    out["new_stop_floor"] = max(floors) if floors else cs

    # ---- Action B: arm the partial when exhaustion is STRONG ----
    # Strong = decisive flow (both OFI and micro decisively reversed) or
    # absorption. The partial routes through the audited scale-out path and
    # flips _be_floor to breakeven — the exact MEGA give-back fix.
    strong_flow = (o is not None and o < -2.0 * thr) and (m is not None and m < 0.0)
    out["partial_arm"] = bool(absorption or strong_flow)
    return out


def tape_accel_reversal_exit(
    *,
    high_water_mark: float,
    entry_price: float,
    bid: float,
    atr_pct: float,
    stop_atr_mult: float,
    reward_risk: float,
    current_stop: float,
    breakeven_floor: float,
    signed_tape_accel: float | None,
    prev_signed_tape_accel: float | None = None,
    side_long: bool = True,
) -> dict[str, Any]:
    """Tape-acceleration reversal exit — SELL INTO STRENGTH at the spike's climax.

    The operator's model: the equity winners give back the spike because no exit
    fires at exhaustion — ``scale_out_limit`` banks dust (~$2) and ``trail_stop``
    only triggers AFTER the giveback. The OFI-exhaustion lock (``ofi_exhaustion_lock``
    above) is the L2-flow analogue, but it is L2-DATA-STARVED on equity (only ~88/684
    names carry ``iqfeed_depth_snapshots``) so it no-ops on most names. This helper
    rides ``signed_tape_accel`` from the executed TRADE tape (``iqfeed_trade_ticks``,
    broad equity coverage) — it covers the names the OFI lock misses.

    It locks the runner the moment the executed-tape PUSH ends / turns NEAR the high
    (sell into strength, before the giveback), NOT after a drop (that is the trail's
    job). It is a sibling of the OFI lock and COMPOSES with it: both run, whichever
    ratchets the stop HIGHER wins via Invariant A.

    Gates (confluence-AND, fail-safe):

      1. profit-arm  ``peak_r ≥ arm_r`` (= ``arm_frac · rr``, floored 0.5R) — only
         ever lock a WINNER. Below the arm the trail/stop owns healthy pullbacks.
      2. REVERSAL    ``signed_tape_accel ≤ 0`` (the aggressive-buy push has ended /
         turned). When ``prev_signed_tape_accel`` is supplied, require a genuine TURN
         (``prev > 0 ∧ current ≤ 0``) for a cleaner climax read; with no prior sample
         ``accel ≤ 0`` alone qualifies. STILL ACCELERATING (``accel > 0``) ⇒ NO fire
         (do not sell into a building spike).
      3. NEAR-HIGH   the giveback ``(hwm − bid)`` is SMALL — within an adaptive band
         (``giveback_frac · risk_dist``, the position's own ATR unit). If price has
         already given a lot back, this is the trail's job, not a sell-into-strength.

    On arm ∧ reversal ∧ near-high: candidate stop = ``bid − cushion`` where the
    cushion is a tight adaptive band off the bid (``base_lock_bps`` — the SAME
    irreducible base the OFI lock uses; NO new magic number). The next tick then
    exits at/near the top.

    RATCHET-ONLY / NEVER-LOOSEN (Invariant A): ``new_stop_floor`` is unconditionally
    ``max(current_stop, breakeven_floor, candidate)`` — it can only RAISE, never null,
    never write below the structural stop. ``fired = new_stop_floor > current_stop``.
    The caller re-applies its own ``> stop_px`` guard (belt-and-suspenders). This can
    therefore ONLY exit a winner near its top; it can NEVER cut a loser early or loosen
    a stop.

    Pure (no I/O) for replay/live parity. FAIL-SAFE: a short, any non-finite/missing
    input, or ``signed_tape_accel is None`` ⇒ no-op (``new_stop_floor == current_stop``,
    ``fired == False``). Crypto (``signed_tape_accel_features`` returns None upstream)
    therefore no-ops ⇒ byte-identical. ALWAYS returns ``counterfactual_fixed_stop ==
    current_stop`` (the lock-OFF baseline) so realized PnL can be A/B-measured.
    """
    out: dict[str, Any] = {
        "new_stop_floor": current_stop,
        "fired": False,
        "armed": False,
        "trigger": None,
        "peak_r": None,
        "counterfactual_fixed_stop": current_stop,  # lock-OFF baseline (no tighten)
        "reason": None,
    }
    if not side_long:
        out["reason"] = "not_long"
        return out
    # FAIL-SAFE: a missing tape signal (crypto / empty tape / any None) ⇒ no-op.
    if signed_tape_accel is None:
        out["reason"] = "no_tape"
        return out
    try:
        hwm = float(high_water_mark)
        entry = float(entry_price)
        b = float(bid)
        cs = float(current_stop)
        be = float(breakeven_floor)
        accel = float(signed_tape_accel)
    except (TypeError, ValueError):
        out["reason"] = "bad_input"
        return out
    if not (
        math.isfinite(hwm)
        and math.isfinite(entry)
        and math.isfinite(b)
        and math.isfinite(cs)
        and math.isfinite(accel)
    ):
        out["reason"] = "non_finite"
        return out
    if entry <= 0 or hwm <= 0 or b <= 0:
        out["reason"] = "non_positive_price"
        return out

    # The trade's own risk unit, frozen at entry — IDENTICAL risk_dist convention to
    # ofi_exhaustion_lock: entry · max(0.003, atr_pct · stop_atr_mult). atr_pct is the
    # raw entry_stop_atr_pct and stop_atr_mult the plan's stop_atr_mult (the SAME two
    # arguments the lock receives at the held tick), so peak_r here == the lock's peak_r.
    risk_dist = entry * max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.0))
    if not (math.isfinite(risk_dist) and risk_dist > 0):
        out["reason"] = "bad_risk_dist"
        return out
    peak_r = max(0.0, (hwm - entry) / risk_dist)
    out["peak_r"] = round(peak_r, 4)

    # ---- knobs: REUSE the OFI lock's irreducible base + arm_frac (no new magic) ----
    try:
        rr = float(reward_risk) if math.isfinite(float(reward_risk)) and float(reward_risk) > 0 else 2.0
    except (TypeError, ValueError):
        rr = 2.0
    try:
        arm_frac = float(getattr(settings, "chili_momentum_exit_ofi_arm_frac", 0.5) or 0.5)
    except (TypeError, ValueError):
        arm_frac = 0.5
    arm_frac = min(max(arm_frac, 0.0), 1.0)
    # arm_r derives from the plan's OWN reward:risk, floored 0.5R (parity with the OFI
    # lock) — a sub-1R plan still arms a winner.
    arm_r = max(0.5, arm_frac * rr)
    try:
        base_lock_bps = float(getattr(settings, "chili_momentum_exit_ofi_base_lock_bps", 120.0) or 120.0)
    except (TypeError, ValueError):
        base_lock_bps = 120.0
    base_lock_bps = max(1.0, base_lock_bps)
    # The ONE new documented knob: how close to the high the price must still be for
    # this to count as "into strength" (giveback ≤ giveback_frac · risk_dist).
    try:
        giveback_frac = float(
            getattr(settings, "chili_momentum_exit_accel_reversal_giveback_frac", 0.35) or 0.35
        )
    except (TypeError, ValueError):
        giveback_frac = 0.35
    giveback_frac = max(0.0, giveback_frac)
    giveback_dist = giveback_frac * risk_dist

    # ---- gate 1: profit-arm (only ever lock a winner) ----
    if peak_r < arm_r:
        out["reason"] = "below_arm"
        return out
    out["armed"] = True

    # ---- gate 2: tape-acceleration REVERSAL (the executed push has ended/turned) ----
    if prev_signed_tape_accel is not None:
        try:
            prev = float(prev_signed_tape_accel)
        except (TypeError, ValueError):
            prev = None
        if prev is not None and math.isfinite(prev):
            # genuine TURN: was pushing up, now ≤ 0 (a cleaner climax than ≤0 alone).
            reversal = (prev > 0.0) and (accel <= 0.0)
        else:
            reversal = accel <= 0.0
    else:
        reversal = accel <= 0.0
    if not reversal:
        out["reason"] = "still_accelerating"
        return out

    # ---- gate 3: NEAR-HIGH (sell INTO strength, not after a drop) ----
    giveback = hwm - b
    if giveback > giveback_dist:
        out["reason"] = "gave_back_too_much"  # the trail owns this, not the lock
        return out

    # ---- lock at the climax: tight adaptive cushion off the BID ----
    # cushion = base_lock_bps off the bid (the SAME irreducible base the OFI lock uses).
    # The candidate sits a hair below the live bid so the NEXT tick exits near the top;
    # Invariant A guarantees it can only ever RAISE the stop.
    cushion = b * (base_lock_bps / 10_000.0)
    candidate = b - cushion
    floors = [c for c in (cs, be, candidate) if math.isfinite(c)]
    new_floor = max(floors) if floors else cs
    out["new_stop_floor"] = new_floor
    out["fired"] = bool(new_floor > cs)
    out["trigger"] = "tape_accel_reversal"
    out["reason"] = "fired" if out["fired"] else "ratchet_no_raise"
    return out


# ── Measured-move scale target + double-top exhaustion (winner-management) ─────
# Ross "measured move": the FIRST leg up off the base breakout has a height; the
# move often extends a SECOND leg of about the SAME height. We measure the name's
# OWN initial impulse (impulse_leg_high − impulse_leg_entry, both frozen at the
# first-target scale-out) and project it ABOVE the impulse high to a measured-move
# target. At that target we SCALE OUT a fraction (the existing partial machinery)
# and ratchet the runner stop up — a PARTIAL, never a full cut. A strong runner
# that blows through keeps running on the cushion/chandelier trail (this helper
# only ever fires ONCE, sells a fraction, and tightens — it cannot flatten).
#
# Double-top exhaustion: price prints the impulse high, pulls back, then RETESTS
# the high and FAILS (a lower-high inside an ATR-relative band, optionally on weak
# flow). That is distribution at the level ⇒ tighten the stop (and optionally arm
# a partial). A CLEAN HIGHER-HIGH (price takes out the impulse high) is NOT a
# double-top ⇒ no exhaustion exit (the winner is left to run).
#
# ADAPTIVE, no flat-% magic: the target is the name's own leg height (not a fixed
# %); the double-top band is ATR-relative. ONE documented base each — the
# scale-out fraction and the double-top retest ATR-mult. Everything else is
# derived (the impulse height, the ATR risk unit frozen at entry). RATCHET-ONLY:
# every stop this module returns is max(current_stop, breakeven_floor, candidate).
# Flag OFF (default) ⇒ every helper is a pass-through no-op (byte-identical).


def measured_move_exit_enabled() -> bool:
    """Kill-switch for the measured-move scale target + double-top exhaustion.

    Default OFF ⇒ both helpers return their inert pass-through (no scale, no
    tighten) so the runner trails EXACTLY as before (byte-identical)."""
    return bool(getattr(settings, "chili_momentum_measured_move_exit_enabled", False))


def _measured_move_scale_fraction(default: float = 0.33) -> float:
    """Fraction of the ORIGINAL position sold into the measured-move target.

    ONE documented base (``chili_momentum_measured_move_exit_scale_fraction``).
    This is a SCALE-OUT (sell a slice into strength), distinct from the heavier
    first-target de-risk; bounded to the open interval so it can never sell 0%
    (no-op) or 100% (no runner)."""
    try:
        v = float(getattr(settings, "chili_momentum_measured_move_exit_scale_fraction", default))
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v):
        v = default
    return max(0.05, min(0.95, v))


def _double_top_atr_mult(default: float = 0.75) -> float:
    """ATR-relative retest tolerance for the double-top band.

    ONE documented base (``chili_momentum_measured_move_exit_double_top_atr_mult``).
    The retest "near the high" band is this multiple of the position's own ATR
    risk unit — adaptive to the name's volatility, NOT a fixed %. Bounded so a
    misconfig can never make the band absurdly wide or zero."""
    try:
        v = float(getattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", default))
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v):
        v = default
    return max(0.1, min(2.0, v))


def measured_move_target(
    *,
    entry_price: float,
    impulse_leg_high: float,
    side_long: bool = True,
) -> float | None:
    """Project the name's OWN first-leg height above the impulse high.

    leg_height = impulse_leg_high − entry (the base-breakout first leg up). The
    measured-move target = impulse_leg_high + leg_height (a second equal leg). No
    flat % — the projection is the name's own measured impulse. Returns None for a
    degenerate (non-positive) leg, a short, or any bad input (the caller no-ops).
    Pure for parity testing. docs/DESIGN/MOMENTUM_LANE.md"""
    if not side_long:
        return None
    try:
        e = float(entry_price)
        h = float(impulse_leg_high)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(e) and math.isfinite(h)) or e <= 0 or h <= 0:
        return None
    leg = h - e
    if leg <= 0:
        return None
    return h + leg


def measured_move_scale_exit_decision(
    *,
    flag_on: bool,
    current_qty: float,
    original_qty: float,
    entry_price: float,
    impulse_leg_high: float,
    bid: float,
    atr_pct: float,
    stop_atr_mult: float,
    current_stop: float,
    breakeven_floor: float,
    already_fired: bool = False,
    symbol: str | None = None,
    base_increment: float | None = None,
    base_min_size: float | None = None,
    side_long: bool = True,
) -> dict[str, Any]:
    """Decide the measured-move PARTIAL scale-out + runner-stop ratchet (pure).

    Returns ``{"fire": bool, "reason": str, "target_price": float|None,
    "scale_qty": float, "remainder_qty": float, "scale_fraction": float,
    "new_stop_floor": float, "leg_height": float|None}``.

    Fires ONCE (``already_fired`` gates re-fire) when the bid reaches the
    measured-move target (impulse high + leg height). On fire it sizes a
    ``_measured_move_scale_fraction`` slice of the ORIGINAL position via the shared
    ``scale_out_quantity`` splitter (so it never oversells or strands dust) and
    ratchets the RUNNER stop up to AT LEAST breakeven (the partial de-risked the
    rest). The remainder keeps running on the existing cushion/chandelier trail —
    this is a PARTIAL, never a full exit.

    WINNER-SAFE: a runner that has already blown PAST the target still only ever
    scales a FRACTION here; the remainder is untouched and trails on. RATCHET-ONLY:
    ``new_stop_floor = max(current_stop, breakeven_floor, breakeven_candidate)`` —
    never below the input stop. Flag OFF / short / no-op ⇒ ``fire=False`` and
    ``new_stop_floor == current_stop`` (byte-identical). Pure for parity testing.
    docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {
        "fire": False,
        "reason": "flag_off" if not flag_on else "wait",
        "target_price": None,
        "scale_qty": 0.0,
        "remainder_qty": max(0.0, float(current_qty or 0.0)),
        "scale_fraction": 0.0,
        "new_stop_floor": current_stop,
        "leg_height": None,
    }
    if not flag_on or not side_long:
        return out
    if already_fired:
        out["reason"] = "already_fired"
        return out
    try:
        e = float(entry_price)
        h = float(impulse_leg_high)
        b = float(bid)
        cs = float(current_stop)
        be = float(breakeven_floor)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (math.isfinite(e) and math.isfinite(h) and math.isfinite(b) and math.isfinite(cs)):
        out["reason"] = "bad_basis"
        return out
    tgt = measured_move_target(entry_price=e, impulse_leg_high=h, side_long=True)
    if tgt is None:
        out["reason"] = "no_leg"
        return out
    out["target_price"] = float(tgt)
    out["leg_height"] = float(h - e)
    # Ratchet candidate is ALWAYS at least breakeven; the partial de-risks the rest.
    be_candidate = max(e, be)  # breakeven of the runner, derived (no new magic)
    floors = [c for c in (cs, be, be_candidate) if math.isfinite(c)]
    ratchet_floor = max(floors) if floors else cs
    if b < tgt * (1.0 - 1e-9):  # target not yet reached
        out["reason"] = "target_not_reached"
        return out
    # Target reached — split a fraction of the ORIGINAL via the shared splitter.
    frac = _measured_move_scale_fraction()
    if symbol is not None:
        # crypto can take a heavier slice via the existing class knob; never below base
        try:
            ov = getattr(settings, "chili_momentum_crypto_scale_out_fraction", None)
            if _is_crypto_symbol(symbol) and ov is not None:
                ovf = float(ov)
                if math.isfinite(ovf) and 0.0 < ovf < 1.0:
                    frac = max(frac, min(0.95, ovf))
        except (TypeError, ValueError):
            pass
    out["scale_fraction"] = float(frac)
    scale_qty, remainder, can_split = scale_out_quantity(
        current_qty=current_qty,
        original_qty=original_qty,
        fraction=frac,
        base_increment=base_increment,
        base_min_size=base_min_size,
    )
    if not can_split:
        # Cannot split cleanly (dust) — do NOT flatten a runner here; the existing
        # target/trail machinery owns the flat case. We still ratchet the stop up.
        out["reason"] = "target_reached_no_split"
        out["new_stop_floor"] = ratchet_floor
        return out
    out["fire"] = True
    out["reason"] = "measured_move_target"
    out["scale_qty"] = float(scale_qty)
    out["remainder_qty"] = float(remainder)
    out["new_stop_floor"] = ratchet_floor
    return out


def double_top_exhaustion_check(
    *,
    flag_on: bool,
    impulse_leg_high: float,
    current_high: float,
    bid: float,
    entry_price: float,
    atr_pct: float,
    stop_atr_mult: float,
    ofi: float | None = None,
    micro_edge: float | None = None,
    side_long: bool = True,
) -> dict[str, Any]:
    """Detect a DOUBLE-TOP weak retest of the impulse high (pure, fail-safe).

    The impulse high prints, price pulls back, then RETESTS the high. Returns
    ``{"exhausted": bool, "weak_retest": bool, "clean_higher_high": bool,
    "reason": str, "retest_gap_atr": float|None, "flow_weak": bool}``.

    A DOUBLE-TOP (exhaustion) requires ALL:
      * the retest peak (``current_high``) is a LOWER high — strictly below the
        impulse high, AND
      * it is NEAR the high — within an ATR-relative band (``_double_top_atr_mult``
        × the position's own ATR risk unit) of the impulse high (a genuine retest,
        not a shallow bounce that never approached the prior peak), AND
      * the live bid has rolled back DOWN off that retest peak (rejected, not still
        pressing) — ``bid < current_high`` within the band.

    A CLEAN HIGHER-HIGH (``current_high`` ≥ impulse high) is NOT a double-top ⇒
    ``exhausted=False, clean_higher_high=True`` (the winner is left to run).

    Flow is an OPTIONAL corroborant: when OFI/micro are supplied and BOTH are weak
    (ofi ≤ 0 and micro < 0) the retest is flagged ``flow_weak=True`` (the caller can
    arm a partial vs a plain tighten). Absent/None flow ⇒ the structural lower-high
    retest alone marks exhaustion (fail-OPEN on flow, never required).

    Pure; fail-safe (bad/NaN inputs ⇒ ``exhausted=False``). Flag OFF ⇒ inert. This
    NEVER returns a stop — the caller derives the tighten via
    ``double_top_tighten_decision`` (ratchet-only). docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {
        "exhausted": False,
        "weak_retest": False,
        "clean_higher_high": False,
        "reason": "flag_off" if not flag_on else "wait",
        "retest_gap_atr": None,
        "flow_weak": False,
    }
    if not flag_on or not side_long:
        return out
    try:
        h = float(impulse_leg_high)
        rh = float(current_high)
        b = float(bid)
        e = float(entry_price)
    except (TypeError, ValueError):
        out["reason"] = "bad_basis"
        return out
    if not (math.isfinite(h) and math.isfinite(rh) and math.isfinite(b) and math.isfinite(e)):
        out["reason"] = "bad_basis"
        return out
    if h <= 0 or e <= 0:
        out["reason"] = "bad_basis"
        return out
    # Clean higher-high — NOT a double top; leave the winner running.
    if rh >= h * (1.0 - 1e-9):
        out["clean_higher_high"] = True
        out["reason"] = "clean_higher_high"
        return out
    # ATR-relative band: the retest must come NEAR the prior high (a real retest).
    risk_dist = e * max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.0))
    band = _double_top_atr_mult() * risk_dist if (math.isfinite(risk_dist) and risk_dist > 0) else None
    gap = h - rh  # how far the lower-high fell short of the impulse high
    out["retest_gap_atr"] = round(gap / risk_dist, 4) if (risk_dist and risk_dist > 0) else None
    if band is None or gap > band:
        out["reason"] = "retest_too_shallow"  # never approached the prior high
        return out
    # The retest must be REJECTED — the live bid has rolled back below the retest peak.
    if b >= rh * (1.0 - 1e-9):
        out["reason"] = "still_pressing"  # price still at/above the retest peak
        return out
    # Structural double-top confirmed (lower-high near the prior high, rejected).
    out["weak_retest"] = True
    out["exhausted"] = True
    out["reason"] = "double_top_weak_retest"
    # Optional flow corroborant: BOTH OFI and micro weak ⇒ strong distribution.
    try:
        o = float(ofi) if ofi is not None and math.isfinite(float(ofi)) else None
    except (TypeError, ValueError):
        o = None
    try:
        m = float(micro_edge) if micro_edge is not None and math.isfinite(float(micro_edge)) else None
    except (TypeError, ValueError):
        m = None
    out["flow_weak"] = bool(o is not None and o <= 0.0 and m is not None and m < 0.0)
    return out


def double_top_tighten_decision(
    *,
    flag_on: bool,
    impulse_leg_high: float,
    current_high: float,
    bid: float,
    entry_price: float,
    atr_pct: float,
    stop_atr_mult: float,
    current_stop: float,
    breakeven_floor: float,
    ofi: float | None = None,
    micro_edge: float | None = None,
    side_long: bool = True,
) -> dict[str, Any]:
    """Map a double-top exhaustion into a RATCHET-ONLY stop tighten (+ partial arm).

    Returns ``{"tighten": bool, "partial_arm": bool, "new_stop_floor": float,
    "reason": str, "exhausted": bool, "flow_weak": bool}``.

    On a confirmed double-top weak retest (``double_top_exhaustion_check``) the
    candidate stop is tightened toward the rejected retest peak — but only the SAME
    ATR distance the trail already uses, and ALWAYS floored at breakeven so the
    runner is de-risked, never loosened. When the retest is also flow-weak (OFI ≤ 0
    AND micro < 0) ``partial_arm=True`` so the caller can sell a fraction instead of
    only tightening. A clean higher-high ⇒ ``tighten=False`` and
    ``new_stop_floor == current_stop`` (winner runs).

    RATCHET-ONLY (never loosens): ``new_stop_floor = max(current_stop,
    breakeven_floor, candidate)``. Flag OFF / no exhaustion ⇒ pass-through. Pure for
    parity testing. docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {
        "tighten": False,
        "partial_arm": False,
        "new_stop_floor": current_stop,
        "reason": "flag_off" if not flag_on else "wait",
        "exhausted": False,
        "flow_weak": False,
    }
    if not flag_on or not side_long:
        return out
    chk = double_top_exhaustion_check(
        flag_on=flag_on,
        impulse_leg_high=impulse_leg_high,
        current_high=current_high,
        bid=bid,
        entry_price=entry_price,
        atr_pct=atr_pct,
        stop_atr_mult=stop_atr_mult,
        ofi=ofi,
        micro_edge=micro_edge,
        side_long=True,
    )
    out["reason"] = chk["reason"]
    out["exhausted"] = bool(chk["exhausted"])
    out["flow_weak"] = bool(chk["flow_weak"])
    try:
        cs = float(current_stop)
        be = float(breakeven_floor)
        e = float(entry_price)
        rh = float(current_high)
    except (TypeError, ValueError):
        return out
    if not chk["exhausted"]:
        # No double-top (incl. a clean higher-high) ⇒ no tighten (winner runs).
        return out
    # Tighten the stop to the SAME ATR distance below the rejected retest peak the
    # trail already uses (no new magic) — but never below breakeven (the runner is
    # de-risked). Ratchet-only via the max() floor.
    risk_dist = e * max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.0))
    candidate = rh - risk_dist if (math.isfinite(risk_dist) and risk_dist > 0) else cs
    floors = [c for c in (cs, be, e, candidate) if math.isfinite(c)]
    new_floor = max(floors) if floors else cs
    out["new_stop_floor"] = new_floor
    out["tighten"] = new_floor > cs + 1e-12
    out["partial_arm"] = bool(chk["flow_weak"])
    return out


def classify_stop_breach(
    *,
    ladder: Any,
    ofi_threshold: float,
    max_age_s: float = 2.5,
    min_snaps: int = 3,
) -> dict[str, Any]:
    """Classify a LOSS-side stop breach at the moment ``bid <= stop`` so the
    caller can distinguish a REAL breakdown (sell now) from a CHOP dip that a
    transient shake-out can recover from (hold one hard-bounded beat).

    This is the OPG-USD fix: OPG was stopped out at a dip VALLEY (-41bps) then
    recovered + re-armed 6 min later. The existing ``>=1s`` flicker guard catches
    a single bad PRINT but not a multi-second chop dip where bids keep absorbing.

    INVARIANT-A SAFE BY CONSTRUCTION: this function returns ONLY a classification.
    It never reads, writes, moves, or loosens the stop. The caller uses the verdict
    to delay the SELL EXECUTION (bounded) — the stop value is never touched.

    BREAKDOWN-FIRST (the safety property): every decisive-sell signal is checked
    BEFORE any hold can be granted, and missing/stale/too-few L2 → BREAKDOWN. So a
    real breakdown's loss-side latency is STRICTLY <= today's time-only confirm;
    the only behaviour change is that a *confirmed CHOP* dip earns a bounded wait.

    Verdicts:
      * ``BREAKDOWN`` — sell now. Any of: stale/missing/too-few L2; OFI decisively
        negative (``< -2T``); newest book ask-heaviest in its own window
        (``depth_imbal_pctile < 0.2``); ask side building faster than bids with a
        negative micro-price (sellers stacking — relative, no magic constant).
      * ``CHOP`` — hold one bounded beat. Confluence-AND (ALL): bids refilling
        (``bid_refill > 0``); OFI not decisively negative (``-T <= ofi <= 0.4T``);
        micro-price not rolling hard (``>= -0.5*spread`` bps, spread-relative);
        book NOT ask-heavy (``depth_imbal_pctile >= 0.5``).
      * ``INCONCLUSIVE`` — neither; caller falls back to today's >=1s path (sell).

    Pure (no I/O). Reuses the entry's tuned ``chili_momentum_ofi_threshold`` — no
    new tuning knobs; only the structural age/snaps floors are passed in.
    """
    def _f(name: str) -> float | None:
        v = getattr(ladder, name, None)
        try:
            fv = float(v)
            return fv if math.isfinite(fv) else None
        except (TypeError, ValueError):
            return None

    sig: dict[str, Any] = {}
    out = {"cls": "BREAKDOWN", "reason": "stale_or_missing_l2", "signals": sig}
    if ladder is None:
        return out
    pctile = _f("depth_imbal_pctile")
    ofi = _f("ofi")
    micro = _f("micro_edge")
    refill = _f("bid_refill")
    ask_build = _f("ask_build")
    age = _f("snapshot_age_s")
    spread = _f("spread_bps")
    try:
        n = int(getattr(ladder, "n_snaps", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    thr = abs(float(ofi_threshold or 0.0)) or 0.25
    sig.update(
        {"pctile": pctile, "ofi": ofi, "micro": micro, "refill": refill,
         "ask_build": ask_build, "age": age, "spread": spread, "n": n}
    )

    # ---- data-validity floor: never hold on bad data → BREAKDOWN ----
    if (age is None or age > float(max_age_s) or n < int(min_snaps)
            or ofi is None or micro is None or pctile is None):
        return out

    # ---- BREAKDOWN veto (evaluated FIRST; any one fires ⇒ sell now) ----
    if ofi < -2.0 * thr:
        return {"cls": "BREAKDOWN", "reason": "ofi_decisive", "signals": sig}
    if pctile < 0.2:
        return {"cls": "BREAKDOWN", "reason": "depth_ask_heaviest", "signals": sig}
    if (ask_build is not None and refill is not None
            and ask_build > max(0.0, refill) and micro < 0.0):
        # ask side stacking faster than the bid refills + price bid-favoured
        # negative = sellers building. Relative (ask vs bid growth) — no constant.
        return {"cls": "BREAKDOWN", "reason": "ask_wall_building", "signals": sig}

    # ---- CHOP (confluence-AND; only reachable when NO breakdown fired) ----
    micro_floor = (0.5 * spread) if (spread is not None and spread > 0) else 0.0
    if (refill is not None and refill > 0.0
            and -thr <= ofi <= 0.4 * thr
            and micro >= -micro_floor
            and pctile >= 0.5):
        return {"cls": "CHOP", "reason": "bids_absorbing", "signals": sig}

    return {"cls": "INCONCLUSIVE", "reason": "mixed", "signals": sig}


def _classify_cadence(
    *,
    high_water_mark: float,
    entry_price: float,
    bid: float,
    atr_pct: float,
    elapsed_minutes: float | None,
    peak_r_prior: float | None,
    ema_5m_rising: bool | None,
    rvol_accelerating: bool | None,
    slow_atr_pct_threshold: float,
    trigger_bar_minutes: float = 1.0,
) -> dict[str, Any]:
    """Classify the runner's CADENCE from signals already present at the green tick.

    PURE (no I/O). Returns one of three classes — ``SLOW_CHOPPER`` (quiet stall:
    only this class loosens the ladder), ``FAST`` (a live runner: NEVER loosened),
    or ``UNCERTAIN`` (defaults to FAST/normal — no modulation). The conservative
    TRIPLE-GATE for SLOW_CHOPPER (all three must hold): velocity LOW, trend NOT
    rising, volume NOT accelerating. A rising 5m-EMA FORCES not-slow (structure
    intact ⇒ it is still a runner). The [0.35, 0.65] velocity-uncertainty band
    falls through to UNCERTAIN ⇒ FAST/normal so we never call a borderline name slow.

    GUARD #1 (cold-start): returns UNCERTAIN — no modulation — when fewer than one
    trigger-bar has elapsed OR ``peak_r_prior`` is unset/zero, because the velocity
    score divides by a tiny ``elapsed_minutes`` early and a near-zero ``peak_r``
    denominator would spuriously flip the class right after entry.

    The returned ``velocity_score`` is the move-since-entry-per-minute as a fraction
    of the entry-ATR-per-minute; ``< 0.35·... `` (below the slow threshold band) =
    velocity-slow, ``> 0.65`` = velocity-fast, in-between = uncertain.
    """
    out: dict[str, Any] = {
        "cls": "UNCERTAIN",
        "reason": None,
        "velocity_score": None,
        "trend_rising": ema_5m_rising,
        "volume_accelerating": rvol_accelerating,
        "elapsed_minutes": elapsed_minutes,
        "peak_r_prior": peak_r_prior,
    }
    # ---- GUARD #1: classifier cold-start (NO modulation until warm) ----
    try:
        bar_min = max(0.01, float(trigger_bar_minutes or 1.0))
    except (TypeError, ValueError):
        bar_min = 1.0
    if (
        elapsed_minutes is None
        or not math.isfinite(float(elapsed_minutes))
        or float(elapsed_minutes) < bar_min
        or peak_r_prior is None
        or not math.isfinite(float(peak_r_prior))
        or float(peak_r_prior) <= 0.0
    ):
        out["reason"] = "cold_start"
        return out

    try:
        hwm = float(high_water_mark)
        entry = float(entry_price)
        b = float(bid)
        ap = float(atr_pct or 0.0)
        elapsed = float(elapsed_minutes)
        slow_thr = float(slow_atr_pct_threshold)
    except (TypeError, ValueError):
        out["reason"] = "bad_inputs"
        return out
    if not (math.isfinite(hwm) and math.isfinite(entry) and math.isfinite(b)):
        out["reason"] = "non_finite"
        return out
    if entry <= 0 or ap <= 0 or elapsed <= 0 or slow_thr <= 0:
        out["reason"] = "degenerate"
        return out

    # ---- velocity_score: realized fractional move/min vs entry-ATR fractional/min ----
    # numerator: |price excursion since entry| / entry, per minute held.
    # denominator: entry ATR% per minute (the trade's OWN expected pace).
    realized_move_frac_per_min = (abs(b - entry) / entry) / elapsed
    atr_frac_per_min = ap / bar_min
    if atr_frac_per_min <= 0:
        out["reason"] = "degenerate"
        return out
    velocity_score = realized_move_frac_per_min / atr_frac_per_min
    out["velocity_score"] = round(velocity_score, 4)

    # velocity classes vs the slow threshold band; [0.35,0.65]·-relative = uncertain.
    vel_slow = velocity_score < (slow_thr * 0.70)        # decisively below pace
    vel_fast = velocity_score > (slow_thr * 1.30)        # decisively above pace
    # trend: a RISING 5m-EMA means structure intact ⇒ NOT slow (force fast bias).
    trend_not_rising = (ema_5m_rising is False)          # None ⇒ unknown ⇒ not "not-rising"
    vol_not_accel = (rvol_accelerating is False)         # None ⇒ unknown ⇒ not "not-accel"

    # ---- EMA-rising overrides everything ⇒ NOT slow ----
    if ema_5m_rising is True:
        out["cls"] = "FAST"
        out["reason"] = "ema_rising"
        return out
    if vel_fast:
        out["cls"] = "FAST"
        out["reason"] = "velocity_fast"
        return out
    # ---- conservative TRIPLE-GATE for SLOW_CHOPPER ----
    if vel_slow and trend_not_rising and vol_not_accel:
        out["cls"] = "SLOW_CHOPPER"
        out["reason"] = "slow_triple_gate"
        return out
    # everything else (incl. the [0.35,0.65] uncertainty band) defaults to UNCERTAIN.
    out["cls"] = "UNCERTAIN"
    out["reason"] = "mixed"
    return out


def grind_mode_decision(
    *,
    enabled: bool,
    prior_active: bool,
    is_day_leader: bool | None,
    cadence_cls: str | None,
    entry_price: float,
    bid: float,
    atr_pct: float,
    stop_atr_mult: float,
    high_water_mark: float,
    ema_5m: float | None,
    last_higher_low: float | None,
    vwap: float | None = None,
) -> dict[str, Any]:
    """G4 P1 — GRIND/TREND mode classifier for the held runner (PURE, no I/O).

    The losers-eat-the-winner pattern (CLRO 07-02: two full-risk stops ate the +$285
    leg to net +$13; capture-matrix grind days: 5-6 scalps whose losers eat 40-60% of
    the winner) happens because the climax-lock exit layers ratchet the stop to a
    tight giveback near every LOCAL high of a 100-min grind. GRIND mode switches the
    runner to STRUCTURE-trailing while the grind demonstrably holds.

    ACTIVATION (prior_active False) — conservative AND of signals the lane ALREADY
    computes (fail-CLOSED: any missing/uncertain input ⇒ inactive ⇒ scalp behavior):
      * ``is_day_leader`` True — the symbol is the top-ranked fresh live_eligible name,
        scores >= the within-day p90 of the live_eligible board, or is the wildcard-
        dominant symbol (all adaptive percentile ranks; no magic number);
      * ``cadence_cls == "FAST"`` — the deployed cadence classifier confirms a live
        runner (SLOW_CHOPPER / UNCERTAIN / None never grind);
      * peak excursion >= 1R in the trade's OWN frozen risk unit — the SAME >=1R basis
        the cushion trail's 5m-EMA structure anchor uses (shared basis, no new number);
      * price HOLDING the 5m EMA-9 (bid >= ema_5m — structure intact);
      * a confirmed HIGHER-LOW above entry exists (``last_higher_low > entry_price``) —
        at least one full pullback-and-continue cycle completed. This is the grind
        signature that a fast pop-scalp (CELZ class) does NOT print on 5m bars, so
        grind mode cannot misfire on a non-grind pop;
      * price HOLDING the derived STRUCTURE FLOOR itself (``bid >= structure_floor``,
        review M2): activation must never report a floor the price has ALREADY broken
        (bid between the EMA and a higher HL-band floor previously slipped through);
      * price HOLDING VWAP when readable (``bid >= vwap``; None ⇒ check skipped —
        the caller derives it from the same cached 5m frame, zero new I/O).

    MAINTENANCE (prior_active True) — hysteresis so board flicker alone cannot drop a
    working grind: keep active while ``enabled``, cadence is NOT SLOW_CHOPPER (and is
    readable), and price holds the structure floor. The grind DIES explicitly on
    (review M2 — the switch BACK to scalp/ratchet, pending exhaustion candidates then
    apply unclamped): STRUCTURE-FLOOR BREAK (``bid < structure_floor``), a LOWER-LOW
    (the confirmed swing-low anchor degrading to/below entry — the pullback-and-
    continue signature is gone), VWAP LOSS (``bid < vwap`` when readable), cadence
    flip, or missing anchors ⇒ deactivate (fail toward scalp).

    Returns ``{"active": bool, "reason": str, "structure_floor": float|None,
    "peak_r": float|None}``. ``structure_floor`` = max(available anchors of 5m-EMA9 and
    the confirmed higher-low) minus the SAME ATR-scaled wick buffer the cushion trail's
    EMA anchor uses (entry * max(0.001, atr_pct * 0.25)) — the level GRIND clamps the
    climax-lock ratchet CANDIDATES to. The placed stop is NEVER loosened (INVARIANT-A:
    callers compose candidates through max(current_stop, ...)). docs/DESIGN/MOMENTUM_LANE.md
    """
    out: dict[str, Any] = {
        "active": False,
        "reason": "",
        "structure_floor": None,
        "peak_r": None,
    }
    if not enabled:
        out["reason"] = "flag_off"
        return out
    try:
        entry = float(entry_price)
        b = float(bid)
        hwm = float(high_water_mark)
        ap = float(atr_pct or 0.0)
        sm = float(stop_atr_mult or 0.0)
    except (TypeError, ValueError):
        out["reason"] = "bad_inputs"
        return out
    if not (math.isfinite(entry) and math.isfinite(b) and math.isfinite(hwm)) or entry <= 0:
        out["reason"] = "bad_inputs"
        return out
    # The trade's own risk unit, frozen at entry (same formula the stop/trail use).
    risk_dist = entry * max(0.003, ap * sm)
    peak_r = max(0.0, (hwm - entry) / risk_dist) if risk_dist > 0 else 0.0
    out["peak_r"] = round(peak_r, 4)
    # Structure floor from whatever anchors are available (same wick buffer as the
    # cushion trail's EMA anchor — shared basis, no new number).
    buf = entry * max(0.001, ap * 0.25)
    anchors: list[float] = []
    ema_ok = None
    try:
        if ema_5m is not None and math.isfinite(float(ema_5m)) and float(ema_5m) > 0:
            ema_ok = float(ema_5m)
            anchors.append(ema_ok)
    except (TypeError, ValueError):
        ema_ok = None
    hl_ok = None
    try:
        if last_higher_low is not None and math.isfinite(float(last_higher_low)) and float(last_higher_low) > 0:
            hl_ok = float(last_higher_low)
            anchors.append(hl_ok)
    except (TypeError, ValueError):
        hl_ok = None
    vwap_ok = None
    try:
        if vwap is not None and math.isfinite(float(vwap)) and float(vwap) > 0:
            vwap_ok = float(vwap)
    except (TypeError, ValueError):
        vwap_ok = None
    structure_floor = (max(anchors) - buf) if anchors else None

    if prior_active:
        # ── MAINTENANCE (hysteresis) — the grind DIES explicitly here (review M2):
        # floor break / lower-low / VWAP loss / cadence flip / missing anchors each
        # force the switch BACK to scalp/ratchet behavior (the pending exhaustion
        # candidates then apply unclamped). ──
        if cadence_cls is None or str(cadence_cls) == "SLOW_CHOPPER":
            out["reason"] = "cadence_dropped"
            return out
        if structure_floor is None:
            out["reason"] = "structure_anchors_missing"
            return out
        if b < structure_floor:
            out["reason"] = "structure_broken"
            return out
        # LOWER-LOW: a READABLE swing-low anchor that has degraded to/below entry means
        # the pullback-and-continue signature is gone (an unreadable None keeps the
        # EMA-anchored hysteresis — flicker alone must not drop a working grind).
        if hl_ok is not None and hl_ok <= entry:
            out["reason"] = "lower_low_below_entry"
            return out
        # VWAP LOSS when readable (None ⇒ skipped, fail-open on the missing input).
        if vwap_ok is not None and b < vwap_ok:
            out["reason"] = "vwap_lost"
            return out
        out["active"] = True
        out["reason"] = "maintained"
        out["structure_floor"] = structure_floor
        return out

    # ── ACTIVATION (all AND; fail-closed) ──
    if is_day_leader is not True:
        out["reason"] = "not_day_leader"
        return out
    if str(cadence_cls or "") != "FAST":
        out["reason"] = "cadence_not_fast"
        return out
    if peak_r < 1.0:
        out["reason"] = "below_1r"
        return out
    if ema_ok is None or b < ema_ok:
        out["reason"] = "ema_not_held"
        return out
    if hl_ok is None or hl_ok <= entry:
        out["reason"] = "no_higher_low_above_entry"
        return out
    # Review M2: activation must never report a structure floor the price has ALREADY
    # broken — with hl > ema the floor (max(anchors) - buf) can sit ABOVE a bid that
    # still holds the EMA; grind may only engage with the floor demonstrably intact.
    if structure_floor is None or b < structure_floor:
        out["reason"] = "structure_floor_not_held"
        return out
    # VWAP hold when readable (symmetric with maintenance; None ⇒ skipped).
    if vwap_ok is not None and b < vwap_ok:
        out["reason"] = "vwap_not_held"
        return out
    out["active"] = True
    out["reason"] = "activated"
    out["structure_floor"] = structure_floor
    return out


def grind_effective_max_adds(
    *,
    base_max_adds: int,
    grind_active: bool,
    cushion_r: float | None,
    min_cushion_r: float,
) -> int:
    """G4 P1 — cushion-adaptive pyramid re-add cap in GRIND mode (PURE, no I/O).

    Outside grind mode (or on any unusable basis): the documented base cap, unchanged.
    In grind mode: prefer RE-ADD over full-exit+reenter — allow one add per
    ``min_cushion_r`` of BANKED cushion (``int(cushion_r // min_cushion_r)``), floored
    at the base. Purely derived from the trade's own banked R (no new number): each
    extra add still requires the FULL pyramid confirmation set (cushion banked, stop
    >= breakeven, new-HOD, OFI thrust, iceberg probe, midday guard) AND the GUARD #4
    aggregate-risk admission — this only lifts the hard COUNT, never a risk bound."""
    try:
        base = max(0, int(base_max_adds))
    except (TypeError, ValueError):
        return 0
    if not grind_active or cushion_r is None:
        return base
    try:
        cr = float(cushion_r)
        per = float(min_cushion_r)
    except (TypeError, ValueError):
        return base
    if not (math.isfinite(cr) and cr > 0 and math.isfinite(per) and per > 0):
        return base
    return max(base, int(cr // per))


def sell_into_strength_ladder(
    *,
    high_water_mark: float,
    entry_price: float,
    bid: float,
    atr_pct: float,
    stop_atr_mult: float,
    reward_risk: float,
    current_stop: float,
    breakeven_floor: float,
    remaining_qty: float,
    ladder: Any,
    prior_partial_taken: bool = False,
    cooldown_active: bool = False,
    side_long: bool = True,
    cadence_loosen: bool = False,
) -> dict[str, Any]:
    """Ross-style PROACTIVE sell-into-strength layer (v2) on top of the v1 exhaustion
    lock. v1 only DEFENDS (tightens the stop after exhaustion, then waits for the stop
    to be hit — structural give-back from the peak, the MEGA/JASMY pattern). This sells
    a small first increment INTO the strength at the top, the way Ross reads the ladder.

    THE SAFETY IS THE MECHANISM, not a forecast: the proactive sell is a RESTING LIMIT
    at/ABOVE the bid (``limit_px = max(bid, hwm*(1-rung_bps))``), never a market dump. If
    the move actually continues (the catastrophic sell-early case the red-team flagged
    on every signal), the limit simply is NOT hit as price runs up, auto-cancels, and the
    runner is intact — an unfilled sell-into-strength limit is a FREE OPTION. It only
    fills when the market genuinely trades up into the offer = literally selling into
    strength. The caller posts it with a short TIF so an unfilled rung leaves no residue.

    Sell-early FIREWALL (the red-team's #1 risk, bounded to recoverable):
      • DISTRIBUTION confluence-AND (all three): depth-imbalance in the bottom quartile
        of its OWN recent window (a TREND, not an absolute a spoof can trip), OFI below a
        2× ENTRY threshold (exit conviction ≫ entry), micro-price decisively rolling.
      • CONTINUATION VETO (any one ⇒ HOLD): bids still refilling, OFI not decisively
        negative, or price still bid-favored. A healthy pullback fails a veto and HOLDs.
      • staleness / thinness / illiquidity / sub-deep-run ⇒ HOLD (no decision on bad data).

    INVARIANT A (ratchet-only): ``new_stop_floor = max(current_stop, breakeven, …)`` — the
    layer can only realize profit earlier and RAISE the stop; it never loosens/nulls any
    stop. ``fill_ratchet_floor`` is what the caller ratchets the remainder to ON fill.

    CADENCE-AWARE (``cadence_loosen``): when the CALLER (who owns the kill-switch flag
    + the cadence classifier) passes ``cadence_loosen=True`` for a SLOW_CHOPPER, the
    DISTRIBUTION gate is relaxed so the small first increment fires EARLIER at the
    stall (ofi softer toward ~1.33·T, micro 0.7×, dist_pctile up to 0.30) — bounded by
    explicit clamp FLOORS (ofi never weaker than 1.0·T, dist_pctile never past 0.30).
    The CONTINUATION VETO is NEVER loosened (its own pinned 2.0·T threshold). A FAST
    runner is passed ``cadence_loosen=False`` ⇒ the gate is BYTE-IDENTICAL to today.

    ADAPTIVE, single base knob ``chili_momentum_exit_ladder_rung_bps``; everything else
    derives from the plan's ``rr``, the position's ``risk_dist`` (ATR), or percentiles.
    Pure (no I/O); fail-safe → HOLD. Emits the pure-hold counterfactual for the live A/B.
    """
    out: dict[str, Any] = {
        "state": "hold",
        "action": "none",
        "limit_px": None,
        "sell_qty": 0.0,
        "new_stop_floor": current_stop,
        "fill_ratchet_floor": current_stop,
        "armed": False,
        "fired": False,
        "vetoed_by": None,
        "peak_r": None,
        "dist_pctile": None,
        "rung_bps": None,
        "first_increment_frac": 0.0,
        "counterfactual_hold_stop": current_stop,
        "reason": None,
        "cadence_loosened": bool(cadence_loosen),
    }
    if not side_long or ladder is None:
        out["reason"] = "not_long_or_no_ladder"
        return out
    try:
        hwm = float(high_water_mark)
        entry = float(entry_price)
        b = float(bid)
        cs = float(current_stop)
        be = float(breakeven_floor)
        rem = float(remaining_qty)
    except (TypeError, ValueError):
        out["reason"] = "bad_inputs"
        return out
    if not (math.isfinite(hwm) and math.isfinite(entry) and math.isfinite(b) and math.isfinite(cs)):
        out["reason"] = "non_finite"
        return out
    if entry <= 0 or hwm <= 0 or b <= 0 or rem <= 0:
        out["reason"] = "non_positive"
        return out

    # INVARIANT A floor is established up-front and NEVER lowered, whatever happens.
    base_floor = max([c for c in (cs, be) if math.isfinite(c)] or [cs])
    out["new_stop_floor"] = base_floor
    out["fill_ratchet_floor"] = base_floor

    risk_dist = entry * max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.0))
    if not (math.isfinite(risk_dist) and risk_dist > 0):
        out["reason"] = "bad_risk_dist"
        return out
    peak_r = max(0.0, (hwm - entry) / risk_dist)
    out["peak_r"] = round(peak_r, 4)
    risk_dist_bps = risk_dist / entry * 10_000.0

    # ---- counterfactual: pure-hold (cushion) floor this tick = the A/B baseline ----
    # Same shape as v1: what the band-only stop would leave with no proactive layer.
    out["counterfactual_hold_stop"] = base_floor

    # ---- derived knobs (ONE base; the rest from rr / risk_dist / percentiles) ----
    try:
        rr = float(reward_risk) if math.isfinite(float(reward_risk)) and float(reward_risk) > 0 else 2.0
    except (TypeError, ValueError):
        rr = 2.0
    try:
        thr = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
    except (TypeError, ValueError):
        thr = 0.25
    try:
        base_rung_bps = float(getattr(settings, "chili_momentum_exit_ladder_rung_bps", 60.0) or 60.0)
    except (TypeError, ValueError):
        base_rung_bps = 60.0
    base_rung_bps = max(1.0, base_rung_bps)
    try:
        arm_frac = min(max(float(getattr(settings, "chili_momentum_exit_ofi_arm_frac", 0.5) or 0.5), 0.0), 1.0)
    except (TypeError, ValueError):
        arm_frac = 0.5
    try:
        drain_s = float(getattr(settings, "chili_crypto_l2_drain_seconds", 5.0) or 5.0)
    except (TypeError, ValueError):
        drain_s = 5.0

    arm_r = max(0.5, arm_frac * rr)
    harvest_gap_r = 0.5 * max(0.0, rr - arm_r)          # only harvest a genuine runner
    ofi_exit_thr = 2.0 * thr                            # exit conviction = 2× entry
    # GUARD #4: the CONTINUATION VETO uses its OWN threshold pinned to the UNLOOSENED
    # 2.0·T and is NEVER modulated — the sell-early firewall stays exactly as strict
    # regardless of any cadence loosening below. (Decoupled from ofi_exit_thr so the
    # loosening of the DISTRIBUTION ofi can never reach into the veto arithmetic.)
    veto_ofi_thr = 2.0 * thr
    micro_roll_bps = max(3.0, 0.10 * risk_dist_bps)     # 10% of the trade's own 1R, ATR-derived
    dist_pctile_max = 0.25                              # bottom quartile of its own window

    # ---- CADENCE-AWARE LOOSENING (SLOW_CHOPPER only; flag-gated by the caller) ----
    # When the runner has gone quiet, fire the SMALL first increment EARLIER at the
    # stall: relax the DISTRIBUTION gate (ofi 1.5× softer toward 1.5·T, micro 0.7×,
    # dist_pctile up to 0.30). The CONTINUATION VETO (v1/v2/v3 below) is LEFT
    # UNTOUCHED. GUARD #3 — EXPLICIT CLAMP FLOORS: even after the compound loosening,
    # ofi_exit_thr is never weaker than 1.0·T and dist_pctile_max never exceeds 0.30,
    # so the gate can degrade only to a documented, bounded floor — never collapse.
    if cadence_loosen:
        ofi_exit_thr = (ofi_exit_thr / 1.5)             # 2.0·T -> ~1.33·T (softer exit conviction)
        micro_roll_bps = 0.7 * micro_roll_bps           # price needs to roll 0.7× as far
        dist_pctile_max = min(0.30, dist_pctile_max + 0.05)
        # GUARD #3: hard floors — clamp AFTER the compound, never past them.
        ofi_exit_thr = max(ofi_exit_thr, 1.0 * thr)
        dist_pctile_max = min(dist_pctile_max, 0.30)
        micro_roll_bps = max(3.0, micro_roll_bps)       # keep the absolute micro floor

    stale_max_s = max(6.0, 2.0 * drain_s)
    spread_cap_bps = 3.0 * risk_dist_bps                # illiquid relative to the trade's risk unit
    refill_floor = 0.0

    # ---- ladder reads (fail-safe) ----
    def _f(name: str) -> float | None:
        v = getattr(ladder, name, None)
        try:
            return float(v) if (v is not None and math.isfinite(float(v))) else None
        except (TypeError, ValueError):
            return None
    pctile = _f("depth_imbal_pctile")
    o = _f("ofi")
    m = _f("micro_edge")
    refill = _f("bid_refill")
    spread = _f("spread_bps")
    age = _f("snapshot_age_s")
    try:
        n_snaps = int(getattr(ladder, "n_snaps", 0) or 0)
    except (TypeError, ValueError):
        n_snaps = 0
    out["dist_pctile"] = round(pctile, 4) if pctile is not None else None

    # ---- GATES (any failure ⇒ HOLD; no decision on bad/insufficient data) ----
    if peak_r < arm_r:
        out["reason"] = "below_profit_arm"
        return out
    out["armed"] = True
    if peak_r < arm_r + harvest_gap_r:
        out["reason"] = "not_deep_run"       # defensive-only territory (v1 handles)
        return out
    if cooldown_active:
        out["reason"] = "cooldown"
        return out
    if age is None or age > stale_max_s or n_snaps < 3:
        out["reason"] = "stale_or_thin"
        return out
    if spread is not None and spread > spread_cap_bps:
        out["reason"] = "illiquid"
        return out
    # required distribution signals must be present (None ⇒ HOLD)
    if pctile is None or o is None or m is None:
        out["reason"] = "missing_signal"
        return out

    # ---- DISTRIBUTION confluence-AND ----
    d1 = pctile <= dist_pctile_max          # now ask-heavy vs its own recent window
    d2 = o < -ofi_exit_thr                  # order flow decisively rolled over
    d3 = m < -micro_roll_bps                # price decisively settling toward the bid
    # ---- CONTINUATION VETO (any TRUE ⇒ HOLD; the sell-early firewall) ----
    # GUARD #4: pinned to veto_ofi_thr (== UNLOOSENED 2.0·T), NOT ofi_exit_thr — the
    # veto is byte-identical whether or not the distribution gate was loosened.
    v1 = refill is not None and refill > refill_floor   # buyers still stacking the bid
    v2 = o > -veto_ofi_thr / 2.0                          # flow not decisively negative
    v3 = m >= 0.0                                         # price still bid-favored
    if v1 or v2 or v3:
        out["vetoed_by"] = "bid_refill" if v1 else ("ofi_weak" if v2 else "micro_nonneg")
        out["reason"] = "continuation_veto"
        return out
    if not (d1 and d2 and d3):
        out["reason"] = "no_distribution"
        return out

    # ---- SELL INTO STRENGTH: one small resting limit at/above the bid ----
    # rung widens on a stronger run (let winners run): base · (1 + 0.3·(peak_r−arm_r)/rr).
    rung_bps = base_rung_bps * (1.0 + 0.3 * max(0.0, peak_r - arm_r) / rr)
    out["rung_bps"] = round(rung_bps, 2)
    limit_px = max(b, hwm * (1.0 - rung_bps / 10_000.0))   # never below market = never a hidden dump
    # first increment SMALL: 10% of remaining at the arm, up to 25% near target.
    inc_frac = min(max(0.10 * (peak_r / rr), 0.10), 0.25)
    out["first_increment_frac"] = round(inc_frac, 4)
    sell_qty = max(0.0, inc_frac * rem)
    if sell_qty <= 0 or not math.isfinite(limit_px) or limit_px <= 0:
        out["reason"] = "degenerate_order"
        return out

    out["state"] = "sell_into_strength"
    out["action"] = "sell_limit"
    out["fired"] = True
    out["limit_px"] = limit_px
    out["sell_qty"] = sell_qty
    # INVARIANT A: on FILL the remainder ratchets to the fill floor; pre-fill the stop
    # is unchanged (max(cs, be)). Never below the structural floor, ever.
    out["fill_ratchet_floor"] = max(base_floor, limit_px)
    out["reason"] = "distribution_confluence"
    return out


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
