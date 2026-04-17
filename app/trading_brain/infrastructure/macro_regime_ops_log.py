"""Structured one-line ops log for the macro regime sweep (Phase L.17).

Emitted when a macro regime sweep computes, persists, refuses, or skips
a snapshot. Release blockers assert that no ``mode=authoritative`` line
appears until Phase L.17.2 is explicitly opened.

Log prefix: ``[macro_regime_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_MACRO_REGIME_OPS_PREFIX = "[macro_regime_ops]"


def format_macro_regime_ops_line(
    *,
    event: str,  # "macro_regime_computed" | "macro_regime_persisted" | "macro_regime_refused_authoritative" | "macro_regime_skipped"
    mode: str,
    regime_id: str | None = None,
    as_of_date: str | None = None,
    macro_label: str | None = None,
    macro_numeric: int | None = None,
    rates_regime: str | None = None,
    credit_regime: str | None = None,
    usd_regime: str | None = None,
    symbols_sampled: int | None = None,
    symbols_missing: int | None = None,
    coverage_score: float | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry."""
    parts: list[str] = [
        CHILI_MACRO_REGIME_OPS_PREFIX,
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

    _add("regime_id", regime_id)
    _add("as_of_date", as_of_date)
    _add("macro_label", macro_label)
    _add("macro_numeric", macro_numeric)
    _add("rates_regime", rates_regime)
    _add("credit_regime", credit_regime)
    _add("usd_regime", usd_regime)
    _add("symbols_sampled", symbols_sampled)
    _add("symbols_missing", symbols_missing)
    _add("coverage_score", coverage_score)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_MACRO_REGIME_OPS_PREFIX",
    "format_macro_regime_ops_line",
]
