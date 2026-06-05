"""Pure helpers for the trades API response contract.

Detects "shadow" mismatches between the current ``/trades`` response rows and the
authoritative envelope rows, comparing only stable fields (no broker-truth display
overlays). Pure/deterministic: no DB, no app state, safe to unit test in isolation.

Re-implemented on main from os-deploy unique work (backup branch
``backup/os-deploy-cleanup-20260605``). Stands alone as an audit utility until a
``/trades`` diagnostic consumes ``_stable_trades_shadow_mismatches``.
"""
from __future__ import annotations

from typing import Any


_TRADES_API_SHADOW_FIELDS = (
    "ticker",
    "direction",
    "exit_price",
    "entry_date",
    "exit_date",
    "status",
    "pnl",
    "tags",
    "notes",
    "broker_source",
    "broker_status",
    "broker_order_id",
    "filled_at",
    "avg_fill_price",
    "tca_reference_entry_price",
    "tca_entry_slippage_bps",
    "tca_reference_exit_price",
    "tca_exit_slippage_bps",
    "strategy_proposal_id",
    "scan_pattern_id",
    "position_id",
)


def _stable_trades_shadow_mismatches(
    current_rows: list[dict[str, Any]],
    envelope_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare stable /trades fields without broker-truth display overlays.

    Returns at most one mismatch per current row (the first differing field), or a
    sentinel ``{"field": "id", "current": "present", "envelope": None}`` when a
    current row has no matching envelope. ``entry_price``/``quantity`` are read from
    the current row's ``local_*`` columns (pre-overlay values).
    """
    envelope_by_id = {
        int(row["id"]): row for row in envelope_rows if row.get("id") is not None
    }
    mismatches: list[dict[str, Any]] = []
    for row in current_rows:
        trade_id = row.get("id")
        if trade_id is None:
            continue
        envelope = envelope_by_id.get(int(trade_id))
        if envelope is None:
            mismatches.append(
                {"id": trade_id, "field": "id", "current": "present", "envelope": None}
            )
            continue
        comparisons = {
            "entry_price": row.get("local_entry_price"),
            "quantity": row.get("local_quantity"),
        }
        comparisons.update({field: row.get(field) for field in _TRADES_API_SHADOW_FIELDS})
        for field, current_value in comparisons.items():
            envelope_value = envelope.get(field)
            if hasattr(envelope_value, "isoformat"):
                envelope_value = envelope_value.isoformat()
            if current_value != envelope_value:
                mismatches.append(
                    {
                        "id": trade_id,
                        "field": field,
                        "current": current_value,
                        "envelope": envelope_value,
                    }
                )
                break
    return mismatches
