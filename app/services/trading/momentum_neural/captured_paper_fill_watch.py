"""Bounded zero-POST watcher for accepted captured Alpaca PAPER entries.

The transport outbox is already terminal for POST/reconciliation when this
worker runs.  Its only external capability is an exact broker-order read for
the broker OID frozen by positive acceptance.  Fill activities are then read
and published through the existing split-phase capture seam.  The durable fill
read, rather than an earlier order-status projection, decides whether positive
fill ownership is handed off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
import uuid
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .adaptive_risk_reservation import (
    AdaptiveRiskReservationStore,
    DurableOrderLifecycleEvidence,
)
from .captured_paper_outbox import (
    CapturedPaperCompletedFillWatchBundle,
    CapturedPaperCompletedFillWatchLease,
    complete_captured_paper_terminal_zero_fill_watch,
    lease_next_captured_paper_completed_fill_watch,
    load_captured_paper_completed_fill_watch_bundle,
    reschedule_captured_paper_completed_fill_watch,
)
from .alpaca_orphan_claims import resolve_action_claim
from .captured_paper_transport_coordinator import (
    CapturedPaperExactBrokerOrderObservation,
    CapturedPaperFillAppendReceipt,
    CapturedPaperFillReadAuthority,
    CapturedPaperFillReconciliationRequiredObservation,
    CapturedPaperPositiveOrderObservation,
    CapturedPaperTerminalZeroFillObservation,
    CapturedPaperTransportContractError,
    CapturedPaperTransportInstruction,
    EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
)
from ..venue.alpaca_spot import (
    AlpacaSpotAdapter,
    quantize_alpaca_equity_limit_price,
)


UTC = timezone.utc
FILL_WATCH_OUTCOME_SCHEMA_VERSION = (
    "chili.captured-paper-completed-fill-watch-outcome.v1"
)
FILL_WATCH_HEALTH_SCHEMA_VERSION = (
    "chili.captured-paper-completed-fill-watch-worker-health.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_TERMINAL_ZERO_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "rejected"}
)
_EXACT_PAPER_CONNECTION_RECEIPT_METHOD = (
    AlpacaSpotAdapter.get_paper_connection_generation_receipt
)
_EXACT_PAPER_ORDER_TRUTH_METHOD = AlpacaSpotAdapter.get_order_truth


class CapturedPaperCompletedFillWatchError(RuntimeError):
    """A completed-order watch cannot proceed without changing authority."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_fill_watch_error")
        super().__init__(self.reason)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperCompletedFillWatchError(f"{field_name}_invalid")
    return value.astimezone(UTC)


def _identifier(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise CapturedPaperCompletedFillWatchError(f"{field_name}_invalid")
    return normalized


def _digest(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapturedPaperCompletedFillWatchError(f"{field_name}_invalid")
    return normalized


def _canonical_uuid(value: Any, *, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperCompletedFillWatchError(
            f"{field_name}_invalid"
        ) from exc
    if str(parsed) != raw:
        raise CapturedPaperCompletedFillWatchError(f"{field_name}_invalid")
    return raw


@dataclass(frozen=True, slots=True)
class CapturedPaperCompletedFillWatchInstruction:
    transport_instruction: CapturedPaperTransportInstruction
    watch_bundle: CapturedPaperCompletedFillWatchBundle

    def __post_init__(self) -> None:
        if type(self.transport_instruction) is not CapturedPaperTransportInstruction:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_transport_instruction_invalid"
            )
        if type(self.watch_bundle) is not CapturedPaperCompletedFillWatchBundle:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_bundle_invalid"
            )
        durable = self.watch_bundle.durable_transport
        if not (
            self.transport_instruction.instruction_sha256
            == durable.transport_instruction_sha256
            and self.transport_instruction.authority.authority_sha256
            == durable.authority.authority_sha256
            and self.transport_instruction.request.completion_sha256
            == self.watch_bundle.completion_sha256
            and self.watch_bundle.broker_order_id
            == str(self.watch_bundle.broker_order_id).strip()
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_instruction_binding_mismatch"
            )

    @property
    def completion_sha256(self) -> str:
        return self.watch_bundle.completion_sha256

    @property
    def lease(self) -> CapturedPaperCompletedFillWatchLease:
        return self.watch_bundle.lease

    @property
    def broker_order_id(self) -> str:
        return self.watch_bundle.broker_order_id

    @property
    def broker_connection_generation(self) -> str:
        return self.watch_bundle.broker_connection_generation


@dataclass(frozen=True, slots=True)
class CapturedPaperFillWatchUnavailableObservation:
    completion_sha256: str
    broker_order_id: str
    reason: str
    evidence_sha256: str
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "completion_sha256",
            _digest(self.completion_sha256, field_name="completion_sha256"),
        )
        object.__setattr__(
            self,
            "broker_order_id",
            _identifier(self.broker_order_id, field_name="broker_order_id"),
        )
        object.__setattr__(
            self, "reason", _identifier(self.reason, field_name="reason")
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _digest(self.evidence_sha256, field_name="evidence_sha256"),
        )
        object.__setattr__(
            self,
            "available_at",
            _aware_utc(self.available_at, field_name="available_at"),
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperCompletedFillWatchOutcome:
    status: str
    completion_sha256: str
    broker_order_id: str
    observation_sha256: str
    fill_receipt_sha256: str | None = None
    schema_version: str = FILL_WATCH_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FILL_WATCH_OUTCOME_SCHEMA_VERSION:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_outcome_schema_invalid"
            )
        if self.status not in {
            "rescheduled_unavailable",
            "rescheduled_zero_fill",
            "rescheduled_fill_activity_pending",
            "terminal_zero_fill",
            "fill_handoff_committed",
        }:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_outcome_status_invalid"
            )
        object.__setattr__(
            self,
            "completion_sha256",
            _digest(self.completion_sha256, field_name="completion_sha256"),
        )
        object.__setattr__(
            self,
            "broker_order_id",
            _identifier(self.broker_order_id, field_name="broker_order_id"),
        )
        object.__setattr__(
            self,
            "observation_sha256",
            _digest(
                self.observation_sha256,
                field_name="fill_watch_observation_sha256",
            ),
        )
        if self.fill_receipt_sha256 is not None:
            object.__setattr__(
                self,
                "fill_receipt_sha256",
                _digest(
                    self.fill_receipt_sha256,
                    field_name="fill_watch_fill_receipt_sha256",
                ),
            )


