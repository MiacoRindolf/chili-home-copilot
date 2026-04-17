"""Structured one-line ops log for the breadth + RS sweep (Phase L.18).

Emitted when a breadth + relative-strength sweep computes, persists,
refuses, or skips a snapshot. Release blockers assert that no
``mode=authoritative`` line appears until Phase L.18.2 is explicitly
opened.

Log prefix: ``[breadth_relstr_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_BREADTH_RELSTR_OPS_PREFIX = "[breadth_relstr_ops]"


def format_breadth_relstr_ops_line(
    *,
    event: str,  # "breadth_relstr_computed" | "breadth_relstr_persisted" | "breadth_relstr_refused_authoritative" | "breadth_relstr_skipped"
    mode: str,
    snapshot_id: str | None = None,
    as_of_date: str | None = None,
    breadth_label: str | None = None,
    breadth_numeric: int | None = None,
    advance_ratio: float | None = None,
    leader_sector: str | None = None,
    laggard_sector: str | None = None,
    size_tilt: float | None = None,
    style_tilt: float | None = None,
    symbols_sampled: int | None = None,
    symbols_missing: int | None = None,
    coverage_score: float | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry."""
    parts: list[str] = [
        CHILI_BREADTH_RELSTR_OPS_PREFIX,
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
    _add("breadth_label", breadth_label)
    _add("breadth_numeric", breadth_numeric)
    _add("advance_ratio", advance_ratio)
    _add("leader_sector", leader_sector)
    _add("laggard_sector", laggard_sector)
    _add("size_tilt", size_tilt)
    _add("style_tilt", style_tilt)
    _add("symbols_sampled", symbols_sampled)
    _add("symbols_missing", symbols_missing)
    _add("coverage_score", coverage_score)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_BREADTH_RELSTR_OPS_PREFIX",
    "format_breadth_relstr_ops_line",
]
