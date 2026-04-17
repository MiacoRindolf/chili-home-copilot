"""Structured one-line ops log for bracket intents (Phase G).

Emitted whenever a bracket intent is upserted by the shadow emitter or
(in a future Phase G.2) actually submitted at the broker. Release
blockers assert that no `event=intent_write mode=authoritative` line
appears until the cutover phase is explicitly opened.

Log prefix: `[bracket_intent_ops]`.
"""
from __future__ import annotations

from typing import Any

CHILI_BRACKET_INTENT_OPS_PREFIX = "[bracket_intent_ops]"


def format_bracket_intent_ops_line(
    *,
    event: str,  # "intent_write" | "mark_reconciled" | "summary"
    mode: str,
    trade_id: int | None = None,
    bracket_intent_id: int | None = None,
    ticker: str | None = None,
    direction: str | None = None,
    quantity: float | None = None,
    entry_price: float | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    stop_model: str | None = None,
    pattern_id: int | None = None,
    regime: str | None = None,
    intent_state: str | None = None,
    broker_source: str | None = None,
    reason: str | None = None,
    intents_total: int | None = None,
    **extra: Any,
) -> str:
    parts: list[str] = [
        CHILI_BRACKET_INTENT_OPS_PREFIX,
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

    _add("trade_id", trade_id)
    _add("bracket_intent_id", bracket_intent_id)
    _add("ticker", ticker)
    _add("direction", direction)
    _add("quantity", quantity)
    _add("entry_price", entry_price)
    _add("stop_price", stop_price)
    _add("target_price", target_price)
    _add("stop_model", stop_model)
    _add("pattern_id", pattern_id)
    _add("regime", regime)
    _add("intent_state", intent_state)
    _add("broker_source", broker_source)
    _add("reason", reason)
    _add("intents_total", intents_total)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_BRACKET_INTENT_OPS_PREFIX",
    "format_bracket_intent_ops_line",
]
