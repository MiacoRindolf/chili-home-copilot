"""Ross-Cameron-style momentum-quality scoring for the Momentum Lane (M2).

Ranks EXPLOSIVE instruments (high relative volume + already-moving + low-float/
small-cap) above generic ones — the *selection* edge a momentum day-trader
relies on. Consumes the screen-able Ross pillars that
``scanner._score_ticker_intraday`` already computes (``vol_ratio`` /
``gap_pct`` / ``daily_change_pct``) but that the scanner→viability bridge
historically discarded (see ``docs/DESIGN/MOMENTUM_LANE.md`` §7).

Design constraints (operator: "no magic numbers, use adaptive logic"):
  * **Adaptive, not hardcoded.** Each pillar is PERCENTILE-RANKED within the
    current universe batch, so there is no fixed ``RVOL >= 5x`` cutoff — the bar
    floats with whatever is actually moving right now. Ross's literal ``5x`` was
    calibrated to the small-cap equity tape; percentile ranking generalises it
    to any universe (incl. 24/7 crypto) without a tuned constant.
  * **Crypto-aware.** Works on Coinbase 24/7 crypto (``-USD``; "float" → market
    cap, and the volume surge itself is the catalyst proxy) and on equities.
  * **Transparent.** Every score carries a per-pillar breakdown for audit.
  * **Pure functions.** No DB, no IO — trivially testable in isolation.

The only fixed constants are the pillar WEIGHTS, which encode Ross Cameron's
own stated priority (relative volume is his #1 filter, then "already moving",
then float/liquidity). They are documented as such, not arbitrary tuning, and
can move to settings later.
"""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass, field

from .volume_pace import trusted_rvol_value

logger = logging.getLogger(__name__)

# Ross's stated 5-pillar priority, normalised to the screen-able structural
# pillars. RVOL is his explicit #1 ("at a minimum I need a relative volume of
# 5"); "already up on the day" is #2 ("never buy what isn't already moving");
# low float / small-cap is the supply side. Sum to 1.0.
ROSS_PILLAR_WEIGHTS: dict[str, float] = {
    "rvol": 0.45,
    "momentum": 0.35,
    "liquidity": 0.20,
}

ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED: dict[str, float] = {
    "rvol": 0.35,
    "momentum": 0.25,
    "liquidity": 0.40,
}

ROSS_ELIGIBILITY_RVOL_FLOOR = 5.0
ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT = 10.0

# Max tilt the Ross momentum quality applies to a momentum-neural viability
# score. ``ross_score`` in [0,1] is centered at 0.5, so the applied tilt is
# +/- (TILT/2): at 0.20 a top-decile explosive setup gets +0.10 — enough to
# clear the live-eligibility floor a generic setup would miss — and a dull one
# -0.10. Same order of magnitude as the existing hand-coded viability nudges.
ROSS_QUALITY_VIABILITY_TILT = 0.20


@dataclass
class RossMomentumScore:
    """Adaptive Ross momentum-quality result for one instrument in a batch."""

    symbol: str
    score: float  # [0,1] blended quality (weighted percentile across pillars)
    rvol_pct: float  # percentile rank of relative volume within the universe
    momentum_pct: float  # percentile rank of daily-change/gap within the universe
    liquidity_pct: float | None  # percentile rank of supply tier (None if absent)
    rank: int  # 1 = most explosive in this universe
    universe_size: int
    breakdown: dict = field(default_factory=dict)

    def in_top_fraction(self, frac: float) -> bool:
        """True if this instrument is in the top ``frac`` of the universe by
        rank (adaptive — the caller, not this module, owns the cutoff)."""
        if self.universe_size <= 0 or frac <= 0.0:
            return False
        return self.rank <= max(1, round(self.universe_size * frac))


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_is_ssr(last: float | None, prior_close: float | None) -> bool:
    """True when the last price is down at least 10% versus prior close."""
    last_f = _to_float(last)
    prior_f = _to_float(prior_close)
    if last_f is None or prior_f is None or prior_f <= 0:
        return False
    return last_f <= prior_f * (1.0 - ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT / 100.0)


