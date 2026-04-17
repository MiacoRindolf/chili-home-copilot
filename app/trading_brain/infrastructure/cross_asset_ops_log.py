"""Structured one-line ops log for the cross-asset sweep (Phase L.19).

Emitted when a cross-asset lead/lag sweep computes, persists, refuses, or
skips a snapshot. Release blockers assert that no ``mode=authoritative``
line appears until Phase L.19.2 is explicitly opened.

Log prefix: ``[cross_asset_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_CROSS_ASSET_OPS_PREFIX = "[cross_asset_ops]"


def format_cross_asset_ops_line(
    *,
    event: str,  # "cross_asset_computed" | "cross_asset_persisted" | "cross_asset_refused_authoritative" | "cross_asset_skipped"
    mode: str,
    snapshot_id: str | None = None,
    as_of_date: str | None = None,
    cross_asset_label: str | None = None,
    cross_asset_numeric: int | None = None,
    bond_equity_label: str | None = None,
    credit_equity_label: str | None = None,
    usd_crypto_label: str | None = None,
    vix_breadth_label: str | None = None,
    crypto_equity_beta: float | None = None,
    symbols_sampled: int | None = None,
    symbols_missing: int | None = None,
    coverage_score: float | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry."""
    parts: list[str] = [
        CHILI_CROSS_ASSET_OPS_PREFIX,
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
    _add("cross_asset_label", cross_asset_label)
    _add("cross_asset_numeric", cross_asset_numeric)
    _add("bond_equity_label", bond_equity_label)
    _add("credit_equity_label", credit_equity_label)
    _add("usd_crypto_label", usd_crypto_label)
    _add("vix_breadth_label", vix_breadth_label)
    _add("crypto_equity_beta", crypto_equity_beta)
    _add("symbols_sampled", symbols_sampled)
    _add("symbols_missing", symbols_missing)
    _add("coverage_score", coverage_score)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_CROSS_ASSET_OPS_PREFIX",
    "format_cross_asset_ops_line",
]
