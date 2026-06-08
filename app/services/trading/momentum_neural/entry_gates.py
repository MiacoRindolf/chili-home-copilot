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


def _evaluate_raw_break(
    high: pd.Series,
    low: pd.Series,
    ema9: list[Any],
    cur: int,
    *,
    entry_interval: str,
    max_pullback_bars: int,
    retracement_threshold: float,
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

    # Shallow: must not retrace more than the threshold of the impulse range.
    retrace = (win_high - pb_low) / impulse_range
    debug["retrace"] = round(retrace, 3)
    if retrace > float(retracement_threshold):
        return False, "pullback_too_deep", None, None, debug

    # Held above EMA-9 (structural support) during the pullback.
    ema_cur = ema9[cur] if cur < len(ema9) and ema9[cur] is not None else None
    if ema_cur is not None and pb_low < float(ema_cur) * 0.999:
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

    retrace = (win_high - base_low) / impulse_range
    debug["retrace"] = round(retrace, 3)
    if retrace > float(retracement_threshold):
        return False, "pullback_too_deep", None, None, debug

    # EMA-9 support is checked at the BASE (when the consolidation formed), not at
    # the current bar — a strong continuation after the break lifts the current EMA
    # above the older base low, which would otherwise reject a valid retest.
    ema_idx = base_end - 1
    ema_base = ema9[ema_idx] if 0 <= ema_idx < len(ema9) and ema9[ema_idx] is not None else None
    if ema_base is not None and base_low < float(ema_base) * 0.999:
        debug["ema_9"] = float(ema_base)
        return False, "pullback_below_ema9", None, None, debug

    tol = max(0.0, float(retest_tolerance))

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
    arrays = compute_all_from_df(df, needed={"ema_9", "volume_ratio"})
    ema9 = arrays.get("ema_9") or []
    vr = arrays.get("volume_ratio") or []

    # Trigger: break-and-retest (#1) when enabled, else the classic first break.
    if require_retest:
        ok_t, reason_t, pb_high, pb_low, debug = _evaluate_break_retest(
            high, low, close, ema9, cur,
            entry_interval=entry_interval,
            max_pullback_bars=max_pullback_bars,
            retracement_threshold=retracement_threshold,
            retest_tolerance=retest_tolerance,
            retest_lookback_bars=retest_lookback_bars,
        )
    else:
        ok_t, reason_t, pb_high, pb_low, debug = _evaluate_raw_break(
            high, low, ema9, cur,
            entry_interval=entry_interval,
            max_pullback_bars=max_pullback_bars,
            retracement_threshold=retracement_threshold,
        )
    if not ok_t:
        return False, reason_t, debug

    # Volume spike on the trigger (break / reclaim) bar.
    vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
    if vol_ratio is None:
        w = vol.tail(21)
        avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
        vol_ratio = (float(vol.iloc[-1]) / avg) if avg > 0 else 0.0
    debug["vol_ratio"] = round(vol_ratio, 2)
    if vol_ratio < float(volume_spike_multiple):
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

    ok_m, reason_m = momentum_volume_confirmation(df)
    if not ok_m:
        return False, reason_m, {"momentum": True}

    debug = {"bars": len(df), "pattern": reason_p, "momentum": reason_m, "regime": reason_r}
    return True, "all_gates_pass", debug