def _clamp01(v: float | None) -> float | None:
    if v is None:
        return None
    return max(0.0, min(1.0, float(v)))


def squeeze_entry_size_multiplier(
    squeeze_rank_pct: float | None,
    *,
    ofi: float | None = None,
    news_agrees: bool = False,
    top_pctl: float = 0.80,
    max_mult: float = 1.50,
) -> tuple[float, dict]:
    """Bounded squeeze size-up. Missing confirmation returns neutral 1.0."""
    rank = _clamp01(_to_float(squeeze_rank_pct))
    top = _clamp01(_to_float(top_pctl)) or 1.0
    max_m = max(1.0, _to_float(max_mult) or 1.0)
    meta = {
        "squeeze_rank_pct": rank,
        "top_pctl": top,
        "ofi": ofi,
        "news_agrees": bool(news_agrees),
        "max_mult": max_m,
    }
    if rank is None or rank < top or ofi is None or float(ofi) <= 0.0 or not news_agrees:
        meta["reason"] = "squeeze_size_neutral"
        return 1.0, meta
    strength = (rank - top) / max(1e-9, 1.0 - top)
    mult = 1.0 + (max_m - 1.0) * max(0.0, min(1.0, strength))
    meta.update({"reason": "squeeze_size_up", "multiplier": round(mult, 4)})
    return mult, meta


def squeeze_exit_band_widen(
    squeeze_rank_pct: float | None,
    *,
    tail_pctl: float = 0.90,
    max_widen: float = 1.50,
) -> tuple[float, dict]:
    """Bounded hold-band widen for extreme squeeze-tail runners."""
    rank = _clamp01(_to_float(squeeze_rank_pct))
    tail = _clamp01(_to_float(tail_pctl)) or 1.0
    max_w = max(1.0, _to_float(max_widen) or 1.0)
    meta = {"squeeze_rank_pct": rank, "tail_pctl": tail, "max_widen": max_w}
    if rank is None or rank < tail:
        meta["reason"] = "squeeze_exit_neutral"
        return 1.0, meta
    strength = (rank - tail) / max(1e-9, 1.0 - tail)
    factor = 1.0 + (max_w - 1.0) * max(0.0, min(1.0, strength))
    meta.update({"reason": "squeeze_exit_widen", "factor": round(factor, 4)})
    return factor, meta


@dataclass
class SqueezeFuelSignal:
    squeeze_pct: float | None
    components: dict = field(default_factory=dict)


def squeeze_fuel_signal(
    short_interest_pct: float | None = None,
    cost_to_borrow: float | None = None,
    *,
    utilization: float | None = None,
    is_easy_to_borrow: bool | None = None,
) -> SqueezeFuelSignal:
    """Borrow-pressure proxy from present short-mechanics legs."""
    vals: list[float] = []
    si = _to_float(short_interest_pct)
    ctb = _to_float(cost_to_borrow)
    util = _to_float(utilization)
    if si is not None:
        vals.append(_clamp01(si / 100.0) or 0.0)
    if ctb is not None:
        vals.append(_clamp01(ctb / 100.0) or 0.0)
    if util is not None:
        vals.append(_clamp01(util / 100.0) or 0.0)
    if is_easy_to_borrow is False:
        vals.append(1.0)
    elif is_easy_to_borrow is True:
        vals.append(0.0)
    pct = (sum(vals) / len(vals)) if vals else None
    return SqueezeFuelSignal(
        squeeze_pct=pct,
        components={
            "short_interest_pct": si,
            "cost_to_borrow": ctb,
            "utilization": util,
            "is_easy_to_borrow": is_easy_to_borrow,
            "legs": len(vals),
        },
    )