class SqlAlchemyCapturedPaperCompletedFillWatchStore:
    """Short PostgreSQL transactions around fill-watch queue authority."""

    def __init__(self, bind: Engine) -> None:
        if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_postgresql_engine_required"
            )
        self._bind = bind
        self._factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        self._reservation_store = AdaptiveRiskReservationStore(bind)

    def _transaction(self, operation: Callable[[Any], Any]) -> Any:
        db = self._factory()
        try:
            with db.begin():
                return operation(db)
        finally:
            db.close()

    def lease_next(
        self,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperCompletedFillWatchLease | None:
        return self._transaction(
            lambda db: lease_next_captured_paper_completed_fill_watch(
                db,
                lease_owner_id=lease_owner_id,
                lease_seconds=lease_seconds,
            )
        )

    def load_instruction(
        self,
        lease: CapturedPaperCompletedFillWatchLease,
    ) -> CapturedPaperCompletedFillWatchInstruction:
        bundle = self._transaction(
            lambda db: load_captured_paper_completed_fill_watch_bundle(
                db,
                lease=lease,
            )
        )
        transport = CapturedPaperTransportInstruction.from_durable_bundle(
            bundle.durable_transport
        )
        return CapturedPaperCompletedFillWatchInstruction(
            transport_instruction=transport,
            watch_bundle=bundle,
        )

    def reschedule(
        self,
        instruction: CapturedPaperCompletedFillWatchInstruction,
        *,
        observation_sha256: str,
        retry_delay_seconds: int,
        reason: str,
    ) -> None:
        self._transaction(
            lambda db: reschedule_captured_paper_completed_fill_watch(
                db,
                lease=instruction.lease,
                observation_sha256=observation_sha256,
                retry_delay_seconds=retry_delay_seconds,
                reason=reason,
            )
        )

    def complete_terminal_zero_fill(
        self,
        instruction: CapturedPaperCompletedFillWatchInstruction,
        *,
        observation: CapturedPaperTerminalZeroFillObservation,
        read: CapturedPaperFillReadAuthority,
        append_receipt: CapturedPaperFillAppendReceipt,
    ) -> str:
        if type(observation) is not CapturedPaperTerminalZeroFillObservation:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_terminal_observation_invalid"
            )
        if type(read) is not CapturedPaperFillReadAuthority or (
            read.positive_fill_observed
            or read.exact_activity_count != 0
            or read.observation_sha256
            != append_receipt.observation_sha256
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_terminal_fill_read_not_zero"
            )
        if type(append_receipt) is not CapturedPaperFillAppendReceipt or (
            append_receipt.positive_fill_handoff_committed
            or append_receipt.fill_handoff_proof_sha256 is not None
            or append_receipt.outbox_fill_handoff_receipt_sha256 is not None
            or append_receipt.durable_receipt_sha256
            != read.observation_sha256
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_terminal_append_receipt_invalid"
            )
        observation.verify_for_instruction(instruction.transport_instruction)
        if not (
            instruction.watch_bundle.broker_observed_at
            <= instruction.watch_bundle.broker_available_at
            <= observation.observed_at
            <= observation.available_at
            <= read.available_at
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_terminal_clock_frontier_invalid"
            )
        normalized_status = (
            "canceled"
            if observation.broker_order_status == "cancelled"
            else observation.broker_order_status
        )
        reason = {
            "rejected": "broker_rejected",
            "canceled": "broker_canceled",
            "expired": "broker_expired",
        }[normalized_status]
        evidence = DurableOrderLifecycleEvidence(
            event_kind="terminal_zero_fill",
            durability_kind="authoritative_broker_event",
            provider_event_id=(
                "captured-paper-fill-watch-terminal:"
                + read.observation_sha256
            ),
            broker_source="alpaca",
            connection_generation=instruction.broker_connection_generation,
            account_scope=instruction.transport_instruction.account_scope,
            execution_family="alpaca_spot",
            broker_environment="paper",
            account_identity_sha256=(
                instruction.transport_instruction.authority.account_identity_sha256
            ),
            client_order_id=(
                instruction.transport_instruction.client_order_id
            ),
            broker_order_id=instruction.broker_order_id,
            observed_at=observation.observed_at,
            available_at=read.available_at,
            event_content_sha256=read.observation_sha256,
            cumulative_filled_quantity=0,
            source_record_table="alpaca_paper_fill_query_observations",
            source_record_id=read.observation_sha256,
            order_status=normalized_status,
        )
        terminal_receipt_sha256 = _sha256_json(
            {
                "schema_version": (
                    "chili.captured-paper-terminal-zero-fill-watch.v1"
                ),
                "completion_sha256": instruction.completion_sha256,
                "broker_order_id": instruction.broker_order_id,
                "observation_sha256": read.observation_sha256,
                "append_receipt_sha256": (
                    append_receipt.durable_receipt_sha256
                ),
                "lifecycle_evidence_sha256": evidence.evidence_sha256,
                "release_reason": reason,
            }
        )

        def commit(db: Any) -> None:
            current = load_captured_paper_completed_fill_watch_bundle(
                db,
                lease=instruction.lease,
            )
            if not (
                current.completion_sha256 == instruction.completion_sha256
                and current.broker_order_id == instruction.broker_order_id
                and current.broker_connection_generation
                == instruction.broker_connection_generation
            ):
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_terminal_reload_mismatch"
                )
            self._reservation_store.release_zero_fill(
                uuid.UUID(
                    instruction.transport_instruction.authority.reservation_id
                ),
                reason=reason,
                evidence=evidence,
                session=db,
            )
            claim_resolved = resolve_action_claim(
                db,
                symbol=(
                    instruction.transport_instruction.symbol
                ),
                claim_token=(
                    instruction.transport_instruction.authority
                    .action_claim_token
                ),
                client_order_id=(
                    instruction.transport_instruction.client_order_id
                ),
                broker_order_id=instruction.broker_order_id,
                broker_order_status=normalized_status,
                zero_fill_terminal=True,
                metadata={
                    "reason": "captured_paper_completed_fill_watch",
                    "observation_sha256": read.observation_sha256,
                    "terminal_receipt_sha256": terminal_receipt_sha256,
                },
                account_scope=(
                    instruction.transport_instruction.account_scope
                ),
            )
            if not claim_resolved:
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_terminal_action_claim_unresolved"
                )
            complete_captured_paper_terminal_zero_fill_watch(
                db,
                lease=instruction.lease,
                observation_sha256=read.observation_sha256,
                terminal_receipt_sha256=terminal_receipt_sha256,
            )

        self._transaction(commit)
        return terminal_receipt_sha256


