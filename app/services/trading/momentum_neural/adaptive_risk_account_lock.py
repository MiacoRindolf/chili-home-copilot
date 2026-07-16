"""Shared account-risk advisory-lock order for Alpaca/adaptive writers.

This module is migration plumbing only.  It does not open a transaction, lock
any table row, write a projection, contact a broker, or make an admission
decision.  A caller that already owns a PostgreSQL transaction may use
``acquire_adaptive_risk_account_locks`` to acquire both legacy advisory-lock
identities in the one permitted order:

1. the signed-BIGINT Alpaca action/account-risk lock;
2. the two-key adaptive-risk account lock.

Writers still need to migrate explicitly.  Merely importing this module does
not close any existing race.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


ACCOUNT_RISK_LOCK_SCHEMA_VERSION = "chili.adaptive-risk-account-lock.v1"
ADAPTIVE_RISK_ADVISORY_NAMESPACE = 0x4152  # ``AR``; legacy two-key namespace.

_ACTION_LOCK_DOMAIN = "chili|alpaca|account-risk|"
_ACTION_LOCK_SQL = "SELECT pg_advisory_xact_lock(:key)"
_ADAPTIVE_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(:namespace, hashtext(:account_scope))"
)


class AccountRiskLockContractError(RuntimeError):
    """The caller cannot safely join the shared account-risk lock domain."""


def require_canonical_account_scope(account_scope: str) -> str:
    """Return an explicit lower-case scope or fail instead of splitting locks.

    The legacy Alpaca action lock lower-cases its scope while the adaptive
    two-key lock hashes the supplied text verbatim.  Accepting an upper-case or
    whitespace alias here would therefore allow old and migrated writers to
    serialize on different keys.  Migration callers must first reconcile their
    durable scope and then pass its already-canonical spelling.
    """

    if not isinstance(account_scope, str):
        raise AccountRiskLockContractError("account_scope must be explicit text")
    stripped = account_scope.strip()
    if not stripped:
        raise AccountRiskLockContractError("account_scope is required")
    canonical = stripped.lower()
    if account_scope != canonical:
        raise AccountRiskLockContractError(
            "account_scope must already be stripped lower-case canonical text"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in canonical):
        raise AccountRiskLockContractError("account_scope contains control characters")
    return canonical


def alpaca_action_account_lock_key(account_scope: str) -> int:
    """Return the exact legacy Alpaca signed-BIGINT advisory-lock key."""

    scope = require_canonical_account_scope(account_scope)
    digest = hashlib.sha256(f"{_ACTION_LOCK_DOMAIN}{scope}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@dataclass(frozen=True)
class AdaptiveRiskAccountLockIdentity:
    """Deterministic identity shared by a migrated account transaction."""

    account_scope: str
    action_advisory_key: int
    adaptive_advisory_namespace: int = ADAPTIVE_RISK_ADVISORY_NAMESPACE
    schema_version: str = ACCOUNT_RISK_LOCK_SCHEMA_VERSION

    @classmethod
    def for_scope(cls, account_scope: str) -> "AdaptiveRiskAccountLockIdentity":
        scope = require_canonical_account_scope(account_scope)
        return cls(
            account_scope=scope,
            action_advisory_key=alpaca_action_account_lock_key(scope),
        )


def acquire_adaptive_risk_account_locks(
    session: Session,
    *,
    account_scope: str,
) -> AdaptiveRiskAccountLockIdentity:
    """Acquire both legacy account locks in canonical order.

    The caller must already own the outer transaction.  This helper deliberately
    performs exactly two ``SELECT pg_advisory_xact_lock`` statements and leaves
    commit/rollback, row locking, and all writes to that caller.
    """

    in_transaction = getattr(session, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        raise AccountRiskLockContractError(
            "caller Session must already own the outer transaction"
        )

    identity = AdaptiveRiskAccountLockIdentity.for_scope(account_scope)
    session.execute(
        text(_ACTION_LOCK_SQL),
        {"key": identity.action_advisory_key},
    )
    session.execute(
        text(_ADAPTIVE_LOCK_SQL),
        {
            "namespace": identity.adaptive_advisory_namespace,
            "account_scope": identity.account_scope,
        },
    )
    return identity


class AccountRiskRowLockStage(str, Enum):
    """Canonical row families after both advisory locks are held."""

    ACCOUNT_SETTLEMENT_HEAD = "account_settlement_head"
    ADAPTIVE_RESERVATION = "adaptive_risk_reservation"
    FILL_ACTIVITY_OR_CYCLE_SETTLEMENT = "fill_activity_or_cycle_settlement"
    ACTION_CLAIM = "broker_symbol_action_claim"
    AUTOMATION_SESSION = "trading_automation_session"
    OPPORTUNITY_CLAIM = "adaptive_risk_opportunity_claim"


@dataclass(frozen=True)
class AccountRiskRowLockMetadata:
    """One row-lock stage and its required stable database ordering."""

    stage: AccountRiskRowLockStage
    ordinal: int
    stable_order: tuple[str, ...]


CANONICAL_ACCOUNT_RISK_ROW_LOCK_ORDER = (
    AccountRiskRowLockMetadata(
        AccountRiskRowLockStage.ACCOUNT_SETTLEMENT_HEAD,
        1,
        ("account_scope",),
    ),
    AccountRiskRowLockMetadata(
        AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
        2,
        ("reservation_id_uuid",),
    ),
    AccountRiskRowLockMetadata(
        AccountRiskRowLockStage.FILL_ACTIVITY_OR_CYCLE_SETTLEMENT,
        3,
        ("terminal_sequence", "event_sequence"),
    ),
    AccountRiskRowLockMetadata(
        AccountRiskRowLockStage.ACTION_CLAIM,
        4,
        ("symbol",),
    ),
    AccountRiskRowLockMetadata(
        AccountRiskRowLockStage.AUTOMATION_SESSION,
        5,
        ("session_id",),
    ),
    AccountRiskRowLockMetadata(
        AccountRiskRowLockStage.OPPORTUNITY_CLAIM,
        6,
        ("account_scope", "symbol", "trading_date", "setup_family", "id"),
    ),
)

_ROW_LOCK_ORDINAL = {
    item.stage: item.ordinal for item in CANONICAL_ACCOUNT_RISK_ROW_LOCK_ORDER
}
_ROW_LOCK_KEY_LENGTH = {
    item.stage: len(item.stable_order)
    for item in CANONICAL_ACCOUNT_RISK_ROW_LOCK_ORDER
}


class CanonicalAccountRiskRowLockGuard:
    """Fail fast when a writer's row-lock walk violates the shared order.

    Instantiate one guard per outer transaction.  Writers call ``observe``
    immediately before each ``SELECT ... FOR UPDATE``.  Stages may be skipped,
    but may never move backwards.  Multiple rows in one stage must use the same
    stable key shape and be strictly increasing in the database's declared
    order, preventing both duplicate locks and inconsistent tie-breaking.
    """

    def __init__(self) -> None:
        self._last_stage: AccountRiskRowLockStage | None = None
        self._last_sort_key: tuple[Any, ...] | None = None

    @property
    def last_stage(self) -> AccountRiskRowLockStage | None:
        return self._last_stage

    @property
    def last_sort_key(self) -> tuple[Any, ...] | None:
        return self._last_sort_key

    def observe(
        self,
        stage: AccountRiskRowLockStage,
        *,
        sort_key: tuple[Any, ...],
    ) -> None:
        if not isinstance(stage, AccountRiskRowLockStage):
            raise AccountRiskLockContractError("row-lock stage must be canonical")
        if not isinstance(sort_key, tuple):
            raise AccountRiskLockContractError("row-lock sort_key must be a tuple")
        key = sort_key
        if not key:
            raise AccountRiskLockContractError("row-lock sort_key is required")
        if len(key) != _ROW_LOCK_KEY_LENGTH[stage]:
            raise AccountRiskLockContractError(
                f"row-lock sort_key shape is invalid for {stage.value}"
            )
        if any(value is None for value in key):
            raise AccountRiskLockContractError("row-lock sort_key cannot contain null")
        try:
            hash(key)
        except TypeError as exc:
            raise AccountRiskLockContractError(
                "row-lock sort_key must contain immutable values"
            ) from exc

        prior_stage = self._last_stage
        if prior_stage is not None:
            prior_ordinal = _ROW_LOCK_ORDINAL[prior_stage]
            next_ordinal = _ROW_LOCK_ORDINAL[stage]
            if next_ordinal < prior_ordinal:
                raise AccountRiskLockContractError(
                    f"row-lock stage inversion: {prior_stage.value} -> {stage.value}"
                )
            if next_ordinal == prior_ordinal:
                try:
                    increasing = bool(key > self._last_sort_key)
                except TypeError as exc:
                    raise AccountRiskLockContractError(
                        "same-stage row-lock sort keys are not comparable"
                    ) from exc
                if not increasing:
                    raise AccountRiskLockContractError(
                        f"row-lock keys are not strictly increasing for {stage.value}"
                    )

        self._last_stage = stage
        self._last_sort_key = key


__all__ = (
    "ACCOUNT_RISK_LOCK_SCHEMA_VERSION",
    "ADAPTIVE_RISK_ADVISORY_NAMESPACE",
    "AccountRiskLockContractError",
    "AccountRiskRowLockMetadata",
    "AccountRiskRowLockStage",
    "AdaptiveRiskAccountLockIdentity",
    "CANONICAL_ACCOUNT_RISK_ROW_LOCK_ORDER",
    "CanonicalAccountRiskRowLockGuard",
    "acquire_adaptive_risk_account_locks",
    "alpaca_action_account_lock_key",
    "require_canonical_account_scope",
)