def _kaufman_efficiency_ratio(closes: list[float] | None) -> float | None:
    try:
        vals = [float(x) for x in (closes or []) if x is not None]
        if len(vals) < 3:
            return None
        change = abs(vals[-1] - vals[0])
        noise = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))
        if noise <= 0:
            return None
        return max(0.0, min(1.0, change / noise))
    except Exception:
        return None


def front_side_strength_score(
    *,
    closes: list[float] | None = None,
    vwap_dist_sigma: float | None = None,
    day_range_pos: float | None = None,
    ofi_level: float | None = None,
    ofi_slope: float | None = None,
    signed_tape: float | None = None,
) -> float | None:
    """Continuous front-side score in [0,1], using only present signal legs."""
    terms: list[float] = []
    er = _kaufman_efficiency_ratio(closes)
    if er is not None:
        terms.append(er)
    vwap = _to_float(vwap_dist_sigma)
    if vwap is not None:
        terms.append(_clamp01((vwap + 1.0) / 2.0) or 0.0)
    rng = _to_float(day_range_pos)
    if rng is not None:
        terms.append(_clamp01(rng) or 0.0)
    ofi = _to_float(ofi_level)
    if ofi is not None:
        terms.append(0.5 + 0.5 * math.tanh(ofi))
    slope = _to_float(ofi_slope)
    if slope is not None:
        terms.append(0.5 + 0.5 * math.tanh(slope))
    tape = _to_float(signed_tape)
    if tape is not None:
        terms.append(0.5 + 0.5 * math.tanh(tape))
    if not terms:
        return None
    return max(0.0, min(1.0, sum(terms) / len(terms)))


def front_side_size_tilt(
    strength: float | None,
    *,
    size_floor: float = 0.50,
    s_lo: float = 0.25,
    s_hi: float = 0.75,
    defer_below: float | None = None,
    stale_tape: bool = False,
) -> tuple[float, dict]:
    """Size-down-only front-side tilt; missing/stale inputs are neutral."""
    floor_raw = _to_float(size_floor)
    floor = max(0.0, min(1.0, floor_raw if floor_raw is not None else 0.5))
    lo = _to_float(s_lo)
    hi = _to_float(s_hi)
    score = _clamp01(_to_float(strength))
    meta = {
        "strength": score,
        "size_floor": floor,
        "s_lo": lo,
        "s_hi": hi,
        "stale_tape": bool(stale_tape),
    }
    if score is None or stale_tape or lo is None or hi is None or hi <= lo:
        meta["reason"] = "frontside_neutral"
        return 1.0, meta
    if score <= lo:
        mult = floor
    elif score >= hi:
        mult = 1.0
    else:
        mult = floor + (1.0 - floor) * ((score - lo) / (hi - lo))
    meta.update({
        "reason": "frontside_size_tilt",
        "multiplier": round(mult, 4),
        "defer": defer_below is not None and score < float(defer_below),
    })
    return mult, meta


def _percentile_rank(value: float, sorted_vals: list[float]) -> float:
    """Fraction of the universe at-or-below ``value``, in [0,1]. The adaptive
    bar — an instrument is "high RVOL" relative to the batch, not an absolute."""
    n = len(sorted_vals)
    if n == 0:
        return 0.5
    return bisect.bisect_right(sorted_vals, value) / n


def _first_float(signal: dict, *keys) -> float | None:
    """First present, parseable value among ``keys``. Schema-tolerant: the
    intraday scanner emits ``vol_ratio``/``daily_change_pct`` while the
    crypto-breakout cache emits ``rvol``/``change_24h`` for the SAME Ross
    pillars — read both so the scorer works on either result source."""
    for k in keys:
        v = _to_float(signal.get(k))
        if v is not None:
            return v
    return None