class ExactAlpacaPaperCompletedFillWatchReader:
    """Exact class-pinned OID lookup with no order-submission method."""

    def __init__(
        self,
        *,
        adapter: Any,
        expected_account_id: str,
        broker_connection_generation: str,
        observation_clock: Callable[[], datetime],
    ) -> None:
        self._adapter = adapter
        self._expected_account_id = _canonical_uuid(
            expected_account_id, field_name="fill_watch_account_id"
        )
        self._connection_generation = _identifier(
            broker_connection_generation,
            field_name="fill_watch_connection_generation",
        )
        if not callable(observation_clock):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_observation_clock_invalid"
            )
        self._clock = observation_clock
        self._assert_adapter_binding()

    def _assert_adapter_binding(self) -> None:
        if type(self._adapter) is not AlpacaSpotAdapter:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_adapter_exact_class_required"
            )
        if (
            AlpacaSpotAdapter.get_paper_connection_generation_receipt
            is not _EXACT_PAPER_CONNECTION_RECEIPT_METHOD
            or AlpacaSpotAdapter.get_order_truth
            is not _EXACT_PAPER_ORDER_TRUTH_METHOD
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_adapter_method_identity_changed"
            )
        if (
            getattr(self._adapter, "broker_environment", None) != "paper"
            or str(
                getattr(self._adapter, "bound_account_id", "") or ""
            ).strip()
            != self._expected_account_id
            or self._adapter.bind_account_id(self._expected_account_id)
            is not True
            or self._adapter.is_enabled() is not True
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_adapter_paper_account_mismatch"
            )

    def _verify_adapter_generation(self) -> tuple[str, datetime]:
        """Rebind every OID lookup to the exact authenticated REST client.

        The durable generation is not merely compared with a caller-supplied
        string.  The exact class method must produce a fresh content-addressed
        receipt for this same adapter/account immediately before the order
        read.  A credential/client replacement therefore fails this cycle
        closed instead of letting a new generation speak for the old order.
        """

        receipt = _EXACT_PAPER_CONNECTION_RECEIPT_METHOD(self._adapter)
        if type(receipt) is not dict:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_connection_receipt_unavailable"
            )
        canonical = receipt.get("receipt_canonical_json")
        supplied_sha256 = str(receipt.get("receipt_sha256") or "").strip()
        body = {
            key: value
            for key, value in receipt.items()
            if key not in {"receipt_canonical_json", "receipt_sha256"}
        }
        try:
            parsed = json.loads(canonical)
        except (TypeError, ValueError) as exc:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_connection_receipt_invalid"
            ) from exc
        receipt_available_at = _aware_utc(
            datetime.fromisoformat(
                str(receipt.get("available_at") or "").replace("Z", "+00:00")
            ),
            field_name="fill_watch_connection_receipt_available_at",
        )
        if not (
            receipt.get("schema_version")
            == "chili.alpaca-paper-connection-generation.v1"
            and receipt.get("broker_environment") == "paper"
            and receipt.get("asset_class") == "us_equity"
            and receipt.get("provider_account_id")
            == self._expected_account_id
            and receipt.get("adapter_connection_generation")
            == self._connection_generation
            and type(canonical) is str
            and parsed == body
            and canonical == _canonical_json(body)
            and _SHA256_RE.fullmatch(supplied_sha256) is not None
            and hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            == supplied_sha256
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_connection_generation_mismatch"
            )
        return supplied_sha256, receipt_available_at

    def _now(self) -> datetime:
        return _aware_utc(
            self._clock(), field_name="fill_watch_observation_clock"
        )

    def _unavailable(
        self,
        instruction: CapturedPaperCompletedFillWatchInstruction,
        *,
        reason: str,
        available_at: datetime,
        detail: Mapping[str, Any],
    ) -> CapturedPaperFillWatchUnavailableObservation:
        evidence = _sha256_json(
            {
                "schema_version": (
                    "chili.captured-paper-fill-watch-unavailable.v1"
                ),
                "completion_sha256": instruction.completion_sha256,
                "transport_instruction_sha256": (
                    instruction.transport_instruction.instruction_sha256
                ),
                "broker_order_id": instruction.broker_order_id,
                "broker_connection_generation": (
                    instruction.broker_connection_generation
                ),
                "reason": reason,
                "available_at": available_at.isoformat(),
                "detail": dict(detail),
            }
        )
        return CapturedPaperFillWatchUnavailableObservation(
            completion_sha256=instruction.completion_sha256,
            broker_order_id=instruction.broker_order_id,
            reason=reason,
            evidence_sha256=evidence,
            available_at=available_at,
        )

    def lookup_exact_order(
        self,
        instruction: CapturedPaperCompletedFillWatchInstruction,
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
        | CapturedPaperTerminalZeroFillObservation
        | CapturedPaperFillWatchUnavailableObservation
    ):
        if type(instruction) is not CapturedPaperCompletedFillWatchInstruction:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_instruction_type_invalid"
            )
        self._assert_adapter_binding()
        if not (
            instruction.transport_instruction.expected_account_id
            == self._expected_account_id
            and instruction.broker_connection_generation
            == self._connection_generation
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_generation_or_account_mismatch"
            )
        try:
            generation_requested_at = self._now()
            (
                generation_receipt_sha256,
                generation_receipt_available_at,
            ) = self._verify_adapter_generation()
            generation_verified_at = self._now()
            if not (
                generation_requested_at <= generation_verified_at
                and generation_requested_at - timedelta(seconds=10)
                <= generation_receipt_available_at
                <= generation_verified_at
            ):
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_connection_generation_clock_invalid"
                )
        except Exception as exc:
            return self._unavailable(
                instruction,
                reason="adapter_generation_unavailable",
                available_at=self._now(),
                detail={"exception_type": type(exc).__name__},
            )
        try:
            truth = _EXACT_PAPER_ORDER_TRUTH_METHOD(
                self._adapter, instruction.broker_order_id
            )
        except Exception as exc:
            return self._unavailable(
                instruction,
                reason="order_read_exception",
                available_at=self._now(),
                detail={"exception_type": type(exc).__name__},
            )
        available_at = self._now()
        if available_at < generation_verified_at:
            return self._unavailable(
                instruction,
                reason="order_clock_regressed",
                available_at=available_at,
                detail={"generation_clock_frontier_invalid": True},
            )
        if type(truth) is not dict or truth.get("readable") is not True:
            return self._unavailable(
                instruction,
                reason="order_unreadable",
                available_at=available_at,
                detail={"truth_shape": type(truth).__name__},
            )
        if truth.get("found") is not True:
            return self._unavailable(
                instruction,
                reason="order_absent_not_terminal",
                available_at=available_at,
                detail={"explicit_oid_absence": True},
            )
        order = truth.get("order")
        raw = getattr(order, "raw", None)
        raw = raw if type(raw) is dict else {}
        broker_echo = {
            key: raw.get(key)
            for key in (
                "broker_account_id_echo",
                "broker_order_id_echo",
                "broker_client_order_id_echo",
                "broker_symbol_echo",
                "broker_side_echo",
                "broker_order_type_echo",
                "broker_quantity_echo",
                "broker_limit_price_echo",
                "broker_time_in_force_echo",
                "broker_extended_hours_echo",
                "broker_order_status_echo",
                "broker_filled_quantity_echo",
                "broker_position_intent_echo",
                "broker_asset_class_echo",
            )
        }
        try:
            raw_quantity = Decimal(
                str(broker_echo["broker_quantity_echo"])
            )
            raw_fill = Decimal(
                str(broker_echo["broker_filled_quantity_echo"])
            )
            if not (
                raw_quantity.is_finite()
                and raw_quantity > 0
                and raw_quantity == raw_quantity.to_integral_value()
                and raw_fill.is_finite()
                and raw_fill >= 0
                and raw_fill == raw_fill.to_integral_value()
            ):
                raise ValueError("non-whole broker economics")
            limit_price = quantize_alpaca_equity_limit_price(
                str(broker_echo["broker_limit_price_echo"]), "buy"
            )
            evidence = _sha256_json(
                {
                    "schema_version": (
                        "chili.captured-paper-fill-watch-order-echo.v1"
                    ),
                    "completion_sha256": instruction.completion_sha256,
                    "transport_instruction_sha256": (
                        instruction.transport_instruction.instruction_sha256
                    ),
                    "broker_order_id": instruction.broker_order_id,
                    "broker_connection_generation": (
                        self._connection_generation
                    ),
                    "generation_receipt_sha256": (
                        generation_receipt_sha256
                    ),
                    "generation_requested_at": (
                        generation_requested_at.isoformat()
                    ),
                    "generation_receipt_available_at": (
                        generation_receipt_available_at.isoformat()
                    ),
                    "generation_verified_at": (
                        generation_verified_at.isoformat()
                    ),
                    "broker_echo": broker_echo,
                    "observed_at": available_at.isoformat(),
                    "available_at": available_at.isoformat(),
                }
            )
            exact = CapturedPaperExactBrokerOrderObservation(
                account_scope=(
                    instruction.transport_instruction.account_scope
                ),
                expected_account_id=self._expected_account_id,
                verified_adapter_account_id=self._expected_account_id,
                account_binding_source=EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
                broker_account_id=broker_echo["broker_account_id_echo"],
                client_order_id=broker_echo[
                    "broker_client_order_id_echo"
                ],
                broker_order_id=broker_echo["broker_order_id_echo"],
                symbol=broker_echo["broker_symbol_echo"],
                side=broker_echo["broker_side_echo"],
                order_type=broker_echo["broker_order_type_echo"],
                asset_class=broker_echo["broker_asset_class_echo"],
                quantity_shares=int(raw_quantity),
                broker_quantity_echo=str(
                    broker_echo["broker_quantity_echo"]
                ),
                broker_filled_quantity_echo=str(
                    broker_echo["broker_filled_quantity_echo"]
                ),
                cumulative_filled_quantity_shares=int(raw_fill),
                limit_price=limit_price,
                broker_limit_price_echo=str(
                    broker_echo["broker_limit_price_echo"]
                ),
                time_in_force=broker_echo["broker_time_in_force_echo"],
                extended_hours=broker_echo["broker_extended_hours_echo"],
                position_intent_echo=broker_echo[
                    "broker_position_intent_echo"
                ],
                broker_order_status=broker_echo[
                    "broker_order_status_echo"
                ],
                broker_order_status_echo=str(
                    broker_echo["broker_order_status_echo"]
                ),
                broker_connection_generation=self._connection_generation,
                broker_order_evidence_sha256=evidence,
                observed_at=available_at,
                available_at=available_at,
            )
            exact.verify_for_instruction(
                instruction.transport_instruction
            )
            if not (
                str(getattr(order, "order_id", "") or "").strip()
                == instruction.broker_order_id
                == exact.broker_order_id
                and str(
                    getattr(order, "client_order_id", "") or ""
                ).strip()
                == exact.client_order_id
                and str(getattr(order, "product_id", "") or "")
                .strip()
                .upper()
                == exact.symbol
                and str(getattr(order, "side", "") or "")
                .strip()
                .lower()
                == "buy"
                and raw.get("fill_truth_readable") is True
                and int(raw.get("filled_size"))
                == exact.cumulative_filled_quantity_shares
            ):
                raise ValueError("normalized order disagrees with exact echo")
            if exact.cumulative_filled_quantity_shares > 0 or (
                exact.broker_order_status
                in {"filled", "partially_filled"}
            ):
                return CapturedPaperFillReconciliationRequiredObservation(
                    order=exact
                )
            if exact.broker_order_status in _TERMINAL_ZERO_STATUSES:
                return CapturedPaperTerminalZeroFillObservation(order=exact)
            if exact.broker_order_status in {"accepted", "new"}:
                return CapturedPaperPositiveOrderObservation(order=exact)
            raise ValueError("unsupported broker order state")
        except (
            CapturedPaperTransportContractError,
            InvalidOperation,
            KeyError,
            TypeError,
            ValueError,
        ):
            return self._unavailable(
                instruction,
                reason="order_economics_ambiguous",
                available_at=available_at,
                detail={"found_but_exact_binding_failed": True},
            )


