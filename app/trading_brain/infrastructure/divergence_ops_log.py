"""Structured one-line ops log for the divergence panel (Phase K).

Emitted when a divergence sweep evaluates a pattern, persists a
divergence row, and when the service refuses to run in authoritative
mode. Release blockers assert that no ``mode=authoritative`` line
appears until Phase K.2 is explicitly opened.

Log prefix: ``[divergence_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_DIVERGENCE_OPS_PREFIX = "[divergence_ops]"


def format_divergence_ops_line(
    *,
    event: str,  # "divergence_evaluated" | "divergence_persisted" | "divergence_refused_authoritative" | "divergence_skipped"
    mode: str,
    divergence_id: str | None = None,
    scan_pattern_id: int | None = None,
    pattern_name: str | None = None,
    severity: str | None = None,
    score: float | None = None,
    layers_sampled: int | None = None,
    layers_agreed: int | None = None,
    as_of_key: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry."""
    parts: list[str] = [
        CHILI_DIVERGENCE_OPS_PREFIX,
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

    _add("divergence_id", divergence_id)
    _add("scan_pattern_id", scan_pattern_id)
    _add("pattern_name", pattern_name)
    _add("severity", severity)
    _add("score", score)
    _add("layers_sampled", layers_sampled)
    _add("layers_agreed", layers_agreed)
    _add("as_of_key", as_of_key)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_DIVERGENCE_OPS_PREFIX",
    "format_divergence_ops_line",
]
