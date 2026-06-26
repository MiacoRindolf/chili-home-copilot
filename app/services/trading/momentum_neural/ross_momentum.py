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
#
# (P1) SECOND-DAY / MULTI-DAY CONTINUATION (chili_momentum_second_day_context_enabled) folds
# INTO this same daily_structure sub-score (in daily_levels.compute_daily_context): a clean
# DAY-2 holding above the prior-day high/close is BOOSTED and a day-3+ (exhaustion) is DERATED,
# so it RE-RANKS via the existing daily_structure pillar — no new pillar weight, no new fetch
# (it reuses the P0 DailyContext daily df). A re-rank tilt only; never blocks a fill.
ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT: dict[str, float] = {
    "rvol": 0.40,
    "momentum": 0.30,
    "liquidity": 0.10,
    "tradeable_liquidity": 0.10,
    "daily_structure": 0.10,
}

# FLOAT-ROTATION sustainability pillar weight (opt-in via chili_momentum_float_rotation_tilt_enabled).
# UNLIKE the variants above this is NOT a full replacement weight-set — it is a SINGLE pillar that the
# pipeline folds onto WHATEVER weight-set is already active (composable). ``score_universe`` reads the
# per-symbol ``float_rotation_pct`` raw sub-score (stamped by the bridge, exactly like
# ``daily_structure_pct``) and renormalises over the present pillars, so adding this key never re-scales
# the other pillars by hand. 0.10 = the same minority magnitude as the daily-structure pillar: it
# RE-RANKS the pool toward names with fuel-remaining (cum-volume rotating the float), it can NEVER block
# a fill or remove a name from the pool. Absent ``float_rotation_pct`` (crypto / thin data / flag OFF) ⇒
# the pillar is simply not present in the blend ⇒ byte-identical ranking.
ROSS_FLOAT_ROTATION_PILLAR_WEIGHT = 0.10

# Ross SS101 sustainability threshold: a move whose CUMULATIVE session volume never clears ~1x its float
# fades (insider/institutional supply overwhelms the bid), while a name rotating its float ~5x+ by EOD has
# the demand to sustain. These are the ONE documented base each (a clear-floor and a saturation-reference),
# not scattered magic: the within-batch PERCENTILE of ``projected_rotation_at_eod`` is what actually ranks
# names, so these only shape the raw sub-score's curve (clip below 1x toward 0, saturate near 5x toward 1).
# Equity-only — crypto "float" is market-cap and 24h volume semantics differ; the bridge applies it to
# equities only. REFERENCE points (the percentile may discover a different live bar), never hard cutoffs.
ROSS_FLOAT_ROTATION_CLEAR_FLOOR = 1.0
ROSS_FLOAT_ROTATION_SATURATION = 5.0

# SQUEEZE-FUEL sustainability pillar weight (opt-in via chili_momentum_squeeze_fuel_tilt_enabled).
# Like ``float_rotation`` this is a SINGLE composable pillar the pipeline folds onto WHATEVER
# weight-set is already active (``score_universe`` reads the per-symbol raw ``squeeze_fuel_pct``
# sub-score, stamped by the bridge from Ortex, and renormalises over the present pillars). 0.10 =
# the same minority magnitude as daily_structure / float_rotation: it RE-RANKS the pool toward
# squeeze-prone names (heavily-shorted, hard-to-borrow float = trapped sellers covering INTO the
# pop) and slightly DE-RATES free-share / easy-to-borrow names (shorts press the pop) — it can
# NEVER block a fill or remove a name from the pool. Absent ``squeeze_fuel_pct`` (crypto / Ortex
# absent / flag OFF / no data) ⇒ the pillar is simply not present in the blend ⇒ byte-identical.
ROSS_SQUEEZE_FUEL_PILLAR_WEIGHT = 0.10

# Squeeze-fuel reference points (Ross SS101 #2). These shape the RAW sub-score's curve only —
# the within-batch PERCENTILE of ``squeeze_fuel_pct`` is what actually ranks names (adaptive,
# the live bar floats), so these are documented REFERENCES, never hard cutoffs:
#   * SI% of free float ~20%+ is a meaningfully-shorted name (the squeeze-prone tier); ~50%+ is
#     extreme (saturates the SI leg).
#   * CTB ~10% annual is "hard to borrow" (squeeze-prone); ~100%+ is extreme (saturates).
#   * CTB at/below EASY_TO_BORROW (free shares) pulls the name BELOW the 0.5 neutral midpoint
#     (the de-rate — shorts attack the pop with impunity).
ROSS_SQUEEZE_SI_PROMINENT = 0.20      # 20% of free float = meaningfully shorted
ROSS_SQUEEZE_SI_SATURATION = 0.50     # 50%+ = extreme (SI leg caps)
ROSS_SQUEEZE_CTB_HARD = 10.0          # 10% annual borrow = hard-to-borrow
ROSS_SQUEEZE_CTB_SATURATION = 100.0   # 100%+ = extreme (CTB leg caps)
ROSS_SQUEEZE_CTB_EASY = 1.0           # at/below this annual % = essentially free shares (de-rate)

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

