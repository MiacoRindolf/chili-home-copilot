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
        if avg_v <= 0:
            return False, "volume_avg_zero"
        if cur_v < 1.5 * avg_v:
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
    if avg_v <= 0 or cur_v < 1.5 * avg_v:
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

    retrace = (win_high - base_low) / impulse_range
    debug["retrace"] = round(retrace, 3)
    if retrace > eff_shallow:
        return False, "pullback_too_deep", None, None, debug

    # EMA-9 support is checked at the BASE (when the consolidation formed), not at
    # the current bar — a strong continuation after the break lifts the current EMA
    # above the older base low, which would otherwise reject a valid retest. Wick
    # tolerance scales with ATR% (small-cap noise below the 9-EMA isn't a break).
    ema_idx = base_end - 1
    ema_base = ema9[ema_idx] if 0 <= ema_idx < len(ema9) and ema9[ema_idx] is not None else None
    if ema_base is not None and base_low < float(ema_base) * (1.0 - ema_wick):
        debug["ema_9"] = float(ema_base)
        return False, "pullback_below_ema9", None, None, debug

    tol = max(0.0, float(retest_tolerance), vol_retest)

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
    n = len(df)
    cur = n - 1
    arrays = compute_all_from_df(
        df, needed={"ema_9", "volume_ratio", "atr", "vwap", "macd", "macd_signal", "macd_hist"}
    )
    ema9 = arrays.get("ema_9") or []
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
        )
    else:
        ok_t, reason_t, pb_high, pb_low, debug = _evaluate_raw_break(
            high, low, ema9, cur,
            entry_interval=entry_interval,
            max_pullback_bars=max_pullback_bars,
            retracement_threshold=retracement_threshold,
            atr_pct=atr_pct,
        )

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

    if not ok_t:
        return False, reason_t, debug

    # Volume spike on the trigger (break / reclaim) bar.
    vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
    if vol_ratio is None:
        w = vol.tail(21)
        avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
        vol_ratio = (float(vol.iloc[-1]) / avg) if avg > 0 else 0.0
    debug["vol_ratio"] = round(vol_ratio, 2)
    # Runaways need MORE conviction (chasing a break without a retest): raise the
    # volume floor to runaway_min_volume_spike for them; normal breaks keep the base.
    _vol_floor = float(runaway_min_volume_spike) if _runaway else float(volume_spike_multiple)
    if vol_ratio < _vol_floor:
        return False, "break_low_volume", debug

    # #3 Sustaining-volume gate (the ESTR guardrail): the move must STILL be carried
    # by volume at the entry tick — recent rel-vol above the floor — so a faded 24h
    # mover (hot at selection, dead by entry) is rejected. Self-relative per
    # instrument, so the floor is adaptive (a FLOOR the system can raise), not a
    # fixed magic count. Fails OPEN on thin data.
    if require_sustained_volume:
        sustained = _sustained_rvol(vr, cur, int(sustain_lookback_bars))
        if sustained is not None:
            debug["sustained_rvol"] = round(sustained, 2)
            if sustained < float(sustained_rvol_floor):
                return False, "faded_volume_no_sustain", debug

    # ── Ross candle / VWAP / MACD confirmations (the tape-reading the structural
    # gate alone misses; each optional + live-runner-gated, fail-OPEN so thin data
    # never blocks an otherwise-valid break). docs/DESIGN/MOMENTUM_LANE.md §8.
    cur_o = float(df["Open"].iloc[cur])
    cur_h, cur_l, cur_c = float(high.iloc[cur]), float(low.iloc[cur]), float(close.iloc[cur])

    # Conviction break candle: reject a doji / topping-tail "break" that wicks out.
    if require_break_candle:
        from .candles import is_strong_bull_break_candle

        if not is_strong_bull_break_candle(
            cur_o, cur_h, cur_l, cur_c, min_close_pos=float(break_candle_min_close_pos)
        ):
            debug["break_candle"] = {"o": cur_o, "h": cur_h, "l": cur_l, "c": cur_c}
            return False, "weak_break_candle", debug

    # VWAP hold: Ross stays long ABOVE VWAP. Skip when VWAP unavailable (fail-open).
    if require_vwap_hold:
        vwap_cur = vwap[cur] if cur < len(vwap) and vwap[cur] is not None else None
        if vwap_cur is not None and float(vwap_cur) > 0:
            debug["vwap"] = round(float(vwap_cur), 6)
            if cur_c < float(vwap_cur) * (1.0 - max(0.0, float(vwap_hold_buffer))):
                return False, "below_vwap", debug

    # MACD momentum confirmation (lenient: histogram >= 0 OR macd line >= signal).
    if require_macd_bullish:
        hh = macd_hist[cur] if cur < len(macd_hist) and macd_hist[cur] is not None else None
        m = macd[cur] if cur < len(macd) and macd[cur] is not None else None
        s = macd_sig[cur] if cur < len(macd_sig) and macd_sig[cur] is not None else None
        if hh is not None or (m is not None and s is not None):
            bullish = (hh is not None and float(hh) >= 0.0) or (
                m is not None and s is not None and float(m) >= float(s)
            )
            debug["macd_hist"] = None if hh is None else round(float(hh), 6)
            if not bullish:
                return False, "macd_not_bullish", debug

    return True, "pullback_break_ok", debug


def breakout_failed_to_hold(
    *,
    breakout_level: float | None,
    bid: float | None,
    held_seconds: float,
    window_seconds: float,
    buffer_pct: float = 0.001,
) -> bool:
    """#2 Breakout-or-bailout (Ross flat-top rule: "if the stock cannot hold the
    breakout level after entry, exit IMMEDIATELY" rather than waiting for the
    structural stop).

    Pure decision: within ``window_seconds`` of a pullback_break entry, return True
    when the bid has fallen back below the broken ``breakout_level`` (minus a small
    wick buffer) — a failed breakout to be cut well inside the structural stop.
    Guarded so it never fights the normal stop: no level / outside the early window
    / non-positive inputs all return False. docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        lvl = float(breakout_level) if breakout_level is not None else 0.0
        b = float(bid) if bid is not None else 0.0
        held = float(held_seconds)
        window = float(window_seconds)
    except (TypeError, ValueError):
        return False
    if lvl <= 0.0 or b <= 0.0 or window <= 0.0:
        return False
    if held > window:
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


def momentum_pullback_trigger(
    df: pd.DataFrame, *, entry_interval: str
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
    deep_cap = min(0.25, max(0.06, 6.0 * (atr_pct or 0.01)))
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
    ok_t, reason_t, pb = momentum_pullback_trigger(df_entry, entry_interval=_interval)
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
