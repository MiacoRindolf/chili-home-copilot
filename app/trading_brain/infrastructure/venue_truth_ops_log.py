"""Structured one-line ops log for venue-truth telemetry (Phase F).

Mirrors the format used by `economic_ledger_ops_log`, `pit_ops_log`,
`triple_barrier_ops_log`, and `net_edge_ranker_ops_log` so release-blocker
greps share a consistent grammar.

Emitted once per fill observation (shadow or authoritative) plus once per
diagnostics rollup.

Log prefix: `[venue_truth_ops]`.
"""
from __future__ import annotations

from typing import Any

CHILI_VENUE_TRUTH_OPS_PREFIX = "[venue_truth_ops]"


def format_venue_truth_ops_line(
    *,
    event: str,  # "fill_observation" | "summary"
    mode: str,
    ticker: str | None = None,
    side: str | None = None,
    trade_id: int | None = None,
    notional_usd: float | None = None,
    expected_spread_bps: float | None = None,
    realized_spread_bps: float | None = None,
    expected_slippage_bps: float | None = None,
    realized_slippage_bps: float | None = None,
    expected_cost_fraction: float | None = None,
    realized_cost_fraction: float | None = None,
    cost_gap_bps: float | None = None,
    paper_bool: bool | None = None,
    observations_total: int | None = None,
    mean_gap_bps: float | None = None,
    p90_gap_bps: float | None = None,
    **extra: Any,
) -> str:
    """Build a single-line structured log entry.

    Pairs are space-separated ``key=value``. Strings with spaces are
    double-quoted. ``None`` values are omitted so grep patterns stay stable.
    """
    parts: list[str] = [CHILI_VENUE_TRUTH_OPS_PREFIX, f"event={event}", f"mode={mode}"]

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
    _add("trade_id", trade_id)
    _add("notional_usd", notional_usd)
    _add("expected_spread_bps", expected_spread_bps)
    _add("realized_spread_bps", realized_spread_bps)
    _add("expected_slippage_bps", expected_slippage_bps)
    _add("realized_slippage_bps", realized_slippage_bps)
    _add("expected_cost_fraction", expected_cost_fraction)
    _add("realized_cost_fraction", realized_cost_fraction)
    _add("cost_gap_bps", cost_gap_bps)
    _add("paper_bool", paper_bool)
    _add("observations_total", observations_total)
    _add("mean_gap_bps", mean_gap_bps)
    _add("p90_gap_bps", p90_gap_bps)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_VENUE_TRUTH_OPS_PREFIX",
    "format_venue_truth_ops_line",
]