# ── 3-layer EXPLOSIVE scorer (flag chili_momentum_explosive_scoring_enabled) ──
# The legacy blend (linear weighted-AVERAGE of percentiles) is fully COMPENSATORY and
# its percentiles SATURATE: a 15,000x-RVOL / +400% recent-IPO rocket and a non-explosive
# +10% mega-cap both map their explosive axes to ~1.0, then the mid pillars average the
# rocket back toward the batch mean — so a deep-tape mega-cap (top tradeable_liquidity)
# OUT-RANKS the rocket. The fix preserves the trusted backbone and adds three layers:
#
# LAYER 1 — a lexicographic EXPLOSIVENESS TIER from RAW batch-relative cuts (batch-MEDIAN
#   multiples, adaptive — NOT magic numbers). Non-compensatory: a higher tier STRICTLY
#   out-ranks a lower one no matter the continuous blend, so the explosive cohort fills the
#   arming slots first (kills slot-starvation). Tier 1 reuses the existing Ross hard floors.
# LAYER 2 — the continuous score = an EXPLOSIVE CORE (magnitude-preserving: log10-min-max
#   on the RAW rvol/momentum signal so 15,000x stays separated from 6x; rvol^0.6 * mom^0.4
#   PRODUCT is non-compensatory) x a BOUNDED quality modifier (0.5 + 0.5*quality) from the
#   EXISTING secondary-pillar blend — secondary pillars can only modulate +/-50%, never
#   average an explosive name down. SAME SHAPE as ``curl_score`` (base*reclaim*(0.5+0.5*x)).
# LAYER 3 — tiebreak: (tier, score, RAW rvol) so the raw-magnitude tiebreak actually fires.
#
# The tier multiples are the ONE documented base each (batch-median multiples for the
# extreme/strong tiers; the existing Ross floors for the eligibility tier); the core
# exponents 0.6/0.4 encode Ross's RVOL-over-momentum priority. Batch-relative throughout.
ROSS_EXPLOSIVE_TIER_EXTREME_MULT = 10.0   # tier 3: rvol AND change >= ~10x batch median
ROSS_EXPLOSIVE_TIER_STRONG_MULT = 3.0     # tier 2: rvol AND change >= ~3x batch median
ROSS_EXPLOSIVE_CORE_RVOL_EXP = 0.6        # explosive-core RVOL exponent (Ross #1 axis)
ROSS_EXPLOSIVE_CORE_MOM_EXP = 0.4         # explosive-core momentum exponent ("already moving")
# FIX A2 — missing-RVOL fallback. When a name has NO rvol (e.g. a ws_ignition mover the
# feed couldn't pair with a baseline) we no longer zero its core (which the viability tilt
# then PENALISES toward the floor). Instead we score it on momentum ALONE — but BOUNDED:
# the momentum-only core is the rvol-neutral assumption rvol_norm=NEUTRAL combined with the
# real mom_norm, then CAPPED below 1.0. The cap is the load-bearing safety the review asked
# for: a +400% vertical blow-off (mom_norm≈1.0) with no rvol confirmation can reach AT MOST
# this ceiling, so it can NEVER out-rank a clean mover that has BOTH rvol and momentum
# (whose confirmed core can reach 1.0). RVOL-confirmed explosiveness always wins.
ROSS_EXPLOSIVE_MISSING_RVOL_NEUTRAL = 0.5   # rvol-neutral assumption for a name with no rvol
ROSS_EXPLOSIVE_MISSING_RVOL_CORE_CEILING = 0.6  # hard cap on a momentum-only (no-rvol) core


