"""Daily-chart context for the momentum lane — the broader-trend read + major daily
S&R levels Ross STARTS with, that the intraday (1m/5m/15m) lane was missing.

PURE (no DB/IO) — operates on a daily OHLCV df the caller fetched + cached, mirroring
the ``ross_momentum.py`` purity pattern. The OUTPUT is used as a SELECTION TILT (a soft
daily-structure sub-score, percentile-ranked like the other pillars — it RE-RANKS,
never blocks a fill) + log-only annotation + an opt-in INVARIANT-A stop FLOOR.

It is DELIBERATELY NOT an entry filter. A news-gap momentum SPIKE (CUPR 2.95→7.80)
often has NO daily uptrend and fires while still under an un-cleared daily level — a
hard daily filter would block exactly the winners (and a deferred entry never re-fires
once the spike runs away). So ``trend_score`` is SOFT (a downtrending gapper lands
~0.2-0.4, NEVER zeroed, never blocked) and breaking ABOVE a level is REWARDED, not
required. Fail-OPEN on thin/short/NaN daily history → no tilt, no gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..indicator_core import compute_atr

# CAP_ATR: distances beyond this many daily-ATR units count as "all the room" — ONE
# documented scale; everything else derives from the daily ATR (equity-relative).
_CAP_ATR = 3.0

# ── Daily-structure TILT bases (the only irreducible documented constants; everything
#    else derives from the daily ATR / median candle range / df length — adaptive). ──
# (A) gap geometry: a gap is "significant" when its size clears EITHER ~2x the median
#     daily candle range OR the daily ATR (whichever the tape supports). Wick windows
#     (intra-candle to-the-wick voids) are a weaker class, gated at the same threshold.
_GAP_SIGNIF_MEDIAN_MULT = 2.0
# (C) blue-sky / recent-IPO: "recent IPO" = fewer than this many daily bars of history
#     (~2 trading years). A name with this much or more history has trapped longs and
#     does NOT get the no-overhead-supply boost. ATH epsilon is ATR-relative (below).
_RECENT_IPO_MAX_BARS = 504  # ~2 yr of trading days; the ONE documented IPO-recency base

# (P1) SECOND-DAY / MULTI-DAY CONTINUATION (chili_momentum_second_day_context_enabled).
# Ross trades the SECOND day of a multi-day runner aggressively (a clean day-2 holding
# above the prior-day high / prior-day close with continuation structure is his bread-and-
# butter) and DISTRUSTS day-3+ (exhaustion / blow-off risk). CHILI had zero multi-day
# memory — each session was amnesiac. This counts the consecutive up/elevated daily bars
# since the catalyst spike (``day_number_in_run``) and folds a bounded SELECTION tilt into
# the daily-structure sub-score (BOOST a clean day-2, DERATE day-3+). It is a RE-RANK tilt,
# never a hard gate (a news-gap day-1 spike still scores HIGH — the CUPR guarantee holds).
#   * A daily bar counts toward the run when it is an UP day (close > prior close) OR
#     "elevated" (close within ~one daily-ATR of the run's high — a tight consolidation
#     after the spike still counts as holding the move, not breaking it). The run resets
#     on a bar that closes BELOW the prior bar by more than one daily-ATR (the move broke).
#   * The ONE documented base is the day-2 boost magnitude; the day-3+ derate is the same
#     magnitude on the other side (symmetric), and the "elevated" tolerance is the daily-ATR
#     (adaptive — no fixed %). The boost is gated on the name actually HOLDING above the
#     prior-day high / prior-day close (continuation structure), so a day-2 that already
#     rolled under yesterday's range gets no boost.
_SECOND_DAY_TILT = 0.12          # ONE documented base: the day-2 continuation boost / day-3+ derate magnitude
_SECOND_DAY_RUN_MAX_BARS = 6     # how far back to scan for the consecutive run (bounds the loop; adaptive cap)

# (A5) SYMBOL-FRESHNESS TILT (chili_momentum_symbol_freshness_tilt_enabled). Ross prefers a
# name with a "somewhat recent reverse split, no big days of volume recently" (FRESH — CLRO)
# and passes a name that "made a huge move and then sold off" (STALE — DSY). Two-signed SOFT
# tilt folded into daily_structure_pct. Reuses the lane's OWN existing explosive change-pct
# floor (ross_momentum.ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT = 10.0) as the "explosive daily move"
# threshold — NO new magic number. The trailing $-volume window is the SAME 20-day selection
# window the daily lane already uses; the "own trailing year" baseline reuses the full df.
#   * FRESH = no explosive daily move within the recency window AND the trailing-20d $vol sits
#     in a LOW percentile vs the symbol's own trailing-year $vol (quiet, un-exhausted) => +tilt.
#   * STALE = a recent explosive daily move that FADED (a bar moved >= the floor UP, then price
#     closed back BELOW that bar's pre-move base) => -tilt.
# The tilt magnitude reuses _FRESHNESS_TILT (the ONE documented base, symmetric both signs).
_FRESHNESS_TILT = 0.10           # ONE documented base: the symbol-freshness boost / stale-fade derate magnitude
_FRESHNESS_STALE_MAX_BARS = 10   # recency window (daily bars) for "recent explosive move" (bounds the scan)
_FRESHNESS_LOW_VOL_PCTL = 0.40   # trailing-20d $vol at/below this own-year percentile = "quiet / no big recent volume"

# (A7) CONSECUTIVE-RED-DAILIES SOFT DE-RANK — Ross passes a daily chart with "four or five red
# candles in a row … price clearly coming down." Counts consecutive DOWN closes ending yesterday
# and folds a SOFT de-rate into daily_structure_pct (SELECTION tilt, NEVER an entry veto — the
# module docstring guarantee keeps day-2 gapper winners tradable). Derate scales with run length
# using the existing red-rejection tilt base (_RED_REJECTION_DERATE_BASE); no new flag (folds under
# chili_momentum_red_rejection_derate_enabled). Fail-open-to-neutral on thin history.
_RED_REJECTION_DERATE_BASE = 0.15  # the existing red-rejection/derate tilt base (mirrors the (B) block below)
_RED_RUN_MAX_BARS = 6              # how far back to scan for the consecutive down-close run (bounds the loop)


@dataclass(frozen=True)
class DailyContext:
    prior_day_high: float | None
    prior_day_low: float | None
    swing_high_nd: float | None
    swing_low_nd: float | None
    daily_atr_pct: float | None
    dist_to_resistance_atr: float | None
    dist_to_support_atr: float | None
    near_round_number: float | None
    trend_score: float                  # [0,1] SOFT broader-trend; 0.5 = neutral/unknown
    breaking_major_level: bool
    daily_structure_pct: float | None   # [0,1] selection sub-score; None = skip the pillar
    reason: str
    sma_200: float | None = None        # Ross's daily macro-trend / support benchmark
    above_sma_200: bool | None = None   # None when < 200 daily bars (fail-open)
    dist_to_sma_200_atr: float | None = None  # signed daily-ATR units (+ above, − below)
    # (A) GAP/WINDOW GEOMETRY (chili_momentum_gap_geometry_tilt_enabled) — log-only
    # annotations; the decision effect is folded into daily_structure_pct.
    nearest_unfilled_gap_bottom: float | None = None  # to-the-penny trigger edge above px
    gap_top: float | None = None                       # clear-sky room ceiling of that gap
    is_window: bool | None = None                      # nearest gap is a weaker wick-window
    room_to_gap_top_atr: float | None = None           # daily-ATR units of overhead clear sky
    opens_into_gap: bool | None = None                 # a break here would enter unfilled space
    # (B) RED-REJECTION HISTORY (chili_momentum_red_rejection_derate_enabled).
    rejection_count: int | None = None                 # large upper-wick red rejections at level
    rejection_recency_frac: float | None = None        # [0,1] recency weight of the rejections
    # (C) BLUE-SKY / RECENT-IPO (chili_momentum_blue_sky_recent_ipo_enabled).
    is_blue_sky: bool | None = None                    # px within ε of the all-history max
    trading_history_days: int | None = None            # bars of available daily history
    is_recent_ipo: bool | None = None                  # short history => no trapped overhead supply
    # (P1) SECOND-DAY / MULTI-DAY CONTINUATION (chili_momentum_second_day_context_enabled).
    day_number_in_run: int | None = None               # consecutive up/elevated daily bars since the catalyst spike (1 = day-1 spike)
    prior_day_close: float | None = None               # yesterday's daily close (the day-2 hold reference)
    prior_week_high: float | None = None               # highest high over the trailing ~5 daily bars (the week's ceiling)
    # (A5) SYMBOL FRESHNESS (chili_momentum_symbol_freshness_tilt_enabled) — log/audit annotations.
    days_since_last_explosive_move: int | None = None  # bars since the last daily |move| >= the lane's explosive floor (None = none in window)
    trailing_dollar_vol_pctl: float | None = None      # [0,1] trailing-20d $vol percentile vs the symbol's own trailing year
    freshness_tilt_sign: int | None = None             # +1 FRESH boost / -1 STALE-fade derate / 0 neutral (log-only)
    # (A7) CONSECUTIVE-RED-DAILIES (folds under chili_momentum_red_rejection_derate_enabled).
    red_run_count: int | None = None                   # consecutive down-closes ending yesterday (0 = none / not down)


_NULL = DailyContext(
    None, None, None, None, None, None, None, None, 0.5, False, None,
    "insufficient_daily_bars", None, None, None,
)


def _round_number_near(price: float, atr_pct: float) -> float | None:
    """Nearest major round number within 0.25 daily-ATR of price (a psych level).
    Clamps the decade exponent so sub-cent crypto and 5-digit names don't compute a
    misaligned grid."""
    if price is None or price <= 0 or not math.isfinite(price):
        return None
    try:
        exp = max(-4, min(6, math.floor(math.log10(price))))
        step = 10.0 ** exp
        tol = 0.25 * atr_pct * price if (atr_pct and atr_pct > 0) else 0.0
        for s in (step, step * 0.5):
            if s <= 0:
                continue
            lvl = round(price / s) * s
            if abs(price - lvl) <= tol:
                return float(lvl)
    except (ValueError, OverflowError):
        return None
    return None


def detect_open_gaps(
    high: Any,
    low: Any,
    close: Any,
    open_: Any | None,
    *,
    px: float,
    atr_last: float,
) -> dict | None:
    """(A) Nearest UNFILLED gap/window ABOVE ``px`` — pure, lookahead-free.

    Two classes, strongest first:
      • a true OPEN gap: bar i's low > bar i-1's close (an up-gap that left an
        untraded void between prior-close and the new open), still unfilled = no
        later bar's LOW has traded back down into it;
      • a weaker WICK WINDOW: bar i's low > bar i-1's high (a candle-body/wick void),
        flagged ``is_window``.

    Significance floor (adaptive): the void must clear ``_GAP_SIGNIF_MEDIAN_MULT`` ×
    the median daily candle range OR the daily ATR — whichever the tape supports — so a
    one-tick rounding void never counts. Returns the NEAREST still-open void whose
    BOTTOM is at/above ``px`` (the to-the-penny break trigger) with its TOP as the
    clear-sky ceiling, or ``None`` when there is none / data is too thin (fail-open).
    """
    try:
        hv = [float(x) for x in high.values]
        lv = [float(x) for x in low.values]
        cv = [float(x) for x in close.values]
    except Exception:
        return None
    n = len(hv)
    if n < 3 or not (math.isfinite(px) and px > 0):
        return None
    # adaptive significance: 2x median candle range OR the daily ATR (whichever holds).
    ranges = sorted(hv[i] - lv[i] for i in range(n) if math.isfinite(hv[i]) and math.isfinite(lv[i]) and hv[i] >= lv[i])
    if not ranges:
        return None
    med_rng = ranges[len(ranges) // 2]
    signif = max(_GAP_SIGNIF_MEDIAN_MULT * med_rng, atr_last if (atr_last and math.isfinite(atr_last)) else 0.0)
    if not (signif > 0 and math.isfinite(signif)):
        return None

    candidates: list[tuple[float, float, bool]] = []  # (bottom, top, is_window)
    for i in range(1, n):
        prev_c, prev_h, cur_l = cv[i - 1], hv[i - 1], lv[i]
        if not (math.isfinite(prev_c) and math.isfinite(prev_h) and math.isfinite(cur_l)):
            continue
        # true gap: today's low above yesterday's close
        if cur_l - prev_c >= signif:
            bottom, top, is_win = prev_c, cur_l, False
        # weaker wick window: today's low above yesterday's HIGH
        elif cur_l - prev_h >= signif:
            bottom, top, is_win = prev_h, cur_l, True
        else:
            continue
        # still UNFILLED: no LATER bar's low has traded back down into the void
        filled = any(lv[j] <= bottom for j in range(i + 1, n) if math.isfinite(lv[j]))
        if filled:
            continue
        candidates.append((bottom, top, is_win))
    if not candidates:
        return None
    # the to-the-penny trigger is the nearest gap whose bottom is at/above px (clear-sky
    # room overhead). If we're already inside / above all gaps, no usable trigger.
    above = [c for c in candidates if c[0] >= px]
    if not above:
        return None
    above.sort(key=lambda c: c[0])  # nearest first
    bottom, top, is_win = above[0]
    return {"gap_bottom": bottom, "gap_top": top, "is_window": is_win}


def _red_rejection_history(
    high: Any,
    low: Any,
    open_: Any | None,
    close: Any,
    *,
    level: float,
    atr_last: float,
    lookback: int,
) -> tuple[int, float]:
    """(B) Count + recency of large UPPER-WICK red rejection candles at/near ``level``.

    A rejection bar = a red (close < open, or close < prior-close when no open) daily
    candle whose HIGH reached the level (within ~0.25 ATR) but whose upper wick is large
    (wick >= ~half the bar range) — repeated sellers defending the same price. Returns
    ``(count, recency_frac)`` where recency_frac in [0,1] weights more-recent rejections
    heavier (linear by position in the window). (0, 0.0) on thin data — fail-open.
    """
    try:
        hv = [float(x) for x in high.values]
        lv = [float(x) for x in low.values]
        cv = [float(x) for x in close.values]
        ov = [float(x) for x in open_.values] if open_ is not None else None
    except Exception:
        return 0, 0.0
    n = len(hv)
    if n < 3 or not (math.isfinite(level) and level > 0 and atr_last and atr_last > 0):
        return 0, 0.0
    win = max(3, min(int(lookback), n - 1))
    tol = 0.25 * atr_last
    start = n - win
    count = 0
    recency_acc = 0.0
    for i in range(start, n):
        h, lo, c = hv[i], lv[i], cv[i]
        if not (math.isfinite(h) and math.isfinite(lo) and math.isfinite(c)) or h <= lo:
            continue
        # high tagged the level
        if abs(h - level) > tol:
            continue
        # red bar
        if ov is not None and math.isfinite(ov[i]):
            is_red = c < ov[i]
            body_top = max(ov[i], c)
        else:
            is_red = i > 0 and math.isfinite(cv[i - 1]) and c < cv[i - 1]
            body_top = c
        if not is_red:
            continue
        rng = h - lo
        upper_wick = h - body_top
        if rng > 0 and upper_wick >= 0.5 * rng:
            count += 1
            recency_acc += (i - start + 1) / float(win)  # later bars weigh more
    recency_frac = max(0.0, min(1.0, recency_acc / float(win))) if win > 0 else 0.0
    return count, recency_frac


def _blue_sky_recent_ipo(
    high: Any,
    *,
    px: float,
    atr_last: float,
    n_bars: int,
) -> tuple[bool, bool]:
    """(C) ``(is_blue_sky, is_recent_ipo)``.

    blue_sky = ``px`` within ~0.25 ATR of (or above) the max HIGH over ALL available
    history (a true all-time-high / multi-period-high break — no overhead supply at
    all). This is computed for EVERY name (an ESTABLISHED name at a true 52wk / all-
    time high with clear room is just as much a blue-sky break as a recent IPO — see
    ``compute_daily_context``); recent_ipo is the SUB-CASE flag. recent_ipo = the
    available daily history is shorter than ``_RECENT_IPO_MAX_BARS`` (~2 trading yrs),
    a proxy for a young listing with no trapped longs from a prior cycle. Fail-open to
    (False, False) on thin data.
    """
    try:
        hmax = max(float(x) for x in high.values if math.isfinite(float(x)))
    except (ValueError, TypeError):
        return False, False
    if not (math.isfinite(px) and px > 0 and math.isfinite(hmax) and hmax > 0):
        return False, False
    eps = 0.25 * atr_last if (atr_last and atr_last > 0) else 0.0
    is_blue_sky = px >= hmax - eps
    is_recent_ipo = 0 < int(n_bars) < _RECENT_IPO_MAX_BARS
    return bool(is_blue_sky), bool(is_recent_ipo)


def _day_number_in_run(
    close: Any,
    *,
    atr_last: float,
    max_bars: int = _SECOND_DAY_RUN_MAX_BARS,
) -> int | None:
    """(P1) Count the consecutive up/elevated daily bars ending at the most recent bar — the
    "day number" of a multi-day runner (1 = a fresh day-1 spike, 2 = day-2 continuation, …).

    Walk BACKWARD from the last bar: the run continues through bar ``i`` as long as the move
    has NOT yet stepped up out of its base BETWEEN ``i`` and ``i+1`` — i.e. the catalyst SPIKE
    (the day the price gapped/ran > 1 daily-ATR ABOVE the prior close) is the FIRST bar of the
    run, and the quiet pre-spike base is excluded. Stop the walk at the bar whose SUCCESSOR
    jumped > 1 ATR above it (that successor is day-1 of the run). Also stop on a > 1-ATR DOWN
    close (the move broke). So a fresh day-1 spike off a flat base reads run==1; each additional
    holding/up day adds 1 (a steep multi-day runner that steps up > 1 ATR/day still counts each
    leg because the step-UP only ENDS the walk at the base, it does not split the run). Bounded
    to ``max_bars`` (a day-7+ runner is just "extended"). Returns ``None`` on thin / non-finite
    data (fail-open ⇒ no tilt). Pure; never raises."""
    try:
        cv = [float(x) for x in close.values]
    except Exception:
        return None
    n = len(cv)
    if n < 2 or not (atr_last and math.isfinite(atr_last) and atr_last > 0):
        return None
    cap = max(1, int(max_bars))
    run = 1  # the most recent bar always anchors the run
    # step back: include bar i while the move did not start at i+1 (a > 1-ATR step UP from i to
    # i+1 = the catalyst spike, so i is the pre-spike base) and i did not break > 1 ATR DOWN.
    for i in range(n - 2, -1, -1):
        if run >= cap:
            break
        c_cur, c_next = cv[i], cv[i + 1]
        if not (math.isfinite(c_cur) and math.isfinite(c_next)):
            break
        if (c_next - c_cur) > atr_last:  # the move stepped up out of the base here — run starts at i+1
            break
        if (c_cur - c_next) > atr_last:  # a > 1-ATR down close — the run already broke before here
            break
        run += 1
    return int(run)


def _red_run_count(close: Any, *, max_bars: int = _RED_RUN_MAX_BARS) -> int | None:
    """(A7) Count the consecutive DOWN-close daily bars ending at YESTERDAY (the last COMPLETED
    bar excluding the still-forming intraday bar at index -1). Ross passes a name showing "four
    or five red candles in a row … price clearly coming down."

    Walk BACKWARD from bar -2 (yesterday): a bar is "red" when its close < the prior bar's close.
    Stop at the first non-red bar. Bounded to ``max_bars``. Returns 0 when yesterday was not a
    down-close, and ``None`` on thin / non-finite data (fail-open ⇒ no derate). Pure; never raises.
    """
    try:
        cv = [float(x) for x in close.values]
    except Exception:
        return None
    n = len(cv)
    if n < 3:
        return None  # need at least yesterday + a prior bar to compare (fail-open)
    cap = max(1, int(max_bars))
    run = 0
    # bar -1 is the still-forming intraday bar; the daily RED run ends at yesterday (index n-2).
    for i in range(n - 2, 0, -1):
        if run >= cap:
            break
        c_cur, c_prev = cv[i], cv[i - 1]
        if not (math.isfinite(c_cur) and math.isfinite(c_prev)):
            break
        if c_cur < c_prev:  # a down close vs the prior bar = a red daily
            run += 1
        else:
            break
    return int(run)


def _symbol_freshness(
    high: Any,
    low: Any,
    close: Any,
    volume: Any | None,
    *,
    explosive_floor_pct: float,
    recency_bars: int = _FRESHNESS_STALE_MAX_BARS,
    vol_window: int = 20,
) -> tuple[int | None, float | None, int]:
    """(A5) Two-signed symbol-freshness read from the daily OHLCV.

    Returns ``(days_since_last_explosive_move, trailing_dollar_vol_pctl, sign)`` where:
      * ``days_since_last_explosive_move`` = bars back (from the last COMPLETED bar) to the most
        recent daily |close-to-close move| >= ``explosive_floor_pct`` (the lane's OWN explosive
        change-pct floor — reused, no new number); ``None`` when there is none in the window.
      * ``trailing_dollar_vol_pctl`` = the trailing ``vol_window``-day mean $volume as a percentile
        vs the symbol's OWN trailing-year ($-vol per bar) history; ``None`` on thin/absent volume.
      * ``sign`` = +1 FRESH (no recent explosive move AND low prior $vol percentile — quiet, un-
        exhausted, room to run), -1 STALE (a recent explosive move that FADED back below its
        pre-move base — spike-and-fade), 0 neutral.

    Pure; fail-OPEN to ``(None, None, 0)`` on thin/NaN history (the module's neutral contract).
    """
    try:
        cv = [float(x) for x in close.values]
        hv = [float(x) for x in high.values]
        lv = [float(x) for x in low.values]
    except Exception:
        return (None, None, 0)
    n = len(cv)
    if n < 3 or not (explosive_floor_pct and math.isfinite(explosive_floor_pct) and explosive_floor_pct > 0):
        return (None, None, 0)
    floor_frac = float(explosive_floor_pct) / 100.0

    # (a) days since the last explosive daily move (scan back over the recency window from the
    #     last COMPLETED bar n-2; index -1 is the still-forming intraday bar). Also record the
    #     index of that explosive bar so the STALE-fade check can compare against its base.
    days_since: int | None = None
    explosive_idx: int | None = None
    cap = max(1, int(recency_bars))
    scanned = 0
    for i in range(n - 2, 0, -1):
        if scanned >= cap:
            break
        c_cur, c_prev = cv[i], cv[i - 1]
        if not (math.isfinite(c_cur) and math.isfinite(c_prev) and c_prev > 0):
            scanned += 1
            continue
        if abs(c_cur / c_prev - 1.0) >= floor_frac:
            days_since = (n - 2) - i  # 0 = yesterday itself was explosive
            explosive_idx = i
            break
        scanned += 1

    # (b) trailing-20d mean $vol as a percentile vs the symbol's own trailing-year $vol/bar.
    dv_pctl: float | None = None
    if volume is not None:
        try:
            vv = [float(x) for x in volume.values]
            dv = [cv[k] * vv[k] for k in range(n) if math.isfinite(cv[k]) and math.isfinite(vv[k]) and cv[k] > 0 and vv[k] >= 0]
            w = max(2, int(vol_window))
            if len(dv) >= w + 2:
                recent = dv[-w:]
                recent_mean = sum(recent) / float(len(recent))
                # own trailing-year baseline = per-bar $vol history (whatever the df carries, up to a year).
                base = dv[-252:] if len(dv) >= 252 else dv
                if base:
                    below = sum(1 for x in base if x <= recent_mean)
                    dv_pctl = max(0.0, min(1.0, below / float(len(base))))
        except Exception:
            dv_pctl = None

    # (c) two-signed sign. STALE dominates FRESH (a recent spike-and-fade is the stronger read).
    sign = 0
    # STALE: a recent explosive move that FADED — the current price closed back BELOW the base the
    # explosive bar launched FROM (its prior-bar close), i.e. the whole move was given back.
    if explosive_idx is not None and explosive_idx >= 1:
        try:
            pre_move_base = cv[explosive_idx - 1]
            last_completed = cv[n - 2]
            if math.isfinite(pre_move_base) and math.isfinite(last_completed) and pre_move_base > 0:
                if last_completed <= pre_move_base:
                    sign = -1
        except Exception:
            pass
    # FRESH: no recent explosive move in the window AND quiet trailing $vol (low own-year percentile).
    if sign == 0 and days_since is None and dv_pctl is not None and dv_pctl <= _FRESHNESS_LOW_VOL_PCTL:
        sign = 1
    return (days_since, dv_pctl, sign)


# ── ENTRY-PATH overhead-supply read (P0) ─────────────────────────────────────────
# The SELECTION tilts above re-rank a name; these two pure helpers let the ENTRY gate
# (entry_gates.py) read the SAME daily structure so a breakout TRIGGER is daily-aware:
#   • overhead_supply_atr — how far (in daily-ATR units) the NEAREST trapped-supply
#     level (prior swing high / unfilled-gap bottom / red-rejection cluster) sits ABOVE
#     a candidate entry; a small distance = a ceiling the price must fight through.
#   • entry_is_clear_sky — True only when there is NO overhead supply within the room
#     floor (a genuine blue-sky / clear-room break, never a mid-range break with a wall
#     just above). Both are ATR-relative (no fixed $), pure, and fail-OPEN.
# These read fields the DailyContext already carries — NO new fetch, NO new state.
def overhead_supply_atr(ctx: "DailyContext", *, entry: float) -> float | None:
    """Daily-ATR distance from ``entry`` UP to the nearest overhead-supply level the
    price must clear (min of: dist_to_resistance, the unfilled-gap bottom if it sits
    above, the swing-high). ``None`` when no level / no ATR read (clear sky / fail-open).

    The smaller this is, the closer a trapped-longs ceiling sits above the entry — the
    overhead veto rejects/derates a breakout whose nearest ceiling is within a documented
    ATR multiple. Pure; never raises (any error / missing input ⇒ None)."""
    try:
        if ctx is None or not (math.isfinite(entry) and entry > 0):
            return None
        atr_pct = ctx.daily_atr_pct
        if not (atr_pct and atr_pct > 0 and math.isfinite(atr_pct)):
            return None
        dists: list[float] = []
        # (a) the pre-computed nearest-resistance distance (prior-day high / swing / round#).
        d_res = ctx.dist_to_resistance_atr
        if d_res is not None and math.isfinite(d_res) and d_res < _CAP_ATR:
            dists.append(float(d_res))
        # (b) a swing high overhead (only when it is genuinely above the entry).
        sh = ctx.swing_high_nd
        if sh is not None and math.isfinite(sh) and sh > entry:
            dists.append((sh - entry) / entry / atr_pct)
        # (c) the nearest unfilled-gap BOTTOM overhead = the lower edge of a void the
        #     price must trade up THROUGH (the void's far side is supply, not clear sky).
        gb = ctx.nearest_unfilled_gap_bottom
        if gb is not None and math.isfinite(gb) and gb > entry:
            dists.append((gb - entry) / entry / atr_pct)
        if not dists:
            return None
        return max(0.0, min(d for d in dists if math.isfinite(d)))
    except Exception:
        return None


def entry_is_clear_sky(
    ctx: "DailyContext", *, entry: float, min_room_atr: float
) -> bool:
    """True only when the break at ``entry`` has CLEAR SKY overhead — a true new multi-
    period / all-time high (``is_blue_sky``) AND the nearest overhead-supply level (if
    any) sits at least ``min_room_atr`` daily-ATR away (clear room above the trigger).

    NEVER True on a mid-range break with a ceiling above (that is what the overhead veto
    rejects). Pure; fail-CLOSED to False on missing data (a blue-sky entry must be a
    POSITIVE, well-evidenced read — absence of data is not clear sky)."""
    try:
        if ctx is None or not bool(ctx.is_blue_sky):
            return False
        room = overhead_supply_atr(ctx, entry=entry)
        # No overhead-supply level found AND we are at a fresh high ⇒ genuine clear sky.
        if room is None:
            return True
        return float(room) >= float(min_room_atr)
    except Exception:
        return False


def compute_daily_context(
    df: Any,
    *,
    lookback: int = 20,
    price: float | None = None,
    gap_geometry_tilt: bool = False,
    red_rejection_derate: bool = False,
    blue_sky_recent_ipo: bool = False,
    fresh_catalyst: bool = False,
    entry_context: bool = False,
    second_day_context: bool | None = None,
    symbol_freshness_tilt: bool | None = None,
) -> DailyContext:
    """Daily context from a daily OHLCV df (cols Open/High/Low/Close/Volume).

    Fail-OPEN to the neutral _NULL (trend_score=0.5, all-None, daily_structure_pct=None)
    on thin/short/NaN data so every consumer degrades to "no tilt, no gate" — never a
    crash, never a block. ``price`` overrides the last daily close (use the live price).

    Three SOFT daily-structure tilts fold INTO ``daily_structure_pct`` (the existing
    selection sub-score) when their flag arg is True — flag-off (all three default False)
    OR input-absent ⇒ the exact same ``ds`` as before (byte-identical):
      (A) ``gap_geometry_tilt``    — UP-weight a break that opens into an unfilled gap +
          a room_to_gap_top feature; surfaces nearest_unfilled_gap_bottom / gap_top.
      (B) ``red_rejection_derate`` — soft DE-RATE a level with a history of upper-wick red
          rejections (repeated sellers); ``fresh_catalyst`` overrides the de-rate.
      (C) ``blue_sky_recent_ipo``  — BOOST an all-time-high break ONLY for recent IPOs
          (short history ⇒ no trapped overhead supply).
    All are bounded re-ranks of the [0,1] sub-score — never a veto, never block a fill.

    ``entry_context`` (P0): when True, the structural READS (gap geometry, red-rejection
    count, is_blue_sky/recent-IPO) are ALWAYS computed and surfaced on the returned
    DailyContext — INDEPENDENT of the three selection-tilt flags above — so the ENTRY
    gate can read overhead supply / clear-sky for ALL names. It NEVER alters ``ds`` (the
    selection sub-score): the boost/derate to ``ds`` stays gated on the tilt flags, so
    the SELECTION path (which never passes ``entry_context``) is byte-identical.

    ``second_day_context`` (P1; flag ``chili_momentum_second_day_context_enabled``): when
    True (``None`` ⇒ resolve the live config flag lazily so the existing caller auto-opts-
    in without a signature change), compute ``day_number_in_run`` / ``prior_day_close`` /
    ``prior_week_high`` and fold a bounded continuation tilt INTO ``ds``: BOOST a clean
    DAY-2 (holding above prior_day_high / prior_day_close), DERATE day-3+ (exhaustion).
    OFF (flag false) ⇒ the run/level fields are still surfaced for log/audit but ``ds`` is
    left byte-identical (the boost/derate is gated on the flag). A re-rank tilt, never a gate.

    ``symbol_freshness_tilt`` (A5; flag ``chili_momentum_symbol_freshness_tilt_enabled``): when
    True (``None`` ⇒ resolve the live flag lazily), fold a bounded TWO-SIGNED tilt INTO ``ds`` —
    FRESH (no recent explosive daily move AND low trailing-20d $vol percentile vs the symbol's own
    trailing year) ⇒ +tilt; STALE (a recent explosive daily move that FADED back below its base)
    ⇒ -tilt. The freshness fields are surfaced for audit regardless; the ds mutation is gated on
    the flag. A re-rank tilt, never a gate; fail-open-to-neutral on thin/NaN history.

    (A7) CONSECUTIVE-RED-DAILIES SOFT DE-RANK: folds under the EXISTING
    ``red_rejection_derate`` flag (no new flag). When that flag is on, a name with N consecutive
    down-closes ending yesterday gets a SOFT de-rate scaled by run length (never a veto). The
    ``red_run_count`` field is surfaced for audit regardless.
    """
    if second_day_context is None:
        try:
            from app.config import settings as _settings  # local import: no top-level dep
            second_day_context = bool(getattr(_settings, "chili_momentum_second_day_context_enabled", False))
        except Exception:
            second_day_context = False
    if symbol_freshness_tilt is None:
        try:
            from app.config import settings as _settings2  # local import: no top-level dep
            symbol_freshness_tilt = bool(getattr(_settings2, "chili_momentum_symbol_freshness_tilt_enabled", True))
        except Exception:
            symbol_freshness_tilt = False
    n = max(2, int(lookback))
    try:
        if df is None or len(df) < n + 2:
            return _NULL
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        # at least half the window must be real (non-zero-volume) bars — kills the
        # "structure from a handful of gappy bars is noise" overfit.
        if "Volume" in df.columns:
            vol = df["Volume"].astype(float)
            if int((vol.tail(n) > 0).sum()) < n // 2:
                return _NULL
        px = float(price) if (price is not None and price > 0) else float(close.iloc[-1])
        if not (math.isfinite(px) and px > 0):
            return _NULL

        atr = compute_atr(high, low, close)
        atr_last = float(atr.iloc[-1])
        last_close = float(close.iloc[-1])
        if not (math.isfinite(atr_last) and last_close > 0):
            return _NULL
        atr_pct = atr_last / last_close
        if not (math.isfinite(atr_pct) and atr_pct > 0):
            return _NULL

        pdh = float(high.iloc[-2])
        pdl = float(low.iloc[-2])
        sh = float(high.tail(n).max())
        sl = float(low.tail(n).min())
        rn = _round_number_near(px, atr_pct)

        # nearest major level above / below px (prior-day H/L + N-day swing + round#)
        above = [lv for lv in (pdh, sh, rn) if lv is not None and lv > px]
        below = [lv for lv in (pdl, sl, rn) if lv is not None and lv < px]
        d_res = (min(above) - px) / px / atr_pct if above else _CAP_ATR  # clear sky => cap
        d_sup = (px - max(below)) / px / atr_pct if below else _CAP_ATR

        breaking = bool(px > pdh or px > sh)

        # SOFT broader-trend read in [0,1] — EXCLUDES rvol/gap (those are the momentum
        # pillar; this is purely daily structure, so no double-count). A downtrending
        # gapper lands LOW but NEVER zero.
        ema9 = close.ewm(span=9, adjust=False).mean()
        above_ema = float((close.tail(n) > ema9.tail(n)).mean())
        hv = high.values
        lv2 = low.values
        start = max(1, len(hv) - n)
        hh_hl = sum(1 for i in range(start, len(hv)) if hv[i] > hv[i - 1] and lv2[i] > lv2[i - 1])
        hh_hl_frac = hh_hl / float(n)
        try:
            ret = float(close.iloc[-1] / close.iloc[-n] - 1.0)
        except (IndexError, ZeroDivisionError):
            ret = 0.0
        ret_score = max(0.0, min(1.0, 0.5 + ret * 2.5))  # +20%/N => ~1.0, -20% => ~0
        # Ross macro-trend: the 200-day SMA (needs >=200 daily bars; fail-open to a
        # NEUTRAL 0.5 macro read otherwise so a fresh listing is never penalized). A
        # SOFT minority input to the broader-trend read — above the 200-SMA is bullish
        # macro context, below is caution; it NEVER hard-filters (a news-gap spike below
        # its 200-SMA breaking a level still scores HIGH — the CUPR guarantee).
        sma200 = above200 = d200 = None
        macro = 0.5
        if len(close) >= 200:
            try:
                _s200 = float(close.rolling(200).mean().iloc[-1])
                if math.isfinite(_s200) and _s200 > 0:
                    sma200 = _s200
                    above200 = bool(px > _s200)
                    d200 = (px - _s200) / px / atr_pct      # signed daily-ATR units
                    macro = 1.0 if above200 else 0.0
            except Exception:
                sma200 = above200 = d200 = None
        trend = max(0.0, min(1.0, (above_ema + hh_hl_frac + ret_score + macro) / 4.0))

        # the SELECTION sub-score (the only decision-affecting output): break ABOVE a
        # major level dominates; jammed-under-resistance (small ceiling) scores low;
        # trend is a 15% MINORITY input so a downtrending news-gapper is not penalized
        # into oblivion (the CUPR guarantee).
        room_below = max(0.0, min(1.0, d_sup / _CAP_ATR))
        ceiling = max(0.0, min(1.0, d_res / _CAP_ATR))
        brk = 1.0 if breaking else 0.0
        ds = max(0.0, min(1.0, 0.40 * brk + 0.30 * ceiling + 0.15 * room_below + 0.15 * trend))

        # ── SOFT daily-structure tilts (each ADDITIVE: flag-off OR signal-absent ⇒ the
        #    pre-tilt ``ds`` above is returned unchanged). All bounded re-ranks of the
        #    [0,1] sub-score; the entry gate is never touched. ───────────────────────
        gap_bottom = gap_top = room_to_gap_top = is_window = opens_into_gap = None
        rej_count = rej_recency = None
        is_blue = trade_days = is_ipo = None

        # (A) GAP/WINDOW GEOMETRY — a break that OPENS INTO an unfilled gap has clear sky
        # overhead (no resistance to chew through); UP-weight it + the room_to_gap_top.
        # entry_context (P0) computes the SAME reads for the entry gate but never mutates ds.
        if gap_geometry_tilt or entry_context:
            try:
                _open = df["Open"].astype(float) if "Open" in df.columns else None
                g = detect_open_gaps(high, low, close, _open, px=px, atr_last=atr_last)
                if g is not None:
                    gap_bottom = float(g["gap_bottom"])
                    gap_top = float(g["gap_top"])
                    is_window = bool(g["is_window"])
                    # clear-sky room from the trigger edge to the gap ceiling, ATR units.
                    room_to_gap_top = max(0.0, (gap_top - gap_bottom) / px / atr_pct) if (px > 0 and atr_pct > 0) else 0.0
                    # a break "opens into" the gap when px is at/through the to-the-penny
                    # bottom; a true gap weighs full, a wick-window is the weaker class.
                    opens_into_gap = bool(px >= gap_bottom)
                    room_norm = max(0.0, min(1.0, room_to_gap_top / _CAP_ATR))
                    cls = 0.5 if is_window else 1.0   # windows are the weaker signal
                    # gate the boost on actually breaking up (don't reward a far-below name).
                    gap_boost = 0.10 * cls * room_norm * (1.0 if (opens_into_gap and brk > 0) else 0.5)
                    if gap_geometry_tilt:  # SELECTION effect ONLY when the tilt flag is on
                        ds = max(0.0, min(1.0, ds + gap_boost))
            except Exception:
                pass

        # (B) RED-REJECTION HISTORY — repeated upper-wick red rejections at the nearest
        # resistance = sellers defending the price; soft DE-RATE (overridable by a strong
        # fresh catalyst, which can flip a defended level into a squeeze). entry_context
        # (P0) computes the count for the entry gate's overhead read but never mutates ds.
        if red_rejection_derate or entry_context:
            try:
                _lvl = min(above) if above else (sh if sh and sh >= px else None)
                if _lvl is not None:
                    _open = df["Open"].astype(float) if "Open" in df.columns else None
                    rej_count, rej_recency = _red_rejection_history(
                        high, low, _open, close, level=float(_lvl), atr_last=atr_last, lookback=n,
                    )
                    # SELECTION de-rate ONLY when the tilt flag is on AND no fresh catalyst
                    # overrides it (a real news squeeze blows through a defended level).
                    if red_rejection_derate and not fresh_catalyst and rej_count and rej_count > 0:
                        # adaptive: scale by how many + how recent; cap so it never zeroes.
                        cnt_norm = min(1.0, rej_count / float(max(1, n // 4)))
                        derate = 0.15 * cnt_norm * max(0.25, rej_recency)
                        ds = max(0.0, min(1.0, ds * (1.0 - derate)))
            except Exception:
                pass

        # (C) BLUE-SKY / RECENT-IPO — an all-time-high break with NO trapped longs above
        # (recent IPO) is the cleanest breakout there is; BOOST it. Gated to recent IPOs:
        # an old name at ATH still has cycle-trapped supply nearby and gets no boost.
        # entry_context (P0): compute is_blue_sky for ALL names (an ESTABLISHED name at a
        # true 52wk / all-time high with clear room is a blue-sky break too) but mutate ds
        # only under the original recent-IPO-gated SELECTION boost (byte-identical there).
        if blue_sky_recent_ipo or entry_context:
            try:
                trade_days = int(len(high))
                is_blue, is_ipo = _blue_sky_recent_ipo(high, px=px, atr_last=atr_last, n_bars=trade_days)
                if blue_sky_recent_ipo and is_blue and is_ipo and brk > 0:
                    ds = max(0.0, min(1.0, ds + 0.12))
            except Exception:
                pass

        # (P1) SECOND-DAY / MULTI-DAY CONTINUATION — count the consecutive up/elevated daily
        # bars since the catalyst spike, surface prior_day_close + prior_week_high, and fold a
        # bounded continuation tilt into ``ds``: BOOST a clean DAY-2 holding above the prior-day
        # high / prior-day close (the textbook Ross continuation), DERATE day-3+ (exhaustion).
        # The run/level fields are surfaced for log/audit regardless; the ds mutation is gated on
        # the flag (OFF ⇒ byte-identical). A re-rank tilt, never a gate.
        day_num = pday_close = pweek_high = None
        try:
            day_num = _day_number_in_run(close, atr_last=atr_last)
            pday_close = float(close.iloc[-2])
            # prior-week high = highest HIGH over the trailing ~5 daily bars EXCLUDING today.
            _wk = high.iloc[-6:-1] if len(high) >= 6 else high.iloc[:-1]
            if len(_wk) > 0:
                pweek_high = float(_wk.max())
            if second_day_context and day_num is not None:
                if day_num == 2:
                    # clean day-2 boost — gated on actually HOLDING the move (price at/above the
                    # prior-day high OR the prior-day close = continuation structure, not a fade).
                    _holding = (px >= pdh) or (pday_close is not None and px >= pday_close)
                    if _holding:
                        ds = max(0.0, min(1.0, ds + _SECOND_DAY_TILT))
                elif day_num >= 3:
                    # day-3+ derate (exhaustion); symmetric magnitude, multiplicative so it
                    # never zeroes a still-breaking name (the CUPR floor holds).
                    ds = max(0.0, min(1.0, ds * (1.0 - _SECOND_DAY_TILT)))
        except Exception:
            pass

        # (A5) SYMBOL-FRESHNESS TWO-SIGNED TILT — Ross prefers a FRESH name ("recent reverse
        # split, no big days of volume recently" — CLRO) and passes a STALE one ("made a huge
        # move and then sold off" — DSY). Reuses the lane's OWN explosive change-pct floor (no
        # new number). The freshness fields are surfaced for audit regardless; the ds mutation is
        # gated on the flag (OFF ⇒ byte-identical). A re-rank tilt, never a gate; fail-open-neutral.
        days_since_explosive = tdv_pctl = fresh_sign = None
        try:
            from .ross_momentum import ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT as _EXP_FLOOR
        except Exception:
            _EXP_FLOOR = 10.0
        try:
            _vol_series = df["Volume"].astype(float) if "Volume" in df.columns else None
            days_since_explosive, tdv_pctl, fresh_sign = _symbol_freshness(
                high, low, close, _vol_series, explosive_floor_pct=float(_EXP_FLOOR), vol_window=n,
            )
            if symbol_freshness_tilt and fresh_sign:
                if fresh_sign > 0:  # FRESH — quiet, un-exhausted, room to run
                    ds = max(0.0, min(1.0, ds + _FRESHNESS_TILT))
                elif fresh_sign < 0:  # STALE spike-and-fade — multiplicative derate (never zeroes)
                    ds = max(0.0, min(1.0, ds * (1.0 - _FRESHNESS_TILT)))
        except Exception:
            pass

        # (A7) CONSECUTIVE-RED-DAILIES SOFT DE-RANK — Ross passes "four or five red candles in a
        # row … price clearly coming down." SOFT selection de-rate scaled by run length; NEVER an
        # entry veto (the module docstring guarantee keeps day-2 gapper winners tradable). Folds
        # under the EXISTING red_rejection_derate flag (no new flag). The count is surfaced for
        # audit regardless; the ds mutation is gated on that flag. Fail-open-neutral on thin history.
        red_run = None
        try:
            red_run = _red_run_count(close)
            if red_rejection_derate and red_run and red_run >= 2:
                # adaptive: scale by how long the run is vs the window; cap so it never zeroes.
                run_norm = min(1.0, red_run / float(max(1, n // 4)))
                red_derate = _RED_REJECTION_DERATE_BASE * run_norm
                ds = max(0.0, min(1.0, ds * (1.0 - red_derate)))
        except Exception:
            pass

        return DailyContext(
            prior_day_high=pdh, prior_day_low=pdl, swing_high_nd=sh, swing_low_nd=sl,
            daily_atr_pct=atr_pct, dist_to_resistance_atr=d_res, dist_to_support_atr=d_sup,
            near_round_number=rn, trend_score=trend, breaking_major_level=breaking,
            daily_structure_pct=ds, reason="ok",
            sma_200=sma200, above_sma_200=above200, dist_to_sma_200_atr=d200,
            nearest_unfilled_gap_bottom=gap_bottom, gap_top=gap_top, is_window=is_window,
            room_to_gap_top_atr=room_to_gap_top, opens_into_gap=opens_into_gap,
            rejection_count=rej_count, rejection_recency_frac=rej_recency,
            is_blue_sky=is_blue, trading_history_days=trade_days, is_recent_ipo=is_ipo,
            day_number_in_run=day_num, prior_day_close=pday_close, prior_week_high=pweek_high,
            days_since_last_explosive_move=days_since_explosive,
            trailing_dollar_vol_pctl=tdv_pctl, freshness_tilt_sign=fresh_sign,
            red_run_count=red_run,
        )
    except Exception:
        return _NULL
