"""Exact captured Alpaca PAPER account-read contract.

The trading adapter and every consumer of its captured receipt share this
module so a synthetic account shape cannot be accepted by one side while the
other side records the broker's real flat payload.  This module performs no
I/O and carries no account balances or credentials.
"""

from __future__ import annotations

from typing import Any

from .alpaca_paper_identity import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    canonical_alpaca_paper_account_id,
)


ALPACA_PAPER_ACCOUNT_PROVIDER = "alpaca_trading_paper"
ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION = (
    "chili.capture.alpaca-paper-account.v1"
)
ALPACA_PAPER_ACCOUNT_QUERY_SCHEMA_VERSION = (
    "chili.capture.alpaca-paper-account-query.v1"
)
ALPACA_PAPER_ACCOUNT_QUERY_OPERATION = "get_account_snapshot"
ALPACA_PAPER_ACCOUNT_READY_STATUS = "ACTIVE"

ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "account_id",
        "account_identity_sha256",
        "account_scope",
        "paper",
        "status",
        "equity_usd",
        "last_equity_usd",
        "buying_power_usd",
        "cash_usd",
        "account_blocked",
        "trading_blocked",
        "trade_suspended_by_user",
        "received_at",
    }
)
ALPACA_PAPER_ACCOUNT_QUERY_KEYS = frozenset(
    {
        "schema_version",
        "operation",
        "account_scope",
        "expected_account_id",
        "fields",
    }
)
ALPACA_PAPER_ACCOUNT_REQUESTED_FIELDS = tuple(
    sorted(
        ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS
        - {"schema_version", "account_identity_sha256", "received_at"}
    )
)


def alpaca_paper_account_capture_query(account_id: object) -> dict[str, Any]:
    """Return the one canonical query recorded beside the flat account payload."""

    return {
        "schema_version": ALPACA_PAPER_ACCOUNT_QUERY_SCHEMA_VERSION,
        "operation": ALPACA_PAPER_ACCOUNT_QUERY_OPERATION,
        "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
        "expected_account_id": canonical_alpaca_paper_account_id(account_id),
        "fields": list(ALPACA_PAPER_ACCOUNT_REQUESTED_FIELDS),
    }


__all__ = (
    "ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS",
    "ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION",
    "ALPACA_PAPER_ACCOUNT_PROVIDER",
    "ALPACA_PAPER_ACCOUNT_QUERY_KEYS",
    "ALPACA_PAPER_ACCOUNT_QUERY_OPERATION",
    "ALPACA_PAPER_ACCOUNT_QUERY_SCHEMA_VERSION",
    "ALPACA_PAPER_ACCOUNT_READY_STATUS",
    "ALPACA_PAPER_ACCOUNT_REQUESTED_FIELDS",
    "alpaca_paper_account_capture_query",
)
