"""Structured one-line ops log for the per-ticker regime sweep (Phase L.20).

Emitted when the daily ``ticker_regime_daily`` scheduler sweep computes,
persists, refuses, or skips a snapshot for a single ticker, and once per
sweep as a ``ticker_regime_sweep_summary`` aggregate line. Release
blockers assert that no ``mode=authoritative`` line appears until Phase
L.20.2 is explicitly opened.

Log prefix: ``[ticker_regime_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_TICKER_REGIME_OPS_PREFIX = "[ticker_regime_ops]"


def format_ticker_regime_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "ticker_regime_computed"
    #   "ticker_regime_persisted"
    #   "ticker_regime_refused_authoritative"
    #   "ticker_regime_skipped"
    #   "ticker_regime_sweep_summary"
    mode: str,
    snapshot_id: str | None = None,
    as_of_date: str | None = None,
    ticker: str | None = None,
    asset_class: str | None = None,
    ticker_regime_label: str | None = None,
    ticker_regime_numeric: int | None = None,
    ac1: float | None = None,
    vr_5: float | None = None,
    vr_20: float | None = None,
    hurst: float | None = None,
    adx_proxy: float | None = None,
    sigma_20d: float | None = None,
    trend_score: float | None = None,
    mean_revert_score: float | None = None,
    bars_used: int | None = None,
    bars_missing: int | None = None,
    coverage_score: float | None = None,
    # Sweep-summary fields.
    tickers_attempted: int | None = None,
    tickers_persisted: int | None = None,
    tickers_skipped: int | None = None,
    trend_up_count: int | None = None,
    trend_down_count: int | None = None,
    mean_revert_count: int | None = None,
    choppy_count: int | None = None,
    neutral_count: int | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry.

    Keys are ``snake_case``; string values that contain whitespace (or
    are empty) are double-quoted. Floats are rendered with ``%.6g`` to
    keep the line narrow and grep-stable.
    """
    parts: list[str] = [
        CHILI_TICKER_REGIME_OPS_PREFIX,
        f"event={event}",
        f"mode={mode}",
    ]

    def _add(k: str, v: Any) -> None:
        if v is None:
            return
        if isinstance(v, bool):
            parts.append(f"{k}={str(v).lower()}")
        elif isinstance(v, str):
            if any(c.isspace() for c in v) or v == "":
                parts.append(f'{k}="{v}"')
            else:
                parts.append(f"{k}={v}")
        elif isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")

    _add("snapshot_id", snapshot_id)
    _add("as_of_date", as_of_date)
    _add("ticker", ticker)
    _add("asset_class", asset_class)
    _add("ticker_regime_label", ticker_regime_label)
    _add("ticker_regime_numeric", ticker_regime_numeric)
    _add("ac1", ac1)
    _add("vr_5", vr_5)
    _add("vr_20", vr_20)
    _add("hurst", hurst)
    _add("adx_proxy", adx_proxy)
    _add("sigma_20d", sigma_20d)
    _add("trend_score", trend_score)
    _add("mean_revert_score", mean_revert_score)
    _add("bars_used", bars_used)
    _add("bars_missing", bars_missing)
    _add("coverage_score", coverage_score)
    _add("tickers_attempted", tickers_attempted)
    _add("tickers_persisted", tickers_persisted)
    _add("tickers_skipped", tickers_skipped)
    _add("trend_up_count", trend_up_count)
    _add("trend_down_count", trend_down_count)
    _add("mean_revert_count", mean_revert_count)
    _add("choppy_count", choppy_count)
    _add("neutral_count", neutral_count)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_TICKER_REGIME_OPS_PREFIX",
    "format_ticker_regime_ops_line",
]
