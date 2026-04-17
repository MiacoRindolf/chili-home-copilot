"""Structured one-line ops log for the execution-cost model (Phase F).

Mirrors the format used by `venue_truth_ops_log`, `pit_ops_log`, etc.
Emitted whenever an estimate is upserted by `execution_cost_builder` or
when the cost model is invoked in shadow / authoritative mode.

Log prefix: `[execution_cost_ops]`.
"""
from __future__ import annotations

from typing import Any

CHILI_EXECUTION_COST_OPS_PREFIX = "[execution_cost_ops]"


def format_execution_cost_ops_line(
    *,
    event: str,  # "estimate_write" | "cost_query" | "summary"
    mode: str,
    ticker: str | None = None,
    side: str | None = None,
    window_days: int | None = None,
    median_spread_bps: float | None = None,
    p90_spread_bps: float | None = None,
    median_slippage_bps: float | None = None,
    p90_slippage_bps: float | None = None,
    avg_daily_volume_usd: float | None = None,
    sample_trades: int | None = None,
    cost_fraction: float | None = None,
    notional_usd: float | None = None,
    capacity_usd: float | None = None,
    estimates_total: int | None = None,
    **extra: Any,
) -> str:
    parts: list[str] = [
        CHILI_EXECUTION_COST_OPS_PREFIX,
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

    _add("ticker", ticker)
    _add("side", side)
    _add("window_days", window_days)
    _add("median_spread_bps", median_spread_bps)
    _add("p90_spread_bps", p90_spread_bps)
    _add("median_slippage_bps", median_slippage_bps)
    _add("p90_slippage_bps", p90_slippage_bps)
    _add("avg_daily_volume_usd", avg_daily_volume_usd)
    _add("sample_trades", sample_trades)
    _add("cost_fraction", cost_fraction)
    _add("notional_usd", notional_usd)
    _add("capacity_usd", capacity_usd)
    _add("estimates_total", estimates_total)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_EXECUTION_COST_OPS_PREFIX",
    "format_execution_cost_ops_line",
]
