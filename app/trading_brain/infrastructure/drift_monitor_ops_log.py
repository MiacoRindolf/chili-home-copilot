"""Structured one-line ops log for the drift monitor (Phase J).

Emitted by the daily APScheduler drift-monitor sweep and the manual
diagnostics path. Release blockers assert that no
``mode=authoritative`` line appears until Phase J.2 is explicitly
opened.

Log prefix: ``[drift_monitor_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_DRIFT_MONITOR_OPS_PREFIX = "[drift_monitor_ops]"


def format_drift_monitor_ops_line(
    *,
    event: str,  # "drift_computed" | "drift_persisted" | "drift_refused_authoritative"
    mode: str,
    drift_id: str | None = None,
    scan_pattern_id: int | None = None,
    pattern_name: str | None = None,
    severity: str | None = None,
    sample_size: int | None = None,
    baseline_win_prob: float | None = None,
    observed_win_prob: float | None = None,
    brier_delta: float | None = None,
    cusum_statistic: float | None = None,
    cusum_threshold: float | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry.

    The format is deterministic so downstream grep / release-blocker
    scripts can rely on ``event=<e>`` and ``mode=<m>`` always appearing
    in the first two tokens after the prefix.
    """
    parts: list[str] = [
        CHILI_DRIFT_MONITOR_OPS_PREFIX,
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

    _add("drift_id", drift_id)
    _add("scan_pattern_id", scan_pattern_id)
    _add("pattern_name", pattern_name)
    _add("severity", severity)
    _add("sample_size", sample_size)
    _add("baseline_win_prob", baseline_win_prob)
    _add("observed_win_prob", observed_win_prob)
    _add("brier_delta", brier_delta)
    _add("cusum_statistic", cusum_statistic)
    _add("cusum_threshold", cusum_threshold)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_DRIFT_MONITOR_OPS_PREFIX",
    "format_drift_monitor_ops_line",
]
