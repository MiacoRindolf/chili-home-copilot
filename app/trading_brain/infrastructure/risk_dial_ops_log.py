"""Structured one-line ops log for the canonical risk dial (Phase I).

Emitted whenever :mod:`app.services.trading.risk_dial_service` resolves
or persists a risk-dial value. Release blockers assert that no
``mode=authoritative`` line appears until Phase I.2 is explicitly
opened.

Log prefix: ``[risk_dial_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_RISK_DIAL_OPS_PREFIX = "[risk_dial_ops]"


def format_risk_dial_ops_line(
    *,
    event: str,  # "dial_resolved" | "dial_override_rejected" | "dial_persisted"
    mode: str,
    user_id: int | None = None,
    dial_value: float | None = None,
    regime: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    regime_default: float | None = None,
    drawdown_pct: float | None = None,
    drawdown_multiplier: float | None = None,
    override_multiplier: float | None = None,
    ceiling: float | None = None,
    capped_at_ceiling: bool | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry.

    The format is deterministic so downstream grep / release-blocker
    scripts can rely on ``event=<e>`` and ``mode=<m>`` always appearing
    in the first two tokens after the prefix.
    """
    parts: list[str] = [
        CHILI_RISK_DIAL_OPS_PREFIX,
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

    _add("user_id", user_id)
    _add("dial_value", dial_value)
    _add("regime", regime)
    _add("source", source)
    _add("reason", reason)
    _add("regime_default", regime_default)
    _add("drawdown_pct", drawdown_pct)
    _add("drawdown_multiplier", drawdown_multiplier)
    _add("override_multiplier", override_multiplier)
    _add("ceiling", ceiling)
    _add("capped_at_ceiling", capped_at_ceiling)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_RISK_DIAL_OPS_PREFIX",
    "format_risk_dial_ops_line",
]
