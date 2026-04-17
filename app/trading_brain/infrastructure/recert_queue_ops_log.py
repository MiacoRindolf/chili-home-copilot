"""Structured one-line ops log for the re-cert proposal queue (Phase J).

Emitted when a drift-monitor sweep produces a proposal, when a user
manually queues a re-cert, and when the service refuses to run in
authoritative mode. Release blockers assert that no
``mode=authoritative`` line appears until Phase J.2 is explicitly
opened.

Log prefix: ``[recert_queue_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_RECERT_QUEUE_OPS_PREFIX = "[recert_queue_ops]"


def format_recert_queue_ops_line(
    *,
    event: str,  # "recert_proposed" | "recert_persisted" | "recert_refused_authoritative" | "recert_skipped"
    mode: str,
    recert_id: str | None = None,
    scan_pattern_id: int | None = None,
    pattern_name: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    status: str | None = None,
    drift_log_id: int | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry."""
    parts: list[str] = [
        CHILI_RECERT_QUEUE_OPS_PREFIX,
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

    _add("recert_id", recert_id)
    _add("scan_pattern_id", scan_pattern_id)
    _add("pattern_name", pattern_name)
    _add("severity", severity)
    _add("source", source)
    _add("status", status)
    _add("drift_log_id", drift_log_id)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_RECERT_QUEUE_OPS_PREFIX",
    "format_recert_queue_ops_line",
]