def _median(sorted_vals: list[float]) -> float | None:
    """Median of an already-sorted list (None if empty). Batch-adaptive reference."""
    n = len(sorted_vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_vals[mid]
    return 0.5 * (sorted_vals[mid - 1] + sorted_vals[mid])


def _log_min_max(value: float | None, sorted_vals: list[float]) -> float | None:
    """log10-min-max normalisation of ``value`` within the batch, clamped [0,1].

    Magnitude-PRESERVING (unlike a percentile rank): two top-of-batch values stay
    separated on a log scale, so a 15,000x RVOL does NOT collapse onto a 6x. Operates
    on the RAW signal. Non-positive values are shifted onto a positive floor relative to
    the batch so a small/negative momentum reads near 0 rather than crashing the log.
    Returns ``None`` when the value is absent (the caller degrades that name gracefully).
    """
    if value is None or not sorted_vals:
        return None
    lo = sorted_vals[0]
    hi = sorted_vals[-1]
    # Shift onto a strictly-positive domain (momentum can be <= 0). Anchor at the batch
    # min so the smallest batch value maps to ~0 and ordering/spacing is preserved.
    floor = 1.0  # additive offset keeps log well-defined and the min at 0
    shift = (floor - lo) if lo < floor else 0.0
    v = value + shift
    lo_s = lo + shift
    hi_s = hi + shift
    if not (v > 0) or not (lo_s > 0) or not (hi_s > 0):
        return 0.0
    lg_lo = math.log10(lo_s)
    lg_hi = math.log10(hi_s)
    span = lg_hi - lg_lo
    if span <= 0:
        return 1.0  # degenerate batch (all equal) -> top of the (flat) range
    norm = (math.log10(v) - lg_lo) / span
    return max(0.0, min(1.0, norm))


def _explosive_tier(
    rvol: float | None,
    mom: float | None,
    rvol_median: float | None,
    mom_median: float | None,
    *,
    rvol_floor: float = ROSS_ELIGIBILITY_RVOL_FLOOR,
    change_floor_pct: float = ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT,
) -> int:
    """Lexicographic explosiveness tier in {0,1,2,3} for one name (LAYER 1).

    Batch-relative (median multiples) + the existing Ross hard floors. Fail-OPEN: a name
    with absent rvol/momentum degrades to tier 0 (omitted from the explosive cohort), never
    a crash and never a veto — selection re-rank only.
    """
    if rvol is None or mom is None:
        return 0
    rm = rvol_median if (rvol_median and rvol_median > 0) else None
    mm = mom_median if (mom_median and mom_median > 0) else None
    if rm is not None and mm is not None:
        if rvol >= ROSS_EXPLOSIVE_TIER_EXTREME_MULT * rm and mom >= ROSS_EXPLOSIVE_TIER_EXTREME_MULT * mm:
            return 3
        if rvol >= ROSS_EXPLOSIVE_TIER_STRONG_MULT * rm and mom >= ROSS_EXPLOSIVE_TIER_STRONG_MULT * mm:
            return 2
    if rvol >= float(rvol_floor) and mom >= float(change_floor_pct):
        return 1
    return 0


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
    tier: int = 0  # explosiveness tier (0..3); 0 unless the explosive scorer is active (LAYER 1)
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


# SSR (short-sale-restriction) trigger: SEC Rule 201 puts a name on SSR for the rest of the
# session + the next when it trades down >= 10%% vs the PRIOR-DAY close. Under SSR shorts may
# only sell on an UPTICK — they cannot hit the bid — so resting ASK-side stacking is NOT the
# bearish "shorts pressing the offer" signal the L2 seller-veto reads it as. We compute this
# FREE from price (no Ortex / borrow data needed). The 10%% is the SEC rule's own documented
# threshold, not a tuned magic number. Equity-only (the caller applies it to equities).
ROSS_SSR_TRIGGER_DOWN_PCT = 10.0


def compute_is_ssr(
    last_price: float | None,
    prior_close: float | None,
    *,
    trigger_down_pct: float = ROSS_SSR_TRIGGER_DOWN_PCT,
) -> bool:
    """True when an EQUITY name is on short-sale restriction (SEC Rule 201): currently DOWN
    by at least ``trigger_down_pct`` versus the PRIOR-DAY close. Free, price-only.

    This is a CONSERVATIVE intraday proxy — Rule 201 latches for the rest of the day once the
    10%% trip prints (and rolls to the next day), so a name that has recovered off the lows is
    still on SSR; without an exchange SSR feed we can only AFFIRM SSR while price sits below the
    trip level, which is exactly the window where the carve-out matters (shorts blocked from the
    bid). Fails CLOSED to ``False`` (not-SSR ⇒ no carve-out ⇒ existing veto behaviour) on any
    missing / non-positive input — the carve-out only ever RELAXES a veto, so a false-negative is
    safe (status quo) while we never assert SSR on bad data."""
    lp = _to_float(last_price)
    pc = _to_float(prior_close)
    if lp is None or pc is None or not (pc > 0) or not (lp > 0):
        return False
    down_pct = (pc - lp) / pc * 100.0
    return down_pct >= float(trigger_down_pct)


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
    explosive: bool | None = None,
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

    ``explosive`` (flag ``chili_momentum_explosive_scoring_enabled``): when truthy,
    the 3-layer EXPLOSIVE scorer replaces the score-compressing linear-percentile
    blend — a lexicographic explosiveness TIER (outer sort key), a magnitude-
    preserving log-min-max multiplicative CORE x bounded quality modifier, and a
    raw-rvol tiebreak (see the module constants). ``None`` (the default) resolves the
    live config flag; tests pass ``True``/``False`` explicitly. With the flag OFF the
    output is BYTE-IDENTICAL to the legacy blend (the explosive path is fully gated).
    """
    if not signals:
        return {}
    w = dict(weights or ROSS_PILLAR_WEIGHTS)
    # FIX A2 — missing-RVOL degrade flag (shares the ross_rvol_feed kill-switch: the whole
    # "un-zero the starved ws_ignition scorer" feature is one switch). Default ON; when OFF
    # the missing-rvol core stays 0.0 (byte-identical to the legacy explosive path).
    _missing_rvol_degrade = True
    if explosive is None:
        # Resolve the live config flag lazily (keeps this module IO-free at import).
        try:
            from app.config import settings as _settings  # local import: no top-level dep
            explosive = bool(getattr(_settings, "chili_momentum_explosive_scoring_enabled", True))
            _missing_rvol_degrade = bool(getattr(_settings, "chili_momentum_ross_rvol_feed_enabled", True))
        except Exception:
            explosive = False  # fail-CLOSED to the legacy blend if config is unavailable
            _missing_rvol_degrade = False  # fail-CLOSED to the legacy core=0.0 when config absent

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
    # Float-rotation sustainability pillar (composable, opt-in): the per-symbol raw
    # ``float_rotation_pct`` sub-score (projected-rotation-at-EOD shaped by the SS101 clear/
    # saturation references) stamped by the bridge. Graceful-degrade exactly like
    # daily_structure — absent / zero-weight ⇒ not in the blend (byte-identical).
    _fr_raw = {sym: _first_float(sig or {}, "float_rotation_pct") for sym, sig in signals.items()}
    fr_sorted = sorted(v for v in _fr_raw.values() if v is not None)
    _w_fr = float(w.get("float_rotation") or 0.0)
    # Squeeze-fuel pillar (composable, opt-in): the per-symbol raw ``squeeze_fuel_pct`` sub-score
    # (short-interest %% + cost-to-borrow shaped to a [0,1] boost/de-rate) stamped by the bridge
    # from Ortex. Graceful-degrade exactly like float_rotation — absent / zero-weight ⇒ not in
    # the blend (byte-identical).
    _sf_raw = {sym: _first_float(sig or {}, "squeeze_fuel_pct") for sym, sig in signals.items()}
    sf_sorted = sorted(v for v in _sf_raw.values() if v is not None)
    _w_sf = float(w.get("squeeze_fuel") or 0.0)
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

    # ── EXPLOSIVE scorer batch stats (LAYER 1 tier cuts + LAYER 2 magnitude norm) ──
    # Only computed when the flag is on; batch-relative (median multiples + log-min-max),
    # so there are no fixed magic cutoffs. Reuses the rvol/mom sorted lists above.
    _rvol_median = _median(rvol_sorted) if explosive else None
    _mom_median = _median(mom_sorted) if explosive else None

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
        _fr = _fr_raw.get(sym)
        fr_pct = _percentile_rank(_fr, fr_sorted) if _fr is not None else None
        _sf = _sf_raw.get(sym)
        sf_pct = _percentile_rank(_sf, sf_sorted) if _sf is not None else None

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
        if fr_pct is not None and _w_fr > 0:
            present.append((fr_pct, _w_fr))
        if sf_pct is not None and _w_sf > 0:
            present.append((sf_pct, _w_sf))

        wsum = sum(wt for _, wt in present)
        score = (sum(pct * wt for pct, wt in present) / wsum) if wsum > 0 else 0.0

        # ── LAYER 1+2: explosive tier + magnitude-preserving core x bounded quality ──
        _tier = 0
        if explosive:
            _tier = _explosive_tier(rvol, mom, _rvol_median, _mom_median)
            # LAYER 2 — explosive CORE on the RAW signal (log-min-max, magnitude-preserving).
            # rvol^0.6 * mom^0.4 PRODUCT (non-compensatory). Missing axis -> degrade to 0.0
            # (fail-OPEN: a name with no rvol/mom is simply not explosive, never crashes).
            rvol_norm = _log_min_max(rvol, rvol_sorted)
            mom_norm = _log_min_max(mom, mom_sorted)
            if rvol_norm is not None and mom_norm is not None:
                core = (rvol_norm ** ROSS_EXPLOSIVE_CORE_RVOL_EXP) * (mom_norm ** ROSS_EXPLOSIVE_CORE_MOM_EXP)
            elif _missing_rvol_degrade and rvol_norm is None and mom_norm is not None:
                # FIX A2 — name has momentum but NO rvol (a ws_ignition mover the feed couldn't
                # pair with a baseline). Don't zero it (which the tilt would then penalise toward
                # the floor). Score it on momentum with an rvol-NEUTRAL assumption, then CAP it so
                # an unconfirmed vertical name can never out-rank a clean rvol+mom mover.
                _neutral_core = (
                    ROSS_EXPLOSIVE_MISSING_RVOL_NEUTRAL ** ROSS_EXPLOSIVE_CORE_RVOL_EXP
                ) * (mom_norm ** ROSS_EXPLOSIVE_CORE_MOM_EXP)
                core = min(ROSS_EXPLOSIVE_MISSING_RVOL_CORE_CEILING, _neutral_core)
            else:
                # No momentum either (or degrade disabled) -> not explosive. Legacy behaviour.
                core = 0.0
            # Bounded QUALITY modifier from the EXISTING SECONDARY-pillar blend (liquidity /
            # tradeable_liquidity / daily_structure / float_rotation / squeeze_fuel — NOT the
            # rvol/mom that drive the core). 0.5 + 0.5*quality clamps the modulation to +/-50%,
            # so secondary pillars can NEVER average an explosive name down. Same shape as
            # curl_score (base*reclaim*(0.5+0.5*confirmer)). Absent secondary pillars -> 0.5
            # neutral quality -> modifier 0.75 (the name rides on its core, undamped).
            _secondary: list[tuple[float, float]] = []
            if liq_pct is not None:
                _secondary.append((liq_pct, w.get("liquidity", 0.0)))
            if tliq_pct is not None and _w_tliq > 0:
                _secondary.append((tliq_pct, _w_tliq))
            if ds_pct is not None and _w_ds > 0:
                _secondary.append((ds_pct, _w_ds))
            if fr_pct is not None and _w_fr > 0:
                _secondary.append((fr_pct, _w_fr))
            if sf_pct is not None and _w_sf > 0:
                _secondary.append((sf_pct, _w_sf))
            _sec_wsum = sum(wt for _, wt in _secondary)
            quality_blend = (sum(p * wt for p, wt in _secondary) / _sec_wsum) if _sec_wsum > 0 else 0.5
            quality_blend = max(0.0, min(1.0, quality_blend))
            score = max(0.0, min(1.0, core * (0.5 + 0.5 * quality_blend)))

        out[sym] = RossMomentumScore(
            symbol=sym,
            score=round(score, 4),
            rvol_pct=round(rvol_pct, 4) if rvol_pct is not None else 0.0,
            momentum_pct=round(mom_pct, 4) if mom_pct is not None else 0.0,
            liquidity_pct=round(liq_pct, 4) if liq_pct is not None else None,
            tradeable_liquidity_pct=round(tliq_pct, 4) if tliq_pct is not None else None,
            tier=int(_tier),
            rank=0,
            universe_size=len(signals),
            breakdown={
                "rvol": rvol,
                "momentum": mom,
                "liquidity_neglog_size": liq,
                "tradeable_liquidity_log_dvol": tliq,
                "daily_structure": _ds if _w_ds > 0 else None,
                "float_rotation_pct": _fr if _w_fr > 0 else None,
                "squeeze_fuel_pct": _sf if _w_sf > 0 else None,
                "pillars_present": [
                    name
                    for name, val in (
                        ("rvol", rvol_pct), ("momentum", mom_pct),
                        ("liquidity", liq_pct),
                        ("tradeable_liquidity", tliq_pct if _w_tliq > 0 else None),
                        ("daily_structure", ds_pct if _w_ds > 0 else None),
                        ("float_rotation", fr_pct if _w_fr > 0 else None),
                        ("squeeze_fuel", sf_pct if _w_sf > 0 else None),
                    )
                    if val is not None
                ],
            },
        )

    if explosive:
        # LAYER 1+3: lexicographic tier (non-compensatory) -> continuous score -> RAW rvol
        # tiebreak (the raw-magnitude tiebreak the legacy key could never reach because score
        # was always distinct first). A tier-3 name STRICTLY out-ranks every lower-tier name no
        # matter the blend, so the explosive cohort fills the arming slots first.
        ordered = sorted(
            out.values(),
            key=lambda s: (
                s.tier,
                s.score,
                (s.breakdown.get("rvol") if s.breakdown.get("rvol") is not None else float("-inf")),
            ),
            reverse=True,
        )
    else:
        # rank: highest blended score first; ties broken by rvol then momentum
        ordered = sorted(
            out.values(), key=lambda s: (s.score, s.rvol_pct, s.momentum_pct), reverse=True
        )
    for i, s in enumerate(ordered, start=1):
        s.rank = i
    return out


# ── SYMBOL-OF-THE-DAY FOCUS (Batch F; flag chili_momentum_symbol_of_day_focus_enabled) ──
# Ross trades the ONE best mover INTENSELY rather than spreading thin across the board —
# the "stock of the day". The selection backbone (score_universe + the 3-layer explosive
# scorer) already RANKS the batch; this layer names the single highest-CONVICTION explosive
# leader so the arm queue can give it ONE guaranteed priority slot (never starved, never
# displaced) while the REMAINING slots still fill by the normal rank (no over-concentration).
#
# The leader is defined by REUSING the explosive scorer — NO new magic:
#   * it must CLEAR Ross's hard floors (``below_explosive_floor`` is False — i.e. it is a
#     real live setup, not the loudest name in a dull batch), then
#   * it is the maximum by the SAME lexicographic key the explosive ranker sorts on
#     ``(tier, score, raw rvol*move)`` — i.e. ``rank == 1`` among floor-clearers — with the
#     conviction tiebreak being the biggest %-move × RVOL (the "biggest explosion" the task
#     asks for, computed from the raw pillars already on the score breakdown).
# Pure: operates on the ``score_universe`` output + the raw signal dicts; no IO.

# The ONE documented focus-tilt base. A leader that is already armed/watched earns a small
# additive ranking bonus each refresh so a TRANSIENT intraday dip does not rotate it out of
# the slot before its setup plays out (Ross stays ON his stock of the day). Same order of
# magnitude as ``ROSS_QUALITY_VIABILITY_TILT`` (the lane's one small-tilt base) so it nudges
# ordering without overpowering a genuinely fresher new leader. Composes with — never
# overrides — the hard guards (the leader still passes every begin/confirm risk gate).
ROSS_SYMBOL_OF_DAY_FOCUS_TILT = 0.20


def _conviction(score: "RossMomentumScore", signal: dict | None) -> float:
    """Biggest-explosion conviction = |%-move| × RVOL from the RAW pillars (the breakdown the
    scorer already stamped, falling back to the signal dict). The leader tiebreak among
    same-(tier,score) names — 'top explosive score / biggest %-move × RVOL', reusing the
    scorer's own raw inputs (no new magic). 0.0 when either axis is absent."""
    bd = getattr(score, "breakdown", None) or {}
    rvol = bd.get("rvol")
    mom = bd.get("momentum")
    if (rvol is None or mom is None) and isinstance(signal, dict):
        _r, _m, _l, _t = _extract_pillars(signal)
        rvol = rvol if rvol is not None else _r
        mom = mom if mom is not None else _m
    rv = _to_float(rvol)
    mv = _to_float(mom)
    if rv is None or mv is None or rv <= 0:
        return 0.0
    return abs(mv) * rv


def identify_leader(
    scores: dict[str, "RossMomentumScore"],
    signals: dict[str, dict] | None = None,
    *,
    rvol_floor: float = ROSS_ELIGIBILITY_RVOL_FLOOR,
    change_floor_pct: float = ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT,
) -> str | None:
    """The symbol-of-the-day = the single highest-conviction explosive LEADER in this batch.

    ``scores``: a ``score_universe`` result. ``signals``: the same ``{symbol: result_dict}``
    fed to ``score_universe`` (optional — used for the floor check + the conviction tiebreak;
    when omitted the breakdown on each score is used). Returns the leader SYMBOL, or ``None``
    when no name clears Ross's hard floors (a dull batch has no stock-of-the-day; the lane
    simply ranks normally that refresh — never forces a weak leader).

    Adaptive / no new magic: the leader is the max by the SAME lexicographic key the explosive
    ranker sorts on — ``(tier, score, conviction)`` — restricted to names that AFFIRMATIVELY
    clear the explosiveness floors (``below_explosive_floor`` is False). Equity floors are
    crypto-tolerant (a crypto name without equity-shaped rvol/change fails the floor OPEN, so
    it can still lead on tier+score). Pure + side-effect-free."""
    if not scores:
        return None
    sig = signals or {}

    def _clears_floor(sym: str) -> bool:
        s = sig.get(sym)
        if not isinstance(s, dict):
            return True  # no raw signal to disprove explosiveness -> fail-OPEN (rank decides)
        return not below_explosive_floor(s, rvol_floor=rvol_floor, change_floor_pct=change_floor_pct)

    eligible = [sym for sym in scores if _clears_floor(sym)]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda sym: (
            int(getattr(scores[sym], "tier", 0) or 0),
            float(getattr(scores[sym], "score", 0.0) or 0.0),
            _conviction(scores[sym], sig.get(sym)),
        ),
    )


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


