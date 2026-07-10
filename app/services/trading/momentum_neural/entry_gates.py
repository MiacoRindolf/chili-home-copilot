"""Pre-entry gates: ScanPattern DSL, momentum/volume, regime filters (autopilot profitability)."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumStrategyVariant, ScanPattern
from ..indicator_core import compute_all_from_df
from ..market_data import fetch_ohlcv_df
from ..pattern_engine import _eval_condition
from ..regime import inject_regime_into_indicators
from .family_regime_stats import family_regime_prefilter_allows

_log = logging.getLogger(__name__)


def _tape_asof_default(as_of):
    """Resolve the tape-read "now" anchor through live_runner's replay-aware clock
    chokepoint when the caller didn't thread one (2026-07-09). LIVE: ``_utcnow()``
    IS naive wall-UTC, so the bounded as-of SQL form is byte-identical to the old
    wall-``now()`` branch. FSM REPLAY: ``_utcnow()`` is the sim clock, so the read
    no longer anchors at wall time (empty window / foreign post-sim ticks). Same
    helper as pipeline._tape_asof_default (kept local: pipeline lazily imports
    this module, so importing back at module level would be circular)."""
    if as_of is not None:
        return as_of
    try:
        from .live_runner import _utcnow as _lr_utcnow

        return _lr_utcnow()
    except Exception:
        from datetime import datetime as _dt

        return _dt.utcnow()


def _compute_confirmed_swing_low_last(df: pd.DataFrame, lookback: int = 10) -> float | None:
    """Most recent confirmed swing low at last bar (aligned with backtest BOS)."""
    if df is None or len(df) < 2 * lookback + 2:
        return None
    lows = df["Low"].astype(float).values
    n = len(lows)
    last_confirmed: float | None = None
    confirm_bar = n - 1
    if confirm_bar < 2 * lookback:
        return None
    for cb in range(2 * lookback, n):
        candidate = cb - lookback
        window_start = max(0, candidate - lookback)
        window_end = min(n, candidate + lookback + 1)
        if lows[candidate] <= lows[window_start:window_end].min():
            last_confirmed = float(lows[candidate])
    return last_confirmed


def bos_exit_triggered_long(df: pd.DataFrame, *, current_close: float, buffer_pct: float = 0.003) -> bool:
    """True if close is below last confirmed swing low (minus buffer)."""
    swing = _compute_confirmed_swing_low_last(df, lookback=10)
    if swing is None or swing <= 0 or current_close <= 0:
        return False
    threshold = swing * (1.0 - float(buffer_pct))
    return float(current_close) < threshold


def _last_indicator_row(df: pd.DataFrame, needed: set[str]) -> dict[str, Any]:
    """Latest bar as flat indicator dict for pattern_engine."""
    arrays = compute_all_from_df(df, needed=needed)
    out: dict[str, Any] = {}
    n = len(df)
    if n <= 0:
        return out
    idx = n - 1
    for k, series in arrays.items():
        if not isinstance(series, list) or idx >= len(series):
            continue
        v = series[idx]
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = v
    out["price"] = float(df["Close"].astype(float).iloc[-1])
    return inject_regime_into_indicators(out)


def evaluate_pattern_conditions_for_variant(
    db: Session,
    variant: MomentumStrategyVariant | None,
    df: pd.DataFrame,
) -> tuple[bool, str]:
    """All rules_json conditions must pass on latest bar; skip if no linked pattern."""
    if variant is None or not getattr(variant, "scan_pattern_id", None):
        return True, "no_scan_pattern_skip"
    pid = int(variant.scan_pattern_id)
    pat = db.query(ScanPattern).filter(ScanPattern.id == pid).one_or_none()
    if pat is None:
        return True, "pattern_missing_skip"
    try:
        if isinstance(pat.rules_json, str):
            rules = json.loads(pat.rules_json)
        else:
            rules = dict(pat.rules_json or {})
    except (json.JSONDecodeError, TypeError):
        return False, "pattern_rules_invalid"
    conditions = rules.get("conditions") or []
    if not conditions:
        return True, "empty_conditions_skip"
    needed: set[str] = set()
    for cond in conditions:
        if isinstance(cond, dict):
            ik = cond.get("indicator")
            if ik:
                needed.add(str(ik))
            ref = cond.get("ref")
            if ref:
                needed.add(str(ref))
    if not needed:
        return True, "no_indicators_skip"
    ind = _last_indicator_row(df, needed)
    for cond in conditions:
        if not isinstance(cond, dict):
            return False, "bad_condition_shape"
        if not _eval_condition(cond, ind):
            return False, "pattern_conditions_not_met"
    return True, "pattern_ok"


def momentum_volume_confirmation(df: pd.DataFrame) -> tuple[bool, str]:
    """Price above EMA-9 and volume above 1.5x recent average (last bar)."""
    if df is None or len(df) < 25:
        return False, "insufficient_bars"
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    arrays = compute_all_from_df(df, needed={"ema_9", "volume_ratio"})
    ema9 = arrays.get("ema_9") or []
    vr = arrays.get("volume_ratio") or []
    idx = len(df) - 1
    price = float(close.iloc[idx])
    ema = ema9[idx] if idx < len(ema9) and ema9[idx] is not None else None
    if ema is None:
        win = vol.tail(20)
        avg_v = float(win.iloc[:-1].mean()) if len(win) > 1 else float(vol.iloc[-1])
        cur_v = float(vol.iloc[-1])
        if not math.isfinite(avg_v) or avg_v <= 0:
            return False, "volume_avg_zero"
        if not math.isfinite(cur_v) or cur_v < 1.5 * avg_v:
            return False, "volume_below_1p5x_avg"
        prev = float(close.iloc[-2]) if len(close) > 1 else price
        if price <= prev:
            return False, "momentum_fallback_no_uptick"
        return True, "momentum_fallback_bar_only"
    ema_f = float(ema)
    if price <= ema_f:
        return False, "price_below_ema9"
    vr_v = vr[idx] if idx < len(vr) and vr[idx] is not None else None
    if vr_v is not None and float(vr_v) >= 1.5:
        return True, "momentum_ok_rel_vol"
    win = vol.tail(21)
    if len(win) < 5:
        return False, "volume_window_short"
    avg_v = float(win.iloc[:-1].mean())
    cur_v = float(vol.iloc[-1])
    if not math.isfinite(avg_v) or avg_v <= 0:
        return False, "volume_avg_zero"
    if not math.isfinite(cur_v) or cur_v < 1.5 * avg_v:
        return False, "volume_below_1p5x_avg"
    return True, "momentum_ok_abs_vol"


def _sustained_rvol(vr: list[Any], cur: int, lookback: int) -> float | None:
    """Mean per-bar relative-volume over the last ``lookback`` bars (incl. current).

    ``vr`` is the ``volume_ratio`` series (each bar's volume / its trailing average),
    so this is inherently self-relative per instrument — an adaptive RVOL, not a
    fixed share count. Returns ``None`` when fewer than 2 valid samples exist so the
    caller can fail OPEN on thin data rather than block a real setup.
    """
    start = max(0, cur - max(1, int(lookback)) + 1)
    ratios: list[float] = []
    for i in range(start, cur + 1):
        v = vr[i] if 0 <= i < len(vr) else None
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:  # not NaN
            ratios.append(fv)
    if len(ratios) < 2:
        return None
    return sum(ratios) / len(ratios)


def _sustained_rvol_excluding_coil(
    vr: list[Any], atr: list[Any], high: Any, low: Any, cur: int, lookback: int,
    *, coil_range_atr_frac: float = 0.5,
) -> float | None:
    """CAPTURE-G2: mean per-bar rel-vol over the last ``lookback`` bars but DROPPING the
    identified low-range COIL bars — a bar whose range (``high-low``) is below
    ``coil_range_atr_frac`` x its ATR is a structural coil bar (a dead, tight consolidation
    candle), not an active trading bar, and its depressed rel-vol should not count against a
    genuine break's sustain. This is the tape-faithful "compute the sustain mean EXCLUDING
    coil bars" recovery for a dry-coil premarket break (JEM 06-30 class): as real volume
    returns, the ACTIVE-bar mean clears the floor even while the coil-inclusive mean is still
    depressed. Returns ``None`` on < 2 remaining active samples (⇒ the caller keeps the
    coil-inclusive gate — never a free pass on a genuinely quiet tape). Pure; no I/O.

    ``high`` / ``low`` are the pandas Close/High/Low series the caller already holds (indexed
    by bar position); ``atr`` is the ATR array from ``compute_all_from_df``.
    """
    start = max(0, cur - max(1, int(lookback)) + 1)
    ratios: list[float] = []
    try:
        _frac = float(coil_range_atr_frac)
    except (TypeError, ValueError):
        _frac = 0.5
    if _frac < 0.0:
        _frac = 0.0
    for i in range(start, cur + 1):
        # identify + DROP a low-range coil bar (range < frac x ATR). A missing/zero ATR
        # cannot classify the bar as coil ⇒ it is KEPT (fail-toward the stricter inclusive mean).
        try:
            _a = float(atr[i]) if (0 <= i < len(atr) and atr[i] is not None) else None
        except (TypeError, ValueError):
            _a = None
        try:
            _rng = float(high.iloc[i]) - float(low.iloc[i])
        except (TypeError, ValueError, IndexError):
            _rng = None
        if _a is not None and _a > 0 and _rng is not None and _frac > 0 and _rng < _frac * _a:
            continue  # coil bar — exclude from the active-bar mean
        v = vr[i] if 0 <= i < len(vr) else None
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:  # not NaN
            ratios.append(fv)
    if len(ratios) < 2:
        return None
    return sum(ratios) / len(ratios)


# ── Volatility-aware pullback validity ────────────────────────────────────────
# The lane now SELECTS explosive low-float small-caps (universe profile #531/#533),
# but the pullback-validity checks were tuned for orderly large-caps: a flat 50%
# shallowness cap, a 0.1% EMA-9 wick buffer and a 0.2% retest tolerance rejected
# ~99% of these names' bars (backtest 2026-06-08: 3 fires across 10 small-caps; 0
# trades on INHD +1700%). These names wick + pull DEEPER in absolute terms while
# still printing a clean Ross flag — so each tolerance scales with the instrument's
# OWN ATR%: a calm name keeps the tight Ross floor, a volatile small-cap gets
# proportional room. No fixed per-name magic; the discipline (shallow pull that
# holds the 9-EMA, retested, broken on volume) is unchanged — only its yardstick is
# now volatility-relative. Floors/ceiling are Ross-discipline guards, the single
# documented place to tune. docs/DESIGN/MOMENTUM_LANE.md
_VOL_SHALLOW_BASE = 0.50        # calm-name "shallow" retrace cap (Ross floor)
_VOL_SHALLOW_CEIL = 0.75        # never deeper than this — beyond is a reversal, not a pullback
_VOL_SHALLOW_ATR_MULT = 1.5     # widen the shallow cap by this x ATR%
_VOL_EMA_WICK_FLOOR = 0.001     # min EMA-9 wick tolerance (the original 0.1%)
_VOL_EMA_WICK_ATR_MULT = 0.5    # tolerate a wick this x ATR% below the 9-EMA
_VOL_RETEST_TOL_ATR_MULT = 0.3  # retest dip/hold tolerance scales this x ATR%


def _vol_aware_pullback_tolerances(
    atr_pct: float | None, base_retrace: float
) -> tuple[float, float, float]:
    """``(shallow_retrace_cap, ema9_wick_tol, retest_tol)`` scaled by the
    instrument's ATR%. ``atr_pct=None``/0 → Ross floors (backward-compatible: the
    shallow cap = ``base_retrace``, the EMA-9 buffer = 0.1%, no extra retest room),
    so calm names behave exactly as before and only volatile small-caps get room.
    """
    a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
    shallow = min(_VOL_SHALLOW_CEIL, float(base_retrace) + a * _VOL_SHALLOW_ATR_MULT)
    ema_wick = max(_VOL_EMA_WICK_FLOOR, a * _VOL_EMA_WICK_ATR_MULT)
    retest = a * _VOL_RETEST_TOL_ATR_MULT
    return shallow, ema_wick, retest


# Wait reasons whose ``debug["pullback_high"]`` is a LIVE tick-watchable level:
# the live runner stashes it as ``watch_break_level`` (tick-speed dispatch), the
# tick-break branch fires through it mid-bar, and the replay engine mirrors both.
# ONE constant for all three call sites — growing the tuple in only one of them
# is how the EDHL disarm bug (2026-06-11) happened.
TICK_ARMED_WAIT_REASONS = (
    "waiting_for_break",
    "waiting_for_reclaim",
    "waiting_for_reclaim_high",
    "waiting_for_dipbuy_break",
    "waiting_for_first_pullback_break",
    # HVM101 (C): the VWAP-reclaim trigger emits this while price is still below VWAP
    # but the K-below structure is in place — arm tick-speed dispatch on the reclaim.
    "waiting_for_vwap_reclaim",
    # SS101 #012: the bull-flag break trigger emits this while the swing high is
    # unbroken on a completed bar -- arm tick-speed dispatch on the live-ask break.
    "waiting_for_bull_flag_break",
)


def _collapse_cap(atr_pct: float | None) -> float:
    """Volatility-relative max retrace depth that still reads as a PULLBACK —
    beyond it the move is a breakdown, not a dip to buy. Shared yardstick of the
    halt-resume dip trigger and the deep-retrace reclaim path (same Ross mechanic:
    buy the dip's reclaim, never a collapse)."""
    return min(0.25, max(0.06, 6.0 * (atr_pct or 0.01)))


def _adaptive_pullback_depth_ceiling(atr_pct: float | None, enabled: bool) -> float:
    """Adaptive Ross retrace ceiling on the IMPULSE-relative axis (not the absolute
    depth axis owned by ``_collapse_cap``). When ``enabled`` is False (default), returns
    ``0.0`` -> callers fall through to their existing ceiling, BYTE-IDENTICAL behaviour.

    THE TRAP (docs/STRATEGY: documented 0-fills/fewer-fills regression): tightening the
    pullback-depth ceiling toward Ross's ~50%-of-prior-candle WITHOUT adapting cuts the
    EXPLOSIVE low-float names whose normal pull is deeper (INHD-like). So the tightening
    is CALM-ONLY: the ~0.50 base is the FLOOR of the ceiling for a calm name (ATR% ~ 0),
    and it WIDENS with the instrument's OWN ATR% so a volatile name keeps the current
    (deeper) tolerance.

    Reuses the ONE documented base (``_VOL_SHALLOW_BASE``/``_VOL_SHALLOW_ATR_MULT``,
    line ~192) -- same formula as ``_vol_aware_pullback_tolerances`` shallow return -- so
    there is a single yardstick to tune. Hard-capped at ``_VOL_SHALLOW_CEIL`` (0.75);
    never exceeds it. No fixed per-name magic.

    Worked points (matches LOCATE): calm ``atr_pct=0.01`` -> ~0.515 (tighter than the
    current 0.70 ceiling -> a calm name's deeper-than-~50% pull is now REJECTED);
    explosive ``atr_pct=0.05`` -> ~0.575 (the same deep pull on a volatile name PASSES);
    ``atr_pct=0.50`` -> 0.75 hard cap (never beyond)."""
    if not enabled:
        return 0.0
    a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
    return min(_VOL_SHALLOW_CEIL, _VOL_SHALLOW_BASE + a * _VOL_SHALLOW_ATR_MULT)


def _is_first_pullback(
    lo: Any, ema9: list, anchor: int, peak_idx: int, ema_wick: float
) -> bool:
    """True if the run INTO ``peak_idx`` held the 9-EMA band the WHOLE way — i.e.
    THIS pull off the peak is the FIRST pullback, with no earlier dip below the
    (vol-aware) 9-EMA between ``anchor`` and the peak. A later/Nth pullback (an
    earlier band-losing dip already happened) is chop — Ross buys only the first.

    Pure + side-effect-free. ``lo`` is the low array (numpy or a list of floats),
    ``ema9`` the per-bar 9-EMA series (``None`` entries skipped, same fail-open
    semantics as the inline check it was extracted from), ``ema_wick`` the
    vol-aware wick tolerance. Identical body to the loop in ``_dipbuy_signals_ok``."""
    for i in range(anchor, peak_idx):
        e = ema9[i] if (0 <= i < len(ema9) and ema9[i] is not None) else None
        if e is not None and float(lo[i]) < float(e) * (1.0 - ema_wick):
            return False
    return True


# Pullback ordinal (Ross gap #7, videos 04/15/17/24/26): the 1st/2nd pullback is an
# A-setup; by the 3rd you are greedy and it usually fails (head-and-shoulders top). A
# 3rd+-pullback break is treated as a WEAKER prior (raised volume floor, like runaway /
# deep-reclaim), so the weak ones filter out and a genuinely strong 3rd still fires.
# Documented bases (the de-rate ordinal + the bounded lookback), no scattered magic.
_LATE_PULLBACK_ORDINAL = 3
_PULLBACK_ORDINAL_LOOKBACK = 20


def pullback_ordinal_recent(
    low: Any, ema9: list, cur: int, ema_wick: float, lookback: int = _PULLBACK_ORDINAL_LOOKBACK
) -> int:
    """Count of distinct band-losing PULLBACKS in the last ``lookback`` bars up to ``cur``
    (1 = the current dip is the first pullback in the window, 2 = second, ...). A pullback
    is a contiguous run of bars whose low dips below the vol-aware 9-EMA band; consecutive
    below-band bars count as ONE event. Pure + bounded-window (no impulse-origin state to
    key across the runner's per-tick re-eval). Fails OPEN to 1 (treat as the first
    pullback — never over-throttle) on thin/missing data."""
    try:
        start = max(0, int(cur) - int(lookback) + 1)
        count = 0
        below_prev = False
        for i in range(start, int(cur) + 1):
            e = ema9[i] if (0 <= i < len(ema9) and ema9[i] is not None) else None
            lo = low[i] if (0 <= i < len(low) and low[i] is not None) else None
            below = e is not None and lo is not None and float(lo) < float(e) * (1.0 - float(ema_wick))
            if below and not below_prev:
                count += 1
            below_prev = below
        return max(1, count)
    except (TypeError, ValueError, IndexError):
        return 1


# How many recent bars to scan for a MACD line->below->signal cross when reading the
# back side (secondary to the dominant 9<20-EMA structural flip). A documented base
# constant, not a magic number scattered at the call site.
_BACKSIDE_MACD_CROSS_LOOKBACK = 3


def _detect_back_side(
    ema9: list, ema20: list, macd: list, macd_sig: list, cur: int, *, macd_lookback: int = 3
) -> tuple[bool, str]:
    """Ross's front/back-side read (gap #1): ``(True, reason)`` when the move has
    rolled to the BACK side — where he STOPS taking continuation entries. Two
    persistent signals, fail-OPEN (``False``) on any missing/short data so a thin
    series can never veto:

      (a) STRUCTURAL flip — the 9-EMA is below the 20-EMA at the current bar (the
          classic trend-rollover line Ross watches on the 1m);
      (b) MOMENTUM rollover — the MACD line crossed below its signal within the last
          ``macd_lookback`` bars AND is STILL below now (a single stale cross with the
          line already back above is ignored, so a brief dip doesn't bench the name).

    Pure + side-effect-free; reads the exact series ``pullback_break_confirmation``
    already computes. ``cur`` is the current (last) bar index."""
    # (a) 9-EMA below 20-EMA — structural back side (dominant signal).
    try:
        e9 = ema9[cur] if 0 <= cur < len(ema9) else None
        e20 = ema20[cur] if 0 <= cur < len(ema20) else None
        if e9 is not None and e20 is not None and float(e9) < float(e20):
            return True, "ema9_below_ema20"
    except (TypeError, ValueError, IndexError):
        pass
    # (b) MACD crossed below signal within the lookback AND still below now.
    try:
        m_cur = macd[cur] if 0 <= cur < len(macd) else None
        s_cur = macd_sig[cur] if 0 <= cur < len(macd_sig) else None
        if m_cur is not None and s_cur is not None and float(m_cur) < float(s_cur):
            lo_i = max(1, cur - int(macd_lookback) + 1)
            for i in range(lo_i, cur + 1):
                mp = macd[i - 1] if 0 <= i - 1 < len(macd) else None
                sp = macd_sig[i - 1] if 0 <= i - 1 < len(macd_sig) else None
                mi = macd[i] if 0 <= i < len(macd) else None
                si = macd_sig[i] if 0 <= i < len(macd_sig) else None
                if mp is None or sp is None or mi is None or si is None:
                    continue
                if float(mp) >= float(sp) and float(mi) < float(si):
                    return True, "macd_crossed_below_signal"
    except (TypeError, ValueError, IndexError):
        pass
    return False, ""


def _doji_trigger_veto(
    o: float, h: float, l: float, c: float, *, atr_pct: float | None, base_body_frac: float,
) -> tuple[bool, dict[str, Any]]:
    """DOJI VETO (candle-quality gate): the TRIGGER candle is a true DOJI — a weak body
    relative to its range = indecision, not a conviction break. Returns ``(veto, debug)``;
    ``veto=True`` means BLOCK the entry (a doji broke the level).

    ADAPTIVE threshold (no fixed magic): a candle is a doji when ``body/range`` falls below
    ``base_body_frac + atr_pct`` — the ONE documented base (``base_body_frac`` = 0.25, the
    calm-name indecision floor) WIDENS with the instrument's volatility so an explosive
    high-ATR name (whose normal bars carry larger wicks) is not over-restricted. ``atr_pct``
    None -> just the base (Ross floor).

    OVERRIDE: a STRONG full-body commitment candle ALWAYS passes regardless of the body/range
    ratio — green (close >= open), closing in the upper half of its range, with a non-dominant
    upper wick (reuses ``is_strong_bull_break_candle``). This keeps a wide-range conviction
    candle (whose body may be a smaller fraction of a very tall range) from being mislabeled a
    doji.

    FAIL-SAFE: a zero-range / unreadable bar -> ``veto=False`` (never block on unreadable data).
    Range-relative, pure, side-effect-free."""
    from .candles import _ohlc, is_strong_bull_break_candle

    dbg: dict[str, Any] = {}
    try:
        rng, body, _upper, _lower = _ohlc(o, h, l, c)
        if rng <= 0:
            return False, dbg  # unreadable bar -> never block (fail-safe)
        body_frac = body / rng
        thresh = float(base_body_frac) + max(0.0, float(atr_pct or 0.0))
        dbg["doji_body_frac"] = round(body_frac, 4)
        dbg["doji_threshold"] = round(thresh, 4)
        if body_frac >= thresh:
            return False, dbg  # full enough body -> not a doji
        # Body is thin in fraction terms, BUT a strong full-body commitment candle still
        # passes (a tall-range conviction bar can have a small body FRACTION yet be a real
        # break). Only veto when it is NOT that conviction shape.
        if is_strong_bull_break_candle(o, h, l, c):
            dbg["doji_override"] = "strong_full_body"
            return False, dbg
        return True, dbg
    except (TypeError, ValueError):
        return False, dbg  # bad inputs -> fail-open (never block on a bug)


def _resample_htf(df: pd.DataFrame, rule: str = "5min") -> pd.DataFrame | None:
    """Resample a 1m OHLCV frame to a higher timeframe (default 5m) for the HTF-against
    read — NO NEW FEED, the HTF is DERIVED from the 1m df the lane already supplies. Requires
    a DatetimeIndex (the live runner / replay always pass one); returns ``None`` when the
    index is not datetime or the frame is too thin to resample (-> caller fails open). Pure."""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 2:
            return None
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            return None
        cols = {x.lower(): x for x in df.columns}
        agg = {}
        if "open" in cols:
            agg[cols["open"]] = "first"
        if "high" in cols:
            agg[cols["high"]] = "max"
        if "low" in cols:
            agg[cols["low"]] = "min"
        if "close" in cols:
            agg[cols["close"]] = "last"
        if "volume" in cols:
            agg[cols["volume"]] = "sum"
        if "close" not in cols:
            return None
        htf = df.resample(rule).agg(agg).dropna(how="any")
        if htf is None or getattr(htf, "empty", True) or len(htf) < 2:
            return None
        return htf
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _htf_against_veto(
    df: pd.DataFrame, *, rule: str = "5min", macd_threshold: float = 0.0,
    rolldown_bars: int = 3,
) -> tuple[bool, dict[str, Any]]:
    """MULTI-TF ALIGNMENT veto: the HIGHER TF (5m, resampled from the 1m ``df`` — no new
    feed) is CLEARLY AGAINST the long. Returns ``(veto, debug)``; ``veto=True`` means BLOCK.

    ⚠️ THE TRAP this gate is built to AVOID: requiring full multi-TF alignment breaks Ross's
    1m-FAST geometry (the 1m leads, the HTF lags). So this fires ONLY when the HTF is CLEARLY
    bearish/rolling-down — NEVER when it is merely neutral/lagging (not yet up but not down),
    and NEVER on a single lagging EMA down-tick (a slow 5m EMA dips for ONE sample off a flush
    while the 1m has already turned up — that is the dip-rip/VWAP-reclaim the lane catches).

    CLEARLY AGAINST (veto) — reuses the SAME EMA/MACD-rollover structure as the 1m
    ``_detect_back_side``, applied to the 5m arrays, but demanding a SUSTAINED (not single-bar)
    deterioration:
      (a) 5m EMA-9 SUSTAINED roll-down — strictly lower across EACH of the last ``rolldown_bars``
          samples (a multi-bar negative slope, ONE documented base = 3 samples / 2 consecutive
          down steps). A single lagging down-tick does NOT count; OR
      (b) 5m MACD histogram clearly PEAKED — ``hist[-1] < hist[-2] >= hist[-3]`` with
          ``hist[-2] > macd_threshold`` (an up-impulse that has topped and rolled over).

    NEUTRAL/LAGGING (PASS) — EMA-9 flat/rising, EMA-9 dipping for only a single sample, MACD
    histogram rising/flat/near-zero, or too few HTF bars to read a trend: all PASS (no
    over-restriction on the lagging HTF). An aligned-UP HTF (EMA rising) also passes.

    FAIL-OPEN: non-datetime index / thin HTF / missing arrays / any error -> ``veto=False``
    (a missing HTF feed can NEVER block a valid 1m-fast entry). Pure, side-effect-free."""
    dbg: dict[str, Any] = {}
    try:
        htf = _resample_htf(df, rule=rule)
        if htf is None:
            return False, dbg  # cannot read HTF -> fail-open (1m-fast preserved)
        arrays = compute_all_from_df(htf, needed={"ema_9", "macd", "macd_signal", "macd_hist"})
        ema9 = arrays.get("ema_9") or []
        hist = arrays.get("macd_hist") or []
        hcur = len(htf) - 1
        # (a) 5m EMA-9 SUSTAINED roll-down — strictly lower across EACH of the last N samples
        # (a multi-bar negative slope), NOT a single lagging down-tick. Needs N+1 EMA samples
        # to read N consecutive steps; too few -> no read (neutral, PASS).
        try:
            _n = max(2, int(rolldown_bars))
            e_cur = ema9[hcur] if 0 <= hcur < len(ema9) else None
            e_prev = ema9[hcur - 1] if 0 <= hcur - 1 < len(ema9) else None
            if e_cur is not None and e_prev is not None:
                dbg["htf_ema9_slope"] = round(float(e_cur) - float(e_prev), 6)
            if hcur - (_n - 1) >= 0 and len(ema9) > hcur:
                _window = [ema9[hcur - k] for k in range(_n)]  # newest -> oldest
                if all(x is not None for x in _window):
                    _vals = [float(x) for x in _window]
                    # newest strictly below next-older at EVERY step = sustained down-slope.
                    _sustained = all(_vals[k] < _vals[k + 1] for k in range(_n - 1))
                    dbg["htf_ema9_rolldown_bars"] = _n
                    if _sustained:
                        dbg["htf_against"] = "ema9_sustained_rolldown"
                        return True, dbg
        except (TypeError, ValueError, IndexError):
            pass
        # (b) 5m MACD histogram clearly PEAKED (positive-then-declining rollover).
        try:
            if len(hist) >= 3:
                h0 = hist[hcur] if hist[hcur] is not None else None
                h1 = hist[hcur - 1] if hist[hcur - 1] is not None else None
                h2 = hist[hcur - 2] if hist[hcur - 2] is not None else None
                if h0 is not None and h1 is not None and h2 is not None:
                    h0, h1, h2 = float(h0), float(h1), float(h2)
                    dbg["htf_macd_hist"] = [round(h2, 6), round(h1, 6), round(h0, 6)]
                    if (h0 < h1) and (h1 >= h2) and (h1 > float(macd_threshold)):
                        dbg["htf_against"] = "macd_peaked"
                        return True, dbg
        except (TypeError, ValueError, IndexError):
            pass
        # Neither rolling-down nor peaked -> neutral/lagging or aligned-up -> PASS.
        return False, dbg
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):
        return False, dbg  # any error -> fail-open (never block a valid 1m-fast entry)


# LULD halt-band proximity (re-analysis survivor S3, video 60): buying a dip whose STOP
# sits inside (or just above) the LULD down-halt band risks getting halt-TRAPPED mid-
# cascade — the stock halts on a further drop and the position can't be exited at the stop
# (an asymmetric small-cap tail loss). A protective veto (can never create a bad fill).
_HALT_BAND_K = 0.5  # the stop must clear the band by at least this fraction of the risk


def luld_down_band(price: float) -> float:
    """Approximate Reg-LULD LOWER price band for a stock (small-caps are Tier 2). A
    downward move through this triggers a 5-min halt. Tiered by price; the first/last-15min
    DOUBLING is omitted (fail-SAFE — omitting it only ever vetoes LESS). 0 on bad input."""
    p = float(price or 0.0)
    if p <= 0:
        return 0.0
    if p >= 3.0:
        pct = 0.10
    elif p >= 0.75:
        pct = 0.20
    else:
        pct = min(0.75, 0.15 / p)
    return p * (1.0 - pct)


def halt_band_trapped(entry: float, stop: float, *, k: float = _HALT_BAND_K) -> bool:
    """True when the dip-buy STOP sits at/below the LULD down-halt band (or within ``k`` x
    the risk distance of it): a further drop would HALT the stock and trap the position.
    Adaptive (band is %-of-price, the buffer is k x the trade's own risk — no magic $).
    Pure; fail-open to ``False``."""
    try:
        band = luld_down_band(entry)
        risk = float(entry) - float(stop)
        if band <= 0 or risk <= 0:
            return False
        return float(stop) <= band + float(k) * risk
    except (TypeError, ValueError):
        return False


def _dip_buy_in_rth_window(
    *, now: Any, bar_ts: Any, symbol: str | None, settings_obj: Any = settings,
) -> tuple[bool, str]:
    """GAP 4 (Warrior re-audit): the flush-dip / deep-reclaim DIP-BUY only works in REGULAR
    trading hours (09:30-16:00 ET) because stops only fire then — premarket has NO stops, so
    a premarket dip-buy that breaks down cannot be exited at its structural stop (an
    asymmetric tail loss). Returns ``(in_window, reason)``.

    FAIL-OPEN when the gate is OFF / the asset is crypto (24/7, no session) / no usable clock
    is available — so a missing timestamp can NEVER manufacture a block (it can only ever
    constrain a dip-buy to RTH when the flag is ON and a real clock is present). The clock is
    ``now`` (the live wall clock) when supplied, else the latest ``bar_ts`` (the completed bar
    that is the dip/curl). Pure; never raises."""
    if not bool(getattr(settings_obj, "chili_momentum_dip_buy_rth_only_enabled", False)):
        return True, "rth_gate_disabled"  # OFF -> never constrain (byte-identical)
    # CRYPTO PARITY: a 24/7 asset has no 09:30 open; the gate is an EQUITY concept.
    if bool(symbol) and str(symbol).upper().endswith("-USD"):
        return True, "rth_crypto_exempt"
    _clock = now if now is not None else bar_ts
    if _clock is None:
        return True, "rth_no_clock"  # no usable clock -> fail-OPEN (never block on a miss)
    try:
        from zoneinfo import ZoneInfo

        _ts = pd.Timestamp(_clock)
        _ts = _ts.tz_localize("UTC") if _ts.tzinfo is None else _ts
        _et = _ts.tz_convert(ZoneInfo("America/New_York"))
        _hod = _et.hour + _et.minute / 60.0 + _et.second / 3600.0
        _start = float(getattr(settings_obj, "chili_momentum_dip_buy_rth_start_hour", 9.5) or 9.5)
        _end = float(getattr(settings_obj, "chili_momentum_dip_buy_rth_end_hour", 16.0) or 16.0)
        if _start <= _hod < _end:
            return True, "rth_in_window"
        return False, "rth_only_outside_window"
    except Exception:
        return True, "rth_clock_error"  # any parse error -> fail-OPEN (never block on a bug)


def add_into_halt_ok(
    *,
    avg_entry: float | None,
    original_stop: float | None,
    current_stop: float | None,
    bid: float | None,
    is_limit_up_halt: bool,
    in_rth: bool,
    tape_confirmed: bool | None = None,
    breakout_level: float | None = None,
    atr_pct: float | None = None,
    df: Any = None,
    consecutive_halt_up_count: int | None = None,
    halt_level: float | None = None,
    resumption_open: float | None = None,
    settings_obj: Any = settings,
) -> tuple[bool, str, dict[str, Any]]:
    """GAP 6 (Warrior re-audit, RISKIEST — default OFF): the EXTRA-GUARDED predicate that
    permits a SMALL pyramid ADD while a name is HALTED LIMIT-UP. EVERY condition must hold;
    FAIL-CLOSED on any miss (a missing input ⇒ no add). Pure; never raises.

    ADD-INTO-HALT is LOSS-SENSITIVE: you cannot exit a halted name, so a bad add is
    dangerous. It therefore carries ALL the breakout-entry chase guards PLUS the now-live
    Cluster-A halt-family context (the same gates the entry path uses — REUSED here, never
    duplicated). Every chase guard fails-CLOSED on a missing input.

    Conditions (ALL required):
      (1)  flag ON (``chili_momentum_add_into_halt_enabled``);
      (2)  the halt is LIMIT-UP / bullish (``is_limit_up_halt``) — NEVER a limit-down halt;
      (3)  RTH (``in_rth``) — halts/resumes only matter in regular hours;
      (4)  ALREADY IN PROFIT by >= ``chili_momentum_add_into_halt_min_profit_r`` of the entry
           risk R = (avg_entry − original_stop): ``bid >= avg_entry + min_profit_r · R``
           (NEVER add if underwater — the profit-first rule);
      (5)  the ORIGINAL STRUCTURAL STOP is intact (``current_stop`` has not LOOSENED below
           ``original_stop`` — a stop can only tighten; if it moved DOWN, structure changed,
           refuse). The structural stop on the ADDED shares = this same intact stop.

    CHASE GUARDS (each fail-CLOSED on missing data — a missing input ⇒ NO add):
      (T)  TAPE REQUIRED: ``tape_confirmed`` must be explicitly True. None / False ⇒ no add
           (you do not add into a halt without confirming the tape is lifting).
      (E)  EXTENSION VETO / NOT-PARABOLIC: reuse ``_entry_extension_veto`` — if the add price
           (``bid``) sits too far above the breakout level for the name's ATR, it is a blow-off
           top, refuse. Requires ``breakout_level`` + ``atr_pct`` (missing ⇒ fail-closed here).
      (B)  NOT-BACKSIDE / ABOVE-VWAP: reuse ``_detect_back_side`` (1m EMA/MACD rollover) +
           ``front_side_state`` (below-VWAP / faded / rolled-over top) on ``df``. Backside or
           below VWAP ⇒ refuse. Missing/thin ``df`` ⇒ fail-closed here.

    HALT-FAMILY CONTEXT — evaluated DIRECTLY on the raw halt signals under the MASTER
    flag, INDEPENDENT of the standalone Cluster-A sub-flags (``halt_chain_risk_gate_
    enabled`` / ``halt_resumption_direction_enabled`` / ``false_halt_avoid_enabled``).
    Those sub-flags govern the PRIMARY-ENTRY path; a halt-ADD is loss-sensitive and must
    self-enforce the halt context whenever the master flag is ON — otherwise a sub-flag-OFF
    lane would silently fail-OPEN and add into a blow-off it could not exit. FAIL-CLOSED on
    any MISSING halt signal:
      (H1) HALT-CHAIN risk: if ``consecutive_halt_up_count`` >= ``chili_momentum_halt_
           chain_block_count`` (an extended consecutive-halt-up blow-off), refuse the add.
           (Reuses the same block-count setting — no new magic. The de-weight branch only
           shrinks size on the primary path; it never refuses an add.)
      (H2) HALT-RESUMPTION DIRECTION: a ``halt_level`` is REQUIRED. The resume must NOT be
           unfavorable — ``resumption_open`` must NOT be below ``halt_level``. A lower resume
           ⇒ refuse. A MISSING ``halt_level`` ⇒ fail-closed (``add_into_halt_no_halt_signal``);
           a missing ``resumption_open`` ⇒ fail-closed (``add_into_halt_no_resumption``).
      (H3) FALSE-HALT AVOID: a WEAK resume (``resumption_open`` below ``halt_level``) is a
           false halt ⇒ refuse (same predicate as H2 — a sub-threshold resume is unfavorable).

    Returns ``(ok, reason, debug)``. ``ok=True`` ONLY when every leg passes. The add SIZE is
    NOT decided here — the existing pyramid sizing + ``chili_momentum_pyramid_max_adds`` cap
    bound it; this gate only PERMITS. Default OFF ⇒ ``(False, "...disabled", {})`` before any
    compute = byte-identical."""
    dbg: dict[str, Any] = {}
    try:
        if not bool(getattr(settings_obj, "chili_momentum_add_into_halt_enabled", False)):
            return False, "add_into_halt_disabled", dbg
        if not is_limit_up_halt:
            return False, "add_into_halt_not_limit_up", dbg
        if not in_rth:
            return False, "add_into_halt_not_rth", dbg
        if avg_entry is None or original_stop is None or bid is None:
            return False, "add_into_halt_missing_inputs", dbg  # fail-CLOSED
        a = float(avg_entry)
        os_ = float(original_stop)
        b = float(bid)
        risk = a - os_
        if not (a > 0 and risk > 0 and b > 0):
            return False, "add_into_halt_bad_inputs", dbg
        min_r = float(getattr(settings_obj, "chili_momentum_add_into_halt_min_profit_r", 1.0) or 1.0)
        profit_r = (b - a) / risk if risk > 0 else 0.0
        dbg.update({
            "avg_entry": round(a, 6), "original_stop": round(os_, 6),
            "bid": round(b, 6), "profit_r": round(profit_r, 3), "min_profit_r": min_r,
        })

        # ── (T) TAPE REQUIRED + fail-CLOSED ────────────────────────────────────────────
        # No add into a halt without an explicit tape confirmation. None (no tape read) or
        # False (tape not lifting) ⇒ refuse. This runs FIRST among the risk legs so a
        # tape-less halt can never even reach the profit/structure checks.
        if tape_confirmed is not True:
            dbg["tape_confirmed"] = tape_confirmed
            return False, "add_into_halt_no_tape", dbg

        # (4) PROFIT-FIRST: never add unless sufficiently in the green.
        if profit_r < min_r:
            return False, "add_into_halt_insufficient_profit", dbg
        # (5) STRUCTURAL STOP INTACT: the current stop must NOT be below the original
        # (a stop only ever tightens; a looser stop = structure changed, refuse). The
        # structural stop carried on the ADDED shares is THIS intact stop.
        _add_stop = os_
        if current_stop is not None:
            try:
                _cs = float(current_stop)
                if _cs < os_ - 1e-9:
                    dbg["current_stop"] = round(_cs, 6)
                    return False, "add_into_halt_stop_loosened", dbg
                _add_stop = _cs  # the (possibly tightened) live stop bounds the added shares
            except (TypeError, ValueError):
                return False, "add_into_halt_bad_stop", dbg
        dbg["add_structural_stop"] = round(_add_stop, 6)

        # ── (E) EXTENSION VETO / NOT-PARABOLIC ─────────────────────────────────────────
        # Reuse the entry-extension chase guard: the add price (bid) must NOT sit too far
        # above the breakout level for the name's ATR. Missing level/atr ⇒ fail-CLOSED
        # (we will NOT add into a halt without the data to prove it is not extended).
        if breakout_level is None or atr_pct is None:
            dbg["breakout_level"] = breakout_level
            dbg["atr_pct"] = atr_pct
            return False, "add_into_halt_no_extension_inputs", dbg
        if _entry_extension_veto(b, float(breakout_level), float(atr_pct), settings_obj):
            dbg["breakout_level"] = round(float(breakout_level), 6)
            dbg["atr_pct"] = round(float(atr_pct), 6)
            return False, "add_into_halt_extended", dbg

        # ── (B) NOT-BACKSIDE / ABOVE-VWAP ──────────────────────────────────────────────
        # Reuse _detect_back_side (1m EMA/MACD rollover) + front_side_state (below-VWAP /
        # faded / rolled-over top). Missing/thin df ⇒ fail-CLOSED (no add without the
        # structure to prove front-side).
        if df is None or getattr(df, "empty", True) or len(df) < 5:
            return False, "add_into_halt_no_structure", dbg
        try:
            _arrays = compute_all_from_df(
                df, needed={"ema_9", "ema_20", "macd", "macd_signal"}
            )
            _ema9 = _arrays.get("ema_9") or []
            _ema20 = _arrays.get("ema_20") or []
            _macd = _arrays.get("macd") or []
            _macd_sig = _arrays.get("macd_signal") or []
            _cur = len(df) - 1
            _bs, _bs_reason = _detect_back_side(
                _ema9, _ema20, _macd, _macd_sig, _cur,
                macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
            )
            if _bs:
                dbg["back_side"] = _bs_reason
                return False, "add_into_halt_back_side", dbg
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            dbg["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                dbg["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "add_into_halt_backside_lifecycle", dbg
        except Exception:
            # thin / degenerate frame ⇒ fail-CLOSED for this loss-sensitive add.
            return False, "add_into_halt_structure_error", dbg

        # ── (H1) HALT-CHAIN risk — evaluate the RAW chain count DIRECTLY under the MASTER ─
        # flag, INDEPENDENT of chili_momentum_halt_chain_risk_gate_enabled. add-into-halt is
        # loss-sensitive: the standalone risk-gate's flag controls the *primary-entry* path,
        # but a halt-ADD must self-enforce the chain block whenever the master flag is ON —
        # otherwise a sub-flag-OFF lane would silently skip H1 (fail-OPEN) and add into an
        # over-extended halt-chain blow-off. Reuse the SAME block-count setting (no new
        # magic). At/above the block count ⇒ refuse. Below it ⇒ pass (the de-weight branch
        # only shrinks size on the primary path; it never refuses an add).
        try:
            _hc_count = int(consecutive_halt_up_count or 0)
            _hc_block_at = max(2, int(getattr(settings_obj, "chili_momentum_halt_chain_block_count", 3) or 3))
            dbg["consecutive_halt_up"] = _hc_count
            dbg["halt_chain_block_count"] = _hc_block_at
            if _hc_count >= _hc_block_at:
                return False, "add_into_halt_halt_chain_blocked", dbg
        except (TypeError, ValueError):
            return False, "add_into_halt_halt_chain_error", dbg

        # ── (H2/H3) HALT-RESUMPTION DIRECTION + FALSE-HALT — evaluate DIRECTLY on the raw ─
        # halt_level / resumption_open under the MASTER flag, INDEPENDENT of the resumption-
        # direction / false-halt sub-flags. A halt-ADD must CONFIRM the resume was favorable
        # (resumption_open NOT below halt_level); an unfavorable / weak resume is a false
        # halt ⇒ refuse. FAIL-CLOSED on a MISSING halt signal: no halt_level ⇒ we cannot
        # prove the resume direction at all (add_into_halt_no_halt_signal); a halt_level but
        # no resumption_open ⇒ we cannot confirm the resume (add_into_halt_no_resumption).
        # We will NOT add into a halt we cannot confirm resumed favorably.
        if halt_level is None:
            return False, "add_into_halt_no_halt_signal", dbg
        try:
            _hl = float(halt_level)
        except (TypeError, ValueError):
            return False, "add_into_halt_bad_halt_level", dbg
        if not (_hl > 0):
            return False, "add_into_halt_no_halt_signal", dbg
        dbg["halt_level"] = round(_hl, 6)
        if resumption_open is None:
            return False, "add_into_halt_no_resumption", dbg
        try:
            _ro = float(resumption_open)
        except (TypeError, ValueError):
            return False, "add_into_halt_bad_resumption", dbg
        dbg["resumption_open"] = round(_ro, 6)
        if _ro < _hl * (1.0 - 1e-9):
            # below the halt level on the resume = unfavorable / false halt.
            return False, "add_into_halt_unfavorable_resumption", dbg

        return True, "add_into_halt_ok", dbg
    except Exception:
        return False, "add_into_halt_error", dbg  # any error -> fail-CLOSED


def halt_chain_risk_gate(
    *,
    consecutive_halt_up_count: int | None,
    settings_obj: Any = settings,
) -> tuple[bool, float, str, dict[str, Any]]:
    """GAP 1 (Warrior re-audit): the HALT-CHAIN risk predicate for a halt-resume-dip long.

    A name that keeps halting UP again and again is climbing the LULD ladder — each
    successive limit-up halt-resume long is later / more extended / sharper to unwind.
    Given the PER-SYMBOL consecutive halt-UP count, returns ``(block, size_mult, reason,
    debug)``:

      * ``block=True``  — at/above ``chili_momentum_halt_chain_block_count`` ⇒ BLOCK the
        halt-resume long entirely (turn a would-fire into a no-fire).
      * ``block=False`` + ``size_mult<1.0`` — below the block but on a chain (count>=2):
        DE-WEIGHT linearly toward the block so the size shrinks as the chain extends.
      * ``block=False`` + ``size_mult=1.0`` — count 0/1 (no chain) ⇒ no change.

    RISK-REDUCING ONLY: it can only ever BLOCK or SHRINK; ``size_mult`` is always in
    ``[lo, 1.0]`` and never exceeds 1.0. Pure; never raises. Default OFF
    (``chili_momentum_halt_chain_risk_gate_enabled``) ⇒ ``(False, 1.0, "disabled", {})``
    before any compute = byte-identical."""
    dbg: dict[str, Any] = {}
    try:
        if not bool(getattr(settings_obj, "chili_momentum_halt_chain_risk_gate_enabled", False)):
            return False, 1.0, "halt_chain_gate_disabled", dbg
        cnt = int(consecutive_halt_up_count or 0)
        block_at = int(getattr(settings_obj, "chili_momentum_halt_chain_block_count", 3) or 3)
        block_at = max(2, block_at)
        dbg.update({"consecutive_halt_up": cnt, "block_count": block_at})
        if cnt >= block_at:
            return True, 1.0, "halt_chain_blocked", dbg
        # De-weight linearly from the 2nd halt up toward the block: at count==1 (the
        # first halt up) full size; at count==block_at-1 the most-shrunk pre-block size.
        if cnt >= 2 and block_at > 2:
            # fraction of the way from the 1st halt-up (full) to the block (most shrunk).
            frac = (cnt - 1) / (block_at - 1)
            mult = max(0.5, 1.0 - 0.5 * frac)  # floor at 0.5x; never below
            dbg["size_mult"] = round(mult, 4)
            return False, mult, "halt_chain_deweighted", dbg
        return False, 1.0, "halt_chain_ok", dbg
    except Exception:
        return False, 1.0, "halt_chain_error", dbg  # fail-open: never blocks on a bug


def _dipbuy_signals_ok(
    high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series, vwap: list,
    *, peak_idx: int, dip_idx: int, dip_low: float, run_high: float, depth: float,
    cur: int, w_start: int, atr_pct: float | None, tol: float, ema_wick: float,
    ema9: list, symbol: str | None, opn: pd.Series | None = None,
    db: Any = None, l2_as_of: Any = None,
) -> tuple[str, float | None, float | None, dict[str, Any]]:
    """Ross "buy the FIRST reversal off the dip" gate — the EARLY deep-reclaim entry.

    Returns ``(verdict, level, stop, patch)`` with ``verdict`` in:
      * ``"FIRE"`` — a completed bar reversed THROUGH the dip bar's OWN pullback high
        on a strong close with returning volume → enter at ``level`` (the pullback
        high, NEAR the dip — EARLIER than the recovery swing high the legacy path uses).
      * ``"ARM"``  — the context + volume-dry-up signals + structure are valid but the
        pullback high has not broken on a completed bar yet → arm a tick-watch at
        ``level`` (the live ask through it fires the tick path; the closed-bar volume-
        return leg is waived there and the dipbuy tick thrust buffer compensates).
      * ``"PASS"`` — any decline / thin data / unaffordable runway → caller falls
        through to the EXISTING recovery-high reclaim BYTE-IDENTICALLY (fail-open).

    Pure + NEVER raises (any error → PASS). The stop is the dip-low ANCHOR only; the
    authoritative ``structural_or_vol_floored_atr_pct`` layer widens it to the vol
    floor downstream (INVARIANT A lives there, identically for live/paper/replay), so
    this gate must NOT pre-floor it. ``vwap`` is the 20-bar ROLLING proxy
    (indicator_core.compute_vwap) → a trend-MA slope, a directional filter only; the
    knife discrimination leans on HH/HL + volume dry-up/return + a strong reversal
    close. ONE adaptive base = the slope lookback; the rest are Ross-discipline
    floors. docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        if not bool(getattr(settings, "chili_momentum_deep_reclaim_dipbuy_enabled", True)):
            return "PASS", None, None, {"dipbuy_declined": "disabled"}
        if cur <= peak_idx or dip_idx <= peak_idx or dip_idx >= cur:
            return "PASS", None, None, {"dipbuy_declined": "bad_indices"}
        L = int(getattr(settings, "chili_momentum_deep_reclaim_dipbuy_vwap_lookback", 12) or 12)
        K = int(getattr(settings, "chili_momentum_deep_reclaim_dipbuy_pullback_bars", 3) or 3)
        dryup = float(getattr(settings, "chili_momentum_deep_reclaim_dipbuy_dryup_ratio", 0.85) or 0.85)
        buf_bps = float(getattr(settings, "chili_momentum_deep_reclaim_dipbuy_stop_buffer_bps", 10.0) or 0.0)
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0

        # SHALLOWNESS (research: a 2-3 red-candle pullback, not a long grind down).
        if (dip_idx - peak_idx) > (K + 1):
            return "PASS", None, None, {"dipbuy_declined": "not_shallow"}

        hi = high.values
        lo = low.values
        cl = close.values

        # ── SIGNAL 1: rising trend (VWAP-proxy slope) + intact HH/HL + first pullback + runway
        # (a) slope > 0 over L bars (least squares on the non-NaN VWAP-proxy points).
        i0 = max(0, cur - L)
        xs: list[float] = []
        ys: list[float] = []
        for i in range(i0, cur + 1):
            v = vwap[i] if (0 <= i < len(vwap)) else None
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv == fv:  # not NaN
                xs.append(float(i))
                ys.append(fv)
        if len(ys) < max(4, L // 2):
            return "PASS", None, None, {"dipbuy_declined": "vwap_warmup"}
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        var = sum((x - mx) ** 2 for x in xs)
        if var <= 0 or (cov / var) <= 0:
            return "PASS", None, None, {"dipbuy_declined": "vwap_not_rising"}
        # (b) HH/HL intact: the dip's peak is a higher HIGH and the dip a higher LOW
        #     than the prior structure window.
        anchor = max(w_start, peak_idx - L)
        if anchor < peak_idx:
            prior_hi = float(hi[anchor:peak_idx].max())
            prior_lo = float(lo[anchor:peak_idx].min())
            if not (run_high > prior_hi and dip_low > prior_lo):
                return "PASS", None, None, {"dipbuy_declined": "hh_hl_broken"}
            # (c) FIRST pullback: the run INTO the peak held the 9-EMA band (no earlier dip).
            if not _is_first_pullback(lo, ema9, anchor, peak_idx, ema_wick):
                return "PASS", None, None, {"dipbuy_declined": "not_first_pullback"}
        # (d) Runway clear: no completed-bar high since the dip overhangs the target (run_high).
        post_hi = float(hi[dip_idx + 1:cur + 1].max()) if cur > dip_idx else float(hi[cur])
        if run_high < post_hi * (1.0 - tol):
            return "PASS", None, None, {"dipbuy_declined": "overhead_resistance"}

        # ── SIGNAL 2: volume DRY-UP on the dip, then volume RETURN on the trigger ──
        vv = vol.values if hasattr(vol, "values") else None
        if vv is None:
            return "PASS", None, None, {"dipbuy_declined": "no_volume"}
        P = max(2, dip_idx - peak_idx)
        push_lo = max(w_start, peak_idx - P) + 1
        dip_v = [float(vv[i]) for i in range(peak_idx + 1, dip_idx + 1) if i < len(vv)]
        push_v = [float(vv[i]) for i in range(push_lo, peak_idx + 1) if i < len(vv)]
        if len(dip_v) < 1 or len(push_v) < 2:
            return "PASS", None, None, {"dipbuy_declined": "thin_volume_window"}
        if any(x != x for x in dip_v + push_v):  # NaN-safe
            return "PASS", None, None, {"dipbuy_declined": "volume_nan"}
        dip_vm = sum(dip_v) / len(dip_v)
        push_vm = sum(push_v) / len(push_v)
        if push_vm <= 0:
            return "PASS", None, None, {"dipbuy_declined": "zero_push_volume"}

        # ── Gate 2b (dip-buy quality, flag-gated): impulse-ACCUMULATION confirm ──
        # The up-impulse's per-bar volume should be NON-DECREASING (real buyers
        # piling in, not fading into the high). Least-squares slope of push_v vs
        # bar index, normalized by push_vm (self-relative, no fixed magic), must be
        # >= the floor. Sentinel -1 = DISABLED ⇒ skipped ⇒ byte-identical. Fail-open
        # when push_v is too short to fit a slope. (Reuses the SIGNAL-1 slope idiom.)
        _accum_min = float(getattr(settings, "chili_momentum_dipbuy_impulse_accum_min_slope", -1.0))
        if _accum_min != -1.0 and len(push_v) >= 2 and push_vm > 0:
            _pxs = [float(i) for i in range(len(push_v))]
            _pys = [float(v) for v in push_v]
            _pmx = sum(_pxs) / len(_pxs)
            _pmy = sum(_pys) / len(_pys)
            _pcov = sum((x - _pmx) * (y - _pmy) for x, y in zip(_pxs, _pys))
            _pvar = sum((x - _pmx) ** 2 for x in _pxs)
            if _pvar > 0:
                _pslope = (_pcov / _pvar) / push_vm
                if _pslope < _accum_min:
                    return "PASS", None, None, {"dipbuy_declined": "impulse_not_accumulating"}

        # ── Gate 2a (dip-buy quality, flag-gated): high-volume SELLING-candle veto ──
        # A pullback bar that is RED (close < open) AND prints >= mult × the impulse's
        # mean per-bar volume is DISTRIBUTION (a big seller stepping in) — distinct
        # from the AVERAGE dry-up check below (this is PER-CANDLE). Self-relative to
        # push_vm (no fixed share). 0 = disabled ⇒ loop skipped ⇒ byte-identical.
        _dist_mult = float(getattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 0.0))
        if _dist_mult > 0 and opn is not None:
            _ov = opn.values if hasattr(opn, "values") else None
            if _ov is not None:
                for i in range(peak_idx + 1, dip_idx + 1):
                    if i >= len(vv) or i >= len(_ov) or i >= len(cl):
                        continue
                    _ci, _oi, _vi = cl[i], _ov[i], vv[i]
                    # NaN-safe: skip any bar with a missing close/open/volume.
                    if _ci != _ci or _oi != _oi or _vi != _vi:
                        continue
                    if float(_ci) < float(_oi) and float(_vi) >= _dist_mult * push_vm:
                        return "PASS", None, None, {
                            "dipbuy_declined": "distribution_candle",
                            "dist_vol_ratio": round(float(_vi) / push_vm, 2),
                        }

        if dip_vm > push_vm:  # heavy dip volume = sellers in control (the falling-knife signature)
            return "PASS", None, None, {"dipbuy_declined": "no_dryup_heavy_dip"}
        if dip_vm > dryup * push_vm:  # not enough dry-up
            return "PASS", None, None, {"dipbuy_declined": "insufficient_dryup"}

        # ── SIGNAL 3: first reversal new-high off the dip bar's OWN pullback high ──
        # Window = the last K dip bars but NEVER reaching back to the peak (else the
        # level becomes the recovery swing high = the late chase this branch replaces).
        _pb_start = max(peak_idx + 1, dip_idx - K + 1)
        pb_dip_high = float(hi[_pb_start:dip_idx + 1].max())
        if pb_dip_high <= 0:
            return "PASS", None, None, {"dipbuy_declined": "bad_level"}
        # Stop = the dip-low ANCHOR only (the vol-floor layer widens it downstream).
        buf = max(buf_bps / 10_000.0, 0.25 * a)
        stop = dip_low * (1.0 - buf)
        if not (0.0 < stop < pb_dip_high):
            return "PASS", None, None, {"dipbuy_declined": "bad_stop"}
        # S3: halt-band proximity veto — if the stop sits inside/just above the LULD
        # down-halt band, a further drop HALTS the stock and traps the dip (an asymmetric
        # small-cap tail loss). Equity-only (crypto has no LULD halts). Protective.
        if not str(symbol or "").upper().endswith("-USD") and halt_band_trapped(pb_dip_high, stop):
            return "PASS", None, None, {"dipbuy_declined": "halt_band_trapped"}
        # Runway affordability: the STRUCTURAL reward:risk must clear the class floor
        # (do NOT tighten the stop to manufacture R:R — PASS instead). Reward is to the
        # MEASURED-MOVE continuation (peak + the dip's own depth), not the bare peak: a
        # dip-buy targets NEW HIGHS, and using the peak alone makes a near-dip-high entry
        # always sub-1:1 (it would never fire). Depth-independent: risk and reward both
        # scale with the dip depth, so affordability does not change with how deep the
        # buyable dip is (echoes the basis-independent sizing rule).
        from .paper_execution import class_aware_reward_risk

        rr_target = float(class_aware_reward_risk(symbol))
        cont_target = run_high + (run_high - dip_low)
        risk_runway = pb_dip_high - stop
        reward_runway = cont_target - pb_dip_high
        if risk_runway <= 0 or (reward_runway / risk_runway) < rr_target:
            return "PASS", None, None, {"dipbuy_declined": "runway_rr_unaffordable"}

        patch = {
            "dipbuy_dip_low": round(dip_low, 6), "dipbuy_level": round(pb_dip_high, 6),
            "dipbuy_dryup": round(dip_vm / push_vm, 3), "dipbuy_depth_pct": round(depth * 100.0, 2),
            "dipbuy_runway_rr": round(reward_runway / risk_runway, 2),
        }
        # ── Gate 3 (dip-buy quality, flag-gated): L2 hidden-seller / big-seller veto ──
        # BEFORE the FIRE/ARM returns: a large resting ask wall the price can't lift, or
        # absorption/micro-price rollover despite buy-side OFI, makes this dip a
        # round-trip → PASS. FAIL-OPEN (helper returns None on disabled/null/stale L2).
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            return "PASS", None, None, {"dipbuy_declined": _reason, **_l2patch}
        cur_hi = float(hi[cur])
        cur_cl = float(cl[cur])
        cur_lo = float(lo[cur])
        # Not broken on a completed bar yet → ARM a tick-watch (vol-return waived for
        # the tick; the dipbuy tick thrust buffer is the compensating guard).
        if cur_hi <= pb_dip_high:
            return "ARM", pb_dip_high, stop, patch
        # A completed bar broke through → require a STRONG reversal close + volume RETURN.
        rng = cur_hi - cur_lo
        strong_close = (rng <= 0.0) or (((cur_cl - cur_lo) / rng) >= 0.5)
        vol_return = (cur < len(vv)) and (vv[cur] == vv[cur]) and (float(vv[cur]) >= dip_vm)
        if cur_cl >= pb_dip_high * (1.0 - tol) and strong_close and vol_return:
            return "FIRE", pb_dip_high, stop, patch
        return "PASS", None, None, {"dipbuy_declined": "weak_completed_break"}
    except Exception:
        return "PASS", None, None, {"dipbuy_declined": "error"}


def _evaluate_deep_reclaim(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema9: list[Any],
    cur: int,
    *,
    win_high: float,
    base_end: int,
    window_bars: int,
    ema_wick: float,
    tol: float,
    atr_pct: float | None,
    fallback_reason: str,
    debug: dict[str, Any],
    bar_ts: Any = None,
    symbol: str | None = None,
    vol: pd.Series | None = None,
    vwap: list | None = None,
    opn: pd.Series | None = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, float | None, float | None, dict[str, Any]] | None:
    """Deep-retrace RECLAIM entry (the 2026-06-11 EDHL gap): after a retrace too
    deep for the flag checks, Ross does not walk away — he waits for price to
    RECLAIM the 9-EMA, hold it, and buys the first break of the recovery swing
    high. The flag checks are anchored on fixed window OFFSETS, so a reclaim +
    new highs could never clear them (only time could); this evaluator re-anchors
    on the detected dip EVENT instead.

    Called only from the two dead-end returns of ``_evaluate_break_retest``
    (``pullback_too_deep`` / ``pullback_below_ema9``). Returns the 5-tuple to
    return instead, or ``None`` to keep the original rejection (collapse tapes,
    still-falling tapes, path disabled). Stateless on the frame — the live
    runner's 15s re-evals and the replay engine reproduce it identically.

    Mechanics (each reused from an existing yardstick — no new magic):
      * collapse guard: ``_collapse_cap`` (the halt-resume dip cap),
      * EMA hold band: the existing vol-aware ``ema_wick``,
      * entry level: the RECOVERY swing high — NOT the pre-fade HOD (Ross bought
        EDHL's reclaim at ~14.2 while HOD was 15.49),
      * stop: the reclaim consolidation low, at least half an ATR under the level
        (``_VOL_EMA_WICK_ATR_MULT`` — the system's own intrabar-noise floor),
        NEVER the deep dip low (which zeroes risk-first sizing).
    """
    if not bool(getattr(settings, "chili_momentum_deep_reclaim_enabled", True)):
        return None
    # MORNING-ONLY (validated on the 06-10/06-11 A/B): a deep-retrace reclaim is a
    # weaker prior than a clean flag — it pays in the discovery phase (premarket
    # through shortly after the open: EDHL +25%, LASE +13%) and bleeds in midday/
    # afternoon chop (06-10 added SPHL -$404, GCDT -$157, DBGI -$161, all 13:22 ET
    # or later). Ross's own discipline: "by 10:30 I'm done." The lane's global
    # entry window bounds the premarket side; this bounds the late side.
    # CRYPTO PARITY (2026-06-11): the morning window is an EQUITY concept (the
    # discovery phase around the 9:30 ET open) — a 24/7 asset has no open, so
    # the gate must not leak onto crypto (it silently blocked crypto reclaims
    # outside 4:00-10:30 ET from the moment #611 shipped).
    _is_crypto = bool(symbol) and str(symbol).upper().endswith("-USD")
    if bar_ts is not None and not _is_crypto:
        try:
            from zoneinfo import ZoneInfo

            _ts = pd.Timestamp(bar_ts)
            _ts = _ts.tz_localize("UTC") if _ts.tzinfo is None else _ts
            _et = _ts.tz_convert(ZoneInfo("America/New_York"))
            _cutoff_min = (9 * 60 + 30) + int(
                60 * float(getattr(settings, "chili_momentum_reclaim_max_hours_after_open", 1.0) or 1.0)
            )
            if (_et.hour * 60 + _et.minute) >= _cutoff_min:
                return None
        except Exception:
            pass  # no usable clock -> don't block on the guard
    # GAP 4 (Warrior re-audit): RTH-only deep-reclaim dip-buy. The morning-only gate above
    # ONLY bounds the LATE side, and it is SKIPPED entirely when bar_ts is None (which leaked
    # PREMARKET reclaims — stops do not fire premarket so a break-down cannot exit at the
    # stop). When chili_momentum_dip_buy_rth_only_enabled is ON, require the bar to be inside
    # RTH (09:30-16:00 ET); outside / missing-clock-while-ON ⇒ reject (fall back to the
    # original rejection). EQUITY-only (crypto exempt). ADDITIVE: flag OFF ⇒ no effect,
    # byte-identical (the helper returns in_window=True before any clock read).
    if not _is_crypto:
        _rth_ok, _ = _dip_buy_in_rth_window(now=None, bar_ts=bar_ts, symbol=symbol)
        if not _rth_ok:
            return None
        # When the flag is ON but bar_ts is None (no clock), the helper fails OPEN — but for
        # this PREMARKET-leak fix we must fail CLOSED on the dip-buy: a missing clock on the
        # equity reclaim path means we cannot PROVE we are in RTH, so do NOT take the dip-buy.
        if (
            bar_ts is None
            and bool(getattr(settings, "chili_momentum_dip_buy_rth_only_enabled", False))
        ):
            return None
    confirm_bars = max(1, int(getattr(settings, "chili_momentum_reclaim_confirm_bars", 2) or 2))
    w_start = max(0, cur - int(window_bars))
    # Dip anchor = the structural EVENT, peak-then-retrace: the pre-dip PEAK is
    # the window's highest completed bar with room after it for dip + reclaim
    # (a plain window-minimum would anchor on the impulse's ORIGIN instead of
    # the retrace). The dip is the lowest low AFTER that peak.
    peak_hi = cur - (confirm_bars + 1)
    if peak_hi <= w_start:
        return None
    hi_win = high.iloc[w_start:peak_hi + 1]
    peak_idx = w_start + int(hi_win.values.argmax())
    lo_after = low.iloc[peak_idx + 1:cur + 1]
    if len(lo_after) < confirm_bars + 1:
        return None
    dip_idx = peak_idx + 1 + int(lo_after.values.argmin())
    dip_low = float(low.iloc[dip_idx])
    if dip_idx >= cur or dip_low <= 0:
        return None  # still making lows -> the original reason stands
    # Collapse guard: deeper than the vol-relative cap = breakdown, don't reclaim-buy.
    run_high = max(float(win_high), float(high.iloc[peak_idx]))
    if run_high <= 0:
        return None
    depth = (run_high - dip_low) / run_high
    cap = _collapse_cap(atr_pct)
    if depth > cap:
        # A dip deeper than the vol-relative collapse cap is normally a BREAKDOWN —
        # reject. EXCEPTION (Ross's halt-resume dip-buy; WNW 2026-06-16 dropped ~31%
        # then RECLAIMED to new highs but was rejected here and never entered): when
        # price has ALREADY reclaimed back to within `tol` of the run-high, the deep
        # dip was BOUGHT, not a collapse — the downstream held>=confirm_bars +
        # full_reclaim confirmation and the reclaim-low stop bound the risk. A still-
        # FALLING knife never reaches this (its price is far below run_high). Bounded by
        # ONE documented multiple so a true collapse (e.g. -60%) is still rejected even
        # if it bounced. BYTE-IDENTICAL for shallow pullbacks (depth <= cap); only deep-
        # but-reclaimed dips in (cap, cap*mult] change. Reuses the existing `tol`.
        _cap_mult = float(getattr(settings, "chili_momentum_deep_reclaim_collapse_cap_mult", 1.6) or 1.6)
        _reclaimed = float(close.iloc[cur]) >= run_high * (1.0 - tol)
        if not (_reclaimed and depth <= cap * _cap_mult):
            return None
    # EARLY DIP-BUY (Ross "first reversal off the dip"): try the earlier NEAR-DIP
    # entry BEFORE the recovery-high reclaim — fire/arm on the first candle to tick
    # the dip bar's OWN pullback high, behind the 3-signal knife gate. On ANY decline
    # ("PASS"/thin data) it falls through to the existing recovery-high logic
    # BYTE-IDENTICALLY (debug untouched). docs/DESIGN/MOMENTUM_LANE.md
    if vol is not None and vwap is not None:
        _dv, _dlvl, _dstop, _dpatch = _dipbuy_signals_ok(
            high, low, close, vol, vwap,
            peak_idx=peak_idx, dip_idx=dip_idx, dip_low=dip_low, run_high=run_high,
            depth=depth, cur=cur, w_start=w_start, atr_pct=atr_pct, tol=tol,
            ema_wick=ema_wick, ema9=ema9, symbol=symbol,
            opn=opn, db=db, l2_as_of=l2_as_of,
        )
        if _dv in ("FIRE", "ARM"):
            _ddebug = dict(debug)
            _ddebug.update(_dpatch)
            _ddebug["pattern"] = "deep_reclaim"
            _ddebug["dipbuy"] = True
            _ddebug["deep_reclaim_from"] = fallback_reason
            _ddebug["pullback_high"] = float(_dlvl)
            _ddebug["pullback_low"] = float(_dstop)
            if _dv == "FIRE":
                return True, "deep_reclaim_dipbuy", float(_dlvl), float(_dstop), _ddebug
            return False, "waiting_for_dipbuy_break", None, None, _ddebug
    # Reclaim hold: the CURRENT streak of closes holding the live EMA-9 band since
    # the dip — judged at each bar's OWN EMA (the frozen base-bar EMA reference is
    # exactly the bug this path fixes). One band-losing close resets the streak;
    # the next pass re-anchors statelessly (self-healing on dead-cat bounces).
    held = 0
    full_reclaim = False
    i = cur
    while i > dip_idx:
        e = ema9[i] if i < len(ema9) and ema9[i] is not None else None
        if e is None or float(close.iloc[i]) < float(e) * (1.0 - ema_wick):
            break
        if float(close.iloc[i]) >= float(e):
            full_reclaim = True
        held += 1
        i -= 1
    debug = dict(debug)
    debug.update({
        "pattern": "deep_reclaim", "deep_reclaim_from": fallback_reason,
        "dip_low": dip_low, "run_high": round(run_high, 6),
        "depth_pct": round(depth * 100.0, 2), "reclaim_bars": held,
    })
    if held < confirm_bars or not full_reclaim:
        # un-armed: strip the base evaluator's stale levels so nothing downstream
        # (tick dispatch, replay parity) can fire through an outdated number
        debug.pop("pullback_high", None)
        debug.pop("pullback_low", None)
        return False, "reclaim_forming", None, None, debug
    r0 = cur - held + 1
    prior_high = float(high.iloc[dip_idx + 1:cur].max()) if cur > dip_idx + 1 else float(high.iloc[cur])
    reclaim_low = float(low.iloc[r0:cur + 1].min())
    stop = min(reclaim_low, prior_high * (1.0 - _VOL_EMA_WICK_ATR_MULT * (atr_pct or 0.01)))
    cur_high = float(high.iloc[cur])
    if cur_high > prior_high and float(close.iloc[cur]) >= prior_high * (1.0 - tol):
        debug["pullback_high"] = prior_high
        debug["pullback_low"] = stop
        return True, "deep_reclaim", prior_high, stop, debug
    # Not fired on a completed bar yet: arm the tick watch at the recovery swing
    # high (ratchets bar-by-bar; the live WS ask through it fires the tick path).
    debug["pullback_high"] = max(prior_high, cur_high)
    debug["pullback_low"] = stop
    return False, "waiting_for_reclaim_high", None, None, debug


def _evaluate_raw_break(
    high: pd.Series,
    low: pd.Series,
    ema9: list[Any],
    cur: int,
    *,
    entry_interval: str,
    max_pullback_bars: int,
    retracement_threshold: float,
    atr_pct: float | None = None,
) -> tuple[bool, str, float | None, float | None, dict[str, Any]]:
    """First-break trigger (Ross's classic rule). Identical to the original logic:
    after an up-impulse, a SHALLOW pullback (holding above EMA-9) whose HIGH the
    current bar breaks. Returns ``(ok, reason, pullback_high, pullback_low, debug)``.
    """
    look = min(20, cur)
    win_high = float(high.iloc[cur - look:cur].max())
    win_low = float(low.iloc[cur - look:cur].min())
    impulse_range = win_high - win_low
    if impulse_range <= 0:
        return False, "no_range", None, None, {"entry_interval": entry_interval}

    # The pullback = the recent few bars before the current bar: its HIGH is the
    # level to break, its LOW is the structural stop.
    pb_start = max(0, cur - max_pullback_bars)
    pb_high = float(high.iloc[pb_start:cur].max())
    pb_low = float(low.iloc[pb_start:cur].min())
    debug = {"entry_interval": entry_interval, "pullback_high": pb_high, "pullback_low": pb_low,
             "win_high": win_high}

    # Shallow: must not retrace more than the (volatility-aware) threshold of the
    # impulse range — a volatile small-cap is allowed a proportionally deeper flag.
    eff_shallow, ema_wick, _ = _vol_aware_pullback_tolerances(atr_pct, retracement_threshold)
    retrace = (win_high - pb_low) / impulse_range
    debug["retrace"] = round(retrace, 3)
    debug["shallow_cap"] = round(eff_shallow, 3)
    if retrace > eff_shallow:
        return False, "pullback_too_deep", None, None, debug

    # Held above EMA-9 (structural support) during the pullback — the wick tolerance
    # scales with ATR% so normal small-cap noise below the 9-EMA isn't read as a break.
    ema_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
    if ema_cur is not None and pb_low < float(ema_cur) * (1.0 - ema_wick):
        debug["ema_9"] = float(ema_cur)
        return False, "pullback_below_ema9", None, None, debug

    # Break: current bar's high must exceed the pullback high.
    if float(high.iloc[cur]) <= pb_high:
        debug["cur_high"] = float(high.iloc[cur])
        return False, "waiting_for_break", None, None, debug

    return True, "raw_break", pb_high, pb_low, debug


def _evaluate_break_retest(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema9: list[Any],
    cur: int,
    *,
    entry_interval: str,
    max_pullback_bars: int,
    retracement_threshold: float,
    retest_tolerance: float,
    retest_lookback_bars: int,
    atr_pct: float | None = None,
    symbol: str | None = None,
    vol: pd.Series | None = None,
    vwap: list | None = None,
    opn: pd.Series | None = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, float | None, float | None, dict[str, Any]]:
    """Break-AND-retest trigger (Ross's recent refinement: "I almost never buy the
    first break anymore — too many wick out and reverse. I wait for the break AND
    retest.").

    Anchors a STABLE breakout LEVEL on the consolidation that ends
    ``retest_lookback_bars`` bars back (so the level doesn't slide across the
    live runner's per-tick re-evaluations), then requires, within the tail:
      1. a break above the level,
      2. a shallow pullback that RETESTS it (price dips back to ~level), and
      3. the level HOLDS (closes stay above it) with the current bar RECLAIMING it.
    Cuts the raw-first-break false signals. Returns the same 5-tuple shape as
    ``_evaluate_raw_break`` (the level becomes ``pullback_high``; the base low the
    structural stop). docs/DESIGN/MOMENTUM_LANE.md
    """
    look_bars = max(2, int(retest_lookback_bars))
    base_end = cur - look_bars
    if base_end < max(2, int(max_pullback_bars)):
        return False, "retest_insufficient_bars", None, None, {"entry_interval": entry_interval}

    # Impulse before the consolidation base.
    look = min(20, base_end)
    win_high = float(high.iloc[base_end - look:base_end].max())
    win_low = float(low.iloc[base_end - look:base_end].min())
    impulse_range = win_high - win_low
    if impulse_range <= 0:
        return False, "no_range", None, None, {"entry_interval": entry_interval}

    base_start = max(0, base_end - int(max_pullback_bars))
    level = float(high.iloc[base_start:base_end].max())   # stable breakout level
    base_low = float(low.iloc[base_start:base_end].min())  # structural stop
    debug = {"entry_interval": entry_interval, "pullback_high": level, "pullback_low": base_low,
             "win_high": win_high, "mode": "retest"}

    # Volatility-aware tolerances: a volatile small-cap pulls + wicks deeper while
    # still printing a clean flag, so scale the shallow cap / EMA-9 wick / retest
    # tolerance by ATR% (calm name -> Ross floors). See _vol_aware_pullback_tolerances.
    eff_shallow, ema_wick, vol_retest = _vol_aware_pullback_tolerances(atr_pct, retracement_threshold)
    debug["shallow_cap"] = round(eff_shallow, 3)
    tol = max(0.0, float(retest_tolerance), vol_retest)
    # Deep-retrace fallback args: both base-check rejections below are anchored on
    # window POSITION (they slide with `cur`, so a reclaim + new highs can never
    # clear them — the 2026-06-11 EDHL miss). Instead of a terminal reject, hand
    # the tape to the event-anchored reclaim evaluator; it returns None to keep
    # the original rejection when the tape really is broken (collapse/still falling).
    _reclaim_kw = dict(
        win_high=win_high, base_end=base_end,
        window_bars=20 + int(max_pullback_bars) + look_bars,
        ema_wick=ema_wick, tol=tol, atr_pct=atr_pct, debug=debug,
        bar_ts=(high.index[cur] if hasattr(high, "index") and len(high.index) > cur else None),
        symbol=symbol, vol=vol, vwap=vwap, opn=opn, db=db, l2_as_of=l2_as_of,
    )

    retrace = (win_high - base_low) / impulse_range
    debug["retrace"] = round(retrace, 3)
    if retrace > eff_shallow:
        res = _evaluate_deep_reclaim(
            high, low, close, ema9, cur,
            fallback_reason="pullback_too_deep", **_reclaim_kw,
        )
        if res is not None:
            return res
        return False, "pullback_too_deep", None, None, debug

    # EMA-9 support is checked at the BASE (when the consolidation formed), not at
    # the current bar — a strong continuation after the break lifts the current EMA
    # above the older base low, which would otherwise reject a valid retest. Wick
    # tolerance scales with ATR% (small-cap noise below the 9-EMA isn't a break).
    ema_idx = base_end - 1
    ema_base = ema9[ema_idx] if 0 <= ema_idx < len(ema9) and ema9[ema_idx] is not None else None
    if ema_base is not None and base_low < float(ema_base) * (1.0 - ema_wick):
        debug["ema_9"] = float(ema_base)
        res = _evaluate_deep_reclaim(
            high, low, close, ema9, cur,
            fallback_reason="pullback_below_ema9", **_reclaim_kw,
        )
        if res is not None:
            return res
        return False, "pullback_below_ema9", None, None, debug

    # 1) Breakout: a tail bar BEFORE the current pierced the level.
    break_idx: int | None = None
    for i in range(base_end, cur):
        if float(high.iloc[i]) > level:
            break_idx = i
            break
    if break_idx is None:
        return False, "waiting_for_break", None, None, debug

    # 2) Retest: from after the break to now, price dipped back to ~level (came down
    #    to within +tol of it) — not a runaway that never offered a retest entry.
    seg_lo = low.iloc[break_idx + 1:cur + 1]
    seg_cl = close.iloc[break_idx + 1:cur + 1]
    if len(seg_lo) < 1:
        return False, "waiting_for_retest", None, None, debug
    retest_low = float(seg_lo.min())
    debug["retest_low"] = retest_low
    if retest_low > level * (1.0 + tol):
        return False, "waiting_for_retest", None, None, debug

    # 3) Hold: closes after the break stayed above the level (a failed breakout that
    #    lost the level on a close is rejected, not bought).
    if float(seg_cl.min()) < level * (1.0 - tol):
        return False, "retest_failed_hold", None, None, debug

    # 4) Reclaim: the current bar trades back above the level (resuming up).
    if not (float(high.iloc[cur]) > level and float(close.iloc[cur]) >= level * (1.0 - tol)):
        debug["cur_high"] = float(high.iloc[cur])
        return False, "waiting_for_reclaim", None, None, debug

    return True, "break_retest", level, base_low, debug


def _premarket_tickbreak_confirmed(
    *, live_price: float, level: float, atr_pct: float | None,
    symbol: str | None, now: Any = None,
) -> bool:
    """Premarket tick-break confirmation (the CUPR fix).

    In PREMARKET (thin tape, whipsaw, NO L2) a tick poking 1¢ through the pullback
    high is the CUPR false-pop: CHILI entered 4.07 on a failed pop, was stopped
    −15% in the 3.2↔4.5 chop, THEN the name ran +92%. Require an ATR-derived THRUST
    buffer so a real break fires, not a chop wick. RTH **and ALL crypto return
    True** (the existing tick-break is byte-unchanged there — ``market_session_now``
    is ``regular`` for both). ONE adaptive base knob: buffer = ``atr_pct · mult ·
    level`` (equity-relative; auto-scales as ATR thickens into RTH = the regime
    adaptivity). Only GATES an entry — it never touches a stop, so INVARIANT A holds
    by construction. Fail-OPEN (return True) on missing vol or any error: never
    block an entry on thin data or a bug. ``now`` (sim time for replay; None=live
    real clock) drives the session check so the replay evaluates the right session.
    """
    try:
        from ....config import settings
        from .market_profile import market_session_now

        if not bool(getattr(settings, "chili_momentum_premarket_tickbreak_confirm", True)):
            return True
        if market_session_now(symbol, now=now) != "premarket":  # RTH + crypto -> unchanged
            return True
        if atr_pct is None:
            return True  # no volatility read -> fail-open (don't block on thin data)
        mult = float(getattr(settings, "chili_momentum_premarket_tickbreak_atr_mult", 0.10) or 0.10)
        # Adaptive ATR buffer, but never below a premarket FLOOR. At the START of a
        # premarket explosion the historical-bar ATR is LOW (the name was quiet before
        # the move), so the ATR buffer alone is too thin to reject the false-pop —
        # CUPR's 4.07 over a 4.04 level cleared a ~0.05-ATR buffer and was let in, then
        # shook out at 3.45 before running +92%. The floor catches the 1¢ wick while ATR
        # has not yet caught up; a real thrust (>> the floor) still clears it.
        floor_bps = float(getattr(settings, "chili_momentum_premarket_tickbreak_floor_bps", 100.0) or 0.0)
        tol = max(max(0.0, float(atr_pct)) * mult, max(0.0, floor_bps) / 10_000.0)
        return float(live_price) >= (float(level) + tol * float(level))
    except Exception:
        return True  # any error -> fail-open (never block an entry on a bug)


def _dipbuy_tick_thrust_ok(*, live_price: float, level: float, atr_pct: float | None) -> bool:
    """Thrust buffer for a DIP-BUY tick fire. The dip bar's OWN high is the tightest
    level the lane ever arms, so — unlike the premarket guard (which is a no-op in
    RTH+crypto) — a 1-tick poke must clear an ATR/floor buffer in EVERY session, or it
    is a dead-cat entry. Reuses the premarket floor_bps + atr_mult knobs. Fail-OPEN
    (True) on a missing volatility read or any error."""
    try:
        from ....config import settings

        if atr_pct is None:
            return True
        mult = float(getattr(settings, "chili_momentum_premarket_tickbreak_atr_mult", 0.10) or 0.10)
        floor_bps = float(getattr(settings, "chili_momentum_premarket_tickbreak_floor_bps", 100.0) or 0.0)
        tol = max(max(0.0, float(atr_pct)) * mult, max(0.0, floor_bps) / 10_000.0)
        return float(live_price) >= (float(level) + tol * float(level))
    except Exception:
        return True


def _resolve_is_ssr(symbol: str | None) -> bool:
    """Best-effort, cached resolution of SEC Rule 201 short-sale-restriction for an EQUITY
    name (currently DOWN >= ~10%% vs the prior-day close). Reuses the market-data quote
    (last + previous_close — both cached in massive_client). Equity-only; fails CLOSED to
    ``False`` (no carve-out ⇒ existing veto behaviour) on crypto / missing data / any error,
    so it can only ever RELAX a veto, never tighten one."""
    try:
        s = (symbol or "").strip().upper()
        if not s or s.endswith("-USD") or "-" in s or "/" in s:
            return False
        from ...massive_client import get_last_quote
        from .ross_momentum import compute_is_ssr

        q = get_last_quote(s)
        if not isinstance(q, dict):
            return False
        last = q.get("last") or q.get("price") or q.get("close")
        prior = q.get("previous_close") or q.get("prev_close")
        return compute_is_ssr(last, prior)
    except Exception:
        return False


def _prior_day_close(symbol: str | None) -> float | None:
    """R8 (WAVE-4 ITEM-3) — the PRIOR-DAY CLOSE for an equity name (Ross's red-to-green
    anchor). Reuses the cached Massive last-quote (``prevDay.c`` -> ``previous_close``), the
    same source ``_resolve_is_ssr`` reads. FAIL-CLOSED to ``None`` on crypto / missing data /
    any error — the caller then SKIPS (does not fall back to the session open, which is the
    bug R8 fixes: reclaiming the intraday open is not a red-to-green)."""
    try:
        s = (symbol or "").strip().upper()
        if not s or s.endswith("-USD") or "-" in s or "/" in s:
            return None
        from ...massive_client import get_last_quote

        q = get_last_quote(s)
        if not isinstance(q, dict):
            return None
        pc = q.get("previous_close") or q.get("prev_close")
        pc = float(pc) if pc is not None else None
        return pc if (pc is not None and pc > 0) else None
    except Exception:
        return None


def _l2_entry_veto(
    symbol: str | None, *, db: Any = None, l2_as_of: Any = None, is_ssr: bool | None = False,
) -> tuple[str, dict[str, Any]] | None:
    """Gate 3 (dip-buy quality, flag-gated): L2 hidden-seller / big-seller veto.

    ``is_ssr``: ``True``/``False`` = caller-supplied SSR state; ``None`` = AUTO-resolve it
    here (best-effort, cached) — pass ``None`` from the live gates so the SSR carve-out is
    actually wired. The default ``False`` keeps any positional/legacy caller byte-identical.

    Reuses the #699 OFI + #704 ladder readers (``read_ladder_distribution`` →
    OFI/micro-price/depth-imbalance) — NOT a new L2 stack. CLASS-AWARE through that
    reader (equity ``iqfeed_depth_snapshots`` / crypto ``fast_orderbook``). Returns:

      * ``("l2_big_seller", patch)``    — a large resting ASK wall at/near the entry
        level the price can't lift: the NEWEST book is ask-heavy and sits at/below the
        big-seller percentile floor (a TREND of distribution in its own window, not a
        single-snapshot spoof).
      * ``("l2_hidden_seller", patch)`` — absorption / micro-price ROLLOVER despite a
        buy-side OFI read (the #704 absorption shape applied at entry): supply is
        eating the bid even as flow looks bid-side, so the breakout has no follow-through.
      * ``None`` — NO veto (FAIL-OPEN): disabled, db None, blank symbol, empty/stale L2,
        or a _NULL read. NEVER blocks a good entry on missing/bad data.

    Pure read (no writes). Any error ⇒ None (fail-open)."""
    try:
        if not bool(getattr(settings, "chili_momentum_entry_l2_veto_enabled", False)):
            return None
        if db is None or not symbol:
            return None
        from .pipeline import read_ladder_distribution

        lr = read_ladder_distribution(symbol, db=db, as_of=l2_as_of)
        if lr is None or int(getattr(lr, "n_snaps", 0) or 0) <= 0:
            return None  # empty / _NULL read -> fail-open

        # AUTO-resolve SSR when the caller passed the None sentinel (the live gates do);
        # explicit True/False is honoured as-is. Fails CLOSED to not-SSR (existing behaviour).
        if is_ssr is None:
            is_ssr = _resolve_is_ssr(symbol)

        # (a) BIG-SELLER wall: the newest book is ask-heavy relative to its own recent
        #     window — depth-imbalance percentile at/below the floor. Self-relative
        #     percentile (no absolute threshold a single spoof can trip). Fail-open
        #     when the percentile is unavailable (too few snaps to rank).
        #     SSR CARVE-OUT (additive): under short-sale restriction shorts may only sell
        #     on an UPTICK — they CANNOT hit the bid — so resting ASK-side stacking is NOT
        #     the bearish "shorts pressing the offer" this leg reads it as (it is far more
        #     likely passive/limit supply that a squeeze lifts). Suppress ONLY this ask-
        #     stacking leg on SSR names; the hidden-seller / absorption leg (b) below still
        #     runs (absorption at the BID is the relevant tell under SSR). is_ssr defaults
        #     False ⇒ every existing caller is byte-identical.
        try:
            floor = float(getattr(settings, "chili_momentum_entry_l2_bigseller_pctile_floor", 0.15))
        except (TypeError, ValueError):
            floor = 0.15
        pct = getattr(lr, "depth_imbal_pctile", None)
        if pct is not None and not is_ssr:
            try:
                if float(pct) <= floor:
                    return "l2_big_seller", {
                        "l2_pctile": round(float(pct), 3),
                        "l2_floor": round(floor, 3),
                    }
            except (TypeError, ValueError):
                pass

        # (b) HIDDEN-SELLER absorption: micro-price ROLLED OVER (micro_edge < 0 =
        #     book leans to the ask / supply at the touch) while OFI still reads buy-
        #     side (>= +threshold) — the #704 absorption shape (supply eating demand)
        #     applied at the ENTRY: a break with no real follow-through. Reuses the
        #     entry's own chili_momentum_ofi_threshold (no new OFI knob).
        try:
            thr = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
        except (TypeError, ValueError):
            thr = 0.25
        ofi = getattr(lr, "ofi", None)
        micro = getattr(lr, "micro_edge", None)
        if ofi is not None and micro is not None:
            try:
                if float(ofi) >= thr and float(micro) < 0.0:
                    return "l2_hidden_seller", {
                        "l2_ofi": round(float(ofi), 4),
                        "l2_micro_edge": round(float(micro), 2),
                    }
            except (TypeError, ValueError):
                pass
        return None
    except Exception:
        return None  # any error -> fail-open (never block an entry on a bug)


def _l2_big_buyer_bid_starter(
    symbol: str | None, *, db: Any = None, l2_as_of: Any = None, price: float | None = None,
    atr_pct: float | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """GAP 5 (Warrior re-audit): the BID-side MIRROR of ``_l2_entry_veto`` — a large stacked
    BUYER on the bid near a whole/half dollar PERMITS / confirms a dip-buy starter (the
    demand-side enabler, the inverse of the seller veto). It is an ENABLER overlay, NEVER a
    veto: it can only ever return a positive permit or ``None``; it can NOT block any entry.

    Reuses the SAME ``read_ladder_distribution`` reader the seller veto uses (NOT a new L2
    stack). Returns:

      * ``("l2_big_buyer_bid", patch)`` — the NEWEST book is bid-heavy relative to its own
        recent window: ``depth_imbal_pctile`` at/ABOVE the ceiling (a TREND of accumulation,
        not a single-snapshot spoof), the SPREAD is tight (the existing wide-spread caveat —
        a wide book still blocks the permit), and (when ``price`` is supplied) the price sits
        near a whole/half-dollar round number (Ross's psychological support).
      * ``None`` — NO permit (FAIL-CLOSED): disabled, db None, blank symbol, empty/stale L2,
        unavailable percentile, or a wide spread. A missing read NEVER manufactures a permit.

    Pure read (no writes). Any error ⇒ ``None`` (fail-closed — the starter is only ARMED on a
    proven big-buyer book)."""
    try:
        if not bool(getattr(settings, "chili_momentum_big_buyer_bid_starter_enabled", False)):
            return None
        if db is None or not symbol:
            return None
        from .pipeline import read_ladder_distribution

        lr = read_ladder_distribution(symbol, db=db, as_of=l2_as_of)
        if lr is None or int(getattr(lr, "n_snaps", 0) or 0) <= 0:
            return None  # empty / _NULL read -> fail-CLOSED (no permit on missing data)

        # SPREAD caveat (kept from the seller-side intent): a wide bid-ask spread = illiquid
        # book; NEVER arm a starter there even if the bid looks stacked.
        try:
            _max_spread = float(getattr(settings, "chili_momentum_big_buyer_bid_max_spread_bps", 80.0) or 80.0)
        except (TypeError, ValueError):
            _max_spread = 80.0
        _spread = getattr(lr, "spread_bps", None)
        if _spread is not None:
            try:
                if float(_spread) >= _max_spread:
                    return None  # wide spread -> block the permit
            except (TypeError, ValueError):
                pass

        # BIG-BUYER wall: the newest book is BID-heavy relative to its own recent window —
        # depth-imbalance percentile at/ABOVE the ceiling. Self-relative percentile (mirror of
        # the big-seller floor; no absolute threshold a single spoof can trip). Fail-CLOSED
        # when the percentile is unavailable (too few snaps to rank).
        try:
            ceiling = float(getattr(settings, "chili_momentum_big_buyer_bid_pctile_ceiling", 0.85))
        except (TypeError, ValueError):
            ceiling = 0.85
        pct = getattr(lr, "depth_imbal_pctile", None)
        if pct is None:
            return None
        try:
            if float(pct) < ceiling:
                return None
        except (TypeError, ValueError):
            return None

        patch: dict[str, Any] = {
            "l2_buyer_pctile": round(float(pct), 3),
            "l2_buyer_ceiling": round(ceiling, 3),
            "l2_spread_bps": (round(float(_spread), 2) if _spread is not None else None),
        }
        # ROUND-NUMBER context (Ross: big buyers stack at half/whole-dollar support). When a
        # price is supplied, require the price to sit near a round number; without a price the
        # bid-stack alone is the permit (the round-number overlay is additive, not required).
        if price is not None:
            try:
                from .daily_levels import _round_number_near

                _rn = _round_number_near(float(price), float(atr_pct or 0.0))
                if _rn is not None:
                    patch["l2_buyer_round_number"] = round(float(_rn), 6)
            except Exception:
                pass
        return "l2_big_buyer_bid", patch
    except Exception:
        return None  # any error -> fail-CLOSED (only arm on a proven big-buyer book)


def _signed_tape_features(rows: Any, *, window_s: float) -> dict[str, Any] | None:
    """PURE (no I/O): from oldest-first ``(price, size, bid, ask, ts_seconds)`` trade ticks
    over a recent window, compute the TAPE-PRIMARY confirmer features. Lookahead-free —
    the caller supplies only COMPLETED ticks up to ``now`` / ``as_of``; this just splits the
    given window in half and measures the back-half against the front-half.

    Returns ``None`` on empty / too-few-ticks (< 3) / zero total volume ⇒ the caller
    FAILS OPEN (confirms). On enough ticks returns::

        {
          "signed_tape_accel": float,   # back_half_buy_vol − front_half_buy_vol (aggressor-
                                        #   signed volume, SAME Lee-Ready quote/tick rule as
                                        #   _aggressor_imbalance) — >0 ⇒ aggressive buying is
                                        #   ACCELERATING into the entry
          "tick_rate": float,           # back-half ticks / back-half seconds (recent activity)
          "tick_rate_floor": float,     # self-relative floor: the ``floor_pctile`` percentile
                                        #   of the per-half tick rates (adaptive, no magic rate)
          "n_ticks": int,
        }

    Aggressor classification is identical to ``_aggressor_imbalance``: QUOTE RULE
    (Lee-Ready) when bid/ask present, TICK RULE fallback (zero-tick carries the prior sign),
    so ``signed_tape_accel`` is in the same signed-volume space as the live trade_flow.
    """
    if not rows:
        return None
    # Parse + aggressor-sign every tick in arrival order (prev_px / last_sign carry across
    # the whole window so the tick-rule fallback is continuous, exactly like _aggressor_imbalance).
    parsed: list[tuple[float, float, int]] = []  # (ts_seconds, signed_vol, abs_vol)
    prev_px = None
    last_sign = 0
    t_min = None
    t_max = None
    for r in rows:
        try:
            px = float(r[0])
            sz = float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0 or sz <= 0:
            continue
        try:
            ts = float(r[4])
        except (TypeError, ValueError, IndexError):
            ts = None
        bid = r[2] if len(r) > 2 else None
        ask = r[3] if len(r) > 3 else None
        sign = 0
        if bid is not None and ask is not None:
            try:
                fb, fa = float(bid), float(ask)
            except (TypeError, ValueError):
                fb = fa = 0.0
            if fa > fb > 0:
                mid = (fa + fb) / 2.0
                if px >= fa:
                    sign = 1
                elif px <= fb:
                    sign = -1
                elif px > mid:
                    sign = 1
                elif px < mid:
                    sign = -1
        if sign == 0:  # tick-rule fallback (zero-tick / first trade carries prior sign)
            if prev_px is not None and px != prev_px:
                sign = 1 if px > prev_px else -1
            else:
                sign = last_sign
        prev_px = px
        if sign != 0:
            last_sign = sign
        parsed.append((ts, sign * sz, sz))
        if ts is not None:
            t_min = ts if t_min is None else min(t_min, ts)
            t_max = ts if t_max is None else max(t_max, ts)
    n = len(parsed)
    if n < 3:
        return None
    total_abs = sum(p[2] for p in parsed)
    if total_abs <= 0:
        return None
    # Split the WINDOW (not the count) in half by timestamp midpoint so accel measures a
    # true rate of change in time; fall back to an index split when timestamps are absent.
    if t_min is not None and t_max is not None and t_max > t_min:
        midpoint = (t_min + t_max) / 2.0
        front = [p for p in parsed if p[0] is not None and p[0] < midpoint]
        back = [p for p in parsed if p[0] is None or p[0] >= midpoint]
        front_secs = max(1e-6, midpoint - t_min)
        back_secs = max(1e-6, t_max - midpoint)
    else:
        half = n // 2
        front = parsed[:half]
        back = parsed[half:]
        span = max(1e-6, float(window_s))
        front_secs = back_secs = span / 2.0
    # back-half buy_vol − front-half buy_vol (positive-signed aggressor volume only).
    front_buy = sum(p[1] for p in front if p[1] > 0)
    back_buy = sum(p[1] for p in back if p[1] > 0)
    signed_tape_accel = back_buy - front_buy
    tick_rate = len(back) / back_secs if back_secs > 0 else 0.0
    # Self-relative floor: the per-half tick rates (front + back) form the symbol's OWN
    # recent activity sample; the floor is the configured percentile of those rates.
    half_rates = sorted(
        [len(front) / front_secs if front_secs > 0 else 0.0,
         len(back) / back_secs if back_secs > 0 else 0.0]
    )
    try:
        fp = float(getattr(settings, "chili_momentum_l2_confirm_tick_rate_floor_pctile", 0.0) or 0.0)
    except (TypeError, ValueError):
        fp = 0.0
    fp = max(0.0, min(1.0, fp))
    # percentile of the small per-half-rate sample (nearest-rank, lower bound)
    idx = min(len(half_rates) - 1, int(fp * (len(half_rates) - 1)))
    tick_rate_floor = half_rates[idx] if half_rates else 0.0
    return {
        "signed_tape_accel": float(signed_tape_accel),
        "tick_rate": float(tick_rate),
        "tick_rate_floor": float(tick_rate_floor),
        "n_ticks": int(n),
    }


def signed_tape_accel_features(
    symbol: str | None, *, db: Any = None, window_s: float | None = None, as_of: Any = None
) -> dict[str, Any] | None:
    """Live wrapper around :func:`_signed_tape_features`: pull the recent ``iqfeed_trade_ticks``
    (equity tape; lookahead-free trailing ``now()`` / ``(as_of-w, as_of]``) and compute the
    tape-primary confirmer features. Returns ``None`` (⇒ fail-open) on no symbol / no db /
    crypto (no equity tick tape) / empty tape / any error. Crypto is intentionally skipped —
    the equity tick-by-tick bridge is the genuinely additive tape (the design's Phase-1 scope);
    crypto rides the existing OFI/flow path and fails open here."""
    s = (symbol or "").strip().upper()
    if not s or db is None or s.endswith("-USD"):
        return None
    try:
        w = float(window_s) if window_s is not None else float(
            getattr(settings, "chili_momentum_l2_confirm_window_s", 15.0) or 15.0
        )
    except (TypeError, ValueError):
        w = 15.0
    try:
        from sqlalchemy import text as _sql

        # Always the bounded as-of form, anchored through the replay-aware chokepoint
        # when the caller didn't thread as_of (live: _utcnow() == wall UTC => identical
        # row set to the old wall-now() branch; replay: the sim clock — this feeds the
        # WATCH->FILL confirmers (tape_confirms_hold/_l2_entry_confirm) and the
        # tape-accel reversal exit, which otherwise read an EMPTY window in replay).
        _ao = _tape_asof_default(as_of)
        _ao = _ao.replace(tzinfo=None) if getattr(_ao, "tzinfo", None) is not None else _ao
        q = (
            "SELECT price, size, bid, ask, "
            "EXTRACT(EPOCH FROM observed_at) FROM iqfeed_trade_ticks "
            "WHERE symbol = :s AND observed_at > :as_of - make_interval(secs => :w) "
            "AND observed_at <= :as_of ORDER BY observed_at ASC"
        )
        p = {"s": s, "w": w, "as_of": _ao}
        rows = db.execute(_sql(q), p).fetchall()
    except Exception:
        return None
    try:
        return _signed_tape_features(rows, window_s=w)
    except Exception:
        return None


def _l2_entry_confirm(
    symbol: str | None,
    *,
    db: Any = None,
    le: Any = None,
    settings: Any = settings,
    l2_as_of: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Phase-1 L2 entry CONFIRMER (DEFER-only). Runs at the live entry seam AFTER the chart
    trigger fires AND AFTER both existing vetoes (_l2_entry_veto + _entry_flow_veto) pass —
    a veto ALWAYS wins, this never confirms into a vetoed book. Returns ``(decision, debug)``
    with ``decision in {"confirm", "defer"}``.

    TAPE-PRIMARY: a CONFIRM requires the executed tape to actively confirm thrust —
    ``signed_tape_accel > 0`` (back-half aggressor-signed buy volume exceeds the front-half)
    AND ``tick_rate >= tick_rate_floor`` (recent activity at/above its self-relative floor).
    OFI (``>= threshold`` OR ``micro_edge > 0``) and a RISING depth-imbalance percentile are
    SECONDARY agreement confirmers (logged + used to break the conservative-active tie).

    CONSERVATIVE-ACTIVE start: DEFER only on CLEAR no-confirmation —
    ``signed_tape_accel <= 0 AND OFI < 0`` (the tape is NOT accelerating into the buy AND the
    book flow leans net-selling). Anything else ⇒ CONFIRM (we do not over-defer valid reclaims;
    [[project_e1_backside_veto_shipped]] over-veto lesson). The tick-rate floor only GATES the
    positive-confirm narrative; it never manufactures a defer on its own.

    FAIL-OPEN (return ``confirm``, reason ``l2_confirm_no_data``): any helper None /
    ``n_snaps < 3`` / empty tape / STALE book (``snapshot_age_s`` over the ceiling). Never
    defers on missing / thin / stale data.

    KILL-SWITCH: ``chili_momentum_l2_confirm_enabled`` False ⇒ return
    ``("confirm", {"reason": "l2_confirm_disabled"})`` IMMEDIATELY, before ANY I/O ⇒
    byte-identical. ENTRY-ONLY: the caller invokes this only on a not-yet-entered candidate;
    held / position states never call it, so a defer can never block an exit. Pure read
    (no writes); any error ⇒ confirm (fail-open)."""
    dbg: dict[str, Any] = {"reason": ""}
    try:
        # KILL-SWITCH FIRST — before any I/O (byte-identical when OFF).
        if not bool(getattr(settings, "chili_momentum_l2_confirm_enabled", False)):
            dbg["reason"] = "l2_confirm_disabled"
            return "confirm", dbg
        if db is None or not symbol:
            dbg["reason"] = "l2_confirm_no_data"
            return "confirm", dbg

        # ── TAPE (primary) ──
        try:
            w = float(getattr(settings, "chili_momentum_l2_confirm_window_s", 15.0) or 15.0)
        except (TypeError, ValueError):
            w = 15.0
        tape = signed_tape_accel_features(symbol, db=db, window_s=w, as_of=l2_as_of)
        if tape is None:
            dbg["reason"] = "l2_confirm_no_data"  # empty/thin tape -> fail-open
            return "confirm", dbg
        accel = float(tape.get("signed_tape_accel", 0.0))
        tick_rate = float(tape.get("tick_rate", 0.0))
        tick_rate_floor = float(tape.get("tick_rate_floor", 0.0))
        dbg.update({
            "signed_tape_accel": round(accel, 6),
            "tick_rate": round(tick_rate, 4),
            "tick_rate_floor": round(tick_rate_floor, 4),
            "n_ticks": int(tape.get("n_ticks", 0)),
        })

        # ── BOOK (secondary agreement): OFI / micro-price / depth-imbalance percentile ──
        from .pipeline import read_ladder_distribution

        lr = read_ladder_distribution(symbol, db=db, as_of=l2_as_of)
        n_snaps = int(getattr(lr, "n_snaps", 0) or 0) if lr is not None else 0
        if lr is None or n_snaps < 3:
            dbg["reason"] = "l2_confirm_no_data"  # too-few snaps -> fail-open
            return "confirm", dbg
        # STALENESS: a frozen feed -> fail-open (never defer on stale data).
        try:
            max_age = float(getattr(settings, "chili_momentum_l2_confirm_max_snapshot_age_s", 10.0) or 10.0)
        except (TypeError, ValueError):
            max_age = 10.0
        age = getattr(lr, "snapshot_age_s", None)
        if age is not None and float(age) > max_age:
            dbg["reason"] = "l2_confirm_no_data"
            dbg["snapshot_age_s"] = round(float(age), 2)
            return "confirm", dbg
        ofi = getattr(lr, "ofi", None)
        micro = getattr(lr, "micro_edge", None)
        pctile = getattr(lr, "depth_imbal_pctile", None)
        try:
            ofi_thr = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
        except (TypeError, ValueError):
            ofi_thr = 0.25
        ofi_f = None if ofi is None else float(ofi)
        micro_f = None if micro is None else float(micro)
        pctile_f = None if pctile is None else float(pctile)
        dbg.update({
            "ofi": None if ofi_f is None else round(ofi_f, 4),
            "micro_edge": None if micro_f is None else round(micro_f, 2),
            "depth_imbal_pctile": None if pctile_f is None else round(pctile_f, 3),
            "ofi_threshold": round(ofi_thr, 4),
        })

        # Secondary agreement: book flow leans buy-side (OFI clears threshold OR micro>0)
        # AND the depth-imbalance percentile is RISING (newest book at/above the median of
        # its own recent window ⇒ accumulation, not distribution).
        ofi_agrees = ofi_f is not None and (ofi_f >= ofi_thr or (micro_f is not None and micro_f > 0.0))
        depth_rising = pctile_f is not None and pctile_f >= 0.5
        dbg["ofi_agrees"] = bool(ofi_agrees)
        dbg["depth_rising"] = bool(depth_rising)

        # PRIMARY confirm: the tape is accelerating AND active.
        tape_confirms = accel > 0.0 and tick_rate >= tick_rate_floor
        # CLEAR no-confirmation: tape NOT accelerating AND book flow net-selling.
        ofi_negative = ofi_f is not None and ofi_f < 0.0
        clear_no_confirm = accel <= 0.0 and ofi_negative

        if tape_confirms:
            dbg["reason"] = "l2_confirm_tape_thrust"
            return "confirm", dbg
        if clear_no_confirm:
            # conservative-active DEFER: only when the tape is dead/negative AND the book
            # flow is net-selling. If any secondary confirmer disagrees with the bearish
            # read (book buy-side or depth rising), give the benefit of the doubt + confirm.
            if ofi_agrees or depth_rising:
                dbg["reason"] = "l2_confirm_secondary_override"
                return "confirm", dbg
            dbg["reason"] = "l2_confirm_defer_no_tape"
            return "defer", dbg
        # mixed (e.g. flat tape but OFI not negative, or accel<=0 with neutral book):
        # CONSERVATIVE-ACTIVE ⇒ confirm (do not over-defer).
        dbg["reason"] = "l2_confirm_pass_mixed"
        return "confirm", dbg
    except Exception:
        dbg["reason"] = "l2_confirm_no_data"  # any error -> fail-open
        return "confirm", dbg


def is_explosive_mover(
    atr_pct: float | None,
    rvol: float | None,
    settings: Any,
) -> bool:
    """Shared explosiveness predicate for the explosive-mover recalibration carve-outs
    (bid-prop exempt, fast-bail lock-in, extension RVOL boost, flow-veto strong-leg
    relaxation). A name is EXPLOSIVE when its intraday volatility OR its relative volume
    is at/above the configured floor — the high-RVOL / extreme-ATR regime the Ross lane
    explicitly targets (its bid steps down + spread widens + it dip-tests the broken
    level mid-squeeze, which the conservative gates misread as failure).

    GATED BY THE MASTER kill-switch: when ``chili_momentum_explosive_recalibration_enabled``
    is OFF (default) this ALWAYS returns False, so every carve-out keyed on it is a no-op
    and the lane is byte-identical. ADDITIVE / fail-closed: any error or both-None inputs
    return False (no name is treated as explosive on a bug ⇒ the protective gate stays).
    Pure; no I/O or mutation."""
    try:
        if not bool(getattr(settings, "chili_momentum_explosive_recalibration_enabled", False)):
            return False
        a = None if atr_pct is None else float(atr_pct)
        rv = None if rvol is None else float(rvol)
        atr_floor = float(getattr(settings, "chili_momentum_explosive_atr_pct_floor", 0.045) or 0.0)
        rvol_floor = float(getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 0.0)
        if a is not None and atr_floor > 0.0 and a >= atr_floor:
            return True
        if rv is not None and rvol_floor > 0.0 and rv >= rvol_floor:
            return True
        return False
    except Exception:
        return False


def continuation_conviction_floors(settings_obj: Any = settings) -> tuple[float, float]:
    """The TWO conviction floors the momentum-continuation gate uses, resolved from one
    place so arm-time (auto_arm) and entry-time (live_runner) NEVER diverge:

      * ross_floor   = ``chili_momentum_continuation_ross_floor``           (default 0.7)
      * rvol_floor   = ``chili_momentum_explosive_rvol_floor`` x
                       ``chili_momentum_coiling_exempt_rvol_mult``          (default 3.0*3.0 = ~9x)

    Returns ``(ross_floor, rvol_conviction_floor)``. Pure; no I/O."""
    _ross_floor = float(getattr(settings_obj, "chili_momentum_continuation_ross_floor", 0.7) or 0.7)
    _rvol_floor = float(getattr(settings_obj, "chili_momentum_explosive_rvol_floor", 3.0) or 3.0)
    _coil_mult = float(getattr(settings_obj, "chili_momentum_coiling_exempt_rvol_mult", 3.0) or 3.0)
    return _ross_floor, _rvol_floor * _coil_mult


def continuation_high_conviction(
    ross_score: float | None,
    rvol: float | None,
    daily_breaking: bool,
    settings_obj: Any = settings,
) -> bool:
    """THE shared high-conviction predicate for the momentum-continuation entry — the
    chase-safety that only lets GENUINE high-conviction movers arm/enter. ONE definition,
    consumed at BOTH the arm-time gate (auto_arm ``_continuation_active_trigger``) and the
    entry-time gate (live_runner WATCHING tick) so the two can never silently diverge.

    HIGH-CONVICTION when ANY of (OR-ed, exactly as deployed 1e2eb09):
      * ross_score        >= chili_momentum_continuation_ross_floor                (~0.7)
      * rvol              >= explosive_rvol_floor x coiling_exempt_rvol_mult        (~9x)
      * daily_breaking_major

    Callers source ``rvol`` their OWN way (row scanner signal first, then the intraday
    fallback via ``compute_intraday_rvol_fallback``) and pass the resolved scalar in — the
    TEST itself is shared. Pure; no I/O."""
    try:
        _ross_floor, _rvol_conviction_floor = continuation_conviction_floors(settings_obj)
        return bool(
            (ross_score is not None and ross_score >= _ross_floor)
            or (rvol is not None and rvol >= _rvol_conviction_floor)
            or daily_breaking
        )
    except Exception:
        return False


def compute_intraday_rvol_fallback(
    df: Any,
    *,
    symbol: str | None = None,
    settings_obj: Any = settings,
) -> float | None:
    """FALLBACK intraday relative volume from the ALREADY-FETCHED OHLCV frame (the 5m/5d
    ``df_pb`` the continuation trigger has in hand) — ZERO new fetch. Fills the EMPTY
    scanner-signal case ONLY: a name that arrived via the SCANNER (not the ignition
    enricher) carries no ``ross_signals`` so its row RVOL is None, yet it can be a genuine
    explosive runner (PED: +25%, AT HOD, true intraday RVOL 13.72x). The conviction gate
    must not depend solely on that unreliable per-name enrichment.

    Intraday RVOL = TODAY's (latest session's) cumulative volume / the trailing AVERAGE of
    the prior complete sessions' cumulative volume, grouped by calendar date on the frame's
    DatetimeIndex. This mirrors the leader-scorer's "today vs typical day" RVOL rather than
    a single-bar ratio, so a straight-up runner that prints heavy volume all session reads
    as high-RVOL even with no pullback bar.

    KILL-SWITCH ``chili_momentum_conviction_rvol_fallback_enabled`` — OFF (default) returns
    None IMMEDIATELY, so the conviction gate sees an empty-signal name exactly as deployed
    1e2eb09 (low-conviction) ⇒ BYTE-IDENTICAL.

    FAIL-CLOSED: returns None (⇒ the gate treats the name as non-explosive on RVOL, does
    NOT admit it) on ANY of — flag off, no/empty frame, no Volume column, < 2 sessions of
    data, a non-datetime index we cannot group by date, a non-positive / NaN trailing
    average, or any exception. NEVER admits a genuinely low-RVOL name. Pure read."""
    try:
        if not bool(getattr(settings_obj, "chili_momentum_conviction_rvol_fallback_enabled", False)):
            return None
        if df is None or getattr(df, "empty", True):
            return None
        if "Volume" not in getattr(df, "columns", []):
            return None
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            try:
                idx = pd.DatetimeIndex(idx)
            except Exception:
                return None
        vol = pd.to_numeric(df["Volume"], errors="coerce")
        # Per-session cumulative volume = sum of the session's bars (group by calendar date).
        by_day = vol.groupby(idx.normalize()).sum()
        by_day = by_day[by_day.notna() & (by_day > 0.0)]
        if len(by_day) < 2:
            # Need at least one prior complete session to form a trailing average.
            return None
        by_day = by_day.sort_index()
        today_vol = float(by_day.iloc[-1])
        prior = by_day.iloc[:-1]
        avg_prior = float(prior.mean())
        if not math.isfinite(avg_prior) or avg_prior <= 0.0:
            return None
        rvol = today_vol / avg_prior
        if not math.isfinite(rvol) or rvol <= 0.0:
            return None
        return float(rvol)
    except Exception:
        return None


def _entry_flow_veto(
    ofi: float | None,
    trade_flow: float | None,
    settings: Any,
    *,
    explosive: bool = False,
) -> bool:
    """Entry-TIME flow veto (separate from selection): if the LIVE OFI AND the
    executed-tape trade_flow are BOTH sufficiently negative, the tape is actively
    SELLING this exact tick → defer the buy (caller keeps the symbol WATCHING; it
    can re-enter when flow flips back positive). This is an ENTRY-TIMING gate, NOT a
    selection de-rate — it MUST run for ALL names including extreme movers
    (ross>=0.8): the never-penalize-the-tail rule keeps explosives on the watchlist,
    but we still must never BUY into max selling.

    Keys on LIVE FLOW (OFI + trade_flow), NOT the static book_imbalance the existing
    L2 seller-veto reads (the PLSM flush had book_imbalance=+0.21 stale yet OFI=-1.0,
    trade_flow=-0.51). Lookahead-free: uses only the entry features already computed.

    Two veto legs (OR):
      * AND-leg (both-bearish): OFI <= ofi_thr AND trade_flow <= tf_thr — both the
        resting-book pressure and the executed tape lean net-selling.
      * STRONG-tape OR-leg: trade_flow <= tf_strong_thr ALONE, regardless of OFI — the
        executed tape (trade_flow) is the most direct "are sellers winning RIGHT NOW"
        signal, so a STRONGLY-selling tape vetoes even when OFI looks mildly positive
        (06-24 RUN: ofi=+0.5 mild buy yet trade_flow=-0.63 strong executed selling —
        the strict AND-leg missed it).

    Returns True ⇒ VETO the buy this tick. ADDITIVE / byte-identical when the flag is
    OFF or the relevant flow is None (absent / no L2 / no tape). Pure; no I/O or mutation.
    """
    try:
        if not bool(getattr(settings, "chili_momentum_entry_flow_veto_enabled", True)):
            return False
        ofi_thr = float(getattr(settings, "chili_momentum_entry_flow_veto_ofi", -0.6))
        tf_thr = float(getattr(settings, "chili_momentum_entry_flow_veto_trade_flow", -0.25))
        tf_strong_thr = float(
            getattr(settings, "chili_momentum_entry_flow_veto_trade_flow_strong", -0.5)
        )
        # EXPLOSIVE carve-out (GATE 4): on a high-RVOL / extreme-ATR low-float, a strong
        # (but not maximum) negative tape is one or two aggressive sellers on a thin bar,
        # not "sellers winning the breakout" — those names RUN on their own two-sided
        # volatility. When the explosive flag is set (caller computed it via the master-
        # gated is_explosive_mover), the STRONG-tape OR-leg threshold drops to the
        # near-maximum-selling level, so the leg vetoes ONLY on extreme selling. The
        # both-bearish AND-leg is UNCHANGED, so a mixed-flow break still vetoes (a falling
        # tape under a deteriorating book is caught regardless of explosiveness). Master
        # OFF ⇒ explosive is always False ⇒ byte-identical.
        if explosive and bool(
            getattr(settings, "chili_momentum_entry_flow_veto_explosive_exempt", False)
        ):
            tf_strong_thr = float(
                getattr(
                    settings,
                    "chili_momentum_entry_flow_veto_trade_flow_strong_explosive",
                    -0.85,
                )
            )
        # AND-leg: both the book (OFI) and the tape (trade_flow) lean net-selling.
        and_leg = (
            ofi is not None
            and trade_flow is not None
            and float(ofi) <= ofi_thr
            and float(trade_flow) <= tf_thr
        )
        # STRONG-tape OR-leg: the executed tape alone is strongly selling (ignore OFI).
        strong_leg = trade_flow is not None and float(trade_flow) <= tf_strong_thr
        return bool(and_leg or strong_leg)
    except Exception:
        return False  # any error -> fail-open (never block an entry on a bug)


def _entry_extension_veto(
    entry_price: float | None,
    breakout_level: float | None,
    atr_pct: float | None,
    settings: Any,
    *,
    rvol: float | None = None,
) -> bool:
    """Entry-EXTENSION (chase) veto: defer the buy when the entry sits too far ABOVE
    the breakout level — i.e. we are buying NEAR a local top after the move already
    ran (06-24: RUN entered @15.51 vs break 12.94 = +19.9% extended; PLSM @10.21 vs
    break 7.63 = +33.8%). Ross's discipline: enter AT the break / on the pullback to
    it, never chase the extension into a reversal.

    The cap is ADAPTIVE to volatility (no flat magic %): the allowed extension above
    the level = ``max(floor, K · atr_pct)`` so a calm name gets the floor and a
    volatile small-cap gets proportional room. Veto when::

        entry_price >= breakout_level · (1 + max(floor, K · atr_pct))

    With the defaults (floor 0.08, K 8.0, atr_pct~0.015 -> cap ~0.12) this blocks
    RUN(+19.9%) and PLSM(+33.8%) while allowing entries within ~12% of the break.

    Returns True ⇒ VETO the buy this tick (caller defers to WATCHING so it can re-enter
    on a pullback toward the level — NOT terminal). ADDITIVE / byte-identical when the
    flag is OFF, the breakout_level is missing/non-positive, or atr_pct is missing
    (None). Pure; no I/O or mutation.
    """
    try:
        if not bool(getattr(settings, "chili_momentum_entry_extension_veto_enabled", True)):
            return False
        if entry_price is None or breakout_level is None:
            return False  # missing level/price -> never veto (parity)
        ep = float(entry_price)
        lvl = float(breakout_level)
        # HIGH-2 fail-SAFE: a missing/non-finite ATR (thin low-float runner with no
        # computable volatility) must NOT disarm the chase-guard. Fall back to a=0.0
        # so the cap collapses to the FLAT extension floor and an entry extended beyond
        # the floor still VETOES, instead of being chased far over the break unguarded.
        if atr_pct is None:
            a = 0.0
        else:
            a = float(atr_pct)
            if not math.isfinite(a):
                a = 0.0
        if lvl <= 0 or ep <= 0:
            return False  # bad level/price -> never veto (parity)
        k = float(getattr(settings, "chili_momentum_entry_extension_atr_mult", 8.0))
        floor = float(getattr(settings, "chili_momentum_entry_extension_floor_pct", 0.08))
        cap = max(floor, k * max(0.0, a))
        # EXPLOSIVE RVOL BOOST (GATE 3): a true outlier squeeze is high-RVOL DESPITE a
        # thin regime-ATR (the discovery-phase setup the lane targets), so the clean
        # ATR-derived cap under-leverages it. When the master + boost flags are ON and an
        # RVOL reading is available, widen the cap proportionally to RVOL above the
        # explosive floor, hard-capped at boost_max so a +33% blow-off chase still vetoes.
        # ADDITIVE: master OFF / boost flag OFF / rvol None ⇒ no boost, byte-identical.
        if (
            rvol is not None
            and bool(getattr(settings, "chili_momentum_explosive_recalibration_enabled", False))
            and bool(getattr(settings, "chili_momentum_entry_extension_rvol_boost_enabled", False))
        ):
            try:
                rvol_floor = float(getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 0.0)
                boost_per = float(getattr(settings, "chili_momentum_entry_extension_rvol_boost_per", 0.05) or 0.0)
                boost_max = float(getattr(settings, "chili_momentum_entry_extension_rvol_boost_max", 0.15) or 0.0)
                _excess = max(0.0, float(rvol) - rvol_floor)
                cap += min(boost_max, boost_per * _excess)
            except (TypeError, ValueError):
                pass
        return ep >= lvl * (1.0 + cap)
    except Exception:
        return False  # any error -> fail-open (never block an entry on a bug)


def round_number_entry_context(
    entry_price: float | None,
    breakout_level: float | None,
    atr_pct: float | None,
    *,
    settings_obj: Any = settings,
) -> tuple[bool, str, dict[str, Any]]:
    """GAP 2 (Warrior re-audit): whole/half-dollar ROUND-NUMBER entry-timing CONTEXT.

    Ross: prefer a break-and-HOLD OVER a round number (under / test / hold-over); AVOID
    firing right INTO a round number from BELOW (the overhead supply that clusters at psych
    levels). This is a CONTEXT modifier on the existing breakout triggers, NOT a standalone
    veto: it only DEFERS (returns ``ok=False``) — the caller stays WATCHING and re-enters
    on the hold over the level, EXACTLY like ``_entry_extension_veto``. It NEVER blocks an
    exit and cannot terminalize.

    Returns ``(ok_to_enter, reason, debug)``. ``ok=False`` (defer) ONLY when:
      * a round number sits in the OVERHEAD band just ABOVE the marketable entry (within an
        ATR-scaled tolerance), AND
      * the breakout LEVEL has NOT yet cleared+held that round number (level <= round) — i.e.
        we would be buying INTO overhead supply, not a confirmed hold over it.

    ADDITIVE / byte-identical when the flag is OFF, the level/price/round-number is missing,
    or the level already cleared the round number. Pure; never raises."""
    dbg: dict[str, Any] = {}
    try:
        if not bool(getattr(settings_obj, "chili_momentum_round_number_entry_timing_enabled", False)):
            return True, "round_number_disabled", dbg
        if entry_price is None or breakout_level is None:
            return True, "round_number_no_inputs", dbg
        ep = float(entry_price)
        lvl = float(breakout_level)
        if ep <= 0 or lvl <= 0:
            return True, "round_number_bad_inputs", dbg
        from .daily_levels import _round_number_near

        # ATR-scaled overhead band: how close ABOVE the entry a round number must be to count
        # as "firing right into it". Reuse the entry-extension floor as the ONE documented
        # base when ATR is thin (no scattered magic %).
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
        floor = float(getattr(settings_obj, "chili_momentum_entry_extension_floor_pct", 0.08))
        band = max(floor, 8.0 * a) * 0.25  # a tight overhead band (¼ of the extension cap)
        rn = _round_number_near(ep, a)
        if rn is None:
            return True, "round_number_none_nearby", dbg
        dbg["round_number"] = round(float(rn), 6)
        dbg["round_number_band"] = round(band, 6)
        # OVERHEAD: the round number is just ABOVE the entry (entry < round <= entry*(1+band)).
        if not (ep < float(rn) <= ep * (1.0 + band)):
            return True, "round_number_not_overhead", dbg
        # If the breakout LEVEL has already cleared + holds the round number, this IS the
        # break-and-hold OVER it Ross wants — permit. Only DEFER when the level is at/below
        # the round number (we'd be buying INTO it from below).
        if lvl > float(rn):
            dbg["round_number_held"] = True
            return True, "round_number_break_and_hold", dbg
        return False, "round_number_into_overhead", dbg
    except Exception:
        return True, "round_number_error", dbg  # any error -> permit (never block on a bug)


def micro_pullback_reentry_detect(
    df: Any,
    *,
    shelf: float,
    max_dip_pct: float,
    ema_span: int = 9,
) -> dict[str, Any]:
    """Ross MICRO-PULLBACK re-load geometry on the SESSION-scoped 15s micro-bar frame
    (NOT the 5d frame — the caller passes the ``_build_micro_bar_df`` output). PURE; no
    I/O. Returns ``{"fire": bool, "reason": str, "bounce_high": float|None,
    "dip_low": float|None}``.

    A micro-pullback re-load fires iff ALL hold (price-structure leg; the FLOW gate +
    cushion + caps + cooldown are applied by the caller):
      * the 9-EMA stack is RISING (ema9[-1] >= ema9[-2] — up-structure intact);
      * a higher-low DIP printed: the recent window made a local high (bounce_high),
        then a dip_low ABOVE the ratcheting ``shelf`` (max(starter entry, breakout, or
        the last re-load's higher-low) — the caller persists + ratchets the shelf);
      * the dip is SHALLOW: (bounce_high - dip_low) / bounce_high <= max_dip_pct (a deep
        rollover is NOT a micro-pullback);
      * the LAST bar CURLS BACK UP: it is a green bounce-curl candle (the caller checks
        ``bounce_curl_from_df`` for the per-bar conviction shape) AND prints a higher-low
        (last bar's low >= dip_low - epsilon, the dip held).

    FAIL-SAFE / SUPERSET: a None/empty/short (<10 bars) frame ⇒ no fire (the caller's
    micro-bar build already returns None on sparse tape so a no-tape name never re-loads).
    Any error ⇒ no fire. ADDITIVE: never consulted when the flag is OFF.
    docs/DESIGN/MOMENTUM_LANE.md"""
    out: dict[str, Any] = {"fire": False, "reason": "", "bounce_high": None, "dip_low": None}
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            out["reason"] = "frame_too_sparse"
            return out
        cols = {x.lower(): x for x in df.columns}
        highs = [float(x) for x in df[cols["high"]].tolist()]
        lows = [float(x) for x in df[cols["low"]].tolist()]
        closes = [float(x) for x in df[cols["close"]].tolist()]
        _shelf = float(shelf)
        # Rising 9-EMA stack (up-structure intact).
        from .candles import _ema as _ema_helper
        ema = _ema_helper(closes, int(ema_span))
        if len(ema) < 2 or not (ema[-1] >= ema[-2] - 1e-12):
            out["reason"] = "ema_not_rising"
            return out
        # Bounce-high = max over the recent window; dip_low = the lowest low AFTER that
        # high (the pullback that followed the local high).
        win = min(len(highs), 8)
        seg_h = highs[-win:]
        seg_l = lows[-win:]
        hi_rel = max(range(len(seg_h)), key=lambda i: seg_h[i])
        bounce_high = seg_h[hi_rel]
        if hi_rel >= len(seg_l) - 1:
            out["reason"] = "no_dip_after_high"      # high is the last bar — no pullback yet
            return out
        dip_low = min(seg_l[hi_rel + 1:])
        out["bounce_high"] = bounce_high
        out["dip_low"] = dip_low
        if bounce_high <= 0:
            out["reason"] = "bad_bounce_high"
            return out
        # Higher-low dip must HOLD the ratcheting shelf (not a deep rollover below it).
        if dip_low < _shelf - 1e-9:
            out["reason"] = "dip_below_shelf"
            return out
        # Shallow-dip cap: a deep rollover is not a micro-pullback.
        dip_pct = (bounce_high - dip_low) / bounce_high
        if dip_pct > float(max_dip_pct) + 1e-12:
            out["reason"] = "dip_too_deep"
            return out
        # The dip must HOLD on the last (curl) bar — its low at/above dip_low.
        if lows[-1] < dip_low - 1e-9:
            out["reason"] = "last_bar_undercut_dip"
            return out
        out["fire"] = True
        out["reason"] = "micro_pullback_curl"
        return out
    except Exception:
        out["reason"] = "error"
        return out  # any error -> no fire (an extra BUY needs proof)


def first_pullback_break(
    df: pd.DataFrame,
    *,
    symbol: str | None = None,
    batch: dict[str, dict] | None = None,
    live_price: float | None = None,
    max_pullback_bars: int = 3,
    retracement_threshold: float = 0.50,
    top_fraction: float = 0.50,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[str, float | None, float | None, dict[str, Any]]:
    """Ross's FIRST-PULLBACK entry — the EARLIEST, most aggressive momentum entry.

    Ross buys the FIRST 1m candle to make a NEW HIGH after the FIRST shallow
    pullback off a confirmed up-impulse (he caught JRSH this way for +$21k). The
    existing retest/deep-reclaim ladder structurally enters LATER (on JRSH its only
    setup fired at 09:26 during the collapse → a loss). This gate fires near the
    resumption of the move, not a bar-close after a retest.

    Returns ``(verdict, level, stop, debug)`` mirroring ``_dipbuy_signals_ok``:
      * ``"FIRE"`` — a completed bar (or, downstream, the live tick) broke the
        pullback's prior swing high → enter at ``level`` (that swing high) with the
        stop at the pullback LOW. The vol-floor layer widens the stop downstream
        (INVARIANT A lives there), so this gate does NOT pre-floor it.
      * ``"ARM"``  — the structure is set (explosive + first-pullback + shallow) but
        the new-high has not printed on a completed bar yet → arm a tick-watch at
        ``level``; the caller routes it through the tick-break block + the dipbuy
        tick-thrust buffer.
      * ``"PASS"`` — not explosive / not the first pullback / too deep / no impulse /
        thin data / any error → caller falls through BYTE-IDENTICALLY to the existing
        retest / deep-reclaim ladder (fail-open, never raises).

    GUARD STACK (every yardstick reused — no new magic numbers; CHOP is the dominant
    risk and the explosive + first-pullback-only + depth guards are the defense):
      1. EXPLOSIVE + tradeable-liquid (chop defense #1). With a ``batch`` context the
         name must be a top-fraction mover by ``score_universe`` (the same adaptive
         percentile ranker the selection layer uses) AND carry $-turnover. Per-symbol
         (the live call site has no batch) it must clear the lane's own adaptive RVOL
         floor (``chili_momentum_entry_sustained_rvol_floor`` over the sustain window —
         the SAME yardstick the sustaining-volume gate uses) AND be already moving up
         (a positive impulse — Ross: "never buy what isn't already moving").
      2. FIRST-pullback-only via ``_is_first_pullback`` — no earlier dip below the
         (vol-aware) 9-EMA between the impulse anchor and the peak (an Nth pullback is
         chop).
      3. The trigger = the first candle to make a NEW HIGH above the pullback's prior
         swing high (FIRE on a completed-bar break, ARM when set but unbroken).
      4. Tight stop = the pullback LOW (the vol-floor widening layer applies downstream,
         identically to the dipbuy/raw-break paths).
      5. Depth gate — reject a too-deep/choppy pullback via the shared ``_collapse_cap``
         + the vol-aware shallow cap (the dipbuy depth yardstick).

    docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return "PASS", None, None, {"fp_declined": "insufficient_bars"}
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        # Open series for the Gate 2a distribution-candle veto (fail-open None if absent).
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "volume_ratio", "atr"})
        ema9 = arrays.get("ema_9") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []

        # Instrument volatility (ATR/price) → vol-aware shallow / EMA-wick tolerances
        # (calm name keeps the Ross floor; volatile small-cap gets proportional room).
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        eff_shallow, ema_wick, _ = _vol_aware_pullback_tolerances(atr_pct, retracement_threshold)

        # ── GUARD 1: EXPLOSIVE + tradeable-liquid (chop defense #1) ──────────────
        # Batch context → adaptive universe-percentile ranking (score_universe); the
        # live per-symbol call site has no batch → the lane's own RVOL floor + an
        # already-moving impulse. A thin top-mover you can't exit is REJECTED.
        explosive = False
        if batch:
            try:
                from .ross_momentum import (
                    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
                    score_universe,
                )

                scores = score_universe(
                    batch, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
                )
                sc = scores.get(symbol) if symbol else None
                # top-fraction mover AND has tradeable ($-turnover) liquidity present.
                if sc is not None and sc.in_top_fraction(float(top_fraction)) and (
                    sc.tradeable_liquidity_pct is None or sc.tradeable_liquidity_pct > 0.0
                ):
                    explosive = True
            except Exception:
                explosive = False
        if not explosive:
            rvol_floor = float(
                getattr(settings, "chili_momentum_entry_sustained_rvol_floor", 1.0) or 0.0
            )
            sustain_n = int(getattr(settings, "chili_momentum_entry_sustain_lookback_bars", 5) or 5)
            srv = _sustained_rvol(vr, cur, sustain_n)
            # already-moving: a positive up-impulse over the structure window.
            look = min(20, cur)
            win_high = float(high.iloc[cur - look:cur + 1].max())
            win_low = float(low.iloc[cur - look:cur + 1].min())
            moving_up = (win_high - win_low) > 0 and float(close.iloc[cur]) > float(close.iloc[cur - look])
            # Fail-OPEN on thin RVOL data (srv None) — never block on missing volume;
            # the depth + first-pullback guards still defend chop.
            rvol_ok = (srv is None) or (srv >= rvol_floor)
            if not (rvol_ok and moving_up):
                return "PASS", None, None, {
                    "fp_declined": "not_explosive",
                    "fp_sustained_rvol": (round(srv, 2) if srv is not None else None),
                }

        # ── Impulse + pullback structure (reuse the raw-break anchoring) ─────────
        look = min(20, cur)
        win_high = float(high.iloc[cur - look:cur].max())
        win_low = float(low.iloc[cur - look:cur].min())
        impulse_range = win_high - win_low
        if impulse_range <= 0:
            return "PASS", None, None, {"fp_declined": "no_impulse"}

        # The peak = the impulse high's bar; the pullback = the recent bars before the
        # current bar. Its prior swing HIGH is the breakout level, its LOW the stop.
        hi_v = high.values
        peak_idx = int(high.iloc[cur - look:cur].values.argmax()) + (cur - look)
        pb_start = max(0, cur - int(max_pullback_bars))
        # the swing high to break = the highest completed bar in the pullback window
        # (NOT the current bar — that is the one doing the breaking).
        pb_high = float(high.iloc[pb_start:cur].max())
        pb_low = float(low.iloc[pb_start:cur].min())
        if not (0.0 < pb_low < pb_high):
            return "PASS", None, None, {"fp_declined": "bad_levels"}

        # ── GUARD 2: FIRST pullback only (no earlier dip below the 9-EMA) ────────
        anchor = max(0, peak_idx - look)
        if peak_idx > anchor and not _is_first_pullback(low.values, ema9, anchor, peak_idx, ema_wick):
            return "PASS", None, None, {"fp_declined": "not_first_pullback"}

        # ── Gate 2 (dip-buy quality, flag-gated): distribution-candle veto + impulse-
        # accumulation confirm — the SAME two checks as _dipbuy_signals_ok, applied to
        # the first-pullback geometry. The IMPULSE bars = anchor..peak_idx (the run
        # into the peak); the PULLBACK bars = pb_start..cur-1 (completed bars only —
        # cur is the breaking bar). Both knobs default to a no-op so this whole block
        # is byte-identical when OFF. NaN-safe; fail-open on thin windows.
        _vv_fp = vol.values if hasattr(vol, "values") else None
        if _vv_fp is not None:
            _push_lo = anchor + 1 if (peak_idx - anchor) >= 2 else anchor
            _push_v = [
                float(_vv_fp[i]) for i in range(_push_lo, peak_idx + 1)
                if i < len(_vv_fp) and float(_vv_fp[i]) == float(_vv_fp[i])
            ]
            _push_vm = (sum(_push_v) / len(_push_v)) if _push_v else 0.0
            # Gate 2b — impulse accumulation (sentinel -1 = disabled ⇒ skipped).
            _accum_min = float(getattr(settings, "chili_momentum_dipbuy_impulse_accum_min_slope", -1.0))
            if _accum_min != -1.0 and len(_push_v) >= 2 and _push_vm > 0:
                _pxs = [float(i) for i in range(len(_push_v))]
                _pmx = sum(_pxs) / len(_pxs)
                _pmy = sum(_push_v) / len(_push_v)
                _pcov = sum((x - _pmx) * (y - _pmy) for x, y in zip(_pxs, _push_v))
                _pvar = sum((x - _pmx) ** 2 for x in _pxs)
                if _pvar > 0 and (_pcov / _pvar) / _push_vm < _accum_min:
                    return "PASS", None, None, {"fp_declined": "impulse_not_accumulating"}
            # Gate 2a — high-volume RED (distribution) pullback candle (0 = disabled).
            _dist_mult = float(getattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 0.0))
            if _dist_mult > 0 and opn is not None and _push_vm > 0:
                _ov_fp = opn.values if hasattr(opn, "values") else None
                _cl_fp = close.values if hasattr(close, "values") else None
                if _ov_fp is not None and _cl_fp is not None:
                    for i in range(pb_start, cur):
                        if i >= len(_vv_fp) or i >= len(_ov_fp) or i >= len(_cl_fp):
                            continue
                        _ci, _oi, _vi = _cl_fp[i], _ov_fp[i], _vv_fp[i]
                        if _ci != _ci or _oi != _oi or _vi != _vi:  # NaN-safe
                            continue
                        if float(_ci) < float(_oi) and float(_vi) >= _dist_mult * _push_vm:
                            return "PASS", None, None, {
                                "fp_declined": "distribution_candle",
                                "dist_vol_ratio": round(float(_vi) / _push_vm, 2),
                            }

        # ── GUARD 5: DEPTH — shallow pullback only (reuse the dipbuy yardsticks) ──
        retrace = (win_high - pb_low) / impulse_range
        depth = (win_high - pb_low) / win_high if win_high > 0 else 1.0
        if retrace > eff_shallow or depth > _collapse_cap(atr_pct):
            return "PASS", None, None, {
                "fp_declined": "too_deep",
                "fp_retrace": round(retrace, 3),
                "fp_shallow_cap": round(eff_shallow, 3),
            }
        # Held above the 9-EMA through the pullback (vol-aware wick tolerance).
        ema_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        if ema_cur is not None and pb_low < float(ema_cur) * (1.0 - ema_wick):
            return "PASS", None, None, {"fp_declined": "pullback_below_ema9"}

        debug: dict[str, Any] = {
            "pattern": "first_pullback",
            "first_pullback": True,
            "pullback_high": pb_high,
            "pullback_low": pb_low,
            "fp_retrace": round(retrace, 3),
            "fp_shallow_cap": round(eff_shallow, 3),
            "fp_explosive": True,
        }

        # ── Gate 3 (dip-buy quality, flag-gated): L2 hidden-seller / big-seller veto ──
        # BEFORE the FIRE/ARM returns: an ask-wall the price can't lift, or absorption/
        # micro rollover despite buy-side OFI, makes this break a round-trip → PASS.
        # FAIL-OPEN (helper None on disabled/null/stale L2).
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            return "PASS", None, None, {"fp_declined": _reason, **_l2patch}

        # ── GUARD 3: TRIGGER — first NEW HIGH above the pullback swing high ──────
        cur_hi = float(hi_v[cur]) if cur < len(hi_v) else float(high.iloc[cur])
        if cur_hi <= pb_high:
            # structure set, not broken on a completed bar yet → ARM a tick-watch.
            return "ARM", pb_high, pb_low, debug
        return "FIRE", pb_high, pb_low, debug
    except Exception:
        return "PASS", None, None, {"fp_declined": "error"}


# -- Bull-flag depth band (SS101 #012) ----------------------------------------
# Ross's bull flag is a DEEPER pull than the SHALLOW first_pullback gate accepts and
# is an ALL-DAY pattern (no morning-only cutoff like the deep-reclaim path), so it is a
# DISTINCT gate, NOT a duplicate of either. The transcript pins the depth band exactly:
# "I don't wanna see this pullback ... more than 50% of the move up" (the FLOOR of the
# band -- anything shallower is a first_pullback/micro-pullback, owned by those gates),
# "but pulling back 70% or whatever, that's fine ... as long as it's not more than half"
# i.e. up to ~70% is acceptable but a touch past 50% is the genuine bull-flag zone. We
# encode the band as [first_pullback's vol-aware shallow cap, the bull-flag ceiling]:
# below the floor the SHALLOW gate already owns it; above the ceiling the sellers are in
# control (Ross: "the sellers are in control, and that's not great"). The ceiling is
# vol-aware (a calm name's 70% ceiling, widened by ATR for a volatile small-cap, but
# never past the _collapse_cap reversal floor). ONE documented base each -- the 0.70
# ceiling and its ATR multiplier -- no scattered magic. docs/DESIGN/MOMENTUM_LANE.md
_BULL_FLAG_RETRACE_CEIL = 0.70       # Ross "70% or whatever ... not more than half" upper bound
_BULL_FLAG_RETRACE_CEIL_ATR_MULT = 1.5  # widen the ceiling this x ATR% for volatile names


def bull_flag_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str | None = None,
    symbol: str | None = None,
    batch: dict[str, dict] | None = None,
    live_price: float | None = None,
    max_pullback_bars: int = 3,
    retracement_threshold: float = 0.50,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross BULL FLAG (SS101 #012; flag ``chili_momentum_bull_flag_entry_enabled``).

    The bull flag is an initial 1-3 large green-candle impulse, THEN a 2-3 RED-candle
    pullback that pulls back "well off the high" (DEEPER than the shallow first_pullback
    gate allows, but NOT more than ~50-70% of the impulse), riding down toward the rising
    9-EMA, THEN the FIRST candle to make a NEW HIGH above the pullback's prior swing high
    is the entry (Ross: "the second it breaks ... I'm a buyer"). Entry = the pullback
    swing high; stop = the pullback LOW. Volume profile: HIGH on the impulse, LIGHT on the
    pullback, RETURNS on the break (Ross's defining tell).

    DISTINCT (not a rebuild) from the existing ladder:
      * ``first_pullback_break`` -- SHALLOW only (vol-aware shallow cap). The bull flag
        starts ABOVE that cap (the DEEPER 50-70% pull), so the two partition the depth
        axis: shallow -> first_pullback, deep-but-not-a-reversal -> bull_flag.
      * ``_evaluate_deep_reclaim`` / dip-buy -- MORNING-ONLY (~10:30 ET cutoff). The bull
        flag trades "almost every day" with NO time-of-day gate (ALL-DAY).
      * ``hod_break`` / ``flat_top`` / ``blue_sky`` -- anchor on the day/all-time high or a
        consolidation shelf; the bull flag anchors on the pullback's OWN swing high after
        a deeper dip, regardless of HOD. Different trigger geometry => DISTINCT.

    Returns the shared ``(ok, reason, debug)`` with ``pullback_high`` (= the break/entry
    level) and ``pullback_low`` (= the structural stop) under the IDENTICAL keys, so the
    downstream sizing / structural-stop / breakout-or-bailout machinery + the setup-
    selector reuse it unchanged. ``reason`` is ``bull_flag_break`` on a completed-bar
    break (``bull_flag_break_tick_ok`` on the live-tick break); the WAIT reason is
    ``waiting_for_bull_flag_break`` (tick-armable -- in ``TICK_ARMED_WAIT_REASONS``).

    ANTI-CHASE (every yardstick reused -- never fires on a vertical blow-off): explosive/
    already-moving (the first_pullback chop defense), first-pullback-only via
    ``_is_first_pullback``, the vol-aware EMA-9 hold + depth band, the high-vol-RED
    distribution-candle veto (Ross's "highest volume candle of the day is red ... we
    don't love that profile" / the shooting-star caution), ``is_strong_bull_break_candle``
    on the breaking bar (rejects topping-tail/doji), the backside/rolled-over veto, the
    L2 hidden-seller veto, and the P0 overhead veto fires downstream at the selector.

    ADDITIVE: flag OFF / thin (<12 bars) / not the bull-flag geometry -> ``(False,
    reason, {...})`` with NO side effects; fail-OPEN to a benign decline on any error
    (never raises, never blocks the rest of the ladder). docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "bull_flag"}
    # Compute ALL flag/knob values up front (no local re-import shadowing => no
    # UnboundLocalError class of bug); every downstream read is from these locals.
    try:
        _enabled = bool(getattr(settings, "chili_momentum_bull_flag_entry_enabled", False))
        _rvol_floor = float(getattr(settings, "chili_momentum_entry_sustained_rvol_floor", 1.0) or 0.0)
        _sustain_n = int(getattr(settings, "chili_momentum_entry_sustain_lookback_bars", 5) or 5)
        _dryup_ratio = float(getattr(settings, "chili_momentum_deep_reclaim_dipbuy_dryup_ratio", 0.85) or 0.85)
        _min_close_pos = float(getattr(settings, "chili_momentum_entry_break_candle_min_close_pos", 0.50) or 0.50)
        _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        _dist_mult = float(getattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 0.0) or 0.0)
    except Exception:
        return False, "bull_flag_error", debug

    try:
        if not _enabled:
            return False, "bull_flag_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, "bull_flag_insufficient_bars", debug

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        # Request vwap too so the NOT-PARABOLIC extension guard's VWAP arm gets a REAL series
        # (compute_all_from_df only computes what is requested — an un-requested vwap would
        # silently no-op the extension VWAP arm, a chase hole). Mirrors wedge_break / cup.
        arrays = compute_all_from_df(
            df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "volume_ratio", "atr"}
        )
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []

        # Instrument volatility -> vol-aware tolerances (calm name keeps the Ross floor;
        # a volatile small-cap gets proportional room -- same yardstick as first_pullback).
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
        eff_shallow, ema_wick, _ = _vol_aware_pullback_tolerances(atr_pct, retracement_threshold)

        # -- GUARD 1: EXPLOSIVE + already-moving (chop defense, reused from first_pullback) --
        explosive = False
        if batch:
            try:
                from .ross_momentum import (
                    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
                    score_universe,
                )

                scores = score_universe(batch, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
                sc = scores.get(symbol) if symbol else None
                if sc is not None and sc.in_top_fraction(0.50) and (
                    sc.tradeable_liquidity_pct is None or sc.tradeable_liquidity_pct > 0.0
                ):
                    explosive = True
            except Exception:
                explosive = False
        srv_guard = _sustained_rvol(vr, cur, _sustain_n)
        if not explosive:
            look0 = min(20, cur)
            win_hi0 = float(high.iloc[cur - look0:cur + 1].max())
            win_lo0 = float(low.iloc[cur - look0:cur + 1].min())
            moving_up = (win_hi0 - win_lo0) > 0 and float(close.iloc[cur]) > float(close.iloc[cur - look0])
            rvol_ok = (srv_guard is None) or (srv_guard >= _rvol_floor)
            if not (rvol_ok and moving_up):
                return False, "bull_flag_not_explosive", {
                    **debug,
                    "sustained_rvol": (round(srv_guard, 2) if srv_guard is not None else None),
                }

        # -- IMPULSE + pullback structure (reuse the first_pullback anchoring) -------------
        look = min(20, cur)
        win_high = float(high.iloc[cur - look:cur].max())
        win_low = float(low.iloc[cur - look:cur].min())
        impulse_range = win_high - win_low
        if impulse_range <= 0:
            return False, "bull_flag_no_impulse", debug

        peak_idx = int(high.iloc[cur - look:cur].values.argmax()) + (cur - look)
        pb_start = max(0, cur - int(max_pullback_bars))
        pb_high = float(high.iloc[pb_start:cur].max())
        pb_low = float(low.iloc[pb_start:cur].min())
        if not (0.0 < pb_low < pb_high):
            return False, "bull_flag_bad_levels", debug

        # -- GUARD 2: a 2-3 candle pullback (NOT 1 = micro-pullback, NOT >3 = too much
        # selling). Count the CONTIGUOUS run of pullback bars ending at cur-1 -- a bar that
        # did NOT make a new high above the running peak.
        pull_bars = 0
        ref_hi = float(high.iloc[peak_idx]) if peak_idx <= cur - 1 else pb_high
        for i in range(cur - 1, peak_idx, -1):
            try:
                _hi_i = float(high.iloc[i])
            except (TypeError, ValueError, IndexError):
                break
            if _hi_i < ref_hi:
                pull_bars += 1
            else:
                break
        debug["pull_bars"] = pull_bars
        if pull_bars < 2:
            return False, "bull_flag_micro_not_flag", debug
        if pull_bars > int(max_pullback_bars):
            return False, "bull_flag_too_many_pullback_bars", debug

        # -- GUARD 3: FIRST pullback only (no earlier dip below the 9-EMA band) ------------
        anchor = max(0, peak_idx - look)
        if peak_idx > anchor and not _is_first_pullback(low.values, ema9, anchor, peak_idx, ema_wick):
            return False, "bull_flag_not_first_pullback", debug

        # -- GUARD 4: DEPTH BAND -- DEEPER than the shallow first_pullback cap but NOT a
        # reversal. DEEPER than ``eff_shallow`` (else first_pullback owns it) and SHALLOWER
        # than the vol-aware bull-flag ceiling (<= ~70%, widened by ATR, hard-capped 0.90)
        # and within the shared _collapse_cap.
        retrace = (win_high - pb_low) / impulse_range
        depth = (win_high - pb_low) / win_high if win_high > 0 else 1.0
        flag_ceil = min(_BULL_FLAG_RETRACE_CEIL + a * _BULL_FLAG_RETRACE_CEIL_ATR_MULT, 0.90)
        # Adaptive Ross retrace ceiling (default OFF -> 0.0 -> no-op, byte-identical).
        # When enabled it TIGHTENS the retrace axis toward Ross's ~50% for CALM names
        # while widening for volatile names (so explosive deeper pulls still pass). Only
        # tightens (min with flag_ceil); never relaxes the existing ceiling.
        adaptive_ceiling = _adaptive_pullback_depth_ceiling(
            atr_pct, settings.chili_momentum_adaptive_pullback_depth_ceiling_enabled
        )
        eff_ceil = min(flag_ceil, adaptive_ceiling) if adaptive_ceiling > 0 else flag_ceil
        debug["bull_flag_retrace"] = round(retrace, 3)
        debug["bull_flag_floor"] = round(eff_shallow, 3)
        debug["bull_flag_ceil"] = round(eff_ceil, 3)
        if adaptive_ceiling > 0:
            debug["bull_flag_adaptive_ceil"] = round(adaptive_ceiling, 3)
        if retrace <= eff_shallow:
            return False, "bull_flag_too_shallow_is_first_pullback", debug
        if retrace > eff_ceil or depth > _collapse_cap(atr_pct):
            return False, "bull_flag_too_deep", debug

        # -- GUARD 5: held the 9-EMA band through the pullback (vol-aware wick tolerance).
        ema_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        if ema_cur is not None and pb_low < float(ema_cur) * (1.0 - ema_wick):
            return False, "bull_flag_below_ema9", debug

        # -- GUARD 6: VOLUME PROFILE -- light on the pullback, high on the impulse; the
        # high-vol-RED distribution-candle veto. Sentinel 0 = disabled -> skipped.
        _vv = vol.values if hasattr(vol, "values") else None
        if _vv is not None:
            _push_lo = anchor + 1 if (peak_idx - anchor) >= 2 else anchor
            _push_v = [
                float(_vv[i]) for i in range(_push_lo, peak_idx + 1)
                if i < len(_vv) and float(_vv[i]) == float(_vv[i])
            ]
            _push_vm = (sum(_push_v) / len(_push_v)) if _push_v else 0.0
            _pull_v = [
                float(_vv[i]) for i in range(pb_start, cur)
                if i < len(_vv) and float(_vv[i]) == float(_vv[i])
            ]
            _pull_vm = (sum(_pull_v) / len(_pull_v)) if _pull_v else 0.0
            if _push_vm > 0 and _pull_vm > 0:
                _dry = _pull_vm / _push_vm
                debug["pullback_dryup_ratio"] = round(_dry, 3)
                if _dry > _dryup_ratio:
                    return False, "bull_flag_pullback_not_dry", debug
            if _dist_mult > 0 and opn is not None and _push_vm > 0:
                _ov = opn.values if hasattr(opn, "values") else None
                _cl = close.values if hasattr(close, "values") else None
                if _ov is not None and _cl is not None:
                    for i in range(pb_start, cur):
                        if i >= len(_vv) or i >= len(_ov) or i >= len(_cl):
                            continue
                        _ci, _oi, _vi = _cl[i], _ov[i], _vv[i]
                        if _ci != _ci or _oi != _oi or _vi != _vi:
                            continue
                        if float(_ci) < float(_oi) and float(_vi) >= _dist_mult * _push_vm:
                            return False, "bull_flag_distribution_candle", {
                                **debug, "dist_vol_ratio": round(float(_vi) / _push_vm, 2),
                            }

        # -- ANTI-CHASE: backside / rolled-over top veto (reused, fail-OPEN) ---------------
        # _detect_back_side reads the 1m EMA/MACD rollover; front_side_state reads WHERE the
        # name sits in its OWN session (below VWAP / faded — Ross never buys below VWAP). The
        # front_side read fails CLOSED on a thin/degenerate frame (this is a new-conviction
        # fire path). Mirrors wedge_break / cup_and_handle.
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "bull_flag_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "bull_flag_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            # thin/degenerate frame -> fail-CLOSED for this new-conviction fire path.
            debug["reason"] = "front_side_read_error"
            return False, "bull_flag_backside_lifecycle", debug

        # -- L2 hidden-seller veto (reused; fail-open on disabled/null/stale L2) -----------
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"bull_flag_{_reason}", debug

        level = pb_high
        stop = pb_low
        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["sustained_rvol"] = round(srv_guard, 2) if srv_guard is not None else None

        # -- NOT PARABOLIC: extension vs the 9-EMA AND VWAP (the blow-off defense) ---------
        # The pullback-swing-high break level (= the entry) must not sit excessively extended
        # above the 9-EMA / VWAP — a vertical run INTO the break is a parabolic blow-off, not a
        # tested flag break. Reuses the SAME adaptive ATR extension yardstick the chase veto
        # uses (fail-OPEN on a missing reference so thin data never blocks). Mirrors wedge/cup.
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "bull_flag_extended", debug

        # -- TICK-BREAK: the live ask already trading through the swing high ("the second it
        # breaks ... I'm a buyer"), gated by the premarket + dipbuy thrust buffers.
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            # -- TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire without buyers on tape) --
            # Mirrors wedge / cup: buyers must be actively lifting the ask THIS tick. Any
            # disabled-flag / no-tape / thin / stale / crypto / error ⇒ NO fire.
            _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
            debug["tape_reason"] = _tape_dbg.get("reason")
            if not _tape_ok:
                return False, "bull_flag_tape_unconfirmed", debug
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "bull_flag_break_tick_ok", debug

        cur_hi = float(high.iloc[cur])
        if cur_hi <= level:
            return False, "waiting_for_bull_flag_break", debug

        # -- A completed bar broke the swing high -> require a STRONG bull break candle
        # (reject topping-tail/doji) AND volume RETURN on the break.
        try:
            from .candles import is_strong_bull_break_candle

            _o = float(opn.iloc[cur]) if opn is not None else float(close.iloc[cur])
            _h = float(high.iloc[cur])
            _l = float(low.iloc[cur])
            _c = float(close.iloc[cur])
            if not is_strong_bull_break_candle(_o, _h, _l, _c, min_close_pos=_min_close_pos):
                return False, "bull_flag_weak_break_candle", debug
        except Exception:
            pass

        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < _vol_mult:
            return False, "bull_flag_low_volume", debug

        # -- TAPE REQUIRED + FAIL-CLOSED (the LAST gate before the completed-bar fire too) --
        # Mirrors wedge / cup: a completed-bar break with no buyers lifting the ask is a dead
        # break — never chase it. Any disabled-flag / no-tape / thin / stale / crypto / error
        # ⇒ NO fire.
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "bull_flag_tape_unconfirmed", debug
        return True, "bull_flag_break", debug
    except Exception:
        return False, "bull_flag_error", {"entry_interval": entry_interval, "pattern": "bull_flag"}



def _today_session_frame(df):
    """Slice a (possibly multi-day) intraday OHLCV frame to its most recent session for the
    SESSION-anchored backside read. front_side_state anchors on the frame first bar + cumulative
    VWAP + day-range, so it must see TODAY only; the live runner fetches period=5d. Returns
    today bars when the index is a DatetimeIndex spanning >1 date; otherwise returns the frame
    unchanged (single-session or non-datetime -> front_side_state own fail-open applies).
    Premarket-inclusive (matches front_side_state contract)."""
    try:
        idx = df.index
        if isinstance(idx, pd.DatetimeIndex) and len(idx) > 1:
            # Key the session on EXCHANGE-LOCAL (ET) date, not UTC: the lane trades extended
            # hours (04:00-20:00 ET) and in winter after-hours (16:00-20:00 ET) the session
            # straddles UTC midnight, so a UTC-date key would slice off the morning. The live
            # index is tz-aware UTC; convert to ET. tz-naive -> fall back to its naive date.
            if idx.tz is not None:
                d = idx.tz_convert("America/New_York").date
            else:
                d = idx.date
            last = d[-1]
            if d[0] != last:
                return df[d == last]
    except Exception:
        pass
    return df


def evaluate_sticky_backside_bench(
    df,
    *,
    benched_at_hod: float | None,
    live_price: float | None = None,
) -> tuple[bool, str, float | None, dict[str, Any]]:
    """STICKY BACK-SIDE BENCH (BATCH B FIX 1) — the SESSION-LEVEL latch the per-tick
    front_side_state veto cannot give on its own.

    The per-tick ``front_side_state`` / ``_detect_back_side`` vetoes recompute backside EACH
    tick, so a name that rolled over midday gets RE-ARMED on the next MACD pivot — chasing a
    dead, rolled-over top. Ross BENCHES a name once it is on the back side for the rest of the
    move. This helper computes the latch decision from TODAY's session frame and the EXISTING
    bench marker the caller persists in the session ledger.

    Returns ``(benched, reason, benched_at_hod_out, debug)`` where:
      * ``benched`` True  -> the name is on the back side AND has NOT made a genuine new high
        -> the caller VETOES the entry this tick and keeps the bench latched.
      * ``benched`` False -> NOT benched (front-side / unknown / thin data) OR a GENUINE NEW
        HIGH cleared a prior bench (the MANDATORY un-bench) -> the caller may proceed.
      * ``benched_at_hod_out`` -> the HOD to persist as the bench anchor (set when latching;
        ``None`` when the bench is cleared by a new high so the marker is dropped).

    MANDATORY UN-BENCH: a genuine NEW HIGH — the session HOD (or a live tick) ABOVE the HOD
    at which the name was benched — clears the bench. A name that truly resumes a new leg can
    still trade; the bench is NEVER a permanent ban. This mirrors ``front_side_state``'s own
    "a fresh HOD is never chasing_top" rule, extended to a session-persistent latch.

    Pure + side-effect-free (the caller owns the persisted marker). Fail-OPEN on any error /
    thin data: returns ``benched=False`` so a bug can never bench (or strand) a name.
    """
    debug: dict[str, Any] = {}
    try:
        from .ross_momentum import front_side_state

        _sess = _today_session_frame(df)
        # the current session HOD, extended by the live tick when present (a live tick over
        # the completed-bar HOD IS the new high the frame cannot see yet).
        cur_hod = None
        try:
            cur_hod = float(_sess["High"].astype(float).max())
        except (TypeError, ValueError, KeyError):
            cur_hod = None
        if live_price is not None:
            try:
                lp = float(live_price)
                cur_hod = lp if cur_hod is None else max(cur_hod, lp)
            except (TypeError, ValueError):
                pass
        debug["cur_hod"] = cur_hod

        # ── MANDATORY UN-BENCH: a genuine NEW HIGH above the benched-at HOD clears it ───
        if benched_at_hod is not None:
            try:
                if cur_hod is not None and float(cur_hod) > float(benched_at_hod):
                    debug["unbenched_new_high"] = {
                        "benched_at_hod": round(float(benched_at_hod), 6),
                        "cur_hod": round(float(cur_hod), 6),
                    }
                    return False, "unbenched_fresh_hod", None, debug
            except (TypeError, ValueError):
                pass
            # ── WAVE-4 ITEM-5: VWAP-RECLAIM CROSS UN-BENCH (all latch reasons) ───────────
            # A benched name that makes a genuine fresh CROSS-from-below of session VWAP has
            # resumed a real leg even without a new HOD (JEM 12:50 8.97->9.06 into 9.0->9.7).
            # CROSS (state change) not level test: the PRIOR completed close was BELOW
            # VWAP*(1-buffer) AND the current px is AT/ABOVE VWAP. A level test alone would
            # un-bench into a hover-then-dump (JEM 13:24); the cross preserves that veto.
            if bool(getattr(settings, "chili_momentum_backside_bench_reclaim_unbench_enabled", True)):
                try:
                    _fs = front_side_state(_sess)
                    _vwap = getattr(_fs, "session_vwap", None)
                    _closes = _sess["Close"].astype(float)
                    if _vwap is not None and float(_vwap) > 0 and len(_closes) >= 2:
                        _vwap = float(_vwap)
                        _buf = 0.0
                        try:
                            _buf = max(0.0, float(getattr(
                                settings, "chili_momentum_entry_vwap_hold_buffer", 0.0) or 0.0))
                        except (TypeError, ValueError):
                            _buf = 0.0
                        _prior_close = float(_closes.iloc[-1])   # last COMPLETED bar close
                        _cur_px = _prior_close
                        if live_price is not None:
                            try:
                                _lp = float(live_price)
                                if _lp > 0:
                                    _cur_px = _lp
                            except (TypeError, ValueError):
                                pass
                        # CROSS: prior completed close genuinely BELOW VWAP (past the buffer)
                        # AND current px at/above VWAP. Both legs required — a name that was
                        # already at/above VWAP (no fresh cross) STAYS benched (the 13:24 dump).
                        _was_below = _prior_close < _vwap * (1.0 - _buf)
                        _now_above = _cur_px >= _vwap * (1.0 - _buf)
                        if _was_below and _now_above and _cur_px > _prior_close:
                            debug["unbenched_vwap_reclaim"] = {
                                "prior_close": round(_prior_close, 6),
                                "cur_px": round(_cur_px, 6),
                                "session_vwap": round(_vwap, 6),
                                "vwap_hold_buffer": round(_buf, 6),
                                "benched_at_hod": round(float(benched_at_hod), 6),
                            }
                            return False, "unbenched_vwap_reclaim", None, debug
                except (TypeError, ValueError, AttributeError, KeyError, IndexError):
                    pass  # any error -> keep the sticky latch (safe: never strand on a bug)
            # still benched (no new high, no VWAP-reclaim cross) -> keep the latch.
            debug["still_benched"] = True
            return True, "benched_backside_sticky", float(benched_at_hod), debug

        # ── not yet benched: latch ONLY on a CONFIRMED session back side ────────────────
        _fs = front_side_state(_sess)
        if not getattr(_fs, "is_backside", False):
            return False, "front_side", None, debug
        _reason = getattr(_fs, "reason", "backside")
        # chasing_top with a LIVE NEW HIGH is front-side RIGHT NOW (the completed-bar
        # rolled-over read is stale) -> do NOT latch (mirrors the per-tick carve-out).
        if _reason == "chasing_top" and live_price is not None and cur_hod is not None:
            try:
                _frame_hod = float(_sess["High"].astype(float).max())
                if float(live_price) > _frame_hod:
                    debug["live_new_high"] = _reason
                    return False, "front_side_live_new_high", None, debug
            except (TypeError, ValueError, KeyError):
                pass
        # ── FIX D: BELOW-VWAP RECLAIM-FROM-BELOW EXCEPTION ──────────────────────────────
        # The w0av0u3qy replay showed this below_vwap bench ATE the SDOT/ILLR early pushes
        # (44x/14x) — names that dipped below VWAP for a tick but were RECLAIMING it from
        # below with upward momentum. Do NOT latch a below_vwap bench when the name is
        # actively reclaiming: current price has crossed BACK to/above VWAP (within the
        # existing vwap_hold_buffer tolerance) AND is RISING vs the prior bar (positive
        # reclaim direction). A name still FALLING below VWAP (price below VWAP-buffer, or
        # not rising) STAYS benched — the genuine-backside fade veto is untouched. ONLY the
        # below_vwap reason qualifies (already_faded / chasing_top still latch). Flag OFF ->
        # this whole block is skipped -> below_vwap latches exactly as before (byte-identical).
        if _reason == "below_vwap" and bool(
            getattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", True)
        ):
            try:
                _vwap = getattr(_fs, "session_vwap", None)
                # current price: live tick over the completed close when present.
                _closes = _sess["Close"].astype(float)
                _last_close = float(_closes.iloc[-1])
                _cur_px = _last_close
                if live_price is not None:
                    try:
                        _lp = float(live_price)
                        if _lp > 0:
                            _cur_px = _lp
                    except (TypeError, ValueError):
                        pass
                _prior_close = float(_closes.iloc[-2]) if len(_closes) >= 2 else None
                if _vwap is not None and float(_vwap) > 0 and _prior_close is not None:
                    _vwap = float(_vwap)
                    _buf = 0.0
                    try:
                        _buf = max(0.0, float(getattr(
                            settings, "chili_momentum_entry_vwap_hold_buffer", 0.0) or 0.0))
                    except (TypeError, ValueError):
                        _buf = 0.0
                    # RECLAIM-from-below: price has crossed back to/above VWAP (within the
                    # below-VWAP hold tolerance) AND is rising vs the prior bar. The buffer
                    # is the SAME tolerance the entry vwap-hold gate uses (one documented
                    # base) — a price at >= VWAP*(1-buf) counts as reclaiming the level.
                    _reclaimed = _cur_px >= _vwap * (1.0 - _buf)
                    _rising = _cur_px > _prior_close
                    if _reclaimed and _rising:
                        debug["vwap_reclaim_exception"] = {
                            "cur_px": round(_cur_px, 6),
                            "session_vwap": round(_vwap, 6),
                            "prior_close": round(_prior_close, 6),
                            "vwap_hold_buffer": round(_buf, 6),
                        }
                        # NOT benched — the name is reclaiming VWAP from below. Downstream
                        # chase-guards + tape-required + structural stop + max-loss circuit
                        # all still gate the entry; this only declines to LATCH the bench.
                        return False, "front_side_vwap_reclaim", None, debug
                    debug["vwap_reclaim_declined"] = {
                        "cur_px": round(_cur_px, 6),
                        "session_vwap": round(_vwap, 6),
                        "reclaimed": bool(_reclaimed),
                        "rising": bool(_rising),
                    }
            except (TypeError, ValueError, AttributeError, KeyError, IndexError):
                pass  # any error -> fall through to the normal below_vwap latch (safe)
        # CONFIRMED backside -> LATCH the bench at the current session HOD (the anchor the
        # un-bench compares against). below_vwap / already_faded / a rolled-over chasing_top.
        debug["latched"] = _reason
        return True, f"benched_backside_{_reason}", cur_hod, debug
    except (TypeError, ValueError, AttributeError, KeyError):
        # thin / degenerate frame or any error -> fail-OPEN (never bench on a bug).
        return False, "bench_fail_open", None, debug


# ── FIX C: TAPE-CONFIRMED-HOLD early entry (graduate the L2 confirmer to a TRIGGER) ──
# Ross buys the pullback-HOLD bounce when the TAPE confirms buyers, BEFORE the confirmed
# break (the choppy explosive names that never cleanly cross the pullback high inside the
# watch window). This pair of helpers supplies the two NEW gates the early-fire path needs;
# every existing veto + the quote gate still run downstream (the early-fire only promotes a
# WATCHING session to LIVE_ENTRY_CANDIDATE, which routes through the full veto chain).

# The arm-trigger families a tape-confirmed-hold early-fire is VALID on: a real pullback that
# HELD the 9-EMA formed (these wait reasons are emitted ONLY after the pullback structure is
# confirmed and the gate is waiting on the BREAK). A cold/blow-off/no-structure arm never sets
# one of these, so a tape thrust alone can never manufacture an entry on a non-pullback name.
TAPE_HOLD_VALID_WAIT_REASONS = (
    "waiting_for_break",
    "waiting_for_reclaim",
    "waiting_for_reclaim_high",
    "waiting_for_dipbuy_break",
    "waiting_for_first_pullback_break",
    "waiting_for_vwap_reclaim",
)


def tape_confirms_hold(
    symbol: str | None, *, db: Any = None, settings: Any = settings, l2_as_of: Any = None
) -> tuple[bool, dict[str, Any]]:
    """STRICT, FAIL-CLOSED tape confirmer for the FIX C early entry (condition 2).

    REQUIRES the executed tape to actively confirm a bounce: ``signed_tape_accel > 0``
    (back-half aggressor-signed buy volume exceeds the front-half — buyers lifting the ask
    THIS tick) AND ``tick_rate >= tick_rate_floor`` (recent activity at/above its self-
    relative floor). This is the SAME tape primitive ``_l2_entry_confirm`` reads, but where
    that DEFER-gate FAILS OPEN (missing/thin tape ⇒ confirm), this confirmer FAILS CLOSED:
    any disabled flag / no symbol / no db / crypto / empty-or-thin tape / stale / error ⇒
    returns ``(False, ...)``. So a missing tape NEVER produces an early fire — the caller
    keeps waiting on the existing break trigger. Pure read (no writes).

    CAPTURE-G1(b) DECOUPLE (2026-07-03): this confirmer now keys on TAPE AVAILABILITY, not on
    the FIX-C early-fire flag. Previously it short-circuited to ``(False, tape_hold_disabled)``
    whenever ``chili_momentum_tape_hold_entry_enabled`` was OFF (the deployed default) — which
    silently made this a hard-False LAST gate for the TWELVE pattern triggers that require it
    (bull_flag, wedge, absorption_snap, false_break_reclaim, ask_thins_dip, sub_vwap_trap,
    pulling_away_roc, premarket_pivot_macd, inverse_h&s, cup_and_handle, bottom_reversal) plus
    the momentum-continuation entry — so those setups could NEVER fire live even though
    WAVE-4 R4 had flipped cup_and_handle ON as a "proven filler". Two distinct concerns were
    fused into one flag. Now: the FIX-C EARLY-FIRE path stays governed by
    ``chili_momentum_tape_hold_entry_enabled`` at its own call sites (live_runner), while THIS
    inline gate evaluates the executed tape whenever it is dense+healthy and FAILS CLOSED on
    genuinely missing/thin/stale/crypto tape EXACTLY as before (``signed_tape_accel_features``
    returns None on <3 ticks / no db / crypto / error ⇒ ``(False, tape_hold_no_data)``). The
    fail-closed floor is unchanged — a name with no buyers on tape still never fires.

    KILL-SWITCH ``chili_momentum_pattern_tape_gate_enabled`` (default True) ⇒ OFF restores the
    legacy hard-False short-circuit (the 12 triggers go dark again) for instant rollback."""
    dbg: dict[str, Any] = {"reason": "tape_hold_no_data"}
    try:
        if not bool(getattr(settings, "chili_momentum_pattern_tape_gate_enabled", True)):
            # Rollback kill-switch ONLY: OFF reverts to the pre-decouple hard-False behavior
            # (every dependent trigger's tape gate refuses ⇒ dark), for a one-flag revert.
            dbg["reason"] = "tape_hold_disabled"
            return False, dbg
        if db is None or not symbol:
            return False, dbg  # fail-CLOSED on missing inputs
        try:
            w = float(getattr(settings, "chili_momentum_l2_confirm_window_s", 15.0) or 15.0)
        except (TypeError, ValueError):
            w = 15.0
        tape = signed_tape_accel_features(symbol, db=db, window_s=w, as_of=l2_as_of)
        if tape is None:
            return False, dbg  # empty / thin / crypto / error -> fail-CLOSED (no early fire)
        accel = float(tape.get("signed_tape_accel", 0.0))
        tick_rate = float(tape.get("tick_rate", 0.0))
        tick_rate_floor = float(tape.get("tick_rate_floor", 0.0))
        dbg.update({
            "signed_tape_accel": round(accel, 6),
            "tick_rate": round(tick_rate, 4),
            "tick_rate_floor": round(tick_rate_floor, 4),
            "n_ticks": int(tape.get("n_ticks", 0)),
        })
        # REQUIRED: buyers actively lifting the ask this tick (accel>0) AND active (tick_rate
        # at/above its own self-relative floor). Either leg failing ⇒ no early fire.
        if accel > 0.0 and tick_rate >= tick_rate_floor:
            dbg["reason"] = "tape_hold_confirmed"
            return True, dbg
        dbg["reason"] = "tape_hold_not_confirmed"
        return False, dbg
    except Exception:
        dbg["reason"] = "tape_hold_error"
        return False, dbg  # any error -> fail-CLOSED


def buyers_confirmed(
    symbol: str | None, *, db: Any = None, settings: Any = settings, l2_as_of: Any = None
) -> tuple[bool, dict[str, Any]]:
    """UNIFIED, CRYPTO-SAFE 'are buyers actually lifting right now' confirmer for the
    HOT-TAPE-ONLY touch triggers (wick_reclaim, micro_pullback_primary) — the ones that
    otherwise fire on price/level GEOMETRY alone (adversarial review 07-04: gating just one
    relocates the too-early fire to the other; this is the ONE gate for BOTH).

    EQUITY (non ``-USD``): reuse the VALIDATED trade-tape confirmer ``tape_confirms_hold``
    (signed_tape_accel>0 AND tick_rate>=self-relative floor). FAIL-CLOSED on missing/thin
    tape — a HOT-TAPE trigger has dense ticks by construction (_is_hot_tape already required
    RVOL/ATR), so a genuine data-miss here is rare and refusing is correct (no buyers => no
    fire). Accurate-FSM proof (CELZ 06-30): this gate filtered 6 entries -> 2 cleaner ones
    (+$229 -> +$269).

    CRYPTO (``-USD``, no iqfeed trade tape — signed_tape_accel is None there): use the L2
    book OFI (``_live_ofi_microprice`` ofi_level, which reads the Coinbase ring / fast_orderbook
    and is defined for crypto). ofi_level>0 = bid-side aggression (buyers). FAIL-OPEN when the
    book is unreadable (the per-process ring can be empty for a subscribed name — must NOT
    silently disable crypto wick_reclaim/micro_pullback, the review's crypto-exclusion finding).

    KILL-SWITCH ``chili_momentum_buyers_confirm_enabled`` (default True): OFF => (True, ...) so
    the touch triggers fire on geometry alone exactly as before this change. Pure read."""
    dbg: dict[str, Any] = {"reason": "buyers_no_symbol"}
    s = (symbol or "").strip().upper()
    if not s:
        return True, dbg  # bad input -> fail-OPEN (never block a trigger on a missing symbol)
    if not bool(getattr(settings, "chili_momentum_buyers_confirm_enabled", True)):
        return True, {"reason": "buyers_confirm_disabled"}
    if s.endswith("-USD"):
        # CRYPTO: L2 book OFI; FAIL-OPEN on an unreadable/empty book (per-process ring).
        try:
            from .pipeline import _live_ofi_microprice

            ofi, _micro = _live_ofi_microprice(s, db=db, as_of=l2_as_of)
            if ofi is None:
                return True, {"reason": "crypto_book_no_data_fail_open"}
            dbg = {"reason": "crypto_ofi", "ofi_level": round(float(ofi), 5)}
            if float(ofi) > 0.0:
                dbg["reason"] = "crypto_buyers_ofi_pos"
                return True, dbg
            dbg["reason"] = "crypto_buyers_ofi_nonpos"
            return False, dbg
        except Exception:
            return True, {"reason": "crypto_ofi_error_fail_open"}
    # EQUITY: the validated fail-CLOSED trade-tape confirmer.
    return tape_confirms_hold(s, db=db, settings=settings, l2_as_of=l2_as_of)


def tape_confirmed_hold_trigger(
    df,
    *,
    pullback_high: float | None,
    pullback_low: float | None,
    live_price: float | None,
    retracement_threshold: float = 0.50,
    entry_interval: str = "5m",
) -> tuple[bool, str, dict[str, Any]]:
    """FIX C conditions (3) + (4) on the bar frame — the STRUCTURE side of the early fire.

    Returns ``(ok, reason, debug)``; ``debug`` carries ``pullback_high`` / ``pullback_low``
    under the SAME keys the break triggers use, so the caller reuses the IDENTICAL
    structural-stop + breakout-or-bailout machinery. ``ok=True`` ONLY when:

      (3) price is HOLDING / turning up off the 9-EMA — the current close is within the
          vol-aware ``ema_wick`` band of the 9-EMA (``close >= ema9 * (1 - ema_wick)``) AND it
          is a HIGHER LOW vs the pullback low (the dip held, did not break down). The live
          tick (when above the bar close) is used as the current price so a fast turn is
          seen sub-bar. It does NOT require ``close > pullback_high`` (that is the BREAK the
          existing trigger waits for — the whole point of the earlier fire).
      (4) NOT backside: ``_detect_back_side`` clears AND ``front_side_state`` reports not
          ``is_backside`` (which already folds in BELOW-VWAP — Ross never buys below VWAP).

    PROTECTIVE / FAIL-CLOSED: a thin / degenerate frame, a missing level, a price that has
    broken the structure low, OR any error ⇒ ``ok=False`` (keep waiting on the break). It can
    NEVER fire on an extended / faded / rolled-over name (the backside read blocks those, and
    the downstream extension + overhead vetoes back it up). Pure; no I/O or mutation.
    """
    debug: dict[str, Any] = {"reason": "tape_hold_struct_wait"}
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            debug["reason"] = "insufficient_bars"
            return False, "tape_hold_struct_wait", debug
        pb_high = None if pullback_high is None else float(pullback_high)
        pb_low = None if pullback_low is None else float(pullback_low)
        if pb_low is None or not (pb_low > 0):
            debug["reason"] = "no_structural_low"
            return False, "tape_hold_struct_wait", debug

        close = df["Close"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "atr"})
        ema9 = arrays.get("ema_9") or []
        atr = arrays.get("atr") or []

        # current price = the live tick when it is ABOVE the completed-bar close (a fast turn
        # the bar has not closed yet), else the bar close. Never LOWERS the price (so a stale
        # tick can only make the gate stricter, never manufacture a hold). Fail-closed if no
        # usable price.
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            debug["reason"] = "no_close"
            return False, "tape_hold_struct_wait", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass
        if cur_px <= 0:
            debug["reason"] = "no_price"
            return False, "tape_hold_struct_wait", debug

        # vol-aware EMA-9 hold band (same yardstick the pullback gates use).
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            if _a is not None and cur_close > 0:
                atr_pct = _a / cur_close
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        _shallow, ema_wick, _retest = _vol_aware_pullback_tolerances(atr_pct, retracement_threshold)
        debug.update({
            "pullback_high": pb_high,
            "pullback_low": pb_low,
            "atr_pct": (round(atr_pct, 6) if atr_pct is not None else None),
            "ema_wick": round(float(ema_wick), 6),
            "cur_px": round(cur_px, 6),
        })

        # (3a) HIGHER LOW vs the pullback low — the dip held, never broke the structural low.
        if cur_px <= pb_low:
            debug["reason"] = "broke_structural_low"
            return False, "tape_hold_struct_wait", debug

        # (3b) HOLDING the 9-EMA band — current price at/above ema9*(1-ema_wick).
        e9 = None
        try:
            e9 = float(ema9[cur]) if cur < len(ema9) and ema9[cur] is not None else None
        except (TypeError, ValueError, IndexError):
            e9 = None
        if e9 is None or e9 <= 0:
            debug["reason"] = "no_ema9"
            return False, "tape_hold_struct_wait", debug  # fail-CLOSED: no EMA -> no early fire
        debug["ema9"] = round(e9, 6)
        if cur_px < e9 * (1.0 - float(ema_wick)):
            debug["reason"] = "below_ema9_band"
            return False, "tape_hold_struct_wait", debug

        # (4) NOT BACKSIDE: _detect_back_side (1m EMA/MACD rollover) AND front_side_state
        # (which already folds BELOW-VWAP into is_backside). Either reading backside ⇒ block.
        try:
            arrays2 = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal"})
            _e9 = arrays2.get("ema_9") or []
            _e20 = arrays2.get("ema_20") or []
            _macd = arrays2.get("macd") or []
            _msig = arrays2.get("macd_signal") or []
            _bs, _bs_reason = _detect_back_side(_e9, _e20, _macd, _msig, cur)
            if _bs:
                debug["reason"] = "backside_detected"
                debug["backside_reason"] = _bs_reason
                return False, "tape_hold_backside", debug
        except Exception:
            # fail-CLOSED on the backside read for THIS new fire path: if we cannot prove
            # the name is front-side, do NOT take the earlier-than-break entry.
            debug["reason"] = "backside_read_error"
            return False, "tape_hold_struct_wait", debug
        try:
            from .ross_momentum import front_side_state
            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            debug["front_side_reason"] = getattr(_fs, "reason", None)
            if getattr(_fs, "is_backside", False):
                debug["reason"] = "front_side_backside"
                return False, "tape_hold_backside", debug
        except Exception:
            debug["reason"] = "front_side_read_error"
            return False, "tape_hold_struct_wait", debug

        debug["reason"] = "tape_hold_ok"
        return True, "tape_hold_ok", debug
    except Exception:
        debug["reason"] = "tape_hold_struct_error"
        return False, "tape_hold_struct_wait", debug


def momentum_continuation_trigger(
    df,
    *,
    live_price: float | None,
    entry_interval: str = "5m",
    swing_lookback: int = 6,
    symbol: str | None = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """FIX 1 — MOMENTUM-CONTINUATION new-high trigger (the STRUCTURE side).

    The strongest movers (WSHP +47% 40x RVOL, SDOT +25% 132x RVOL) trend STRAIGHT UP and
    never give the pullback / consolidation base every OTHER trigger requires, so they are
    caught + watched but never enter, then reaped at 300s while the lane trades weaker
    pullback names. Ross BUYS the continuation: a fresh new high on a front-side name that
    is NOT parabolic. This is the structural new-high check that does NOT require a prior
    pullback (unlike pullback_break) and does NOT require a tested consolidation base
    (unlike ``hod_break_confirmation`` — the straight-up runner has no base).

    Returns the shared ``(ok, reason, debug)`` 3-tuple. ``debug`` carries ``pullback_low``
    (= the recent swing low / structural stop) and ``pullback_high`` (= the broken recent
    high / breakout-or-bailout level) under the SAME keys the break triggers use, so the
    downstream sizing / structural-stop / breakout-or-bailout machinery is reused unchanged.
    ``reason`` is ``momentum_continuation`` on a completed-bar new high, ``momentum_continuation_tick``
    on a live-tick break (caller's tick path); the WAIT reason is the tick-armable
    ``waiting_for_break`` (so tick-speed dispatch fires the instant the ask trades through).

    CONDITION (2) NEW HIGH: ``cur_px`` makes a fresh high above the recent completed-bar high
    (the prior ``swing_lookback`` bars, EXCLUDING the current bar) — a genuine continuation
    break, NOT a stale level. The live tick (when above the bar close) is the current price
    so a fast break is seen sub-bar; it NEVER lowers the price (a stale tick only makes the
    gate stricter).

    CONDITION (4) NOT PARABOLIC — the #1 chase guard: ``_hod_extension_ok`` (the SAME adaptive
    ATR-scaled ``_entry_extension_veto`` cap measured vs BOTH the 9-EMA and session VWAP)
    rejects a break level sitting excessively extended above the 9-EMA / VWAP (a vertical
    blow-off, not a continuation). The downstream LIVE_PENDING_ENTRY ``_entry_extension_veto``
    re-checks this on the real entry price — this is the EARLY block.

    CONDITION (5) NOT BACKSIDE / NOT BELOW-VWAP: ``_detect_back_side`` (1m EMA/MACD rollover)
    AND ``front_side_state`` (which folds in BELOW-VWAP / faded / chasing-a-rolled-over-top).
    A live-tick NEW HIGH carve-out applies ONLY to ``chasing_top`` (a fresh tick to a new
    high IS front-side right now — mirrors ``hod_break_confirmation``); ``below_vwap`` /
    ``already_faded`` stay HARD vetoes. The L2 hidden-seller / big-seller veto
    (``_l2_entry_veto``) also gates it (fail-open on disabled / null L2).

    PROTECTIVE / FAIL-CLOSED: thin (<12 bars) / degenerate / no swing structure / a price that
    is NOT above the recent high / backside / extended ⇒ ``ok=False`` (keep waiting). Flag
    ``chili_momentum_momentum_continuation_entry_enabled`` OFF ⇒ ``(False, ..._disabled, {})``
    before any compute, so the entry path is byte-identical. NEVER raises (any error ⇒ a
    benign decline). docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "momentum_continuation"}
    try:
        if not bool(getattr(settings, "chili_momentum_momentum_continuation_entry_enabled", False)):
            return False, "momentum_continuation_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, "momentum_continuation_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr = arrays.get("atr") or []

        # current price = the live tick when ABOVE the completed-bar close (a fast break the
        # bar has not closed yet), else the bar close. Never LOWERS the price (a stale tick can
        # only make the gate stricter, never manufacture a break). Fail-closed if no price.
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "momentum_continuation_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass
        if cur_px <= 0:
            return False, "momentum_continuation_no_price", debug

        # ── (2) NEW HIGH: a fresh high above the recent COMPLETED-bar high ───────────────
        # The recent high = the max high over the prior ``swing_lookback`` COMPLETED bars
        # (cur excluded — that is the breaking bar). The structural stop = the recent swing
        # LOW over the SAME window (a defined, tested low; the vol-floor layer widens it
        # downstream — INVARIANT A — so we do NOT pre-floor it here).
        K = max(2, int(swing_lookback))
        w_start = max(0, cur - K)
        if w_start >= cur:
            return False, "momentum_continuation_insufficient_bars", debug
        recent_high = float(high.iloc[w_start:cur].max())
        recent_low = float(low.iloc[w_start:cur].min())
        if not (0.0 < recent_low < recent_high):
            return False, "momentum_continuation_bad_structure", debug
        debug["recent_high"] = round(recent_high, 6)
        debug["recent_low"] = round(recent_low, 6)
        level = recent_high     # the break level (breakout-or-bailout level)
        stop = recent_low       # the structural stop

        # instrument ATR% — the adaptive yardstick for the extension guard.
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            if _a is not None and cur_close > 0:
                atr_pct = _a / cur_close
        except (TypeError, ValueError, IndexError):
            atr_pct = None

        # ── (5) NOT BACKSIDE: _detect_back_side (1m EMA/MACD rollover) — fail-OPEN ───────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "momentum_continuation_back_side", debug
        # front_side_state folds in BELOW-VWAP / faded / chasing-a-rolled-over-top. A live-
        # tick NEW HIGH carve-out applies ONLY to chasing_top (a fresh tick to a new high IS
        # front-side now — mirrors hod_break_confirmation); below_vwap / already_faded stay
        # HARD vetoes (Ross never buys below VWAP).
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                _fsr = getattr(_fs, "reason", "backside")
                _sess_hod = float(high.iloc[:cur].max()) if cur > 0 else recent_high
                _live_new_high = False
                if _fsr == "chasing_top" and live_price is not None:
                    try:
                        _live_new_high = float(live_price) > float(_sess_hod)
                    except (TypeError, ValueError):
                        _live_new_high = False
                if not _live_new_high:
                    debug["front_side_state"] = _fsr
                    return False, "momentum_continuation_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            # thin/degenerate frame -> fail-CLOSED for THIS new high-conviction fire path:
            # if we cannot prove the name is front-side, do NOT take the continuation entry.
            debug["reason"] = "front_side_read_error"
            return False, "momentum_continuation_backside_lifecycle", debug

        # ── (4) NOT PARABOLIC — the #1 chase guard (extension vs 9-EMA AND VWAP) ─────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "momentum_continuation_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"momentum_continuation_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # ── TRIGGER: the break to a fresh NEW HIGH above the recent high ────────────────
        # TICK-BREAK (Ross-speed): the structure is valid on completed bars and the LIVE tick
        # is already trading through the recent high — enter on that tick, not a bar-close
        # later. Require the ATR/floor THRUST buffer (a real thrust, not a 1-tick poke) and
        # the premarket false-pop guard, mirroring hod_break_confirmation's tick path.
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=None,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "momentum_continuation_tick", debug

        # not broken on a completed bar yet -> ARM a tick-watch at the level (surfaced by the
        # caller as a TICK_ARMED_WAIT_REASON via the pullback_high).
        cur_hi = float(high.iloc[cur])
        if cur_hi <= level:
            return False, "waiting_for_break", debug
        # a completed bar broke to a NEW HIGH (cur_px above the recent high) — fire. The
        # strong-close / candle-quality + sustained-volume confirmations are applied by the
        # shared caller's confirmation stack; the breakout-or-bailout fast exit compensates.
        if cur_px <= level:
            return False, "waiting_for_break", debug
        return True, "momentum_continuation", debug
    except Exception:
        return False, "momentum_continuation_error", {"entry_interval": entry_interval}


def _bottoming_tail(o: float, h: float, l: float, c: float, *, min_lower_wick_frac: float = 0.50) -> bool:
    """Bottoming-tail / hammer: a long LOWER wick that dominates the bar's range — a
    fast flush that got bought back up (the V-bounce signature). The mirror of
    ``is_topping_tail``'s upper-wick read, reused as the per-bar flush-rejection shape.
    Color-independent (a red bar that recovered most of its low still rejects). Range-
    relative (no fixed cents); fail-safe False on a zero-range bar."""
    rng, _body, _upper, lower = _ohlc_local(o, h, l, c)
    if rng <= 0:
        return False
    return (lower / rng) >= float(min_lower_wick_frac)


def _ohlc_local(o: float, h: float, l: float, c: float) -> tuple[float, float, float, float]:
    """(range, body, upper_wick, lower_wick) for one bar — local mirror of candles._ohlc
    (kept here so this module has no import cycle at function-def time)."""
    rng = float(h) - float(l)
    body = abs(float(c) - float(o))
    upper = float(h) - max(float(o), float(c))
    lower = min(float(o), float(c)) - float(l)
    return rng, body, upper, lower


def flush_dip_buy_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """AS101 algo-flush V-bounce dip-buy (flag ``chili_momentum_flush_dip_buy_enabled``).

    On an ALREADY-STRONG, FRONT-SIDE up name, a FAST down-spike (a bottoming-tail flush
    bar on the fast interval) INTO VWAP / 20-MA support, followed by a CURL / RECLAIM back
    up on GREEN tape, is Ross's algo-flush dip-buy: the flush is an algo/stop-run, not a
    trend change, so the reclaim off support is a low-risk long with the dip low as the
    structural stop.

    Returns ``(ok, reason, debug)`` with ``debug`` carrying ``pullback_low`` (the dip low
    = the structural stop) and ``pullback_high`` (the flush bar / curl high = the breakout
    level) under the SAME keys the existing pullback-break trigger uses, so the downstream
    sizing / stop / bailout machinery is reused unchanged.

    GUARDS (each yardstick reused — no scattered magic; the flush depth is ATR-scaled):
      1. FRONT-SIDE strong up name — price above the rising 9-EMA and above VWAP before
         the flush (Ross only flush-buys what is already trending UP).
      2. FAST flush — a bottoming-tail flush bar whose DOWN-spike is at least an ATR-scaled
         depth (the "25-50c+" fast spike, expressed volatility-relatively, not fixed cents)
         that dipped INTO VWAP / 20-MA support (touched/undercut it on the low).
      3. CURL / RECLAIM — the CURRENT bar is a green bounce-curl candle reclaiming back
         ABOVE the support (close back above VWAP) on returning tape.

    ADDITIVE: flag OFF / thin (<10 bars) / degenerate / non-applicable -> ``(False, reason,
    {...})`` with NO side effects; fail-OPEN to a benign decline on any error (never raises,
    never blocks downstream). docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        if not bool(getattr(settings, "chili_momentum_flush_dip_buy_enabled", True)):
            return False, "flush_dip_disabled", {"entry_interval": entry_interval}
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return False, "flush_dip_insufficient_bars", {"entry_interval": entry_interval}
        # GAP 4 (Warrior re-audit): RTH-only dip-buy. Stops only fire 09:30-16:00 ET, so a
        # premarket flush-dip that breaks down cannot be exited at the structural stop. Uses
        # the now param (else the latest completed bar's timestamp from df.index). FAIL-OPEN
        # when the flag is OFF / crypto / no usable clock (so the unused now param now does
        # real work without ever manufacturing a block). docs/DESIGN/MOMENTUM_LANE.md
        try:
            _bar_ts = df.index[-1] if getattr(df, "index", None) is not None and len(df.index) else None
        except Exception:
            _bar_ts = None
        _rth_ok, _rth_reason = _dip_buy_in_rth_window(
            now=now, bar_ts=_bar_ts, symbol=symbol,
        )
        if not _rth_ok:
            return False, "flush_dip_rth_only_outside_window", {
                "entry_interval": entry_interval, "rth_reason": _rth_reason,
            }
        # MORNING-ONLY (2026-07-10, JZXN 12:30-ET midday backside dip -$889): the SAME
        # validated 06-10 A/B lesson deep_reclaim already enforces — a dip-buy pays in
        # the DISCOVERY phase and bleeds in midday/afternoon chop (SPHL -$404, GCDT
        # -$157, DBGI -$161, all 13:22 ET or later) — was only ever applied to ONE of
        # the two dip detectors. Mirror it here with the SAME documented knob
        # (chili_momentum_reclaim_max_hours_after_open; no new magic number).
        # EQUITY-only; fail-open without a usable clock (same semantics as deep_reclaim).
        _fd_clock = now if now is not None else _bar_ts
        if _fd_clock is not None and not (bool(symbol) and str(symbol).upper().endswith("-USD")):
            try:
                from zoneinfo import ZoneInfo

                _fts = pd.Timestamp(_fd_clock)
                _fts = _fts.tz_localize("UTC") if _fts.tzinfo is None else _fts
                _fet = _fts.tz_convert(ZoneInfo("America/New_York"))
                _fd_cutoff_min = (9 * 60 + 30) + int(
                    60 * float(getattr(settings, "chili_momentum_reclaim_max_hours_after_open", 1.0) or 1.0)
                )
                if (_fet.hour * 60 + _fet.minute) >= _fd_cutoff_min:
                    return False, "flush_dip_past_morning_window", {
                        "entry_interval": entry_interval, "et_time": _fet.strftime("%H:%M"),
                    }
            except Exception:
                pass  # no usable clock -> huwag mag-block sa guard mismo
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr = arrays.get("atr") or []

        # Instrument volatility (ATR/price) -> the flush DEPTH yardstick (the "25-50c+"
        # fast spike expressed volatility-relatively). None on thin data -> a small floor.
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None

        debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "flush_dip"}

        # The flush bar = the bar BEFORE the current (curl) bar; the current bar is the
        # reclaim. Need both present.
        flush_idx = cur - 1
        if flush_idx < 1:
            return False, "flush_dip_insufficient_bars", debug

        # ── GUARD 1: FRONT-SIDE strong up name (rising 9-EMA, price above VWAP pre-flush) ──
        e9_flush = ema9[flush_idx] if flush_idx < len(ema9) and ema9[flush_idx] is not None else None
        e9_prev = ema9[flush_idx - 1] if (flush_idx - 1) < len(ema9) and ema9[flush_idx - 1] is not None else None
        if e9_flush is None or e9_prev is None or float(e9_flush) < float(e9_prev):
            # VWAP-anchored front-side ALTERNATIVE (2026-07-09, VWAV 06-30 forensic):
            # after a parabolic leg the 9-EMA is falling DURING the retrace by
            # construction, so the rising-9EMA test rejects exactly the dip Ross buys
            # — the FIRST touch of RISING VWAP on an intact uptrend day (VWAV: dip to
            # 7.99 on rising VWAP -> +$10.8k; rejected 1810x as "not_front_side").
            # The session's own volume-weighted trend is the alternative front-side
            # proof: VWAP RISING at the flush bar. The other guards still gate the
            # shape (pre-flush close above VWAP, ATR-scaled bottoming-tail flush INTO
            # support, green curl reclaim) — this only widens WHICH trend yardstick
            # may certify "front side". Default-ON per the operator's overfit rule
            # (small-sample-positive -> ship + live-test; the replay window for this
            # shape is unadjudicable — its profitable segment predates the recorded
            # tape — so live IS the test, with per-sha rollback as the net).
            v_f = vwap[flush_idx] if flush_idx < len(vwap) and vwap[flush_idx] is not None else None
            v_p = vwap[flush_idx - 1] if (flush_idx - 1) < len(vwap) and vwap[flush_idx - 1] is not None else None
            if not (v_f is not None and v_p is not None and float(v_f) > float(v_p)):
                return False, "flush_dip_not_front_side", debug  # 9-EMA not rising, VWAP not rising
            debug["front_side_via"] = "rising_vwap"
        vwap_flush = vwap[flush_idx] if flush_idx < len(vwap) and vwap[flush_idx] is not None else None
        # Pre-flush strength: the bar BEFORE the flush closed above VWAP (was front-side).
        pre = flush_idx - 1
        if vwap_flush is not None and float(vwap_flush) > 0 and float(close.iloc[pre]) < float(vwap_flush):
            return False, "flush_dip_below_vwap_pre", debug

        # ── GUARD 2: FAST flush — bottoming-tail bar, ATR-scaled down-spike INTO support ──
        f_o = float(opn.iloc[flush_idx]) if opn is not None else float(close.iloc[flush_idx - 1])
        f_h, f_l, f_c = float(high.iloc[flush_idx]), float(low.iloc[flush_idx]), float(close.iloc[flush_idx])
        if not _bottoming_tail(f_o, f_h, f_l, f_c):
            return False, "flush_dip_no_bottoming_tail", debug
        # Down-spike depth: from the pre-flush close down to the flush LOW, ATR-scaled
        # (the "25-50c+" fast spike, volatility-relative). Floor so a calm name still needs
        # a real flush. a==0 -> the floor alone (thin-data fail-open uses the floor).
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
        ref = float(close.iloc[pre])
        spike_pct = (ref - f_l) / ref if ref > 0 else 0.0
        flush_floor = max(0.005, a * 0.5)  # >= 0.5 ATR or 0.5% — the documented base
        if spike_pct < flush_floor:
            return False, "flush_dip_too_shallow", debug
        # INTO support: the flush low touched/undercut VWAP (or, if VWAP is warming up,
        # the rising 9-EMA proxies the 20-MA support). Fail-open on missing support read.
        support = None
        if vwap_flush is not None and float(vwap_flush) > 0:
            support = float(vwap_flush)
        elif e9_flush is not None and float(e9_flush) > 0:
            support = float(e9_flush)
        if support is not None and f_l > support * (1.0 + max(0.0, a * 0.5)):
            return False, "flush_dip_no_support_touch", debug  # never reached support

        dip_low = f_l
        if not (dip_low > 0):
            return False, "flush_dip_bad_low", debug

        # ── GUARD 3: CURL / RECLAIM — current bar green curl back ABOVE support ──────────
        c_o = float(opn.iloc[cur]) if opn is not None else float(close.iloc[cur - 1])
        c_h, c_l, c_c = float(high.iloc[cur]), float(low.iloc[cur]), float(close.iloc[cur])
        from .candles import is_bounce_curl_candle

        # Live tick (when present) is the reclaim price; else the curl bar close.
        px = float(live_price) if (live_price is not None and float(live_price) > 0) else c_c
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        reclaimed = True
        if vwap_cur is not None and float(vwap_cur) > 0:
            reclaimed = px >= float(vwap_cur)
        if not reclaimed:
            return False, "flush_dip_not_reclaimed", debug
        # The dip must HOLD on the curl bar (its low at/above the flush low minus noise).
        if c_l < dip_low * (1.0 - max(0.0, a * 0.5)):
            return False, "flush_dip_undercut", debug
        # Per-bar conviction: a green bounce-curl candle (close in the upper part of range).
        if not is_bounce_curl_candle(c_o, c_h, c_l, c_c):
            return False, "flush_dip_weak_curl", debug

        # Level = the curl bar's own high (the reclaim's break level); stop = the dip low.
        level = max(c_h, f_h)
        if not (0.0 < dip_low < level):
            return False, "flush_dip_bad_level", debug
        debug.update({
            "pullback_high": float(level),
            "pullback_low": float(dip_low),
            "flush_spike_pct": round(spike_pct * 100.0, 2),
            "flush_support": (round(support, 6) if support is not None else None),
        })
        return True, "flush_dip_buy", debug
    except Exception:
        return False, "flush_dip_error", {"entry_interval": entry_interval}


def vwap_reclaim_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """SCAL101 VWAP-reclaim entry (flag ``chili_momentum_vwap_reclaim_enabled``).

    Price closed BELOW VWAP for at least ``K`` recent bars (the name lost VWAP), then the
    CURRENT bar RECLAIMS back ABOVE VWAP on a VOLUME SPIKE — the SCAL101 long: the reclaim
    of VWAP with conviction is the resumption signal, with the reclaim bar's LOW as the
    structural stop (lose VWAP again and the reclaim failed).

    Returns ``(ok, reason, debug)`` with ``debug`` carrying ``pullback_low`` (the reclaim
    bar low = the structural stop) and ``pullback_high`` (the reclaim bar high = the
    breakout level) under the SAME keys the existing pullback-break trigger uses, so the
    downstream sizing / stop / bailout machinery is reused unchanged.

    ADAPTIVE: ``K`` = ``chili_momentum_vwap_reclaim_min_below_bars`` (one documented base,
    default 2); the volume-spike floor reuses the lane's own ``volume_spike_multiple``
    yardstick via ``chili_momentum_vwap_reclaim_vol_mult`` (default 1.5). VWAP is the
    rolling proxy already used across the lane (indicator_core.compute_vwap).

    ADDITIVE: flag OFF / thin (<10 bars) / VWAP warming up / non-applicable -> ``(False,
    reason, {...})`` with NO side effects; fail-OPEN to a benign decline on any error
    (never raises, never blocks downstream). docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        if not bool(getattr(settings, "chili_momentum_vwap_reclaim_enabled", True)):
            return False, "vwap_reclaim_disabled", {"entry_interval": entry_interval}
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return False, "vwap_reclaim_insufficient_bars", {"entry_interval": entry_interval}
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"vwap", "volume_ratio"})
        vwap = arrays.get("vwap") or []
        vr = arrays.get("volume_ratio") or []

        debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "vwap_reclaim"}

        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        if vwap_cur is None or float(vwap_cur) <= 0:
            return False, "vwap_reclaim_vwap_warmup", debug  # VWAP not ready -> fail-open

        # ── K bars below VWAP, then the CURRENT bar reclaims above it ───────────────────
        K = max(1, int(getattr(settings, "chili_momentum_vwap_reclaim_min_below_bars", 2) or 2))
        # The CURRENT bar must reclaim: price (live tick when present, else close) >= VWAP
        # AND the current close is back above VWAP (a real reclaim, not just a wick poke).
        px = float(live_price) if (live_price is not None and float(live_price) > 0) else float(close.iloc[cur])
        if px < float(vwap_cur) or float(close.iloc[cur]) < float(vwap_cur):
            return False, "waiting_for_vwap_reclaim", debug
        # The PRIOR K bars must each have CLOSED below their own VWAP (sustained loss of
        # VWAP, not a one-bar dip). Fail-open if any of the K VWAP samples is warming up.
        start = cur - K
        if start < 0:
            return False, "vwap_reclaim_insufficient_bars", debug
        below_count = 0
        for i in range(start, cur):
            vi = vwap[i] if i < len(vwap) and vwap[i] is not None else None
            if vi is None or float(vi) <= 0:
                return False, "vwap_reclaim_vwap_warmup", debug
            if float(close.iloc[i]) < float(vi):
                below_count += 1
        if below_count < K:
            return False, "vwap_reclaim_not_below_enough", debug

        # ── VOLUME SPIKE on the reclaim bar (conviction, not a drift back over VWAP) ─────
        vol_mult = float(getattr(settings, "chili_momentum_vwap_reclaim_vol_mult", 1.5) or 1.5)
        vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        if vol_ratio is None:
            w = vol.tail(21)
            avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / avg) if avg > 0 else 0.0
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < vol_mult:
            return False, "vwap_reclaim_low_volume", debug

        # Level = the reclaim bar high (break level); stop = the reclaim bar low.
        level = float(high.iloc[cur])
        stop = float(low.iloc[cur])
        if not (0.0 < stop < level):
            return False, "vwap_reclaim_bad_level", debug
        debug.update({
            "pullback_high": float(level),
            "pullback_low": float(stop),
            "vwap": round(float(vwap_cur), 6),
            "bars_below": below_count,
        })
        return True, "vwap_reclaim", debug
    except Exception:
        return False, "vwap_reclaim_error", {"entry_interval": entry_interval}


def _is_hot_tape(atr_pct: float | None, rvol: float | None) -> bool:
    """Hot/parabolic-tape predicate for the wick-reclaim trigger (BATCH B FIX 2).

    A name is HOT when its intraday volatility (ATR%) OR its relative volume is at/above
    the lane's explosive floors — the SAME floors ``is_explosive_mover`` uses
    (``chili_momentum_explosive_atr_pct_floor`` / ``chili_momentum_explosive_rvol_floor``),
    so the hot-tape read shares one source of truth for "explosive". Unlike
    ``is_explosive_mover`` this is NOT gated by the explosive-RECALIBRATION master switch —
    the wick-reclaim has its OWN kill-switch (``chili_momentum_wick_reclaim_entry_enabled``)
    and the hot-tape gate must hold regardless of whether the recalibration carve-outs are
    on. Fail-CLOSED: both-None or any error -> False (the trigger then never fires -> the
    cold-tape case is rejected, exactly as required). Pure; no I/O or mutation."""
    try:
        a = None if atr_pct is None else float(atr_pct)
        rv = None if rvol is None else float(rvol)
        atr_floor = float(getattr(settings, "chili_momentum_explosive_atr_pct_floor", 0.045) or 0.0)
        rvol_floor = float(getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 0.0)
        if a is not None and atr_floor > 0.0 and a >= atr_floor:
            return True
        if rv is not None and rvol_floor > 0.0 and rv >= rvol_floor:
            return True
        return False
    except Exception:
        return False


def wick_reclaim_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """HOT-TAPE WICK-RECLAIM entry (HVM101 #008; flag ``chili_momentum_wick_reclaim_entry_enabled``).

    The extreme-volatility variant of the VWAP-reclaim, GATED to HOT/parabolic tape ONLY.
    The shape (Ross HVM101):

      1. a HUGE REJECTION CANDLE — a recent bar with a large UPPER WICK (>= a documented
         fraction of its range) on an OUTSIZED range (relative to the name's own ATR%): a
         spike that got rejected hard;
      2. an IMMEDIATE FLUSH on LOW (drying-up) volume — the bar(s) after the rejection trade
         DOWN off the wick on RECEDING volume (a vacuum, not real distribution);
      3. a RETRACE of ~``min_retrace_frac`` of the wick on rate-of-change — price re-enters
         the wick from the flush low back UP toward the wick high — that re-entry IS the long.

    Re-enter into the wick; the STOP is the wick LOW (the flush low) — lose it and the
    reclaim failed. Returns ``(ok, reason, debug)`` with ``pullback_high`` (the wick high =
    the breakout/continuation level) and ``pullback_low`` (the flush/wick low = the
    structural stop) under the SAME keys the pullback-break trigger uses, so the downstream
    sizing / stop / breakout-or-bailout machinery is reused unchanged (NO new sizing path).

    MANDATORY HOT-TAPE GATE: ``_is_hot_tape`` (RVOL/ATR via the shared explosive floors). On
    SLOW/COLD tape this returns ``(False, "wick_reclaim_cold_tape", ...)`` and NEVER fires —
    so it can never fire on the slow recoveries / extended 5-min patterns it is invalid for.

    ADAPTIVE knobs (one documented base each): ``chili_momentum_wick_reclaim_min_wick_frac``
    (the upper-wick size floor) and ``chili_momentum_wick_reclaim_min_retrace_frac`` (the
    retrace-into-the-wick depth). The outsized-range test is measured against the name's OWN
    ATR% (no fixed-price magnitude).

    ADDITIVE: flag OFF / thin (<10 bars) / cold tape / non-applicable -> ``(False, reason,
    {...})`` with NO side effects; fail-OPEN to a benign decline on any error (never raises,
    never blocks downstream). docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        if not bool(getattr(settings, "chili_momentum_wick_reclaim_entry_enabled", True)):
            return False, "wick_reclaim_disabled", {"entry_interval": entry_interval}
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return False, "wick_reclaim_insufficient_bars", {"entry_interval": entry_interval}
        opn = df["Open"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"atr", "volume_ratio"})
        atr = arrays.get("atr") or []
        vr = arrays.get("volume_ratio") or []

        debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "wick_reclaim"}

        # ── MANDATORY HOT-TAPE GATE (the trigger is INVALID on slow/cold tape) ──────────
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            atr_pct = (_a / _p) if (_a is not None and _p > 0) else None
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        rvol = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        if not _is_hot_tape(atr_pct, rvol):
            debug["atr_pct"] = atr_pct
            debug["rvol"] = rvol
            return False, "wick_reclaim_cold_tape", debug

        # ── (1) the REJECTION CANDLE: a recent big-upper-wick / outsized-range bar ───────
        # Scan a small recent window (the rejection should be CLOSE behind the current bar;
        # reuse the VWAP-reclaim K so the look-back is one consistent lane yardstick). The
        # rejection bar is the most recent bar whose upper wick dominates its range AND whose
        # range is outsized vs the name's own ATR. Excludes the current (forming) bar.
        min_wick_frac = float(
            getattr(settings, "chili_momentum_wick_reclaim_min_wick_frac", 0.5) or 0.5
        )
        min_retrace_frac = float(
            getattr(settings, "chili_momentum_wick_reclaim_min_retrace_frac", 0.4) or 0.4
        )
        # The rejection should be CLOSE behind the reclaim bar: the rejection bar itself plus
        # the immediate (low-volume) flush bars between it and the reclaim. Reuse the
        # VWAP-reclaim K as the flush-span base, +1 for the rejection bar — one consistent
        # lane yardstick, no new magic. (K=2 -> rejection up to 3 bars behind the reclaim.)
        K = max(1, int(getattr(settings, "chili_momentum_vwap_reclaim_min_below_bars", 2) or 2))
        rej_scan = K + 1
        # an outsized bar is one whose range exceeds the name's ATR (the ATR is itself the
        # average true range — a "huge" rejection bar prints meaningfully above it). When ATR
        # is unavailable, fall back to the bar's own range>0 (the wick-frac floor still gates).
        _atr_abs = None
        try:
            _atr_abs = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
        except (TypeError, ValueError, IndexError):
            _atr_abs = None
        rej_idx = None
        lo_scan = max(1, cur - rej_scan)  # the rejection + immediate flush bars behind cur
        for i in range(cur - 1, lo_scan - 1, -1):
            try:
                o_i, h_i, l_i, c_i = (
                    float(opn.iloc[i]), float(high.iloc[i]),
                    float(low.iloc[i]), float(close.iloc[i]),
                )
            except (TypeError, ValueError, IndexError):
                continue
            rng_i, _body, upper_i, _lower = _ohlc_local(o_i, h_i, l_i, c_i)
            if rng_i <= 0:
                continue
            big_wick = (upper_i / rng_i) >= min_wick_frac
            outsized = (rng_i >= _atr_abs) if (_atr_abs is not None and _atr_abs > 0) else True
            if big_wick and outsized:
                rej_idx = i
                break
        if rej_idx is None:
            return False, "wick_reclaim_no_rejection", debug

        wick_high = float(high.iloc[rej_idx])

        # ── (2) the FLUSH on RECEDING volume + (3) the RETRACE back into the wick ───────
        # The flush low = the lowest low from the rejection bar through the current bar (the
        # vacuum off the wick). The flush must be on DRYING volume: the post-rejection bars'
        # rel-volume should recede vs the rejection bar (a low-volume vacuum, not real selling).
        flush_low = float(low.iloc[rej_idx])
        flush_recedes = True
        try:
            _rej_vr = float(vr[rej_idx]) if rej_idx < len(vr) and vr[rej_idx] is not None else None
            _post_vrs = [
                float(vr[j]) for j in range(rej_idx + 1, cur)
                if j < len(vr) and vr[j] is not None
            ]
            for j in range(rej_idx + 1, cur + 1):
                if j < len(low) and low.iloc[j] is not None:
                    flush_low = min(flush_low, float(low.iloc[j]))
            if _rej_vr is not None and _post_vrs:
                # the flush bars (between rejection and the reclaim bar) must, on average,
                # carry LESS rel-volume than the rejection bar (drying up / a vacuum).
                flush_recedes = (sum(_post_vrs) / len(_post_vrs)) < _rej_vr
        except (TypeError, ValueError, IndexError):
            flush_recedes = True  # thin data -> fail-open on the volume-dry-up leg only

        # ── SLOW-RECOVERY BAR-COUNT GATE (HVM101 #008; quality filter, REJECTS only) ────
        # Ross: a wick rejection must RECOVER within 1-3 bars; the 4th bar only counts when
        # the tape is "really showing a lot of price action" (here: a high-rate-of-change,
        # drying-up flush == flush_recedes True); 5-6+ bars = a slow trickle = invalid =
        # confirms the rejection, NOT a reclaim. The rejection-bar OFFSET is already
        # computed (cur - rej_idx) and was only logged before — this gates on it. ADAPTIVE
        # RELAXATION: bars <= (max-1) fire; the boundary bar (== max) fires ONLY on the
        # strong-action proof (flush_recedes); > max is rejected outright. Flag OFF ->
        # skipped entirely (byte-identical). It can only REJECT a slow trickle; it never
        # loosens the existing wick-reclaim guards (which still run below).
        if bool(
            getattr(settings, "chili_momentum_wick_reclaim_slow_recovery_gate_enabled", False)
        ):
            max_recovery_bars = max(
                1,
                int(getattr(settings, "chili_momentum_wick_reclaim_max_recovery_bars", 4) or 4),
            )
            recovery_bars = int(cur - rej_idx)
            # the boundary (Nth) bar needs the strong price-action proof; bars beyond it are
            # rejected unconditionally (no relaxation can save a 5-6+ bar slow trickle).
            strong_action = bool(flush_recedes)
            too_slow = (recovery_bars > max_recovery_bars) or (
                recovery_bars == max_recovery_bars and not strong_action
            )
            if too_slow:
                debug["rejection_bar_offset"] = recovery_bars
                debug["max_recovery_bars"] = max_recovery_bars
                debug["strong_action"] = strong_action
                return False, "wick_reclaim_slow_recovery", debug

        if not flush_recedes:
            debug["flush_recedes"] = False
            return False, "wick_reclaim_flush_not_dry", debug

        wick_span = wick_high - flush_low
        if not (wick_span > 0):
            return False, "wick_reclaim_bad_wick", debug

        # The RECLAIM: price (live tick when present, else the current close) re-enters the
        # wick from the flush low by at least min_retrace_frac of the wick span (the HVM101
        # ~40% retrace-on-rate-of-change). Re-enter INTO the wick — not a full break above it.
        px = (
            float(live_price)
            if (live_price is not None and float(live_price) > 0)
            else float(close.iloc[cur])
        )
        retrace_frac = (px - flush_low) / wick_span
        debug["retrace_frac"] = round(retrace_frac, 4)
        debug["atr_pct"] = atr_pct
        debug["rvol"] = rvol
        if retrace_frac < min_retrace_frac:
            return False, "wick_reclaim_retrace_too_shallow", debug

        # Level = the wick high (the breakout/continuation target the bailout machinery uses);
        # stop = the flush/wick low. Reuse the IDENTICAL pullback_high/pullback_low keys.
        level = wick_high
        stop = flush_low
        if not (0.0 < stop < level):
            return False, "wick_reclaim_bad_level", debug
        debug.update({
            "pullback_high": float(level),
            "pullback_low": float(stop),
            "wick_high": round(wick_high, 6),
            "flush_low": round(flush_low, 6),
            "rejection_bar_offset": int(cur - rej_idx),
        })
        # ── UNIFIED BUYERS-CONFIRM (2026-07-04): the wick-reclaim fired on the retrace-into-wick
        # GEOMETRY alone (hot-tape RVOL/ATR + rejection+flush) with NO check that buyers are
        # actually lifting — so a momentary touch into the opening flush could fire a whipsaw
        # entry. Require the SAME crypto-safe buyers gate as micro_pullback_primary before firing;
        # a valid shape with no buyers yet just re-evaluates next tick.
        _b_ok, _b_dbg = buyers_confirmed(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["buyers_confirm"] = _b_dbg.get("reason") if isinstance(_b_dbg, dict) else None
        if not _b_ok:
            return False, "wick_reclaim_buyers_unconfirmed", debug
        return True, "wick_reclaim", debug
    except Exception:
        return False, "wick_reclaim_error", {"entry_interval": entry_interval}


# ── BATCH C: swing-pivot scanner + ABCD + double-bottom (SS101 #013) ──────────────
# The ABCD pattern (a dedicated Ross chapter) and the double-bottom both require a
# SWING-HIGH/LOW (pivot) scanner the lane did not have. Lower hit-rate than the
# breakout/pullback families (deferred in the audit) — built here to COMPLETE the
# playbook, each independently kill-switched. The defense against reading CHOP as
# structure is the ATR pivot filter (ignore a pivot whose vertical move off its
# neighbors is smaller than a fraction of ATR) PLUS the per-pattern hold / no-new-low
# conditions. docs/DESIGN/MOMENTUM_LANE.md
#
# ONE documented adaptive knob each: the pivot HALF-WINDOW (how many bars on each side
# define a local extreme) and the ATR pivot-noise FRACTION (the minimum prominence a
# pivot must clear, expressed in ATRs). Everything else is reused (ATR from
# indicator_core, _collapse_cap, _bottoming_tail, the shared (ok,reason,debug) +
# pullback_high/pullback_low keys, _l2_entry_veto, class_aware_reward_risk).


def _swing_pivots(
    high: Any,
    low: Any,
    *,
    half_window: int,
    atr_abs: float | None,
    atr_noise_frac: float,
) -> list[dict[str, Any]]:
    """Pure swing-pivot scanner over the COMPLETED bars.

    A bar ``i`` is a swing HIGH iff its high is the strict-or-equal local maximum over
    ``[i-half_window, i+half_window]`` (a confirmed pivot needs ``half_window`` bars on
    EACH side, so the last ``half_window`` bars can never be pivots — they are not yet
    confirmed, exactly like the BOS swing-low confirmation already used in this module).
    A swing LOW is the mirror. ATR-NOISE FILTER: a pivot is kept only when its PROMINENCE
    — the vertical distance from the pivot to the higher of its two adjacent opposite
    extremes within the window — is at least ``atr_noise_frac · atr_abs``; this is how
    CHOP (tiny wiggles) is NOT mistaken for structure. When ``atr_abs`` is missing/<=0
    the filter is skipped (fail-open: the window-extreme test alone still applies).

    Returns the pivots in chronological order, each a dict
    ``{"idx": int, "price": float, "kind": "H"|"L"}``. Pure; never raises (any error ->
    empty list, so the caller's pattern simply does not fire)."""
    out: list[dict[str, Any]] = []
    try:
        hi = high.values if hasattr(high, "values") else list(high)
        lo = low.values if hasattr(low, "values") else list(low)
        n = len(hi)
        w = max(1, int(half_window))
        if n < (2 * w + 1):
            return out
        a = float(atr_abs) if (atr_abs is not None and float(atr_abs) > 0) else 0.0
        min_prom = a * max(0.0, float(atr_noise_frac))
        for i in range(w, n - w):
            try:
                h_i = float(hi[i])
                l_i = float(lo[i])
            except (TypeError, ValueError):
                continue
            if h_i != h_i or l_i != l_i:  # NaN-safe
                continue
            lo_w = i - w
            hi_w = i + w + 1
            win_hi = [float(hi[j]) for j in range(lo_w, hi_w)]
            win_lo = [float(lo[j]) for j in range(lo_w, hi_w)]
            if any(x != x for x in win_hi) or any(x != x for x in win_lo):
                continue
            is_high = h_i >= max(win_hi)
            is_low = l_i <= min(win_lo)
            # A bar that is BOTH (a flat degenerate window) is ambiguous — skip it.
            if is_high == is_low:
                continue
            if is_high:
                # prominence = drop from this swing high to the LOWEST low in the window
                # (the deepest trough flanking it). Below the ATR floor -> chop, drop it.
                prom = h_i - min(win_lo)
                if min_prom > 0.0 and prom < min_prom:
                    continue
                out.append({"idx": i, "price": h_i, "kind": "H"})
            else:
                prom = max(win_hi) - l_i
                if min_prom > 0.0 and prom < min_prom:
                    continue
                out.append({"idx": i, "price": l_i, "kind": "L"})
        # Collapse consecutive same-kind pivots to the more-extreme one so the sequence
        # alternates H/L (a clean structural skeleton, not duplicate flat shelves).
        cleaned: list[dict[str, Any]] = []
        for p in out:
            if cleaned and cleaned[-1]["kind"] == p["kind"]:
                prev = cleaned[-1]
                if (p["kind"] == "H" and p["price"] >= prev["price"]) or (
                    p["kind"] == "L" and p["price"] <= prev["price"]
                ):
                    cleaned[-1] = p  # keep the more-extreme same-kind pivot
                continue
            cleaned.append(p)
        return cleaned
    except Exception:
        return []


def _batch_c_atr_pct(df: pd.DataFrame, close: pd.Series, cur: int) -> tuple[float | None, float | None]:
    """``(atr_pct, atr_abs)`` at the current bar (the shared volatility yardstick the
    Batch-C scanner + depth caps use). None on thin data / any error (fail-open)."""
    try:
        arrays = compute_all_from_df(df, needed={"atr"})
        atr = arrays.get("atr") or []
        _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
        _p = float(close.iloc[cur])
        if _a is not None and _a == _a and _p > 0:
            return (_a / _p), _a
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return None, None


def ross_abcd_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    df_context: pd.DataFrame | None = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross ABCD entry (SS101 #013; flag ``chili_momentum_abcd_entry_enabled``).

    From the ATR-filtered swing pivots, an ABCD long is::

        A = an impulse-UP leg (a swing LOW -> a swing HIGH at/near the highs);
        B = the pullback LOW after A (the first higher-low retrace off the A high);
        C = a SECOND pullback LOW that HOLDS above the prior structure — it does NOT
            break to a new low below the B/C support (a higher-low, the coil tightening);
        D = price BREAKS above the B swing-HIGH (the high between B and C).

    Fire on the D break (with a volume confirm). Entry = the B-high break level
    (``pullback_high``); stop = the C-low structural low (``pullback_low``). The
    downstream vol-floor layer widens the stop (INVARIANT A) — this gate does NOT
    pre-floor it. The shared keys mean the runner's stop / sizing / bailout machinery is
    reused unchanged (NO new sizing path).

    MULTI-TIMEFRAME (optional, a CONFIDENCE BREADCRUMB only — never a hard gate): when a
    higher-timeframe ``df_context`` frame is supplied, a 5-min ABCD aligned with the
    1-min flag is surfaced in ``debug["abcd_mtf_aligned"]`` but is not required to fire.

    NOISE DEFENSE: the ATR pivot filter (prominence >= a fraction of ATR) + the
    no-new-low HOLD condition for C + a depth cap (``_collapse_cap``) on the B/C
    retraces — a too-deep "C" is a breakdown, not a coil. ADDITIVE: flag OFF / thin
    (<2·window+3 bars) / no clean ABCD -> ``(False, reason, {...})`` with NO side
    effects; fail-OPEN to a benign decline on any error. docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "abcd"}
    try:
        if not bool(getattr(settings, "chili_momentum_abcd_entry_enabled", True)):
            return False, "abcd_disabled", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 3):
            return False, "abcd_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)
        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        debug["n_pivots"] = len(pivots)
        # Need at least H(A) L(B) H(B->C) L(C) to describe an ABCD coil (the A-high is the
        # readable start of the impulse; an origin low before it is not required as a pivot).
        if len(pivots) < 4:
            return False, "abcd_too_few_pivots", debug

        # Walk from the most-recent pivots backward to find the freshest A-H, B-L, BC-H, C-L.
        # The skeleton alternates H/L; we want the last three lows (origin/B/C) framing two
        # highs (A and the B->C swing high). Identify the last LOW pivot = C, the high before
        # it = the B->C swing high, the low before that = B, the high before that = A.
        c_low = None
        bc_high = None
        b_low = None
        a_high = None
        # scan from the end for: L (C), then H (bc), then L (B), then H (A)
        seq = pivots[:]  # chronological
        # Find the last index that is a LOW (C).
        ci = None
        for k in range(len(seq) - 1, -1, -1):
            if seq[k]["kind"] == "L":
                ci = k
                break
        if ci is None or ci < 3:
            return False, "abcd_no_c_low", debug
        c_low = seq[ci]
        bc_high = seq[ci - 1] if seq[ci - 1]["kind"] == "H" else None
        b_low = seq[ci - 2] if seq[ci - 2]["kind"] == "L" else None
        a_high = seq[ci - 3] if seq[ci - 3]["kind"] == "H" else None
        if not (c_low and bc_high and b_low and a_high):
            return False, "abcd_skeleton_incomplete", debug

        a_h = float(a_high["price"])
        b_l = float(b_low["price"])
        bc_h = float(bc_high["price"])
        c_l = float(c_low["price"])
        # A is an impulse UP to the highs; B and C are HIGHER LOWS (the coil holds).
        # C must NOT break below B (no new low) — the structural HOLD that distinguishes
        # an ABCD coil from a breakdown.
        if not (b_l > 0 and c_l > 0 and bc_h > a_h * (1.0 - 0.0)):
            # bc_h should be a genuine swing high after B (continuation), at/above A's region
            pass
        if c_l < b_l:
            debug.update({"b_low": round(b_l, 6), "c_low": round(c_l, 6)})
            return False, "abcd_c_broke_b_low", debug  # C made a new low -> not a hold
        # Depth guard: each retrace (A->B, BC->C) must be a PULLBACK, not a collapse.
        cap = _collapse_cap(atr_pct)
        ab_depth = (a_h - b_l) / a_h if a_h > 0 else 1.0
        cd_depth = (bc_h - c_l) / bc_h if bc_h > 0 else 1.0
        debug.update({
            "a_high": round(a_h, 6), "b_low": round(b_l, 6),
            "bc_high": round(bc_h, 6), "c_low": round(c_l, 6),
            "ab_depth_pct": round(ab_depth * 100.0, 2),
            "cd_depth_pct": round(cd_depth * 100.0, 2),
            "abcd_collapse_cap_pct": round(cap * 100.0, 2),
        })
        if ab_depth > cap or cd_depth > cap:
            return False, "abcd_retrace_too_deep", debug

        # The break level = the B->C swing high (D = a break above it); stop = the C low.
        level = bc_h
        stop = c_l
        if not (0.0 < stop < level):
            return False, "abcd_bad_level", debug
        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # MULTI-TIMEFRAME confidence breadcrumb (NOT a gate): a higher-TF ABCD coil aligned
        # with this 1-min flag. Best-effort; any error simply omits the breadcrumb.
        if df_context is not None and not getattr(df_context, "empty", True):
            try:
                _ch = df_context["High"].astype(float)
                _cl = df_context["Low"].astype(float)
                _cc = df_context["Close"].astype(float)
                _cur2 = len(df_context) - 1
                _ap2, _aa2 = _batch_c_atr_pct(df_context, _cc, _cur2)
                _piv2 = _swing_pivots(
                    _ch, _cl, half_window=half_w, atr_abs=_aa2, atr_noise_frac=noise_frac,
                )
                # aligned if the higher-TF skeleton also ends on a higher-low coil (L,H,L,...)
                _aligned = (
                    len(_piv2) >= 3 and _piv2[-1]["kind"] == "L"
                    and _piv2[-3]["kind"] == "L"
                    and float(_piv2[-1]["price"]) >= float(_piv2[-3]["price"])
                )
                debug["abcd_mtf_aligned"] = bool(_aligned)
            except (TypeError, ValueError, KeyError, IndexError):
                pass

        # L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2).
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"abcd_{_reason}", debug

        # ── TRIGGER: D = the break above the B->C swing high ─────────────────────────────
        cur_hi = float(high.iloc[cur])
        # TICK-BREAK: the structure is valid on completed bars and the live tick is already
        # trading through the level -> enter on that tick (mirrors the other Batch triggers;
        # the caller's tick-break block applies the thrust buffer via the WAIT reason).
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "abcd_break_tick_ok", debug
        if cur_hi <= level:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)
        # A completed bar broke D -> require a VOLUME spike on the break bar (real demand).
        arrays_v = compute_all_from_df(df, needed={"volume_ratio"})
        vr = arrays_v.get("volume_ratio") or []
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < _vol_mult:
            return False, "abcd_low_volume", debug
        return True, "abcd_break", debug
    except Exception:
        return False, "abcd_error", {"entry_interval": entry_interval}


def wedge_break_entry(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """GAP 1 (Warrior re-audit): CONVERGING-WEDGE break (flag
    ``chili_momentum_wedge_break_entry_enabled``).

    From the ATR-filtered swing pivots, a bullish wedge is a coil where the upper line
    (swing HIGHS) and the lower line (swing LOWS) CONVERGE to an apex:

      * FALLING / DESCENDING wedge (the STRONGER bull setup): the upper highs trend DOWN
        while the lower lows trend UP (or are flat) — supply is exhausting into rising
        demand; the break of the upper (descending) line resolves UP. 3+ taps total.
      * RISING / ASCENDING wedge (LOWER odds — Ross/technical lore: a rising wedge is a
        bearish/exhaustion shape): both lines rise. This trigger SKIPS it (never fires).

    Fire on the body/wick breaking OUT of the wedge at the apex with tape. Entry = the
    upper-line level at the current bar (``pullback_high``); STOP = back INTO the wedge =
    the most-recent lower-line pivot low (the apex low; ``pullback_low``). The downstream
    vol-floor layer widens the stop (INVARIANT A). Shared (ok, reason, debug) + the
    IDENTICAL pullback_high / pullback_low keys, so the runner's stop / sizing / bailout
    machinery is reused unchanged.

    CHASE-GUARDS (each reused — no new magic, no weakened veto):
      * TAPE REQUIRED + FAIL-CLOSED: ``tape_confirms_hold`` (buyers lifting the ask THIS
        tick) must confirm; any disabled-flag / no-tape / thin / stale ⇒ NO fire.
      * NOT PARABOLIC: ``_hod_extension_ok`` (the SAME adaptive ATR extension cap vs the
        9-EMA AND VWAP) rejects a break level sitting excessively extended.
      * NOT BACKSIDE / NOT BELOW-VWAP: ``_detect_back_side`` (1m EMA/MACD rollover) AND
        ``front_side_state`` (folds in below-VWAP / faded).
      * L2 hidden-seller / big-seller veto (``_l2_entry_veto``; fail-open on disabled/null).

    ADDITIVE: flag OFF / thin / no clean converging wedge / rising-wedge ⇒ ``(False,
    reason, {...})`` with NO side effects; fail-OPEN to a benign decline on any error.
    docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "wedge_break"}
    try:
        if not bool(getattr(settings, "chili_momentum_wedge_break_entry_enabled", False)):
            return False, "wedge_break_disabled", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 5):
            return False, "wedge_break_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)
        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        debug["n_pivots"] = len(pivots)

        # Need >= 2 swing HIGHS (upper line) and >= 2 swing LOWS (lower line) = 3+ taps that
        # define BOTH converging trendlines. Take the last two of each.
        highs = [p for p in pivots if p["kind"] == "H"]
        lows = [p for p in pivots if p["kind"] == "L"]
        if len(highs) < 2 or len(lows) < 2:
            return False, "wedge_break_too_few_taps", debug
        h2, h1 = highs[-2], highs[-1]   # older, newer upper taps
        l2, l1 = lows[-2], lows[-1]     # older, newer lower taps

        h2_p, h1_p = float(h2["price"]), float(h1["price"])
        l2_p, l1_p = float(l2["price"]), float(l1["price"])
        if not (h2_p > 0 and h1_p > 0 and l2_p > 0 and l1_p > 0):
            return False, "wedge_break_bad_pivots", debug

        # CONVERGENCE: the lines must be narrowing (the newer gap < the older gap) -> a coil.
        gap_old = h2_p - l2_p
        gap_new = h1_p - l1_p
        if not (gap_old > 0 and 0 < gap_new < gap_old):
            return False, "wedge_break_not_converging", debug

        upper_slope = h1_p - h2_p   # < 0 -> descending upper line (the strong bull wedge)
        lower_slope = l1_p - l2_p   # > 0 -> ascending lower line
        debug.update({
            "wedge_upper_old": round(h2_p, 6), "wedge_upper_new": round(h1_p, 6),
            "wedge_lower_old": round(l2_p, 6), "wedge_lower_new": round(l1_p, 6),
            "wedge_gap_old": round(gap_old, 6), "wedge_gap_new": round(gap_new, 6),
        })
        # RISING / ASCENDING wedge (both lines rising) = LOWER-odds / bearish exhaustion ->
        # SKIP (never fire). The bull setup requires a DESCENDING (or flat) upper line.
        if upper_slope > 0 and lower_slope > 0:
            debug["wedge_kind"] = "rising"
            return False, "wedge_break_rising_skip", debug
        debug["wedge_kind"] = "falling" if upper_slope < 0 else "symmetric"

        # The break LEVEL = the upper (resistance) line projected to the CURRENT bar. Use a
        # simple linear extrapolation from the two upper taps; clamp to >= the newer tap so a
        # descending line never sets a level BELOW the most-recent high (we break the line, not
        # a stale high). STOP = the apex low = the most-recent lower-line pivot (back INTO the
        # wedge). The vol-floor layer widens it downstream (INVARIANT A).
        idx_span = max(1, int(h1["idx"]) - int(h2["idx"]))
        per_bar = upper_slope / idx_span
        level = h1_p + per_bar * max(0, cur - int(h1["idx"]))
        level = max(level, h1_p)   # never below the last real tap
        stop = l1_p
        if not (0.0 < stop < level):
            return False, "wedge_break_bad_level", debug

        # current price (live tick when above the bar close; never lowers the price).
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "wedge_break_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass

        # ── NOT BACKSIDE (the #1 chase guard) ───────────────────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "wedge_break_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "wedge_break_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            # thin/degenerate frame -> fail-CLOSED for this new-conviction fire path.
            debug["reason"] = "front_side_read_error"
            return False, "wedge_break_backside_lifecycle", debug

        # ── NOT PARABOLIC (extension vs 9-EMA AND VWAP) ─────────────────────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "wedge_break_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"wedge_break_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # ── BREAK: price must be breaking OUT of the wedge at the apex ──────────────────
        cur_hi = float(high.iloc[cur])
        _broke = (cur_px > level) or (cur_hi > level)
        if not _broke:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire without buyers on tape) ──
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "wedge_break_tape_unconfirmed", debug
        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "wedge_break_tick", debug
        return True, "wedge_break", debug
    except Exception:
        return False, "wedge_break_error", {"entry_interval": entry_interval}


def absorption_snap_entry(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """GAP 3 (Warrior re-audit): ABSORPTION / soaker + absorption-then-SNAP L2/tape long
    (flag ``chili_momentum_absorption_snap_entry_enabled``).

    A large resting SELLER on the ask is being ABSORBED — eaten by buyers, the ask-wall
    refilled repeatedly but price HOLDS just under it on buy-side OFI — then the SNAP when
    the wall CLEARS (price ticks through the absorption level on accelerating buy flow).
    This is Ross's "ask is getting eaten, it pops the second it clears" read.

    Reuses the L2 book read (``read_ladder_distribution`` → OFI / micro_edge / ask_build)
    + the bar structure. The absorption LEVEL = the recent intrabar resistance the price
    is pinned under (the recent completed-bar high); STOP = below the absorption hold
    (the recent swing low; ``pullback_low``). Shared (ok, reason, debug) + the IDENTICAL
    pullback_high / pullback_low keys.

    DETECTION (all required):
      (1) buy-side OFI: ``ofi >= chili_momentum_ofi_threshold`` (demand pressing the offer);
      (2) the ASK is being REFILLED / built (``ask_build >= 0`` — a wall present, being
          absorbed, not vanishing because price ran away);
      (3) price HOLDING just under the level (a higher-low; not breaking down);
      (4) the SNAP: price (live tick / current high) ticks ABOVE the absorption level.

    CHASE-GUARDS (each reused; no weakened veto):
      * TAPE REQUIRED + FAIL-CLOSED (``tape_confirms_hold``);
      * NOT PARABOLIC (``_hod_extension_ok`` vs 9-EMA AND VWAP);
      * NOT BACKSIDE / NOT BELOW-VWAP (``_detect_back_side`` + ``front_side_state``);
      * L2 hidden-seller / big-seller veto (``_l2_entry_veto``).

    ADDITIVE: flag OFF / thin / no absorption shape / no snap ⇒ ``(False, reason, {...})``
    with NO side effects; fail-OPEN to a benign decline on any error.
    docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "absorption_snap"}
    try:
        if not bool(getattr(settings, "chili_momentum_absorption_snap_entry_enabled", False)):
            return False, "absorption_snap_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, "absorption_snap_insufficient_bars", debug
        if db is None or not symbol:
            return False, "absorption_snap_no_l2", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr_pct, _atr_abs = _batch_c_atr_pct(df, close, cur)

        # current price (live tick when above the bar close; never lowers).
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "absorption_snap_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass

        # The absorption LEVEL = the recent COMPLETED-bar high (the resistance the price is
        # pinned under, where the ask wall sits). STOP = the recent swing low (the hold).
        K = max(3, int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2) * 2)
        w_start = max(0, cur - K)
        if w_start >= cur:
            return False, "absorption_snap_insufficient_bars", debug
        level = float(high.iloc[w_start:cur].max())
        stop = float(low.iloc[w_start:cur].min())
        if not (0.0 < stop < level):
            return False, "absorption_snap_bad_structure", debug
        debug["absorption_level"] = round(level, 6)
        debug["absorption_low"] = round(stop, 6)

        # ── (1)+(2) L2 ABSORPTION SHAPE: buy-side OFI while the ask is being REFILLED ─────
        from .pipeline import read_ladder_distribution

        lr = read_ladder_distribution(symbol, db=db, as_of=l2_as_of)
        if lr is None or int(getattr(lr, "n_snaps", 0) or 0) <= 0:
            return False, "absorption_snap_no_l2", debug
        try:
            thr = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
        except (TypeError, ValueError):
            thr = 0.25
        ofi = getattr(lr, "ofi", None)
        ask_build = getattr(lr, "ask_build", None)
        if ofi is None:
            return False, "absorption_snap_no_ofi", debug
        try:
            if float(ofi) < thr:
                return False, "absorption_snap_weak_ofi", debug
        except (TypeError, ValueError):
            return False, "absorption_snap_no_ofi", debug
        # The ask wall must be PRESENT / refilling (absorbed, not vanished). ask_build >= 0
        # means Σask5 held or grew across the window (a refilling wall). Fail-CLOSED when the
        # build read is unavailable (we cannot prove absorption without it).
        if ask_build is None:
            return False, "absorption_snap_no_ask_build", debug
        try:
            if float(ask_build) < 0.0:
                return False, "absorption_snap_wall_vanished", debug
        except (TypeError, ValueError):
            return False, "absorption_snap_no_ask_build", debug
        debug["ofi"] = round(float(ofi), 4)
        debug["ask_build"] = round(float(ask_build), 4)

        # ── (3) HOLDING just under the level (a higher-low; the dip did not break down) ──
        # the current low must hold at/above the absorption low (no new low while absorbing).
        if float(low.iloc[cur]) < stop:
            return False, "absorption_snap_broke_low", debug

        # ── NOT BACKSIDE ────────────────────────────────────────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "absorption_snap_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "absorption_snap_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            debug["reason"] = "front_side_read_error"
            return False, "absorption_snap_backside_lifecycle", debug

        # ── NOT PARABOLIC ──────────────────────────────────────────────────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "absorption_snap_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"absorption_snap_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # ── (4) THE SNAP: price ticks ABOVE the absorption level (the wall cleared) ──────
        cur_hi = float(high.iloc[cur])
        _snapped = (cur_px > level) or (cur_hi > level)
        if not _snapped:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED ────────────────────────────────────────────────
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "absorption_snap_tape_unconfirmed", debug
        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "absorption_snap_tick", debug
        return True, "absorption_snap", debug
    except Exception:
        return False, "absorption_snap_error", {"entry_interval": entry_interval}


# ── GAP-B — TIGHT-MOMENTUM FALSE-BREAKOUT-REVERSAL / VWAP-RECLAIM ENTRY ────────────
# A NEW trigger family (distinct from every break/pullback/HOD/ORB trigger above). The
# CORE is the TIGHT-MOMENTUM regime: a name whose intrabar range has COMPRESSED to the
# low tail of its own recent range distribution (a coil), with REQUIRED order-flow
# (fail-closed) + a self-relative VOLUME surge. On that tight base it fires on ONE of two
# geometries: (B.2) a FALSE-BREAKOUT REVERSAL (a push pierces a level L, fails/flushes
# below L on elevated/red volume, then the current bar RIPS back and reclaims L), or
# (B.3) a VWAP-RECLAIM (price reclaims VWAP from below with upward momentum). It carries
# the SAME four chase-guards every breakout trigger carries (tape REQUIRED + fail-closed
# via tape_confirms_hold as the LAST gate, _hod_extension_ok NOT-parabolic, _detect_back_side
# + front_side_state NOT-backside/NOT-below-VWAP, _l2_entry_veto) and returns the shared
# (ok, reason, debug) 3-tuple with pullback_high / pullback_low under the IDENTICAL keys,
# so the runner's structural-stop + sizing + breakout-or-bailout machinery + the setup-
# selector are reused unchanged. EVERY threshold is an ADAPTIVE percentile/ratio of the
# name's OWN recent distribution (no magic numbers) with ONE documented base each. The
# structural stop + #769 max-loss circuit stay AHEAD + always-live; GAP-A governs only the
# first post-entry window and can only TIGHTEN. Flag-gated INSIDE the detector (default
# OFF -> disabled before any compute, byte-identical). MUTUALLY EXCLUSIVE per-tick with
# raw_break/break_retest (flag-selected at dispatch; no double-fire). docs/DESIGN/MOMENTUM_LANE.md


def _tight_compression(
    high: pd.Series, low: pd.Series, close: pd.Series, cur: int, *, lookback: int,
    coil_bars: int = 5,
) -> tuple[float | None, float | None, list[float]]:
    """PURE: the TIGHT-MOMENTUM compression read at ``cur`` and the name's OWN recent
    compression distribution (so the threshold theta_c is a self-relative percentile, not a
    magic level).

    Per-bar ``atr_pct`` proxy = (High-Low)/Close (a one-bar true-range fraction; cheap,
    lookahead-free, no EMA warmup). The compression read is the recent COIL (the base
    LEADING INTO the firing bar — the median atr_pct of the last ``coil_bars`` COMPLETED bars
    BEFORE ``cur``) divided by the longer ``lookback`` median: a coiled base tightens BELOW
    1.0. The firing bar itself (the rip-back / reclaim) is EXCLUDED — it is wide by nature, so
    measuring tightness on it would always read non-tight. ``compression`` = median(coil
    atr_pct) / median(lookback atr_pct). TIGHT ⇒ compression below the low tail of the name's
    own recent rolling-compression distribution. Returns ``(compression_now, coil_atr_pct,
    recent_compression_distribution)``; the distribution is the rolling coil-vs-baseline ratio
    at each recent completed bar, so the caller derives theta_c = p30 of THIS list.
    ``(None, None, [])`` on thin data."""
    look = min(int(lookback), cur)
    cb = max(1, int(coil_bars))
    if look < max(3, cb + 1):
        return None, None, []
    def _bar_atr_pct(i: int) -> float | None:
        try:
            h = float(high.iloc[i]); l = float(low.iloc[i]); c = float(close.iloc[i])
        except (TypeError, ValueError, IndexError):
            return None
        if not (c > 0) or not (h >= l):
            return None
        return (h - l) / c
    def _med(xs: list[float]) -> float | None:
        xs = sorted(v for v in xs if math.isfinite(v))
        if not xs:
            return None
        m = len(xs)
        return xs[m // 2] if m % 2 == 1 else 0.5 * (xs[m // 2 - 1] + xs[m // 2])
    # per-bar atr_pct over the COMPLETED bars [cur-look, cur) (EXCLUDE the firing bar).
    series: list[float] = []
    for i in range(cur - look, cur):
        v = _bar_atr_pct(i)
        if v is not None and math.isfinite(v):
            series.append(v)
    if len(series) < (cb + 1):
        return None, None, []
    baseline_med = _med(series)
    if baseline_med is None or not (baseline_med > 0):
        return None, None, []
    # the COIL = the last coil_bars completed bars BEFORE cur (the base into the setup).
    coil_now = _med(series[-cb:])
    if coil_now is None:
        return None, None, []
    comp_now = coil_now / baseline_med
    # the name's OWN recent rolling-compression distribution: at each recent completed bar t,
    # the median of the prior coil_bars / the baseline median (self-relative ratios). theta_c =
    # a percentile of these, so a structurally compressed name still reads tight vs its OWN past.
    dist: list[float] = []
    for t in range(cb, len(series)):
        cm = _med(series[t - cb:t])
        if cm is not None and math.isfinite(cm / baseline_med):
            dist.append(cm / baseline_med)
    if not dist:
        dist = [comp_now]
    return comp_now, coil_now, dist


def _recent_rvol_distribution(vol: pd.Series, cur: int, *, lookback: int) -> list[float]:
    """PURE: each recent completed bar's RVOL (bar volume / the rolling mean of the prior
    ``lookback`` bars) — the name's OWN recent RVOL distribution, used to derive the adaptive
    volume multiple ``vmult`` (a quantile of THIS list). Lookahead-free; ``[]`` on thin data."""
    n = len(vol)
    look = min(int(lookback), cur)
    if look < 3:
        return []
    out: list[float] = []
    for i in range(cur - look + 1, cur + 1):
        j0 = max(0, i - look)
        prior = vol.iloc[j0:i]
        if len(prior) < 1:
            continue
        try:
            mean_prior = float(prior.mean())
            v = float(vol.iloc[i])
        except (TypeError, ValueError, IndexError):
            continue
        if mean_prior > 0 and math.isfinite(v):
            out.append(v / mean_prior)
    return [r for r in out if math.isfinite(r)]


def false_break_reclaim_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """GAP-B: TIGHT-MOMENTUM FALSE-BREAKOUT-REVERSAL / VWAP-RECLAIM entry (flag
    ``chili_momentum_entry_tight_false_break_reclaim_enabled``, default OFF).

    A NEW trigger family. The CORE (all REQUIRED, fail-closed) is the TIGHT-MOMENTUM regime:

      (1) COMPRESSION — ``compression = atr_pct_now / median(atr_pct, Lc)`` is TIGHT when it
          sits below ``theta_c`` = the p30 of the name's OWN recent compression distribution
          (adaptive; ``_tight_compression`` + ``hold_signals.percentile``). A coil, not chop.
      (2) FLOW_OK — REQUIRED + FAIL-CLOSED: ``ofi_level > +T_flow_entry`` AND ``ofi_slope > 0``
          (demand pressing AND building). T_flow_entry = the |lower-tail| (``flow_tail_q``) of
          the name's OWN recent OFI-level distribution, floored at the lane's existing
          ``ofi_threshold``. Any stale / empty / thin tape ⇒ flow_ok = False ⇒ NO fire.
      (3) VOL_OK — ``trade_volume_now > vmult * median(trade_volume, Lv)``, ``vmult =
          clamp(q60(recent RVOL distribution), floor, ceil)`` (the SAME adaptive-volume
          kill-2.5x logic; ``_recent_rvol_distribution`` + the shipped floor/ceil knobs).

    Then ONE geometry (B.2 OR B.3):

      (B.2) FALSE-BREAKOUT REVERSAL — within the recent tail a push PIERCED a level L (a swing
            high), the next bars FAILED/FLUSHED below L (a close back under L) on elevated /
            red volume, and the CURRENT bar RIPS back and RECLAIMS L (trades/closes back above
            it). Entry = L (the reclaimed level; ``pullback_high``); STOP = the flush low
            (``pullback_low``).
      (B.3) VWAP-RECLAIM — the prior bar(s) sat BELOW VWAP and the current bar reclaims it
            from below with upward momentum (close > VWAP, close > open). Entry = VWAP-reclaim
            level = max(VWAP, prior bar high) (``pullback_high``); STOP = the recent swing low
            (``pullback_low``).

    Fires ONLY when TIGHT ∧ flow_ok ∧ vol_ok ∧ (B.2 ∨ B.3) ∧ ALL FOUR chase-guards pass.

    CHASE-GUARDS (each reused; no weakened veto), wired EXACTLY like ``wedge_break_entry`` /
    ``absorption_snap_entry``:
      * NOT BACKSIDE / NOT BELOW-VWAP — ``_detect_back_side`` (1m EMA/MACD rollover) AND
        ``front_side_state`` (folds in below-VWAP / faded; FAILS CLOSED on a thin frame).
      * NOT PARABOLIC — ``_hod_extension_ok`` vs the 9-EMA AND VWAP.
      * L2 hidden-seller / big-seller veto — ``_l2_entry_veto``.
      * TAPE REQUIRED + FAIL-CLOSED — ``tape_confirms_hold`` is the LAST gate before each
        return-True (in ADDITION to the core flow_ok gate; both must confirm).

    Shared (ok, reason, debug) + the IDENTICAL pullback_high / pullback_low keys, so the
    runner's stop / sizing / breakout-or-bailout machinery + the setup-selector are reused
    unchanged. ADDITIVE: flag OFF / thin / not tight / weak flow / weak volume / no geometry
    ⇒ ``(False, reason, {...})`` with NO side effects; fail-OPEN to a benign decline on any
    error (never raises, never blocks the rest of the ladder). docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "tight_false_break_reclaim"}
    try:
        if not bool(getattr(settings, "chili_momentum_entry_tight_false_break_reclaim_enabled", False)):
            return False, "tight_false_break_reclaim_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, "tight_false_break_reclaim_insufficient_bars", debug
        from .hold_signals import adaptive_quantile_clamp, percentile

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(
            df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"}
        )
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr_pct, _atr_abs = _batch_c_atr_pct(df, close, cur)

        # ── (1) COMPRESSION (TIGHT) — adaptive theta_c = p30 of the name's own dist ─────
        _lc = int(getattr(settings, "chili_momentum_tight_compression_lookback", 20) or 20)
        _coil = int(getattr(settings, "chili_momentum_tight_compression_coil_bars", 5) or 5)
        comp_now, _bar_atr_pct_now, comp_dist = _tight_compression(
            high, low, close, cur, lookback=_lc, coil_bars=_coil
        )
        if comp_now is None or not comp_dist:
            return False, "tight_false_break_reclaim_no_compression", debug
        # NOTE: a quantile of 0.0 is a VALID (most-aggressive) setting, so do NOT coalesce it
        # with `or` (which would silently snap 0.0 -> the default); use a None-safe read.
        _theta_q_raw = getattr(settings, "chili_momentum_tight_compression_pctile", 0.30)
        _theta_q = float(_theta_q_raw) if _theta_q_raw is not None else 0.30
        theta_c = percentile(comp_dist, _theta_q)
        debug["compression"] = round(float(comp_now), 4)
        debug["theta_c"] = (round(float(theta_c), 4) if theta_c is not None else None)
        if theta_c is None or float(comp_now) >= float(theta_c):
            return False, "tight_false_break_reclaim_not_tight", debug

        # ── (3) VOL_OK — vmult = clamp(q60(recent RVOL dist), floor, ceil) ──────────────
        # (computed before flow so a cheap reject short-circuits before the DB tape read.)
        _lv = int(getattr(settings, "chili_momentum_tight_volume_lookback", 20) or 20)
        rvol_dist = _recent_rvol_distribution(vol, cur, lookback=_lv)
        _vq_raw = getattr(settings, "chili_momentum_tight_volume_pctile", 0.60)
        _vq = float(_vq_raw) if _vq_raw is not None else 0.60
        _vfloor = float(getattr(settings, "chili_momentum_tight_volume_mult_floor", 1.5) or 1.5)
        _vceil = float(getattr(settings, "chili_momentum_tight_volume_mult_ceil", 3.0) or 3.0)
        vmult = adaptive_quantile_clamp(
            rvol_dist, _vq, floor=_vfloor, ceil=_vceil, fallback=_vfloor
        )
        # current bar volume vs the median of the recent completed bars (self-relative).
        _vol_look = min(_lv, cur)
        _vol_med = None
        if _vol_look >= 2:
            try:
                _vol_med = float(vol.iloc[cur - _vol_look:cur].median())
            except (TypeError, ValueError, IndexError):
                _vol_med = None
        try:
            _vol_now = float(vol.iloc[cur])
        except (TypeError, ValueError, IndexError):
            _vol_now = None
        debug["vmult"] = round(float(vmult), 3)
        if _vol_med is None or not (_vol_med > 0) or _vol_now is None:
            return False, "tight_false_break_reclaim_no_volume", debug
        debug["vol_ratio"] = round(_vol_now / _vol_med, 3)
        if _vol_now <= vmult * _vol_med:
            return False, "tight_false_break_reclaim_weak_volume", debug

        # ── (2) FLOW_OK — REQUIRED + FAIL-CLOSED (ofi_level > +T_flow_entry ∧ slope > 0) ─
        # Reuse the wqwu5t2n2 live flow read (denoised OFI level + EWMA slope). A None read
        # (no db / no symbol / crypto / thin / stale / error) ⇒ flow_ok=False ⇒ NO fire.
        from .pipeline import _live_flow_slope

        _fs_flow = _live_flow_slope(symbol, db=db, as_of=l2_as_of) if (db is not None and symbol) else None
        if not _fs_flow:
            debug["flow_reason"] = "no_flow_read"
            return False, "tight_false_break_reclaim_no_flow", debug
        _ofi_level = _fs_flow.get("ofi_level")
        _ofi_slope = _fs_flow.get("ofi_slope")
        if _ofi_level is None or _ofi_slope is None:
            debug["flow_reason"] = "incomplete_flow_read"
            return False, "tight_false_break_reclaim_no_flow", debug
        # T_flow_entry = the |lower-tail| of the name's own recent OFI-level distribution
        # (here the available sample is the per-grid OFI level series proxy; we floor it at
        # the lane's existing ofi_threshold so the entry pressure bar is never weaker than
        # the lane's documented base). ADAPTIVE with ONE documented floor.
        _flow_tail_q_raw = getattr(settings, "chili_momentum_tight_flow_tail_q", 0.15)
        _flow_tail_q = float(_flow_tail_q_raw) if _flow_tail_q_raw is not None else 0.15
        _flow_floor = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
        # the grid OFI levels aren't returned, so derive T_flow_entry from the single floor
        # (documented base) — adaptive_quantile_clamp degrades to the floor on a thin sample,
        # which is exactly this case (one live level), keeping ONE documented base, no magic.
        t_flow_entry = adaptive_quantile_clamp(
            [], _flow_tail_q, floor=_flow_floor, ceil=max(_flow_floor, 1.0), fallback=_flow_floor
        )
        debug["ofi_level"] = round(float(_ofi_level), 4)
        debug["ofi_slope"] = round(float(_ofi_slope), 5)
        debug["t_flow_entry"] = round(float(t_flow_entry), 4)
        if not (float(_ofi_level) > float(t_flow_entry) and float(_ofi_slope) > 0.0):
            debug["flow_reason"] = "weak_or_rolling_flow"
            return False, "tight_false_break_reclaim_weak_flow", debug

        # current price (live tick when above the bar close; never lowers the price).
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "tight_false_break_reclaim_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass

        # ── GEOMETRY: (B.2) false-breakout reversal OR (B.3) VWAP-reclaim ───────────────
        # window of recent COMPLETED bars to read the pierce/flush/reclaim shape.
        K = max(3, int(getattr(settings, "chili_momentum_tight_geometry_lookback", 6) or 6))
        w0 = max(0, cur - K)
        level: float | None = None
        stop: float | None = None
        geom: str | None = None

        # (B.2) FALSE-BREAKOUT REVERSAL: a level L (the max swing high among the completed
        # bars BEFORE the most recent flush) was pierced, then a bar CLOSED back below L
        # (the failed breakout / flush) on elevated-or-red volume, and now we reclaim L.
        try:
            if w0 < cur - 1:
                # L = the highest completed-bar HIGH in the window EXCLUDING the last bar
                # (the level that was pierced then lost).
                _Lwin_hi = high.iloc[w0:cur]
                _Lwin_cl = close.iloc[w0:cur]
                _Lwin_lo = low.iloc[w0:cur]
                L = float(_Lwin_hi.max())
                # a PIERCE: some completed bar's high exceeded L's region (by construction L is
                # the max high, so the bar that set it pierced). A FAIL: a LATER completed bar
                # closed back BELOW L. Find the pierce bar (argmax high) and require a close
                # below L after it (the flush).
                _hi_vals = list(_Lwin_hi.values)
                _pierce_off = int(_hi_vals.index(max(_hi_vals)))  # offset within [w0, cur)
                _pierce_idx = w0 + _pierce_off
                _flush_low = None
                _failed = False
                for j in range(_pierce_idx + 1, cur):
                    try:
                        if float(close.iloc[j]) < L:
                            _failed = True
                            _lj = float(low.iloc[j])
                            _flush_low = _lj if _flush_low is None else min(_flush_low, _lj)
                    except (TypeError, ValueError, IndexError):
                        continue
                # RECLAIM: the current bar trades/closes back above L (the rip-back).
                _reclaimed = (cur_px > L) or (float(high.iloc[cur]) > L)
                if _failed and _reclaimed and _flush_low is not None and 0.0 < _flush_low < L:
                    level = L
                    stop = _flush_low
                    geom = "false_break_reversal"
                    debug["fbr_level"] = round(L, 6)
                    debug["fbr_flush_low"] = round(_flush_low, 6)
        except (TypeError, ValueError, IndexError):
            pass

        # (B.3) VWAP-RECLAIM: the prior bar(s) sat BELOW VWAP and the current bar reclaims it
        # from below with upward momentum (close > VWAP and close > open). Entry = the reclaim
        # level = max(VWAP_cur, prior-bar high); STOP = the recent swing low in the window.
        if geom is None:
            try:
                _vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
                _vwap_prev = vwap[cur - 1] if cur - 1 < len(vwap) and vwap[cur - 1] is not None else None
                if _vwap_cur is not None and float(_vwap_cur) > 0:
                    _prev_close = float(close.iloc[cur - 1])
                    _below_before = (
                        _prev_close < float(_vwap_prev) if _vwap_prev is not None
                        else _prev_close < float(_vwap_cur)
                    )
                    _o_cur = float(opn.iloc[cur]) if opn is not None else cur_close
                    _up_momentum = (cur_close > float(_vwap_cur)) and (cur_close > _o_cur)
                    if _below_before and _up_momentum:
                        _prev_hi = float(high.iloc[cur - 1])
                        level = max(float(_vwap_cur), _prev_hi)
                        stop = float(low.iloc[w0:cur + 1].min())
                        geom = "vwap_reclaim"
                        debug["vwap_reclaim_level"] = round(level, 6)
                        debug["vwap"] = round(float(_vwap_cur), 6)
            except (TypeError, ValueError, IndexError):
                pass

        if geom is None or level is None or stop is None or not (0.0 < stop < level):
            return False, "tight_false_break_reclaim_no_geometry", debug
        debug["geometry"] = geom

        # ── CHASE-GUARD 1: NOT BACKSIDE (1m EMA/MACD rollover) ──────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "tight_false_break_reclaim_back_side", debug
        # ── CHASE-GUARD 1b: NOT BELOW-VWAP / NOT FADED (front_side_state; fail-CLOSED) ───
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "tight_false_break_reclaim_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            debug["reason"] = "front_side_read_error"
            return False, "tight_false_break_reclaim_backside_lifecycle", debug

        # ── CHASE-GUARD 2: NOT PARABOLIC (extension vs 9-EMA AND VWAP) ──────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "tight_false_break_reclaim_extended", debug

        # ── CHASE-GUARD 3: L2 hidden-seller / big-seller veto (reused; fail-open) ───────
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"tight_false_break_reclaim_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # ── CHASE-GUARD 4: TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire w/o buyers) ─
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "tight_false_break_reclaim_tape_unconfirmed", debug

        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "tight_false_break_reclaim_tick", debug
        return True, "tight_false_break_reclaim", debug
    except Exception:
        return False, "tight_false_break_reclaim_error", {"entry_interval": entry_interval}


# ── LOCATE #2/#4/#5/#6: four NEW scalp/dip entry triggers ─────────────────────────
# Each is flag-gated INSIDE the detector (default OFF -> returns disabled BEFORE any
# compute = byte-identical), carries the SAME chase-guards as wedge/absorption (tape
# REQUIRED+fail-closed via tape_confirms_hold as the LAST gate, _hod_extension_ok
# (NOT parabolic) + _detect_back_side + front_side_state (NOT backside / NOT below-VWAP),
# _l2_entry_veto) and returns the shared (ok, reason, debug) 3-tuple with pullback_low /
# pullback_high under the IDENTICAL keys so the runner's structural-stop + sizing +
# breakout-or-bailout machinery + the setup-selector are reused unchanged. No lookahead
# (levels from completed bars; the live tick break is the only intrabar use). Fail-OPEN
# to a benign decline on any error. docs/DESIGN/MOMENTUM_LANE.md


def _dip_velocity_size_mult(
    *, dip_roc_per_bar: float | None, atr_pct: float | None, settings_obj: Any = settings,
) -> float:
    """LOCATE #3 DIP-VELOCITY CONVICTION multiplier. A steeper/faster dip (a more violent
    algo-stop-run) snaps back harder, so scale entry SIZE by the dip's ROC (per-bar % drop)
    measured RELATIVE to the instrument's own ATR%. Returns a multiplier in
    ``[1.0, 1+max_boost]`` (NEVER < 1.0, so it can only ADD conviction within the existing
    3x clamp + max_notional caps, never reduce size below the base / increase risk past the
    caps). Pure; fail-OPEN to ``1.0`` (no boost) on flag-off / missing data / any error."""
    try:
        if not bool(getattr(settings_obj, "chili_momentum_dip_velocity_conviction_enabled", False)):
            return 1.0
        if dip_roc_per_bar is None:
            return 1.0
        roc = abs(float(dip_roc_per_bar))
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.01
        # The ROC is "steep" when the per-bar drop exceeds ~1 ATR%; interpolate the boost
        # from 0 (at 1x ATR%) to max_boost (at 3x ATR%+). Below 1x ATR% = noise -> no boost.
        floor = a
        ceil = 3.0 * a
        if roc <= floor or ceil <= floor:
            return 1.0
        frac = min(1.0, (roc - floor) / (ceil - floor))
        max_boost = float(getattr(settings_obj, "chili_momentum_dip_velocity_conviction_max_boost", 0.25) or 0.0)
        max_boost = max(0.0, min(0.5, max_boost))
        return 1.0 + frac * max_boost
    except (TypeError, ValueError):
        return 1.0


def ask_thins_dip_entry(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """LOCATE #2: ASK-THINS-TO-ZERO DIP-BOTTOM long (flag
    ``chili_momentum_ask_thins_dip_entry_enabled``).

    A real dip (an ATR-scaled retrace that holds a structural higher-low) where the resting
    ASK supply has been EXHAUSTED — the offer collapsed across the L2 window
    (``read_ladder_distribution.ask_build <= -min_depletion_frac``) WITH buy-side OFI — then
    price ticks back up off the dip low. The thinning offer means the sellers are gone, so
    the bounce has room. Entry = the bounce/recent high (``pullback_high``); STOP = the dip
    low (``pullback_low``).

    CHASE-GUARDS (each reused; no weakened veto): TAPE REQUIRED+FAIL-CLOSED
    (``tape_confirms_hold``), NOT PARABOLIC (``_hod_extension_ok`` vs 9-EMA AND VWAP), NOT
    BACKSIDE / NOT BELOW-VWAP (``_detect_back_side`` + ``front_side_state``), L2 hidden/
    big-seller veto (``_l2_entry_veto``). ADDITIVE: flag OFF / thin / no depletion / no dip
    ⇒ ``(False, reason, {...})`` with NO side effects; fail-OPEN on any error."""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "ask_thins_dip"}
    try:
        if not bool(getattr(settings, "chili_momentum_ask_thins_dip_entry_enabled", False)):
            return False, "ask_thins_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, "ask_thins_insufficient_bars", debug
        if db is None or not symbol:
            return False, "ask_thins_no_l2", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr_pct, _atr_abs = _batch_c_atr_pct(df, close, cur)

        # current price (live tick when above the bar close; never lowers the price).
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "ask_thins_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass

        # ── STRUCTURE: a real dip off a recent reference high holding a higher-low ───────
        K = max(3, int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2) * 2)
        w_start = max(0, cur - K)
        if w_start >= cur:
            return False, "ask_thins_insufficient_bars", debug
        ref_high = float(high.iloc[w_start:cur].max())
        dip_low = float(low.iloc[w_start:cur + 1].min())
        if not (0.0 < dip_low < ref_high):
            return False, "ask_thins_bad_structure", debug
        dip_depth = (ref_high - dip_low) / ref_high if ref_high > 0 else 0.0
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
        noise_floor = max(0.003, 0.5 * (a or 0.01))
        deep_cap = _collapse_cap(atr_pct)
        if dip_depth < noise_floor:
            return False, "ask_thins_too_shallow", debug
        if dip_depth > deep_cap:
            return False, "ask_thins_too_deep", debug
        # the CURRENT bar must hold the dip (a higher-low; no fresh new low while thinning).
        if float(low.iloc[cur]) < dip_low * (1.0 - 1e-9):
            return False, "ask_thins_broke_low", debug
        # the break/bounce LEVEL = the recent reference high; STOP = the dip low.
        level = ref_high
        stop = dip_low
        debug["dip_low"] = round(stop, 6)
        debug["dip_depth_pct"] = round(dip_depth * 100.0, 2)
        # dip ROC (per-bar % drop into the low) — the conviction yardstick (#3 reads this).
        try:
            _lo_pos = int(low.iloc[w_start:cur + 1].astype(float).values.argmin()) + w_start
            _ref_pos = int(high.iloc[w_start:cur].astype(float).values.argmax()) + w_start
            _bars = max(1, _lo_pos - _ref_pos)
            debug["dip_roc_per_bar"] = round(dip_depth / _bars, 6)
        except (TypeError, ValueError):
            debug["dip_roc_per_bar"] = None

        # ── (1)+(2) L2 ASK DEPLETION: the offer collapsed across the window + buy-side OFI ─
        from .pipeline import read_ladder_distribution

        lr = read_ladder_distribution(symbol, db=db, as_of=l2_as_of)
        if lr is None or int(getattr(lr, "n_snaps", 0) or 0) <= 0:
            return False, "ask_thins_no_l2", debug
        ask_build = getattr(lr, "ask_build", None)
        ofi = getattr(lr, "ofi", None)
        if ask_build is None:
            return False, "ask_thins_no_ask_build", debug
        try:
            min_dep = abs(float(getattr(settings, "chili_momentum_ask_thins_min_depletion_frac", 0.25) or 0.25))
            if float(ask_build) > -min_dep:
                return False, "ask_thins_ask_not_depleted", debug
        except (TypeError, ValueError):
            return False, "ask_thins_no_ask_build", debug
        # buy-side OFI confirms demand is pressing the (now-thin) offer. Fail-CLOSED on miss.
        try:
            ofi_thr = abs(float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25))
        except (TypeError, ValueError):
            ofi_thr = 0.25
        if ofi is None:
            return False, "ask_thins_no_ofi", debug
        try:
            if float(ofi) < ofi_thr:
                return False, "ask_thins_weak_ofi", debug
        except (TypeError, ValueError):
            return False, "ask_thins_no_ofi", debug
        debug["ask_build"] = round(float(ask_build), 4)
        debug["ofi"] = round(float(ofi), 4)

        # ── NOT BACKSIDE ────────────────────────────────────────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "ask_thins_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "ask_thins_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            debug["reason"] = "front_side_read_error"
            return False, "ask_thins_backside_lifecycle", debug

        # ── NOT PARABOLIC ──────────────────────────────────────────────────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "ask_thins_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"ask_thins_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # ── BREAK: price ticks back up off the dip toward the level ─────────────────────
        cur_hi = float(high.iloc[cur])
        _broke = (cur_px > level) or (cur_hi > level)
        if not _broke:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire without buyers on tape) ──
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "ask_thins_tape_unconfirmed", debug
        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "ask_thins_dip_tick", debug
        return True, "ask_thins_dip", debug
    except Exception:
        return False, "ask_thins_error", {"entry_interval": entry_interval}


def sub_vwap_trap_entry(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """LOCATE #4: SUB-VWAP TRAP / short-cover long (flag
    ``chili_momentum_sub_vwap_trap_entry_enabled``).

    A SHARP breakdown BELOW VWAP that FAILS to follow through (a bottoming-tail flush bar
    that undercut VWAP and got bought, no fresh new low for the trap bar) then the CURRENT
    bar RECLAIMS back above VWAP = a bear-trap / short-cover squeeze long. DISTINCT from
    ``vwap_reclaim_confirmation`` (which requires ``K`` sustained closes below VWAP): the
    trap is the violent stop-run undercut-and-reclaim, not a slow loss of VWAP. Entry = the
    reclaim bar high (``pullback_high``); STOP = the trap low (``pullback_low``).

    CHASE-GUARDS reused (no weakened veto): TAPE REQUIRED+FAIL-CLOSED, NOT PARABOLIC
    (``_hod_extension_ok``), NOT BACKSIDE / NOT BELOW-VWAP (``_detect_back_side`` +
    ``front_side_state`` — the reclaim must put price back ABOVE VWAP so front_side_state
    is satisfied), L2 veto. ADDITIVE: flag OFF / thin / no trap ⇒ ``(False, reason, {...})``
    with NO side effects; fail-OPEN on any error."""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "sub_vwap_trap"}
    try:
        if not bool(getattr(settings, "chili_momentum_sub_vwap_trap_entry_enabled", False)):
            return False, "sub_vwap_trap_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return False, "sub_vwap_trap_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr_pct, _atr_abs = _batch_c_atr_pct(df, close, cur)

        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        if vwap_cur is None or float(vwap_cur) <= 0:
            return False, "sub_vwap_trap_vwap_warmup", debug

        # The TRAP bar = the bar before the current (reclaim) bar. It must have UNDERCUT VWAP
        # on its low (the stop-run) and printed a bottoming tail (got bought), not closed down
        # in a trend. The current bar reclaims back above VWAP.
        trap_idx = cur - 1
        if trap_idx < 1:
            return False, "sub_vwap_trap_insufficient_bars", debug
        vwap_trap = vwap[trap_idx] if trap_idx < len(vwap) and vwap[trap_idx] is not None else None
        if vwap_trap is None or float(vwap_trap) <= 0:
            return False, "sub_vwap_trap_vwap_warmup", debug
        t_o = float(opn.iloc[trap_idx]) if opn is not None else float(close.iloc[trap_idx - 1])
        t_h, t_l, t_c = float(high.iloc[trap_idx]), float(low.iloc[trap_idx]), float(close.iloc[trap_idx])
        # UNDERCUT: the trap low pierced below VWAP (the breakdown that traps shorts).
        if t_l >= float(vwap_trap):
            return False, "sub_vwap_trap_no_undercut", debug
        # BOTTOMING TAIL: the flush got bought (close in the upper part of the bar range).
        if not _bottoming_tail(t_o, t_h, t_l, t_c):
            return False, "sub_vwap_trap_no_bottoming_tail", debug
        trap_low = t_l
        if not (trap_low > 0):
            return False, "sub_vwap_trap_bad_low", debug

        # CURRENT bar RECLAIMS above VWAP (price + close back above) AND holds the trap low.
        px = float(live_price) if (live_price is not None and float(live_price) > 0) else float(close.iloc[cur])
        if px < float(vwap_cur) or float(close.iloc[cur]) < float(vwap_cur):
            return False, "waiting_for_vwap_reclaim", debug  # tick-armable on the reclaim
        if float(low.iloc[cur]) < trap_low * (1.0 - 1e-9):
            return False, "sub_vwap_trap_undercut_again", debug
        from .candles import is_strong_bull_break_candle

        c_o = float(opn.iloc[cur]) if opn is not None else float(close.iloc[cur - 1])
        c_h, c_l, c_c = float(high.iloc[cur]), float(low.iloc[cur]), float(close.iloc[cur])
        if not is_strong_bull_break_candle(
            c_o, c_h, c_l, c_c,
            min_close_pos=float(getattr(settings, "chili_momentum_entry_break_candle_min_close_pos", 0.50) or 0.50),
        ):
            return False, "sub_vwap_trap_weak_reclaim", debug

        level = float(high.iloc[cur])   # the reclaim bar high (break level)
        stop = trap_low                 # the trap low (structural stop)
        if not (0.0 < stop < level):
            return False, "sub_vwap_trap_bad_level", debug

        # ── NOT BACKSIDE / NOT BELOW-VWAP ───────────────────────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "sub_vwap_trap_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "sub_vwap_trap_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            debug["reason"] = "front_side_read_error"
            return False, "sub_vwap_trap_backside_lifecycle", debug

        # ── NOT PARABOLIC ──────────────────────────────────────────────────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=float(vwap_cur), atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "sub_vwap_trap_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"sub_vwap_trap_{_reason}", debug

        # dip ROC for the conviction modifier (#3): the undercut depth per bar.
        try:
            _roc = (float(vwap_trap) - trap_low) / float(vwap_trap) if float(vwap_trap) > 0 else None
            debug["dip_roc_per_bar"] = round(_roc, 6) if _roc is not None else None
        except (TypeError, ValueError):
            debug["dip_roc_per_bar"] = None
        debug.update({
            "pullback_high": float(level),
            "pullback_low": float(stop),
            "vwap": round(float(vwap_cur), 6),
            "atr_pct": (round(atr_pct, 6) if atr_pct is not None else None),
        })

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate) ─────────────────────────────────
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "sub_vwap_trap_tape_unconfirmed", debug
        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "sub_vwap_trap_tick", debug
        return True, "sub_vwap_trap", debug
    except Exception:
        return False, "sub_vwap_trap_error", {"entry_interval": entry_interval}


def pulling_away_roc_entry(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """LOCATE #5: PULLING-AWAY ROC-inflection break (flag
    ``chili_momentum_pulling_away_roc_entry_enabled``).

    Price tapped a multi-tap RESISTANCE band (>= ``min_taps`` swing-high taps within an
    ATR-derived band = a tested ceiling, not a first touch) then PULLS AWAY: the current-bar
    rate-of-change ACCELERATES above its recent baseline by an ATR-scaled margin (a ROC
    inflection — the break is finally going, vs the dead taps before it). Entry = the
    resistance/break level (``pullback_high``); STOP = the last swing low under the base
    (``pullback_low``).

    CHASE-GUARDS reused (no weakened veto): TAPE REQUIRED+FAIL-CLOSED, NOT PARABOLIC
    (``_hod_extension_ok``), NOT BACKSIDE / NOT BELOW-VWAP, L2 veto. ADDITIVE: flag OFF /
    thin / too-few-taps / no ROC inflection ⇒ ``(False, reason, {...})``; fail-OPEN on error."""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "pulling_away_roc"}
    try:
        if not bool(getattr(settings, "chili_momentum_pulling_away_roc_entry_enabled", False)):
            return False, "pulling_away_disabled", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 6):
            return False, "pulling_away_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)
        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        highs = [p for p in pivots if p["kind"] == "H"]
        lows = [p for p in pivots if p["kind"] == "L"]
        min_taps = max(2, int(getattr(settings, "chili_momentum_pulling_away_min_taps", 2) or 2))
        if len(highs) < min_taps:
            return False, "pulling_away_too_few_taps", debug

        # The resistance BAND = the taps clustered near the highest recent swing high within an
        # ATR-derived band. Count taps in the band; require >= min_taps (a tested ceiling).
        top = max(float(p["price"]) for p in highs)
        band = (0.6 * float(atr_abs)) if (atr_abs is not None and atr_abs > 0) else (0.01 * top)
        taps = [p for p in highs if abs(float(p["price"]) - top) <= band]
        debug["n_taps"] = len(taps)
        if len(taps) < min_taps:
            return False, "pulling_away_too_few_band_taps", debug
        level = top
        # STOP = the most-recent swing low UNDER the base (back into the consolidation).
        if not lows:
            return False, "pulling_away_no_stop", debug
        stop = float(lows[-1]["price"])
        if not (0.0 < stop < level):
            return False, "pulling_away_bad_level", debug

        # ── ROC INFLECTION: the current-bar ROC accelerates above its recent baseline ────
        # ROC per bar = fractional close change; baseline = the mean |ROC| over the prior
        # window. A genuine pull-away spikes the current ROC well above that flat baseline.
        roc_lb = 5
        if cur - roc_lb - 1 < 0:
            return False, "pulling_away_insufficient_bars", debug
        cur_roc = (float(close.iloc[cur]) - float(close.iloc[cur - 1])) / float(close.iloc[cur - 1]) if float(close.iloc[cur - 1]) > 0 else 0.0
        base_rocs = []
        for i in range(cur - roc_lb, cur):
            p0 = float(close.iloc[i - 1])
            if p0 > 0:
                base_rocs.append(abs((float(close.iloc[i]) - p0) / p0))
        base_roc = (sum(base_rocs) / len(base_rocs)) if base_rocs else 0.0
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.01
        # the inflection margin = max(baseline + ~0.5 ATR%, a small abs floor) — adaptive.
        infl_margin = max(base_roc + 0.5 * a, 0.003)
        debug["cur_roc"] = round(cur_roc, 5)
        debug["base_roc"] = round(base_roc, 5)
        debug["infl_margin"] = round(infl_margin, 5)
        if cur_roc < infl_margin:
            return False, "pulling_away_no_roc_inflection", debug

        # ── NOT BACKSIDE ────────────────────────────────────────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "pulling_away_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "pulling_away_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            debug["reason"] = "front_side_read_error"
            return False, "pulling_away_backside_lifecycle", debug

        # ── NOT PARABOLIC ──────────────────────────────────────────────────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "pulling_away_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"pulling_away_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # current price (live tick when above the bar close; never lowers).
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "pulling_away_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass
        cur_hi = float(high.iloc[cur])
        _broke = (cur_px > level) or (cur_hi > level)
        if not _broke:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate) ─────────────────────────────────
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "pulling_away_tape_unconfirmed", debug
        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "pulling_away_roc_tick", debug
        return True, "pulling_away_roc", debug
    except Exception:
        return False, "pulling_away_error", {"entry_interval": entry_interval}


def premarket_pivot_macd_entry(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """LOCATE #6: PREMARKET PIVOT + MACD re-cross gap-and-go (flag
    ``chili_momentum_premarket_pivot_macd_entry_enabled``).

    A premarket break: price breaks the premarket PIVOT (the recent premarket swing high)
    WITH a fresh MACD re-cross (the MACD line crosses back ABOVE its signal within the
    lookback — momentum re-igniting) AND a COLD-MARKET avoid (skip when RVOL is below the
    cold floor — a cold premarket with no interest fakes out). Entry = the pivot level
    (``pullback_high``); STOP = the premarket pivot low (``pullback_low``). EQUITY-ONLY
    (crypto is 24/7, no premarket).

    CHASE-GUARDS reused (no weakened veto): TAPE REQUIRED+FAIL-CLOSED, NOT PARABOLIC
    (``_hod_extension_ok``), NOT BACKSIDE / NOT BELOW-VWAP, L2 veto. ADDITIVE: flag OFF /
    crypto / thin / cold / no MACD re-cross ⇒ ``(False, reason, {...})``; fail-OPEN on error."""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "premarket_pivot_macd"}
    try:
        if not bool(getattr(settings, "chili_momentum_premarket_pivot_macd_entry_enabled", False)):
            return False, "premarket_pivot_disabled", debug
        # EQUITY-ONLY: crypto is 24/7 and has no premarket pivot concept.
        if bool(symbol) and str(symbol).upper().endswith("-USD"):
            return False, "premarket_pivot_crypto_exempt", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 6):
            return False, "premarket_pivot_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "atr", "volume_ratio"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        macd = arrays.get("macd") or []
        macd_sig = arrays.get("macd_signal") or []
        vr = arrays.get("volume_ratio") or []
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)

        # COLD-MARKET avoid: a real gap-and-go has volume; skip a cold premarket fake-out.
        rvol = None
        try:
            rvol = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            rvol = None
        cold_floor = float(getattr(settings, "chili_momentum_premarket_pivot_cold_rvol_floor", 1.5) or 0.0)
        debug["rvol"] = round(rvol, 2) if rvol is not None else None
        if cold_floor > 0.0 and (rvol is None or rvol < cold_floor):
            return False, "premarket_pivot_cold_market", debug

        # The premarket PIVOT = the most-recent swing high (the level that, broken, signals the
        # gap-and-go); STOP = the most-recent swing low under it.
        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        highs = [p for p in pivots if p["kind"] == "H"]
        lows = [p for p in pivots if p["kind"] == "L"]
        if not highs or not lows:
            return False, "premarket_pivot_no_pivot", debug
        level = float(highs[-1]["price"])
        stop = float(lows[-1]["price"])
        if not (0.0 < stop < level):
            return False, "premarket_pivot_bad_level", debug

        # ── MACD RE-CROSS: the line crossed back ABOVE signal within the lookback AND is
        # STILL above now (a fresh bullish re-cross, not a stale one). ─────────────────
        _lb = int(_BACKSIDE_MACD_CROSS_LOOKBACK)
        _recrossed = False
        try:
            m_cur = macd[cur] if cur < len(macd) else None
            s_cur = macd_sig[cur] if cur < len(macd_sig) else None
            if m_cur is not None and s_cur is not None and float(m_cur) > float(s_cur):
                lo_i = max(1, cur - _lb + 1)
                for i in range(lo_i, cur + 1):
                    mp = macd[i - 1] if 0 <= i - 1 < len(macd) else None
                    sp = macd_sig[i - 1] if 0 <= i - 1 < len(macd_sig) else None
                    mi = macd[i] if 0 <= i < len(macd) else None
                    si = macd_sig[i] if 0 <= i < len(macd_sig) else None
                    if mp is None or sp is None or mi is None or si is None:
                        continue
                    if float(mp) <= float(sp) and float(mi) > float(si):
                        _recrossed = True
                        break
        except (TypeError, ValueError, IndexError):
            _recrossed = False
        if not _recrossed:
            return False, "premarket_pivot_no_macd_recross", debug
        debug["macd_recross"] = True

        # ── NOT BACKSIDE ────────────────────────────────────────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            macd, macd_sig, cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "premarket_pivot_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "premarket_pivot_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            debug["reason"] = "front_side_read_error"
            return False, "premarket_pivot_backside_lifecycle", debug

        # ── NOT PARABOLIC ──────────────────────────────────────────────────────────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=rvol,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "premarket_pivot_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"premarket_pivot_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["atr_pct"] = (round(atr_pct, 6) if atr_pct is not None else None)

        # current price (live tick when above the bar close; never lowers).
        try:
            cur_close = float(close.iloc[cur])
        except (TypeError, ValueError, IndexError):
            return False, "premarket_pivot_no_close", debug
        cur_px = cur_close
        if live_price is not None:
            try:
                lp = float(live_price)
                if lp > 0:
                    cur_px = max(cur_close, lp)
            except (TypeError, ValueError):
                pass
        cur_hi = float(high.iloc[cur])
        _broke = (cur_px > level) or (cur_hi > level)
        if not _broke:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate) ─────────────────────────────────
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "premarket_pivot_tape_unconfirmed", debug
        if live_price is not None and float(live_price) > 0 and float(live_price) > level:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "premarket_pivot_macd_tick", debug
        return True, "premarket_pivot_macd", debug
    except Exception:
        return False, "premarket_pivot_error", {"entry_interval": entry_interval}


def ross_double_bottom_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross DOUBLE-BOTTOM entry (flag ``chili_momentum_double_bottom_entry_enabled``).

    From the ATR-filtered swing pivots: TWO swing LOWS at ~the same support level (within
    an ATR-derived band), the SECOND printing a bottoming tail / reversal and HOLDING,
    then a BREAK above the intervening swing high. Entry = the intervening-high break
    level (``pullback_high``); stop = below the double-bottom low (``pullback_low``).

    NOISE DEFENSE: the ATR pivot filter (the two lows must be REAL pivots, not chop wiggles)
    + the equal-lows band is ATR-derived (no fixed cents) + the second low must print a
    bottoming tail (a flush that got bought) + HOLD (no new low below the first). The shared
    (ok, reason, debug) + pullback_high/pullback_low keys reuse the runner's stop / sizing /
    bailout machinery unchanged (NO new sizing path). The vol-floor layer widens the stop
    downstream (INVARIANT A) — this gate does NOT pre-floor it.

    ADDITIVE: flag OFF / thin / no double-bottom -> ``(False, reason, {...})`` with NO side
    effects; fail-OPEN to a benign decline on any error. docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "double_bottom"}
    try:
        if not bool(getattr(settings, "chili_momentum_double_bottom_entry_enabled", True)):
            return False, "double_bottom_disabled", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 3):
            return False, "double_bottom_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)
        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        debug["n_pivots"] = len(pivots)
        lows = [p for p in pivots if p["kind"] == "L"]
        if len(lows) < 2:
            return False, "double_bottom_too_few_lows", debug

        # The two most-recent swing lows + the swing HIGH that sits BETWEEN them.
        low2 = lows[-1]   # the second (right) bottom
        low1 = lows[-2]   # the first (left) bottom
        i1, i2 = int(low1["idx"]), int(low2["idx"])
        l1, l2 = float(low1["price"]), float(low2["price"])
        mid_highs = [p for p in pivots if p["kind"] == "H" and i1 < int(p["idx"]) < i2]
        if not mid_highs:
            return False, "double_bottom_no_neckline", debug
        neckline = max(float(p["price"]) for p in mid_highs)  # the intervening swing high

        # EQUAL LOWS: the two bottoms within an ATR-derived band (no fixed cents). When ATR
        # is unavailable, fall back to a small relative band so chop is still rejected.
        band_mult = float(getattr(settings, "chili_momentum_double_bottom_band_atr_mult", 0.6) or 0.6)
        ref = min(l1, l2)
        if ref <= 0:
            return False, "double_bottom_bad_low", debug
        band = (band_mult * float(atr_abs)) if (atr_abs is not None and atr_abs > 0) else (0.01 * ref)
        debug.update({
            "low1": round(l1, 6), "low2": round(l2, 6),
            "neckline": round(neckline, 6),
            "equal_band": round(band, 6),
        })
        if abs(l1 - l2) > band:
            return False, "double_bottom_lows_unequal", debug
        # HOLD: the second low must NOT undercut the first (a clean double bottom, not a
        # lower-low breakdown). A tiny tolerance = the noise band.
        if l2 < l1 - band:
            return False, "double_bottom_second_lower", debug

        # SECOND-LOW REVERSAL: the bar AT (or adjacent to) the second pivot prints a
        # bottoming tail (the flush that got bought back up — the V-bounce signature).
        _bt_ok = False
        try:
            for j in (i2, i2 - 1, i2 + 1):
                if not (0 <= j <= cur):
                    continue
                _o = float(opn.iloc[j]) if opn is not None else float(close.iloc[max(0, j - 1)])
                _h, _l, _c = float(high.iloc[j]), float(low.iloc[j]), float(close.iloc[j])
                if _bottoming_tail(_o, _h, _l, _c):
                    _bt_ok = True
                    break
        except (TypeError, ValueError, IndexError):
            _bt_ok = False
        debug["second_low_bottoming_tail"] = _bt_ok
        if not _bt_ok:
            return False, "double_bottom_no_reversal_tail", debug

        # The break level = the intervening neckline; stop = below the double-bottom low.
        level = neckline
        stop = min(l1, l2)
        if not (0.0 < stop < level):
            return False, "double_bottom_bad_level", debug
        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2).
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"double_bottom_{_reason}", debug

        # ── TRIGGER: the break above the intervening swing high (the neckline) ───────────
        cur_hi = float(high.iloc[cur])
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "double_bottom_break_tick_ok", debug
        if cur_hi <= level:
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)
        arrays_v = compute_all_from_df(df, needed={"volume_ratio"})
        vr = arrays_v.get("volume_ratio") or []
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < _vol_mult:
            return False, "double_bottom_low_volume", debug
        return True, "double_bottom_break", debug
    except Exception:
        return False, "double_bottom_error", {"entry_interval": entry_interval}


def inverse_head_shoulders_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Inverse (inverted) head-and-shoulders entry (SS101 #017; flag
    ``chili_momentum_inverse_head_shoulders_entry_enabled``).

    Ross trades this as a SEPARATE bullish setup ("I do trade the inverted head and
    shoulders"). From the ATR-filtered swing pivots, an inverse H&S long is the LOW
    skeleton ``L(shoulder) H L(head) H L(shoulder)`` with the bracketing shoulder highs::

        LEFT SHOULDER  = a swing LOW (+ its recovery swing HIGH = the left neckline point);
        HEAD           = the NEXT swing LOW that prints a LOWER low than the left shoulder
                         (+ its recovery swing HIGH = the right neckline point);
        RIGHT SHOULDER = a THIRD swing LOW that HOLDS as a HIGHER low than the HEAD (a
                         higher-low hold — the down-pressure exhausting);
        NECKLINE       = ``min`` of the two shoulder highs (the resistance edge of the
                         two shoulders — Ross: "the edges of the two shoulders").

    Fire on the BREAK above the neckline (with a volume confirm), or on a live tick already
    trading through it. Entry = the neckline break level (``pullback_high``); stop = the
    HEAD low = the structural support of the pattern (``pullback_low``). The downstream
    vol-floor layer widens the stop (INVARIANT A) — this gate does NOT pre-floor it. The
    shared (ok, reason, debug) + pullback_high/pullback_low keys reuse the runner's stop /
    sizing / bailout machinery unchanged (NO new sizing path).

    DISTINCT from ``ross_double_bottom_confirmation`` (THREE pivot lows + a head-below-both-
    shoulders ordering + a right-shoulder-above-head HOLD vs. TWO equal lows; neckline =
    ``min`` of the two shoulder highs vs. the single intervening high). DISTINCT from
    ``ross_abcd_confirmation`` (the neckline is the shoulder highs, not a B-high; inverted
    pivot order). DISTINCT from ``first_pullback_break`` (multi-pivot structure vs. a single
    shallow retrace).

    NOISE DEFENSE: the ATR pivot filter (the three lows must be REAL pivots, not chop) + the
    head-below-both-shoulders ordering + the right-shoulder-above-head hold + a depth cap
    (``_collapse_cap``) on each shoulder retrace (a too-deep shoulder is a breakdown, not a
    pattern). Reuses ``_l2_entry_veto`` (hidden-seller / big-seller veto; fail-open on
    disabled/null L2) and ``_detect_back_side`` (fail-open).

    ADDITIVE: flag OFF / thin (<2·window+3 bars) / no clean inverse-H&S -> ``(False, reason,
    {...})`` with NO side effects; fail-OPEN to a benign decline on any error.
    docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "inverse_head_shoulders"}
    try:
        if not bool(getattr(settings, "chili_momentum_inverse_head_shoulders_entry_enabled", False)):
            return False, "inverse_head_shoulders_disabled", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 3):
            return False, "inverse_head_shoulders_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)
        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        debug["n_pivots"] = len(pivots)
        # The skeleton alternates H/L. We want the three most-recent swing LOWS
        # (left-shoulder, head, right-shoulder) and the two swing HIGHS BETWEEN them
        # (the neckline points). Find the last LOW = right shoulder, then walk back:
        # L(rs) <- H(right neck) <- L(head) <- H(left neck) <- L(ls).
        seq = pivots[:]  # chronological
        ri = None
        for k in range(len(seq) - 1, -1, -1):
            if seq[k]["kind"] == "L":
                ri = k
                break
        if ri is None or ri < 4:
            return False, "inverse_head_shoulders_too_few_pivots", debug
        rs_low = seq[ri]
        rn_high = seq[ri - 1] if seq[ri - 1]["kind"] == "H" else None
        head_low = seq[ri - 2] if seq[ri - 2]["kind"] == "L" else None
        ln_high = seq[ri - 3] if seq[ri - 3]["kind"] == "H" else None
        ls_low = seq[ri - 4] if seq[ri - 4]["kind"] == "L" else None
        if not (rs_low and rn_high and head_low and ln_high and ls_low):
            return False, "inverse_head_shoulders_skeleton_incomplete", debug

        ls_l = float(ls_low["price"])
        ln_h = float(ln_high["price"])
        head_l = float(head_low["price"])
        rn_h = float(rn_high["price"])
        rs_l = float(rs_low["price"])
        if not (ls_l > 0 and head_l > 0 and rs_l > 0 and ln_h > 0 and rn_h > 0):
            return False, "inverse_head_shoulders_bad_pivot", debug

        # ORDERING: the HEAD is the LOWEST low (below BOTH shoulders) and the RIGHT SHOULDER
        # HOLDS as a higher low than the head (the down-pressure exhausting). A small ATR-
        # derived tolerance keeps near-equal pivots from being rejected as chop.
        tol = (0.25 * float(atr_abs)) if (atr_abs is not None and atr_abs > 0) else (0.005 * head_l)
        if not (head_l < ls_l - tol and head_l < rs_l - tol):
            debug.update({"left_shoulder_low": round(ls_l, 6), "head_low": round(head_l, 6),
                          "right_shoulder_low": round(rs_l, 6)})
            return False, "inverse_head_shoulders_head_not_lowest", debug
        if rs_l < head_l - tol:
            debug.update({"head_low": round(head_l, 6), "right_shoulder_low": round(rs_l, 6)})
            return False, "inverse_head_shoulders_rs_not_hold", debug

        # NECKLINE = the MINIMUM of the two shoulder highs (Ross: the edges of the two
        # shoulders; the break of the LOWER of the two confirms first).
        neckline = min(ln_h, rn_h)
        # Depth guard: each shoulder retrace (neckline-point down to the shoulder low) must
        # be a PULLBACK, not a collapse — a too-deep shoulder is a breakdown, not structure.
        cap = _collapse_cap(atr_pct)
        ls_depth = (ln_h - ls_l) / ln_h if ln_h > 0 else 1.0
        rs_depth = (rn_h - rs_l) / rn_h if rn_h > 0 else 1.0
        debug.update({
            "left_shoulder_low": round(ls_l, 6), "left_neck_high": round(ln_h, 6),
            "head_low": round(head_l, 6), "right_neck_high": round(rn_h, 6),
            "right_shoulder_low": round(rs_l, 6), "neckline": round(neckline, 6),
            "ls_depth_pct": round(ls_depth * 100.0, 2),
            "rs_depth_pct": round(rs_depth * 100.0, 2),
            "ihs_collapse_cap_pct": round(cap * 100.0, 2),
        })
        if ls_depth > cap or rs_depth > cap:
            return False, "inverse_head_shoulders_shoulder_too_deep", debug

        # The break level = the neckline; stop = the HEAD low (structural support).
        level = neckline
        stop = head_l
        if not (0.0 < stop < level):
            return False, "inverse_head_shoulders_bad_level", debug
        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # Request ema_9 + volume_ratio (break surge) PLUS ema_20 / macd / macd_signal / vwap
        # so the NOT-BACKSIDE + NOT-PARABOLIC chase-guards below get REAL series —
        # compute_all_from_df only computes what is requested, so an un-requested ema_20/macd
        # would silently no-op _detect_back_side (a chase hole). Mirrors wedge_break / cup.
        arrays = compute_all_from_df(
            df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "volume_ratio"},
        )
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []

        # L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2).
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"inverse_head_shoulders_{_reason}", debug

        # ── NOT BACKSIDE / NOT BELOW-VWAP (the #1 chase guard — mirrors wedge / cup) ─────
        # Never fire a fresh long into a rolled-over, back-side-of-the-move tape — the pattern
        # only buys an exhaustion bottom turning up. _detect_back_side reads the 1m EMA/MACD
        # rollover (ARRAY signature, mirrors wedge/cup — NOT the old df+kwarg call that raised
        # TypeError + silently no-opped); front_side_state reads WHERE the name sits in its OWN
        # session (below VWAP / faded). The front_side read fails CLOSED on a thin frame.
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "inverse_head_shoulders_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "inverse_head_shoulders_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            # thin/degenerate frame -> fail-CLOSED for this new-conviction fire path.
            debug["reason"] = "front_side_read_error"
            return False, "inverse_head_shoulders_backside_lifecycle", debug

        # ── NOT PARABOLIC: extension vs the 9-EMA AND VWAP (the blow-off defense) ────────
        # The neckline break level (= the entry) must not sit excessively extended above the
        # 9-EMA / VWAP. Reuses the SAME adaptive ATR extension yardstick the chase veto uses
        # (fail-OPEN on a missing reference so thin data never blocks). Mirrors cup_and_handle.
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "inverse_head_shoulders_extended", debug

        # ── TRIGGER: the break above the neckline (the edges of the two shoulders) ───────
        # Compute WHICH fire path BEFORE the tape gate so TAPE REQUIRED applies to BOTH the
        # tick-break and the completed-bar break (mirrors cup_and_handle).
        cur_hi = float(high.iloc[cur])
        _tick_break = bool(
            live_price is not None and float(live_price) > 0 and float(live_price) > level
        )
        _bar_break = bool(cur_hi > level)
        if not (_tick_break or _bar_break):
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire without buyers on tape) ──
        # Mirrors wedge / absorption / cup: buyers must be actively lifting the ask THIS tick.
        # Any disabled-flag / no-tape / thin / stale / crypto / error ⇒ NO fire. Applies to
        # BOTH the tick-break and the completed-bar break.
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "inverse_head_shoulders_tape_unconfirmed", debug

        if _tick_break:
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "inverse_head_shoulders_break_tick_ok", debug
        # A completed bar broke the neckline -> ALSO require a VOLUME spike on the break bar.
        vr = arrays.get("volume_ratio") or []
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < _vol_mult:
            return False, "inverse_head_shoulders_low_volume", debug
        return True, "inverse_head_shoulders_break", debug
    except Exception:
        return False, "inverse_head_shoulders_error", {"entry_interval": entry_interval}


def cup_and_handle_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross CUP-AND-HANDLE entry (SS101 #016; flag ``chili_momentum_cup_and_handle_entry_enabled``).

    Ross (verbatim, SS101 #016): the cup-and-handle "is formed by a double top that then
    doesn't totally fail" -- the move up, a pullback, a second push to ~the same high (the
    DOUBLE-TOP that traces the rounded CUP), then a shallow pullback (the HANDLE), and we
    "buy here for this breakout, using the low of the handle as support" on "the first candle
    to make a new high, high volume surge". This gate encodes exactly that three-phase
    structure::

        CUP  = TWO swing HIGHS at ~the same level (within an ATR-derived band) over the
               recent ~15-20 bars -- the double-top rim (ATR-noise-filtered pivots, so chop
               wiggles are not mistaken for the rim);
        HANDLE = a SHALLOW pullback (1-3 completed bars) AFTER the second high, capped by the
               SAME vol-aware shallow tolerance ``first_pullback`` uses, holding above the
               9-EMA (vol-aware wick tolerance) -- Ross: "much better ... we're right at the
               nine moving average";
        BREAK = the first bar (or the live tick) to make a NEW HIGH above the double-top peak
               (the cup rim) with a volume surge (>= the shared spike multiple).

    Entry = the double-top peak / cup rim (``pullback_high``); stop = the HANDLE LOW
    (``pullback_low`` -- "using the low of the handle as support"; Ross notes the cup-bottom
    low is "way down here ... too far away"). The downstream vol-floor layer widens the stop
    (INVARIANT A) -- this gate does NOT pre-floor it. The shared (ok, reason, debug) +
    pullback_high/pullback_low keys reuse the runner's stop / sizing / bailout machinery
    unchanged (NO new sizing path).

    DISTINCT (parity reasoning, vs the existing live gates -- confirmed against the transcript
    + frames): ``first_pullback_break`` fires on ANY impulse + first shallow pullback with NO
    double-top prerequisite (Ross even says zoomed-in this "is just a bull flag"); cup-and-
    handle REQUIRES the two-top rim FIRST. ``ross_abcd_confirmation`` is a 4-swing A-B-C-D coil
    (a higher-LOW C hold); cup-and-handle is 2-tops + a handle, no C-low hold. ``ross_double_
    bottom_confirmation`` keys off two swing LOWS at support; this keys off two swing HIGHS at
    resistance. ``_evaluate_deep_reclaim`` is MORNING-only + allows deeper retraces; this is
    ALL-DAY + shallow-only. ``flat_top`` is a single level tested repeatedly; cup-and-handle is
    a rounded double-top followed by a SEPARATE handle phase. ``blue_sky_break`` keys off no
    overhead supply; this keys off the double-top + handle structure.

    ANTI-CHASE (parity with wedge_break_entry / hod_break_confirmation — the gatekeeper's
    chase-safety bar): the structural guards (the ATR pivot filter so the two tops are REAL
    pivots + the ATR-derived equal-highs band + the vol-aware shallow handle cap + the
    ``_collapse_cap`` depth gate + the 9-EMA hold) PLUS the FOUR shared chase-guards every
    other live breakout trigger carries:
      * NOT BACKSIDE / NOT BELOW-VWAP -- ``_detect_back_side`` (1m EMA/MACD rollover) AND
        ``front_side_state`` (folds in below-VWAP / faded; fails CLOSED on a thin frame);
      * NOT PARABOLIC -- ``_hod_extension_ok`` (the SAME adaptive ATR extension cap vs the
        9-EMA AND VWAP) so a vertical run INTO the rim is rejected as a blow-off;
      * L2 hidden-seller / big-seller veto (``_l2_entry_veto``; Ross watches L2 for "a big
        seller right around five");
      * TAPE REQUIRED + FAIL-CLOSED -- ``tape_confirms_hold`` is the LAST gate before EITHER
        fire path (tick-break OR completed bar): buyers must be lifting the ask THIS tick, and
        a disabled-flag / no-tape / thin / stale / crypto / error ⇒ NO fire.
    -- so it never fires on a vertical blow-off, a rolled-over backside, a below-VWAP fade, a
    hidden-seller wall, or a dead break with no buyers on tape. The STRUCTURAL STOP is the
    handle low (``pullback_low``; the vol-floor layer widens it downstream, INVARIANT A).
    ADDITIVE: flag OFF / thin (<2*window+3 bars) / no double-top / handle too deep / any veto
    -> ``(False, reason, {...})`` with NO side effects; fail-OPEN to a benign decline on any
    error. docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "cup_and_handle"}
    try:
        if not bool(getattr(settings, "chili_momentum_cup_and_handle_entry_enabled", False)):
            return False, "cup_and_handle_disabled", debug
        half_w = int(getattr(settings, "chili_momentum_swing_pivot_half_window", 2) or 2)
        if df is None or getattr(df, "empty", True) or len(df) < (2 * half_w + 3):
            return False, "cup_and_handle_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1
        atr_pct, atr_abs = _batch_c_atr_pct(df, close, cur)
        # Vol-aware shallow / EMA-wick tolerances (the SAME yardstick first_pullback uses) --
        # the handle is a calm-name Ross-floor shallow pull, widened proportional to ATR for
        # the explosive small-caps the lane selects. retracement base = the first-pullback base.
        eff_shallow, ema_wick, _ = _vol_aware_pullback_tolerances(atr_pct, 0.50)

        noise_frac = float(getattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5) or 0.0)
        pivots = _swing_pivots(
            high, low, half_window=half_w, atr_abs=atr_abs, atr_noise_frac=noise_frac,
        )
        debug["n_pivots"] = len(pivots)
        highs = [p for p in pivots if p["kind"] == "H"]
        if len(highs) < 2:
            return False, "cup_and_handle_too_few_highs", debug

        # CUP: the TWO most-recent swing HIGHS = the double-top rim.
        high1 = highs[-2]  # the first (left) top
        high2 = highs[-1]  # the second (right) top
        i1, i2 = int(high1["idx"]), int(high2["idx"])
        h1, h2 = float(high1["price"]), float(high2["price"])
        if not (h1 > 0 and h2 > 0):
            return False, "cup_and_handle_bad_high", debug
        # The two tops must be within ~15-20 bars of each other (a readable cup, not two
        # unrelated highs from different legs). Bars-apart ceiling derived from the lookback.
        cup_lookback = int(getattr(settings, "chili_momentum_cup_and_handle_lookback_bars", 20) or 20)
        if (i2 - i1) > cup_lookback:
            debug.update({"cup_bars_apart": i2 - i1, "cup_lookback": cup_lookback})
            return False, "cup_and_handle_tops_too_far", debug

        # EQUAL HIGHS: the two tops within an ATR-derived band (no fixed cents) -- reuse the
        # double-bottom band knob (the same "at the same level" ATR yardstick, applied to
        # resistance instead of support). When ATR is unavailable, a small relative band.
        band_mult = float(getattr(settings, "chili_momentum_double_bottom_band_atr_mult", 0.6) or 0.6)
        peak = max(h1, h2)  # the cup rim = the higher of the two tops (the level to break)
        ref = peak
        band = (band_mult * float(atr_abs)) if (atr_abs is not None and atr_abs > 0) else (0.01 * ref)
        debug.update({
            "high1": round(h1, 6), "high2": round(h2, 6),
            "cup_rim": round(peak, 6), "equal_band": round(band, 6),
            "cup_bars_apart": i2 - i1,
        })
        if abs(h1 - h2) > band:
            return False, "cup_and_handle_tops_unequal", debug

        # HANDLE: a SHALLOW pullback on the COMPLETED bars AFTER the second top.
        # The handle = the bars strictly after the right top up to (but excluding) the
        # current/breaking bar. 1-3 bars (the shallow, separate dip Ross calls the handle).
        max_handle = int(getattr(settings, "chili_momentum_cup_and_handle_max_handle_bars", 3) or 3)
        h_start = i2 + 1
        h_end = cur  # exclusive -- cur is the bar doing the breaking
        if h_end <= h_start:
            return False, "cup_and_handle_no_handle", debug
        # cap the handle to the most-recent ``max_handle`` completed bars before cur.
        h_start = max(h_start, h_end - max_handle)
        handle_low = float(low.iloc[h_start:h_end].min())
        handle_high = float(high.iloc[h_start:h_end].max())
        handle_bars = h_end - h_start
        if not (0.0 < handle_low < peak):
            return False, "cup_and_handle_bad_handle", debug
        debug.update({
            "handle_low": round(handle_low, 6),
            "handle_high": round(handle_high, 6),
            "handle_bars": handle_bars,
        })

        # DEPTH: the handle must be a SHALLOW pullback off the rim (reuse the first_pullback
        # yardsticks) -- beyond the vol-aware shallow cap / the _collapse_cap it is a breakdown,
        # not a handle. Measured off the cup rim (the resistance the handle pulls back from).
        depth = (peak - handle_low) / peak if peak > 0 else 1.0
        collapse_cap = _collapse_cap(atr_pct)
        debug.update({
            "handle_depth_pct": round(depth * 100.0, 2),
            "handle_shallow_cap": round(eff_shallow, 3),
            "handle_collapse_cap_pct": round(collapse_cap * 100.0, 2),
        })
        if depth > eff_shallow or depth > collapse_cap:
            return False, "cup_and_handle_handle_too_deep", debug

        # HOLD THE 9-EMA: the handle low must hold above the 9-EMA (vol-aware wick tolerance)
        # -- Ross: "much better ... we're right at the nine moving average" (an over-extended,
        # below-EMA handle is the risky one he warns against). Fail-OPEN on a missing EMA.
        # Request ema_9 + volume_ratio (handle-hold + break surge) PLUS ema_20 / macd /
        # macd_signal / vwap so the NOT-BACKSIDE + NOT-PARABOLIC chase-guards below get REAL
        # series -- compute_all_from_df only computes what is requested, so an un-requested
        # ema_20/macd would silently no-op _detect_back_side (a chase hole). Mirrors wedge_break.
        arrays = compute_all_from_df(
            df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "volume_ratio"},
        )
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        ema_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        if ema_cur is not None and handle_low < float(ema_cur) * (1.0 - ema_wick):
            debug["ema9"] = round(float(ema_cur), 6)
            return False, "cup_and_handle_handle_below_ema9", debug

        # The break level = the cup rim (the double-top peak); stop = the handle low.
        level = peak
        stop = handle_low
        if not (0.0 < stop < level):
            return False, "cup_and_handle_bad_level", debug
        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # ── NOT BACKSIDE / NOT BELOW-VWAP (the #1 chase guard — mirrors wedge / hod_break) ──
        # Never fire the rim break into a name that has rolled to the BACK side. _detect_back_
        # side reads the 1m EMA/MACD rollover; front_side_state reads WHERE the name sits in
        # its OWN session (below VWAP / faded — Ross never buys below VWAP). The front_side
        # read fails CLOSED on a thin/degenerate frame (this is a new-conviction fire path).
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "cup_and_handle_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "cup_and_handle_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            # thin/degenerate frame -> fail-CLOSED for this new-conviction fire path.
            debug["reason"] = "front_side_read_error"
            return False, "cup_and_handle_backside_lifecycle", debug

        # ── NOT PARABOLIC: extension vs the 9-EMA AND VWAP (the blow-off defense) ────────
        # The cup rim (= the entry) must not sit excessively extended above the 9-EMA / VWAP:
        # a vertical run INTO the rim is a parabolic blow-off, not a tested double-top break.
        # Reuses the SAME adaptive ATR extension yardstick the chase veto uses (fail-OPEN on a
        # missing reference so thin data never blocks).
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "cup_and_handle_extended", debug

        # L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2). Ross
        # explicitly watches L2 here for "a big seller right around five" before the break.
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"cup_and_handle_{_reason}", debug

        # ── A6 (clro-p1) TOPPING-TAIL ANTICIPATORY EARLY FIRE ──────────────────────────────
        # Ross's biggest challenge winner: "jumped in a little early to anticipate the break-
        # through … these were BOTH topping tails … got in as volume started to pick up down
        # here." When BOTH rim-high bars are topping tails (exhaustion wicks that will likely be
        # cleared on the next thrust), fire EARLY on a live uptick through the handle-low reclaim
        # level (handle_low x (1 + the SAME min_reclaim_bps base tick_scalp uses)) + the existing
        # volume-surge leg, BEFORE waiting for a full new-high above the rim. Stop UNCHANGED
        # (handle low). EVERY guard above (backside/front-side, extension, L2 seller veto) has
        # already run; the tape-required fail-closed gate runs below before this returns True.
        # FAIL-CLOSED on unreadable rim-bar wick geometry ⇒ no early path (rim-break only).
        if (
            bool(getattr(settings, "chili_momentum_cup_handle_anticipatory_enabled", False))
            and live_price is not None and float(live_price) > 0
        ):
            try:
                from .candles import is_topping_tail

                _open = df["Open"].astype(float)
                _o1, _hi1, _lo1, _c1 = float(_open.iloc[i1]), float(high.iloc[i1]), float(low.iloc[i1]), float(close.iloc[i1])
                _o2, _hi2, _lo2, _c2 = float(_open.iloc[i2]), float(high.iloc[i2]), float(low.iloc[i2]), float(close.iloc[i2])
                _both_topping = is_topping_tail(_o1, _hi1, _lo1, _c1) and is_topping_tail(_o2, _hi2, _lo2, _c2)
                debug["rim_both_topping_tails"] = bool(_both_topping)
            except Exception:
                _both_topping = False  # unreadable wick geometry ⇒ fail-closed to rim-break only
            if _both_topping:
                # reclaim level = handle_low x (1 + min_reclaim_bps) — the SAME base tick_scalp
                # uses (getattr default 8.0 bps); no new magic number.
                _reclaim_bps = float(getattr(settings, "chili_momentum_tick_first_pullback_min_reclaim_bps", 8.0) or 8.0)
                _reclaim_level = handle_low * (1.0 + max(0.0, _reclaim_bps) / 10_000.0)
                debug["anticipatory_reclaim_level"] = round(_reclaim_level, 6)
                debug["anticipatory_reclaim_bps"] = _reclaim_bps
                if float(live_price) > _reclaim_level:
                    # the existing volume-surge leg (the SAME break-bar surge the rim-break path
                    # requires) must also confirm — an early fire still needs volume coming in.
                    _vr = arrays.get("volume_ratio") or []
                    _vol_ratio = None
                    try:
                        _vol_ratio = float(_vr[cur]) if cur < len(_vr) and _vr[cur] is not None else None
                    except (TypeError, ValueError):
                        _vol_ratio = None
                    if _vol_ratio is None:
                        _w = vol.tail(21)
                        _avg = float(_w.iloc[:-1].mean()) if len(_w) > 1 else float(vol.iloc[-1])
                        _vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
                    _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
                    debug["anticipatory_vol_ratio"] = round(_vol_ratio, 2)
                    if _vol_ratio >= _vol_mult:
                        # TAPE REQUIRED + FAIL-CLOSED — the LAST gate, identical to the rim-break
                        # fire path: buyers must be actively lifting the ask THIS tick.
                        _atape_ok, _atape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
                        debug["anticipatory_tape_reason"] = _atape_dbg.get("reason")
                        if _atape_ok:
                            debug["anticipatory_topping_tail"] = True
                            debug["live_price"] = float(live_price)
                            return True, "cup_and_handle_anticipatory_topping_tail", debug
                        # tape unconfirmed ⇒ do NOT fire early; fall through to the rim-break path.

        # TRIGGER: the first NEW HIGH above the cup rim (the double-top peak) -- either the
        # live tick already trading through the rim (tick-break) OR a completed bar that broke
        # it. Compute WHICH before the tape gate so TAPE REQUIRED applies to BOTH fire paths.
        cur_hi = float(high.iloc[cur])
        _tick_break = bool(
            live_price is not None and float(live_price) > 0 and float(live_price) > level
        )
        _bar_break = bool(cur_hi > level)
        if not (_tick_break or _bar_break):
            return False, "waiting_for_break", debug  # tick-armable (pullback_high set)

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire without buyers on tape) ──
        # Mirrors wedge / absorption: buyers must be actively lifting the ask THIS tick
        # (signed_tape_accel > 0 AND tick_rate at/above its self-relative floor). Any disabled-
        # flag / no-tape / thin / stale / crypto / error ⇒ NO fire (never chase a dead break).
        # Applies to BOTH the tick-break and the completed-bar break.
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "cup_and_handle_tape_unconfirmed", debug

        if _tick_break:
            # TICK-BREAK: structure valid on completed bars + the live tick already trading
            # through the rim -> enter on that tick (mirrors the other Batch-C triggers; the
            # caller's tick-break block applies the thrust buffer via the WAIT reason).
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "cup_and_handle_break_tick_ok", debug
        # A completed bar broke the rim -> ALSO require a VOLUME spike on the break bar (Ross:
        # "high volume surge" on the first new-high candle).
        vr = arrays.get("volume_ratio") or []
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < _vol_mult:
            return False, "cup_and_handle_low_volume", debug
        return True, "cup_and_handle_break", debug
    except Exception:
        return False, "cup_and_handle_error", {"entry_interval": entry_interval}


def _explosive_raw_break_escape(
    symbol: str | None,
    *,
    vol_ratio: float | None,
    explosive_rvol_floor: float,
    db: Any = None,
    l2_as_of: Any = None,
    debug: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """ADAPTIVE-RETEST escape (the explosive raw-break Ross actually takes on the strongest
    setups). When ``chili_momentum_pullback_require_retest`` is True the trigger waits for a
    break→pullback→retest→reclaim sequence; a genuinely EXPLOSIVE vertical runner never offers
    that pullback. Ross's real behaviour is asymmetric: he takes the RAW first break on the
    STRONGEST setups (huge RVOL, the ask getting eaten) and waits for the retest on the weaker
    ones. This is the strongest-setups-only escape — it converts a ``waiting_for_retest`` into a
    raw-break FIRE *only* when the break is, by the tape, clearly explosive.

    EXPLOSIVE is defined STRICTLY from EXISTING adaptive floors (no new magic level):
      1. RVOL >= an ADAPTIVE explosive floor = ``explosive_floor_rvol`` (Ross's '5x') x ONE
         documented multiplier ``chili_momentum_pullback_raw_break_rvol_mult`` (>= 1.0, default
         1.0 — i.e. AT the explosive floor; the operator can raise it). The floor itself is the
         instrument-relative trigger-bar vol_ratio the gate already computed, so this is
         self-relative, not a fixed share count.
      2. The executed TAPE actively confirms aggressive buying ACCELERATING into the break —
         ``signed_tape_accel > 0`` (back-half aggressor-signed buy volume exceeds the front-half:
         the ask is getting eaten) AND ``tick_rate >= tick_rate_floor`` (recent activity at/above
         its OWN self-relative percentile floor) AND the thrust is STRONG: the back-half buy
         acceleration is a meaningful fraction of the window's signed buy volume (the thrust is a
         real surge, not a one-lot poke) — ``signed_tape_accel >= thrust_frac x back_half_buy_vol``
         where ``thrust_frac`` = ``chili_momentum_pullback_raw_break_thrust_frac`` (the SAME
         single documented base; default a strict 0.50 = the back half must out-buy the front by
         at least half the back-half buy volume).

    FAIL-CLOSED on the tape (the 'TAPE REQUIRED + fail-closed' chase-guard): no symbol / no db /
    crypto / empty-or-thin tape / any error ⇒ NO escape (``False``). The escape NEVER fires
    blind — without a live tick tape confirming the ask is being eaten, the retest discipline
    stays in force. Returns ``(escape, dbg_patch)`` where ``dbg_patch`` is merged into ``debug``
    for observability. PURE read (no writes). docs/DESIGN/MOMENTUM_LANE.md"""
    dbg: dict[str, Any] = {}
    try:
        # 1) ADAPTIVE explosive RVOL floor (derived from the existing explosive floor x ONE base).
        try:
            _rvol_mult = float(
                getattr(settings, "chili_momentum_pullback_raw_break_rvol_mult", 1.0) or 1.0
            )
        except (TypeError, ValueError):
            _rvol_mult = 1.0
        _rvol_mult = max(1.0, _rvol_mult)
        _explosive_floor = max(0.0, float(explosive_rvol_floor)) * _rvol_mult
        dbg["raw_break_rvol_floor"] = round(_explosive_floor, 3)
        if vol_ratio is None or float(vol_ratio) < _explosive_floor:
            dbg["raw_break_rvol"] = None if vol_ratio is None else round(float(vol_ratio), 3)
            dbg["raw_break_blocked"] = "rvol_below_explosive_floor"
            return False, dbg
        dbg["raw_break_rvol"] = round(float(vol_ratio), 3)

        # 2) TAPE thrust — REQUIRED + FAIL-CLOSED (no tape ⇒ no escape).
        tape = signed_tape_accel_features(symbol, db=db, as_of=l2_as_of)
        if tape is None:
            dbg["raw_break_blocked"] = "tape_required_fail_closed"
            return False, dbg
        accel = float(tape.get("signed_tape_accel", 0.0))
        tick_rate = float(tape.get("tick_rate", 0.0))
        tick_rate_floor = float(tape.get("tick_rate_floor", 0.0))
        dbg.update({
            "raw_break_signed_tape_accel": round(accel, 6),
            "raw_break_tick_rate": round(tick_rate, 4),
            "raw_break_tick_rate_floor": round(tick_rate_floor, 4),
            "raw_break_n_ticks": int(tape.get("n_ticks", 0)),
        })
        # ask-eaten + active: positive aggressor acceleration AND recent activity at/above floor.
        if not (accel > 0.0 and tick_rate >= tick_rate_floor):
            dbg["raw_break_blocked"] = "tape_not_confirming"
            return False, dbg
        # STRONG thrust: the back-half buy acceleration is a meaningful fraction of the window's
        # back-half buy volume (a real surge, not a single-lot poke). back_half_buy_vol is
        # reconstructed from the front_buy + accel identity isn't available here, so derive the
        # strength self-relatively against the signed-buy magnitude the tape exposes: require the
        # acceleration to clear thrust_frac of (accel + |front-half buy|) ≈ back-half buy volume.
        try:
            _thrust_frac = float(
                getattr(settings, "chili_momentum_pullback_raw_break_thrust_frac", 0.50) or 0.50
            )
        except (TypeError, ValueError):
            _thrust_frac = 0.50
        _thrust_frac = max(0.0, min(1.0, _thrust_frac))
        # back_half_buy = front_half_buy + signed_tape_accel; front_half_buy = back_half_buy - accel.
        # We only have accel (= back_buy - front_buy). The strongest-thrust read is accel relative
        # to the back-half buy volume; since back_buy >= accel (front_buy >= 0), accel/back_buy <= 1.
        # Without back_buy exposed we use the conservative self-relative proxy: the acceleration
        # must be POSITIVE and at least thrust_frac of itself-plus-the-prior-half — i.e. the back
        # half bought at least (1/(1-thrust_frac)) x more than the front half. Express directly:
        # accel >= thrust_frac/(1-thrust_frac) x front_buy. front_buy = back_buy - accel is unknown,
        # so fall back to requiring accel to clear a thrust_frac share of the tape's total signed
        # buy magnitude when exposed; else require strictly-positive accel (already checked) AND a
        # tick_rate strictly ABOVE the floor (a true surge, not merely at-floor).
        _back_buy = tape.get("back_half_buy_vol")
        _strong = True
        if _back_buy is not None:
            try:
                _bb = float(_back_buy)
                _strong = _bb > 0 and accel >= _thrust_frac * _bb
            except (TypeError, ValueError):
                _strong = True
        else:
            # No back-half buy volume exposed → demand a strictly-rising tape (tick_rate ABOVE
            # floor, not merely at it) as the strong-thrust proxy. Strictly-positive accel is
            # already required above; this raises the bar so an at-floor flat tape does not escape.
            _strong = tick_rate > tick_rate_floor
        dbg["raw_break_thrust_frac"] = round(_thrust_frac, 3)
        dbg["raw_break_strong_thrust"] = bool(_strong)
        if not _strong:
            dbg["raw_break_blocked"] = "thrust_not_strong"
            return False, dbg

        dbg["raw_break_explosive"] = True
        return True, dbg
    except Exception:
        dbg["raw_break_blocked"] = "raw_break_error_fail_closed"
        return False, dbg


def pullback_break_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str = "5m",
    max_pullback_bars: int = 3,
    retracement_threshold: float = 0.50,
    volume_spike_multiple: float = 1.5,
    require_retest: bool = False,
    retest_tolerance: float = 0.002,
    retest_lookback_bars: int = 4,
    require_sustained_volume: bool = False,
    sustained_rvol_floor: float = 1.0,
    sustain_lookback_bars: int = 5,
    require_break_candle: bool = False,
    break_candle_min_close_pos: float = 0.50,
    require_vwap_hold: bool = False,
    vwap_hold_buffer: float = 0.0,
    require_macd_bullish: bool = False,
    allow_runaway_break: bool = False,
    runaway_min_volume_spike: float = 2.0,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    first_pullback_interval: str | None = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross-style pullback-break entry on intraday (1m/5m) bars.

    After an up-impulse, a SHALLOW pullback (retraces < ``retracement_threshold`` of
    the recent range, holding above EMA-9), fire ENTRY when price breaks the
    pullback's high with a volume spike — Ross's low-risk continuation point, vs
    buying mid-trend extension. Returns ``(ok, reason, debug)``; ``debug`` carries
    ``pullback_low`` (the structural stop) and ``pullback_high`` (the breakout level,
    used by the breakout-or-bailout fast exit) on success.

    Two of Ross's RECENT (post-book) refinements are optional, documented knobs
    (defaults preserve the original first-break behavior; the live runner turns them
    on via settings):

    * ``require_retest`` (#1) — wait for break AND retest of the broken level instead
      of buying the raw first break (which wicks out and reverses).
    * ``require_sustained_volume`` (#3) — at the entry tick the move must STILL be
      carried by volume (recent rel-vol above ``sustained_rvol_floor``), rejecting a
      faded 24h mover that was hot at selection but dead by entry (the ESTR guardrail).

    docs/DESIGN/MOMENTUM_LANE.md
    """
    if df is None or getattr(df, "empty", True) or len(df) < 10:
        return False, "insufficient_bars", {"bars": 0 if df is None else len(df), "entry_interval": entry_interval}
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)
    # Open series for the Gate 2a distribution-candle veto (df ALWAYS carries Open;
    # fail-open to None if absent so a malformed df can never raise here).
    opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
    n = len(df)
    cur = n - 1
    arrays = compute_all_from_df(
        df, needed={"ema_9", "ema_20", "volume_ratio", "atr", "vwap", "macd", "macd_signal", "macd_hist"}
    )
    ema9 = arrays.get("ema_9") or []
    ema20 = arrays.get("ema_20") or []
    vr = arrays.get("volume_ratio") or []
    atr = arrays.get("atr") or []
    vwap = arrays.get("vwap") or []
    macd = arrays.get("macd") or []
    macd_sig = arrays.get("macd_signal") or []
    macd_hist = arrays.get("macd_hist") or []

    # Instrument volatility (ATR / price) drives the volatility-aware pullback
    # tolerances in the evaluators, so the explosive small-caps the lane selects
    # get room a flat threshold denied them. None on thin data -> Ross floors.
    atr_pct = None
    try:
        _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
        _p = float(close.iloc[cur])
        if _a is not None and _p > 0:
            atr_pct = _a / _p
    except (TypeError, ValueError, IndexError):
        atr_pct = None

    # Trigger: break-and-retest (#1) when enabled, else the classic first break.
    if require_retest:
        ok_t, reason_t, pb_high, pb_low, debug = _evaluate_break_retest(
            high, low, close, ema9, cur,
            entry_interval=entry_interval,
            max_pullback_bars=max_pullback_bars,
            retracement_threshold=retracement_threshold,
            retest_tolerance=retest_tolerance,
            retest_lookback_bars=retest_lookback_bars,
            atr_pct=atr_pct,
            symbol=symbol,
            vol=vol,
            vwap=vwap,
            opn=opn,
            db=db,
            l2_as_of=l2_as_of,
        )
    else:
        ok_t, reason_t, pb_high, pb_low, debug = _evaluate_raw_break(
            high, low, ema9, cur,
            entry_interval=entry_interval,
            max_pullback_bars=max_pullback_bars,
            retracement_threshold=retracement_threshold,
            atr_pct=atr_pct,
        )

    # FIRST-PULLBACK (Ross's EARLIEST, most aggressive entry: the first candle to make
    # a new high after the first shallow pullback off a confirmed impulse — JRSH +$21k).
    # ADDITIVE + flag-gated: tried alongside the retest/deep-reclaim ladder. A FIRE WINS
    # (it is the earlier, designed-for-this entry); an ARM sets the tick-watchable wait
    # reason + pb levels and falls into the EXISTING tick-break block (so the 1m thrust +
    # sustained-volume gate apply to the breakout tick); a PASS leaves the prior trigger
    # result BYTE-IDENTICAL. Flag OFF ⇒ this whole branch is skipped ⇒ byte-identical to
    # the current ladder. docs/DESIGN/MOMENTUM_LANE.md
    _fp_on = bool(getattr(settings, "chili_momentum_entry_first_pullback_enabled", True))
    # Timeframe guard: the first-pullback geometry (shallow pull -> immediate new high)
    # only reads on the base interval (1m); a 5m bar structurally collapses it. When a
    # first-pullback interval is specified and the entry df is on a DIFFERENT interval,
    # skip the gate (the live runner supplies a 1m df when it wants this entry) — this
    # keeps the 5m path byte-identical to before. None ⇒ no constraint (run on whatever
    # df is given), which is the path the unit tests + the 1m runner take.
    _fp_iv = first_pullback_interval if first_pullback_interval is not None else entry_interval
    # F2 (capture-g fix): with the micropull default flip the CONFIGURED first-pullback
    # interval is '15s', but the live runner FALLS BACK to the base-interval frame whenever
    # the micro build is unavailable (sparse tape, or the trade bridge's documented
    # silent-hang). A strict '15s' == '1m' match made Ross's EARLIEST entry silently vanish
    # on exactly the degraded path (and broke live-vs-replay parity —
    # counterfactual_replay passes fp_interval=entry_interval). The branch therefore ALSO
    # arms when (a) the configured first-pullback interval is SUB-MINUTE (the micro config
    # is what created the mismatch) AND (b) the entry frame is the base pullback interval
    # (the live runner's fallback frame by construction) — i.e. the fallback frame behaves
    # exactly as pre-flip. Non-micro mismatches (e.g. fp '1m' vs a 5m df) keep the original
    # skip contract byte-identical (test_flag_off_is_byte_identical).
    _fp_iv_s = str(_fp_iv).strip().lower()
    _fp_is_micro = _fp_iv_s.endswith("s") and not _fp_iv_s.endswith("ms")
    _fp_base_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
    if _fp_on and (
        str(_fp_iv) == str(entry_interval)
        or (_fp_is_micro and str(entry_interval) == _fp_base_iv)
    ):
        _fpv, _fp_high, _fp_low, _fp_dbg = first_pullback_break(
            df,
            symbol=symbol,
            max_pullback_bars=max_pullback_bars,
            retracement_threshold=retracement_threshold,
            db=db,
            l2_as_of=l2_as_of,
        )
        if _fpv == "FIRE":
            ok_t, reason_t, pb_high, pb_low, debug = (
                True, "first_pullback_break", _fp_high, _fp_low, _fp_dbg
            )
        elif _fpv == "ARM" and not ok_t:
            # Structure set, not broken on a completed bar yet → arm the tick-watch.
            # Carry the first-pullback marker + levels so the tick-break block routes
            # this through _dipbuy_tick_thrust_ok exactly like a dipbuy arm.
            reason_t = "waiting_for_first_pullback_break"
            debug = dict(debug)
            debug.update(_fp_dbg)
            debug["pullback_high"] = float(_fp_high)
            debug["pullback_low"] = float(_fp_low)

    # Runaway-break allowance: a break that RAN without offering a retest
    # (``waiting_for_retest``) — take the break itself rather than miss a vertical
    # runner that never comes back. STRICT, not the MRVL loosening: only the retest
    # WAIT is waived; it must still clear a RAISED volume floor (below) AND the
    # conviction-candle / VWAP / MACD confirmations. pb_high/pb_low are already the
    # broken level + structural stop. docs/DESIGN/MOMENTUM_LANE.md §8
    # (the break-retest evaluator returns None for pb_high/pb_low on a non-fire, but
    # the broken level + structural stop are carried in ``debug`` — read them there.)
    _runaway = False
    if (
        not ok_t
        and require_retest
        and allow_runaway_break
        and reason_t == "waiting_for_retest"
        and debug.get("pullback_high") is not None
        and debug.get("pullback_low") is not None
    ):
        ok_t, _runaway = True, True
        debug["runaway"] = True

    # ADAPTIVE-RETEST EXPLOSIVE RAW-BREAK ESCAPE (Ross's asymmetric rule: raw break on the
    # STRONGEST setups, retest on the rest). When require_retest is True and the break RAN with
    # NO pullback (``waiting_for_retest``), the retest ladder strands a genuinely explosive
    # vertical runaway. This GUARDED escape converts that wait into a raw-break FIRE *only* when
    # the break is, BY THE TAPE, clearly explosive: RVOL >= an ADAPTIVE explosive floor (derived
    # from ``explosive_floor_rvol`` x ONE documented base) AND the executed tape confirms
    # aggressive buying ACCELERATING into the break (signed_tape_accel > 0 = the ask getting
    # eaten, tick_rate >= floor, strong thrust) — TAPE REQUIRED + FAIL-CLOSED (no tape ⇒ no
    # escape, the retest discipline stays in force). It is a strongest-setups-only RAW BREAK, NOT
    # a chase: it sets ok_t and then runs the SAME 4 chase-guards every other fire runs (backside
    # EMA/MACD + front_side_state + VWAP-hold, extension/verticality veto, structural stop, and —
    # via the runaway weak-prior set below — the RAISED volume floor + fail-CLOSED sustained
    # volume). Flag OFF ⇒ this whole block is skipped ⇒ BYTE-IDENTICAL to the current ladder.
    # ONE adaptive base (rvol_mult x explosive_floor); no magic level. docs/DESIGN/MOMENTUM_LANE.md
    _explosive_raw_break = False
    if (
        not ok_t
        and require_retest
        and reason_t == "waiting_for_retest"
        and debug.get("pullback_high") is not None
        and debug.get("pullback_low") is not None
        and bool(getattr(settings, "chili_momentum_pullback_raw_break_when_explosive", False))
    ):
        # Trigger-bar RVOL (the SAME self-relative vol_ratio the explosive-floor gate uses below).
        _erb_vr = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        if _erb_vr is None:
            _erb_w = vol.tail(21)
            _erb_avg = float(_erb_w.iloc[:-1].mean()) if len(_erb_w) > 1 else float(vol.iloc[-1])
            _erb_vr = (float(vol.iloc[-1]) / _erb_avg) if _erb_avg > 0 else 0.0
        try:
            _erb_floor = float(getattr(settings, "chili_momentum_explosive_floor_rvol", 5.0) or 5.0)
        except (TypeError, ValueError):
            _erb_floor = 5.0
        _escape, _escape_dbg = _explosive_raw_break_escape(
            symbol,
            vol_ratio=_erb_vr,
            explosive_rvol_floor=_erb_floor,
            db=db,
            l2_as_of=l2_as_of,
            debug=debug,
        )
        debug.update(_escape_dbg)
        if _escape:
            ok_t, _explosive_raw_break = True, True
            debug["explosive_raw_break"] = True

    # FIX C(1): EXPLOSIVE RAW FIRST-PUSH. The escape above only rescues a break that ALREADY
    # printed and ran without a retest (``waiting_for_retest``). But the require_retest ladder also
    # strands the EXPLOSIVE tier ONE step earlier — at ``waiting_for_break`` — because the stable
    # retest level is anchored ``retest_lookback_bars`` back, so the first completed-bar break of
    # the (nearer) raw pullback high is NOT yet a "break" of that older level. For a genuinely
    # explosive low-float runner (NVCT/FISN/SKYQ class in the w0av0u3qy study) the retest never
    # comes; the name simply runs. Ross's asymmetric rule takes the RAW first break on the
    # STRONGEST setups. When this flag is ON and the name is, BY THE TAPE, clearly explosive (the
    # SAME _explosive_raw_break_escape gate: RVOL >= adaptive explosive floor AND tape-confirmed
    # aggressive buying — TAPE REQUIRED + FAIL-CLOSED), re-evaluate the trigger as a RAW first break
    # (the require_retest=False semantics for THIS tier only) so it fires the instant a COMPLETED
    # bar crosses the (nearer) pullback high. Crucially this is NOT a chase: it only sets ok_t +
    # adopts the raw-break pb_high/pb_low, then runs the SAME 4 chase-guards every other fire runs
    # (backside EMA/MACD + front_side_state + VWAP-hold, extension/verticality, structural stop, and
    # — via the _explosive_raw_break weak-prior set below — the RAISED runaway volume floor +
    # fail-CLOSED sustained volume). Flag OFF ⇒ skipped ⇒ BYTE-IDENTICAL. docs/DESIGN/MOMENTUM_LANE.md
    _explosive_raw_first_push = False
    if (
        not ok_t
        and require_retest
        and reason_t == "waiting_for_break"
        and bool(getattr(settings, "chili_momentum_explosive_raw_break_enabled", True))
    ):
        # Trigger-bar RVOL (the SAME self-relative vol_ratio the explosive-floor gate uses).
        _rfp_vr = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        if _rfp_vr is None:
            _rfp_w = vol.tail(21)
            _rfp_avg = float(_rfp_w.iloc[:-1].mean()) if len(_rfp_w) > 1 else float(vol.iloc[-1])
            _rfp_vr = (float(vol.iloc[-1]) / _rfp_avg) if _rfp_avg > 0 else 0.0
        try:
            _rfp_floor = float(getattr(settings, "chili_momentum_explosive_floor_rvol", 5.0) or 5.0)
        except (TypeError, ValueError):
            _rfp_floor = 5.0
        # TAPE REQUIRED + FAIL-CLOSED — reuse the EXACT explosive-tape gate the retest escape uses,
        # so the first-push tier can never fire blind (no tape ⇒ no escape ⇒ retest discipline holds).
        _rfp_escape, _rfp_escape_dbg = _explosive_raw_break_escape(
            symbol,
            vol_ratio=_rfp_vr,
            explosive_rvol_floor=_rfp_floor,
            db=db,
            l2_as_of=l2_as_of,
            debug=debug,
        )
        debug.update(_rfp_escape_dbg)
        if _rfp_escape:
            # Re-evaluate as the RAW first break (require_retest=False semantics for THIS tier only):
            # fires ``raw_break`` the instant the current COMPLETED bar's high crosses the nearer
            # pullback high, with NO retest wait.
            _rfp_ok, _rfp_reason, _rfp_pbh, _rfp_pbl, _rfp_dbg = _evaluate_raw_break(
                high, low, ema9, cur,
                entry_interval=entry_interval,
                max_pullback_bars=max_pullback_bars,
                retracement_threshold=retracement_threshold,
                atr_pct=atr_pct,
            )
            if _rfp_ok and _rfp_pbh is not None and _rfp_pbl is not None:
                # Adopt the raw-break levels; treat as the explosive raw-break weak-prior set so the
                # RAISED runaway volume floor + fail-CLOSED sustained-volume apply downstream.
                ok_t = True
                _explosive_raw_first_push = True
                _explosive_raw_break = True
                pb_high, pb_low = _rfp_pbh, _rfp_pbl
                debug.update(_rfp_dbg)
                debug["pullback_high"] = float(_rfp_pbh)
                debug["pullback_low"] = float(_rfp_pbl)
                debug["explosive_raw_first_push"] = True
            else:
                # Tape said explosive but the completed bar has not crossed the raw pullback high
                # yet — leave the wait reason intact (the tick-break block below can still arm).
                debug["explosive_raw_first_push_armed"] = True

    # TICK-BREAK (Ross-speed entry): the structure is valid on COMPLETED bars but
    # the break hasn't printed on a CLOSED bar yet — and the LIVE tick is already
    # trading through the level. Ross enters on that tick ("first candle to make a
    # new high"), not a bar-close later. The forming bar's candle quality and
    # volume spike are unknowable mid-bar, so this path leans on (a) the sustained
    # rel-vol of the COMPLETED bars below, (b) VWAP/MACD confirmations, and (c)
    # the breakout-or-bailout fast exit (pullback_high is stashed as the level),
    # which is exactly Ross's own protection for a tick entry that wicks out.
    _tick_break = False
    if (
        not ok_t
        and live_price is not None
        and float(live_price) > 0
        and reason_t in TICK_ARMED_WAIT_REASONS
        and debug.get("pullback_high") is not None
        and debug.get("pullback_low") is not None
        and float(live_price) > float(debug["pullback_high"])
    ):
        # Premarket false-pop guard (CUPR): a thin-tape wick poking 1¢ through the
        # level is a shake-out entry (4.07 on a failed pop → −15% stop → THEN +92%).
        # In premarket require an ATR thrust buffer; RTH + crypto are unchanged.
        _confirmed = _premarket_tickbreak_confirmed(
            live_price=float(live_price), level=float(debug["pullback_high"]),
            atr_pct=atr_pct, symbol=symbol, now=now,
        )
        _unconf = "premarket_tickbreak_unconfirmed"
        # DIP-BUY (the dip bar's OWN high) and FIRST-PULLBACK (the pullback swing high)
        # both arm on a TIGHT level — the tightest the lane arms. Require an ATR/floor
        # thrust buffer in EVERY session (RTH + crypto too, not just premarket) so a
        # 1-tick poke of that tight level is not a dead-cat entry.
        if _confirmed and reason_t in (
            "waiting_for_dipbuy_break", "waiting_for_first_pullback_break"
        ) and not _dipbuy_tick_thrust_ok(
            live_price=float(live_price), level=float(debug["pullback_high"]), atr_pct=atr_pct,
        ):
            _confirmed, _unconf = False, (
                "dipbuy_tickbreak_unconfirmed"
                if reason_t == "waiting_for_dipbuy_break"
                else "first_pullback_tickbreak_unconfirmed"
            )
        if _confirmed:
            ok_t, _tick_break = True, True
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
        else:
            debug[_unconf] = True
            reason_t = _unconf

    if not ok_t:
        return False, reason_t, debug

    # A deep-retrace reclaim (whether bar-fired or tick-fired) has a weaker
    # structural prior than a clean shallow flag — the gates below treat it like
    # a runaway: raised volume floor + fail-CLOSED sustained-volume.
    _deep_reclaim = debug.get("pattern") == "deep_reclaim"

    # FRONT/BACK-SIDE veto (Ross gap #1): once the move rolls to the back side
    # (9<20-EMA, or MACD crossed below signal and still below) Ross STOPS taking
    # continuation entries — it makes lower highs / distributes. CHILI's point-in-time
    # MACD-bullish gate can't see the rollover, so it can re-arm a faded name every
    # tick; the back-side state is itself persistent, so this point-in-time decline
    # gives the sticky "benched for the move" semantics. EXEMPT the deep-reclaim/dip-buy
    # reversal path — that mode intentionally catches the turn off a dip and carries its
    # own dip-vs-dump discipline (#734). Always-on (no dark flags); fail-open on thin
    # data so it can never veto on warmup; reversible by reverting the sha.
    if not _deep_reclaim:
        _bs, _bs_reason = _detect_back_side(
            ema9, ema20, macd, macd_sig, cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "back_side_disabled", debug

    # E1: SESSION-ANCHORED backside veto (Ross gap #1, build_order #1). The point-in-time
    # _detect_back_side above reads the 1m EMA/MACD ROLLOVER; front_side_state reads WHERE
    # the name sits in its OWN session today — below VWAP / faded past the retrace veto /
    # chasing an extended blow-off top (the QXL chase) — which the EMA cross cannot see.
    # CHILI computes front_side_state (#798) but it was UNWIRED in the entry path; this
    # wires it as a HARD veto. ADDITIVE: fires only when the state AFFIRMATIVELY reads
    # backside; front-side / unknown / thin data -> NO change (front_side_state fails OPEN
    # to is_backside=False). EXEMPT the deep-reclaim/dip-buy reversal path (same carve-out
    # as the MACD/EMA gate — that mode intentionally catches the turn off a dip). Reuses the
    # session OHLCV frame already passed in; flag OFF -> the whole block is skipped ->
    # byte-identical. docs/STRATEGY/CC_REPORTS/2026-06-24_ross-course-study.md
    if not _deep_reclaim and bool(
        getattr(settings, "chili_momentum_backside_veto_enabled", True)
    ):
        try:
            from .ross_momentum import front_side_state

            _sess = _today_session_frame(df)
            _fs = front_side_state(_sess)
            if getattr(_fs, "is_backside", False):
                # LIVE-TICK NEW-HIGH carve-out for chasing_top ONLY. front_side_state reads
                # COMPLETED bars; on a tick-break entry the live_price can be breaking to a
                # NEW high ABOVE the completed-bar HOD. The chasing_top read keys off an
                # OFF-THE-HIGH (rolled-over) structure in the completed bars — but a live tick
                # making a fresh high IS the new high, so that rolled-over read is stale and
                # the name is front-side RIGHT NOW. Skip the veto in that exact case (mirrors
                # front_side_state's own 'fresh HOD is never chasing_top' rule, extended to the
                # live tick the frame can't see). below_vwap / already_faded stay HARD vetoes —
                # a live tick over the bar-HOD does not undo being below VWAP or deeply faded.
                _reason = getattr(_fs, "reason", "backside")
                _live_new_high = False
                if _reason == "chasing_top" and live_price is not None:
                    try:
                        _frame_hod = float(_sess["High"].astype(float).max())
                        _live_new_high = float(live_price) > _frame_hod
                    except (TypeError, ValueError, KeyError):
                        _live_new_high = False
                if not _live_new_high:
                    debug["front_side_state"] = _reason
                    debug["front_side_score"] = getattr(_fs, "front_side_score", None)
                    return False, "backside_lifecycle_veto", debug
                debug["front_side_state_live_new_high"] = _reason
        except (TypeError, ValueError, AttributeError, KeyError):
            pass  # thin/degenerate frame or other error -> fail-open (never block on a bug)

    # ── CANDLE-QUALITY + MULTI-TF (HTF-against) ENTRY VETO (flag-gated, default OFF) ──
    # Two ADDITIVE entry-quality gates, slotted AFTER the trigger fires + the backside vetoes
    # and BEFORE the downstream VWAP/MACD/volume confirmations (validate the trigger candle's
    # quality + the HTF context before the heavier checks). Flag OFF -> the whole block is
    # skipped -> BYTE-IDENTICAL.
    #
    # EXEMPT the deep-reclaim/dip-buy reversal path (same `if not _deep_reclaim` carve-out the
    # backside gates above use): that mode INTENTIONALLY catches the turn off a dip, so it
    # expects a lagging HTF (a slow 5m EMA still leaning down off the flush) and an indecision
    # bar at the very bottom — exactly the shapes these two gates veto. Without this carve-out
    # the HTF/doji gates kill the dip-rip / VWAP-reclaim. The deep-reclaim path carries its own
    # dip-vs-dump discipline (#734). docs/DESIGN/MOMENTUM_LANE.md
    if not _deep_reclaim and bool(
        getattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    ):
        # (6) DOJI VETO — the TRIGGER candle is a true doji (weak body relative to range =
        # indecision). Skip on a TICK-break (the breaking bar is still FORMING mid-bar — its
        # body/wick are unknowable, exactly as the conviction-candle gate skips it). A strong
        # full-body commitment candle passes (handled inside _doji_trigger_veto). ATR-adaptive
        # band (ONE documented base, widened by atr_pct); fail-safe on a zero-range bar.
        if not _tick_break:
            try:
                _dj_o = float(df["Open"].iloc[cur])
                _dj_h, _dj_l, _dj_c = (
                    float(high.iloc[cur]), float(low.iloc[cur]), float(close.iloc[cur])
                )
                _dj_base = float(getattr(settings, "chili_momentum_doji_body_frac", 0.25))
                _dj_veto, _dj_dbg = _doji_trigger_veto(
                    _dj_o, _dj_h, _dj_l, _dj_c, atr_pct=atr_pct, base_body_frac=_dj_base,
                )
                if _dj_dbg:
                    debug["doji"] = _dj_dbg
                if _dj_veto:
                    return False, "doji_trigger_veto", debug
            except (TypeError, ValueError, IndexError, KeyError):
                pass  # unreadable trigger bar -> fail-open (never block on a bug)

        # (7) HTF-AGAINST VETO — the higher TF (5m, resampled from the 1m df, NO new feed) is
        # CLEARLY bearish (5m EMA-9 in a SUSTAINED roll-down or MACD clearly peaked). A
        # NEUTRAL/LAGGING HTF (not yet up but not down) — including a single lagging EMA
        # down-tick — MUST still pass -> the 1m-FAST geometry is preserved (the 1m leads, the
        # HTF lags; requiring full alignment would break Ross's method). Applies to tick-breaks
        # too (it reads COMPLETED HTF bars, independent of the forming 1m bar). Fail-OPEN on
        # non-datetime index / thin HTF (a missing feed never blocks).
        try:
            _htf_thresh = float(getattr(settings, "chili_momentum_htf_against_macd_threshold", 0.0))
            _htf_bars = int(getattr(settings, "chili_momentum_htf_against_ema9_rolldown_bars", 3))
            _htf_veto, _htf_dbg = _htf_against_veto(
                df, rule="5min", macd_threshold=_htf_thresh, rolldown_bars=_htf_bars,
            )
            if _htf_dbg:
                debug["htf"] = _htf_dbg
            if _htf_veto:
                return False, "htf_against_veto", debug
        except (TypeError, ValueError, AttributeError):
            pass  # thin / non-datetime frame -> fail-open (1m-fast preserved)

    # Volume spike on the trigger (break / reclaim) bar.
    # F1 (capture-g fix): NEVER manufacture a concrete 0.0 from a missing/all-NaN volume
    # frame. The 15s micro frame carries NaN volume wherever the trade tape doesn't cover a
    # bucket (volume UNKNOWN); the old fallback turned an all-NaN/zero-avg frame into
    # vol_ratio=0.0 — a concrete "dead bar" that failed EVERY volume gate below CLOSED
    # (break_low_volume / faded_volume_no_sustain / the E3 rvol floor), the exact opposite
    # of the documented missing-volume fail-OPEN intent. Now: unknown stays None and each
    # volume gate below skips (fail-OPEN) on None; a genuine 0-volume bar INSIDE trade-tape
    # coverage still computes a real 0.0 ratio via vr and still blocks (fail-closed kept).
    vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
    if vol_ratio is not None and vol_ratio != vol_ratio:  # NaN -> UNKNOWN
        vol_ratio = None
    if vol_ratio is None:
        try:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            _last_v = float(vol.iloc[-1])
            if _avg == _avg and _avg > 0 and _last_v == _last_v:
                vol_ratio = _last_v / _avg
            # else: volume UNKNOWN (all-NaN frame / zero average) -> stays None (fail-OPEN)
        except (TypeError, ValueError, IndexError):
            vol_ratio = None
    debug["vol_ratio"] = None if vol_ratio is None else round(vol_ratio, 2)

    # RED-VOLUME EXHAUSTION VETO (AS101/HVM101 "first sign of weakness"). A trigger bar
    # that closes RED (close<open) WHILE printing the session's MAX volume AND a NEW
    # session HIGH is a climactic high-volume-red exhaustion top — the breakout bar is
    # the blow-off, not a continuation. Buying it is buying the top into distribution.
    # VETO (protective; can never create a bad fill). Self-relative — the volume bar is
    # judged against the session's OWN max (no fixed share count) and "new high" against
    # the session's OWN prior high (no magic level). Skip on a TICK-break (the breaking
    # bar is still FORMING — its close/volume are unknowable mid-bar, exactly as the
    # conviction-candle gate skips it) and EXEMPT the deep-reclaim/dip-buy reversal path
    # (that mode intentionally catches the turn off a dip and carries its own discipline).
    # ADDITIVE: flag OFF / thin frame -> the block is skipped -> byte-identical. Fail-OPEN
    # on any missing data or error (never block a valid break on a bug).
    if (
        not _tick_break
        and debug.get("pattern") != "deep_reclaim"
        and bool(getattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", True))
    ):
        try:
            _opn_v = opn.values if (opn is not None and hasattr(opn, "values")) else None
            _vol_v = vol.values if hasattr(vol, "values") else None
            _cl_v = close.values if hasattr(close, "values") else None
            _hi_v = high.values if hasattr(high, "values") else None
            if (
                _opn_v is not None and _vol_v is not None and _cl_v is not None
                and _hi_v is not None and cur < len(_opn_v) and cur < len(_vol_v)
                and cur < len(_cl_v) and cur < len(_hi_v)
            ):
                _o, _c = float(_opn_v[cur]), float(_cl_v[cur])
                _v, _h = float(_vol_v[cur]), float(_hi_v[cur])
                # NaN-safe: any missing OHLCV on the trigger bar -> fail-open (no veto).
                if _o == _o and _c == _c and _v == _v and _h == _h:
                    _is_red = _c < _o
                    _sess_max_vol = float(_vol_v[: cur + 1].max())
                    _is_max_vol = _sess_max_vol > 0 and _v >= _sess_max_vol
                    _prior_high = (
                        float(_hi_v[:cur].max()) if cur >= 1 else 0.0
                    )
                    _is_new_high = _h > _prior_high
                    if _is_red and _is_max_vol and _is_new_high:
                        debug["red_vol_exhaustion"] = {
                            "o": round(_o, 6), "c": round(_c, 6),
                            "vol": _v, "sess_max_vol": _sess_max_vol,
                            "high": round(_h, 6), "prior_high": round(_prior_high, 6),
                        }
                        return False, "red_vol_exhaustion_veto", debug
        except (TypeError, ValueError, IndexError, AttributeError):
            pass  # thin / malformed frame -> fail-open (never block on a bug)

    # E3: EXPLOSIVE-FLOOR HARD GATE (Ross gap #3, build_order #2). Selection ranks names
    # by within-batch PERCENTILE, so on a dull tape the best-of-a-dull-batch ranks #1 and
    # arms a slow mover Ross would not touch. Ross's floors are ABSOLUTE: RVOL >= ~5x AND
    # day-change >= ~10%. ADD that hard floor at the entry tick (the OHLCV frame already
    # carries today's vol-ratio + session open), on TOP of the percentile rank. EQUITY-only
    # (crypto 24h RVOL/change semantics differ and get their own calibration — symbols are
    # bare tickers here, crypto carries '-USD'). Fail-OPEN on missing data and flag-OFF ->
    # the whole block is skipped -> byte-identical. This RAISES the floor for the weak-batch
    # case; the genuine explosive tail (50x / +200%) clears it trivially.
    # docs/STRATEGY/CC_REPORTS/2026-06-24_ross-course-study.md
    _is_crypto = bool(symbol) and str(symbol).upper().endswith("-USD")
    if (
        not _is_crypto
        and bool(getattr(settings, "chili_momentum_explosive_floor_enabled", True))
    ):
        try:
            _rvol_floor = float(getattr(settings, "chili_momentum_explosive_floor_rvol", 5.0))
            _change_floor = float(
                getattr(settings, "chili_momentum_explosive_floor_change_pct", 10.0)
            )
            # RVOL: reuse the trigger-bar vol_ratio already computed above.
            if vol_ratio is not None and float(vol_ratio) < _rvol_floor:
                debug["explosive_floor_rvol"] = round(float(vol_ratio), 2)
                debug["explosive_floor_rvol_required"] = _rvol_floor
                return False, "below_explosive_floor_rvol", debug
            # Day-change %: session open (first bar) -> current close. Fail-open if the
            # frame is empty or the open is non-positive.
            # F1 (capture-g fix): the day-change floor needs SESSION-OPEN context. A
            # sub-minute MICRO frame (entry_interval like '15s') spans only the recent
            # lookback window (~30 min), so its first bar is NOT the session open —
            # computing "day change" from it misreads a 30-min window as the whole day
            # and would block every micro-frame break. Skip the day-change leg on a
            # sub-minute frame (fail-open); the 1m/5m day frames keep the check.
            _iv_str = str(entry_interval or "").strip().lower()
            _is_micro_frame = _iv_str.endswith("s") and not _iv_str.endswith("ms")
            if len(df) >= 1 and not _is_micro_frame:
                _sess_open = float(df["Open"].iloc[0])
                if _sess_open > 0:
                    _daily_change_pct = (float(close.iloc[cur]) - _sess_open) / _sess_open * 100.0
                    if _daily_change_pct < _change_floor:
                        debug["explosive_floor_change_pct"] = round(_daily_change_pct, 2)
                        debug["explosive_floor_change_pct_required"] = _change_floor
                        return False, "below_explosive_floor_change", debug
        except (TypeError, ValueError, AttributeError, IndexError, KeyError):
            pass  # thin data / malformed frame -> fail-open (never block on a bug)

    # Pullback ordinal (Ross gap #7): a 3rd+ pullback break is a weaker prior — Ross's
    # 1st/2nd are A-setups, the 3rd is greedy and usually fails. Count the recent
    # band-losing dips; a late pullback joins the runaway/deep-reclaim weaker-prior set
    # below (raised volume floor) so only a strongly-confirmed 3rd still fires. No-op /
    # byte-identical for the 1st-2nd pullback (the common case). Reuses the vol-aware band.
    _, _ord_wick, _ = _vol_aware_pullback_tolerances(atr_pct, retracement_threshold)
    _pb_ordinal = pullback_ordinal_recent(low.values, ema9, cur, _ord_wick)
    _late_pullback = _pb_ordinal >= _LATE_PULLBACK_ORDINAL
    if _late_pullback:
        debug["pullback_ordinal"] = _pb_ordinal
    # Runaways need MORE conviction (chasing a break without a retest): raise the
    # volume floor to runaway_min_volume_spike for them; same for deep reclaims
    # (buying without the classic shallow flag) and 3rd+ pullbacks. Normal breaks keep
    # the base. Tick-breaks skip the per-bar spike check (the breaking bar is still
    # forming — its volume is unknowable); the sustained-volume gate below still applies.
    _vol_floor = (
        float(runaway_min_volume_spike)
        if (_runaway or _explosive_raw_break or _deep_reclaim or _late_pullback)
        else float(volume_spike_multiple)
    )
    # FIX C(2): RVOL-RELATIVE break-volume floor (kills the fixed 1.5x/2.0x magic). The fixed floor
    # above held a +400%-RVOL explosive name to the SAME trigger-bar relative-volume bar as a +20%
    # name — the w0av0u3qy study saw break_low_volume reject 529 explosive breaks. When the flag is
    # ON, scale the floor DOWN as the name's OWN trigger-bar RVOL rises above the explosive floor:
    # a name running R x the explosive RVOL floor needs only floor x clamp(explosive_floor/R, ratio,
    # 1.0). At ratio=0.25 a name >=4x the explosive floor needs only 25% of the base bar; a name AT
    # the floor still needs the full base; the floor NEVER drops below a documented absolute minimum
    # (a hyper-explosive name still needs a real green volume bar, not a one-lot poke). Self-relative,
    # no magic share count. Flag OFF ⇒ _vol_floor unchanged ⇒ byte-identical. docs/DESIGN/MOMENTUM_LANE.md
    if (
        not _tick_break
        and bool(getattr(settings, "chili_momentum_break_volume_rvol_relative", True))
        and vol_ratio is not None
    ):
        try:
            _bv_ratio = float(getattr(settings, "chili_momentum_break_volume_rvol_ratio", 0.25) or 0.25)
        except (TypeError, ValueError):
            _bv_ratio = 0.25
        _bv_ratio = max(0.0, min(1.0, _bv_ratio))
        try:
            _bv_min = float(getattr(settings, "chili_momentum_break_volume_rvol_min_floor", 1.0) or 0.0)
        except (TypeError, ValueError):
            _bv_min = 1.0
        try:
            _bv_explosive_floor = float(getattr(settings, "chili_momentum_explosive_floor_rvol", 5.0) or 0.0)
        except (TypeError, ValueError):
            _bv_explosive_floor = 5.0
        # Scale factor in [ratio, 1.0]: 1.0 when the name is AT/below the explosive floor (no relief),
        # shrinking toward `ratio` as the name's RVOL exceeds it. clamp keeps it bounded both ways.
        if _bv_explosive_floor > 0 and float(vol_ratio) > 0:
            _bv_scale = _bv_explosive_floor / float(vol_ratio)
        else:
            _bv_scale = 1.0
        _bv_scale = max(_bv_ratio, min(1.0, _bv_scale))
        _bv_relaxed = max(_bv_min, _vol_floor * _bv_scale)
        # Only ever RELAX (never tighten) the per-bar floor — a misconfigured min/ratio must not
        # raise the bar above the fixed multiple the operator already accepted.
        if _bv_relaxed < _vol_floor:
            debug["break_volume_rvol_relative"] = {
                "base_floor": round(_vol_floor, 3),
                "rvol": round(float(vol_ratio), 3),
                "explosive_floor": round(_bv_explosive_floor, 3),
                "scale": round(_bv_scale, 4),
                "min_floor": round(_bv_min, 3),
                "effective_floor": round(_bv_relaxed, 3),
            }
            _vol_floor = _bv_relaxed
    # F1: vol_ratio None = volume UNKNOWN (micro frame outside trade-tape coverage) -> the
    # per-bar spike gate fails OPEN (documented); a real computed ratio still gates as before.
    if not _tick_break and vol_ratio is not None and vol_ratio < _vol_floor:
        return False, "break_low_volume", debug

    # #3 Sustaining-volume gate (the ESTR guardrail): the move must STILL be carried
    # by volume at the entry tick — recent rel-vol above the floor — so a faded 24h
    # mover (hot at selection, dead by entry) is rejected. Self-relative per
    # instrument, so the floor is adaptive (a FLOOR the system can raise), not a
    # fixed magic count. Fails OPEN on thin data — EXCEPT for a deep reclaim:
    # a deep-retrace bounce with unknowable volume support is the textbook
    # dead-cat trap, so that one path fails CLOSED.
    if require_sustained_volume:
        # F1 (capture-g fix, acceptance follow-through): the sustain window is a WALL-CLOCK
        # design — N bars of the BASE entry interval span the impulse+pullback (the "faded
        # 24h mover" read needs ~minutes of context). The lookback is a BAR COUNT, so on the
        # 15s micro frame 5 buckets = only 75 SECONDS — i.e. exactly the quiet pullback
        # itself — and EVERY pullback break read as "faded" by construction (SVRE 06-30
        # 12:45:46Z tick-break at Ross's 6.88: mean 0.78 over 75s, vs the designed 5-minute
        # span). On a SUB-MINUTE frame, convert the configured bar count to the SAME
        # wall-clock span via the base pullback interval (reuses _orb_bar_count — the ORB
        # "derive bars from minutes" precedent; no new knob). Non-micro frames keep the raw
        # count (byte-identical).
        _sustain_n = int(sustain_lookback_bars)
        _iv_sus = str(entry_interval or "").strip().lower()
        if _iv_sus.endswith("s") and not _iv_sus.endswith("ms"):
            _base_iv_sus = str(
                getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m"
            ).strip().lower()
            _base_min = 1
            try:
                if _base_iv_sus.endswith("m"):
                    _base_min = max(1, int(_base_iv_sus[:-1] or 1))
                elif _base_iv_sus.endswith("h"):
                    _base_min = max(1, int(_base_iv_sus[:-1] or 1) * 60)
            except (TypeError, ValueError):
                _base_min = 1
            _sustain_n = _orb_bar_count(entry_interval, _sustain_n * _base_min)
            debug["sustain_lookback_effective_bars"] = _sustain_n
        sustained = _sustained_rvol(vr, cur, _sustain_n)
        # CAPTURE-G2 COIL-EXEMPT (2026-07-03): the N-bar sustain MEAN includes the quiet
        # coil bars that precede a dry-coil premarket break (JEM 06-30 12:59Z class), so the
        # break tick is mathematically < 1.0 BY CONSTRUCTION — the setup is structurally
        # untradeable even as it explodes. TAPE-VERIFIED on JEM 2026-06-30: at the first-break
        # window 12:59:16-30Z the coil-inclusive 5-bar mean is 0.74-0.91 (< 1.0 floor) while a
        # coil-EXCLUDED mean crosses 1.0 as the active volume builds, and the forming-bar rvol
        # reaches the explosive floor by ~12:59:40Z. Two OR-ed exemptions, both fail-CLOSED:
        #
        #   (A) EXPLOSIVE FORMING BAR — the break bar's OWN rvol (vol_ratio, already computed)
        #       is >= chili_momentum_explosive_floor_rvol (no new magic threshold): the bar
        #       itself proves volume is exploding NOW, exactly what the mean shows once the coil
        #       rolls off. Catches the completed-bar explosive break.
        #   (B) COIL-EXCLUDED MEAN — recompute the sustain mean over the recent window but DROP
        #       the identified low-range COIL bars (bar range < chili_momentum_sustained_coil_
        #       range_atr_frac x ATR — a structural, not volume-defined, coil marker), then
        #       require the ACTIVE-bar mean to clear the SAME sustained_rvol_floor. This is the
        #       matrix's "compute the sustain mean EXCLUDING coil bars" option; it recovers the
        #       window as real volume returns without ever passing a genuinely dead tape.
        #
        # The ESTR faded-24h-mover guardrail stays INTACT: a genuine low-volume drift (break
        # bar not exploding AND the active-bar mean still below the floor) is blocked below.
        # Deep-reclaim bounces are NEVER exempted (dead-cat guard). Kill-switch
        # chili_momentum_sustained_volume_coil_exempt_enabled (default True) ⇒ OFF is byte-
        # identical to the coil-inclusive gate.
        _coil_exempt = False
        if (
            bool(getattr(settings, "chili_momentum_sustained_volume_coil_exempt_enabled", True))
            and not _deep_reclaim  # never exempt a deep-retrace bounce (dead-cat trap guard)
        ):
            try:
                _cx_floor = float(
                    getattr(settings, "chili_momentum_explosive_floor_rvol", 5.0) or 0.0
                )
            except (TypeError, ValueError):
                _cx_floor = 5.0
            # (A) explosive forming-bar exemption.
            if (
                vol_ratio is not None
                and _cx_floor > 0
                and float(vol_ratio) == float(vol_ratio)
                and float(vol_ratio) >= _cx_floor
            ):
                _coil_exempt = True
                debug["sustained_volume_coil_exempt"] = {
                    "mode": "explosive_break_bar",
                    "break_bar_rvol": round(float(vol_ratio), 3),
                    "explosive_floor": round(_cx_floor, 3),
                }
            # (B) coil-excluded active-bar mean exemption (recover the window as volume returns).
            if not _coil_exempt:
                # F5 (capture-g fix): a configured 0.0 must BIND (0.0 disables the coil
                # exclusion = the strictest setting) — `or 0.5` silently coerced the falsy
                # 0.0 back to 0.5, making the strict setting unbindable. Explicit None check.
                _cx_frac_raw = getattr(
                    settings, "chili_momentum_sustained_coil_range_atr_frac", None
                )
                try:
                    _cx_frac = 0.5 if _cx_frac_raw is None else float(_cx_frac_raw)
                except (TypeError, ValueError):
                    _cx_frac = 0.5
                _active_mean = _sustained_rvol_excluding_coil(
                    vr, atr, high, low, cur, _sustain_n,  # same wall-clock-scaled window
                    coil_range_atr_frac=_cx_frac,
                )
                if _active_mean is not None and _active_mean >= float(sustained_rvol_floor):
                    _coil_exempt = True
                    debug["sustained_volume_coil_exempt"] = {
                        "mode": "coil_excluded_mean",
                        "active_bar_rvol_mean": round(float(_active_mean), 3),
                        "floor": round(float(sustained_rvol_floor), 3),
                    }
        if not _coil_exempt:
            if sustained is not None:
                debug["sustained_rvol"] = round(sustained, 2)
                if sustained < float(sustained_rvol_floor):
                    return False, "faded_volume_no_sustain", debug
            elif _deep_reclaim:
                return False, "faded_volume_no_sustain", debug

    # ── Ross candle / VWAP / MACD confirmations (the tape-reading the structural
    # gate alone misses; each optional + live-runner-gated, fail-OPEN so thin data
    # never blocks an otherwise-valid break). docs/DESIGN/MOMENTUM_LANE.md §8.
    cur_o = float(df["Open"].iloc[cur])
    cur_h, cur_l, cur_c = float(high.iloc[cur]), float(low.iloc[cur]), float(close.iloc[cur])

    # Conviction break candle: reject a doji / topping-tail "break" that wicks out.
    # Not applicable to a tick-break (the breaking bar is mid-formation) — the
    # breakout-or-bailout fast exit covers a tick entry that wicks out.
    if require_break_candle and not _tick_break:
        from .candles import is_strong_bull_break_candle

        # ADAPTIVE close-position floor (gate-audit fix): the FIXED 0.50 close-pos
        # over-rejected explosive FIRST-pushes (Ross buys the first strong push even
        # when the 1m candle isn't textbook-strong). When the flag is ON and THIS break
        # is explosive (trigger-bar RVOL >= the SAME E3 explosive floor used above),
        # float the requirement DOWN from 0.50 toward the relaxed floor as RVOL exceeds
        # the floor — RVOL-DERIVED, no new magic threshold. Ordinary tape (RVOL below
        # the floor, or unknown) keeps the textbook 0.50; a genuinely weak/doji break
        # still blocks below the relaxed floor even at high RVOL (doji rejection
        # preserved). Flag OFF ⇒ _close_pos == break_candle_min_close_pos AND the upper-
        # wick cap stays at its 0.50 default ⇒ byte-identical.
        #
        # NOTE on the upper-wick cap: for a GREEN break bar (the only bar this gate lets
        # through — red is rejected outright) is_strong_bull_break_candle's upper-wick
        # fraction is identically (1 - close_pos), so the default max_upper_wick_frac=0.50
        # would re-impose the very 0.50 floor we are relaxing. So the relaxed path floats
        # the wick cap UP in lockstep to (1 - effective_close_pos), keeping the two
        # conditions equivalent; the non-relaxed/flag-off path leaves it at the 0.50
        # default (byte-identical).
        _close_pos = float(break_candle_min_close_pos)
        _max_upper_wick = 0.50  # is_strong_bull_break_candle's documented default
        if bool(
            getattr(settings, "chili_momentum_break_candle_adaptive_close_pos_enabled", False)
        ):
            _adapt_floor_rvol = float(
                getattr(settings, "chili_momentum_explosive_floor_rvol", 5.0) or 0.0
            )
            if (
                _adapt_floor_rvol > 0
                and vol_ratio is not None
                and float(vol_ratio) >= _adapt_floor_rvol
            ):
                _relaxed = float(
                    getattr(settings, "chili_momentum_break_candle_adaptive_close_pos_floor", 0.30)
                    or 0.0
                )
                # Only relax DOWNWARD (a relaxed floor mistakenly set above the textbook
                # 0.50 must never TIGHTEN the gate — keep the smaller of the two).
                if _relaxed < _close_pos:
                    # Over-floor excess in [0,1]: 0 AT the floor -> textbook 0.50;
                    # 1 at RVOL = 2x the floor (and beyond) -> the relaxed floor.
                    _excess = (float(vol_ratio) - _adapt_floor_rvol) / _adapt_floor_rvol
                    _excess = max(0.0, min(1.0, _excess))
                    _close_pos = _close_pos - _excess * (_close_pos - _relaxed)
                    # Float the wick cap up in lockstep (never below the 0.50 default, so a
                    # relaxed bar can never be held to a STRICTER wick than the textbook gate).
                    _max_upper_wick = max(0.50, 1.0 - _close_pos)
                    debug["break_candle_adaptive_close_pos"] = {
                        "rvol": round(float(vol_ratio), 2),
                        "rvol_floor": round(_adapt_floor_rvol, 2),
                        "relaxed_floor": round(_relaxed, 4),
                        "effective_min_close_pos": round(_close_pos, 4),
                        "effective_max_upper_wick_frac": round(_max_upper_wick, 4),
                    }

        if not is_strong_bull_break_candle(
            cur_o, cur_h, cur_l, cur_c,
            min_close_pos=_close_pos, max_upper_wick_frac=_max_upper_wick,
        ):
            debug["break_candle"] = {"o": cur_o, "h": cur_h, "l": cur_l, "c": cur_c}
            return False, "weak_break_candle", debug

    # VWAP hold: Ross stays long ABOVE VWAP. Fail-OPEN when VWAP unavailable —
    # EXCEPT on the deep-reclaim path (2026-06-12 entry study: both VSME losers
    # entered BELOW VWAP via deep-reclaim; "weak structure bought cheap" is the
    # loser signature, so the weak-prior path must not fire blind).
    if require_vwap_hold:
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        if vwap_cur is not None and float(vwap_cur) > 0:
            debug["vwap"] = round(float(vwap_cur), 6)
            _px_vs_vwap = float(live_price) if _tick_break else cur_c
            if _px_vs_vwap < float(vwap_cur) * (1.0 - max(0.0, float(vwap_hold_buffer))):
                return False, "below_vwap", debug
        elif _deep_reclaim:
            return False, "vwap_unavailable_weak_path", debug

    # MACD momentum confirmation (lenient: histogram >= 0 OR macd line >= signal).
    if require_macd_bullish:
        hh = macd_hist[cur] if cur < len(macd_hist) and macd_hist[cur] is not None else None
        m = macd[cur] if cur < len(macd) and macd[cur] is not None else None
        s = macd_sig[cur] if cur < len(macd_sig) and macd_sig[cur] is not None else None
        if hh is not None or (m is not None and s is not None):
            # Gate 1 (dip-buy quality, flag-gated): STRICT requires the MACD LINE
            # strictly above SIGNAL (a true cross-up, not the lenient hist>=0 OR
            # line>=signal). Warmup (line or signal None) STILL fails open under
            # strict — never veto on missing MACD. OFF ⇒ the EXACT current
            # expression (byte-identical). Self-relative (m vs s), no magic number.
            _macd_strict = bool(getattr(settings, "chili_momentum_entry_macd_open_strict", False))
            if _macd_strict and (m is None or s is None):
                bullish = True  # fail-open on warmup (do NOT veto)
            elif _macd_strict:
                bullish = float(m) > float(s)
                # A/B tag only when strict is ON (OFF leaves debug byte-identical).
                debug["macd_open"] = {"m": m, "s": s}
            else:
                bullish = (hh is not None and float(hh) >= 0.0) or (
                    m is not None and s is not None and float(m) >= float(s)
                )
            debug["macd_hist"] = None if hh is None else round(float(hh), 6)
            if not bullish:
                return False, "macd_not_bullish", debug

    # VERTICALITY SKIP (2026-06-12 entry study: 3/3 fills with >3% 1m-EMA9
    # extension went a full R underwater — vertical-bar chases). The cap is
    # ATR-scaled (an instrument that breathes 4% gets more room than one that
    # breathes 1%): extension above EMA9 must stay under atr_pct x the knob.
    # 0 disables. Applies to every success path incl. tick-breaks — the tick
    # path is exactly where mid-extension chases happen.
    try:
        _vert_mult = float(getattr(settings, "chili_momentum_entry_verticality_atr_mult", 1.5) or 0.0)
    except (TypeError, ValueError):
        _vert_mult = 1.5
    # SD-1 (cold-frame fail-OPEN): when atr_pct is None (ATR array not warm on a
    # cold/short frame), the cap below collapses to the 0.5% punitive floor, which
    # over-rejects valid low-float Ross names that normally breathe >0.5% per bar.
    # When the flag is ON (default True) we SKIP the verticality veto entirely on a
    # cold frame (fail-OPEN — we cannot tell a chase from a normal breath without
    # ATR). The veto stays FULLY active whenever atr_pct IS known. Flag OFF ⇒ the
    # exact 0.5%-floor behaviour below (byte-identical).
    _vert_skip_cold = bool(getattr(settings, "chili_momentum_verticality_skip_on_cold_atr", True))
    if _vert_mult > 0 and not (_vert_skip_cold and atr_pct is None):
        _e9 = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        if _e9 is not None and float(_e9) > 0:
            _px_v = float(live_price) if (_tick_break and live_price) else cur_c
            _ext = (_px_v / float(_e9)) - 1.0
            _cap = max(0.005, float(atr_pct or 0.0) * _vert_mult)
            if _ext > _cap:
                debug["verticality"] = {"ext": round(_ext, 4), "cap": round(_cap, 4)}
                return False, "extended_verticality", debug

    if _deep_reclaim:
        if debug.get("dipbuy"):
            # observable/A/B-able dip-buy reason (else it would be swallowed as plain
            # deep_reclaim_ok and the 06/12 A/B could not attribute it)
            return True, ("deep_reclaim_dipbuy_tick_ok" if _tick_break else "deep_reclaim_dipbuy_ok"), debug
        return True, ("deep_reclaim_tick_ok" if _tick_break else "deep_reclaim_ok"), debug
    # First-pullback gets its OWN observable reason (so the replay A/B can attribute the
    # aggressive early entry vs the legacy ladder), bar-fired or tick-fired.
    if debug.get("pattern") == "first_pullback":
        return True, ("first_pullback_tick_ok" if _tick_break else "first_pullback_ok"), debug
    # FIX C(1): the explosive RAW FIRST-PUSH gets its OWN observable reason (distinct from the
    # waiting_for_retest escape) so the live A/B can attribute the first-push tier separately.
    if _explosive_raw_first_push:
        return True, ("explosive_raw_first_push_tick_ok" if _tick_break else "explosive_raw_first_push_ok"), debug
    # The adaptive-retest explosive RAW-BREAK escape gets its OWN observable reason (so the replay
    # A/B can attribute it vs the runaway rescue and the plain break-retest).
    if _explosive_raw_break:
        return True, ("explosive_raw_break_tick_ok" if _tick_break else "explosive_raw_break_ok"), debug
    return True, ("pullback_break_tick_ok" if _tick_break else "pullback_break_ok"), debug


def breakout_failed_to_hold(
    *,
    breakout_level: float | None,
    bid: float | None,
    held_seconds: float,
    window_seconds: float,
    buffer_pct: float = 0.001,
    lock_in_seconds: float = 0.0,
) -> bool:
    """#2 Breakout-or-bailout (Ross flat-top rule: "if the stock cannot hold the
    breakout level after entry, exit IMMEDIATELY" rather than waiting for the
    structural stop).

    Pure decision: within ``window_seconds`` of a pullback_break entry, return True
    when the bid has fallen back below the broken ``breakout_level`` (minus a small
    wick buffer) — a failed breakout to be cut well inside the structural stop.
    Guarded so it never fights the normal stop: no level / outside the early window
    / non-positive inputs all return False. docs/DESIGN/MOMENTUM_LANE.md

    LOCK-IN (GATE 2 of the explosive-mover recalibration): ``lock_in_seconds`` is a
    floor BELOW which the fast-bail CANNOT fire — give the breakout time to stabilize
    through a NORMAL retest before treating a momentary sub-level dip as a failed
    breakout (a violent squeeze dips the bid below the level within a few seconds, then
    resumes — FCUV +21% after a 4.5s bail). The structural stop / #769 max-loss circuit
    still fire INSIDE the lock-in (they are evaluated separately, ahead of this gate), so
    a genuinely collapsing position is NOT held hostage. ADDITIVE: ``lock_in_seconds``
    defaults to 0.0 ⇒ byte-identical (the caller passes 0 when the master flag is OFF).
    """
    try:
        lvl = float(breakout_level) if breakout_level is not None else 0.0
        b = float(bid) if bid is not None else 0.0
        held = float(held_seconds)
        window = float(window_seconds)
        lock_in = max(0.0, float(lock_in_seconds))
    except (TypeError, ValueError):
        return False
    if lvl <= 0.0 or b <= 0.0 or window <= 0.0:
        return False
    if held > window:
        return False
    if held < lock_in:
        # Inside the stabilization lock-in — defer the fast-bail (structural stop +
        # max-loss circuit still protect the position; they run before this gate).
        return False
    return b < lvl * (1.0 - max(0.0, float(buffer_pct)))


def regime_entry_allowed(
    family_id: str | None,
    *,
    atr_pct: float | None,
    chop_expansion: str,
    vol_regime: str,
) -> tuple[bool, str]:
    """Regime-aware entry filter (low vol breakouts, extreme vol all, chop vs impulse)."""
    fid = (family_id or "").lower()
    ap = float(atr_pct) if atr_pct is not None else None
    chop = (chop_expansion or "").lower()
    vreg = (vol_regime or "").lower()

    if ap is not None and ap > 0.045:
        # The breakout/impulse/momentum families ARE the Ross small-cap lane — explosive,
        # extreme-ATR names ($1->$66 movers) are the SETUP, not a disqualifier. Their risk is
        # bounded by the wide vol-floor stop + risk-first sizing + the per-trade notional cap
        # (extreme ATR -> wide stop -> small size), NOT by refusing the trade. A flat 4.5% ATR
        # ceiling structurally blocked the lane from ever entering its best names — the real
        # paper-flow replay on NPT (2026-06-08) hit this on EVERY candidate (0 fills), matching
        # the live 157 cancelled-pre-entry. So the ceiling applies only to NON-momentum families.
        if not ("breakout" in fid or "impulse" in fid or "momentum" in fid or "ross" in fid):
            return False, "extreme_atr_block_all"
    if ap is not None and ap < 0.008:
        if "breakout" in fid or "impulse" in fid:
            return False, "low_atr_block_breakout_family"
    if chop == "chop":
        if "breakout" in fid or "impulse" in fid:
            return False, "chop_regime_block_breakout_impulse"
    if vreg == "extreme":
        return False, "extreme_vol_regime_block_all"
    return True, "regime_ok"


def hurst_proxy_from_closes(close: pd.Series) -> float:
    """Simple lag-1 autocorrelation proxy in ~[0.35, 0.65] for regime meta."""
    s = close.astype(float).dropna()
    if len(s) < 25:
        return 0.5
    prev = s.shift(1)
    ratio = s / prev
    rets = []
    for a, b in zip(s.values[1:], prev.values[1:]):
        try:
            if b and b > 0 and a and a > 0:
                rets.append(math.log(float(a) / float(b)))
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    if len(rets) < 20:
        return 0.5
    ser = pd.Series(rets[-60:])
    r1 = ser.autocorr(lag=1)
    if r1 is None or (isinstance(r1, float) and pd.isna(r1)):
        r1 = 0.0
    h = 0.5 + 0.35 * math.tanh(float(r1) * 4.0)
    return max(0.35, min(0.65, h))


def _hod_extension_ok(
    *, level: float, ema9_cur: float | None, vwap_cur: float | None,
    atr_pct: float | None, rvol: float | None,
) -> tuple[bool, dict[str, Any]]:
    """ANTI-CHASE extension guard for a HOD/flat-top BREAKOUT (the blow-off defense).

    The break LEVEL (the base's resistance, which is the entry) must NOT sit excessively
    extended above the 9-EMA / VWAP — an over-extended vertical IS a parabolic blow-off,
    not a tested base break. Reuses ``_entry_extension_veto`` (the SAME adaptive ATR-scaled
    cap + RVOL boost the chase veto uses, so there is ONE documented extension yardstick)
    measured against BOTH the 9-EMA and the session VWAP: the entry is rejected when it is
    extended past either. Returns ``(ok, debug)``; fail-OPEN (ok=True) on missing reads so
    thin data never blocks. Pure."""
    dbg: dict[str, Any] = {}
    try:
        for _name, _ref in (("ema9", ema9_cur), ("vwap", vwap_cur)):
            if _ref is None or float(_ref) <= 0:
                continue
            # _entry_extension_veto returns True when level >= ref*(1+cap) — i.e. the
            # entry is too far above the reference (a vertical extension = a blow-off).
            if _entry_extension_veto(float(level), float(_ref), atr_pct, settings, rvol=rvol):
                dbg["hod_extended_vs"] = _name
                dbg["hod_ext_pct"] = round(float(level) / float(_ref) - 1.0, 4)
                return False, dbg
    except (TypeError, ValueError):
        return True, dbg  # bad inputs -> fail-open (never block the break on a bug)
    return True, dbg


def hod_break_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    flat_top: bool = False,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross HOD / NEW-HIGH BREAKOUT entry (Batch A) — the lane's FIRST breakout trigger.

    Every other trigger needs a prior DIP (pullback / reclaim / flush). A straight-up
    parabolic HOD runner that never pulls back to the 9-EMA produces NO fills (SHPH +86%,
    armed 8x, 0 entries). Ross buys the HOD break verbatim (SS101 #011: "buying the high
    of day... get in a couple cents underneath that level to anticipate the break").

    Detects a CONSOLIDATION BASE holding a tight range just under the day high (a base/flag
    right under the high, NOT a vertical spike), then FIRES on the break to a NEW HIGH with
    (a) a volume spike on the break bar and (b) tick-thrust confirmation. Entry = the base
    resistance (the break level, "a couple cents underneath"); stop = the consolidation low
    (a tight, well-defined stop). The vol-floor layer widens the stop downstream
    (INVARIANT A), so this gate does NOT pre-floor it.

    ``flat_top=True`` parameterizes the SAME logic for a FLAT-TOP breakout: 2-3 taps
    (topping tails) at a FLAT resistance, with whole/half-dollar round-number context.

    Returns the shared ``(ok, reason, debug)`` 3-tuple with ``pullback_low`` (= the base
    low / structural stop) and ``pullback_high`` (= the break level) under the SAME keys
    the pullback-break trigger uses, so the downstream sizing / structural-stop /
    breakout-or-bailout machinery is reused unchanged. ``reason`` is ``hod_break``
    (or ``flat_top_break``) on a completed-bar break, the matching ``..._tick_ok`` is set
    by the caller's tick path. WAIT reasons are tick-armable (``waiting_for_break``).

    ANTI-CHASE (must not buy the top of a blow-off — verification will try to make it):
      (a) ``front_side_state`` / ``_detect_back_side`` — never fire on a backside /
          rolled-over top (an extended-AND-rolling blow-off);
      (b) the EXTENSION guard (``_hod_extension_ok``) — the break level must not be
          excessively extended above the 9-EMA / VWAP (a vertical = blow-off, skip);
      (c) the CONSOLIDATION REQUIREMENT itself — a tested base (a tight range under the
          high) must exist, so we buy a tested break, not a spike.

    ADDITIVE: flag OFF / thin (<12 bars) / no base / not at the highs -> ``(False, reason,
    {...})`` with NO side effects. Fail-OPEN to a benign decline on any error (never raises,
    never blocks the rest of the ladder). docs/DESIGN/MOMENTUM_LANE.md"""
    _flag = (
        "chili_momentum_flat_top_entry_enabled" if flat_top
        else "chili_momentum_hod_break_entry_enabled"
    )
    _reason_fire = "flat_top_break" if flat_top else "hod_break"
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": _reason_fire}
    try:
        if not bool(getattr(settings, _flag, True)):
            return False, f"{_reason_fire}_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, f"{_reason_fire}_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        opn = df["Open"].astype(float) if "Open" in getattr(df, "columns", []) else None
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "vwap", "volume_ratio", "atr"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []

        # Instrument volatility (ATR/price) -> the ADAPTIVE base-tightness yardstick + the
        # vol-aware tolerances. None on thin data -> a small floor (so the base check still
        # has a yardstick); never magic cents.
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
        eff_a = a if a > 0 else 0.01  # thin-data floor for the relative base width

        # ── CONSOLIDATION BASE just under the HOD (anti-chase requirement (c)) ───────────
        # The base = the last K COMPLETED bars (cur is the breaking bar). Its high is the
        # resistance to break, its low the structural stop. The ATR-relative knob is the
        # ONE documented base for both base-width and the under-HOD band.
        base_bars = int(getattr(settings, "chili_momentum_hod_base_bars", 4) or 4)
        base_atr_mult = float(getattr(settings, "chili_momentum_hod_base_atr_mult", 1.5) or 1.5)
        base_start = max(0, cur - base_bars)
        if base_start >= cur:
            return False, f"{_reason_fire}_insufficient_bars", debug
        base_hi = float(high.iloc[base_start:cur].max())
        base_lo = float(low.iloc[base_start:cur].min())
        if not (0.0 < base_lo < base_hi):
            return False, f"{_reason_fire}_bad_base", debug

        # The base must sit just UNDER the day high (a base AT the highs, not a mid-range
        # pause): the session HOD on the COMPLETED bars is at/just-above the base high.
        sess_hod = float(high.iloc[:cur].max())  # day high on completed bars
        debug["base_high"] = round(base_hi, 6)
        debug["base_low"] = round(base_lo, 6)
        debug["sess_hod"] = round(sess_hod, 6)
        # base must be within the under-HOD band (its high near the HOD) — else this is a
        # mid-range consolidation, not the under-the-high coil Ross buys.
        if sess_hod > 0 and (sess_hod - base_hi) / sess_hod > base_atr_mult * eff_a:
            debug["hod_gap_pct"] = round((sess_hod - base_hi) / sess_hod, 4)
            return False, f"{_reason_fire}_not_at_highs", debug

        # TIGHT base (a flag, not a sloppy chop): the base range must be within the
        # ATR-relative width. A base wider than this is chop -> not a tested break.
        base_range_pct = (base_hi - base_lo) / base_hi if base_hi > 0 else 1.0
        debug["base_range_pct"] = round(base_range_pct, 4)
        if base_range_pct > base_atr_mult * eff_a:
            return False, f"{_reason_fire}_base_too_wide", debug

        # ── FLAT-TOP parameterization: 2-3 taps (topping tails) at a FLAT resistance ─────
        # The flat-top variant additionally requires the base bars to TAP a flat level
        # (highs clustered within a tight band of base_hi) with >=2 topping-tail rejections
        # there — the textbook flat-top coil. Round-number context (whole/half dollar) is
        # surfaced for the operator but is NOT a gate (a flat top is valid off any level).
        if flat_top:
            from .candles import is_topping_tail
            from .daily_levels import _round_number_near

            _tol = max(0.001, 0.5 * eff_a)  # flat-band tolerance (half the base ATR room)
            taps = 0
            _ov = opn.values if (opn is not None and hasattr(opn, "values")) else None
            for i in range(base_start, cur):
                _hi = float(high.iloc[i])
                if _hi < base_hi * (1.0 - _tol):  # this bar's high isn't near the flat top
                    continue
                # a TAP = a high at the flat level; a topping tail there = a rejection tap.
                _o = float(_ov[i]) if (_ov is not None and i < len(_ov)) else float(close.iloc[i])
                _l, _c = float(low.iloc[i]), float(close.iloc[i])
                if is_topping_tail(_o, _hi, _l, _c):
                    taps += 1
            debug["flat_top_taps"] = taps
            if taps < 2:
                return False, "flat_top_too_few_taps", debug
            try:
                _rn = _round_number_near(base_hi, eff_a)
                if _rn is not None:
                    debug["flat_top_round_level"] = round(float(_rn), 6)
            except Exception:
                pass

        level = base_hi  # the break level = the base resistance ("a couple cents under")
        stop = base_lo   # tight structural stop below the consolidation low

        # ── ANTI-CHASE (a): backside / rolled-over top vetoes ───────────────────────────
        # Never fire the break into a name that has already rolled over. _detect_back_side
        # reads the 1m EMA/MACD rollover; front_side_state reads WHERE the name sits in its
        # OWN session (below VWAP / faded / chasing an extended blow-off top). Both fail-OPEN.
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, f"{_reason_fire}_back_side", debug
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            if getattr(_fs, "is_backside", False):
                # LIVE-TICK NEW-HIGH carve-out for chasing_top ONLY (mirrors the pullback
                # path): a live tick breaking to a fresh high IS front-side right now, so a
                # stale completed-bar rolled-over read should not veto it. below_vwap /
                # already_faded stay HARD vetoes.
                _fsr = getattr(_fs, "reason", "backside")
                _live_new_high = False
                if _fsr == "chasing_top" and live_price is not None:
                    try:
                        _live_new_high = float(live_price) > float(sess_hod)
                    except (TypeError, ValueError):
                        _live_new_high = False
                if not _live_new_high:
                    debug["front_side_state"] = _fsr
                    return False, f"{_reason_fire}_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            pass  # thin/degenerate frame -> fail-open (never block on a bug)

        # ── ANTI-CHASE (b): EXTENSION guard — the break level must not be a vertical ────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _rvol = None
        try:
            _rvol = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            _rvol = None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=_rvol,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, f"{_reason_fire}_extended", debug

        # ── L2 hidden-seller / big-seller veto (reused; fail-open on disabled/null L2) ──
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"{_reason_fire}_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # ── TRIGGER: the break to a NEW HIGH above the base resistance ──────────────────
        # The break LEVEL is the session HOD (the base sits at the highs, so clearing the
        # base IS clearing the HOD) — fire on a new HIGH above BOTH the base resistance and
        # the completed-bar HOD ("a couple cents underneath that level to anticipate it").
        brk_level = max(level, sess_hod)
        cur_hi = float(high.iloc[cur])

        # TICK-BREAK (Ross-speed): the structure is valid on completed bars and the LIVE
        # tick is already trading through the break level — enter on that tick, not a bar-
        # close later. Require the ATR/floor THRUST buffer (the tightest level the lane
        # arms must clear a real thrust, not a 1-tick poke) in EVERY session, plus the
        # premarket false-pop guard. The break bar's volume is unknowable mid-bar, so the
        # volume-spike leg is waived here (the shared caller's sustained-volume gate + the
        # breakout-or-bailout fast exit are the compensating guards).
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > brk_level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(brk_level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(brk_level), atr_pct=atr_pct,
            )
        ):
            debug["pullback_high"] = float(brk_level)
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, f"{_reason_fire}_tick_ok", debug

        # not broken on a completed bar yet -> ARM a tick-watch at the level (surfaced by
        # the caller as a TICK_ARMED_WAIT_REASON via the pullback_high).
        if cur_hi <= brk_level:
            debug["pullback_high"] = float(brk_level)
            return False, "waiting_for_break", debug
        # a completed bar broke to a NEW HIGH — require a VOLUME SPIKE on the break bar
        # (real demand, the new-HOD + thrust core, mirroring the pyramid new-HOD predicate).
        # The strong-close / candle-quality + sustained-volume confirmations are applied by
        # the shared caller's confirmation stack.
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        _vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < _vol_mult:
            return False, f"{_reason_fire}_low_volume", debug

        debug["pullback_high"] = float(brk_level)
        return True, _reason_fire, debug
    except Exception:
        return False, f"{_reason_fire}_error", {"entry_interval": entry_interval}


# ── P0: DAILY-CONTEXT-AWARE OVERHEAD VETO + BLUE-SKY ENTRY TRIGGER ────────────────
# entry_gates consumed ZERO daily context, so EVERY breakout trigger fired BLIND to
# overhead supply — it would buy a break into a wall of trapped longs $0.10 above just
# as readily as a break into clear blue sky. These two functions plumb the DailyContext
# (built/cached once per session by the live runner — NO new per-tick fetch) into the
# entry path: a veto that rejects a break into a ceiling, and a dedicated trigger that
# fires ONLY on a genuine clear-sky new-high break. Both kill-switch-gated: OFF ⇒ the
# entry path is daily-blind = byte-identical to before. docs/DESIGN/MOMENTUM_LANE.md


def _overhead_supply_veto(
    daily_ctx: Any, *, entry: float | None,
) -> tuple[str, dict[str, Any]] | None:
    """P0 OVERHEAD-SUPPLY VETO for ANY breakout entry (flag
    ``chili_momentum_overhead_veto_enabled``).

    When trapped supply (a prior daily swing high / unfilled gap / red-rejection cluster
    the price must fight THROUGH) sits within ``chili_momentum_overhead_veto_atr`` daily-
    ATR units ABOVE the ``entry``, return ``("overhead_supply", patch)`` so the caller
    rejects the break (do not buy straight into a ceiling). The threshold is ATR-derived
    (adaptive, no magic $); a TRUE blue-sky / clear-room break (overhead distance beyond
    the floor, or no overhead level at all) returns ``None`` (PASS) so the veto NEVER
    over-blocks a clean breakout.

    FAIL-OPEN to ``None`` on: flag off, no DailyContext, no usable ATR/entry, or any
    error — the entry path is never blocked on missing daily data or a bug. Pure read."""
    try:
        if not bool(getattr(settings, "chili_momentum_overhead_veto_enabled", False)):
            return None
        if daily_ctx is None or entry is None or not (float(entry) > 0):
            return None
        from .daily_levels import overhead_supply_atr

        room = overhead_supply_atr(daily_ctx, entry=float(entry))
        if room is None:                     # no overhead level found -> clear sky, PASS
            return None
        floor = float(getattr(settings, "chili_momentum_overhead_veto_atr", 0.5) or 0.5)
        if float(room) < floor:
            patch = {
                "overhead_supply_atr": round(float(room), 3),
                "overhead_veto_floor_atr": round(floor, 3),
            }
            # surface WHICH level is the nearest ceiling for the operator (best-effort).
            try:
                sh = getattr(daily_ctx, "swing_high_nd", None)
                gb = getattr(daily_ctx, "nearest_unfilled_gap_bottom", None)
                rej = getattr(daily_ctx, "rejection_count", None)
                if sh is not None:
                    patch["overhead_swing_high"] = round(float(sh), 4)
                if gb is not None:
                    patch["overhead_gap_bottom"] = round(float(gb), 4)
                if rej:
                    patch["overhead_rejection_count"] = int(rej)
            except (TypeError, ValueError):
                pass
            return "overhead_supply", patch
        return None
    except Exception:
        return None  # any error -> fail-open (never block an entry on a bug)


def blue_sky_break_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    daily_ctx: Any = None,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """P0 BLUE-SKY BREAKOUT entry (flag ``chili_momentum_blue_sky_entry_enabled``).

    DISTINCT from ``hod_break_confirmation`` (which breaks the high of DAY): this fires
    the break of a NEW MULTI-PERIOD / ALL-TIME high with NO overhead resistance — a true
    clear-sky breakout (the cleanest there is: no trapped longs to sell into the move).
    Requires (read from the session-cached ``DailyContext`` — NO new fetch):
      * ``entry_is_clear_sky`` — px at/above a fresh multi-period/all-time high
        (``is_blue_sky``) AND the nearest overhead-supply level (if any) at least
        ``chili_momentum_blue_sky_entry_min_room_atr`` daily-ATR away (genuine clear sky,
        NEVER a mid-range break with a ceiling above);
    plus, on the intraday tape: a consolidation base just under the high (REUSING the
    HOD-break base machinery), a break to a new high, and a volume confirm.

    Returns the shared ``(ok, reason, debug)`` with ``pullback_high`` (= the break level)
    and ``pullback_low`` (= the base low / structural stop) under the IDENTICAL keys, so
    the downstream sizing / structural-stop / breakout-or-bailout machinery + the setup-
    selector reuse it unchanged. ``reason`` is ``blue_sky_break`` on a completed-bar break
    (``blue_sky_break_tick_ok`` on the live-tick break); WAIT reasons are tick-armable
    (``waiting_for_break``).

    ADDITIVE: flag OFF / no DailyContext / not clear sky / thin -> ``(False, reason,
    {...})`` with NO side effects; fail-OPEN to a benign decline on any error (never
    raises, never blocks the rest of the ladder). docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "blue_sky_break"}
    try:
        if not bool(getattr(settings, "chili_momentum_blue_sky_entry_enabled", False)):
            return False, "blue_sky_break_disabled", debug
        if daily_ctx is None:
            return False, "blue_sky_break_no_daily_ctx", debug
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return False, "blue_sky_break_insufficient_bars", debug

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "vwap", "volume_ratio", "atr", "macd", "macd_signal"})
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []

        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None
        a = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.0
        eff_a = a if a > 0 else 0.01

        # ── CONSOLIDATION BASE just under the high (REUSE the HOD-break base read) ──────
        base_bars = int(getattr(settings, "chili_momentum_hod_base_bars", 4) or 4)
        base_atr_mult = float(getattr(settings, "chili_momentum_hod_base_atr_mult", 1.5) or 1.5)
        base_start = max(0, cur - base_bars)
        if base_start >= cur:
            return False, "blue_sky_break_insufficient_bars", debug
        base_hi = float(high.iloc[base_start:cur].max())
        base_lo = float(low.iloc[base_start:cur].min())
        if not (0.0 < base_lo < base_hi):
            return False, "blue_sky_break_bad_base", debug
        level = base_hi   # the break level = the base resistance ("a couple cents under")
        stop = base_lo    # tight structural stop below the consolidation low
        debug["base_high"] = round(base_hi, 6)
        debug["base_low"] = round(base_lo, 6)

        # tight base (a flag, not chop) — the same ATR-relative width guard as the HOD break.
        base_range_pct = (base_hi - base_lo) / base_hi if base_hi > 0 else 1.0
        debug["base_range_pct"] = round(base_range_pct, 4)
        if base_range_pct > base_atr_mult * eff_a:
            return False, "blue_sky_break_base_too_wide", debug

        # ── CLEAR-SKY REQUIREMENT (the defining gate): the break level must be a genuine
        # new multi-period/all-time high with clear room overhead — NEVER a mid-range
        # break into a ceiling. Evaluated at the BREAK LEVEL (the price we'd enter at).
        from .daily_levels import entry_is_clear_sky

        min_room = float(getattr(settings, "chili_momentum_blue_sky_entry_min_room_atr", 1.5) or 1.5)
        if not entry_is_clear_sky(daily_ctx, entry=float(level), min_room_atr=min_room):
            debug["is_blue_sky"] = bool(getattr(daily_ctx, "is_blue_sky", False))
            debug["min_room_atr"] = round(min_room, 3)
            try:
                from .daily_levels import overhead_supply_atr as _osa
                _r = _osa(daily_ctx, entry=float(level))
                if _r is not None:
                    debug["overhead_supply_atr"] = round(float(_r), 3)
            except Exception:
                pass
            return False, "blue_sky_break_not_clear_sky", debug
        debug["is_blue_sky"] = True
        debug["room_to_gap_top_atr"] = (
            round(float(getattr(daily_ctx, "room_to_gap_top_atr", None) or 0.0), 3)
            if getattr(daily_ctx, "room_to_gap_top_atr", None) is not None else None
        )

        # ── ANTI-CHASE: backside / rolled-over top veto (reused, fail-OPEN) ─────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "blue_sky_break_back_side", debug

        # ── L2 hidden-seller veto (reused; fail-open on disabled/null L2) ───────────────
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"blue_sky_break_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # ── TICK-BREAK: the live ask already trading through the break level (Ross speed) ──
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "blue_sky_break_tick_ok", debug

        # not broken on a completed bar yet -> ARM a tick-watch at the break level.
        cur_hi = float(high.iloc[cur])
        if cur_hi <= level:
            return False, "waiting_for_break", debug

        # a completed bar broke to a new high -> require a VOLUME SPIKE (real demand).
        vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < vol_mult:
            return False, "blue_sky_break_low_volume", debug
        return True, "blue_sky_break", debug
    except Exception:
        return False, "blue_sky_break_error", {"entry_interval": entry_interval}


def select_best_setup(
    candidates: list[tuple[bool, str, dict[str, Any]]],
    *,
    symbol: str | None = None,
    atr_pct: float | None = None,
    daily_ctx: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Setup-selector (Batch A): given several trigger results that fired on the SAME bar
    (each a shared ``(ok, reason, debug)`` with ``pullback_high`` = entry level +
    ``pullback_low`` = structural stop), pick the FIRE with the best structural
    reward:risk instead of first-clears-gates.

    R:R is computed from the SAME ``stop_target_prices`` the live runner uses to place the
    bracket, so the selection matches the actual order geometry (the entry = pullback_high,
    the stop = pullback_low, the target = the class-aware R:R-anchored target → reward/risk
    in price terms). Ties and any candidate missing a usable level fall back to the FIRST
    fire (the legacy ladder order), so a degenerate input is byte-identical to no selector.

    Returns the chosen ``(ok, reason, debug)`` (the debug carries a ``setup_rr`` +
    ``setup_selected_from`` breadcrumb on a real choice). Pure; no I/O. Fail-OPEN to the
    first fire on any error. docs/DESIGN/MOMENTUM_LANE.md"""
    def _veto_choice(choice: tuple[bool, str, dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
        """P0 OVERHEAD VETO at the single FIRE choke point: any chosen breakout entry that
        would buy straight into trapped supply within the ATR-floor overhead is rejected
        here, so it covers EVERY trigger that reaches the selector. Flag off / no
        DailyContext / clear sky ⇒ the choice is returned unchanged (byte-identical)."""
        try:
            if not (choice and choice[0] and isinstance(choice[2], dict)):
                return choice
            _v = _overhead_supply_veto(daily_ctx, entry=choice[2].get("pullback_high"))
            if _v is None:
                return choice
            _reason, _patch = _v
            _d = dict(choice[2])
            _d.update(_patch)
            _d["overhead_vetoed_from"] = choice[1]
            return False, f"overhead_veto_{_reason}", _d
        except Exception:
            return choice

    try:
        fires = [c for c in candidates if c and c[0] and isinstance(c[2], dict)
                 and c[2].get("pullback_high") and c[2].get("pullback_low")]
        if not fires:
            # no usable fire -> return the first truthy result (preserves legacy reason),
            # else the first candidate unchanged. (A truthy-but-levelless fire is not a
            # breakout the veto can reason about -> returned unchanged.)
            for c in candidates:
                if c and c[0]:
                    return c
            return candidates[0] if candidates else (False, "no_candidate", {})
        if len(fires) == 1:
            return _veto_choice(fires[0])
        from .paper_execution import class_aware_reward_risk, stop_target_prices

        # LOCATE #8 SECOND-LEG PREFERENCE: prefer a later, BASED leg over a 1st extended leg.
        # A based second-leg candidate (a breakout whose tested base/support sits ABOVE the
        # candidate set's lowest structural low by an ATR-scaled margin — i.e. it consolidated
        # higher, a second leg) gets a small R:R TILT in the arbitration. It is a PREFERENCE
        # among already-passing fires — it never admits a NEW entry and never loosens a guard.
        # OFF / single fire ⇒ no tilt (byte-identical). Bounded so it can't dominate a far
        # worse R:R. docs/DESIGN/MOMENTUM_LANE.md
        _2leg_on = bool(getattr(settings, "chili_momentum_second_leg_preference_enabled", False))
        _2leg_tilt = 0.0
        _set_low = None
        if _2leg_on and len(fires) > 1:
            try:
                _lows = [float(d["pullback_low"]) for _, _, d in fires
                         if isinstance(d, dict) and d.get("pullback_low") and float(d["pullback_low"]) > 0]
                _set_low = min(_lows) if _lows else None
                _2leg_tilt = max(0.0, min(1.0, float(
                    getattr(settings, "chili_momentum_second_leg_rr_tilt", 0.15) or 0.0
                )))
            except (TypeError, ValueError):
                _set_low, _2leg_tilt = None, 0.0

        _ap = float(atr_pct) if (atr_pct is not None and atr_pct > 0) else 0.01
        best = None
        best_rr = -1.0
        for ok_t, reason_t, dbg in fires:
            try:
                entry = float(dbg["pullback_high"])
                stop = float(dbg["pullback_low"])
                if not (0.0 < stop < entry):
                    continue
                _s, target = stop_target_prices(
                    entry, atr_pct=_ap, side_long=True,
                    reward_risk=class_aware_reward_risk(symbol),
                )
                risk = entry - stop
                reward = float(target) - entry
                rr = (reward / risk) if risk > 0 else -1.0
                # #8: a based second leg (this fire's structural base sits an ATR-scaled
                # margin ABOVE the candidate set's lowest base) earns the bounded R:R tilt.
                _eff_rr = rr
                if _2leg_tilt > 0.0 and _set_low is not None and rr > 0:
                    _margin = _ap * float(entry)  # ~1 ATR in price terms
                    if _margin > 0 and (stop - _set_low) >= _margin:
                        _eff_rr = rr * (1.0 + _2leg_tilt)
                        if isinstance(dbg, dict):
                            dbg["second_leg_based"] = True
            except (TypeError, ValueError, KeyError):
                continue
            if _eff_rr > best_rr:
                best_rr, best = _eff_rr, (ok_t, reason_t, dbg)
        if best is None:
            return _veto_choice(fires[0])
        _ok, _reason, _dbg = best
        _dbg = dict(_dbg)
        _dbg["setup_rr"] = round(best_rr, 3)
        _dbg["setup_selected_from"] = [c[1] for c in fires]
        return _veto_choice((_ok, _reason, _dbg))
    except Exception:
        for c in candidates:
            if c and c[0]:
                return c
        return candidates[0] if candidates else (False, "no_candidate", {})


# ── BATCH D: opening-range breakout + red-to-green + micro-pullback-primary ───────
# The remaining entry gaps from the Ross course audit. ALL three return the shared
# (ok, reason, debug) 3-tuple with ``pullback_high`` (= entry/break level) and
# ``pullback_low`` (= structural stop) under the IDENTICAL keys the pullback-break
# trigger uses, so the downstream sizing / structural-stop / breakout-or-bailout
# machinery + the setup-selector are reused unchanged. Each is flag-gated INSIDE the
# detector (OFF -> no-op, byte-identical) and runs AFTER the existing ladder.
# docs/DESIGN/MOMENTUM_LANE.md


def _orb_bar_count(entry_interval: str, orb_minutes: int) -> int:
    """How many COMPLETED bars of ``entry_interval`` cover the opening-range MINUTES — the
    ORB length DERIVED from the bar interval + the one documented ``orb_minutes`` knob (no
    fixed bar count). ``"15s"`` -> 4 bars/min, ``"1m"/"5m"/"15m"`` -> the integer minutes.
    Floors at 1 bar; fail-safe to ``orb_minutes`` bars (the 1m assumption) on a bad interval."""
    try:
        iv = str(entry_interval or "").strip().lower()
        m = max(1, int(orb_minutes))
        if iv.endswith("s"):
            secs = int(iv[:-1] or 60)
            if secs <= 0:
                secs = 60
            return max(1, int(round(m * 60.0 / secs)))
        if iv.endswith("m"):
            mins = int(iv[:-1] or 1)
            if mins <= 0:
                mins = 1
            return max(1, int(round(m / mins)))
        if iv.endswith("h"):
            hrs = int(iv[:-1] or 1)
            return max(1, int(round(m / (hrs * 60))))
        return max(1, m)
    except Exception:
        return max(1, int(orb_minutes) if orb_minutes else 5)


def opening_range_breakout_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross OPENING-RANGE BREAKOUT (Batch D; flag ``chili_momentum_orb_entry_enabled``).

    Define the OPENING RANGE = the high/low of the first ``chili_momentum_orb_minutes``
    (default 5) of COMPLETED bars after the session open, then FIRE on a break ABOVE the
    OR-high with a volume spike. Entry = the OR-high break level ("a couple cents under" the
    break, mirroring the HOD break); stop = the OR-low (the tight, well-defined opening-range
    floor). Only valid within ``chili_momentum_orb_window_minutes`` (default 60) AFTER the
    open — past it the rest of the ladder owns the tape.

    NO LOOKAHEAD: the opening range is built from COMPLETED bars whose timestamps fall in the
    first-N-minutes window (never the forming bar); the only intrabar use is the LIVE TICK
    trading through the OR-high (Ross enters on the breaking tick, not a bar-close later) —
    the SAME tick-break contract the HOD-break uses, gated by ``_premarket_tickbreak_confirmed``
    + ``_dipbuy_tick_thrust_ok`` so a 1-tick poke can't fire it.

    SESSION WINDOW: uses ``minutes_since_regular_open`` (the lane's DST-correct open clock).
    Equity-only (it returns ``None`` for crypto/weekend -> ``(False, ...)`` no-op) — the
    opening-range concept is an RTH-open edge, not a 24/7 crypto one.

    ANTI-CHASE: reuses ``_detect_back_side`` / ``front_side_state`` (never break a rolled-over
    top) and the volume-spike floor (a real break, not a drift). ADDITIVE: flag OFF / thin
    (<3 bars) / outside the window / no OR -> ``(False, reason, {...})`` with NO side effects;
    fail-OPEN to a benign decline on any error. docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "orb"}
    try:
        if not bool(getattr(settings, "chili_momentum_orb_entry_enabled", True)):
            return False, "orb_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 3:
            return False, "orb_insufficient_bars", debug

        # ── SESSION WINDOW: only within the first ~N min after the RTH open ─────────────
        # minutes_since_regular_open returns None for crypto / weekend / non-equity -> the
        # ORB is an RTH-open edge, no-op elsewhere (fail-CLOSED on the window: the OR is
        # undefined off the open).
        from .market_profile import minutes_since_regular_open

        mins_since_open = minutes_since_regular_open(symbol, now=now)
        if mins_since_open is None:
            return False, "orb_not_equity_session", debug
        orb_minutes = int(getattr(settings, "chili_momentum_orb_minutes", 5) or 5)
        window_minutes = float(getattr(settings, "chili_momentum_orb_window_minutes", 60.0) or 60.0)
        debug["mins_since_open"] = round(float(mins_since_open), 1)
        # The OR is only DEFINED once the opening-range minutes have fully elapsed (we need
        # the completed first-N-min bars), and the breakout is only VALID inside the window.
        if mins_since_open < float(orb_minutes):
            return False, "orb_forming", debug   # opening range not complete yet
        if mins_since_open > window_minutes:
            return False, "orb_window_passed", debug

        # ── OPENING RANGE from COMPLETED bars in the first-N-min window (no lookahead) ──
        # Slice today's session frame, then take the bars whose ET timestamp falls within
        # the first ``orb_minutes`` after 09:30 — these are the completed opening-range bars.
        sess = _today_session_frame(df)
        if sess is None or getattr(sess, "empty", True) or len(sess) < 2:
            return False, "orb_no_session", debug
        n = len(df)
        cur = n - 1
        opn = df["Open"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)

        # Prefer a timestamp-based OR (bars in [open, open+orb_minutes) on the ET clock);
        # fall back to the first ``_orb_bar_count`` bars of the session frame when the index
        # is non-datetime (e.g. replay synthetic frames). The forming (current) bar is
        # EXCLUDED from the OR either way (completed bars only).
        or_hi = or_lo = None
        or_bars = 0
        try:
            sidx = sess.index
            if isinstance(sidx, pd.DatetimeIndex) and sidx.tz is not None:
                et = sidx.tz_convert("America/New_York")
                day_open = et[-1].normalize() + pd.Timedelta(minutes=9 * 60 + 30)
                or_end = day_open + pd.Timedelta(minutes=orb_minutes)
                mask = (et >= day_open) & (et < or_end)
                # never include the still-forming current bar in the OR
                if cur < n and len(sess) == n:
                    mask = mask & (et < et[-1]) if et[-1] >= or_end else mask
                or_slice = sess[mask]
                if or_slice is not None and not or_slice.empty:
                    or_hi = float(or_slice["High"].astype(float).max())
                    or_lo = float(or_slice["Low"].astype(float).min())
                    or_bars = int(len(or_slice))
        except Exception:
            or_hi = or_lo = None
        if or_hi is None or or_lo is None:
            # non-datetime / sparse index -> first K COMPLETED bars of the session frame.
            k = _orb_bar_count(entry_interval, orb_minutes)
            try:
                s_hi = sess["High"].astype(float)
                s_lo = sess["Low"].astype(float)
                end = min(len(sess) - 1, k)  # exclude the last (possibly forming) bar
                if end < 1:
                    return False, "orb_forming", debug
                or_hi = float(s_hi.iloc[:end].max())
                or_lo = float(s_lo.iloc[:end].min())
                or_bars = int(end)
            except (TypeError, ValueError, KeyError):
                return False, "orb_no_range", debug
        if or_bars < 1 or not (0.0 < or_lo < or_hi):
            return False, "orb_no_range", debug
        debug["or_high"] = round(or_hi, 6)
        debug["or_low"] = round(or_lo, 6)
        debug["or_bars"] = or_bars

        # ── ATR% (the tick-thrust + anti-chase yardstick; ATR-relative, no fixed cents) ──
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "volume_ratio", "atr"})
        ema9 = arrays.get("ema_9") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None

        level = or_hi
        stop = or_lo

        # ── ANTI-CHASE: backside / rolled-over vetoes (reused, fail-OPEN) ───────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "orb_back_side", debug

        # ── L2 hidden-seller veto (reused; fail-open on disabled/null L2) ───────────────
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"orb_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # ── TICK-BREAK: the live ask already trading through the OR-high (Ross speed) ───
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "orb_break_tick_ok", debug

        # not broken on a completed bar -> ARM a tick-watch at the OR-high level.
        cur_hi = float(high.iloc[cur])
        if cur_hi <= level:
            return False, "waiting_for_break", debug

        # a completed bar broke above the OR-high -> require a VOLUME SPIKE (real demand).
        vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < vol_mult:
            return False, "orb_low_volume", debug
        return True, "orb_break", debug
    except Exception:
        return False, "orb_error", {"entry_interval": entry_interval}


def red_to_green_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross RED-TO-GREEN reclaim (Batch D; flag ``chili_momentum_red_to_green_entry_enabled``).

    A name trading RED (below the PRIOR-DAY CLOSE) that RECLAIMS that prior close with a
    bottoming-tail / reversal bar + volume is the textbook red-to-green long: Ross's
    red-to-green is crossing the PREVIOUS DAY'S CLOSE (the level that flips the name green
    ON THE DAY), not merely reclaiming today's intraday open. The move back through the prior
    close flips daily sentiment green; the session (red) low is the tight structural stop.
    Entry = the PRIOR-CLOSE reclaim ("a couple cents under" it, mirroring the breakout
    family); stop = the session low (the red low).

    R8 (WAVE-4 ITEM-3): the anchor is the PRIOR-DAY CLOSE (``_prior_day_close`` off the
    cached Massive ``prevDay.c``), FAIL-CLOSED — if the prior close is unavailable we SKIP
    (do NOT fall back to the intraday session open; reclaiming the open is not a
    red-to-green and was the bug this fixes).

    Reuses the bottoming-tail (``_bottoming_tail``) + the dipbuy reversal machinery (the
    ``is_bounce_curl_candle`` per-bar conviction + the ``_dipbuy_tick_thrust_ok`` /
    ``_premarket_tickbreak_confirmed`` tick-break contract).

    NO LOOKAHEAD: the prior close is a completed prior-session level; the red low is read
    from COMPLETED bars; the only intrabar use is the live tick reclaiming the prior close.

    GUARDS: the name must currently BE red (below the prior close before/at the reclaim — a
    name already green above the prior close has no red-to-green to make), the reversal bar
    must be a bottoming-tail bounce-curl, and the reclaim needs a volume spike. ANTI-CHASE
    backside veto reused. ADDITIVE: flag OFF / thin (<3 bars) / no prior close / not red /
    no reversal -> ``(False, reason, {...})`` with NO side effects; fail-OPEN on any error.
    docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "red_to_green"}
    try:
        if not bool(getattr(settings, "chili_momentum_red_to_green_entry_enabled", True)):
            return False, "red_to_green_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 3:
            return False, "red_to_green_insufficient_bars", debug

        sess = _today_session_frame(df)
        if sess is None or getattr(sess, "empty", True) or len(sess) < 2:
            return False, "red_to_green_no_session", debug

        n = len(df)
        cur = n - 1
        opn = df["Open"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)

        # ── ANCHOR = the PRIOR-DAY CLOSE (Ross's red-to-green level). FAIL-CLOSED: no prior
        # close -> SKIP (never fall back to the intraday open — that is not a red-to-green). ──
        prior_close = _prior_day_close(symbol)
        if prior_close is None or not (prior_close > 0):
            return False, "red_to_green_no_prior_close", debug
        anchor = float(prior_close)
        debug["prior_close"] = round(anchor, 6)

        # The session (red) LOW = the lowest low so far today = the structural stop. Built
        # from COMPLETED bars (the forming bar's low can only be lower -> the stop only
        # widens, never tightens below a completed structure).
        try:
            sess_low = float(sess["Low"].astype(float).min())
        except (TypeError, ValueError, KeyError):
            return False, "red_to_green_no_low", debug
        # RED STRUCTURE: the name must have traded BELOW the prior close today (a red day so
        # far) for there to be a red->green to make.
        if not (0.0 < sess_low < anchor):
            return False, "red_to_green_not_red_structure", debug   # never traded red below the prior close
        debug["session_low"] = round(sess_low, 6)

        # ── The name must currently BE red: the PRIOR (completed) bar closed below the prior
        # CLOSE (it has been trading red on the day), so reclaiming the prior close NOW is the
        # red->green flip. A name already green above the prior close has no red-to-green. ──
        prev_close = float(close.iloc[cur - 1])
        if prev_close >= anchor:
            debug["prev_close"] = round(prev_close, 6)
            return False, "red_to_green_already_green", debug

        # ── ATR% yardstick (tick-thrust + thin-data floors) ────────────────────────────
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "volume_ratio", "atr"})
        ema9 = arrays.get("ema_9") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None

        # ── The REVERSAL bar: the current bar is a bottoming-tail bounce-curl (the V-flip).
        c_o, c_h, c_l, c_c = (
            float(opn.iloc[cur]), float(high.iloc[cur]),
            float(low.iloc[cur]), float(close.iloc[cur]),
        )
        if not _bottoming_tail(c_o, c_h, c_l, c_c):
            return False, "red_to_green_no_bottoming_tail", debug
        from .candles import is_bounce_curl_candle

        if not is_bounce_curl_candle(c_o, c_h, c_l, c_c):
            return False, "red_to_green_weak_curl", debug

        level = anchor       # the reclaim level = the PRIOR-DAY CLOSE (Ross's red->green)
        stop = sess_low      # the structural stop = the red (session) low

        # ── ANTI-CHASE backside veto (reused, fail-OPEN) ───────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "red_to_green_back_side", debug

        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"red_to_green_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # ── TICK-BREAK: the live ask reclaiming the PRIOR CLOSE (red->green flip tick) ──
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "red_to_green_tick_ok", debug

        # not reclaimed on a completed bar yet -> ARM a tick-watch at the prior close.
        cur_close = float(close.iloc[cur])
        if cur_close <= level:
            return False, "waiting_for_reclaim", debug

        # a completed bar reclaimed the prior close -> require a VOLUME SPIKE (a real reclaim).
        vol_mult = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5)
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        debug["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio < vol_mult:
            return False, "red_to_green_low_volume", debug
        return True, "red_to_green", debug
    except Exception:
        return False, "red_to_green_error", {"entry_interval": entry_interval}


def ma_vwap_pullback_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross MOVING-AVERAGE / VWAP PULLBACK (SS101 #014; flag
    ``chili_momentum_ma_vwap_pullback_enabled``).

    The COOLER-MARKET grinder dip-buy: after an initial impulse (3+ green candles) the name
    pulls back 2+ bars into a SIDEWAYS consolidation that GRINDS along the moving averages -
    deeper than the shallow first-pullback gate tolerates (may touch the 9-EMA, then the
    20-EMA, possibly the VWAP) but NOT a clean bull-flag / ABCD (which this setup fails to
    form). Ross: "buy dips off the moving averages, getting in as close to support as
    possible, then letting it work." Entry FIRES when price RECLAIMS the 9-EMA (primary
    support) or, if the 9-EMA has been broken by the pull, the 20-EMA (secondary support);
    stop = the pullback's RETRACEMENT LOW (the structural low - NOT a reclaim state machine).

    DISTINCT (parity-checked against the live ladder, so it never duplicates an existing fire):
      * ``first_pullback_break`` rejects pulls deeper than its vol-aware SHALLOW cap; this
        fires on THE DEEPER pull (to the 9/20-EMA) the shallow gate forbids.
      * ``_evaluate_deep_reclaim`` is MORNING-ONLY (~10:30 ET cutoff) and needs a reclaim
        state-machine; this is ALL-DAY and fires on an EMA touch, no reclaim event required.
      * ``vwap_reclaim_confirmation`` fires on K-bars-below-VWAP then a reclaim ABOVE VWAP;
        this fires on dips TO the 9/20-EMA cascade (may never reach VWAP).
      * HOD/flat-top/ABCD/double-bottom need CLEAN geometry; this is the messy-consolidation
        case those fail to form. ``wick_reclaim`` is a hot-tape wick re-entry; this is an
        EMA-cascade dip-buy on a cooler grinder.

    Returns the shared ``(ok, reason, debug)`` 3-tuple with ``pullback_high`` (= the EMA
    LEVEL being reclaimed = the entry) and ``pullback_low`` (= the pullback retracement low =
    the structural stop) under the IDENTICAL keys the rest of the ladder uses, so the
    downstream sizing / structural-stop / bailout machinery + the setup-selector are reused
    unchanged (NO new sizing path).

    ANTI-CHASE (reused, never fires on a vertical blow-off):
      * EXTENSION guard ``_hod_extension_ok`` - the reclaim level must not sit excessively
        extended above the 9-EMA / VWAP (an over-extended print is a blow-off, not a dip);
      * COLLAPSE cap ``_collapse_cap`` - a pull deeper than the vol-relative cap is a
        breakdown, not a buyable dip;
      * BACKSIDE veto ``_detect_back_side`` - never buy a rolled-over top;
      * L2 hidden-seller veto ``_l2_entry_veto``;
      * the P0 OVERHEAD-supply veto is applied at the single FIRE choke point in
        ``select_best_setup`` (this gate joins that candidate set).

    ADAPTIVE (one documented base each; ATR-relative geometry, no fixed-price magic):
      ``chili_momentum_ma_vwap_impulse_bars`` (default 3), ``..._consolidation_bars``
      (default 2), ``..._vol_mult`` (default 1.5; falls back to the lane's shared
      ``chili_momentum_vwap_reclaim_vol_mult``). The EMA-touch band reuses the vol-aware
      ``ema_wick`` tolerance; the pullback-depth cap reuses ``_collapse_cap``.

    ADDITIVE: flag OFF / thin (<10 bars) / EMAs warming / no impulse / no consolidation / no
    EMA touch / weak volume -> ``(False, reason, {...})`` with NO side effects; fail-OPEN to
    a benign decline on any error (never raises, never blocks downstream).
    docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "ma_vwap_pullback"}
    try:
        if not bool(getattr(settings, "chili_momentum_ma_vwap_pullback_enabled", False)):
            return False, "ma_vwap_pullback_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return False, "ma_vwap_pullback_insufficient_bars", debug

        opn = df["Open"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        n = len(df)
        cur = n - 1

        arrays = compute_all_from_df(
            df, needed={"ema_9", "ema_20", "vwap", "macd", "macd_signal", "volume_ratio", "atr"}
        )
        ema9 = arrays.get("ema_9") or []
        ema20 = arrays.get("ema_20") or []
        vwap = arrays.get("vwap") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []

        # -- ATR% yardstick (vol-aware EMA band + extension guard; ATR-relative, no fixed %)
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None

        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        ema20_cur = ema20[cur] if cur < len(ema20) and ema20[cur] is not None else None
        if ema9_cur is None or ema20_cur is None or float(ema9_cur) <= 0 or float(ema20_cur) <= 0:
            return False, "ma_vwap_pullback_ema_warmup", debug  # EMAs not ready -> fail-open
        ema9_cur = float(ema9_cur)
        ema20_cur = float(ema20_cur)
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        vwap_cur = float(vwap_cur) if (vwap_cur is not None and float(vwap_cur) > 0) else None

        # vol-aware EMA-touch band (the same intrabar-noise tolerance the dip ladder uses).
        _shallow, ema_wick, _retest = _vol_aware_pullback_tolerances(
            atr_pct, float(getattr(settings, "chili_momentum_pullback_retrace_pct", 0.5) or 0.5)
        )

        impulse_bars = max(2, int(getattr(settings, "chili_momentum_ma_vwap_impulse_bars", 3) or 3))
        consol_bars = max(2, int(getattr(settings, "chili_momentum_ma_vwap_consolidation_bars", 2) or 2))

        # -- (1) IMPULSE: ``impulse_bars`` consecutive GREEN candles ending where the
        # consolidation begins. The consolidation occupies the LAST ``consol_bars`` completed
        # bars up to the forming current bar; the impulse is the run that precedes it.
        c_start = cur - consol_bars + 1
        imp_end = c_start - 1            # last impulse bar (inclusive)
        imp_start = imp_end - impulse_bars + 1
        if imp_start < 1:
            return False, "ma_vwap_pullback_no_room", debug
        green = 0
        for i in range(imp_start, imp_end + 1):
            if float(close.iloc[i]) > float(opn.iloc[i]):
                green += 1
        debug["impulse_green"] = green
        if green < impulse_bars:
            return False, "ma_vwap_pullback_no_impulse", debug

        # The impulse must actually have RISEN (a real leg up, not tiny dojis): the impulse
        # peak high is above where the impulse started.
        imp_low = float(low.iloc[imp_start])
        imp_peak = float(high.iloc[imp_start:imp_end + 1].max())
        if not (imp_peak > imp_low > 0):
            return False, "ma_vwap_pullback_flat_impulse", debug

        # -- (2) CONSOLIDATION: the last ``consol_bars`` GRIND sideways near/below the 9-EMA
        # (deeper than the shallow first-pullback, but holding the EMA cascade). The pullback
        # RETRACEMENT LOW = the lowest low across the consolidation (the structural stop).
        pull_low = float(low.iloc[c_start:cur + 1].min())
        if not (0.0 < pull_low):
            return False, "ma_vwap_pullback_bad_low", debug
        debug["pull_low"] = round(pull_low, 6)

        # COLLAPSE GUARD (anti-chase): a pull deeper than the vol-relative cap below the
        # impulse peak is a BREAKDOWN, not a buyable dip on the averages.
        depth = (imp_peak - pull_low) / imp_peak if imp_peak > 0 else 1.0
        cap = _collapse_cap(atr_pct)
        debug["depth"] = round(depth, 4)
        debug["collapse_cap"] = round(cap, 4)
        if depth > cap:
            return False, "ma_vwap_pullback_collapse", debug

        # The consolidation must GRIND near the averages: at least one consolidation bar's low
        # touched/penetrated the 9-EMA band (a dip TO support, not a shelf far above it).
        touched_9 = False
        broke_9 = False
        for i in range(c_start, cur + 1):
            e9 = ema9[i] if (0 <= i < len(ema9) and ema9[i] is not None) else None
            if e9 is None or float(e9) <= 0:
                continue
            lo_i = float(low.iloc[i])
            if lo_i <= float(e9) * (1.0 + ema_wick):
                touched_9 = True
            if float(close.iloc[i]) < float(e9) * (1.0 - ema_wick):
                broke_9 = True
        debug["touched_9ema"] = touched_9
        debug["broke_9ema"] = broke_9
        if not touched_9:
            return False, "ma_vwap_pullback_no_ema_touch", debug

        # -- (3) RECLAIM: current close AT/ABOVE the 9-EMA (primary), OR if the 9-EMA was
        # broken by the pull, current close AT/ABOVE the 20-EMA (secondary). The reclaimed
        # EMA level IS the entry level.
        cur_close = float(close.iloc[cur])
        level = None
        support = None
        if cur_close >= ema9_cur * (1.0 - ema_wick):
            level = ema9_cur
            support = "9ema"
        elif broke_9 and cur_close >= ema20_cur * (1.0 - ema_wick):
            level = ema20_cur
            support = "20ema"
        if level is None or not (0.0 < pull_low < float(level)):
            debug["support"] = support
            return False, "waiting_for_ma_reclaim", debug
        level = float(level)
        stop = float(pull_low)
        debug["support"] = support
        debug["reclaim_level"] = round(level, 6)

        # -- ANTI-CHASE: EXTENSION guard - the reclaim level must not be over-extended above
        # the 9-EMA / VWAP (a vertical blow-off is not a dip-on-the-averages).
        rvol = None
        try:
            rvol = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            rvol = None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=rvol,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "ma_vwap_pullback_extended", debug

        # -- ANTI-CHASE: BACKSIDE veto - never buy a rolled-over top (reused, fail-OPEN).
        _bs, _bs_reason = _detect_back_side(
            ema9, ema20,
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "ma_vwap_pullback_back_side", debug

        # -- NOT BELOW-VWAP (the lifecycle veto — mirrors cup_and_handle / wedge): never fire a
        # 9/20-EMA reclaim while the name sits BELOW VWAP (a backside reclaim Ross skips —
        # "I never buy below VWAP"). front_side_state reads WHERE the name sits in its OWN
        # session; it fails CLOSED on a thin/degenerate frame (this is a new-conviction fire
        # path). The _detect_back_side EMA/MACD read above does NOT cover below-VWAP, so this
        # is the distinct lifecycle arm. (No inline tape here — this gate matches the live
        # DIP-BUY family that substitutes tick-thrust + volume for tape.)
        try:
            from .ross_momentum import front_side_state

            _fs = front_side_state(_today_session_frame(df))
            debug["above_vwap"] = bool(getattr(_fs, "above_vwap", True))
            if getattr(_fs, "is_backside", False):
                debug["front_side_state"] = getattr(_fs, "reason", "backside")
                return False, "ma_vwap_pullback_backside_lifecycle", debug
        except (TypeError, ValueError, AttributeError, KeyError):
            # thin/degenerate frame -> fail-CLOSED for this new-conviction fire path.
            debug["reason"] = "front_side_read_error"
            return False, "ma_vwap_pullback_backside_lifecycle", debug

        # -- L2 hidden-seller veto (reused; fail-open on disabled/null L2)
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"ma_vwap_pullback_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)

        # -- TICK-BREAK: the live ask trading through the reclaim level (Ross speed: get in
        # as close to support as possible - the bounce off the EMA). The SAME tick-break
        # contract the rest of the ladder uses.
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "ma_vwap_pullback_tick_ok", debug

        # not reclaimed past the level on a completed bar yet -> ARM a tick-watch at the EMA.
        if cur_close <= level:
            return False, "waiting_for_ma_reclaim", debug

        # -- (4) VOLUME on the reclaim bar: ELEVATED (conviction, not a drift back). Adaptive
        # base (reuses the lane's vwap-reclaim vol-mult yardstick when the dedicated knob is
        # unset).
        vol_mult = float(
            getattr(settings, "chili_momentum_ma_vwap_vol_mult", None)
            or getattr(settings, "chili_momentum_vwap_reclaim_vol_mult", 1.5)
            or 1.5
        )
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        debug["vol_ratio"] = round(vol_ratio, 2)
        debug["vol_mult"] = round(vol_mult, 2)
        if vol_ratio < vol_mult:
            return False, "ma_vwap_pullback_low_volume", debug
        return True, "ma_vwap_pullback", debug
    except Exception:
        return False, "ma_vwap_pullback_error", {"entry_interval": entry_interval}


def bottom_reversal_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross BOTTOM REVERSAL (SS101 #019; flag ``chili_momentum_bottom_reversal_entry_enabled``).

    The counter-trend bounce off a sell-off: a SERIES of N CONSECUTIVE RED candles (Ross:
    "one, two, three, four, five, maybe more ... you could have 10, 15 red candles in a row")
    on increasing/elevated volume, then the FIRST candle to CLOSE GREEN is the confirmation
    the trend is shifting. Often a DOJI / bottoming-tail at the low marks exhaustion (the
    "tug of war" Ross calls out). Entry = the first green candle's CLOSE (or the break above
    its HIGH on live price); stop = the recent RED-SERIES LOW (the structural pivot).

    DISTINCT from every existing gate (parity reasoning, see docs/STRATEGY/CC_REPORTS):
      • RED-TO-GREEN requires the name to be SESSION-red (below the session OPEN) and enters
        on the OPEN-level RECLAIM (a structural tie to the open). BOTTOM-REVERSAL has NO
        session-tie: it fires on ANY series of consecutive red candles and enters on the
        FIRST GREEN candle itself, not a reclaim of any anchored level.
      • DOUBLE-BOTTOM needs TWO equal swing lows + an intervening neckline. BOTTOM-REVERSAL
        is the simpler single-leg flush: N reds then a green (no double-structure).
      • FIRST-PULLBACK-BREAK / micro-pullback are BULLISH continuation (a shallow dip inside
        an up-leg). BOTTOM-REVERSAL is COUNTER-TREND, into a DOWN series.
      • DEEP-RECLAIM needs an EMA-9 reclaim + recovery-swing-high break and is MORNING-ONLY.
        BOTTOM-REVERSAL has no EMA requirement and fires on the green candle itself, all day.

    NOISE / ANTI-CHASE DEFENSE: a minimum consecutive-red count (the ``bottom_reversal_min_red``
    base, default 2 — Ross's floor; 3–5 recommended for noise) so a single down bar is not a
    "reversal"; an elevated-volume confirm on the green bar (the green close or the recent
    RVOL floor passed) so a dead-tape green dribble does not fire; the backside MACD veto +
    the L2 seller veto reused unchanged so it never buys a rolled-over blow-off into trapped
    supply. A DOJI / bottoming-tail at the low is detected as an OPTIONAL exhaustion signal
    (recorded in debug, never required — Ross treats it as a confirmer, not a gate).

    NO LOOKAHEAD: the red series + the red-series low are read from COMPLETED bars; the green
    confirmation bar is the current (forming-then-completed) bar, and the only intrabar use is
    the live tick breaking the green bar's HIGH (the SAME tick-break contract the rest of the
    ladder uses). Tick-armable pre-close on ``pullback_high`` (the green-bar high).

    Level = the green candle CLOSE (completed) or the green-bar HIGH (live, pre-close);
    stop = the red-series low (ATR-floored downstream per INVARIANT A). Debug keys:
    ``{pullback_high, pullback_low, red_bars_count, volume_spike_ratio, doji_confirmed,
    pattern="bottom_reversal"}``.

    ADDITIVE: flag OFF / thin (<3 bars) / not-enough-reds / current-bar-not-green / low-volume
    -> ``(False, reason, {...})`` with NO side effects; fail-OPEN on any error.
    docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "bottom_reversal"}
    try:
        if not bool(getattr(settings, "chili_momentum_bottom_reversal_entry_enabled", False)):
            return False, "bottom_reversal_disabled", debug
        # Need at least (min_red) red bars + 1 green confirmation bar.
        min_red = int(getattr(settings, "chili_momentum_bottom_reversal_min_red", 2) or 2)
        min_red = max(2, min_red)
        debug["min_red"] = min_red
        if df is None or getattr(df, "empty", True) or len(df) < (min_red + 1):
            return False, "bottom_reversal_insufficient_bars", debug

        n = len(df)
        cur = n - 1
        opn = df["Open"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)

        # ── (4) The CURRENT bar must be GREEN (the first candle to close in the opposite
        # direction = the confirmation). A still-red current bar = no reversal yet. ─────
        c_o, c_h, c_l, c_c = (
            float(opn.iloc[cur]), float(high.iloc[cur]),
            float(low.iloc[cur]), float(close.iloc[cur]),
        )
        if not (c_c > c_o):
            debug["cur_open"] = round(c_o, 6)
            debug["cur_close"] = round(c_c, 6)
            return False, "bottom_reversal_not_green", debug

        # ── (3) Count the CONSECUTIVE RED candles IMMEDIATELY PRECEDING the green bar
        # (a red bar = close < open). Walk backwards from the bar just before ``cur``. ──
        red_count = 0
        i = cur - 1
        while i >= 0:
            _o = float(opn.iloc[i])
            _c = float(close.iloc[i])
            if _c < _o:
                red_count += 1
                i -= 1
            else:
                break
        debug["red_bars_count"] = red_count
        if red_count < min_red:
            return False, "bottom_reversal_not_enough_reds", debug

        # The structural pivot = the LOWEST low across the red series + the green bar
        # (the recent red-series low). Built from completed bars; the forming green bar's
        # low can only deepen it, so the stop only widens (never tightens) — INVARIANT-A safe.
        red_lo_start = cur - red_count   # first index of the red run
        try:
            series_low = float(low.iloc[red_lo_start:cur + 1].min())
        except (TypeError, ValueError, KeyError):
            return False, "bottom_reversal_no_low", debug
        if not (series_low > 0):
            return False, "bottom_reversal_bad_low", debug
        debug["pullback_low"] = float(series_low)

        # ── ATR% yardstick (tick-thrust + volume floors) ───────────────────────────────
        # Request vwap too so the NOT-PARABOLIC extension guard's VWAP arm gets a REAL series
        # (compute_all_from_df only computes what is requested — an un-requested vwap would
        # silently no-op the extension VWAP arm, a chase hole). Mirrors absorption_snap / cup.
        arrays = compute_all_from_df(
            df, needed={"ema_9", "ema_20", "macd", "macd_signal", "vwap", "volume_ratio", "atr"}
        )
        ema9 = arrays.get("ema_9") or []
        vwap = arrays.get("vwap") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None

        # ── (5) OPTIONAL exhaustion signal: a DOJI / bottoming-tail at the LOW. A doji =
        # a small body relative to the bar range (the ATR-derived EMA-wick tolerance is the
        # ONE documented body-fraction base, reused — no fresh magic). Recorded, NEVER
        # required (Ross uses it as a confirmer, not a gate). The "low" candle is the LAST
        # red bar of the series (the bottom of the flush) OR the green bar itself if it
        # printed a bottoming tail. ──────────────────────────────────────────────────────
        _, ema_wick, _ = _vol_aware_pullback_tolerances(atr_pct, 0.0)
        doji_confirmed = False
        try:
            # the bar at the bottom of the flush = the last red bar (just before cur)
            bidx = cur - 1
            b_o, b_h, b_l, b_c = (
                float(opn.iloc[bidx]), float(high.iloc[bidx]),
                float(low.iloc[bidx]), float(close.iloc[bidx]),
            )
            b_rng = b_h - b_l
            if b_rng > 0:
                body_frac = abs(b_c - b_o) / b_rng
                # small body (doji) OR a dominant lower wick (bottoming tail) = exhaustion
                if body_frac <= max(0.20, float(ema_wick)) or _bottoming_tail(b_o, b_h, b_l, b_c):
                    doji_confirmed = True
            # the green confirmation bar with a bottoming tail also counts as exhaustion
            if _bottoming_tail(c_o, c_h, c_l, c_c):
                doji_confirmed = True
        except (TypeError, ValueError, IndexError):
            doji_confirmed = False
        debug["doji_confirmed"] = bool(doji_confirmed)

        # ── (6) VOLUME confirmation on the GREEN bar (a real reclaim, not a dead-tape
        # green dribble). Pass if the green bar's RVOL >= the spike multiple OR the recent
        # RVOL floor is elevated. ───────────────────────────────────────────────────────
        vol_mult = float(
            getattr(settings, "chili_momentum_bottom_reversal_volume_spike_multiple", 1.5) or 1.5
        )
        debug["volume_spike_multiple"] = vol_mult
        vol_ratio = None
        try:
            vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        if vol_ratio is None:
            w = vol.tail(21)
            _avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
            vol_ratio = (float(vol.iloc[-1]) / _avg) if _avg > 0 else 0.0
        debug["volume_spike_ratio"] = round(vol_ratio, 2)
        if vol_ratio < vol_mult:
            return False, "bottom_reversal_low_volume", debug

        # Level = the green-bar HIGH (the break-above level Ross enters on; also the live
        # tick-watch level). The completed green CLOSE is recorded for reference.
        level = c_h
        debug["green_close"] = round(c_c, 6)
        if not (0.0 < series_low < level):
            return False, "bottom_reversal_bad_level", debug
        debug["pullback_high"] = float(level)

        # ── ANTI-CHASE backside veto (reused, fail-OPEN). A bottom reversal is counter-trend
        # by nature, but the backside veto here guards the OTHER failure mode the transcript
        # warns about: a green pop on TERRIBLE-news selling that is just a bear-flag relief
        # before continued downside — the rolled-over MACD/EMA structure flags exactly that. ─
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "bottom_reversal_back_side", debug

        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"bottom_reversal_{_reason}", debug

        # ── NOT PARABOLIC: extension vs the 9-EMA AND VWAP (the blow-off / knife-catch
        # defense — mirrors absorption_snap). A snap-back into a parabolic prior-high is a
        # chase: the green-bar break LEVEL (= the entry) must not sit excessively extended
        # above the 9-EMA / VWAP. Reuses the SAME adaptive ATR extension yardstick the chase
        # veto uses (fail-OPEN on a missing reference so thin data never blocks). ──────────
        ema9_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        _ext_ok, _ext_dbg = _hod_extension_ok(
            level=level, ema9_cur=ema9_cur, vwap_cur=vwap_cur, atr_pct=atr_pct, rvol=None,
        )
        if not _ext_ok:
            debug.update(_ext_dbg)
            return False, "bottom_reversal_extended", debug

        # ── OPTIONAL VELOCITY REFINEMENT (the "jackknife" sharp-V delta): gate the flush to be
        # a sharp V (a violent algo-stop-run that snaps back), NOT a slow grind down. Reuses the
        # EXISTING _dip_velocity yardstick (per-bar % drop relative to the name's OWN ATR%; a
        # drop is "steep" when the per-bar move exceeds ~1 ATR%). Require flush_roc_per_bar >=
        # k*atr_pct, where k = the config floor. floor=0.0 ⇒ OFF ⇒ byte-identical current
        # behavior. fail-OPEN (no gate) on missing atr%/degenerate data. ───────────────────
        vel_floor = float(getattr(settings, "chili_momentum_bottom_reversal_velocity_floor_atr_mult", 0.0) or 0.0)
        if vel_floor > 0.0 and atr_pct is not None and atr_pct > 0:
            try:
                # the red-run START price = the open of the first red bar (top of the flush);
                # the flush depth = (start - series_low) / start, spread over the red bars =
                # a per-bar ROC measured the SAME way _dip_velocity_size_mult reads it.
                _start_px = float(opn.iloc[red_lo_start])
                if _start_px > 0 and red_count > 0:
                    flush_roc_per_bar = ((_start_px - float(series_low)) / _start_px) / float(red_count)
                    debug["flush_roc_per_bar"] = round(flush_roc_per_bar, 6)
                    debug["velocity_floor_atr_mult"] = round(vel_floor, 3)
                    if flush_roc_per_bar < vel_floor * float(atr_pct):
                        return False, "bottom_reversal_flush_too_slow", debug
            except (TypeError, ValueError, IndexError, ZeroDivisionError):
                pass  # fail-OPEN: a velocity-read error never blocks the fire

        # ── TICK-BREAK: the live ask breaking the green bar's HIGH with the ATR thrust
        # buffer (the SAME contract the rest of the ladder uses). ──────────────────────
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate; no fire without buyers on tape) ──
            # Mirrors absorption_snap: buyers must be actively lifting the ask THIS tick. Any
            # disabled-flag / no-tape / thin / stale / crypto / error ⇒ NO fire (never chase a
            # snap-back into a dead bottom).
            _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
            debug["tape_reason"] = _tape_dbg.get("reason")
            if not _tape_ok:
                return False, "bottom_reversal_tape_unconfirmed", debug
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "bottom_reversal_tick_ok", debug

        # Not yet broken the green-bar high on live price -> ARM a tick-watch at that level
        # (pre-close tick-armable on pullback_high), unless price already cleared it on a
        # completed bar (the green CLOSE itself confirms the reversal -> fire now).
        if live_price is not None and float(live_price) > 0 and float(live_price) <= level:
            return False, "waiting_for_break", debug

        # ── TAPE REQUIRED + FAIL-CLOSED (the LAST gate before the completed-bar fire too) ──
        # The green candle has closed, but still require buyers on tape — a green close on a
        # dead tape is a knife-catch, not a reversal. Mirrors absorption_snap; fail-CLOSED.
        _tape_ok, _tape_dbg = tape_confirms_hold(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["tape_reason"] = _tape_dbg.get("reason")
        if not _tape_ok:
            return False, "bottom_reversal_tape_unconfirmed", debug

        # Completed-bar confirmation: the green candle has closed (the first-green-close
        # confirmation Ross enters on). Fire.
        return True, "bottom_reversal", debug
    except Exception:
        return False, "bottom_reversal_error", {"entry_interval": entry_interval}


def micro_pullback_primary_confirmation(
    df: pd.DataFrame,
    *,
    entry_interval: str,
    live_price: float | None = None,
    symbol: str | None = None,
    now: Any = None,
    db: Any = None,
    l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """MICRO-PULLBACK AS PRIMARY (Batch D; flag ``chili_momentum_micro_pullback_primary_enabled``).

    A thin wrapper that lets the 1-candle shallow micro-pullback flag fire as an INITIAL
    entry (not just a post-fill re-load). It reuses ``micro_pullback_reentry_detect``'s shelf/
    dip geometry on the SAME entry-interval frame the rest of the ladder uses, GATED to HOT/
    explosive tape via ``_is_hot_tape`` (the SAME RVOL/ATR floors as the wick-reclaim) so it
    cannot over-fire on slow names — the micro-pullback is a parabolic-runner re-load shape,
    invalid on cold tape.

    The SHELF (the ratcheting higher-low floor the re-load path persists in the session ledger)
    is not available as a primary entry, so it is derived from the COMPLETED-bar structure:
    the recent swing low (a confirmed structural floor) — the dip must hold ABOVE it, the same
    higher-low contract. Entry = the micro-break (the bounce high = ``pullback_high``); stop =
    the micro-pullback dip low (= ``pullback_low``).

    NO LOOKAHEAD: the geometry is read from completed bars (the detector excludes a high that
    is the last bar); the only intrabar use is the live tick breaking the bounce high (the
    SAME tick-break contract the rest of the ladder uses).

    ADDITIVE: flag OFF / cold tape / thin (<10 bars) / no micro-pullback -> ``(False, reason,
    {...})`` with NO side effects; fail-OPEN on any error. docs/DESIGN/MOMENTUM_LANE.md"""
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "micro_pullback_primary"}
    try:
        if not bool(getattr(settings, "chili_momentum_micro_pullback_primary_enabled", True)):
            return False, "micro_primary_disabled", debug
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return False, "micro_primary_insufficient_bars", debug
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        n = len(df)
        cur = n - 1

        # ── MANDATORY HOT-TAPE GATE (the micro-pullback re-load is invalid on slow tape) ─
        arrays = compute_all_from_df(df, needed={"ema_9", "ema_20", "macd", "macd_signal", "volume_ratio", "atr"})
        ema9 = arrays.get("ema_9") or []
        vr = arrays.get("volume_ratio") or []
        atr = arrays.get("atr") or []
        atr_pct = None
        try:
            _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
            _p = float(close.iloc[cur])
            if _a is not None and _p > 0:
                atr_pct = _a / _p
        except (TypeError, ValueError, IndexError):
            atr_pct = None
        rvol = None
        try:
            rvol = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
        except (TypeError, ValueError):
            rvol = None
        if not _is_hot_tape(atr_pct, rvol):
            debug["atr_pct"] = atr_pct
            debug["rvol"] = rvol
            return False, "micro_primary_cold_tape", debug
        debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None
        debug["rvol"] = round(rvol, 2) if rvol is not None else None

        # ── SHELF (the higher-low floor): the recent CONFIRMED swing low (a structural
        # floor the dip must hold above). On a primary entry there is no persisted re-load
        # shelf, so the completed-bar swing low is the conservative higher-low contract.
        # Fall back to the recent-window min low when no confirmed swing exists yet.
        shelf = _compute_confirmed_swing_low_last(df, lookback=5)
        if shelf is None or shelf <= 0:
            try:
                shelf = float(df["Low"].astype(float).iloc[max(0, cur - 8):cur].min())
            except (TypeError, ValueError, KeyError):
                shelf = 0.0
        debug["shelf"] = round(float(shelf), 6) if shelf else None

        # ── REUSE the micro-pullback geometry detector (shelf/dip/curl) ────────────────
        max_dip = float(
            getattr(settings, "chili_momentum_micropullback_reentry_max_dip_pct", 0.04) or 0.04
        )
        _det = micro_pullback_reentry_detect(df, shelf=float(shelf), max_dip_pct=max_dip)
        debug["detect_reason"] = _det.get("reason")
        if not _det.get("fire"):
            return False, f"micro_primary_{_det.get('reason') or 'no_fire'}", debug
        from .candles import bounce_curl_from_df

        if not bounce_curl_from_df(df):
            return False, "micro_primary_weak_curl", debug

        bounce_high = _det.get("bounce_high")
        dip_low = _det.get("dip_low")
        if bounce_high is None or dip_low is None:
            return False, "micro_primary_no_levels", debug
        level = float(bounce_high)   # the micro-break level
        stop = float(dip_low)        # the micro-pullback low = structural stop
        if not (0.0 < stop < level):
            return False, "micro_primary_bad_level", debug

        # ── ANTI-CHASE backside veto (reused, fail-OPEN) ───────────────────────────────
        _bs, _bs_reason = _detect_back_side(
            ema9, (arrays.get("ema_20") or []),
            (arrays.get("macd") or []), (arrays.get("macd_signal") or []),
            cur, macd_lookback=_BACKSIDE_MACD_CROSS_LOOKBACK,
        )
        if _bs:
            debug["back_side"] = _bs_reason
            return False, "micro_primary_back_side", debug

        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of, is_ssr=None)
        if _l2v is not None:
            _reason, _l2patch = _l2v
            debug.update(_l2patch)
            return False, f"micro_primary_{_reason}", debug

        debug["pullback_high"] = float(level)
        debug["pullback_low"] = float(stop)
        debug["bounce_high"] = round(level, 6)
        debug["dip_low"] = round(stop, 6)

        # ── UNIFIED BUYERS-CONFIRM (2026-07-04): this hot-tape touch trigger fired on the
        # bounce-high GEOMETRY alone (RVOL/ATR + curl + hidden-seller veto) with NO check that
        # buyers are actually lifting — the whack-a-mole sibling of wick_reclaim. Require the
        # SAME crypto-safe buyers gate before either fire path. Gates the fire only; a valid
        # geometry with no buyers yet just re-evaluates next tick (wait for the lift).
        _b_ok, _b_dbg = buyers_confirmed(symbol, db=db, settings=settings, l2_as_of=l2_as_of)
        debug["buyers_confirm"] = _b_dbg.get("reason") if isinstance(_b_dbg, dict) else None
        if not _b_ok:
            return False, "micro_primary_buyers_unconfirmed", debug

        # ── TICK-BREAK: the live ask trading through the micro-break (bounce high) ──────
        if (
            live_price is not None and float(live_price) > 0
            and float(live_price) > level
            and _premarket_tickbreak_confirmed(
                live_price=float(live_price), level=float(level),
                atr_pct=atr_pct, symbol=symbol, now=now,
            )
            and _dipbuy_tick_thrust_ok(
                live_price=float(live_price), level=float(level), atr_pct=atr_pct,
            )
        ):
            debug["tick_break"] = True
            debug["live_price"] = float(live_price)
            return True, "micro_pullback_primary_tick_ok", debug

        # not broken on a completed bar yet -> ARM a tick-watch at the micro-break level.
        cur_hi = float(high.iloc[cur])
        if cur_hi <= level:
            return False, "waiting_for_break", debug
        return True, "micro_pullback_primary", debug
    except Exception:
        return False, "micro_primary_error", {"entry_interval": entry_interval}


def momentum_pullback_trigger(
    df: pd.DataFrame, *, entry_interval: str, live_price: float | None = None,
    symbol: str | None = None, now: Any = None, db: Any = None, l2_as_of: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """The Ross pullback-break trigger resolved from live settings — the SINGLE
    source BOTH the live runner and the paper runner call, so the two paths make
    the IDENTICAL entry decision (the dual-path parity contract). Reads every
    ``chili_momentum_*`` entry knob and runs ``pullback_break_confirmation``;
    returns its ``(ok, reason, debug)`` (debug carries ``pullback_low`` /
    ``pullback_high`` on a fire). Centralizing this is what keeps paper a true
    shadow of live — previously paper used the legacy ``momentum_volume`` gate and
    the brain trained on a strategy that wasn't live. docs/DESIGN/MOMENTUM_LANE.md
    """
    return pullback_break_confirmation(
        df,
        entry_interval=entry_interval,
        live_price=live_price,
        symbol=symbol,
        now=now,
        db=db,
        l2_as_of=l2_as_of,
        first_pullback_interval=str(
            getattr(settings, "chili_momentum_first_pullback_interval", "1m") or "1m"
        ),
        volume_spike_multiple=float(
            getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5) or 1.5
        ),
        require_retest=bool(getattr(settings, "chili_momentum_pullback_require_retest", True)),
        retest_tolerance=float(getattr(settings, "chili_momentum_pullback_retest_tolerance", 0.002) or 0.0),
        retest_lookback_bars=int(getattr(settings, "chili_momentum_pullback_retest_lookback_bars", 4) or 4),
        require_sustained_volume=bool(
            getattr(settings, "chili_momentum_entry_require_sustained_volume", True)
        ),
        sustained_rvol_floor=float(getattr(settings, "chili_momentum_entry_sustained_rvol_floor", 1.0) or 0.0),
        sustain_lookback_bars=int(getattr(settings, "chili_momentum_entry_sustain_lookback_bars", 5) or 5),
        require_break_candle=bool(getattr(settings, "chili_momentum_entry_require_break_candle", True)),
        break_candle_min_close_pos=float(
            getattr(settings, "chili_momentum_entry_break_candle_min_close_pos", 0.50) or 0.50
        ),
        require_vwap_hold=bool(getattr(settings, "chili_momentum_entry_require_vwap_hold", True)),
        vwap_hold_buffer=float(getattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0) or 0.0),
        require_macd_bullish=bool(getattr(settings, "chili_momentum_entry_require_macd_bullish", True)),
        allow_runaway_break=bool(getattr(settings, "chili_momentum_entry_allow_runaway_break", True)),
        runaway_min_volume_spike=float(
            getattr(settings, "chili_momentum_entry_runaway_min_volume_spike", 2.0) or 2.0
        ),
    )


def halt_resume_dip_trigger(
    df: pd.DataFrame,
    *,
    entry_interval: str = "1m",
    halt_resumed_at_utc: Any,
    now: Any = None,
    halt_level: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross's halt-resume DIP BUY (2026-06-10 DSY +$20k leg: "it drops and on the
    resumption I bought the dip").

    After a halt resumes, price discovery is violent: a pop (or flush), then the
    FIRST dip that stabilizes and curls back up is the entry — the generic
    pullback-break needs more bars than the move gives it, and the resume cooldown
    sat the lane out entirely (DSY 06-10: armed at rank #1 all day, zero entries).
    This trigger demands STRUCTURE, not a market-chase at the resume tick:

      1. RECENCY — only within ``chili_momentum_halt_resume_dip_window_seconds``
         of the resume; past it the normal trigger ladder owns the tape.
      2. A REAL DIP off the post-resume reference high — depth at least the
         ATR%-scaled noise floor (not jitter) and at most the volatility-scaled
         deep cap (not a collapse). All bounds derive from the instrument's own
         ATR%; the absolute floors only guard the thin-data case.
      3. STABILIZATION + RECLAIM — the entry bar makes no new low under the dip
         and closes back above the prior bar's high with conviction
         (``is_strong_bull_break_candle``: topping-tail/doji "reclaims" rejected).
      4. VOLUME still carries the move (sustained rel-vol, fails open on thin
         data — the shared `_sustained_rvol` semantics).

    Returns the shared ``(ok, reason, debug)`` 3-tuple; on fire, ``debug`` carries
    ``pullback_low`` (the dip low = structural stop) and ``pullback_high`` (the
    post-resume reference high = breakout-or-bailout level) under the SAME keys as
    the pullback-break trigger, so sizing, stop placement, and the fast-bailout
    machinery are reused unchanged in live, paper, and replay.

    ``halt_level`` (GAP 2 / GAP 3, Warrior re-audit): the price at the moment the name
    HALTED (captured by the live runner at halt detection). Default ``None`` keeps the
    behaviour byte-identical. When supplied AND the relevant flag is ON, the resumption
    open (first post-resume bar open) is compared to ``halt_level``: a WEAK resume
    (opens below) is a FALSE halt → no-fire (``chili_momentum_false_halt_avoid_enabled``);
    a directional read (``chili_momentum_halt_resumption_direction_enabled``) annotates
    ``debug['resumption_size_mult']`` for the runner to scale entry size by. The modifier
    NEVER enables an entry or loosens a gate — risk-reducing / conviction only.
    """
    debug: dict[str, Any] = {"entry_interval": entry_interval, "pattern": "halt_resume_dip"}
    if df is None or getattr(df, "empty", True) or len(df) < 3:
        return False, "resume_dip_insufficient_bars", debug
    try:
        resumed = pd.Timestamp(halt_resumed_at_utc)
        if resumed.tzinfo is None:
            resumed = resumed.tz_localize("UTC")
        else:
            resumed = resumed.tz_convert("UTC")
    except Exception:
        return False, "resume_dip_bad_resume_ts", debug
    now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    window_s = float(getattr(settings, "chili_momentum_halt_resume_dip_window_seconds", 600.0) or 600.0)
    age_s = (now_ts - resumed).total_seconds()
    debug["resume_age_seconds"] = round(age_s, 1)
    if age_s < 0 or age_s > window_s:
        return False, "resume_dip_window_passed", debug

    idx = df.index
    try:
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
    except Exception:
        return False, "resume_dip_bad_index", debug
    post = df.loc[idx >= resumed]
    debug["bars_post_resume"] = int(len(post))
    if len(post) < 3:
        return False, "resume_dip_forming", debug

    # GAP 2 + GAP 3 (Warrior re-audit) — HALT-RESUMPTION PRICE-DIRECTION.
    # The deployed trigger reads ONLY post-resume bars and never the halt_level (the
    # price at the moment the name halted). When the caller passes halt_level AND the
    # relevant flag is ON, compare the resumption open (the OPEN of the first post-resume
    # bar) vs halt_level:
    #   • GAP 3 FALSE-HALT (chili_momentum_false_halt_avoid_enabled): a limit-UP halt
    #     that resumes WEAK — resumption_open BELOW halt_level — is a FALSE halt (the
    #     limit-up move did not hold through the auction). AVOID: return a no-fire. This
    #     runs BEFORE all the dip/reclaim structure checks, so a weak resume never even
    #     reaches the fire path. Pure risk-reduction (only ever adds a no-fire).
    #   • GAP 2 CONVICTION (chili_momentum_halt_resumption_direction_enabled): annotate
    #     debug with resumption_direction ('higher'/'lower'/'flat') + a bounded
    #     resumption_size_mult the live runner applies to entry size (HIGHER ⇒ small
    #     boost, LOWER ⇒ penalty). It is ONLY a size annotation — it never enables an
    #     entry on its own and never loosens a gate.
    # Both flags default OFF and halt_level defaults None ⇒ this whole block is skipped
    # ⇒ byte-identical. Fail-OPEN on any error (no annotation, no veto).
    _hr_dir_on = bool(getattr(settings, "chili_momentum_halt_resumption_direction_enabled", False))
    _hr_false_on = bool(getattr(settings, "chili_momentum_false_halt_avoid_enabled", False))
    try:
        _hl = float(halt_level) if (halt_level is not None and (_hr_dir_on or _hr_false_on)) else None
        if _hl is not None and _hl > 0:
            _resume_open = float(post["Open"].astype(float).iloc[0])
            debug["halt_level"] = round(_hl, 6)
            debug["resumption_open"] = round(_resume_open, 6)
            _resume_dir = (
                "higher" if _resume_open > _hl * (1.0 + 1e-9)
                else "lower" if _resume_open < _hl * (1.0 - 1e-9)
                else "flat"
            )
            # GAP 3 — FALSE-HALT REVERSAL avoid (limit-UP halt resuming below the halt
            # price = the up-move failed to hold). Only ever turns a would-fire into a
            # no-fire; never enables.
            if _hr_false_on and _resume_dir == "lower":
                debug["resumption_direction"] = _resume_dir
                debug["false_halt"] = True
                return False, "resume_dip_false_halt_resume_weak", debug
            # GAP 2 — conviction-size modifier annotation (does NOT gate; applied to size
            # by the live runner only).
            if _hr_dir_on:
                try:
                    _frac = float(getattr(settings, "chili_momentum_halt_resumption_boost_frac", 0.15) or 0.15)
                except (TypeError, ValueError):
                    _frac = 0.15
                _frac = max(0.0, min(_frac, 0.5))
                _mult = (
                    1.0 + _frac if _resume_dir == "higher"
                    else 1.0 - _frac if _resume_dir == "lower"
                    else 1.0
                )
                debug["resumption_direction"] = _resume_dir
                debug["resumption_size_mult"] = round(_mult, 4)
    except Exception:
        pass

    high = post["High"].astype(float)
    low = post["Low"].astype(float)
    close = post["Close"].astype(float)
    opn = post["Open"].astype(float)

    ref_pos = int(high.values.argmax())
    ref_high = float(high.iloc[ref_pos])
    debug["pullback_high"] = ref_high
    if ref_pos >= len(post) - 1 or ref_high <= 0:
        return False, "resume_dip_forming", debug  # still pumping — no dip yet

    after = post.iloc[ref_pos + 1:]
    if len(after) < 2:
        return False, "resume_dip_forming", debug  # need at least a dip bar + a reclaim bar
    # the dip is measured BEFORE the candidate entry bar — the entry bar must HOLD it
    dip_low = float(after["Low"].astype(float).iloc[:-1].min())
    debug["pullback_low"] = dip_low
    dip_depth = (ref_high - dip_low) / ref_high
    debug["dip_depth_pct"] = round(dip_depth * 100.0, 2)

    # Volatility-relative bounds from the FULL frame's ATR% (same source as the
    # pullback trigger); absolute floors only protect the thin-data case.
    atr_pct = None
    try:
        arrays = compute_all_from_df(df, needed={"atr", "volume_ratio"})
        atr = arrays.get("atr") or []
        cur = len(df) - 1
        _a = float(atr[cur]) if cur < len(atr) and atr[cur] is not None else None
        _p = float(df["Close"].astype(float).iloc[-1])
        if _a is not None and _p > 0:
            atr_pct = _a / _p
    except Exception:
        arrays = {}
        atr_pct = None
    noise_floor = max(0.003, 0.5 * (atr_pct or 0.01))
    deep_cap = _collapse_cap(atr_pct)
    debug["atr_pct"] = round(atr_pct, 5) if atr_pct is not None else None
    debug["noise_floor_pct"] = round(noise_floor * 100.0, 2)
    debug["deep_cap_pct"] = round(deep_cap * 100.0, 2)
    if dip_depth < noise_floor:
        return False, "resume_dip_too_shallow", debug
    if dip_depth > deep_cap:
        return False, "resume_dip_too_deep", debug

    last_o, last_h = float(opn.iloc[-1]), float(high.iloc[-1])
    last_l, last_c = float(low.iloc[-1]), float(close.iloc[-1])
    prev_h = float(high.iloc[-2])
    if last_l < dip_low * (1.0 - 1e-9) or last_c <= prev_h:
        return False, "resume_dip_no_reclaim", debug
    from .candles import is_strong_bull_break_candle

    if not is_strong_bull_break_candle(
        last_o, last_h, last_l, last_c,
        min_close_pos=float(getattr(settings, "chili_momentum_entry_break_candle_min_close_pos", 0.50) or 0.50),
    ):
        return False, "resume_dip_weak_candle", debug

    vr = arrays.get("volume_ratio") or []
    srv = _sustained_rvol(vr, len(df) - 1, lookback=3)
    debug["sustained_rvol"] = round(srv, 2) if srv is not None else None
    if srv is not None and srv < 1.0:
        return False, "resume_dip_volume_faded", debug

    return True, "halt_resume_dip_ok", debug


def run_paper_entry_gates(
    db: Session,
    *,
    symbol: str,
    variant: MomentumStrategyVariant | None,
    regime_snapshot: dict[str, Any],
    family_id: str | None,
) -> tuple[bool, str, dict[str, Any]]:
    """Returns (allowed, reason_code, debug_dict)."""
    if not bool(getattr(settings, "chili_momentum_entry_gates_enabled", True)):
        return True, "gates_disabled", {}

    sym = (symbol or "").strip().upper()
    try:
        df = fetch_ohlcv_df(sym, interval="15m", period="5d")
    except Exception as ex:
        _log.debug("[entry_gates] ohlcv failed %s: %s", sym, ex)
        return False, "ohlcv_fetch_failed", {"error": str(ex)}
    if df is None or df.empty or len(df) < 30:
        return False, "ohlcv_insufficient", {"rows": 0 if df is None else len(df)}

    meta = regime_snapshot.get("meta") if isinstance(regime_snapshot.get("meta"), dict) else {}
    atr_top = regime_snapshot.get("atr_pct")
    if atr_top is None:
        atr_top = meta.get("atr_pct")
    try:
        atr_f = float(atr_top) if atr_top is not None else None
    except (TypeError, ValueError):
        atr_f = None

    chop = str(regime_snapshot.get("chop_expansion") or meta.get("chop_expansion") or "")
    vreg = str(regime_snapshot.get("volatility_regime") or "")

    ok_r, reason_r = regime_entry_allowed(family_id, atr_pct=atr_f, chop_expansion=chop, vol_regime=vreg)
    if not ok_r:
        return False, reason_r, {"regime": True}

    ok_fr, reason_fr = family_regime_prefilter_allows(db, family_id=family_id or "", regime_snapshot=regime_snapshot)
    if not ok_fr:
        return False, reason_fr, {"family_regime": True}

    ok_p, reason_p = evaluate_pattern_conditions_for_variant(db, variant, df)
    if not ok_p:
        return False, reason_p, {"pattern": True}

    # Trigger PARITY with the live runner: the Ross pullback-break on the entry
    # interval (vol-aware shallow/EMA, candle/VWAP/MACD, runaway) via the SHARED
    # helper — NOT the legacy momentum_volume gate — so paper shadows live and the
    # brain trains on the strategy that actually trades. The structural stop
    # (pullback_low) + breakout level (pullback_high) ride out in the debug so the
    # paper stop can mirror live's structural stop. docs/DESIGN/MOMENTUM_LANE.md
    _interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
    try:
        df_entry = fetch_ohlcv_df(sym, interval=_interval, period="5d")
    except Exception as ex:
        _log.debug("[entry_gates] entry-interval ohlcv failed %s: %s", sym, ex)
        df_entry = None
    if df_entry is None or getattr(df_entry, "empty", True):
        return False, "no_entry_data", {"interval": _interval}
    ok_t, reason_t, pb = momentum_pullback_trigger(df_entry, entry_interval=_interval, symbol=sym, db=db)
    if not ok_t:
        return False, reason_t, {"trigger": True, "interval": _interval}

    debug = {
        "bars": len(df_entry),
        "pattern": reason_p,
        "trigger": reason_t,
        "regime": reason_r,
        "pullback_low": pb.get("pullback_low"),
        "pullback_high": pb.get("pullback_high"),
    }
    return True, "all_gates_pass", debug