class _FillCapture(Protocol):
    def read_exact_order_fills(
        self,
        instruction: CapturedPaperTransportInstruction,
        observation: Any,
    ) -> CapturedPaperFillReadAuthority: ...

    def append_fill_read(
        self,
        read: CapturedPaperFillReadAuthority,
        *,
        instruction: CapturedPaperTransportInstruction,
        fill_handoff_required: bool,
    ) -> CapturedPaperFillAppendReceipt: ...


class CapturedPaperCompletedFillWatchCoordinator:
    """One bounded accepted-order read and split-phase fill publication."""

    def __init__(
        self,
        *,
        store: SqlAlchemyCapturedPaperCompletedFillWatchStore,
        reader: ExactAlpacaPaperCompletedFillWatchReader,
        fill_capture: _FillCapture,
        retry_delay_seconds: int,
    ) -> None:
        for component, methods, label in (
            (store, ("lease_next", "load_instruction", "reschedule",
                     "complete_terminal_zero_fill"), "store"),
            (reader, ("lookup_exact_order",), "reader"),
            (fill_capture, ("read_exact_order_fills", "append_fill_read"),
             "fill_capture"),
        ):
            if any(not callable(getattr(component, name, None)) for name in methods):
                raise CapturedPaperCompletedFillWatchError(
                    f"fill_watch_{label}_capability_unavailable"
                )
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int)
            or retry_delay_seconds <= 0
            or retry_delay_seconds > 86_400
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_retry_delay_invalid"
            )
        self._store = store
        self._reader = reader
        self._fill = fill_capture
        self._retry_delay_seconds = retry_delay_seconds

    def run_one_cycle(
        self,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperCompletedFillWatchOutcome | None:
        lease = self._store.lease_next(
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return None
        instruction = self._store.load_instruction(lease)
        observation = self._reader.lookup_exact_order(instruction)
        if type(observation) is CapturedPaperFillWatchUnavailableObservation:
            if (
                observation.available_at
                < instruction.watch_bundle.broker_available_at
            ):
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_unavailable_clock_frontier_invalid"
                )
            self._store.reschedule(
                instruction,
                observation_sha256=observation.evidence_sha256,
                retry_delay_seconds=self._retry_delay_seconds,
                reason="broker_read_unavailable",
            )
            return CapturedPaperCompletedFillWatchOutcome(
                status="rescheduled_unavailable",
                completion_sha256=instruction.completion_sha256,
                broker_order_id=instruction.broker_order_id,
                observation_sha256=observation.evidence_sha256,
            )
        if type(observation) not in {
            CapturedPaperPositiveOrderObservation,
            CapturedPaperFillReconciliationRequiredObservation,
            CapturedPaperTerminalZeroFillObservation,
        }:
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_reader_result_invalid"
            )
        observation.verify_for_instruction(instruction.transport_instruction)
        if not (
            instruction.watch_bundle.broker_observed_at
            <= instruction.watch_bundle.broker_available_at
            <= observation.observed_at
            <= observation.available_at
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_order_clock_frontier_invalid"
            )
        read = self._fill.read_exact_order_fills(
            instruction.transport_instruction,
            observation,
        )
        if not (
            type(read) is CapturedPaperFillReadAuthority
            and read.reservation_id
            == instruction.transport_instruction.authority.reservation_id
            and read.client_order_id
            == instruction.transport_instruction.client_order_id
            and read.broker_order_id == instruction.broker_order_id
            and observation.available_at <= read.available_at
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_fill_read_binding_mismatch"
            )
        receipt = self._fill.append_fill_read(
            read,
            instruction=instruction.transport_instruction,
            fill_handoff_required=read.positive_fill_observed,
        )
        if not (
            type(receipt) is CapturedPaperFillAppendReceipt
            and receipt.observation_sha256 == read.observation_sha256
            and receipt.positive_fill_handoff_committed
            == read.positive_fill_observed
            and read.available_at <= receipt.committed_at
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_append_receipt_mismatch"
            )
        if read.positive_fill_observed:
            if not (
                receipt.fill_handoff_proof_sha256 is not None
                and receipt.outbox_fill_handoff_receipt_sha256 is not None
            ):
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_positive_handoff_unconfirmed"
                )
            return CapturedPaperCompletedFillWatchOutcome(
                status="fill_handoff_committed",
                completion_sha256=instruction.completion_sha256,
                broker_order_id=instruction.broker_order_id,
                observation_sha256=read.observation_sha256,
                fill_receipt_sha256=(
                    receipt.outbox_fill_handoff_receipt_sha256
                ),
            )
        if type(observation) is CapturedPaperTerminalZeroFillObservation:
            terminal_receipt = self._store.complete_terminal_zero_fill(
                instruction,
                observation=observation,
                read=read,
                append_receipt=receipt,
            )
            return CapturedPaperCompletedFillWatchOutcome(
                status="terminal_zero_fill",
                completion_sha256=instruction.completion_sha256,
                broker_order_id=instruction.broker_order_id,
                observation_sha256=read.observation_sha256,
                fill_receipt_sha256=terminal_receipt,
            )
        reason = (
            "fill_activity_not_yet_available"
            if type(observation)
            is CapturedPaperFillReconciliationRequiredObservation
            else "zero_fill_working"
        )
        self._store.reschedule(
            instruction,
            observation_sha256=read.observation_sha256,
            retry_delay_seconds=self._retry_delay_seconds,
            reason=reason,
        )
        return CapturedPaperCompletedFillWatchOutcome(
            status=(
                "rescheduled_fill_activity_pending"
                if reason == "fill_activity_not_yet_available"
                else "rescheduled_zero_fill"
            ),
            completion_sha256=instruction.completion_sha256,
            broker_order_id=instruction.broker_order_id,
            observation_sha256=read.observation_sha256,
            fill_receipt_sha256=receipt.durable_receipt_sha256,
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperCompletedFillWatchWorkerHealth:
    schema_version: str
    worker_id: str
    ever_started: bool
    running: bool
    stop_requested: bool
    fatal: bool
    fatal_error_type: str | None
    cycles_completed: int
    work_outcomes: int
    idle_cycles: int
    last_outcome_status: str | None
    last_completion_sha256: str | None
    last_cycle_completed_at: datetime | None

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "ever_started": self.ever_started,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "fatal": self.fatal,
            "fatal_error_type": self.fatal_error_type,
            "cycles_completed": self.cycles_completed,
            "work_outcomes": self.work_outcomes,
            "idle_cycles": self.idle_cycles,
            "last_outcome_status": self.last_outcome_status,
            "last_completion_sha256": self.last_completion_sha256,
            "last_cycle_completed_at": (
                None
                if self.last_cycle_completed_at is None
                else self.last_cycle_completed_at.isoformat()
            ),
        }