# ── The Curl (HVM101): rounding-bottom continuation, as a pure SCORING signal ──
# Ross's "The Curl" (HVM101) = a rounding-bottom CONTINUATION off a gap-fade or a
# mid-trend pop-fade: the name dips, the selling decelerates, a base rounds out, and
# price reclaims successive micro-channels printing HIGHER LOWS — a cup-and-handle on
# the intraday frame. It is the pre-break "forming" shape: catching it EARLY lets the
# lane pre-arm a watcher before the textbook break fires (the entry gate still owns the
# actual fill). This is a SELECTION TILT, never a veto.
#
# This is deliberately NOT the per-bar ``candles.is_bounce_curl_candle`` (a single green
# re-load bar inside an already-confirmed pullback) nor the multi-bar
# ``entry_gates.pullback_break_confirmation`` (a fired break). Those are entry triggers;
# this reads the SESSION-FRAME rounding-bottom geometry and emits a continuous
# ``curl_score`` in [0,1] for ranking — distinct axis, distinct consumer.
#
# Adaptive by construction (operator: "no magic numbers"):
#   * The base is the recent LOW-anchored window; every threshold is a FRACTION of the
#     name's own intraday range (rounding depth, reclaim, higher-low slope) — no fixed
#     cents / fixed %. The one documented base is ``lookback`` (bars of session frame to
#     consider), mirroring ``intraday_impulse_freshness``'s own knob.
#   * Lookahead-free: reads completed bars only; the "reclaim" is measured up to the
#     CURRENT close, never a future bar.
# Fail-OPEN to a neutral 0.0 (no tilt) on thin/degenerate data — it can only ADD
# preference for a forming curl, never block or penalise a name that lacks the shape.


