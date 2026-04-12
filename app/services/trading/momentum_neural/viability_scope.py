"""Shared scope helpers for symbol-specific vs aggregate viability rows."""

from __future__ import annotations


VIABILITY_SCOPE_SYMBOL = "symbol"
VIABILITY_SCOPE_AGGREGATE = "aggregate"
AGGREGATE_SYMBOL = "__aggregate__"


def normalize_viability_scope(value: str | None) -> str:
    scope = (value or "").strip().lower()
    if scope == VIABILITY_SCOPE_AGGREGATE:
        return VIABILITY_SCOPE_AGGREGATE
    return VIABILITY_SCOPE_SYMBOL


def is_aggregate_symbol(symbol: str | None) -> bool:
    return (symbol or "").strip().upper() == AGGREGATE_SYMBOL


def infer_viability_scope(symbol: str | None, *, explicit: str | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        return normalize_viability_scope(explicit)
    return VIABILITY_SCOPE_AGGREGATE if is_aggregate_symbol(symbol) else VIABILITY_SCOPE_SYMBOL