class CapturedPaperCompletedFillWatchWorker:
    """Supervisor-compatible bounded worker; unknown faults are terminal."""

    def __init__(
        self,
        *,
        coordinator: CapturedPaperCompletedFillWatchCoordinator,
        worker_id: str,
        lease_seconds: int,
        idle_poll_seconds: float,
        observation_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(coordinator, "run_one_cycle", None)):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_coordinator_invalid"
            )
        self._worker_id = _canonical_uuid(
            worker_id, field_name="fill_watch_worker_id"
        )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
            or lease_seconds > 86_400
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_worker_lease_seconds_invalid"
            )
        if (
            isinstance(idle_poll_seconds, bool)
            or not isinstance(idle_poll_seconds, (int, float))
            or not 0.01 <= float(idle_poll_seconds) <= 60.0
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_worker_idle_poll_invalid"
            )
        clock = observation_clock or (lambda: datetime.now(UTC))
        if not callable(clock):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_worker_clock_invalid"
            )
        self._coordinator = coordinator
        self._lease_seconds = lease_seconds
        self._idle_poll_seconds = float(idle_poll_seconds)
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running_ready = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ever_started = False
        self._running = False
        self._fatal_error_type: str | None = None
        self._cycles_completed = 0
        self._work_outcomes = 0
        self._idle_cycles = 0
        self._last_outcome_status: str | None = None
        self._last_completion_sha256: str | None = None
        self._last_cycle_completed_at: datetime | None = None

    def _now(self) -> datetime:
        return _aware_utc(
            self._clock(), field_name="fill_watch_worker_clock"
        )

    def run_one_cycle(
        self,
    ) -> CapturedPaperCompletedFillWatchOutcome | None:
        if not self._cycle_lock.acquire(blocking=False):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_worker_cycle_already_running"
            )
        try:
            outcome = self._coordinator.run_one_cycle(
                lease_owner_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
            if outcome is not None and type(outcome) is not CapturedPaperCompletedFillWatchOutcome:
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_worker_outcome_invalid"
                )
            completed_at = self._now()
            with self._state_lock:
                self._cycles_completed += 1
                self._last_cycle_completed_at = completed_at
                if outcome is None:
                    self._idle_cycles += 1
                    self._last_outcome_status = None
                    self._last_completion_sha256 = None
                else:
                    self._work_outcomes += 1
                    self._last_outcome_status = outcome.status
                    self._last_completion_sha256 = outcome.completion_sha256
            return outcome
        finally:
            self._cycle_lock.release()

    def _run(self) -> None:
        with self._state_lock:
            self._running = True
        self._running_ready.set()
        try:
            while not self._stop.is_set():
                try:
                    outcome = self.run_one_cycle()
                except Exception as exc:
                    with self._state_lock:
                        self._fatal_error_type = type(exc).__name__
                    self._stop.set()
                    self._wake.set()
                    break
                if outcome is None:
                    self._wake.wait(self._idle_poll_seconds)
                    self._wake.clear()
        finally:
            with self._state_lock:
                self._running = False

    def start(self) -> None:
        with self._state_lock:
            if self._ever_started:
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_worker_start_is_one_shot"
                )
            self._ever_started = True
            self._thread = threading.Thread(
                target=self._run,
                name="chili-captured-paper-fill-watch",
                daemon=False,
            )
            thread = self._thread
        thread.start()
        if not self._running_ready.wait(5.0):
            self._stop.set()
            self._wake.set()
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_worker_start_unconfirmed"
            )

    def wake(self) -> None:
        self._wake.set()

    def close(self, *, join_timeout_seconds: float) -> None:
        if (
            isinstance(join_timeout_seconds, bool)
            or not isinstance(join_timeout_seconds, (int, float))
            or not 0.01 <= float(join_timeout_seconds) <= 300.0
        ):
            raise CapturedPaperCompletedFillWatchError(
                "fill_watch_worker_join_timeout_invalid"
            )
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(float(join_timeout_seconds))
            if thread.is_alive():
                raise CapturedPaperCompletedFillWatchError(
                    "fill_watch_worker_did_not_join"
                )

    def health(self) -> CapturedPaperCompletedFillWatchWorkerHealth:
        with self._state_lock:
            thread = self._thread
            return CapturedPaperCompletedFillWatchWorkerHealth(
                schema_version=FILL_WATCH_HEALTH_SCHEMA_VERSION,
                worker_id=self._worker_id,
                ever_started=self._ever_started,
                running=bool(
                    self._running
                    and thread is not None
                    and thread.is_alive()
                ),
                stop_requested=self._stop.is_set(),
                fatal=self._fatal_error_type is not None,
                fatal_error_type=self._fatal_error_type,
                cycles_completed=self._cycles_completed,
                work_outcomes=self._work_outcomes,
                idle_cycles=self._idle_cycles,
                last_outcome_status=self._last_outcome_status,
                last_completion_sha256=self._last_completion_sha256,
                last_cycle_completed_at=self._last_cycle_completed_at,
            )


__all__ = (
    "CapturedPaperCompletedFillWatchCoordinator",
    "CapturedPaperCompletedFillWatchError",
    "CapturedPaperCompletedFillWatchInstruction",
    "CapturedPaperCompletedFillWatchOutcome",
    "CapturedPaperCompletedFillWatchWorker",
    "CapturedPaperCompletedFillWatchWorkerHealth",
    "CapturedPaperFillWatchUnavailableObservation",
    "ExactAlpacaPaperCompletedFillWatchReader",
    "SqlAlchemyCapturedPaperCompletedFillWatchStore",
)
