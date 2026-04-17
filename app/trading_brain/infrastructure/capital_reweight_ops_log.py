"""Structured one-line ops log for the weekly capital re-weighter (Phase I).

Emitted by the APScheduler ``capital_reweight_weekly`` job when the
sweep computes a proposed allocation, persists it, or refuses to run
in authoritative mode. Release blockers assert that no
``mode=authoritative`` line appears until Phase I.2 is explicitly
opened.

Log prefix: ``[capital_reweight_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_CAPITAL_REWEIGHT_OPS_PREFIX = "[capital_reweight_ops]"


def format_capital_reweight_ops_line(
    *,
    event: str,  # "sweep_computed" | "sweep_persisted" | "sweep_refused_authoritative"
    mode: str,
    reweight_id: str | None = None,
    user_id: int | None = None,
    as_of_date: str | None = None,
    regime: str | None = None,
    total_capital: float | None = None,
    mean_drift_bps: float | None = None,
    p90_drift_bps: float | None = None,
    bucket_count: int | None = None,
    single_bucket_cap_triggered: bool | None = None,
    concentration_cap_triggered: bool | None = None,
    bucket_resized: bool | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry.

    The format is deterministic so downstream grep / release-blocker
    scripts can rely on ``event=<e>`` and ``mode=<m>`` always appearing
    in the first two tokens after the prefix.
    """
    parts: list[str] = [
        CHILI_CAPITAL_REWEIGHT_OPS_PREFIX,
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

    _add("reweight_id", reweight_id)
    _add("user_id", user_id)
    _add("as_of_date", as_of_date)
    _add("regime", regime)
    _add("total_capital", total_capital)
    _add("mean_drift_bps", mean_drift_bps)
    _add("p90_drift_bps", p90_drift_bps)
    _add("bucket_count", bucket_count)
    _add("single_bucket_cap_triggered", single_bucket_cap_triggered)
    _add("concentration_cap_triggered", concentration_cap_triggered)
    _add("bucket_resized", bucket_resized)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_CAPITAL_REWEIGHT_OPS_PREFIX",
    "format_capital_reweight_ops_line",
]
