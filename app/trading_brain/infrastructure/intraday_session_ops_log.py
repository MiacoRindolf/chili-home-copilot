"""Structured one-line ops log for the intraday session regime sweep
(Phase L.22).

Emitted when the daily ``intraday_session_daily`` scheduler sweep
computes, persists, refuses, or skips a snapshot. Release blockers
assert that no ``mode=authoritative`` line appears until Phase L.22.2
is explicitly opened.

Log prefix: ``[intraday_session_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_INTRADAY_SESSION_OPS_PREFIX = "[intraday_session_ops]"


def format_intraday_session_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "intraday_session_computed"
    #   "intraday_session_persisted"
    #   "intraday_session_refused_authoritative"
    #   "intraday_session_skipped"
    mode: str,
    snapshot_id: str | None = None,
    as_of_date: str | None = None,
    source_symbol: str | None = None,
    # Anchors
    open_price: float | None = None,
    close_price: float | None = None,
    session_high: float | None = None,
    session_low: float | None = None,
    session_range_pct: float | None = None,
    # Gap
    prev_close: float | None = None,
    gap_open_pct: float | None = None,
    # OR
    or_high: float | None = None,
    or_low: float | None = None,
    or_range_pct: float | None = None,
    or_volume_ratio: float | None = None,
    # Midday
    midday_range_pct: float | None = None,
    midday_compression_ratio: float | None = None,
    # Power hour
    ph_range_pct: float | None = None,
    ph_volume_ratio: float | None = None,
    close_vs_or_mid_pct: float | None = None,
    # Vol
    intraday_rv: float | None = None,
    # Composite
    session_label: str | None = None,
    session_numeric: int | None = None,
    # Coverage
    bars_observed: int | None = None,
    coverage_score: float | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line whitespace-tokenised ops log entry."""
    parts: list[str] = [
        CHILI_INTRADAY_SESSION_OPS_PREFIX,
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
    _add("source_symbol", source_symbol)
    _add("open_price", open_price)
    _add("close_price", close_price)
    _add("session_high", session_high)
    _add("session_low", session_low)
    _add("session_range_pct", session_range_pct)
    _add("prev_close", prev_close)
    _add("gap_open_pct", gap_open_pct)
    _add("or_high", or_high)
    _add("or_low", or_low)
    _add("or_range_pct", or_range_pct)
    _add("or_volume_ratio", or_volume_ratio)
    _add("midday_range_pct", midday_range_pct)
    _add("midday_compression_ratio", midday_compression_ratio)
    _add("ph_range_pct", ph_range_pct)
    _add("ph_volume_ratio", ph_volume_ratio)
    _add("close_vs_or_mid_pct", close_vs_or_mid_pct)
    _add("intraday_rv", intraday_rv)
    _add("session_label", session_label)
    _add("session_numeric", session_numeric)
    _add("bars_observed", bars_observed)
    _add("coverage_score", coverage_score)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_INTRADAY_SESSION_OPS_PREFIX",
    "format_intraday_session_ops_line",
]
