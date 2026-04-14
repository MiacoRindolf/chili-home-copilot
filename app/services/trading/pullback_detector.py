"""RSI + Fib 0.382 + FVG pullback continuation detector.

Orchestrates the reusable Fibonacci, FVG, and cross-timeframe modules into a
single detection pipeline that returns structured evidence suitable for
persistence, learning, and UI display.

This module is the **pattern-specific orchestrator**; the building blocks it
calls (``fibonacci``, ``fvg``, ``cross_timeframe``) are fully reusable by
future patterns.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: dict[str, Any] = {
    "htf": "1d",
    "ltf": "1h",
    "htf_rsi_threshold": 75,
    "ltf_rsi_threshold": 50,
    "fib_target": 0.382,
    "fib_tolerance_pct": 0.5,
    "fvg_fib_overlap_tolerance_pct": 0.5,
    "impulse_lookback": 50,
    "fvg_lookback": 20,
    "pivot_lookback": 5,
    "direction": "bull",
    "max_staleness_seconds": 86400,
}


def detect_rsi_fib_fvg_pullback(
    ticker: str,
    *,
    htf: str | None = None,
    ltf: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run the full RSI + Fib + FVG pullback detection for *ticker*.

    Returns a structured evidence dict on match, or ``None`` if the setup
    is not present.  The evidence dict contains every field required by the
    persistence / learning / UI contracts.

    Sequence enforced:
        1. Impulse leg forms (LTF)
        2. HTF strength exists (RSI > threshold)
        3. Pullback begins
        4. Price revisits Fib 0.382 zone
        5. FVG overlaps / confirms near the zone
        6. LTF RSI remains supportive (> threshold)
    """
    from .cross_timeframe import fetch_cross_timeframe_evidence
    from .fibonacci import find_impulse_leg, compute_fib_levels, check_fib_level_hit
    from .fvg import detect_fvg_records, check_fvg_fib_confluence
    from .market_data import fetch_ohlcv_df

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    _htf = htf or cfg["htf"]
    _ltf = ltf or cfg["ltf"]

    evidence = fetch_cross_timeframe_evidence(
        ticker, htf=_htf, ltf=_ltf,
        max_staleness_seconds=cfg["max_staleness_seconds"],
    )

    if evidence.fetch_error:
        logger.debug("[pullback_detector] fetch error for %s: %s", ticker, evidence.fetch_error)
        return None

    htf_rsi = evidence.htf_indicators.get("rsi_14")
    if htf_rsi is None or htf_rsi <= cfg["htf_rsi_threshold"]:
        return None

    ltf_rsi = evidence.ltf_indicators.get("rsi_14")
    if ltf_rsi is None or ltf_rsi <= cfg["ltf_rsi_threshold"]:
        return None

    _ltf_period_map = {
        "5m": "5d", "15m": "14d", "1h": "30d", "4h": "60d", "1d": "6mo",
    }
    ltf_period = _ltf_period_map.get(_ltf, "30d")

    try:
        ltf_df = fetch_ohlcv_df(ticker, period=ltf_period, interval=_ltf)
    except Exception:
        return None

    if ltf_df is None or ltf_df.empty or len(ltf_df) < 30:
        return None

    h = ltf_df["High"]
    l = ltf_df["Low"]
    c = ltf_df["Close"]

    leg = find_impulse_leg(
        h, l, c,
        direction=cfg["direction"],
        lookback=cfg["impulse_lookback"],
        pivot_lookback=cfg["pivot_lookback"],
    )
    if leg is None:
        return None

    fib_prices = compute_fib_levels(
        leg["end_price"], leg["start_price"],
        levels=(cfg["fib_target"],),
    )
    fib_price = fib_prices[cfg["fib_target"]]

    current_price = float(c.iloc[-1])
    if not check_fib_level_hit(current_price, fib_price, cfg["fib_tolerance_pct"]):
        return None

    if leg["end_idx"] >= len(h) - 1:
        return None

    fvg_records = detect_fvg_records(h, l)
    if cfg["direction"] == "bull":
        fvg_records = [r for r in fvg_records if r["direction"] == "bull"]
    else:
        fvg_records = [r for r in fvg_records if r["direction"] == "bear"]

    confluent_fvg = None
    for rec in reversed(fvg_records):
        if rec["bar_idx"] < leg["end_idx"]:
            continue
        if check_fvg_fib_confluence(
            rec["fvg_high"], rec["fvg_low"], fib_price,
            cfg["fvg_fib_overlap_tolerance_pct"],
        ):
            confluent_fvg = rec
            break

    if confluent_fvg is None:
        return None

    return {
        "pattern": "rsi_fib_fvg_pullback",
        "ticker": ticker,
        "side": cfg["direction"],
        "htf": _htf,
        "ltf": _ltf,
        "htf_rsi": round(htf_rsi, 2),
        "htf_rsi_threshold": cfg["htf_rsi_threshold"],
        "htf_timestamp": evidence.htf_last_timestamp,
        "ltf_rsi": round(ltf_rsi, 2),
        "ltf_rsi_threshold": cfg["ltf_rsi_threshold"],
        "ltf_timestamp": evidence.ltf_last_timestamp,
        "impulse_high": leg["end_price"],
        "impulse_low": leg["start_price"],
        "impulse_bars": leg["bars"],
        "impulse_start_idx": leg["start_idx"],
        "impulse_end_idx": leg["end_idx"],
        "fib_target_level": cfg["fib_target"],
        "fib_level_price": round(fib_price, 6),
        "fib_tolerance_pct": cfg["fib_tolerance_pct"],
        "current_price": round(current_price, 6),
        "fvg_high": confluent_fvg["fvg_high"],
        "fvg_low": confluent_fvg["fvg_low"],
        "fvg_direction": confluent_fvg["direction"],
        "fvg_bar_idx": confluent_fvg["bar_idx"],
        "fvg_fib_overlap_tolerance_pct": cfg["fvg_fib_overlap_tolerance_pct"],
        "coherence_ok": evidence.coherence_ok,
        "evidence_age_seconds": evidence.evidence_age_seconds,
        "detected_at": time.time(),
        "config": cfg,
        "reasons": [
            f"HTF ({_htf}) RSI {htf_rsi:.1f} > {cfg['htf_rsi_threshold']}",
            f"LTF ({_ltf}) RSI {ltf_rsi:.1f} > {cfg['ltf_rsi_threshold']}",
            f"Impulse leg {leg['start_price']:.2f} → {leg['end_price']:.2f} ({leg['bars']} bars)",
            f"Price {current_price:.2f} at Fib {cfg['fib_target']} level {fib_price:.2f} (±{cfg['fib_tolerance_pct']}%)",
            f"FVG [{confluent_fvg['fvg_low']:.2f}–{confluent_fvg['fvg_high']:.2f}] confluent with Fib zone",
        ],
    }
