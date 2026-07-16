"""Production split-phase fill capture for captured Alpaca PAPER entries.

The network read and database publication are deliberately separated.  A
short read-only lifecycle transaction freezes the exact adaptive cycle, then
closes before the Alpaca request.  A later pristine transaction publishes the
verified batch, advances adaptive fill truth, and (for a fill-bearing transport
observation) commits the typed outbox handoff before the transaction can
commit.  No database session is ever held across broker I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
)

from .adaptive_risk_account_lock import acquire_adaptive_risk_account_locks
from .alpaca_fill_activity import (
    AlpacaPaperFillCycleBinding,
    PreparedAlpacaPaperFillBatch,
    publish_prepared_alpaca_paper_entry_fill_batch,
    read_verified_alpaca_paper_fill_batch,
)
from .captured_paper_outbox import (
    OUTBOX_STATUS_FILL_HANDOFF_COMMITTED,
    commit_captured_paper_fill_handoff,
)
from .captured_paper_transport_coordinator import (
    CapturedPaperFillAppendReceipt,
    CapturedPaperFillReadAuthority,
    CapturedPaperFillReconciliationRequiredObservation,
    CapturedPaperPositiveOrderObservation,
    CapturedPaperTerminalZeroFillObservation,
    CapturedPaperTransportContractError,
    CapturedPaperTransportInstruction,
    CapturedPaperTransportUnavailable,
)


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class _PendingFillRead:
    read: CapturedPaperFillReadAuthority
    instruction_sha256: str
    batch: PreparedAlpacaPaperFillBatch


class SqlAlchemyCapturedPaperFillCapture:
    """Exact adapter + PostgreSQL implementation of the fill-capture seam."""

    def __init__(
        self,
        *,
        bind: Engine,
        adapter: Any,
        max_pending_reads: int,
    ) -> None:
        if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
            raise CapturedPaperTransportContractError(
                "captured_paper_fill_postgresql_engine_required"
            )
        if (
            isinstance(max_pending_reads, bool)
            or not isinstance(max_pending_reads, int)
            or max_pending_reads <= 0
            or max_pending_reads > 32_767
        ):
            raise CapturedPaperTransportContractError(
                "captured_paper_fill_pending_capacity_invalid"
            )
        self._bind = bind
        self._adapter = adapter
        self._max_pending_reads = max_pending_reads
        self._factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        self._pending: dict[str, _PendingFillRead] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _verify_instruction_observation(
        instruction: CapturedPaperTransportInstruction,
        observation: (
            CapturedPaperPositiveOrderObservation
            | CapturedPaperFillReconciliationRequiredObservation
            | CapturedPaperTerminalZeroFillObservation
        ),
    ) -> None:
        if type(instruction) is not CapturedPaperTransportInstruction:
            raise CapturedPaperTransportContractError(
                "fill_capture_instruction_type_invalid"
            )
        if type(observation) not in {
            CapturedPaperPositiveOrderObservation,
            CapturedPaperFillReconciliationRequiredObservation,
            CapturedPaperTerminalZeroFillObservation,
        }:
            raise CapturedPaperTransportContractError(
                "fill_capture_observation_type_invalid"
            )
        observation.verify_for_instruction(instruction)

    def _load_cycle_snapshot(
        self,
        *,
        instruction: CapturedPaperTransportInstruction,
        observation: (
            CapturedPaperPositiveOrderObservation
            | CapturedPaperFillReconciliationRequiredObservation
            | CapturedPaperTerminalZeroFillObservation
        ),
    ) -> AlpacaPaperFillCycleBinding:
        db = self._factory()
        try:
            with db.begin():
                acquire_adaptive_risk_account_locks(
                    db,
                    account_scope=instruction.account_scope,
                )
                reservation = db.scalar(
                    select(AdaptiveRiskReservation)
                    .where(
                        AdaptiveRiskReservation.reservation_id
                        == uuid.UUID(instruction.authority.reservation_id)
                    )
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if reservation is None:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_reservation_missing"
                    )
                packet = db.scalar(
                    select(AdaptiveRiskDecisionPacket).where(
                        AdaptiveRiskDecisionPacket.decision_packet_sha256
                        == reservation.decision_packet_sha256
                    )
                )
                if packet is None:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_decision_packet_missing"
                    )
                authority = instruction.authority
                if not (
                    reservation.decision_packet_sha256
                    == authority.decision_packet_sha256
                    and packet.decision_packet_sha256
                    == authority.decision_packet_sha256
                    and packet.reservation_request_sha256
                    == authority.reservation_request_sha256
                    and reservation.account_scope == authority.account_scope
                    and packet.account_scope == authority.account_scope
                    and packet.account_identity_sha256
                    == authority.account_identity_sha256
                    and packet.client_order_id == authority.client_order_id
                    and str(packet.symbol).strip().upper() == authority.symbol
                ):
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_cycle_authority_mismatch"
                    )
                broker_fields = (
                    reservation.broker_source,
                    reservation.broker_connection_generation,
                    reservation.broker_order_id,
                    reservation.last_broker_observed_at,
                    reservation.last_broker_available_at,
                    reservation.last_source_event_content_sha256,
                )
                if all(value is None for value in broker_fields):
                    return (
                        AlpacaPaperFillCycleBinding.from_unbound_fill_bearing_rows(
                            reservation,
                            packet,
                            broker_connection_generation=(
                                observation.broker_connection_generation
                            ),
                            entry_provider_order_id=observation.broker_order_id,
                        )
                    )
                if any(value is None for value in broker_fields):
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_partial_broker_binding"
                    )
                cycle = AlpacaPaperFillCycleBinding.from_rows(
                    reservation,
                    packet,
                )
                if not (
                    cycle.entry_provider_order_id
                    == observation.broker_order_id
                    and cycle.broker_connection_generation
                    == observation.broker_connection_generation
                ):
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_bound_order_mismatch"
                    )
                return cycle
        finally:
            db.close()

    def read_exact_order_fills(
        self,
        instruction: CapturedPaperTransportInstruction,
        observation: (
            CapturedPaperPositiveOrderObservation
            | CapturedPaperFillReconciliationRequiredObservation
            | CapturedPaperTerminalZeroFillObservation
        ),
    ) -> CapturedPaperFillReadAuthority:
        """Close the lifecycle snapshot transaction before broker I/O."""

        self._verify_instruction_observation(instruction, observation)
        cycle = self._load_cycle_snapshot(
            instruction=instruction,
            observation=observation,
        )
        batch = read_verified_alpaca_paper_fill_batch(
            self._adapter,
            cycle=cycle,
            provider_order_id=observation.broker_order_id,
            expected_client_order_id=instruction.client_order_id,
        )
        read = CapturedPaperFillReadAuthority(
            account_scope=instruction.account_scope,
            expected_account_id=instruction.expected_account_id,
            reservation_id=instruction.authority.reservation_id,
            client_order_id=instruction.client_order_id,
            broker_order_id=observation.broker_order_id,
            query_receipt_sha256=batch.query_receipt_sha256,
            observation_sha256=batch.batch_content_sha256,
            exact_activity_count=len(batch.activities),
            positive_fill_observed=bool(batch.activities),
            pagination_complete=True,
            available_at=batch.available_at,
        )
        pending = _PendingFillRead(
            read=read,
            instruction_sha256=instruction.instruction_sha256,
            batch=batch,
        )
        with self._lock:
            existing = self._pending.get(read.observation_sha256)
            if existing is not None:
                if existing != pending:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_observation_identity_collision"
                    )
                return existing.read
            if len(self._pending) >= self._max_pending_reads:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_pending_capacity_exhausted"
                )
            self._pending[read.observation_sha256] = pending
        return read

    def append_fill_read(
        self,
        read: CapturedPaperFillReadAuthority,
        *,
        instruction: CapturedPaperTransportInstruction,
        fill_handoff_required: bool,
    ) -> CapturedPaperFillAppendReceipt:
        """Publish fill truth and the outbox handoff in one outer transaction."""

        if type(read) is not CapturedPaperFillReadAuthority:
            raise CapturedPaperTransportContractError(
                "fill_capture_read_authority_type_invalid"
            )
        if type(instruction) is not CapturedPaperTransportInstruction:
            raise CapturedPaperTransportContractError(
                "fill_capture_instruction_type_invalid"
            )
        if type(fill_handoff_required) is not bool:
            raise CapturedPaperTransportContractError(
                "fill_capture_handoff_requirement_invalid"
            )
        key = read.observation_sha256
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_prepared_batch_missing"
                )
            if key in self._inflight:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_publication_already_inflight"
                )
            if not (
                pending.read == read
                and pending.instruction_sha256 == instruction.instruction_sha256
                and read.exact_activity_count == len(pending.batch.activities)
                and read.positive_fill_observed
                == bool(pending.batch.activities)
                and fill_handoff_required == read.positive_fill_observed
            ):
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_prepared_batch_binding_mismatch"
                )
            self._inflight.add(key)

        committed = False
        try:
            db = self._factory()
            try:
                with db.begin():
                    publication = (
                        publish_prepared_alpaca_paper_entry_fill_batch(
                            db,
                            pending.batch,
                        )
                    )
                    proof = publication.handoff_proof
                    handed = None
                    if fill_handoff_required:
                        if proof is None:
                            raise CapturedPaperTransportUnavailable(
                                "fill_capture_positive_handoff_proof_missing"
                            )
                        handed = commit_captured_paper_fill_handoff(
                            db,
                            completion_sha256=(
                                instruction.request.completion_sha256
                            ),
                            authority=instruction.authority,
                            proof=proof,
                        )
                        if not (
                            handed.status
                            == OUTBOX_STATUS_FILL_HANDOFF_COMMITTED
                            and handed.fill_handoff_proof_sha256
                            == proof.proof_sha256
                            and handed.fill_handoff_receipt_sha256 is not None
                            and handed.fill_handoff_committed_at is not None
                        ):
                            raise CapturedPaperTransportUnavailable(
                                "fill_capture_outbox_handoff_unconfirmed"
                            )
                        committed_at = handed.fill_handoff_committed_at
                        durable_receipt_sha256 = (
                            handed.fill_handoff_receipt_sha256
                        )
                    else:
                        committed_at = db.execute(
                            text("SELECT clock_timestamp()")
                        ).scalar_one()
                        durable_receipt_sha256 = (
                            pending.batch.batch_content_sha256
                        )
                    receipt = CapturedPaperFillAppendReceipt(
                        observation_sha256=read.observation_sha256,
                        durable_receipt_sha256=durable_receipt_sha256,
                        committed_at=committed_at.astimezone(UTC),
                        positive_fill_handoff_committed=(
                            fill_handoff_required
                        ),
                        fill_handoff_proof_sha256=(
                            proof.proof_sha256
                            if fill_handoff_required and proof is not None
                            else None
                        ),
                        outbox_fill_handoff_receipt_sha256=(
                            handed.fill_handoff_receipt_sha256
                            if handed is not None
                            else None
                        ),
                    )
                committed = True
                return receipt
            finally:
                db.close()
        finally:
            with self._lock:
                self._inflight.discard(key)
                if committed:
                    self._pending.pop(key, None)


__all__ = ("SqlAlchemyCapturedPaperFillCapture",)