def _trusted_rvol_from_signal(signal: dict) -> float | None:
    """Trusted Ross RVOL pillar from a scanner/ignition signal.

    ``rvol_pace`` is the preferred semantic: actual cumulative volume so far /
    expected cumulative volume at this time. Raw cumulative day/ADV participation
    is audit evidence only; with that basis it returns ``None`` so the scorer
    treats RVOL as incomplete rather than as low RVOL.
    """
    basis = signal.get("rvol_basis") or signal.get("volume_basis")
    source = signal.get("rvol_source") or signal.get("volume_source") or signal.get("source")

    pace = _first_float(signal, "rvol_pace", "volume_pace", "time_normalized_rvol")
    if pace is not None:
        return trusted_rvol_value(
            pace,
            basis="actual_cum_over_expected_cum",
            source=source or "rvol_pace",
            fallback_legacy=False,
        )

    for key in ("vol_ratio", "rvol", "volume_ratio"):
        raw = _to_float(signal.get(key))
        if raw is None:
            continue
        return trusted_rvol_value(raw, basis=basis, source=source)
    return None


def _extract_pillars(signal: dict) -> tuple[float | None, float | None, float | None]:
    """(rvol, momentum, liquidity) raw pillar values from a scanner/breakout
    result dict (reads the equivalent key from either schema).

    * rvol      — trusted relative volume pace (``rvol_pace`` preferred; legacy
                  ``vol_ratio`` | ``rvol`` | ``volume_ratio`` accepted unless
                  metadata marks it as raw cumulative day/ADV participation).
    * momentum  — signed "already moving" %, the stronger of daily/24h change
                  (``daily_change_pct`` | ``change_24h`` | ``change_pct``) and the
                  gap (``gap_pct``). Long bias: a down-mover ranks low; a
                  high-volume *dump* (high rvol, negative momentum) correctly does
                  not top-rank.
    * liquidity — explosiveness of supply: SMALLER float / market-cap → MORE
                  explosive, so we return ``-log10(size)``. ``None`` when the size
                  field is unavailable (common for the crypto result sources).
    """
    rvol = _trusted_rvol_from_signal(signal)

    cands = [
        x
        for x in (
            _first_float(signal, "daily_change_pct", "change_24h", "change_pct"),
            _first_float(signal, "gap_pct"),
        )
        if x is not None
    ]
    momentum = max(cands) if cands else None

    size = _first_float(signal, "float_shares", "market_cap")
    liquidity = -math.log10(size) if (size and size > 0) else None

    return rvol, momentum, liquidity


