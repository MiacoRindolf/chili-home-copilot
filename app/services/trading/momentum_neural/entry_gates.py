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
