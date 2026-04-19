"""Canonical management/provenance labels for live trading rows."""
from __future__ import annotations

from typing import Any

MANAGEMENT_SCOPE_MANUAL = "manual"
MANAGEMENT_SCOPE_AUTO_TRADER_V1 = "auto_trader_v1"
MANAGEMENT_SCOPE_ADOPTED_POSITION = "adopted_position"
MANAGEMENT_SCOPE_BROKER_SYNC = "broker_sync"
MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY = "broker_sync_legacy"
MANAGEMENT_SCOPE_MOMENTUM_NEURAL = "momentum_neural"

KNOWN_MANAGEMENT_SCOPES = frozenset(
    {
        MANAGEMENT_SCOPE_MANUAL,
        MANAGEMENT_SCOPE_AUTO_TRADER_V1,
        MANAGEMENT_SCOPE_ADOPTED_POSITION,
        MANAGEMENT_SCOPE_BROKER_SYNC,
        MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY,
        MANAGEMENT_SCOPE_MOMENTUM_NEURAL,
    }
)


def normalize_management_scope(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    return raw if raw in KNOWN_MANAGEMENT_SCOPES else None


def infer_trade_management_scope_from_fields(
    *,
    management_scope: Any = None,
    auto_trader_version: Any = None,
    broker_source: Any = None,
    tags: Any = None,
) -> str:
    explicit = normalize_management_scope(management_scope)
    if explicit:
        return explicit
    if str(auto_trader_version or "").strip().lower() == "v1":
        return MANAGEMENT_SCOPE_AUTO_TRADER_V1
    broker = str(broker_source or "").strip().lower()
    raw_tags = str(tags or "").strip().lower()
    if broker and ("sync" in raw_tags or broker in {"robinhood", "coinbase"}):
        return MANAGEMENT_SCOPE_BROKER_SYNC
    return MANAGEMENT_SCOPE_MANUAL