@dataclass
class CurlScore:
    """Rounding-bottom (cup-and-handle) continuation read of the session frame.

    ``curl_score`` in [0,1]: 0 = no rounding base / making lower-lows (not a curl), 1 =
    a clean deep round-bottom that has reclaimed its left rim on rising higher-lows. A
    pure ranking signal — additive selection tilt only, never a gate."""

    curl_score: float                  # [0,1] blended rounding-bottom-reclaim quality
    is_curling: bool                   # convenience: curl_score above a neutral midpoint
    base_depth: float | None           # how deep the bowl dipped, as a fraction of range
    reclaim: float | None              # how far price has climbed back up the bowl [0,1]
    higher_lows: float | None          # monotonic-rise quality of the post-trough lows [0,1]
    trough_centered: float | None      # how centered the trough is (U vs late-V) [0,1]
    reason: str
    debug: dict = field(default_factory=dict)


def curl_score(
    df,
    *,
    lookback: int = 20,
) -> CurlScore:
    """Detect a forming rounding-bottom / cup-and-handle (Ross "The Curl", HVM101) on the
    session frame and emit a continuous ``curl_score`` in [0,1] for selection ranking.

    Geometry (all range-relative — no fixed magnitude):
      * **base_depth** — the bowl actually DIPPED: ``(rim - trough) / range`` where ``rim``
        is the higher of the pre-trough start and the most recent close. A flat shelf
        (no dip to round off) scores ~0; a real fade-and-base scores high.
      * **reclaim** — price has CLIMBED BACK UP out of the bowl toward the rim:
        ``(last - trough) / (rim - trough)``. 0 = still at the bottom (no curl yet),
        1 = fully reclaimed the rim (handle / breakout-pending). This is the "curling
        back up" leg — the continuation tell.
      * **higher_lows** — since the trough, the bar-lows are RISING (the staircase of
        higher-lows reclaiming successive micro-channels). Measured as the fraction of
        consecutive post-trough lows that step up, minus down-steps — a monotonic-rise
        quality in [0,1].
      * **trough_centered** — the low sits in the MIDDLE of the window (a U / rounded
        base), not jammed at the right edge (a late V-dip that has not based yet).
        ``1 - |2*trough_pos - 1|`` over the window position.

    The score is the product-ish blend ``base_depth^? × reclaim`` gated by ``higher_lows``
    and softened by ``trough_centered`` — a name must have BOTH dipped (something to round
    off) AND begun reclaiming on higher-lows to read as a curl. A name making fresh lows
    (reclaim≈0) or one that never dipped (base_depth≈0) scores ~0.

    Lookahead-free: completed bars only, reclaim measured to the current close. Pure /
    side-effect-free. Fail-OPEN to ``curl_score=0.0`` (neutral, no tilt) on thin or
    degenerate data — this can only ADD ranking preference for a forming curl, never veto.
    """
    def _neutral(reason: str, **dbg) -> CurlScore:
        return CurlScore(0.0, False, None, None, None, None, reason, debug=dbg)

    if df is None or getattr(df, "empty", True):
        return _neutral("insufficient_bars", bars=0)
    closes = _df_col_floats(df, "Close")
    lows = _df_col_floats(df, "Low")
    highs = _df_col_floats(df, "High")
    n = len(closes)
    # Need enough bars to see a dip AND a round-back-up; mirror the freshness floor.
    if n < 6 or len(lows) != n or len(highs) != n:
        return _neutral("insufficient_bars", bars=n)
    look = min(int(lookback), n)
    if look < 6:
        return _neutral("insufficient_bars", bars=n)
    w_lows = lows[n - look:]
    w_highs = highs[n - look:]
    w_closes = closes[n - look:]
    win_high = max(w_highs)
    win_low = min(w_lows)
    rng = win_high - win_low
    last = w_closes[-1]
    if not (rng > 0) or last <= 0:
        return _neutral("no_range", win_high=win_high, win_low=win_low)

    # Trough = the lowest LOW in the window (the bottom of the bowl).
    t_idx = min(range(look), key=lambda i: w_lows[i])
    trough = w_lows[t_idx]
    # Rim = the higher of the window's opening close (left rim of the cup) and the
    # current close (the right rim / handle we are reclaiming toward). Range-relative.
    left_rim = w_closes[0]
    rim = max(left_rim, last)
    bowl = rim - trough
    if not (bowl > 0):
        return _neutral("no_bowl", trough=trough, rim=rim)

    # base_depth: how much of the window range the bowl dipped (something to round off).
    base_depth = max(0.0, min(1.0, bowl / rng))
    # reclaim: how far price has curled back up out of the bowl toward the rim.
    reclaim = max(0.0, min(1.0, (last - trough) / bowl))
    # trough_centered: a U-base has its trough mid-window; a late V-dip jams it right.
    t_pos = t_idx / (look - 1) if look > 1 else 0.5
    trough_centered = max(0.0, 1.0 - abs(2.0 * t_pos - 1.0))

    # higher_lows: the staircase of rising lows AFTER the trough (reclaiming micro-
    # channels). Net up-steps over the post-trough leg, normalised to [0,1]. Too few
    # post-trough bars -> graceful 0.5 (neutral, neither confirmed nor denied).
    post = w_lows[t_idx:]
    if len(post) >= 3:
        ups = sum(1 for a, b in zip(post, post[1:]) if b > a)
        downs = sum(1 for a, b in zip(post, post[1:]) if b < a)
        steps = len(post) - 1
        higher_lows = max(0.0, min(1.0, (ups - downs) / steps + 0.5)) if steps > 0 else 0.5
    else:
        higher_lows = 0.5

    # Blend: must have DIPPED (base_depth) AND be RECLAIMING (reclaim); higher-lows and a
    # centered U-base are confirmers that scale the read. Multiplicative so any missing
    # leg collapses the score (a flat shelf, a fresh-low dump, or a one-bar V-spike all
    # read low). Geometric-ish to keep it in [0,1] and reward simultaneous presence.
    score = (
        base_depth
        * reclaim
        * (0.5 + 0.5 * higher_lows)
        * (0.5 + 0.5 * trough_centered)
    )
    score = max(0.0, min(1.0, score))
    return CurlScore(
        curl_score=round(score, 4),
        is_curling=bool(score >= 0.5),
        base_depth=round(base_depth, 4),
        reclaim=round(reclaim, 4),
        higher_lows=round(higher_lows, 4),
        trough_centered=round(trough_centered, 4),
        reason="curling" if score >= 0.5 else ("reclaiming" if reclaim >= 0.5 else "basing_or_falling"),
        debug={
            "trough": round(trough, 8),
            "rim": round(rim, 8),
            "left_rim": round(left_rim, 8),
            "last": round(last, 8),
            "trough_pos": round(t_pos, 4),
            "lookback": int(look),
        },
    )


