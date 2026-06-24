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

# ATTENTION-LEADERSHIP variant (opt-in via chili_momentum_attention_leadership_enabled):
# the 2026-06-22 Ross study's TRUE winner/loser separator. Position (pos-in-range, VWAP
# extension) does NOT separate — NXTS-winner and QXL-loser both break to new highs near
# VWAP (which is why L3 + front_side fail and a position-veto kills winners). What DOES
# separate: AMPLITUDE-LEADERSHIP — the winners (NXTS, CRMT) were rank #1 carrying ~30-36%
# of the live mover-field's amplitude; every loser was a follower (QXL was DOWN -2% while
# NXTS sat +186% beside it). `attention` = the name's share+rank of the field's amplitude
# (a 6th pillar, percentile-ranked like the rest — it RE-RANKS the pool toward the dominant
# leader, NEVER vetoes, so a fresh-breakout leader near VWAP scores HIGH and breadth is kept:
# the #2-#5 movers still rank + can arm). `dormant` = today's vol vs the name's own prior-day
# baseline (Ross's dormant->explosive precondition; best-effort, graceful-degrades).
ROSS_PILLAR_WEIGHTS_ATTENTION: dict[str, float] = {
    "rvol": 0.30,
    "momentum": 0.20,
    "liquidity": 0.10,
    "tradeable_liquidity": 0.10,
    "attention": 0.20,
    "dormant": 0.10,
}

# Max tilt the Ross momentum quality applies to a momentum-neural viability
# score. ``ross_score`` in [0,1] is centered at 0.5, so the applied tilt is
# +/- (TILT/2): at 0.20 a top-decile explosive setup gets +0.10 — enough to
# clear the live-eligibility floor a generic setup would miss — and a dull one
# -0.10. Same order of magnitude as the existing hand-coded viability nudges.
ROSS_QUALITY_VIABILITY_TILT = 0.20

# Ross's two most-stated HARD FLOORS for a small-cap to even be a setup (videos
# 01/05/17/29/36): relative volume >= ~5x and up >= ~10% on the day. Below these a
# name is simply not a Ross trade no matter how it ranks WITHIN a dull batch — the
# within-batch percentile (score_universe) only ORDERS names that already clear the
# floor. Documented REFERENCE floors (the system may raise them), not ceilings
# (a 50x-RVOL / +200% name is MORE eligible, never less). Equity-only — crypto RVOL/
# change semantics differ (24h) and get their own calibration if/when needed.
ROSS_ELIGIBILITY_RVOL_FLOOR = 5.0
ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT = 10.0


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
            # ``todays_change_perc`` is the VENDOR change-vs-prior-close field and the
            # key the WS ignition loop emits (ignition_loop.py:346) for an exploding
            # name. It was MISSING from this list, so every ignition-discovered mover
            # (SLBT +479% on 2026-06-16) read momentum=None → Ross quality 0.00
            # "generic setup" → out-ranked by smaller, fully-enriched names and never
            # armed. Reading it here lets the biggest explosions rank by their move.
            _first_float(signal, "daily_change_pct", "change_24h", "change_pct", "todays_change_perc"),
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


def below_explosive_floor(
    signal: dict,
    *,
    rvol_floor: float = ROSS_ELIGIBILITY_RVOL_FLOOR,
    change_floor_pct: float = ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT,
) -> bool:
    """True when an EQUITY name fails Ross's hard explosiveness floors and so is not a
    LIVE setup regardless of its within-batch percentile rank: relative volume below
    ``rvol_floor`` OR up-move below ``change_floor_pct``. Reuses the same raw fields as
    ``_extract_pillars`` (``vol_ratio``/``rvol`` + ``daily_change_pct``/``gap_pct``).

    Fails OPEN (returns ``False`` = "not below the floor") whenever a field is missing —
    a name is never benched on absent data, only on data that AFFIRMATIVELY shows it is
    not explosive. Caller is responsible for applying this to equities only."""
    rvol, momentum, _liq, _tl = _extract_pillars(signal)
    if rvol is not None and float(rvol) < float(rvol_floor):
        return True
    if momentum is not None and float(momentum) < float(change_floor_pct):
        return True
    return False


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
    # 6th/7th pillars (attention-leadership variant): the name's amplitude-leadership
    # share+rank of the live mover-field (the TRUE winner/loser separator) + its
    # dormant->explosive volume. Stamped cross-sectionally in _bridge_scanner_to_viability
    # over the full field; graceful-degrade (absent / zero-weight ⇒ not in the blend).
    _att_raw = {sym: _first_float(sig or {}, "attention_leadership") for sym, sig in signals.items()}
    att_sorted = sorted(v for v in _att_raw.values() if v is not None)
    _w_att = float(w.get("attention") or 0.0)
    _dorm_raw = {sym: _first_float(sig or {}, "dormant_rvol") for sym, sig in signals.items()}
    dorm_sorted = sorted(v for v in _dorm_raw.values() if v is not None)
    _w_dorm = float(w.get("dormant") or 0.0)

    out: dict[str, RossMomentumScore] = {}
    for sym, (rvol, mom, liq, tliq) in pillars.items():
        rvol_pct = _percentile_rank(rvol, rvol_sorted) if rvol is not None else None
        mom_pct = _percentile_rank(mom, mom_sorted) if mom is not None else None
        liq_pct = _percentile_rank(liq, liq_sorted) if liq is not None else None
        tliq_pct = _percentile_rank(tliq, tliq_sorted) if tliq is not None else None
        _ds = _ds_raw.get(sym)
        ds_pct = _percentile_rank(_ds, ds_sorted) if _ds is not None else None
        _att = _att_raw.get(sym)
        att_pct = _percentile_rank(_att, att_sorted) if _att is not None else None
        _dorm = _dorm_raw.get(sym)
        dorm_pct = _percentile_rank(_dorm, dorm_sorted) if _dorm is not None else None

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
        if att_pct is not None and _w_att > 0:
            present.append((att_pct, _w_att))
        if dorm_pct is not None and _w_dorm > 0:
            present.append((dorm_pct, _w_dorm))

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


