"""Reusable cross-timeframe same-ticker evidence assembler.

Complements ``mtf_consensus.py`` (which checks the *same* conditions across
all timeframes) by supporting **asymmetric** condition sets — e.g. HTF RSI > 75
while LTF RSI > 50 — and returning structured evidence with timing-coherence
guarantees.

Usage::

    evidence = fetch_cross_timeframe_evidence("AAPL", htf="1d", ltf="1h")
    result = eval_cross_timeframe_conditions(
        evidence,
        htf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 75}],
        ltf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 50}],
    )
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger(__name__)

_HTF_PERIOD_MAP: dict[str, str] = {
    "5m": "5d", "15m": "14d", "1h": "30d", "4h": "60d",
    "1d": "6mo", "1wk": "2y",
}


@dataclass
class CrossTimeframeEvidence:
    """Structured evidence from two timeframes for the same ticker."""

    ticker: str
    htf: str
    ltf: str
    htf_indicators: dict[str, Any] = field(default_factory=dict)
    ltf_indicators: dict[str, Any] = field(default_factory=dict)
    htf_last_timestamp: str | None = None
    ltf_last_timestamp: str | None = None
    coherence_ok: bool = False
    evidence_age_seconds: float | None = None
    fetch_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_cross_timeframe_evidence(
    ticker: str,
    htf: str = "1d",
    ltf: str = "1h",
    htf_period: str | None = None,
    ltf_period: str | None = None,
    *,
    max_staleness_seconds: float = 86400,
    needed: set[str] | None = None,
) -> CrossTimeframeEvidence:
    """Fetch OHLCV for *ticker* at two timeframes and compute indicators.

    Parameters
    ----------
    ticker : str
        The symbol (e.g. ``"AAPL"`` or ``"BTC-USD"``).
    htf / ltf : str
        Higher / lower timeframe intervals (e.g. ``"1d"`` / ``"1h"``).
    htf_period / ltf_period : str | None
        History depth overrides; defaults come from ``_HTF_PERIOD_MAP``.
    max_staleness_seconds : float
        Maximum allowed age (in seconds) of the most recent bar before the
        evidence is flagged as stale (``coherence_ok = False``).
    needed : set[str] | None
        Indicator keys to compute; ``None`` → compute all.

    Returns
    -------
    CrossTimeframeEvidence
    """
    from .market_data import fetch_ohlcv_df
    from .indicator_core import compute_all_from_df

    ev = CrossTimeframeEvidence(ticker=ticker, htf=htf, ltf=ltf)

    h_period = htf_period or _HTF_PERIOD_MAP.get(htf, "6mo")
    l_period = ltf_period or _HTF_PERIOD_MAP.get(ltf, "30d")

    try:
        htf_df = fetch_ohlcv_df(ticker, period=h_period, interval=htf)
        ltf_df = fetch_ohlcv_df(ticker, period=l_period, interval=ltf)
    except Exception as exc:
        ev.fetch_error = str(exc)
        return ev

    if htf_df is None or htf_df.empty or ltf_df is None or ltf_df.empty:
        ev.fetch_error = "empty dataframe for one or both timeframes"
        return ev

    htf_arrays = compute_all_from_df(htf_df, needed=needed)
    ltf_arrays = compute_all_from_df(ltf_df, needed=needed)

    def _last_values(arrays: dict[str, list]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, arr in arrays.items():
            if arr and len(arr) > 0:
                val = arr[-1]
                if val is not None:
                    out[k] = val
        return out

    ev.htf_indicators = _last_values(htf_arrays)
    ev.ltf_indicators = _last_values(ltf_arrays)

    if hasattr(htf_df.index, 'strftime'):
        ev.htf_last_timestamp = str(htf_df.index[-1])
    if hasattr(ltf_df.index, 'strftime'):
        ev.ltf_last_timestamp = str(ltf_df.index[-1])

    now = time.time()
    try:
        import pandas as _pd
        htf_ts = _pd.Timestamp(htf_df.index[-1]).timestamp()
        ltf_ts = _pd.Timestamp(ltf_df.index[-1]).timestamp()
        age = now - min(htf_ts, ltf_ts)
        ev.evidence_age_seconds = round(age, 1)
        ev.coherence_ok = age <= max_staleness_seconds
    except Exception:
        ev.coherence_ok = True

    return ev


def eval_cross_timeframe_conditions(
    evidence: CrossTimeframeEvidence,
    htf_conditions: list[dict[str, Any]],
    ltf_conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate *different* condition sets against the HTF and LTF indicator snapshots.

    Returns::

        {
            "htf_pass": bool,
            "ltf_pass": bool,
            "all_pass": bool,
            "coherence_ok": bool,
            "htf_results": [{"condition": ..., "pass": bool}, ...],
            "ltf_results": [...],
        }
    """
    from .pattern_engine import _eval_condition

    def _run(conditions: list[dict], indicators: dict) -> tuple[bool, list[dict]]:
        results = []
        all_ok = True
        for cond in conditions:
            ok = _eval_condition(cond, indicators)
            results.append({"condition": cond, "pass": ok})
            if not ok:
                all_ok = False
        return all_ok, results

    htf_pass, htf_results = _run(htf_conditions, evidence.htf_indicators)
    ltf_pass, ltf_results = _run(ltf_conditions, evidence.ltf_indicators)

    return {
        "htf_pass": htf_pass,
        "ltf_pass": ltf_pass,
        "all_pass": htf_pass and ltf_pass and evidence.coherence_ok,
        "coherence_ok": evidence.coherence_ok,
        "htf_results": htf_results,
        "ltf_results": ltf_results,
    }


def build_cross_tf_snapshot_keys(
    evidence: CrossTimeframeEvidence,
) -> dict[str, Any]:
    """Flatten cross-TF evidence into prefixed indicator keys for snapshot injection.

    HTF indicators get ``"{htf}:"`` prefix; LTF indicators are returned
    unprefixed (matching the pattern's native timeframe convention).
    """
    snap: dict[str, Any] = {}
    for k, v in evidence.htf_indicators.items():
        snap[f"{evidence.htf}:{k}"] = v
    for k, v in evidence.ltf_indicators.items():
        snap[k] = v
    return snap