# ── Float-rotation sustainability (Ross SS101), as a pure SCORING signal ───────
# Ross SS101: ``float_rotation = cumulative_session_volume / shares_float`` — how many
# times the move has TURNED OVER the entire tradeable float so far today. A move whose
# cumulative volume never clears ~1x its float fades: there simply is not enough DEMAND
# to absorb the insider/institutional supply, so the spike round-trips. A move rotating
# its float ~5x+ by the close has the demand to SUSTAIN. CHILI already ranks RVOL and
# low-float INDEPENDENTLY but never divides volume BY float — this is that missing axis.
#
# ``projected_rotation_at_eod = current_rotation / session_fraction_elapsed`` linearly
# extrapolates today's pace to the close (a name 2x-rotated 25% into the session projects
# ~8x — plenty of fuel; a name 0.3x-rotated 80% in projects ~0.4x — a big float that
# CANNOT rotate, the high-RVOL-but-stalling de-rate). The raw sub-score is shaped by the
# SS101 clear-floor (below 1x projected ⇒ toward 0) and saturation (~5x ⇒ toward 1); the
# bridge then percentile-ranks it WITHIN the batch (adaptive — the live bar floats).
#
# Pure / side-effect-free. Lookahead-free (cumulative volume is volume printed SO FAR;
# the projection only extrapolates, it reads no future bar). Fail-OPEN to ``None`` on
# thin / missing / pre-open data so the bridge simply omits the pillar (byte-identical) —
# this can only RE-RANK toward sustainable fuel, it is NEVER a veto. Equity-only (the
# caller applies it to equities; crypto "float" is market-cap and 24h semantics differ).


