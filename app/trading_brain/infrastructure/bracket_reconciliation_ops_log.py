"""Structured one-line ops log for bracket reconciliation sweeps (Phase G).

One summary line per sweep (``event=sweep_summary``) plus one line per
non-``agree`` discrepancy (``event=discrepancy``). Release blockers
assert that no ``event=submit`` / ``event=cancel`` line appears outside
of the Phase G.2 authoritative flip.

Log prefix: `[bracket_reconciliation_ops]`.
"""
from __future__ import annotations

from typing import Any

CHILI_BRACKET_RECONCILIATION_OPS_PREFIX = "[bracket_reconciliation_ops]"


def format_bracket_reconciliation_ops_line(
    *,
    event: str,  # "sweep_summary" | "discrepancy" | "submit" | "cancel"
    mode: str,
    sweep_id: str | None = None,
    trade_id: int | None = None,
    bracket_intent_id: int | None = None,
    ticker: str | None = None,
    broker_source: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    trades_scanned: int | None = None,
    brackets_checked: int | None = None,
    agree_count: int | None = None,
    orphan_stop: int | None = None,
    missing_stop: int | None = None,
    qty_drift: int | None = None,
    state_drift: int | None = None,
    price_drift: int | None = None,
    broker_down: int | None = None,
    unreconciled: int | None = None,
    took_ms: float | None = None,
    **extra: Any,
) -> str:
    parts: list[str] = [
        CHILI_BRACKET_RECONCILIATION_OPS_PREFIX,
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

    _add("sweep_id", sweep_id)
    _add("trade_id", trade_id)
    _add("bracket_intent_id", bracket_intent_id)
    _add("ticker", ticker)
    _add("broker_source", broker_source)
    _add("kind", kind)
    _add("severity", severity)
    _add("trades_scanned", trades_scanned)
    _add("brackets_checked", brackets_checked)
    _add("agree_count", agree_count)
    _add("orphan_stop", orphan_stop)
    _add("missing_stop", missing_stop)
    _add("qty_drift", qty_drift)
    _add("state_drift", state_drift)
    _add("price_drift", price_drift)
    _add("broker_down", broker_down)
    _add("unreconciled", unreconciled)
    _add("took_ms", took_ms)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_BRACKET_RECONCILIATION_OPS_PREFIX",
    "format_bracket_reconciliation_ops_line",
]
