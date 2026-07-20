"""Production split-phase fill capture for captured Alpaca PAPER orders.

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
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import threading
import uuid
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
    AlpacaPaperFillActivity,
    AlpacaPaperFillObservationActivity,
    AlpacaPaperFillQueryObservation,
    AlpacaPaperPostSettlementFillContradiction,
)

from .adaptive_risk_account_lock import acquire_adaptive_risk_account_locks
from .adaptive_risk_reservation import (
    AdaptiveExitOwnerReceipt,
    AdaptiveReservationError,
    AdaptiveRiskReservationStore,
)
from .alpaca_fill_activity import (
    AlpacaPaperFillCycleBinding,
    AlpacaPaperEntryFillPublicationResult,
    PreparedAlpacaPaperFillBatch,
    append_prepared_alpaca_paper_fill_batch,
    publish_prepared_alpaca_paper_post_settlement_fill_batch,
    publish_prepared_alpaca_paper_entry_fill_batch,
    read_verified_alpaca_paper_exit_fill_batch,
    read_verified_alpaca_paper_fill_batch,
    verify_alpaca_paper_fill_activity_chain,
    verify_alpaca_paper_post_settlement_fill_contradiction_row,
)
from .captured_paper_outbox import (
    OUTBOX_STATUS_FILL_HANDOFF_COMMITTED,
    commit_captured_paper_fill_handoff,
)
from .alpaca_paper_identity import (
    AlpacaPaperAccountIdentityError,
    alpaca_paper_account_identity_sha256,
    canonical_alpaca_paper_account_id,
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
CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY = (
    "captured_paper_exit_fill_post_commit_request"
)
CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY = (
    "captured_paper_exit_transport_post_commit_request"
)
_EXIT_FILL_POST_COMMIT_SCHEMA_VERSION = (
    "chili.captured-paper-exit-fill-post-commit.v1"
)
_EXIT_TRANSPORT_POST_COMMIT_SCHEMA_VERSION = (
    "chili.captured-paper-exit-transport-post-commit.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,35}$")
_TERMINAL_ZERO_EXIT_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "failed", "rejected", "voided"}
)


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verified_json_object(
    canonical_json: Any,
    expected_sha256: Any,
    field: str,
) -> Mapping[str, Any]:
    if not (
        isinstance(canonical_json, str)
        and isinstance(expected_sha256, str)
        and _SHA256_RE.fullmatch(expected_sha256) is not None
        and hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        == expected_sha256
    ):
        raise CapturedPaperTransportUnavailable(
            f"fill_capture_{field}_hash_mismatch"
        )
    try:
        parsed = json.loads(canonical_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapturedPaperTransportUnavailable(
            f"fill_capture_{field}_malformed"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise CapturedPaperTransportUnavailable(
            f"fill_capture_{field}_malformed"
        )
    return parsed


def _exit_contract_text(value: Any, field: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CapturedPaperTransportContractError(
            f"exit_post_commit_{field}_invalid"
        )
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise CapturedPaperTransportContractError(
            f"exit_post_commit_{field}_invalid"
        )
    return value


def _exit_contract_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CapturedPaperTransportContractError(
            f"exit_post_commit_{field}_invalid"
        )
    return value


@dataclass(frozen=True, slots=True)
class CapturedPaperExitFillPostCommitRequest:
    """Content-addressed authority for one already-committed PAPER exit read.

    This value is deliberately broker-read-only.  It can authorize loading one
    opaque durable exit-owner receipt and reading that receipt's exact OID/CID;
    it contains no order placement, cancel, replace, or fallback instruction.
    """

    schema_version: str
    session_id: int
    reservation_id: uuid.UUID
    decision_packet_sha256: str
    account_scope: str
    expected_account_id: str
    account_identity_sha256: str
    runtime_generation: str
    broker_connection_generation: str
    execution_family: str
    symbol: str
    entry_client_order_id: str
    exit_client_order_id: str
    provider_order_id: str
    exit_owner_receipt_sha256: str
    request_sha256: str

    def _content_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "reservation_id": str(self.reservation_id),
            "decision_packet_sha256": self.decision_packet_sha256,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "account_identity_sha256": self.account_identity_sha256,
            "runtime_generation": self.runtime_generation,
            "broker_connection_generation": self.broker_connection_generation,
            "execution_family": self.execution_family,
            "symbol": self.symbol,
            "entry_client_order_id": self.entry_client_order_id,
            "exit_client_order_id": self.exit_client_order_id,
            "provider_order_id": self.provider_order_id,
            "exit_owner_receipt_sha256": self.exit_owner_receipt_sha256,
        }

    def verify(self) -> "CapturedPaperExitFillPostCommitRequest":
        if (
            self.schema_version != _EXIT_FILL_POST_COMMIT_SCHEMA_VERSION
            or isinstance(self.session_id, bool)
            or not isinstance(self.session_id, int)
            or self.session_id <= 0
            or type(self.reservation_id) is not uuid.UUID
            or self.account_scope != "alpaca:paper"
            or self.execution_family != "alpaca_spot"
        ):
            raise CapturedPaperTransportContractError(
                "exit_post_commit_request_contract_invalid"
            )
        try:
            account_id = canonical_alpaca_paper_account_id(
                self.expected_account_id
            )
            runtime_generation = str(uuid.UUID(self.runtime_generation))
        except (AlpacaPaperAccountIdentityError, ValueError, AttributeError):
            raise CapturedPaperTransportContractError(
                "exit_post_commit_runtime_identity_invalid"
            ) from None
        if (
            account_id != self.expected_account_id
            or runtime_generation != self.runtime_generation
            or alpaca_paper_account_identity_sha256(account_id)
            != self.account_identity_sha256
        ):
            raise CapturedPaperTransportContractError(
                "exit_post_commit_runtime_identity_mismatch"
            )
        for field in (
            "decision_packet_sha256",
            "account_identity_sha256",
            "exit_owner_receipt_sha256",
            "request_sha256",
        ):
            _exit_contract_sha256(getattr(self, field), field)
        for field in (
            "broker_connection_generation",
            "entry_client_order_id",
            "exit_client_order_id",
            "provider_order_id",
        ):
            _exit_contract_text(getattr(self, field), field)
        if (
            self.entry_client_order_id == self.exit_client_order_id
            or not isinstance(self.symbol, str)
            or self.symbol != self.symbol.strip().upper()
            or _EQUITY_SYMBOL_RE.fullmatch(self.symbol) is None
            or self.symbol.endswith(".")
            or ".." in self.symbol
        ):
            raise CapturedPaperTransportContractError(
                "exit_post_commit_order_identity_invalid"
            )
        if _canonical_sha256(self._content_body()) != self.request_sha256:
            raise CapturedPaperTransportContractError(
                "exit_post_commit_request_hash_mismatch"
            )
        return self

    @classmethod
    def build(
        cls,
        *,
        session_id: int,
        reservation_id: uuid.UUID | str,
        decision_packet_sha256: str,
        expected_account_id: str,
        account_identity_sha256: str,
        runtime_generation: str,
        broker_connection_generation: str,
        symbol: str,
        entry_client_order_id: str,
        exit_client_order_id: str,
        provider_order_id: str,
        exit_owner_receipt_sha256: str,
        account_scope: str = "alpaca:paper",
        execution_family: str = "alpaca_spot",
    ) -> "CapturedPaperExitFillPostCommitRequest":
        try:
            normalized_reservation_id = uuid.UUID(str(reservation_id))
        except (ValueError, TypeError, AttributeError):
            raise CapturedPaperTransportContractError(
                "exit_post_commit_reservation_id_invalid"
            ) from None
        body = {
            "schema_version": _EXIT_FILL_POST_COMMIT_SCHEMA_VERSION,
            "session_id": session_id,
            "reservation_id": str(normalized_reservation_id),
            "decision_packet_sha256": decision_packet_sha256,
            "account_scope": account_scope,
            "expected_account_id": expected_account_id,
            "account_identity_sha256": account_identity_sha256,
            "runtime_generation": runtime_generation,
            "broker_connection_generation": broker_connection_generation,
            "execution_family": execution_family,
            "symbol": symbol,
            "entry_client_order_id": entry_client_order_id,
            "exit_client_order_id": exit_client_order_id,
            "provider_order_id": provider_order_id,
            "exit_owner_receipt_sha256": exit_owner_receipt_sha256,
        }
        values = {**body, "reservation_id": normalized_reservation_id}
        return cls(**values, request_sha256=_canonical_sha256(body)).verify()


@dataclass(frozen=True, slots=True)
class CapturedPaperExitTransportPostCommitRequest:
    """Content-addressed staged PAPER exit; never broker authority by itself."""

    schema_version: str
    session_id: int
    reservation_id: uuid.UUID
    decision_packet_sha256: str
    account_scope: str
    expected_account_id: str
    account_identity_sha256: str
    session_owner_content_sha256: str
    runtime_generation: str
    broker_connection_generation: str
    execution_family: str
    symbol: str
    entry_client_order_id: str
    exit_client_order_id: str
    transport_claim_token: str
    transport_owner_generation: int
    transport_owner_kind: str
    transport_lease_id: str
    order_request_canonical_json: str
    order_request_sha256: str
    session_position_quantity: str
    exit_reason: str
    attempt_no: int
    quote_independent_authority: bool
    bbo_required: bool
    bbo_max_age_seconds: str
    request_sha256: str

    def _content_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "reservation_id": str(self.reservation_id),
            "decision_packet_sha256": self.decision_packet_sha256,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "account_identity_sha256": self.account_identity_sha256,
            "session_owner_content_sha256": self.session_owner_content_sha256,
            "runtime_generation": self.runtime_generation,
            "broker_connection_generation": self.broker_connection_generation,
            "execution_family": self.execution_family,
            "symbol": self.symbol,
            "entry_client_order_id": self.entry_client_order_id,
            "exit_client_order_id": self.exit_client_order_id,
            "transport_claim_token": self.transport_claim_token,
            "transport_owner_generation": self.transport_owner_generation,
            "transport_owner_kind": self.transport_owner_kind,
            "transport_lease_id": self.transport_lease_id,
            "order_request_canonical_json": self.order_request_canonical_json,
            "order_request_sha256": self.order_request_sha256,
            "session_position_quantity": self.session_position_quantity,
            "exit_reason": self.exit_reason,
            "attempt_no": self.attempt_no,
            "quote_independent_authority": self.quote_independent_authority,
            "bbo_required": self.bbo_required,
            "bbo_max_age_seconds": self.bbo_max_age_seconds,
        }

    @property
    def order_request(self) -> dict[str, Any]:
        parsed = _verified_json_object(
            self.order_request_canonical_json,
            self.order_request_sha256,
            "exit_transport_order_request",
        )
        return dict(parsed)

    def marker(self) -> dict[str, Any]:
        return {**self._content_body(), "request_sha256": self.request_sha256}

    def verify(self) -> "CapturedPaperExitTransportPostCommitRequest":
        if (
            self.schema_version != _EXIT_TRANSPORT_POST_COMMIT_SCHEMA_VERSION
            or isinstance(self.session_id, bool)
            or not isinstance(self.session_id, int)
            or self.session_id <= 0
            or type(self.reservation_id) is not uuid.UUID
            or self.account_scope != "alpaca:paper"
            or self.execution_family != "alpaca_spot"
            or isinstance(self.transport_owner_generation, bool)
            or not isinstance(self.transport_owner_generation, int)
            or self.transport_owner_generation <= 0
            or isinstance(self.attempt_no, bool)
            or not isinstance(self.attempt_no, int)
            or self.attempt_no <= 0
            or type(self.quote_independent_authority) is not bool
            or type(self.bbo_required) is not bool
        ):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_contract_invalid"
            )
        try:
            account_id = canonical_alpaca_paper_account_id(
                self.expected_account_id
            )
            runtime_generation = str(uuid.UUID(self.runtime_generation))
            position_quantity = Decimal(self.session_position_quantity)
            bbo_max_age = Decimal(self.bbo_max_age_seconds)
        except (
            AlpacaPaperAccountIdentityError,
            ValueError,
            TypeError,
            AttributeError,
            InvalidOperation,
        ):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_runtime_identity_invalid"
            ) from None
        if (
            account_id != self.expected_account_id
            or runtime_generation != self.runtime_generation
            or alpaca_paper_account_identity_sha256(account_id)
            != self.account_identity_sha256
            or not position_quantity.is_finite()
            or position_quantity <= 0
            or not bbo_max_age.is_finite()
            or bbo_max_age < 0
            or bbo_max_age > 2
        ):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_runtime_identity_mismatch"
            )
        for field in (
            "decision_packet_sha256",
            "account_identity_sha256",
            "session_owner_content_sha256",
            "order_request_sha256",
            "request_sha256",
        ):
            _exit_contract_sha256(getattr(self, field), field)
        for field in (
            "broker_connection_generation",
            "entry_client_order_id",
            "exit_client_order_id",
            "transport_claim_token",
            "transport_lease_id",
            "exit_reason",
        ):
            _exit_contract_text(getattr(self, field), field)
        if (
            self.entry_client_order_id == self.exit_client_order_id
            or self.transport_owner_kind
            not in {"ordinary_exit", "emergency_exit"}
            or not isinstance(self.symbol, str)
            or self.symbol != self.symbol.strip().upper()
            or _EQUITY_SYMBOL_RE.fullmatch(self.symbol) is None
            or self.symbol.endswith(".")
            or ".." in self.symbol
        ):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_order_identity_invalid"
            )
        request = self.order_request
        expected_request_keys = {
            "account_scope",
            "alpaca_account_id",
            "product_id",
            "side",
            "base_size",
            "client_order_id",
            "position_intent",
            "order_type",
            "time_in_force",
            "extended_hours",
            "limit_price",
        }
        try:
            request_quantity = Decimal(str(request.get("base_size")))
        except (InvalidOperation, TypeError, ValueError):
            request_quantity = Decimal("NaN")
        order_type = str(request.get("order_type") or "").strip().lower()
        limit_price = request.get("limit_price")
        try:
            normalized_limit = (
                None if limit_price is None else Decimal(str(limit_price))
            )
        except (InvalidOperation, TypeError, ValueError):
            normalized_limit = Decimal("NaN")
        canonical_quantity = (
            format(request_quantity, "f")
            if request_quantity.is_finite()
            else ""
        )
        if "." in canonical_quantity:
            canonical_quantity = canonical_quantity.rstrip("0").rstrip(".")
        if canonical_quantity.startswith("."):
            canonical_quantity = "0" + canonical_quantity
        if not (
            set(request) == expected_request_keys
            and request.get("account_scope") == "alpaca:paper"
            and request.get("alpaca_account_id") == self.expected_account_id
            and request.get("product_id") == self.symbol
            and request.get("side") == "sell"
            and request.get("position_intent") == "sell_to_close"
            and request.get("client_order_id") == self.exit_client_order_id
            and request_quantity.is_finite()
            and request_quantity > 0
            and type(request.get("base_size")) is str
            and str(request.get("base_size")) == canonical_quantity
            and request_quantity <= position_quantity
            and order_type in {"market", "limit"}
            and request.get("order_type") == order_type
            and request.get("time_in_force") in {"day", "gtc"}
            and type(request.get("extended_hours")) is bool
            and (self.bbo_required or self.quote_independent_authority)
            and (
                (
                    order_type == "market"
                    and limit_price is None
                    and request.get("time_in_force") == "day"
                    and request.get("extended_hours") is False
                )
                or (
                    order_type == "limit"
                    and normalized_limit is not None
                    and normalized_limit.is_finite()
                    and normalized_limit > 0
                    and (
                        request.get("extended_hours") is False
                        or request.get("time_in_force") == "day"
                    )
                )
            )
            and _canonical_sha256(self._content_body())
            == self.request_sha256
        ):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_request_mismatch"
            )
        return self

    @classmethod
    def build(cls, **values: Any) -> "CapturedPaperExitTransportPostCommitRequest":
        try:
            reservation_id = uuid.UUID(str(values["reservation_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_reservation_id_invalid"
            ) from None
        request = dict(values.pop("order_request"))
        request_json = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        values = {
            "schema_version": _EXIT_TRANSPORT_POST_COMMIT_SCHEMA_VERSION,
            **values,
            "reservation_id": reservation_id,
            "order_request_canonical_json": request_json,
            "order_request_sha256": hashlib.sha256(
                request_json.encode("utf-8")
            ).hexdigest(),
        }
        body = {
            **values,
            "reservation_id": str(reservation_id),
        }
        return cls(**values, request_sha256=_canonical_sha256(body)).verify()

    @classmethod
    def from_marker(
        cls,
        marker: Mapping[str, Any],
    ) -> "CapturedPaperExitTransportPostCommitRequest":
        if not isinstance(marker, Mapping):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_marker_invalid"
            )
        values = dict(marker)
        try:
            values["reservation_id"] = uuid.UUID(str(values["reservation_id"]))
            request = cls(**values)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CapturedPaperTransportContractError(
                "exit_transport_post_commit_marker_invalid"
            ) from None
        return request.verify()

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
        self._reservation_store = AdaptiveRiskReservationStore(bind)
        self._max_pending_reads = max_pending_reads
        self._factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        self._pending: dict[str, _PendingFillRead] = {}
        self._inflight: set[str] = set()
        self._exit_inflight: set[tuple[uuid.UUID, str]] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _verify_exit_owner_context(
        request: CapturedPaperExitFillPostCommitRequest,
        owner_receipt: AdaptiveExitOwnerReceipt,
        cycle: AlpacaPaperFillCycleBinding,
    ) -> None:
        if type(request) is not CapturedPaperExitFillPostCommitRequest:
            raise CapturedPaperTransportContractError(
                "exit_post_commit_request_type_invalid"
            )
        request.verify()
        if type(owner_receipt) is not AdaptiveExitOwnerReceipt:
            raise CapturedPaperTransportContractError(
                "fill_capture_exit_owner_receipt_type_invalid"
            )
        if type(cycle) is not AlpacaPaperFillCycleBinding:
            raise CapturedPaperTransportContractError(
                "fill_capture_exit_cycle_type_invalid"
            )
        binding = owner_receipt.binding
        if not (
            owner_receipt.receipt_sha256
            == request.exit_owner_receipt_sha256
            and owner_receipt.reservation_id == request.reservation_id
            and owner_receipt.provider_order_id == request.provider_order_id
            and owner_receipt.provider_client_order_id
            == request.exit_client_order_id
            and owner_receipt.event_type
            in {"alpaca_exit_owner_submitted", "alpaca_exit_owner_reconciled"}
            and owner_receipt.observer_session_id == request.session_id
            and binding.reservation_id == request.reservation_id
            and binding.expected_account_id == request.expected_account_id
            and binding.account_identity_sha256
            == request.account_identity_sha256
            and binding.decision_packet_sha256
            == request.decision_packet_sha256
            and binding.account_scope == request.account_scope
            and binding.execution_family == request.execution_family
            and binding.symbol == request.symbol
            and binding.entry_client_order_id == request.entry_client_order_id
            and binding.exit_client_order_id == request.exit_client_order_id
            and cycle.reservation_id == request.reservation_id
            and cycle.decision_packet_sha256
            == request.decision_packet_sha256
            and cycle.account_scope == request.account_scope
            and cycle.account_identity_sha256
            == request.account_identity_sha256
            and cycle.execution_family == request.execution_family
            and cycle.symbol == request.symbol
            and cycle.cycle_client_order_id == request.entry_client_order_id
            and cycle.entry_provider_order_id != request.provider_order_id
        ):
            raise CapturedPaperTransportUnavailable(
                "exit_post_commit_durable_owner_mismatch"
            )

    def load_exit_owner_context(
        self,
        request: CapturedPaperExitFillPostCommitRequest,
    ) -> tuple[AdaptiveExitOwnerReceipt, AlpacaPaperFillCycleBinding]:
        """Load opaque owner authority and immutable cycle in one short txn."""

        if type(request) is not CapturedPaperExitFillPostCommitRequest:
            raise CapturedPaperTransportContractError(
                "exit_post_commit_request_type_invalid"
            )
        request.verify()
        db = self._factory()
        try:
            with db.begin():
                try:
                    owner_receipt = self._reservation_store.load_exit_owner_receipt(
                        request.exit_owner_receipt_sha256,
                        reservation_id=request.reservation_id,
                        session=db,
                    )
                except AdaptiveReservationError as exc:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_exit_owner_receipt_unavailable"
                    ) from exc
                reservation = db.scalar(
                    select(AdaptiveRiskReservation).where(
                        AdaptiveRiskReservation.reservation_id
                        == request.reservation_id
                    )
                )
                if reservation is None:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_reservation_missing"
                    )
                packet = db.get(
                    AdaptiveRiskDecisionPacket,
                    reservation.decision_packet_sha256,
                )
                if packet is None:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_decision_packet_missing"
                    )
                cycle = AlpacaPaperFillCycleBinding.from_rows(
                    reservation,
                    packet,
                )
                self._verify_exit_owner_context(request, owner_receipt, cycle)
                return owner_receipt, cycle
        finally:
            db.close()

    def read_exact_exit_order_fills(
        self,
        request: CapturedPaperExitFillPostCommitRequest,
        *,
        owner_receipt: AdaptiveExitOwnerReceipt,
        cycle: AlpacaPaperFillCycleBinding,
    ) -> PreparedAlpacaPaperFillBatch:
        """Perform one exact read after all local authority checks, with no DB."""

        self._verify_exit_owner_context(request, owner_receipt, cycle)
        return read_verified_alpaca_paper_exit_fill_batch(
            self._adapter,
            cycle=cycle,
            exit_owner_receipt=owner_receipt,
        )

    def _verified_exit_observation_resolution(
        self,
        db: Any,
        *,
        request: CapturedPaperExitFillPostCommitRequest,
        owner_receipt: AdaptiveExitOwnerReceipt,
        cycle: AlpacaPaperFillCycleBinding,
        fill_rows: tuple[AlpacaPaperFillActivity, ...],
    ) -> tuple[int, bool]:
        """Verify durable query evidence bound to this exact owner/order."""

        observations = list(
            db.scalars(
                select(AlpacaPaperFillQueryObservation)
                .where(
                    AlpacaPaperFillQueryObservation.reservation_id
                    == request.reservation_id,
                    AlpacaPaperFillQueryObservation.provider_order_id
                    == request.provider_order_id,
                    AlpacaPaperFillQueryObservation.expected_client_order_id
                    == request.exit_client_order_id,
                    AlpacaPaperFillQueryObservation.order_role == "exit",
                )
                .order_by(
                    AlpacaPaperFillQueryObservation.available_at,
                    AlpacaPaperFillQueryObservation.observation_sha256,
                )
                .with_for_update()
            )
        )
        rows_by_sha = {row.event_sha256: row for row in fill_rows}
        maximum_cumulative = -1
        terminal_zero = False
        expected_cycle = cycle.to_payload()
        for observation in observations:
            content = _verified_json_object(
                observation.observation_content_canonical_json,
                observation.observation_content_sha256,
                "retained_exit_observation_content",
            )
            read_binding = _verified_json_object(
                observation.read_binding_canonical_json,
                observation.read_binding_sha256,
                "retained_exit_read_binding",
            )
            provider_order = _verified_json_object(
                observation.provider_order_payload_canonical_json,
                observation.provider_order_payload_sha256,
                "retained_exit_provider_order",
            )
            _verified_json_object(
                observation.query_receipt_canonical_json,
                observation.query_receipt_sha256,
                "retained_exit_query_receipt",
            )
            bound_owner_sha = read_binding.get("exit_owner_receipt_sha256")
            if not isinstance(bound_owner_sha, str):
                continue
            try:
                bound_owner = self._reservation_store.load_exit_owner_receipt(
                    bound_owner_sha,
                    reservation_id=request.reservation_id,
                    for_projection=True,
                    session=db,
                )
            except AdaptiveReservationError as exc:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_owner_unavailable"
                ) from exc
            expected_binding = {
                "schema_version": "chili.alpaca-paper-fill-read-binding.v1",
                "cycle": expected_cycle,
                "provider_order_id": request.provider_order_id,
                "expected_client_order_id": request.exit_client_order_id,
                "order_role": "exit",
                "exit_owner_receipt_sha256": bound_owner_sha,
            }
            if not (
                observation.observation_sha256
                == observation.observation_content_sha256
                and observation.pagination_complete is True
                and observation.account_scope == request.account_scope
                and observation.account_identity_sha256
                == request.account_identity_sha256
                and observation.decision_packet_sha256
                == request.decision_packet_sha256
                and observation.execution_family == request.execution_family
                and observation.symbol == request.symbol
                and observation.cycle_broker_connection_generation
                == cycle.broker_connection_generation
                and read_binding == expected_binding
                and content.get("cycle") == expected_cycle
                and content.get("provider_order_id")
                == request.provider_order_id
                and content.get("expected_client_order_id")
                == request.exit_client_order_id
                and content.get("order_role") == "exit"
                and content.get("exit_owner_receipt_sha256")
                == bound_owner_sha
                and content.get("adapter_connection_generation")
                == observation.adapter_connection_generation
                and content.get("provider_order_payload_sha256")
                == observation.provider_order_payload_sha256
                and content.get("query_receipt_sha256")
                == observation.query_receipt_sha256
                and content.get("read_binding_sha256")
                == observation.read_binding_sha256
                and bound_owner.binding == owner_receipt.binding
                and bound_owner.provider_order_id
                == request.provider_order_id
                and bound_owner.provider_client_order_id
                == request.exit_client_order_id
                and provider_order.get("id") == request.provider_order_id
                and provider_order.get("client_order_id")
                == request.exit_client_order_id
                and str(provider_order.get("symbol") or "").upper()
                == request.symbol
                and str(provider_order.get("side") or "").lower() == "sell"
            ):
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_observation_mismatch"
                )
            if provider_order.get("account_id") not in {
                None,
                request.expected_account_id,
            }:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_account_mismatch"
                )
            try:
                filled_quantity = Decimal(str(provider_order.get("filled_qty")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_quantity_invalid"
                ) from exc
            if (
                not filled_quantity.is_finite()
                or filled_quantity < 0
                or filled_quantity != filled_quantity.to_integral_value()
            ):
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_quantity_invalid"
                )
            content_activities = content.get("activities")
            mappings = list(
                db.scalars(
                    select(AlpacaPaperFillObservationActivity)
                    .where(
                        AlpacaPaperFillObservationActivity.observation_sha256
                        == observation.observation_sha256
                    )
                    .order_by(
                        AlpacaPaperFillObservationActivity.activity_ordinal
                    )
                    .with_for_update()
                )
            )
            if not (
                isinstance(content_activities, list)
                and len(mappings) == int(observation.exact_activity_count)
                == len(content_activities)
            ):
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_observation_incomplete"
                )
            observed_cumulative = 0
            for ordinal, (mapping, activity_content) in enumerate(
                zip(mappings, content_activities, strict=True)
            ):
                fill_row = rows_by_sha.get(mapping.fill_event_sha256)
                if fill_row is None or not isinstance(activity_content, Mapping):
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_retained_exit_mapping_missing"
                    )
                mapping_body = {
                    "observation_sha256": observation.observation_sha256,
                    "activity_ordinal": ordinal,
                    "fill_event_sha256": fill_row.event_sha256,
                    "immutable_fill_identity_sha256": (
                        fill_row.immutable_fill_identity_sha256
                    ),
                    "provider_activity_id": fill_row.provider_activity_id,
                    "provider_payload_sha256": fill_row.provider_payload_sha256,
                }
                if not (
                    int(mapping.activity_ordinal) == ordinal
                    and mapping.mapping_sha256
                    == _canonical_sha256(mapping_body)
                    and mapping.immutable_fill_identity_sha256
                    == fill_row.immutable_fill_identity_sha256
                    and mapping.provider_activity_id
                    == fill_row.provider_activity_id
                    and mapping.provider_payload_sha256
                    == fill_row.provider_payload_sha256
                    and activity_content.get("provider_activity_id")
                    == fill_row.provider_activity_id
                    and activity_content.get("immutable_fill_identity_sha256")
                    == fill_row.immutable_fill_identity_sha256
                    and activity_content.get(
                        "observation_record_content_sha256"
                    )
                    == fill_row.record_content_sha256
                ):
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_retained_exit_mapping_mismatch"
                    )
                observed_cumulative = max(
                    observed_cumulative,
                    int(fill_row.cumulative_quantity),
                )
            if observed_cumulative != int(filled_quantity):
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_retained_exit_cumulative_mismatch"
                )
            if mappings:
                maximum_cumulative = max(
                    maximum_cumulative,
                    observed_cumulative,
                )
            terminal_zero = terminal_zero or bool(
                bound_owner_sha == request.exit_owner_receipt_sha256
                and
                observed_cumulative == 0
                and str(provider_order.get("status") or "").strip().lower()
                in _TERMINAL_ZERO_EXIT_STATUSES
            )
        return maximum_cumulative, terminal_zero

    def project_committed_exit_fill_chain_if_present(
        self,
        request: CapturedPaperExitFillPostCommitRequest,
        *,
        owner_receipt: AdaptiveExitOwnerReceipt,
    ) -> bool | None:
        """Re-verify and project sufficient retained evidence without broker I/O."""

        request.verify()
        db = self._factory()
        try:
            with db.begin():
                retained_owner = self._reservation_store.load_exit_owner_receipt(
                    request.exit_owner_receipt_sha256,
                    reservation_id=request.reservation_id,
                    for_projection=True,
                    session=db,
                )
                if retained_owner != owner_receipt:
                    raise CapturedPaperTransportUnavailable(
                        "exit_post_commit_durable_owner_mismatch"
                    )
                reservation = db.get(
                    AdaptiveRiskReservation,
                    request.reservation_id,
                )
                packet = (
                    db.get(
                        AdaptiveRiskDecisionPacket,
                        reservation.decision_packet_sha256,
                    )
                    if reservation is not None
                    else None
                )
                if reservation is None or packet is None:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_retained_exit_cycle_missing"
                    )
                cycle = AlpacaPaperFillCycleBinding.from_rows(
                    reservation,
                    packet,
                )
                fill_rows = list(
                    db.scalars(
                        select(AlpacaPaperFillActivity)
                        .where(
                            AlpacaPaperFillActivity.reservation_id
                            == request.reservation_id
                        )
                        .order_by(AlpacaPaperFillActivity.sequence)
                        .with_for_update()
                    )
                )
                verify_alpaca_paper_fill_activity_chain(fill_rows)
                exact_fill_rows = [
                    row
                    for row in fill_rows
                    if row.provider_order_id == request.provider_order_id
                    and row.provider_client_order_id
                    == request.exit_client_order_id
                ]
                for row in exact_fill_rows:
                    if not (
                        row.side == "sell"
                        and row.order_role == "exit"
                        and row.account_scope == request.account_scope
                        and row.account_identity_sha256
                        == request.account_identity_sha256
                        and row.decision_packet_sha256
                        == request.decision_packet_sha256
                        and row.execution_family == request.execution_family
                        and row.symbol == request.symbol
                        and row.cycle_client_order_id
                        == request.entry_client_order_id
                        and row.broker_connection_generation
                        == cycle.broker_connection_generation
                    ):
                        raise CapturedPaperTransportUnavailable(
                            "fill_capture_retained_exit_identity_mismatch"
                        )
                observed_cumulative, observed_terminal_zero = (
                    self._verified_exit_observation_resolution(
                        db,
                        request=request,
                        owner_receipt=owner_receipt,
                        cycle=cycle,
                        fill_rows=tuple(fill_rows),
                    )
                )
                if (
                    observed_cumulative
                    >= owner_receipt.provider_cumulative_quantity
                    or observed_terminal_zero
                    or (
                        owner_receipt.provider_cumulative_quantity == 0
                        and owner_receipt.provider_status
                        in _TERMINAL_ZERO_EXIT_STATUSES
                    )
                ):
                    return True

                all_rows = list(
                    db.scalars(
                        select(AlpacaPaperPostSettlementFillContradiction)
                        .where(
                            AlpacaPaperPostSettlementFillContradiction.reservation_id
                            == request.reservation_id
                        )
                        .order_by(
                            AlpacaPaperPostSettlementFillContradiction
                            .contradiction_sequence
                        )
                        .with_for_update()
                    )
                )
                previous_sha: str | None = None
                for expected_sequence, row in enumerate(all_rows, start=1):
                    verify_alpaca_paper_post_settlement_fill_contradiction_row(
                        row
                    )
                    if not (
                        int(row.contradiction_sequence) == expected_sequence
                        and row.previous_contradiction_sha256 == previous_sha
                    ):
                        raise CapturedPaperTransportUnavailable(
                            "fill_capture_retained_exit_chain_incomplete"
                        )
                    previous_sha = row.contradiction_sha256
                rows = [
                    row
                    for row in all_rows
                    if row.provider_order_id == request.provider_order_id
                    and row.provider_client_order_id
                    == request.exit_client_order_id
                ]
                if not rows:
                    return None
                for row in rows:
                    sibling_owner = (
                        self._reservation_store.load_exit_owner_receipt(
                            row.exit_owner_receipt_sha256,
                            reservation_id=request.reservation_id,
                            for_projection=True,
                            session=db,
                        )
                    )
                    if not (
                        sibling_owner.binding == owner_receipt.binding
                        and sibling_owner.provider_order_id
                        == request.provider_order_id
                        and sibling_owner.provider_client_order_id
                        == request.exit_client_order_id
                        and sibling_owner.provider_cumulative_quantity
                        <= int(row.broker_observed_cumulative_quantity)
                        and
                        row.side == "sell"
                        and row.order_role == "exit"
                        and row.account_scope == request.account_scope
                        and row.account_identity_sha256
                        == request.account_identity_sha256
                        and row.decision_packet_sha256
                        == request.decision_packet_sha256
                        and row.execution_family == request.execution_family
                        and row.symbol == request.symbol
                        and row.expected_client_order_id
                        == request.entry_client_order_id
                    ):
                        raise CapturedPaperTransportUnavailable(
                            "fill_capture_retained_exit_identity_mismatch"
                        )
                batches: dict[str, list[Any]] = {}
                for row in rows:
                    batches.setdefault(row.batch_content_sha256, []).append(row)
                for batch_rows in batches.values():
                    expected_count = int(batch_rows[0].batch_activity_count)
                    if not (
                        len(batch_rows) == expected_count
                        and [
                            int(row.batch_activity_ordinal)
                            for row in batch_rows
                        ]
                        == list(range(expected_count))
                        and all(
                            int(row.batch_activity_count) == expected_count
                            for row in batch_rows
                        )
                        and batch_rows[-1].is_projection_terminal is True
                        and all(
                            row.is_projection_terminal is False
                            for row in batch_rows[:-1]
                        )
                    ):
                        raise CapturedPaperTransportUnavailable(
                            "fill_capture_retained_exit_batch_incomplete"
                        )
                terminal = rows[-1]
                if not terminal.is_projection_terminal:
                    raise CapturedPaperTransportUnavailable(
                        "fill_capture_retained_exit_batch_incomplete"
                    )
                observed_cumulative = int(
                    terminal.broker_observed_cumulative_quantity
                )
                if observed_cumulative < owner_receipt.provider_cumulative_quantity:
                    return None
                self._reservation_store.apply_post_settlement_fill_contradiction(
                    request.reservation_id,
                    contradiction_sha256=terminal.contradiction_sha256,
                    session=db,
                )
                return True
        finally:
            db.close()

    def append_exit_fill_read(
        self,
        request: CapturedPaperExitFillPostCommitRequest,
        read: PreparedAlpacaPaperFillBatch,
        *,
        owner_receipt: AdaptiveExitOwnerReceipt,
        cycle: AlpacaPaperFillCycleBinding,
    ) -> Any:
        """Publish a verified exit batch in a fresh DB-only transaction."""

        self._verify_exit_owner_context(request, owner_receipt, cycle)
        if not (
            type(read) is PreparedAlpacaPaperFillBatch
            and read.order_role == "exit"
            and read.cycle == cycle
            and read.provider_order_id == request.provider_order_id
            and read.expected_client_order_id == request.exit_client_order_id
            and read.exit_owner_receipt_sha256
            == request.exit_owner_receipt_sha256
            and read.adapter_connection_generation
            == request.broker_connection_generation
        ):
            raise CapturedPaperTransportUnavailable(
                "fill_capture_exit_read_binding_mismatch"
            )
        db = self._factory()
        try:
            with db.begin():
                if not read.activities:
                    return append_prepared_alpaca_paper_fill_batch(
                        db,
                        read,
                    )
                return publish_prepared_alpaca_paper_post_settlement_fill_batch(
                    db,
                    read,
                )
        finally:
            db.close()

    def complete_exit_post_commit(
        self,
        request: CapturedPaperExitFillPostCommitRequest,
    ) -> dict[str, Any]:
        """Complete one committed request through the read-only broker seam."""

        if type(request) is not CapturedPaperExitFillPostCommitRequest:
            raise CapturedPaperTransportContractError(
                "exit_post_commit_request_type_invalid"
            )
        request.verify()
        inflight_key = (request.reservation_id, request.provider_order_id)
        with self._lock:
            if inflight_key in self._exit_inflight:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_exit_order_already_inflight"
                )
            self._exit_inflight.add(inflight_key)
        try:
            owner_receipt, cycle = self.load_exit_owner_context(request)
            retained = self.project_committed_exit_fill_chain_if_present(
                request,
                owner_receipt=owner_receipt,
            )
            if retained is True:
                return {
                    "ok": True,
                    "broker_read_count": 0,
                    "observed_count": 0,
                    "committed_evidence_reused": True,
                    "request_sha256": request.request_sha256,
                }
            read = self.read_exact_exit_order_fills(
                request,
                owner_receipt=owner_receipt,
                cycle=cycle,
            )
            publication = self.append_exit_fill_read(
                request,
                read,
                owner_receipt=owner_receipt,
                cycle=cycle,
            )
            confirmed = self.project_committed_exit_fill_chain_if_present(
                request,
                owner_receipt=owner_receipt,
            )
            if confirmed is not True:
                raise CapturedPaperTransportUnavailable(
                    "fill_capture_exit_projection_unconfirmed"
                )
            return {
                "ok": True,
                "broker_read_count": 1,
                "observed_count": len(read.activities),
                "created_contradiction_count": len(
                    getattr(publication, "contradiction_sha256s", ())
                ),
                "committed_evidence_reused": False,
                "request_sha256": request.request_sha256,
            }
        finally:
            with self._lock:
                self._exit_inflight.discard(inflight_key)

    def _pending_exit_owner_requests(
        self,
        *,
        expected_account_id: str,
        runtime_generation: str,
        broker_connection_generation: str,
        execution_family: str,
        limit: int,
    ) -> tuple[
        tuple[CapturedPaperExitFillPostCommitRequest, ...],
        tuple[str, ...],
        bool,
    ]:
        """Inventory immutable owner heads; never infer authority from claims."""

        account_id = canonical_alpaca_paper_account_id(expected_account_id)
        if (
            execution_family != "alpaca_spot"
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > self._max_pending_reads
        ):
            raise CapturedPaperTransportContractError(
                "exit_owner_inventory_scope_invalid"
            )
        identity_sha256 = alpaca_paper_account_identity_sha256(account_id)
        pending: list[CapturedPaperExitFillPostCommitRequest] = []
        unavailable: list[str] = []
        overflow = False
        db = self._factory()
        try:
            with db.begin():
                watermark = db.scalar(
                    text(
                        """
                        SELECT MAX(e.id)
                        FROM adaptive_risk_reservation_events AS e
                        JOIN adaptive_risk_reservations AS r
                          ON r.reservation_id = e.reservation_id
                        JOIN adaptive_risk_decision_packets AS p
                          ON p.decision_packet_sha256 =
                                r.decision_packet_sha256
                        WHERE r.account_scope = 'alpaca:paper'
                          AND p.account_scope = 'alpaca:paper'
                          AND p.account_identity_sha256 = :identity_sha256
                          AND p.execution_family = 'alpaca_spot'
                          AND p.broker_environment = 'paper'
                          AND e.event_type IN (
                              'alpaca_exit_owner_submitted',
                              'alpaca_exit_owner_reconciled'
                          )
                        """
                    ),
                    {"identity_sha256": identity_sha256},
                )
        finally:
            db.close()
        if watermark is None:
            return (), (), False

        after_event_id = 0
        while True:
            page_candidates: list[
                tuple[
                    CapturedPaperExitFillPostCommitRequest,
                    AdaptiveExitOwnerReceipt,
                ]
            ] = []
            db = self._factory()
            try:
                with db.begin():
                    rows = db.execute(
                        text(
                            """
                            WITH ranked AS (
                                SELECT
                                    e.id AS event_id,
                                    e.event_sha256,
                                    e.reservation_id,
                                    e.sequence,
                                    e.payload_json -> 'details' AS details,
                                    ROW_NUMBER() OVER (
                                        PARTITION BY
                                            e.reservation_id,
                                            e.payload_json -> 'details'
                                                ->> 'provider_order_id',
                                            e.payload_json -> 'details'
                                                ->> 'provider_client_order_id'
                                        ORDER BY e.sequence DESC
                                    ) AS rank
                                FROM adaptive_risk_reservation_events AS e
                                JOIN adaptive_risk_reservations AS r
                                  ON r.reservation_id = e.reservation_id
                                JOIN adaptive_risk_decision_packets AS p
                                  ON p.decision_packet_sha256 =
                                        r.decision_packet_sha256
                                WHERE r.account_scope = 'alpaca:paper'
                                  AND p.account_scope = 'alpaca:paper'
                                  AND p.account_identity_sha256 =
                                        :identity_sha256
                                  AND p.execution_family = 'alpaca_spot'
                                  AND p.broker_environment = 'paper'
                                  AND e.event_type IN (
                                      'alpaca_exit_owner_submitted',
                                      'alpaca_exit_owner_reconciled'
                                  )
                                  AND e.id <= :watermark
                            ),
                            heads AS (
                                SELECT * FROM ranked WHERE rank = 1
                            )
                            SELECT
                                h.event_id,
                                h.event_sha256,
                                h.reservation_id,
                                h.sequence,
                                h.details
                            FROM heads AS h
                            WHERE h.event_id > :after_event_id
                            ORDER BY h.event_id
                            LIMIT :page_size
                            """
                        ),
                        {
                            "identity_sha256": identity_sha256,
                            "watermark": int(watermark),
                            "after_event_id": after_event_id,
                            "page_size": limit,
                        },
                    ).all()
                    for row in rows:
                        after_event_id = int(row.event_id)
                        details = row.details
                        details = (
                            details if isinstance(details, Mapping) else {}
                        )
                        try:
                            receipt = (
                                self._reservation_store
                                .load_latest_exit_owner_receipt_for_order(
                                    reservation_id=row.reservation_id,
                                    provider_order_id=details.get(
                                        "provider_order_id"
                                    ),
                                    provider_client_order_id=details.get(
                                        "provider_client_order_id"
                                    ),
                                    expected_account_id=account_id,
                                    expected_observer_claim_token=details.get(
                                        "observer_claim_token"
                                    ),
                                    expected_observer_session_id=details.get(
                                        "observer_session_id"
                                    ),
                                    expected_observer_generation=details.get(
                                        "observer_generation"
                                    ),
                                    expected_observer_runtime_generation=(
                                        details.get(
                                            "observer_runtime_generation"
                                        )
                                    ),
                                    expected_observer_connection_generation=(
                                        details.get(
                                            "observer_connection_generation"
                                        )
                                    ),
                                    expected_cumulative_quantity=details.get(
                                        "provider_cumulative_quantity"
                                    ),
                                    session=db,
                                )
                            )
                            binding = receipt.binding
                            if not (
                                binding.expected_account_id == account_id
                                and binding.execution_family
                                == execution_family
                            ):
                                raise CapturedPaperTransportUnavailable(
                                    "exit_owner_inventory_identity_mismatch"
                                )
                            request = (
                                CapturedPaperExitFillPostCommitRequest.build(
                                    session_id=receipt.observer_session_id,
                                    reservation_id=receipt.reservation_id,
                                    decision_packet_sha256=(
                                        binding.decision_packet_sha256
                                    ),
                                    expected_account_id=account_id,
                                    account_identity_sha256=(
                                        binding.account_identity_sha256
                                    ),
                                    runtime_generation=runtime_generation,
                                    broker_connection_generation=(
                                        broker_connection_generation
                                    ),
                                    symbol=binding.symbol,
                                    entry_client_order_id=(
                                        binding.entry_client_order_id
                                    ),
                                    exit_client_order_id=(
                                        binding.exit_client_order_id
                                    ),
                                    provider_order_id=(
                                        receipt.provider_order_id
                                    ),
                                    exit_owner_receipt_sha256=(
                                        receipt.receipt_sha256
                                    ),
                                )
                            )
                            page_candidates.append((request, receipt))
                        except Exception as exc:
                            unavailable.append(type(exc).__name__)
            finally:
                db.close()

            for request, receipt in page_candidates:
                try:
                    projected = (
                        self.project_committed_exit_fill_chain_if_present(
                            request,
                            owner_receipt=receipt,
                        )
                    )
                    if projected is not True:
                        pending.append(request)
                        if len(pending) > limit:
                            overflow = True
                            break
                except Exception as exc:
                    unavailable.append(type(exc).__name__)
            if overflow or len(rows) < limit:
                break

        db = self._factory()
        try:
            with db.begin():
                current_watermark = db.scalar(
                    text(
                        """
                        SELECT MAX(e.id)
                        FROM adaptive_risk_reservation_events AS e
                        JOIN adaptive_risk_reservations AS r
                          ON r.reservation_id = e.reservation_id
                        JOIN adaptive_risk_decision_packets AS p
                          ON p.decision_packet_sha256 =
                                r.decision_packet_sha256
                        WHERE r.account_scope = 'alpaca:paper'
                          AND p.account_scope = 'alpaca:paper'
                          AND p.account_identity_sha256 = :identity_sha256
                          AND p.execution_family = 'alpaca_spot'
                          AND p.broker_environment = 'paper'
                          AND e.event_type IN (
                              'alpaca_exit_owner_submitted',
                              'alpaca_exit_owner_reconciled'
                          )
                        """
                    ),
                    {"identity_sha256": identity_sha256},
                )
                overflow = overflow or bool(
                    current_watermark is not None
                    and int(current_watermark) > int(watermark)
                )
        finally:
            db.close()
        return tuple(pending), tuple(unavailable), overflow

    def recover_exit_owner_inventory_bounded(
        self,
        *,
        expected_account_id: str,
        runtime_generation: str,
        broker_connection_generation: str,
        execution_family: str,
        limit: int,
    ) -> Mapping[str, Any]:
        """Bounded GET-only recovery for durable exit-owner receipts."""

        pending, initial_unavailable, initial_overflow = (
            self._pending_exit_owner_requests(
                expected_account_id=expected_account_id,
                runtime_generation=runtime_generation,
                broker_connection_generation=broker_connection_generation,
                execution_family=execution_family,
                limit=limit,
            )
        )
        completed: list[str] = []
        failures: list[str] = list(initial_unavailable)
        broker_reads = 0
        for request in pending[:limit]:
            try:
                result = self.complete_exit_post_commit(request)
                if not isinstance(result, Mapping) or result.get("ok") is not True:
                    raise CapturedPaperTransportUnavailable(
                        "exit_owner_recovery_result_invalid"
                    )
                broker_reads += int(result.get("broker_read_count") or 0)
                completed.append(request.request_sha256)
            except Exception as exc:
                failures.append(type(exc).__name__)
        remaining, final_unavailable, final_overflow = (
            self._pending_exit_owner_requests(
                expected_account_id=expected_account_id,
                runtime_generation=runtime_generation,
                broker_connection_generation=broker_connection_generation,
                execution_family=execution_family,
                limit=limit,
            )
        )
        failures.extend(final_unavailable)
        exhausted = bool(
            initial_overflow
            or final_overflow
            or (len(pending) >= limit and bool(remaining))
        )
        body = {
            "schema_version": "chili.captured-paper-exit-owner-recovery.v1",
            "account_scope": "alpaca:paper",
            "expected_account_id": expected_account_id,
            "runtime_generation": runtime_generation,
            "broker_connection_generation": broker_connection_generation,
            "execution_family": execution_family,
            "bounded_limit": limit,
            "attempted_request_sha256s": [
                request.request_sha256 for request in pending[:limit]
            ],
            "completed_request_sha256s": completed,
            "remaining_request_sha256s": [
                request.request_sha256 for request in remaining
            ],
            "unavailable_error_types": sorted(failures),
            "broker_read_count": broker_reads,
            "exit_owner_inventory_resolved": bool(
                not remaining and not failures and not final_overflow
            ),
            "exit_owner_recovery_bounded": True,
            "exit_owner_recovery_exhausted": exhausted,
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        return {**body, "receipt_sha256": _canonical_sha256(body)}

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


class CapturedPaperExitOwnerWorker:
    """Supervise bounded GET-only completion of immutable exit-owner facts."""

    HEALTH_SCHEMA_VERSION = "chili.captured-paper-exit-owner-worker-health.v1"

    def __init__(
        self,
        *,
        fill_capture: SqlAlchemyCapturedPaperFillCapture,
        expected_account_id: str,
        runtime_generation: str,
        broker_connection_generation: str,
        execution_family: str,
        max_items_per_cycle: int,
        idle_poll_seconds: float,
        observation_clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        if type(fill_capture) is not SqlAlchemyCapturedPaperFillCapture:
            raise CapturedPaperTransportContractError(
                "exit_owner_worker_fill_capture_invalid"
            )
        try:
            account_id = canonical_alpaca_paper_account_id(
                expected_account_id
            )
            generation = str(uuid.UUID(str(runtime_generation)))
        except (AlpacaPaperAccountIdentityError, ValueError, AttributeError):
            raise CapturedPaperTransportContractError(
                "exit_owner_worker_runtime_identity_invalid"
            ) from None
        if not (
            account_id == expected_account_id
            and generation == runtime_generation
            and execution_family == "alpaca_spot"
            and isinstance(broker_connection_generation, str)
            and broker_connection_generation.strip()
            == broker_connection_generation
            and broker_connection_generation
            and not isinstance(max_items_per_cycle, bool)
            and isinstance(max_items_per_cycle, int)
            and 0 < max_items_per_cycle <= fill_capture._max_pending_reads
            and not isinstance(idle_poll_seconds, bool)
            and isinstance(idle_poll_seconds, (int, float))
            and math.isfinite(float(idle_poll_seconds))
            and 0.01 <= float(idle_poll_seconds) <= 60.0
            and callable(observation_clock)
        ):
            raise CapturedPaperTransportContractError(
                "exit_owner_worker_configuration_invalid"
            )
        self._fill_capture = fill_capture
        self._expected_account_id = account_id
        self._runtime_generation = generation
        self._broker_connection_generation = broker_connection_generation
        self._execution_family = execution_family
        self._limit = max_items_per_cycle
        self._idle_poll_seconds = float(idle_poll_seconds)
        self._clock = observation_clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ever_started = False
        self._running = False
        self._fatal_error_type: str | None = None
        self._cycles_completed = 0
        self._pending = 0
        self._broker_reads = 0
        self._last_cycle_completed_at: datetime | None = None

    def run_one_cycle(self) -> Mapping[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            raise CapturedPaperTransportContractError(
                "exit_owner_worker_cycle_already_running"
            )
        try:
            receipt = dict(
                self._fill_capture.recover_exit_owner_inventory_bounded(
                    expected_account_id=self._expected_account_id,
                    runtime_generation=self._runtime_generation,
                    broker_connection_generation=(
                        self._broker_connection_generation
                    ),
                    execution_family=self._execution_family,
                    limit=self._limit,
                )
            )
            supplied_sha = receipt.pop("receipt_sha256", None)
            if not (
                supplied_sha == _canonical_sha256(receipt)
                and receipt.get("exit_owner_recovery_bounded") is True
                and receipt.get("paper_order_submission_authorized") is False
                and receipt.get("live_cash_authorized") is False
                and receipt.get("real_money_authorized") is False
            ):
                raise CapturedPaperTransportContractError(
                    "exit_owner_worker_recovery_receipt_invalid"
                )
            observed_at = self._clock()
            if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
                raise CapturedPaperTransportContractError(
                    "exit_owner_worker_clock_invalid"
                )
            with self._state_lock:
                self._cycles_completed += 1
                self._pending = len(
                    receipt.get("remaining_request_sha256s") or ()
                ) + len(receipt.get("unavailable_error_types") or ())
                self._broker_reads += int(receipt.get("broker_read_count") or 0)
                self._last_cycle_completed_at = observed_at.astimezone(UTC)
            return {**receipt, "receipt_sha256": supplied_sha}
        finally:
            self._cycle_lock.release()

    def _run(self) -> None:
        with self._state_lock:
            self._running = True
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    receipt = self.run_one_cycle()
                except Exception as exc:
                    with self._state_lock:
                        self._fatal_error_type = type(exc).__name__
                    self._stop.set()
                    break
                if (
                    receipt.get("exit_owner_inventory_resolved") is True
                    or not receipt.get("completed_request_sha256s")
                ):
                    self._wake.wait(self._idle_poll_seconds)
                    self._wake.clear()
        finally:
            with self._state_lock:
                self._running = False

    def start(self) -> None:
        with self._state_lock:
            if self._ever_started:
                raise CapturedPaperTransportContractError(
                    "exit_owner_worker_start_is_one_shot"
                )
            self._ever_started = True
            self._thread = threading.Thread(
                target=self._run,
                name="chili-captured-paper-exit-owner",
                daemon=False,
            )
        self._thread.start()
        if not self._ready.wait(5.0):
            self._stop.set()
            raise CapturedPaperTransportContractError(
                "exit_owner_worker_start_unconfirmed"
            )

    def wake(self) -> None:
        self._wake.set()

    def close(self, *, join_timeout_seconds: float) -> None:
        if (
            isinstance(join_timeout_seconds, bool)
            or not isinstance(join_timeout_seconds, (int, float))
            or not math.isfinite(float(join_timeout_seconds))
            or not 0.01 <= float(join_timeout_seconds) <= 300.0
        ):
            raise CapturedPaperTransportContractError(
                "exit_owner_worker_join_timeout_invalid"
            )
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(float(join_timeout_seconds))
            if self._thread.is_alive():
                raise CapturedPaperTransportContractError(
                    "exit_owner_worker_did_not_join"
                )

    def health(self) -> Mapping[str, Any]:
        with self._state_lock:
            thread = self._thread
            return {
                "schema_version": self.HEALTH_SCHEMA_VERSION,
                "ever_started": self._ever_started,
                "running": bool(
                    self._running and thread is not None and thread.is_alive()
                ),
                "stop_requested": self._stop.is_set(),
                "fatal": self._fatal_error_type is not None,
                "fatal_error_type": self._fatal_error_type,
                "cycles_completed": self._cycles_completed,
                "pending": self._pending,
                "broker_reads": self._broker_reads,
                "last_cycle_completed_at": (
                    self._last_cycle_completed_at.isoformat()
                    if self._last_cycle_completed_at is not None
                    else None
                ),
            }


__all__ = (
    "CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY",
    "CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY",
    "CapturedPaperExitFillPostCommitRequest",
    "CapturedPaperExitTransportPostCommitRequest",
    "CapturedPaperExitOwnerWorker",
    "SqlAlchemyCapturedPaperFillCapture",
)
