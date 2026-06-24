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
)


def _collapse_cap(atr_pct: float | None) -> float:
    """Volatility-relative max retrace depth that still reads as a PULLBACK —
    beyond it the move is a breakdown, not a dip to buy. Shared yardstick of the
    halt-resume dip trigger and the deep-retrace reclaim path (same Ross mechanic:
    buy the dip's reclaim, never a collapse)."""
    return min(0.25, max(0.06, 6.0 * (atr_pct or 0.01)))


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
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of)
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


def _l2_entry_veto(
    symbol: str | None, *, db: Any = None, l2_as_of: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """Gate 3 (dip-buy quality, flag-gated): L2 hidden-seller / big-seller veto.

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

        # (a) BIG-SELLER wall: the newest book is ask-heavy relative to its own recent
        #     window — depth-imbalance percentile at/below the floor. Self-relative
        #     percentile (no absolute threshold a single spoof can trip). Fail-open
        #     when the percentile is unavailable (too few snaps to rank).
        try:
            floor = float(getattr(settings, "chili_momentum_entry_l2_bigseller_pctile_floor", 0.15))
        except (TypeError, ValueError):
            floor = 0.15
        pct = getattr(lr, "depth_imbal_pctile", None)
        if pct is not None:
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


def _entry_flow_veto(
    ofi: float | None,
    trade_flow: float | None,
    settings: Any,
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
        if entry_price is None or breakout_level is None or atr_pct is None:
            return False  # missing level/atr -> never veto (parity)
        ep = float(entry_price)
        lvl = float(breakout_level)
        a = float(atr_pct)
        if lvl <= 0 or ep <= 0:
            return False  # bad level/price -> never veto (parity)
        k = float(getattr(settings, "chili_momentum_entry_extension_atr_mult", 8.0))
        floor = float(getattr(settings, "chili_momentum_entry_extension_floor_pct", 0.08))
        cap = max(floor, k * max(0.0, a))
        return ep >= lvl * (1.0 + cap)
    except Exception:
        return False  # any error -> fail-open (never block an entry on a bug)


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
        _l2v = _l2_entry_veto(symbol, db=db, l2_as_of=l2_as_of)
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
    if _fp_on and str(_fp_iv) == str(entry_interval):
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

    # Volume spike on the trigger (break / reclaim) bar.
    vol_ratio = float(vr[cur]) if cur < len(vr) and vr[cur] is not None else None
    if vol_ratio is None:
        w = vol.tail(21)
        avg = float(w.iloc[:-1].mean()) if len(w) > 1 else float(vol.iloc[-1])
        vol_ratio = (float(vol.iloc[-1]) / avg) if avg > 0 else 0.0
    debug["vol_ratio"] = round(vol_ratio, 2)

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
            if len(df) >= 1:
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
        if (_runaway or _deep_reclaim or _late_pullback)
        else float(volume_spike_multiple)
    )
    if not _tick_break and vol_ratio < _vol_floor:
        return False, "break_low_volume", debug

    # #3 Sustaining-volume gate (the ESTR guardrail): the move must STILL be carried
    # by volume at the entry tick — recent rel-vol above the floor — so a faded 24h
    # mover (hot at selection, dead by entry) is rejected. Self-relative per
    # instrument, so the floor is adaptive (a FLOOR the system can raise), not a
    # fixed magic count. Fails OPEN on thin data — EXCEPT for a deep reclaim:
    # a deep-retrace bounce with unknowable volume support is the textbook
    # dead-cat trap, so that one path fails CLOSED.
    if require_sustained_volume:
        sustained = _sustained_rvol(vr, cur, int(sustain_lookback_bars))
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

        if not is_strong_bull_break_candle(
            cur_o, cur_h, cur_l, cur_c, min_close_pos=float(break_candle_min_close_pos)
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
    if _vert_mult > 0:
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
    return True, ("pullback_break_tick_ok" if _tick_break else "pullback_break_ok"), debug


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
