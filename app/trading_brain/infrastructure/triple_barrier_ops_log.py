"""Structured one-line ops log for the triple-barrier labeler (Phase D).

Mirrors the format used by `economic_ledger_ops_log`, `pit_ops_log`, and
`net_edge_ranker_ops_log` so release-blocker greps share a consistent
grammar. Emitted once per label write (shadow or authoritative) plus once
per labeler invocation for a summary line.

Log prefix: `[triple_barrier_ops]`.
"""
from __future__ import annotations

from typing import Any

CHILI_TRIPLE_BARRIER_OPS_PREFIX = "[triple_barrier_ops]"


def format_triple_barrier_ops_line(
    *,
    event: str,  # e.g. "label_write" | "run_summary"
    mode: str,
    ticker: str | None = None,
    label_date: str | None = None,
    side: str | None = None,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    max_bars: int | None = None,
    label: int | None = None,
    barrier_hit: str | None = None,
    exit_bar_idx: int | None = None,
    realized_return_pct: float | None = None,
    snapshot_id: int | None = None,
    labels_total: int | None = None,
    labels_tp: int | None = None,
    labels_sl: int | None = None,
    labels_timeout: int | None = None,
    labels_missing: int | None = None,
    **extra: Any,
) -> str:
    """Build a single-line structured log entry.

    Pairs are space-separated ``key=value``. Strings with spaces are
    double-quoted. ``None`` values are omitted so grep patterns are stable.
    """
    parts: list[str] = [CHILI_TRIPLE_BARRIER_OPS_PREFIX, f"event={event}", f"mode={mode}"]

    def _add(k: str, v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str):
            if any(c.isspace() for c in v) or v == "":
                parts.append(f'{k}="{v}"')
            else:
                parts.append(f"{k}={v}")
        elif isinstance(v, bool):
            parts.append(f"{k}={str(v).lower()}")
        elif isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")

    _add("ticker", ticker)
    _add("label_date", label_date)
    _add("side", side)
    _add("tp_pct", tp_pct)
    _add("sl_pct", sl_pct)
    _add("max_bars", max_bars)
    _add("label", label)
    _add("barrier_hit", barrier_hit)
    _add("exit_bar_idx", exit_bar_idx)
    _add("realized_return_pct", realized_return_pct)
    _add("snapshot_id", snapshot_id)
    _add("labels_total", labels_total)
    _add("labels_tp", labels_tp)
    _add("labels_sl", labels_sl)
    _add("labels_timeout", labels_timeout)
    _add("labels_missing", labels_missing)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_TRIPLE_BARRIER_OPS_PREFIX",
    "format_triple_barrier_ops_line",
]