@dataclass
class FrontSideState:
    """Session-anchored front-side (fresh, buyable) vs backside (extended/faded) read
    of TODAY's move — where the name sits in its OWN session, which ``position_in_range``
    (a 20-bar window) cannot tell: a fresh breakout near VWAP and an extended backside
    blow-off top both sit near a recent high. Pure + side-effect-free."""

    is_backside: bool
    front_side_score: float            # [0,1]; 0.5 neutral, higher = more front-side
    above_vwap: bool
    session_vwap: float | None
    vwap_dist_sigma: float | None
    retrace_from_hod: float | None
    day_range_pos: float | None
    reason: str
    debug: dict = field(default_factory=dict)


def front_side_state(
    df,
    *,
    top_range_pct: float = 0.85,
    retrace_veto: float = 0.66,
    ext_sigma: float = 2.0,
    rollover_min_range_frac: float = 0.02,
) -> FrontSideState:
    """Front-side vs backside lifecycle read of TODAY's session move (the QXL/NXTS fix).

    2026-06-22 study: CHILI bought QXL at 98.6% of HOD (top of the day-range AND far above
    VWAP = a backside blow-off top) and MISSED NXTS (a fresh thrust that broke to a NEW HIGH
    near VWAP). ``intraday_impulse_freshness.position_in_range`` cannot separate them — both
    sit near a recent high — and is ranked DESCENDING, so the extended top wins the slot.
    The SESSION anchors separate them.

    ``is_backside`` (hard veto; ANY of) =
      * below session VWAP (Ross: below-VWAP is inherently bearish — the SAGT skip), OR
      * retraced > ``retrace_veto`` of the day's up-move from the open (already faded), OR
      * EXTENDED-AND-ROLLING: in the top ``top_range_pct`` of the day-range AND > ``ext_sigma``
        above VWAP in the name's OWN close-vs-VWAP sigma AND the move has ROLLED OVER — the HOD
        is NOT the most recent bar and a confirmed LOWER HIGH has formed after the HOD bar
        (best post-HOD high below the HOD by > ``rollover_min_range_frac`` of the day-range).
        That STRUCTURE leg is the discriminator (the QXL chase): pure extension alone does not
        separate a fresh thrust from a rolled-over top.
    CRITICAL: a fresh front-side thrust making (or just breaking to) a NEW HIGH has its HOD on
    (or very near) the most recent bar -> NO confirmed lower-high -> NOT ``chasing_top`` ->
    NOT vetoed, even when it sits top-of-range AND far above VWAP (low-noise clean climbs blow
    ``vwap_dist_sigma`` up). Only a name that is extended AND has started to FADE off the top is
    ``chasing_top``. This is why the recalibration uses an OFF-THE-HIGH structure condition, not
    a brittle ``ext_sigma`` bump — and does NOT kill the NXTS-type front-side entry (that mistake
    killed the NVCT winner in the L3 study).

    Adaptive: ``vwap_dist_sigma`` floats with the name's own close-vs-VWAP dispersion;
    ``day_range_pos``/``retrace`` are name-relative ratios; the rollover lower-high test is
    measured as a fraction of the name's OWN day-range (``rollover_min_range_frac``), so it has
    no fixed-price magnitude. The four knobs are the only documented bases. Pure (operates on
    the session OHLCV frame the caller already fetched;
    premarket-inclusive). Thin/degenerate data -> fail-OPEN (not backside, neutral score),
    preserving current arm/entry behaviour.
    """
    def _unknown(reason: str, **dbg) -> FrontSideState:
        return FrontSideState(False, 0.5, True, None, None, None, None, reason, debug=dbg)

    if df is None or getattr(df, "empty", True):
        return _unknown("insufficient_bars", bars=0)
    highs = _df_col_floats(df, "High")
    lows = _df_col_floats(df, "Low")
    closes = _df_col_floats(df, "Close")
    vols = _df_col_floats(df, "Volume")
    n = len(closes)
    if n < 5 or len(highs) != n or len(lows) != n:
        return _unknown("insufficient_bars", bars=n)
    last = closes[-1]
    hod, lod, sess_open = max(highs), min(lows), closes[0]
    rng = hod - lod
    if not (rng > 0) or last <= 0:
        return _unknown("no_range", hod=hod, lod=lod)
    # session-anchored cumulative VWAP + the name's OWN close-vs-VWAP dispersion (sigma)
    vwap = None
    dist_sigma = None
    if len(vols) == n and sum(vols) > 0:
        cum_pv = cum_v = 0.0
        dists: list[float] = []
        for i in range(n):
            typ = (highs[i] + lows[i] + closes[i]) / 3.0
            cum_pv += typ * max(0.0, vols[i])
            cum_v += max(0.0, vols[i])
            if cum_v > 0:
                dists.append(closes[i] - cum_pv / cum_v)
        if cum_v > 0:
            vwap = cum_pv / cum_v
            if len(dists) >= 5:
                m = sum(dists) / len(dists)
                sd = (sum((d - m) ** 2 for d in dists) / len(dists)) ** 0.5
                if sd > 0:
                    dist_sigma = (last - vwap) / sd
    above_vwap = (vwap is None) or (last >= vwap)          # unknown vwap -> don't penalize
    day_range_pos = (last - lod) / rng
    up_move = hod - sess_open
    retrace_from_hod = ((hod - last) / up_move) if up_move > 0 else 0.0
    below_vwap = (vwap is not None) and (last < vwap)
    faded = retrace_from_hod > float(retrace_veto)
    # OFF-THE-HIGH structure: the move has ROLLED OVER iff the HOD is NOT the most recent bar
    # AND a confirmed LOWER HIGH has formed after the HOD bar (the best post-HOD high sits below
    # the HOD by more than rollover_min_range_frac of the day-range). A name AT/NEAR a fresh HOD
    # (HOD on the last bar, or only a wick below) is NEVER rolled_over -> NEVER chasing_top, so a
    # clean front-side thrust to a new high is kept even when extended. Pure structure, range-
    # relative (no fixed-price magnitude) -> not the brittle ext_sigma-bump path.
    hod_idx = max(i for i in range(n) if highs[i] == hod)
    rolled_over = False
    if hod_idx < (n - 1):
        post_hod_high = max(highs[hod_idx + 1:])
        rolled_over = ((hod - post_hod_high) / rng) > float(rollover_min_range_frac)
    chasing_top = (day_range_pos >= float(top_range_pct)
                   and dist_sigma is not None and dist_sigma >= float(ext_sigma)
                   and rolled_over)
    is_backside = below_vwap or faded or chasing_top
    reason = ("below_vwap" if below_vwap else "already_faded" if faded
              else "chasing_top" if chasing_top else "front_side")
    # front_side_score in [0,1], centered ~0.5 — a backside-PRESSURE penalty (extension is
    # the discriminator, NOT 'low retrace': being at-the-high is exactly the QXL backside, so
    # rewarding low retrace would re-invert). Penalize extension-above-VWAP, below-VWAP, and
    # deep fade; a fresh dip near VWAP carries ~no penalty -> high score.
    pen_ext = 0.0 if dist_sigma is None else max(0.0, min(1.0, max(0.0, dist_sigma) / float(ext_sigma)))
    pen_below = 0.0 if above_vwap else 1.0
    pen_faded = max(0.0, min(1.0, retrace_from_hod / float(retrace_veto)))
    score = round(max(0.0, min(1.0, 1.0 - (0.5 * pen_ext + 0.3 * pen_below + 0.2 * pen_faded))), 4)
    return FrontSideState(
        is_backside=bool(is_backside), front_side_score=float(score),
        above_vwap=bool(above_vwap),
        session_vwap=(round(vwap, 6) if vwap is not None else None),
        vwap_dist_sigma=(round(dist_sigma, 3) if dist_sigma is not None else None),
        retrace_from_hod=round(retrace_from_hod, 4), day_range_pos=round(day_range_pos, 4),
        reason=reason,
        debug={"hod": round(hod, 6), "lod": round(lod, 6), "open": round(sess_open, 6),
               "last": round(last, 6), "rolled_over": bool(rolled_over)},
    )