@dataclass
class FloatRotationSignal:
    """Volume/float rotation sustainability read (Ross SS101). ``rotation_pct`` in
    [0,1] is the raw sub-score the bridge stamps + percentile-ranks like the other
    pillars; ``None`` (fail-open) ⇒ the pillar is omitted from the blend."""

    rotation_pct: float | None          # [0,1] raw sub-score; None = omit the pillar
    float_rotation: float | None        # cum_session_volume / shares_float (turns so far)
    projected_rotation_at_eod: float | None  # current / session_fraction_elapsed
    reason: str
    debug: dict = field(default_factory=dict)


def float_rotation_signal(
    cumulative_session_volume: float | None,
    shares_float: float | None,
    session_fraction_elapsed: float | None,
    *,
    clear_floor: float = ROSS_FLOAT_ROTATION_CLEAR_FLOOR,
    saturation: float = ROSS_FLOAT_ROTATION_SATURATION,
) -> FloatRotationSignal:
    """Compute Ross's float-rotation sustainability sub-score for one EQUITY name.

    Args:
      cumulative_session_volume — shares traded so far TODAY (the last session bar's
        cumulative volume from the candle frame).
      shares_float — the name's tradeable share float (the same value the low-float
        ``liquidity`` pillar already reads).
      session_fraction_elapsed — fraction of the regular session elapsed in (0,1]
        (e.g. ``minutes_since_regular_open / 390``, clamped). Used ONLY to project the
        current pace to EOD; clamped to a small floor so an early-session divide is sane.

    Returns a ``FloatRotationSignal`` whose ``rotation_pct`` (the decision-affecting
    output) shapes ``projected_rotation_at_eod`` against the SS101 clear-floor and
    saturation references: below ``clear_floor`` projected rotation ⇒ toward 0 (the move
    cannot clear its float — fades); ``saturation`` and above ⇒ 1.0 (ample fuel). Between,
    a smooth ramp. Fail-OPEN to ``rotation_pct=None`` (omit the pillar) on any missing /
    non-positive input — a name is NEVER de-ranked for absent float/volume data.
    """
    cv = _to_float(cumulative_session_volume)
    fl = _to_float(shares_float)
    if cv is None or fl is None or not (cv > 0) or not (fl > 0):
        return FloatRotationSignal(None, None, None, "missing_or_nonpositive",
                                   debug={"cum_vol": cv, "float": fl})
    rotation = cv / fl
    sf = _to_float(session_fraction_elapsed)
    # Clamp the session fraction to a small positive floor so a just-after-open divide
    # does not explode the projection; >1.0 (afterhours) clamps to 1.0 (no extrapolation).
    if sf is None or sf <= 0:
        # Pre-open / unknown clock — project at face value (no extrapolation), fail-open.
        projected = rotation
        sf_used = None
    else:
        sf_used = max(0.05, min(1.0, sf))
        projected = rotation / sf_used
    # Shape the projection into [0,1] against the SS101 references. A move PROJECTING to
    # clear its float (>= clear_floor) ramps up; one stalling below it (big float that can't
    # rotate) is pushed toward 0; saturation caps it. Smooth, monotone, no hard step.
    span = max(1e-9, float(saturation) - float(clear_floor))
    if projected <= float(clear_floor):
        # below the clear-floor: scale 0 -> ~0.5 of the floor so a totally-stalled name (≈0
        # rotation) reads ~0 and a name right AT the floor reads ~0.5 (neutral-leaning).
        rot_pct = 0.5 * max(0.0, min(1.0, projected / float(clear_floor)))
    else:
        rot_pct = 0.5 + 0.5 * max(0.0, min(1.0, (projected - float(clear_floor)) / span))
    rot_pct = max(0.0, min(1.0, rot_pct))
    return FloatRotationSignal(
        rotation_pct=round(rot_pct, 4),
        float_rotation=round(rotation, 4),
        projected_rotation_at_eod=round(projected, 4),
        reason=("projects_to_clear" if projected >= float(clear_floor) else "stalling_rotation"),
        debug={
            "cum_vol": round(cv, 2),
            "float": round(fl, 2),
            "session_fraction": (round(sf_used, 4) if sf_used is not None else None),
            "clear_floor": float(clear_floor),
            "saturation": float(saturation),
        },
    )


