"""Canonical non-secret identity for the one Alpaca PAPER account boundary.

The broker UUID by itself is not a complete execution identity: the same text
must never be reusable under a different broker or environment.  Capture,
adaptive risk, settlement, reconciliation, and launch preflight therefore hash
this exact three-field payload.  The account scope remains the separate stable
lock domain ``alpaca:paper``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping
import uuid

from .replay_capture_contract import sha256_json


ALPACA_PAPER_ACCOUNT_SCOPE = "alpaca:paper"
ALPACA_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION = (
    "chili.alpaca-paper-account-identity.v1"
)


class AlpacaPaperAccountIdentityError(ValueError):
    """The supplied value cannot identify the certified PAPER account."""


def canonical_alpaca_paper_account_id(value: object) -> str:
    """Return one canonical lower-case UUID or fail closed."""

    supplied = str(value or "")
    raw = supplied.strip().lower()
    try:
        canonical = str(uuid.UUID(raw))
    except (AttributeError, ValueError) as exc:
        raise AlpacaPaperAccountIdentityError(
            "Alpaca PAPER account id must be a canonical UUID"
        ) from exc
    if supplied != raw or canonical != raw:
        raise AlpacaPaperAccountIdentityError(
            "Alpaca PAPER account id must use canonical lower-case UUID text"
        )
    return canonical


def alpaca_paper_account_identity_payload(
    account_id: object,
) -> Mapping[str, str]:
    """Return the exact immutable payload shared by every paper subsystem."""

    return MappingProxyType(
        {
            "broker": "alpaca",
            "environment": "paper",
            "account_id": canonical_alpaca_paper_account_id(account_id),
        }
    )


def alpaca_paper_account_identity_sha256(account_id: object) -> str:
    """Content address the broker + environment + canonical account UUID."""

    return sha256_json(dict(alpaca_paper_account_identity_payload(account_id)))


__all__ = (
    "ALPACA_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION",
    "ALPACA_PAPER_ACCOUNT_SCOPE",
    "AlpacaPaperAccountIdentityError",
    "alpaca_paper_account_identity_payload",
    "alpaca_paper_account_identity_sha256",
    "canonical_alpaca_paper_account_id",
)
