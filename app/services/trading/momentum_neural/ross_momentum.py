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

# Liquidity-BIASED variant (opt-in via `weights=`): adds a fourth pillar that rewards
# TRADEABLE liquidity (dollar turnover) so the lane prefers movers it can ACTUALLY
# fill, not just the most explosive (smallest-float) names whose wide BBO spread gets
# spread-gated and only ever watched. The "liquidity" pillar above rewards explosive
# SUPPLY (small float); "tradeable_liquidity" rewards FILLABILITY (high $-volume ->
# tighter spread) — opposite axes, deliberately balanced. Weights validated by the
# previous-days replay (scripts/_sim_liquidity_selection.py) before becoming default.
ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED: dict[str, float] = {
    "rvol": 0.40,
    "momentum": 0.30,
    "liquidity": 0.15,
    "tradeable_liquidity": 0.15,
}

# Daily-context variant (opt-in via `weights=` when chili_momentum_daily_context_enabled):
# adds a FIFTH pillar `daily_structure` — the daily-chart S&R level-awareness from
# daily_levels.compute_daily_context (break ABOVE a major daily level + room to the next
# level + a SOFT broader-trend minority input). 10% weight → it RE-RANKS the candidate
# pool toward clean daily breakouts, it can never block a fill (the entry gate is
# untouched). A news-gap spike breaking a level scores HIGH (the CUPR guarantee), so it
# is never demoted. Percentile-ranked like every other pillar (raw range normalised away).
ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT: dict[str, float] = {
    "rvol": 0.40,
    "momentum": 0.30,
    "liquidity": 0.10,
    "tradeable_liquidity": 0.10,
    "daily_structure": 0.10,
}

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
    tradeable_liquidity_pct: float | None = None  # percentile of $-turnover (fillability); None unless the biased weights are used
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


def _extract_pillars(
    signal: dict,
) -> tuple[float | None, float | None, float | None, float | None]:
    """(rvol, momentum, liquidity, tradeable_liquidity) raw pillar values from a
    scanner/breakout result dict (reads the equivalent key from either schema).

    * rvol      — relative volume (``vol_ratio`` | ``rvol`` | ``volume_ratio``).
    * momentum  — signed "already moving" %, the stronger of daily/24h change
                  (``daily_change_pct`` | ``change_24h`` | ``change_pct``) and the
                  gap (``gap_pct``). Long bias: a down-mover ranks low; a
                  high-volume *dump* (high rvol, negative momentum) correctly does
                  not top-rank.
    * liquidity — explosiveness of supply: SMALLER float / market-cap → MORE
                  explosive, so we return ``-log10(size)``. ``None`` when the size
                  field is unavailable (common for the crypto result sources).
    """
    rvol = _first_float(signal, "vol_ratio", "rvol", "volume_ratio")

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

    # tradeable_liquidity — dollar turnover today (price * shares, or an explicit
    # $-volume / quote-volume field). HIGHER turnover -> tighter BBO spread -> more
    # FILLABLE. Distinct from (and opposite to) the small-float ``liquidity`` pillar.
    # log10-scaled (turnover spans many orders of magnitude). None when unavailable.
    dvol = _first_float(
        signal, "dollar_volume", "dollar_vol", "turnover",
        "approximate_quote_24h_volume", "quote_volume_24h", "quote_volume",
    )
    if dvol is None:
        _price = _first_float(signal, "price", "last", "close", "last_price")
        _vol = _first_float(signal, "volume", "day_volume", "shares", "today_volume")
        dvol = _price * _vol if (_price and _vol) else None
    tradeable_liquidity = math.log10(dvol) if (dvol and dvol > 0) else None

    return rvol, momentum, liquidity, tradeable_liquidity


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

    pillars: dict[str, tuple[float | None, float | None, float | None, float | None]] = {
        sym: _extract_pillars(sig or {}) for sym, sig in signals.items()
    }

    rvol_sorted = sorted(p[0] for p in pillars.values() if p[0] is not None)
    mom_sorted = sorted(p[1] for p in pillars.values() if p[1] is not None)
    liq_sorted = sorted(p[2] for p in pillars.values() if p[2] is not None)
    tliq_sorted = sorted(p[3] for p in pillars.values() if p[3] is not None)
    _w_tliq = float(w.get("tradeable_liquidity") or 0.0)  # only an active pillar when weighted
    # 5th pillar (daily-context variant only): the daily-structure sub-score from the
    # signal dict, read directly (no _extract_pillars signature change). Graceful-degrade
    # exactly like tradeable_liquidity — absent/zero-weight ⇒ not in the blend.
    _ds_raw = {sym: _first_float(sig or {}, "daily_structure_pct") for sym, sig in signals.items()}
    ds_sorted = sorted(v for v in _ds_raw.values() if v is not None)
    _w_ds = float(w.get("daily_structure") or 0.0)

    out: dict[str, RossMomentumScore] = {}
    for sym, (rvol, mom, liq, tliq) in pillars.items():
        rvol_pct = _percentile_rank(rvol, rvol_sorted) if rvol is not None else None
        mom_pct = _percentile_rank(mom, mom_sorted) if mom is not None else None
        liq_pct = _percentile_rank(liq, liq_sorted) if liq is not None else None
        tliq_pct = _percentile_rank(tliq, tliq_sorted) if tliq is not None else None
        _ds = _ds_raw.get(sym)
        ds_pct = _percentile_rank(_ds, ds_sorted) if _ds is not None else None

        present: list[tuple[float, float]] = []  # (percentile, weight)
        if rvol_pct is not None:
            present.append((rvol_pct, w["rvol"]))
        if mom_pct is not None:
            present.append((mom_pct, w["momentum"]))
        if liq_pct is not None:
            present.append((liq_pct, w["liquidity"]))
        if tliq_pct is not None and _w_tliq > 0:
            present.append((tliq_pct, _w_tliq))
        if ds_pct is not None and _w_ds > 0:
            present.append((ds_pct, _w_ds))

        wsum = sum(wt for _, wt in present)
        score = (sum(pct * wt for pct, wt in present) / wsum) if wsum > 0 else 0.0

        out[sym] = RossMomentumScore(
            symbol=sym,
            score=round(score, 4),
            rvol_pct=round(rvol_pct, 4) if rvol_pct is not None else 0.0,
            momentum_pct=round(mom_pct, 4) if mom_pct is not None else 0.0,
            liquidity_pct=round(liq_pct, 4) if liq_pct is not None else None,
            tradeable_liquidity_pct=round(tliq_pct, 4) if tliq_pct is not None else None,
            rank=0,
            universe_size=len(signals),
            breakdown={
                "rvol": rvol,
                "momentum": mom,
                "liquidity_neglog_size": liq,
                "tradeable_liquidity_log_dvol": tliq,
                "daily_structure": _ds if _w_ds > 0 else None,
                "pillars_present": [
                    name
                    for name, val in (
                        ("rvol", rvol_pct), ("momentum", mom_pct),
                        ("liquidity", liq_pct),
                        ("tradeable_liquidity", tliq_pct if _w_tliq > 0 else None),
                        ("daily_structure", ds_pct if _w_ds > 0 else None),
                    )
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