# ── Squeeze-fuel sustainability (Ross SS101 #2), as a pure SCORING signal ──────
# A heavily-shorted, hard/expensive-to-borrow float = trapped sellers who must cover INTO
# the pop — the rocket fuel behind the 100-1000% low-float verticals. Free shares (very low
# cost-to-borrow / easy-to-borrow) let shorts press the pop, so the same breakout fades.
# This maps the two core Ortex signals (short-interest %% of free float + cost-to-borrow,
# optionally utilization) into ONE raw [0,1] sub-score the bridge stamps as ``squeeze_fuel_pct``
# and then percentile-ranks WITHIN the batch (adaptive — the live bar floats). Centered at the
# 0.5 neutral midpoint: squeeze-prone names ramp ABOVE 0.5 (the boost); easy-to-borrow / free-
# share names sit BELOW 0.5 (the de-rate). Fail-OPEN to ``None`` (omit the pillar) when neither
# signal is present. Equity-only (crypto has no borrow data; the caller skips ``-USD``).


@dataclass
class SqueezeFuelSignal:
    """Short-squeeze-fuel read (Ross SS101 #2). ``squeeze_pct`` in [0,1] is the raw
    sub-score the bridge stamps + percentile-ranks like the other pillars; ``None``
    (fail-open) ⇒ the pillar is omitted from the blend (byte-identical)."""

    squeeze_pct: float | None            # [0,1] raw sub-score (>0.5 boost, <0.5 de-rate); None = omit
    short_interest_pct: float | None     # SI as a fraction of free float (Ortex)
    cost_to_borrow: float | None         # annual borrow cost %% (Ortex)
    utilization: float | None            # short utilization %% if available, else None
    is_easy_to_borrow: bool | None       # free shares ⇒ de-rate flag
    reason: str
    debug: dict = field(default_factory=dict)


def squeeze_fuel_signal(
    short_interest_pct: float | None,
    cost_to_borrow: float | None,
    *,
    utilization: float | None = None,
    is_easy_to_borrow: bool | None = None,
    si_prominent: float = ROSS_SQUEEZE_SI_PROMINENT,
    si_saturation: float = ROSS_SQUEEZE_SI_SATURATION,
    ctb_hard: float = ROSS_SQUEEZE_CTB_HARD,
    ctb_saturation: float = ROSS_SQUEEZE_CTB_SATURATION,
) -> SqueezeFuelSignal:
    """Compute Ross's squeeze-fuel sub-score for one EQUITY name from Ortex short mechanics.

    The decision-affecting output ``squeeze_pct`` is centered at 0.5 (neutral). The SI%% leg
    and the CTB leg each contribute a ramp ABOVE 0.5 when the name is squeeze-prone (heavily
    shorted / hard-to-borrow), saturating at their reference points; utilization, when present,
    is a soft confirmer on the same axis. A very-low-CTB / easy-to-borrow name is pulled BELOW
    0.5 (the de-rate — free shares, shorts attack the pop). Fail-OPEN to ``squeeze_pct=None``
    (omit the pillar) when BOTH SI%% and CTB are absent — a name is NEVER de-ranked for missing
    borrow data, only re-ranked on data that AFFIRMATIVELY shows squeeze fuel (or its absence).
    """
    si = _to_float(short_interest_pct)
    ctb = _to_float(cost_to_borrow)
    util = _to_float(utilization)
    if si is None and ctb is None:
        return SqueezeFuelSignal(None, None, None, util, is_easy_to_borrow,
                                 "missing_short_mechanics", debug={"si": si, "ctb": ctb})

    # Each present leg ramps in [0,1]; we blend the present legs, then map to a [0,1] score
    # centered at 0.5 (so the pillar is a symmetric boost/de-rate around neutral). SI%% and CTB
    # carry the squeeze signal; utilization is a soft +confirmer. Easy-to-borrow forces a de-rate.
    legs: list[tuple[float, float]] = []  # (leg_score_0to1, weight)
    if si is not None:
        # below `si_prominent` ⇒ ramps 0->~0.5; above ⇒ ramps ~0.5->1 saturating at si_saturation.
        if si <= float(si_prominent):
            si_leg = 0.5 * max(0.0, min(1.0, si / max(1e-9, float(si_prominent))))
        else:
            span = max(1e-9, float(si_saturation) - float(si_prominent))
            si_leg = 0.5 + 0.5 * max(0.0, min(1.0, (si - float(si_prominent)) / span))
        legs.append((max(0.0, min(1.0, si_leg)), 0.50))
    if ctb is not None:
        if ctb <= float(ctb_hard):
            ctb_leg = 0.5 * max(0.0, min(1.0, ctb / max(1e-9, float(ctb_hard))))
        else:
            span = max(1e-9, float(ctb_saturation) - float(ctb_hard))
            ctb_leg = 0.5 + 0.5 * max(0.0, min(1.0, (ctb - float(ctb_hard)) / span))
        legs.append((max(0.0, min(1.0, ctb_leg)), 0.40))
    if util is not None:
        # utilization is a 0-100 %% (or already a fraction); normalise either way to [0,1].
        util_frac = util / 100.0 if util > 1.0 else util
        legs.append((max(0.0, min(1.0, util_frac)), 0.10))

    wsum = sum(w for _, w in legs)
    score = (sum(v * w for v, w in legs) / wsum) if wsum > 0 else 0.5

    # Easy-to-borrow override (free shares): pull the score below the 0.5 neutral midpoint so
    # the name is DE-RATED relative to the batch — shorts can press the pop with impunity.
    eb = is_easy_to_borrow
    if eb is None and ctb is not None:
        eb = bool(ctb <= ROSS_SQUEEZE_CTB_EASY)
    if eb:
        score = min(score, 0.40)

    score = max(0.0, min(1.0, score))
    return SqueezeFuelSignal(
        squeeze_pct=round(score, 4),
        short_interest_pct=(round(si, 6) if si is not None else None),
        cost_to_borrow=(round(ctb, 4) if ctb is not None else None),
        utilization=(round(util, 4) if util is not None else None),
        is_easy_to_borrow=eb,
        reason=("easy_to_borrow_derate" if eb else
                "squeeze_prone" if score >= 0.5 else "low_fuel"),
        debug={
            "si": si, "ctb": ctb, "utilization": util,
            "si_prominent": float(si_prominent), "ctb_hard": float(ctb_hard),
        },
    )