def score_universe(
    signals: dict[str, dict],
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, RossMomentumScore]:
    """Rank a batch of instruments by Ross momentum quality, adaptively.

    ``signals``: ``{symbol: scanner_result_dict}`` (the dicts
    ``scanner._score_ticker_intraday`` returns — carrying ``vol_ratio``,
    ``gap_pct``, ``daily_change_pct``, optionally ``float_shares``/``market_cap``).

    Returns ``{symbol: RossMomentumScore}`` with ``rank`` assigned (1 = most
    explosive). Each pillar is percentile-ranked within this batch and blended
    with Ross-priority weights, renormalised over whichever pillars are present
    (so a missing liquidity field degrades gracefully to rvol+momentum rather
    than zeroing the score).
    """
    if not signals:
        return {}
    w = dict(weights or ROSS_PILLAR_WEIGHTS)

    pillars: dict[str, tuple[float | None, float | None, float | None]] = {
        sym: _extract_pillars(sig or {}) for sym, sig in signals.items()
    }

    rvol_sorted = sorted(p[0] for p in pillars.values() if p[0] is not None)
    mom_sorted = sorted(p[1] for p in pillars.values() if p[1] is not None)
    liq_sorted = sorted(p[2] for p in pillars.values() if p[2] is not None)

    out: dict[str, RossMomentumScore] = {}
    for sym, (rvol, mom, liq) in pillars.items():
        sig = signals.get(sym) or {}
        rvol_pct = _percentile_rank(rvol, rvol_sorted) if rvol is not None else None
        mom_pct = _percentile_rank(mom, mom_sorted) if mom is not None else None
        liq_pct = _percentile_rank(liq, liq_sorted) if liq is not None else None

        present: list[tuple[float, float]] = []  # (percentile, weight)
        if rvol_pct is not None:
            present.append((rvol_pct, w["rvol"]))
        if mom_pct is not None:
            present.append((mom_pct, w["momentum"]))
        if liq_pct is not None:
            present.append((liq_pct, w["liquidity"]))

        wsum = sum(wt for _, wt in present)
        score = (sum(pct * wt for pct, wt in present) / wsum) if wsum > 0 else 0.0

        out[sym] = RossMomentumScore(
            symbol=sym,
            score=round(score, 4),
            rvol_pct=round(rvol_pct, 4) if rvol_pct is not None else 0.0,
            momentum_pct=round(mom_pct, 4) if mom_pct is not None else 0.0,
            liquidity_pct=round(liq_pct, 4) if liq_pct is not None else None,
            rank=0,
            universe_size=len(signals),
            breakdown={
                "rvol": rvol,
                "momentum": mom,
                "liquidity_neglog_size": liq,
                "rvol_source": sig.get("rvol_source") or sig.get("volume_source"),
                "rvol_basis": sig.get("rvol_basis") or sig.get("volume_basis"),
                "pillars_present": [
                    name
                    for name, val in (("rvol", rvol_pct), ("momentum", mom_pct), ("liquidity", liq_pct))
                    if val is not None
                ],
            },
        )

    # rank: highest blended score first; ties broken by rvol then momentum
    ordered = sorted(
        out.values(), key=lambda s: (s.score, s.rvol_pct, s.momentum_pct), reverse=True
    )
    for i, s in enumerate(ordered, start=1):
        s.rank = i
    return out


# ── Selection→entry alignment: intraday-impulse freshness (M4 keystone) ───────
# ``score_universe`` ranks the day's movers from 24h-CUMULATIVE pillars (RVOL /
# gap / daily-change). But by the time the pullback-break entry gate evaluates a
# top-ranked name, many have already FADED into a deep intraday retracement, so
# the gate reads ``pullback_too_deep`` and never fires (live diagnostic 2026-06-07:
# all 10 live-eligible candidates ``pullback_too_deep``; lane = 0 entries). Ross
# instead enters names moving RIGHT NOW — near their high-of-day with a shallow
# pullback available. This measures exactly that, from the SAME intraday bars the
# entry gate uses, so faded 24h movers can be dropped from the live-eligible set
# and the survivors RE-RANKED by current impulse (the freshest watched first).
# docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md §3 ME-4.


@dataclass
class ImpulseFreshness:
    """Where the current price sits within the recent intraday range — the
    structural precondition for a SHALLOW pullback to be available to enter on."""

    is_fresh: bool
    score: float  # clamped [0,1]: position of current price in the recent range
    position_in_range: float  # raw (can exceed 1.0 on a fresh new high) — ranking key
    reason: str
    win_high: float | None = None
    win_low: float | None = None
    last: float | None = None
    debug: dict = field(default_factory=dict)


def _df_col_floats(df, name: str) -> list[float]:
    """Column as a plain ``list[float]`` (keeps this module pandas-import-free and
    pure — it operates on whatever OHLCV frame the caller already fetched)."""
    try:
        return [float(x) for x in df[name].tolist()]
    except Exception:
        return []


