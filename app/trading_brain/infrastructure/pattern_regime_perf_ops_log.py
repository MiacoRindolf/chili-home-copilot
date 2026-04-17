"""Structured one-line ops log for the pattern x regime performance
ledger sweep (Phase M.1).

Emitted when the daily ``pattern_regime_perf_daily`` scheduler sweep
computes, persists, refuses, or skips a ledger run. Release blockers
assert that no ``mode=authoritative`` line appears until Phase M.2 is
explicitly opened.

Log prefix: ``[pattern_regime_perf_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_PATTERN_REGIME_PERF_OPS_PREFIX = "[pattern_regime_perf_ops]"


def format_pattern_regime_perf_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "pattern_regime_perf_computed"
    #   "pattern_regime_perf_persisted"
    #   "pattern_regime_perf_refused_authoritative"
    #   "pattern_regime_perf_skipped"
    mode: str,
    as_of_date: str | None = None,
    ledger_run_id: str | None = None,
    window_days: int | None = None,
    min_trades_per_cell: int | None = None,
    max_patterns: int | None = None,
    pattern_count: int | None = None,
    trade_count: int | None = None,
    cell_count: int | None = None,
    confident_cells: int | None = None,
    unavailable_cells: int | None = None,
    truncated_patterns: int | None = None,
    dimensions_count: int | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line whitespace-tokenised ops log entry."""
    parts: list[str] = [
        CHILI_PATTERN_REGIME_PERF_OPS_PREFIX,
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

    _add("as_of_date", as_of_date)
    _add("ledger_run_id", ledger_run_id)
    _add("window_days", window_days)
    _add("min_trades_per_cell", min_trades_per_cell)
    _add("max_patterns", max_patterns)
    _add("pattern_count", pattern_count)
    _add("trade_count", trade_count)
    _add("cell_count", cell_count)
    _add("confident_cells", confident_cells)
    _add("unavailable_cells", unavailable_cells)
    _add("truncated_patterns", truncated_patterns)
    _add("dimensions_count", dimensions_count)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_PATTERN_REGIME_PERF_OPS_PREFIX",
    "format_pattern_regime_perf_ops_line",
]
