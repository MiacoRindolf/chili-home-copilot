"""Deterministic Alpaca PAPER cycle settlement and integrity verification.

This module never contacts Alpaca and never commits a transaction.  Its public
settlement boundary consumes an already-retained authoritative fill chain and,
inside the caller-owned transaction, appends the immutable settlement, advances
the account head, closes the adaptive reservation, and appends the matching
reservation projection event.  Database triggers independently require those
writes to commit atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
    AlpacaPaperAccountSettlementHead,
    AlpacaPaperCycleSettlement,
    AlpacaPaperFillActivity,
)
from .adaptive_risk_account_lock import (
    AccountRiskRowLockStage,
    CanonicalAccountRiskRowLockGuard,
    acquire_adaptive_risk_account_locks,
)


SETTLEMENT_SCHEMA_VERSION = "chili.alpaca-paper-cycle-settlement.v1"
SETTLEMENT_HASH_DOMAIN = "chili|alpaca-paper-cycle-settlement|v1"
SETTLEMENT_HEAD_HASH_DOMAIN = "chili|alpaca-paper-settlement-head|v1"
SETTLED_DAILY_PNL_EVIDENCE_SCHEMA_VERSION = (
    "chili.alpaca-paper-settled-daily-pnl-evidence.v1"
)
SETTLED_DAILY_PNL_EVIDENCE_SOURCE = (
    "postgresql:alpaca-paper-cycle-settlements"
)
_MONEY_QUANTUM = Decimal("0.0000000001")


class AlpacaCycleSettlementIntegrityError(ValueError):
    """Stored settlement/head bytes are not internally content-addressed."""


def _sha(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise AlpacaCycleSettlementIntegrityError(f"{field} must be SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise AlpacaCycleSettlementIntegrityError(
            f"{field} must be SHA-256 hex"
        ) from exc
    if value != value.lower():
        raise AlpacaCycleSettlementIntegrityError(
            f"{field} must use lower-case canonical hex"
        )
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AlpacaCycleSettlementIntegrityError(f"{field} is not canonical text")
    return value


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise AlpacaCycleSettlementIntegrityError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AlpacaCycleSettlementIntegrityError(
            f"{field} must be an integer"
        ) from exc
    if parsed != value or (positive and parsed <= 0) or (not positive and parsed < 0):
        raise AlpacaCycleSettlementIntegrityError(f"{field} is out of range")
    return parsed


def _decimal(value: Any, field: str, *, nonnegative: bool = False) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlpacaCycleSettlementIntegrityError(
            f"{field} must be exact decimal data"
        ) from exc
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise AlpacaCycleSettlementIntegrityError(f"{field} is out of range")
    return format(parsed.quantize(_MONEY_QUANTUM), "f")


def _timestamp(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlpacaCycleSettlementIntegrityError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _uuid(value: Any, field: str) -> str:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AlpacaCycleSettlementIntegrityError(
            f"{field} must be a canonical UUID"
        ) from exc
    canonical = str(parsed)
    if not isinstance(value, uuid.UUID) and value != canonical:
        raise AlpacaCycleSettlementIntegrityError(
            f"{field} must be a canonical UUID"
        )
    return canonical


def _date(value: Any, field: str) -> str:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise AlpacaCycleSettlementIntegrityError(f"{field} must be a date")
    return value.isoformat()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AlpacaPaperSettledDailyPnlEvidence:
    """Replayable ET-day P&L derived from the verified settlement chain.

    This is economic evidence, not a live database-lock capability.  Live
    admission additionally requires a process-private attestation issued while
    the account advisory locks and settlement-head row lock are held.  Replay
    may retain these public bytes and authorize them through its sealed input
    boundary without reconstructing the live capability.
    """

    account_scope: str
    account_identity_sha256: str
    risk_date_et: date
    decision_as_of: datetime
    local_realized_pnl_usd: Decimal
    settlement_head_content_sha256: str
    settlement_head_sequence: int
    settlement_head_tail_sha256: str | None
    included_day_settlement_sha256s: tuple[str, ...]
    observed_at: datetime
    available_at: datetime
    evidence_sha256: str
    schema_version: str = SETTLED_DAILY_PNL_EVIDENCE_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        account_identity_sha256: str,
        risk_date_et: date,
        decision_as_of: datetime,
        local_realized_pnl_usd: Decimal,
        settlement_head_content_sha256: str,
        settlement_head_sequence: int,
        settlement_head_tail_sha256: str | None,
        included_day_settlement_sha256s: tuple[str, ...],
    ) -> "AlpacaPaperSettledDailyPnlEvidence":
        canonical_clock = _timestamp(decision_as_of, "decision_as_of")
        if canonical_clock is None:  # pragma: no cover - guarded by _timestamp
            raise AlpacaCycleSettlementIntegrityError(
                "decision_as_of cannot be null"
            )
        normalized_clock = datetime.fromisoformat(
            canonical_clock.replace("Z", "+00:00")
        )
        body = {
            "schema_version": SETTLED_DAILY_PNL_EVIDENCE_SCHEMA_VERSION,
            "account_scope": "alpaca:paper",
            "account_identity_sha256": _sha(
                account_identity_sha256, "account_identity_sha256"
            ),
            "risk_date_et": _date(risk_date_et, "risk_date_et"),
            "decision_as_of": canonical_clock,
            "local_realized_pnl_usd": _decimal(
                local_realized_pnl_usd, "local_realized_pnl_usd"
            ),
            "settlement_head_content_sha256": _sha(
                settlement_head_content_sha256,
                "settlement_head_content_sha256",
            ),
            "settlement_head_sequence": _integer(
                settlement_head_sequence, "settlement_head_sequence"
            ),
            "settlement_head_tail_sha256": _sha(
                settlement_head_tail_sha256,
                "settlement_head_tail_sha256",
                nullable=True,
            ),
            "included_day_settlement_sha256s": [
                _sha(value, "included_day_settlement_sha256")
                for value in included_day_settlement_sha256s
            ],
            "observed_at": canonical_clock,
            "available_at": canonical_clock,
        }
        evidence_sha = _digest(_canonical_json(body))
        return cls(
            account_scope="alpaca:paper",
            account_identity_sha256=str(body["account_identity_sha256"]),
            risk_date_et=risk_date_et,
            decision_as_of=normalized_clock,
            local_realized_pnl_usd=Decimal(
                str(body["local_realized_pnl_usd"])
            ),
            settlement_head_content_sha256=str(
                body["settlement_head_content_sha256"]
            ),
            settlement_head_sequence=int(body["settlement_head_sequence"]),
            settlement_head_tail_sha256=body[
                "settlement_head_tail_sha256"
            ],
            included_day_settlement_sha256s=tuple(
                str(value)
                for value in body["included_day_settlement_sha256s"]
            ),
            observed_at=normalized_clock,
            available_at=normalized_clock,
            evidence_sha256=evidence_sha,
        )

    def __post_init__(self) -> None:
        self.verify()

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_scope": self.account_scope,
            "account_identity_sha256": _sha(
                self.account_identity_sha256, "account_identity_sha256"
            ),
            "risk_date_et": _date(self.risk_date_et, "risk_date_et"),
            "decision_as_of": _timestamp(
                self.decision_as_of, "decision_as_of"
            ),
            "local_realized_pnl_usd": _decimal(
                self.local_realized_pnl_usd, "local_realized_pnl_usd"
            ),
            "settlement_head_content_sha256": _sha(
                self.settlement_head_content_sha256,
                "settlement_head_content_sha256",
            ),
            "settlement_head_sequence": _integer(
                self.settlement_head_sequence, "settlement_head_sequence"
            ),
            "settlement_head_tail_sha256": _sha(
                self.settlement_head_tail_sha256,
                "settlement_head_tail_sha256",
                nullable=True,
            ),
            "included_day_settlement_sha256s": [
                _sha(value, "included_day_settlement_sha256")
                for value in self.included_day_settlement_sha256s
            ],
            "observed_at": _timestamp(self.observed_at, "observed_at"),
            "available_at": _timestamp(self.available_at, "available_at"),
        }

    def verify(self) -> "AlpacaPaperSettledDailyPnlEvidence":
        if self.schema_version != SETTLED_DAILY_PNL_EVIDENCE_SCHEMA_VERSION:
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L evidence schema is invalid"
            )
        if self.account_scope != "alpaca:paper":
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L evidence is not Alpaca PAPER"
            )
        if not isinstance(self.local_realized_pnl_usd, Decimal):
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L must retain exact Decimal data"
            )
        decision_clock = _timestamp(self.decision_as_of, "decision_as_of")
        if (
            _timestamp(self.observed_at, "observed_at") != decision_clock
            or _timestamp(self.available_at, "available_at") != decision_clock
        ):
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L evidence clocks must equal decision_as_of"
            )
        sequence = _integer(
            self.settlement_head_sequence, "settlement_head_sequence"
        )
        tail = _sha(
            self.settlement_head_tail_sha256,
            "settlement_head_tail_sha256",
            nullable=True,
        )
        if (sequence == 0) != (tail is None):
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L head sequence/tail are inconsistent"
            )
        included = tuple(self.included_day_settlement_sha256s)
        if len(included) != len(set(included)):
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L evidence repeats a settlement"
            )
        expected = _digest(_canonical_json(self._body()))
        if _sha(self.evidence_sha256, "evidence_sha256") != expected:
            raise AlpacaCycleSettlementIntegrityError(
                "settled daily P&L evidence content hash changed"
            )
        return self

    def to_payload(self) -> dict[str, Any]:
        return {**self._body(), "evidence_sha256": self.evidence_sha256}


def cycle_settlement_content_payload(
    row: AlpacaPaperCycleSettlement,
) -> dict[str, Any]:
    """Return the exact canonical payload represented by one ORM row."""

    return {
        "schema_version": _text(
            row.settlement_schema_version, "settlement_schema_version"
        ),
        "settlement_authority_status": _text(
            row.settlement_authority_status, "settlement_authority_status"
        ),
        "reservation_id": _uuid(row.reservation_id, "reservation_id"),
        "decision_packet_sha256": _sha(
            row.decision_packet_sha256, "decision_packet_sha256"
        ),
        "reservation_request_sha256": _sha(
            row.reservation_request_sha256, "reservation_request_sha256"
        ),
        "account_scope": _text(row.account_scope, "account_scope"),
        "account_identity_sha256": _sha(
            row.account_identity_sha256, "account_identity_sha256"
        ),
        "account_snapshot_sha256": _sha(
            row.account_snapshot_sha256, "account_snapshot_sha256"
        ),
        "broker_connection_generation": _text(
            row.broker_connection_generation, "broker_connection_generation"
        ),
        "execution_family": _text(row.execution_family, "execution_family"),
        "broker_environment": _text(row.broker_environment, "broker_environment"),
        "position_direction": _text(row.position_direction, "position_direction"),
        "symbol": _text(row.symbol, "symbol"),
        "trading_date": _date(row.trading_date, "trading_date"),
        "setup_family": _text(row.setup_family, "setup_family"),
        "terminal_sequence": _integer(
            row.terminal_sequence, "terminal_sequence", positive=True
        ),
        "previous_account_settlement_sha256": _sha(
            row.previous_account_settlement_sha256,
            "previous_account_settlement_sha256",
            nullable=True,
        ),
        "source_fill_count": _integer(
            row.source_fill_count, "source_fill_count", positive=True
        ),
        "terminal_fill_sequence": _integer(
            row.terminal_fill_sequence, "terminal_fill_sequence", positive=True
        ),
        "terminal_fill_event_sha256": _sha(
            row.terminal_fill_event_sha256, "terminal_fill_event_sha256"
        ),
        "fill_chain_root_sha256": _sha(
            row.fill_chain_root_sha256, "fill_chain_root_sha256"
        ),
        "flat_evidence_sha256": _sha(
            row.flat_evidence_sha256, "flat_evidence_sha256"
        ),
        "capture_authority_status": _text(
            row.capture_authority_status, "capture_authority_status"
        ),
        "capture_authority_receipt_sha256": _sha(
            row.capture_authority_receipt_sha256,
            "capture_authority_receipt_sha256",
        ),
        "provider_event_clock_status": _text(
            row.provider_event_clock_status, "provider_event_clock_status"
        ),
        "provider_client_order_id_status": _text(
            row.provider_client_order_id_status,
            "provider_client_order_id_status",
        ),
        "exit_order_ownership_status": _text(
            row.exit_order_ownership_status, "exit_order_ownership_status"
        ),
        "fee_status": _text(row.fee_status, "fee_status"),
        "fee_evidence_root_sha256": _sha(
            row.fee_evidence_root_sha256, "fee_evidence_root_sha256"
        ),
        "entry_quantity": _decimal(
            row.entry_quantity, "entry_quantity", nonnegative=True
        ),
        "exit_quantity": _decimal(
            row.exit_quantity, "exit_quantity", nonnegative=True
        ),
        "entry_cost_usd": _decimal(
            row.entry_cost_usd, "entry_cost_usd", nonnegative=True
        ),
        "exit_proceeds_usd": _decimal(
            row.exit_proceeds_usd, "exit_proceeds_usd", nonnegative=True
        ),
        "gross_realized_pnl_usd": _decimal(
            row.gross_realized_pnl_usd, "gross_realized_pnl_usd"
        ),
        "fee_usd": _decimal(row.fee_usd, "fee_usd", nonnegative=True),
        "net_realized_pnl_usd": _decimal(
            row.net_realized_pnl_usd, "net_realized_pnl_usd"
        ),
        "settlement_policy_sha256": _sha(
            row.settlement_policy_sha256, "settlement_policy_sha256"
        ),
        "effective_config_sha256": _sha(
            row.effective_config_sha256, "effective_config_sha256"
        ),
        "code_build_sha256": _sha(row.code_build_sha256, "code_build_sha256"),
        "feature_flags_sha256": _sha(
            row.feature_flags_sha256, "feature_flags_sha256"
        ),
        "closed_observed_at": _timestamp(
            row.closed_observed_at, "closed_observed_at"
        ),
        "closed_available_at": _timestamp(
            row.closed_available_at, "closed_available_at"
        ),
    }


def verify_cycle_settlement_content(row: AlpacaPaperCycleSettlement) -> None:
    """Recompute canonical content/chain hashes; grants no authority status."""

    payload = cycle_settlement_content_payload(row)
    canonical = _canonical_json(payload)
    content_sha = _digest(canonical)
    if row.settlement_content_canonical_json != canonical:
        raise AlpacaCycleSettlementIntegrityError(
            "settlement canonical JSON does not match typed columns"
        )
    if row.settlement_content_sha256 != content_sha:
        raise AlpacaCycleSettlementIntegrityError(
            "settlement content SHA-256 does not match canonical JSON"
        )
    previous = payload["previous_account_settlement_sha256"] or "genesis"
    settlement_sha = _digest(
        f"{SETTLEMENT_HASH_DOMAIN}|{previous}|{content_sha}"
    )
    if row.settlement_sha256 != settlement_sha:
        raise AlpacaCycleSettlementIntegrityError(
            "settlement chain SHA-256 does not match content/previous head"
        )


def settlement_head_content_payload(
    row: AlpacaPaperAccountSettlementHead,
) -> dict[str, Any]:
    return {
        "schema_version": _text(
            row.settlement_schema_version, "settlement_schema_version"
        ),
        "account_scope": _text(row.account_scope, "account_scope"),
        "account_identity_sha256": _sha(
            row.account_identity_sha256, "account_identity_sha256"
        ),
        "execution_family": _text(row.execution_family, "execution_family"),
        "broker_environment": _text(row.broker_environment, "broker_environment"),
        "settled_cycle_sequence": _integer(
            row.settled_cycle_sequence, "settled_cycle_sequence"
        ),
        "last_settlement_sha256": _sha(
            row.last_settlement_sha256, "last_settlement_sha256", nullable=True
        ),
        "cumulative_gross_realized_pnl_usd": _decimal(
            row.cumulative_gross_realized_pnl_usd,
            "cumulative_gross_realized_pnl_usd",
        ),
        "cumulative_fee_usd": _decimal(
            row.cumulative_fee_usd, "cumulative_fee_usd", nonnegative=True
        ),
        "cumulative_net_realized_pnl_usd": _decimal(
            row.cumulative_net_realized_pnl_usd,
            "cumulative_net_realized_pnl_usd",
        ),
        "version": _integer(row.version, "version", positive=True),
        "last_settled_at": _timestamp(
            row.last_settled_at, "last_settled_at", nullable=True
        ),
    }


def settlement_head_content_sha256(
    row: AlpacaPaperAccountSettlementHead,
) -> str:
    canonical = _canonical_json(settlement_head_content_payload(row))
    return _digest(f"{SETTLEMENT_HEAD_HASH_DOMAIN}|{canonical}")


def verify_settlement_head_content(row: AlpacaPaperAccountSettlementHead) -> None:
    expected = settlement_head_content_sha256(row)
    if row.head_content_sha256 != expected:
        raise AlpacaCycleSettlementIntegrityError(
            "settlement head SHA-256 does not match typed columns"
        )


def new_zero_settlement_head(
    *,
    account_identity_sha256: str,
) -> AlpacaPaperAccountSettlementHead:
    """Construct the only directly insertable head; performs no database I/O."""

    identity = _sha(account_identity_sha256, "account_identity_sha256")
    row = AlpacaPaperAccountSettlementHead(
        account_scope="alpaca:paper",
        account_identity_sha256=identity,
        settlement_schema_version=SETTLEMENT_SCHEMA_VERSION,
        execution_family="alpaca_spot",
        broker_environment="paper",
        settled_cycle_sequence=0,
        last_settlement_sha256=None,
        cumulative_gross_realized_pnl_usd=Decimal("0"),
        cumulative_fee_usd=Decimal("0"),
        cumulative_net_realized_pnl_usd=Decimal("0"),
        head_content_sha256="0" * 64,
        version=1,
        last_settled_at=None,
    )
    row.head_content_sha256 = settlement_head_content_sha256(row)
    return row


@dataclass(frozen=True)
class AlpacaPaperCycleSettlementResult:
    row: AlpacaPaperCycleSettlement
    created: bool


def _money(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(_MONEY_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlpacaCycleSettlementIntegrityError(
            "settlement economic input is not exact decimal data"
        ) from exc
    if not parsed.is_finite():
        raise AlpacaCycleSettlementIntegrityError(
            "settlement economic input is non-finite"
        )
    return parsed


def settle_flat_alpaca_paper_cycle(
    session: Session,
    *,
    reservation_id: uuid.UUID,
) -> AlpacaPaperCycleSettlementResult:
    """Atomically settle one complete authoritative flat PAPER cycle.

    The caller owns the transaction.  Account advisory locking, row locks, the
    immutable settlement insert, account-head advance, and reservation close
    happen in that one transaction; the database's deferred completion trigger
    independently rejects a partial commit.  Replays return the exact retained
    settlement after re-verifying its hashes and head binding.
    """

    if not session.in_transaction():
        raise AlpacaCycleSettlementIntegrityError(
            "caller must own an explicit transaction before cycle settlement"
        )
    try:
        cycle_id = (
            reservation_id
            if isinstance(reservation_id, uuid.UUID)
            else uuid.UUID(str(reservation_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise AlpacaCycleSettlementIntegrityError(
            "reservation_id must be a UUID"
        ) from exc

    from .alpaca_fill_activity import (
        append_alpaca_paper_terminal_fill_observation_receipt,
        evaluate_alpaca_paper_cycle_settlement,
        verify_alpaca_paper_fill_activity_chain,
    )

    account_scope = "alpaca:paper"
    acquire_adaptive_risk_account_locks(
        session,
        account_scope=account_scope,
    )
    row_locks = CanonicalAccountRiskRowLockGuard()

    # Identity/provenance columns are immutable. Read them without a row lock
    # only to select the account-head row; both account advisory domains are
    # already held, and every mutable row lock below follows stage 1 -> 2 -> 3.
    preflight = session.execute(
        select(
            AdaptiveRiskReservation.account_scope,
            AdaptiveRiskDecisionPacket.account_identity_sha256,
        )
        .join(
            AdaptiveRiskDecisionPacket,
            AdaptiveRiskDecisionPacket.decision_packet_sha256
            == AdaptiveRiskReservation.decision_packet_sha256,
        )
        .where(AdaptiveRiskReservation.reservation_id == cycle_id)
    ).one_or_none()
    if preflight is None or preflight[0] != account_scope:
        raise AlpacaCycleSettlementIntegrityError(
            "adaptive PAPER reservation identity is missing"
        )
    identity = _sha(preflight[1], "account_identity_sha256")

    row_locks.observe(
        AccountRiskRowLockStage.ACCOUNT_SETTLEMENT_HEAD,
        sort_key=(account_scope,),
    )
    head = session.scalar(
        select(AlpacaPaperAccountSettlementHead)
        .where(
            AlpacaPaperAccountSettlementHead.account_scope == account_scope,
            AlpacaPaperAccountSettlementHead.account_identity_sha256 == identity,
        )
        .with_for_update()
    )
    if head is None:
        head = new_zero_settlement_head(account_identity_sha256=identity)
        session.add(head)
        session.flush([head])
    else:
        verify_settlement_head_content(head)

    row_locks.observe(
        AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
        sort_key=(str(cycle_id),),
    )
    reservation = session.scalar(
        select(AdaptiveRiskReservation)
        .where(AdaptiveRiskReservation.reservation_id == cycle_id)
        .with_for_update()
    )
    if reservation is None:
        raise AlpacaCycleSettlementIntegrityError(
            "adaptive reservation is missing"
        )
    packet = session.get(
        AdaptiveRiskDecisionPacket, reservation.decision_packet_sha256
    )
    if packet is None:
        raise AlpacaCycleSettlementIntegrityError(
            "immutable decision packet is missing"
        )
    if not (
        reservation.account_scope == "alpaca:paper"
        and packet.account_scope == "alpaca:paper"
        and packet.execution_surface == "alpaca_paper"
        and packet.execution_family == "alpaca_spot"
        and packet.broker_environment == "paper"
    ):
        raise AlpacaCycleSettlementIntegrityError(
            "cycle settlement is scoped only to Alpaca PAPER"
        )
    if packet.account_identity_sha256 != identity:
        raise AlpacaCycleSettlementIntegrityError(
            "cycle account identity changed after head selection"
        )

    row_locks.observe(
        AccountRiskRowLockStage.FILL_ACTIVITY_OR_CYCLE_SETTLEMENT,
        sort_key=(0, 0),
    )
    existing = session.scalar(
        select(AlpacaPaperCycleSettlement)
        .where(AlpacaPaperCycleSettlement.reservation_id == cycle_id)
        .with_for_update()
    )
    if existing is not None:
        verify_cycle_settlement_content(existing)
        if not (
            existing.account_scope == account_scope
            and existing.account_identity_sha256 == identity
            and reservation.state == "closed"
            and int(head.settled_cycle_sequence) >= int(existing.terminal_sequence)
            and (
                int(head.settled_cycle_sequence) > int(existing.terminal_sequence)
                or head.last_settlement_sha256 == existing.settlement_sha256
            )
        ):
            raise AlpacaCycleSettlementIntegrityError(
                "retained settlement/head/reservation are inconsistent"
            )
        append_alpaca_paper_terminal_fill_observation_receipt(
            session,
            settlement=existing,
        )
        return AlpacaPaperCycleSettlementResult(row=existing, created=False)

    if reservation.state != "flat_pending_settlement":
        raise AlpacaCycleSettlementIntegrityError(
            "cycle is not flat_pending_settlement"
        )
    row_locks.observe(
        AccountRiskRowLockStage.FILL_ACTIVITY_OR_CYCLE_SETTLEMENT,
        sort_key=(1, 0),
    )
    rows = list(
        session.scalars(
            select(AlpacaPaperFillActivity)
            .where(AlpacaPaperFillActivity.reservation_id == cycle_id)
            .order_by(AlpacaPaperFillActivity.sequence)
            .with_for_update()
        )
    )
    verify_alpaca_paper_fill_activity_chain(rows)
    coverage = evaluate_alpaca_paper_cycle_settlement(
        reservation_id=cycle_id,
        rows=rows,
        expected_entry_quantity=reservation.cumulative_filled_quantity_shares,
    )
    if coverage.pending:
        raise AlpacaCycleSettlementIntegrityError(
            "cycle settlement evidence incomplete: "
            + ",".join(coverage.pending_reasons)
        )

    closed_observed_at = reservation.last_broker_observed_at
    closed_available_at = reservation.last_broker_available_at
    if (
        not isinstance(closed_observed_at, datetime)
        or closed_observed_at.tzinfo is None
        or not isinstance(closed_available_at, datetime)
        or closed_available_at.tzinfo is None
        or closed_observed_at > closed_available_at
    ):
        raise AlpacaCycleSettlementIntegrityError(
            "authoritative flat observation clocks are unavailable"
        )
    flat_evidence_sha256 = _sha(
        reservation.last_source_event_content_sha256,
        "flat_evidence_sha256",
    )
    terminal = rows[-1]
    capture_receipt = _digest(
        _canonical_json(
            {
                "schema_version": "chili.alpaca-paper-fill-capture-receipt.v1",
                "reservation_id": str(cycle_id),
                "events": [
                    {
                        "sequence": int(row.sequence),
                        "event_sha256": row.event_sha256,
                        "record_content_sha256": row.record_content_sha256,
                        "provider_payload_sha256": row.provider_payload_sha256,
                        "order_binding_sha256": row.order_binding_sha256,
                    }
                    for row in rows
                ],
            }
        )
    )
    fee_root = _digest(
        _canonical_json(
            {
                "schema_version": "chili.alpaca-paper-fee-evidence-root.v1",
                "fee_evidence_sha256s": [
                    row.fee_evidence_sha256 for row in rows
                ],
            }
        )
    )
    policy_sha = _digest(
        _canonical_json(
            {
                "schema_version": "chili.alpaca-paper-cycle-settlement-policy.v1",
                "pricing": "provider_execution_price",
                "fees": "alpaca_paper_equity_simulator_contract",
                "quantity": "complete_append_only_fill_chain",
                "flatness": "authoritative_exact_account_position_zero",
            }
        )
    )
    entry_cost = sum(
        (_money(row.quantity) * _money(row.price) for row in rows if row.side == "buy"),
        Decimal("0"),
    ).quantize(_MONEY_QUANTUM)
    exit_proceeds = sum(
        (_money(row.quantity) * _money(row.price) for row in rows if row.side == "sell"),
        Decimal("0"),
    ).quantize(_MONEY_QUANTUM)
    fee_usd = sum(
        (_money(row.fee_usd) for row in rows), Decimal("0")
    ).quantize(_MONEY_QUANTUM)
    gross = (exit_proceeds - entry_cost).quantize(_MONEY_QUANTUM)
    net = (gross - fee_usd).quantize(_MONEY_QUANTUM)

    settlement = AlpacaPaperCycleSettlement(
        settlement_sha256="0" * 64,
        settlement_schema_version=SETTLEMENT_SCHEMA_VERSION,
        settlement_authority_status="sealed_verified",
        reservation_id=cycle_id,
        decision_packet_sha256=packet.decision_packet_sha256,
        reservation_request_sha256=packet.reservation_request_sha256,
        account_scope="alpaca:paper",
        account_identity_sha256=identity,
        account_snapshot_sha256=packet.account_snapshot_sha256,
        broker_connection_generation=reservation.broker_connection_generation,
        execution_family="alpaca_spot",
        broker_environment="paper",
        position_direction="long",
        symbol=packet.symbol,
        trading_date=packet.trading_date,
        setup_family=packet.setup_family,
        terminal_sequence=int(head.settled_cycle_sequence) + 1,
        previous_account_settlement_sha256=head.last_settlement_sha256,
        source_fill_count=len(rows),
        terminal_fill_sequence=int(terminal.sequence),
        terminal_fill_event_sha256=terminal.event_sha256,
        fill_chain_root_sha256=terminal.event_sha256,
        flat_evidence_sha256=flat_evidence_sha256,
        capture_authority_status="verified",
        capture_authority_receipt_sha256=capture_receipt,
        provider_event_clock_status="authoritative",
        provider_client_order_id_status="authoritative",
        exit_order_ownership_status="authoritative",
        fee_status="authoritative",
        fee_evidence_root_sha256=fee_root,
        entry_quantity=coverage.entry_quantity,
        exit_quantity=coverage.exit_quantity,
        entry_cost_usd=entry_cost,
        exit_proceeds_usd=exit_proceeds,
        gross_realized_pnl_usd=gross,
        fee_usd=fee_usd,
        net_realized_pnl_usd=net,
        settlement_policy_sha256=policy_sha,
        effective_config_sha256=packet.effective_config_sha256,
        code_build_sha256=packet.code_build_sha256,
        feature_flags_sha256=packet.feature_flags_sha256,
        settlement_content_canonical_json="{}",
        settlement_content_sha256="0" * 64,
        closed_observed_at=closed_observed_at.astimezone(UTC),
        closed_available_at=closed_available_at.astimezone(UTC),
    )
    canonical = _canonical_json(cycle_settlement_content_payload(settlement))
    settlement.settlement_content_canonical_json = canonical
    settlement.settlement_content_sha256 = _digest(canonical)
    previous = settlement.previous_account_settlement_sha256 or "genesis"
    settlement.settlement_sha256 = _digest(
        f"{SETTLEMENT_HASH_DOMAIN}|{previous}|"
        f"{settlement.settlement_content_sha256}"
    )
    verify_cycle_settlement_content(settlement)
    session.add(settlement)
    session.flush([settlement])
    append_alpaca_paper_terminal_fill_observation_receipt(
        session,
        settlement=settlement,
    )

    head.settled_cycle_sequence = settlement.terminal_sequence
    head.last_settlement_sha256 = settlement.settlement_sha256
    head.cumulative_gross_realized_pnl_usd = (
        _money(head.cumulative_gross_realized_pnl_usd) + gross
    ).quantize(_MONEY_QUANTUM)
    head.cumulative_fee_usd = (
        _money(head.cumulative_fee_usd) + fee_usd
    ).quantize(_MONEY_QUANTUM)
    head.cumulative_net_realized_pnl_usd = (
        _money(head.cumulative_net_realized_pnl_usd) + net
    ).quantize(_MONEY_QUANTUM)
    head.version = int(head.version) + 1
    head.last_settled_at = settlement.closed_available_at
    head.updated_at = settlement.closed_available_at
    head.head_content_sha256 = settlement_head_content_sha256(head)
    verify_settlement_head_content(head)
    session.flush([head])

    reservation.state = "closed"
    reservation.closed_at = settlement.closed_available_at
    reservation.updated_at = settlement.closed_available_at
    reservation.version = int(reservation.version) + 1
    # Keep the reservation projection reconstructible from its append-only
    # event stream.  The immutable settlement remains the economic authority;
    # this event only records the same-transaction projection transition.
    from .adaptive_risk_reservation import AdaptiveRiskReservationStore

    AdaptiveRiskReservationStore._append_reservation_event(
        session,
        reservation,
        event_type="alpaca_paper_cycle_settled",
        effective_at=settlement.closed_available_at,
        broker_event_id=None,
        details={
            "settlement_sha256": settlement.settlement_sha256,
            "terminal_sequence": int(settlement.terminal_sequence),
            "terminal_fill_event_sha256": settlement.terminal_fill_event_sha256,
            "net_realized_pnl_usd": str(settlement.net_realized_pnl_usd),
            "fee_usd": str(settlement.fee_usd),
            "opportunity_remains_consumed": (
                reservation.opportunity_claim_id is not None
            ),
        },
    )
    session.flush()
    return AlpacaPaperCycleSettlementResult(row=settlement, created=True)


__all__ = (
    "AlpacaCycleSettlementIntegrityError",
    "AlpacaPaperCycleSettlementResult",
    "AlpacaPaperSettledDailyPnlEvidence",
    "SETTLEMENT_SCHEMA_VERSION",
    "SETTLED_DAILY_PNL_EVIDENCE_SCHEMA_VERSION",
    "SETTLED_DAILY_PNL_EVIDENCE_SOURCE",
    "cycle_settlement_content_payload",
    "new_zero_settlement_head",
    "settlement_head_content_payload",
    "settlement_head_content_sha256",
    "settle_flat_alpaca_paper_cycle",
    "verify_cycle_settlement_content",
    "verify_settlement_head_content",
)