def intraday_impulse_freshness(
    df,
    *,
    lookback: int = 20,
    retracement_threshold: float = 0.50,
) -> ImpulseFreshness:
    """Is this instrument in a FRESH intraday up-impulse near its recent high
    (so a shallow pullback can still form and break), or has it already faded?

    Computed from the SAME bars + window the pullback-break entry gate evaluates
    (``entry_gates.pullback_break_confirmation``: ``look = min(20, cur)`` over the
    bars BEFORE the current one):

      * ``position_in_range = (last_close - win_low) / (win_high - win_low)`` — 1.0
        at the recent high (freshest), >1.0 on a fresh new high, ~0 at the low
        (most faded). Used to RANK candidates so the freshest is watched first.
      * ``is_fresh = score >= (1 - retracement_threshold)`` — the current price has
        NOT retraced more than the gate's own shallow/deep boundary below the recent
        high. This REUSES the entry gate's one documented knob (``retracement_threshold``)
        rather than inventing a new "near-high" cutoff, so the freshness filter and
        the gate share a single, self-consistent definition of "shallow". A faded
        24h mover (price rolled into the lower portion of its range) fails it.

    Adaptive by construction: the bar floats with each name's own intraday range —
    no fixed % or hardcoded RVOL. Pure + side-effect-free for unit testing.
    """
    thr = float(retracement_threshold)
    if df is None or getattr(df, "empty", True):
        return ImpulseFreshness(False, 0.0, 0.0, "insufficient_bars", debug={"bars": 0})
    n = len(df)
    if n < 5:
        return ImpulseFreshness(False, 0.0, 0.0, "insufficient_bars", debug={"bars": n})
    highs = _df_col_floats(df, "High")
    lows = _df_col_floats(df, "Low")
    closes = _df_col_floats(df, "Close")
    if len(highs) != n or len(lows) != n or len(closes) != n:
        return ImpulseFreshness(False, 0.0, 0.0, "bad_ohlcv", debug={"bars": n})
    cur = n - 1
    look = min(int(lookback), cur)
    if look < 2:
        return ImpulseFreshness(False, 0.0, 0.0, "insufficient_bars", debug={"bars": n})
    win_high = max(highs[cur - look:cur])  # excludes the current bar (gate parity)
    win_low = min(lows[cur - look:cur])
    rng = win_high - win_low
    last = closes[cur]
    if not (rng > 0):
        # Flat / no intraday impulse to pull back from — not a Ross setup.
        return ImpulseFreshness(
            False, 0.0, 0.0, "no_range",
            win_high=win_high, win_low=win_low, last=last,
            debug={"win_high": win_high, "win_low": win_low},
        )
    pos = (last - win_low) / rng
    score = min(1.0, max(0.0, pos))
    is_fresh = score >= (1.0 - thr)
    return ImpulseFreshness(
        is_fresh=bool(is_fresh),
        score=round(score, 4),
        position_in_range=round(pos, 4),
        reason="fresh_impulse" if is_fresh else "faded_below_high",
        win_high=win_high,
        win_low=win_low,
        last=last,
        debug={
            "win_high": round(win_high, 8),
            "win_low": round(win_low, 8),
            "last": round(last, 8),
            "retrace_from_high": round(max(0.0, (win_high - last) / rng), 4),
            "threshold": thr,
            "lookback": int(look),
        },
    )


@dataclass
class FrontSideState:
    """CONVERSION FIX D — front-side vs backside read for a candidate at entry.

    "Backside" = price working LOWER off the day's high: below session VWAP AND in
    the lower portion of the day's range, with no reclaim momentum. Ross trades the
    FRONT side (making new highs); the backside is where momentum bots get chopped
    buying a falling knife. BUT a VWAP-reclaim-FROM-BELOW carried by momentum (price
    has pushed back above VWAP and is rising) is the FRONT-side reclaim entry — it
    must NOT be benched. Pure + side-effect-free."""

    is_backside: bool
    reason: str
    above_vwap: bool | None = None
    day_range_pos: float | None = None  # 0=day low, 1=day high
    vwap_dist_sigma: float | None = None  # signed (last-vwap)/range, informational
    front_side_score: float = 0.0  # higher = more front-side
    reclaim_momentum: bool | None = None
    debug: dict = field(default_factory=dict)


