"""Structured one-line ops log for the volatility / dispersion snapshot
sweep (Phase L.21).

Emitted when the daily ``vol_dispersion_daily`` scheduler sweep
computes, persists, refuses, or skips a snapshot. Release blockers
assert that no ``mode=authoritative`` line appears until Phase L.21.2
is explicitly opened.

Log prefix: ``[vol_dispersion_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_VOL_DISPERSION_OPS_PREFIX = "[vol_dispersion_ops]"


def format_vol_dispersion_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "vol_dispersion_computed"
    #   "vol_dispersion_persisted"
    #   "vol_dispersion_refused_authoritative"
    #   "vol_dispersion_skipped"
    mode: str,
    snapshot_id: str | None = None,
    as_of_date: str | None = None,
    # VIX term structure
    vixy_close: float | None = None,
    vixm_close: float | None = None,
    vxz_close: float | None = None,
    vix_slope_4m_1m: float | None = None,
    vix_slope_7m_1m: float | None = None,
    # SPY realised vol
    spy_realized_vol_5d: float | None = None,
    spy_realized_vol_20d: float | None = None,
    spy_realized_vol_60d: float | None = None,
    vix_realized_gap: float | None = None,
    # Dispersion + correlation
    cross_section_return_std_5d: float | None = None,
    cross_section_return_std_20d: float | None = None,
    mean_abs_corr_20d: float | None = None,
    corr_sample_size: int | None = None,
    sector_leadership_churn_20d: float | None = None,
    # Composite labels
    vol_regime_label: str | None = None,
    vol_regime_numeric: int | None = None,
    dispersion_label: str | None = None,
    dispersion_numeric: int | None = None,
    correlation_label: str | None = None,
    correlation_numeric: int | None = None,
    # Coverage
    universe_size: int | None = None,
    tickers_missing: int | None = None,
    coverage_score: float | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry.

    Keys are ``snake_case``; string values that contain whitespace (or
    are empty) are double-quoted. Floats are rendered with ``%.6g`` to
    keep the line narrow and grep-stable.
    """
    parts: list[str] = [
        CHILI_VOL_DISPERSION_OPS_PREFIX,
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
    _add("vixy_close", vixy_close)
    _add("vixm_close", vixm_close)
    _add("vxz_close", vxz_close)
    _add("vix_slope_4m_1m", vix_slope_4m_1m)
    _add("vix_slope_7m_1m", vix_slope_7m_1m)
    _add("spy_realized_vol_5d", spy_realized_vol_5d)
    _add("spy_realized_vol_20d", spy_realized_vol_20d)
    _add("spy_realized_vol_60d", spy_realized_vol_60d)
    _add("vix_realized_gap", vix_realized_gap)
    _add("cross_section_return_std_5d", cross_section_return_std_5d)
    _add("cross_section_return_std_20d", cross_section_return_std_20d)
    _add("mean_abs_corr_20d", mean_abs_corr_20d)
    _add("corr_sample_size", corr_sample_size)
    _add("sector_leadership_churn_20d", sector_leadership_churn_20d)
    _add("vol_regime_label", vol_regime_label)
    _add("vol_regime_numeric", vol_regime_numeric)
    _add("dispersion_label", dispersion_label)
    _add("dispersion_numeric", dispersion_numeric)
    _add("correlation_label", correlation_label)
    _add("correlation_numeric", correlation_numeric)
    _add("universe_size", universe_size)
    _add("tickers_missing", tickers_missing)
    _add("coverage_score", coverage_score)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_VOL_DISPERSION_OPS_PREFIX",
    "format_vol_dispersion_ops_line",
]
