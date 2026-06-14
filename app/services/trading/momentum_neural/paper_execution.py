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


def stop_target_prices(
    entry: float,
    *,
    atr_pct: float,
    side_long: bool = True,
    stop_atr_mult: float = 0.60,
    target_atr_mult: float = 0.90,  # legacy; superseded by reward_risk below
    reward_risk: float | None = None,
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
        target = entry + rr * (entry - stop)  # reward = rr x risk(stop distance)
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


def scale_out_fraction(default: float = 0.5, symbol: str | None = None) -> float:
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

    if not (confluence or absorption):
        return out

    out["fired"] = True
    out["trigger"] = "absorption" if (absorption and not confluence) else "ofi_micro_confluence"

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
    micro_roll_bps = max(3.0, 0.10 * risk_dist_bps)     # 10% of the trade's own 1R, ATR-derived
    dist_pctile_max = 0.25                              # bottom quartile of its own window
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
    v1 = refill is not None and refill > refill_floor   # buyers still stacking the bid
    v2 = o > -ofi_exit_thr / 2.0                          # flow not decisively negative
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