def front_side_state(
    df,
    *,
    day_range_pos_floor: float = 0.34,
    reclaim_lookback: int = 3,
) -> FrontSideState:
    """Classify a candidate as front-side (tradable) or backside (bench).

    Benches a GENUINE fade — below VWAP, in the lower ``day_range_pos_floor`` of the
    day's range, and NOT reclaiming (price not back above VWAP with rising closes).
    Un-benches a VWAP-reclaim-from-below: ``above_vwap`` is back True (or the last
    ``reclaim_lookback`` closes are rising back through VWAP) — the Ross reclaim
    entry. Fails OPEN (front-side) on thin/missing data so it never blocks on cold
    candles. Pure; operates on whatever OHLCV frame the caller fetched."""
    if df is None or getattr(df, "empty", True):
        return FrontSideState(False, "insufficient_bars", debug={"bars": 0})
    n = len(df)
    if n < 5:
        return FrontSideState(False, "insufficient_bars", debug={"bars": n})
    highs = _df_col_floats(df, "High")
    lows = _df_col_floats(df, "Low")
    closes = _df_col_floats(df, "Close")
    vols = _df_col_floats(df, "Volume")
    if not (len(highs) == len(lows) == len(closes) == n) or len(vols) != n:
        return FrontSideState(False, "bad_ohlcv", debug={"bars": n})

    day_high = max(highs)
    day_low = min(lows)
    rng = day_high - day_low
    last = closes[-1]
    if not (rng > 0):
        return FrontSideState(False, "no_range", debug={"day_high": day_high, "day_low": day_low})
    day_range_pos = (last - day_low) / rng

    # Session VWAP (typical-price * volume, cumulative). Fail open if no volume.
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_pv += tp * vols[i]
        cum_v += vols[i]
    if cum_v <= 0:
        return FrontSideState(False, "no_volume_for_vwap", day_range_pos=round(day_range_pos, 4))
    vwap = cum_pv / cum_v
    above_vwap = last >= vwap
    vwap_dist_sigma = (last - vwap) / rng

    # Reclaim momentum: the last ``reclaim_lookback`` closes are rising (a push back
    # UP), i.e. coming off the lows rather than rolling over.
    lb = min(int(reclaim_lookback), n - 1)
    recent = closes[-lb - 1:]
    rising = all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1)) and recent[-1] > recent[0]
    reclaim_momentum = bool(above_vwap and rising)

    # Front-side score: above VWAP and high in the day's range => front side.
    front_side_score = round(
        0.5 * (1.0 if above_vwap else 0.0) + 0.5 * min(1.0, max(0.0, day_range_pos)), 4
    )

    debug = {
        "vwap": round(vwap, 6),
        "last": round(last, 6),
        "day_high": round(day_high, 6),
        "day_low": round(day_low, 6),
        "rising_closes": rising,
    }

    # Genuine fade => bench. Below VWAP AND lower portion of range AND not reclaiming.
    if (not above_vwap) and day_range_pos <= float(day_range_pos_floor) and not reclaim_momentum:
        return FrontSideState(
            is_backside=True,
            reason="backside_fade",
            above_vwap=above_vwap,
            day_range_pos=round(day_range_pos, 4),
            vwap_dist_sigma=round(vwap_dist_sigma, 4),
            front_side_score=front_side_score,
            reclaim_momentum=reclaim_momentum,
            debug=debug,
        )

    reason = "vwap_reclaim_from_below" if reclaim_momentum and day_range_pos <= 0.6 else "front_side"
    return FrontSideState(
        is_backside=False,
        reason=reason,
        above_vwap=above_vwap,
        day_range_pos=round(day_range_pos, 4),
        vwap_dist_sigma=round(vwap_dist_sigma, 4),
        front_side_score=front_side_score,
        reclaim_momentum=reclaim_momentum,
        debug=debug,
    )
