"""Crash-safe transport coordinator for captured Alpaca PAPER entries.

The coordinator is deliberately split into short, committed database phases:

``lease -> consume transport authority -> HTTP -> persist outcome``.

No SQLAlchemy ``Session`` is accepted by the public coordinator and none can
escape :class:`SqlAlchemyCapturedPaperTransportStore`.  Consequently a broker
POST or same-CID lookup can never execute while a database transaction or row
lock owned by this module is alive.

After the transport-start fence is durable, every non-positive outcome is
``transport_indeterminate``.  Restart work is reconciliation-only: explicit CID
absence is recorded as another unresolved observation and never becomes
permission to release ownership or POST again.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .adaptive_risk_account_lock import AdaptiveRiskAccountLockIdentity
from .captured_paper_admission import CommittedCapturedPaperAdmission
from .captured_paper_entry_intent import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    CapturedPaperPostCommitRequest,
)
from .captured_paper_financial_breaker import (
    CapturedPaperFinancialBreakerError,
    CapturedPaperFinancialBreakerReceipt,
)
from .captured_paper_outbox import (
    CapturedPaperTransportInvocationAuthority,
    CapturedPaperTransportDispatchAuthority,
    CapturedPaperTransportPreDispatchEvidence,
    CapturedPaperBrokerAcceptanceProof,
    CapturedPaperDurableTransportBundle,
    CapturedPaperOutboxLease,
    CapturedPaperTransportAuthority,
    DURABLE_TRANSPORT_INSTRUCTION_SCHEMA_VERSION,
    OUTBOX_STATUS_COMPLETED,
    OUTBOX_STATUS_TRANSPORT_INDETERMINATE,
    OUTBOX_STATUS_TRANSPORT_STARTED,
    authorize_captured_paper_transport_invocation,
    _consume_transport_dispatch_process_attestation,
    consume_captured_paper_transport_dispatch_authority,
    find_next_due_captured_paper_completion,
    find_next_due_captured_paper_reconciliation,
    lease_captured_paper_completion,
    lease_captured_paper_indeterminate_reconciliation,
    load_captured_paper_durable_transport_bundle,
    mark_captured_paper_completion_accepted,
    mark_captured_paper_reconciliation_accepted,
    mark_captured_paper_reconciliation_pending,
    mark_captured_paper_transport_indeterminate,
    mark_captured_paper_transport_started,
    record_captured_paper_transport_financial_breaker,
    revalidate_captured_paper_transport_dispatch_authority,
    recover_expired_captured_paper_leases,
)
from ..venue import alpaca_spot as _alpaca_spot


quantize_alpaca_equity_limit_price = (
    _alpaca_spot.quantize_alpaca_equity_limit_price
)

# Freeze every adapter method used by the entry transport at module import.
# The activation manifest pins both this module and ``alpaca_spot``; retaining
# these concrete function objects also prevents a later instance/class
# monkeypatch from becoming the function that performs the order I/O.
_EXACT_ALPACA_SPOT_ADAPTER_CLASS = _alpaca_spot.AlpacaSpotAdapter
_EXACT_ALPACA_BIND_ACCOUNT_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS.bind_account_id
)
_EXACT_ALPACA_IS_ENABLED_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS.is_enabled
)
_EXACT_ALPACA_BROKER_ENVIRONMENT_PROPERTY = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS.broker_environment
)
_EXACT_ALPACA_CONNECTION_RECEIPT_METHOD = (
    _alpaca_spot._EXACT_PAPER_CONNECTION_RECEIPT_METHOD
)
_EXACT_ALPACA_ENTRY_POST_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS.place_limit_order_gtc
)
_EXACT_ALPACA_ENTRY_SUBMIT_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS._submit
)
_EXACT_ALPACA_CID_LOOKUP_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS.get_order_by_client_order_id_truth
)
_EXACT_ALPACA_ACCOUNT_CLIENT_METHOD = (
    _alpaca_spot._EXACT_FILL_ACCOUNT_CLIENT_METHOD
)
_EXACT_ALPACA_NORMALIZE_ORDER_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS._normalize_order
)
_EXACT_ALPACA_POSITION_INTENT_METHOD = (
    _EXACT_ALPACA_SPOT_ADAPTER_CLASS._resolve_position_intent
)
_EXACT_ALPACA_ADAPTER_BUILD_SHA256 = _alpaca_spot._ALPACA_SPOT_BUILD_SHA256


UTC = timezone.utc
TRANSPORT_INSTRUCTION_SCHEMA_VERSION = (
    DURABLE_TRANSPORT_INSTRUCTION_SCHEMA_VERSION
)
TRANSPORT_OBSERVATION_SCHEMA_VERSION = (
    "chili.captured-paper-transport-observation.v1"
)
FILL_READ_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-fill-read-authority-seam.v1"
)
FILL_APPEND_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-fill-append-receipt-seam.v1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,35}$")
_POSITIVE_ZERO_FILL_BROKER_STATUSES = frozenset({"accepted", "new"})
_FILL_BEARING_BROKER_STATUSES = frozenset({"filled", "partially_filled"})
_TERMINAL_ZERO_FILL_BROKER_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "rejected"}
)
EXACT_PAPER_ACCOUNT_BINDING_SOURCE = (
    "exact_class_pinned_alpaca_paper_adapter.bound_account_id"
)
_ORDER_FIELDS = frozenset(
    {
        "asset_class",
        "client_order_id",
        "extended_hours",
        "limit_price",
        "position_intent",
        "qty",
        "side",
        "symbol",
        "time_in_force",
        "type",
    }
)


class CapturedPaperTransportError(RuntimeError):
    """Stable fail-closed transport error."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_transport_error")
        super().__init__(self.reason)


class CapturedPaperTransportContractError(CapturedPaperTransportError):
    pass


class CapturedPaperTransportUnavailable(CapturedPaperTransportError):
    pass


class _CapturedPaperTransportFinancialBreakerError(
    CapturedPaperTransportContractError
):
    """Fail-closed pre-I/O error carrying a content-addressed receipt."""

    def __init__(
        self,
        reason: str,
        *,
        receipt: CapturedPaperFinancialBreakerReceipt | None,
    ) -> None:
        super().__init__(reason)
        self.financial_breaker_receipt_sha256 = (
            receipt.receipt_sha256 if receipt is not None else None
        )
        self.financial_breaker_evidence_sha256 = (
            receipt.breaker_evidence_sha256 if receipt is not None else None
        )
        self.financial_breaker_blocker = (
            receipt.blocker if receipt is not None else None
        )


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


def _sha(value: Any, *, field_name: str) -> str:
    digest = str(value or "").strip()
    if _SHA256_RE.fullmatch(digest) is None:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    return digest


def _canonical_uuid(value: Any, *, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        canonical = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperTransportContractError(
            f"{field_name}_invalid"
        ) from exc
    if raw != canonical:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    return canonical


def _identifier(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if text != value or _IDENTIFIER_RE.fullmatch(text) is None:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    return text


def _aware_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    try:
        if value.utcoffset() is None:
            raise ValueError(field_name)
    except Exception as exc:
        raise CapturedPaperTransportContractError(
            f"{field_name}_invalid"
        ) from exc
    return value.astimezone(UTC)


def _positive_decimal_text(value: Any, *, field_name: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperTransportContractError(
            f"{field_name}_invalid"
        ) from exc
    if not number.is_finite() or number <= 0:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    canonical = format(number.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical


def _positive_share_count(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    text = str(value or "").strip()
    if not text.isdigit():
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    quantity = int(text)
    if quantity <= 0:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    return quantity


def _nonnegative_whole_share_count(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperTransportContractError(
            f"{field_name}_invalid"
        ) from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise CapturedPaperTransportContractError(f"{field_name}_invalid")
    return int(number)


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportInstruction:
    """Exact content-addressed long-entry instruction passed to Alpaca PAPER."""

    request: CapturedPaperPostCommitRequest
    authority: CapturedPaperTransportAuthority
    order_request: Mapping[str, Any]
    order_request_sha256: str
    instruction_sha256: str = ""

    def __post_init__(self) -> None:
        if type(self.request) is not CapturedPaperPostCommitRequest:
            raise CapturedPaperTransportContractError(
                "transport_instruction_request_invalid"
            )
        self.request.verify()
        if type(self.authority) is not CapturedPaperTransportAuthority:
            raise CapturedPaperTransportContractError(
                "transport_instruction_authority_invalid"
            )
        self.authority.verify_for_request(self.request)
        order = dict(self.order_request)
        if frozenset(order) != _ORDER_FIELDS:
            raise CapturedPaperTransportContractError(
                "transport_instruction_order_shape_invalid"
            )
        intent = self.request.intent
        route = intent.route_token
        quantity = _positive_share_count(
            order.get("qty"), field_name="transport_instruction_quantity"
        )
        limit_price = _positive_decimal_text(
            order.get("limit_price"),
            field_name="transport_instruction_limit_price",
        )
        extended_hours = order.get("extended_hours")
        time_in_force = order.get("time_in_force")
        if not (
            order.get("asset_class") == "us_equity"
            and order.get("client_order_id") == intent.client_order_id
            and type(extended_hours) is bool
            and order.get("position_intent") == "buy_to_open"
            and order.get("side") == "buy"
            and order.get("symbol") == route.symbol
            and time_in_force in {"day", "gtc"}
            and (extended_hours is False or time_in_force == "day")
            and order.get("type") == "limit"
            and quantity > 0
            and Decimal(limit_price)
            == Decimal(
                _positive_decimal_text(
                    intent.entry_limit_ceiling_price,
                    field_name="intent_entry_limit_ceiling_price",
                )
            )
        ):
            raise CapturedPaperTransportContractError(
                "transport_instruction_order_binding_mismatch"
            )
        try:
            canonical_limit = quantize_alpaca_equity_limit_price(
                order["limit_price"], "buy"
            )
        except (TypeError, ValueError) as exc:
            raise CapturedPaperTransportContractError(
                "transport_instruction_limit_tick_invalid"
            ) from exc
        if str(order["limit_price"]).strip() != canonical_limit:
            raise CapturedPaperTransportContractError(
                "transport_instruction_limit_not_canonical"
            )
        request_sha = _sha256_json(order)
        supplied_order_sha = _sha(
            self.order_request_sha256,
            field_name="transport_instruction_order_request_sha256",
        )
        if request_sha != supplied_order_sha:
            raise CapturedPaperTransportContractError(
                "transport_instruction_order_hash_mismatch"
            )
        if self.authority.broker_request_sha256 != supplied_order_sha:
            raise CapturedPaperTransportContractError(
                "transport_instruction_authority_order_hash_mismatch"
            )
        object.__setattr__(self, "order_request", order)
        object.__setattr__(self, "order_request_sha256", supplied_order_sha)
        expected = _sha256_json(self._content_payload())
        if self.instruction_sha256:
            supplied = _sha(
                self.instruction_sha256,
                field_name="transport_instruction_sha256",
            )
            if supplied != expected:
                raise CapturedPaperTransportContractError(
                    "transport_instruction_hash_mismatch"
                )
        object.__setattr__(self, "instruction_sha256", expected)

    @classmethod
    def from_admission(
        cls,
        admission: CommittedCapturedPaperAdmission,
    ) -> "CapturedPaperTransportInstruction":
        if type(admission) is not CommittedCapturedPaperAdmission:
            raise CapturedPaperTransportContractError(
                "committed_admission_type_invalid"
            )
        request = admission.post_commit_request
        intent = request.intent
        route = intent.route_token
        order = dict(admission.order_request)
        if str(order.get("qty") or "") != str(admission.quantity_shares):
            raise CapturedPaperTransportContractError(
                "committed_admission_quantity_mismatch"
            )
        authority = CapturedPaperTransportAuthority(
            completion_sha256=request.completion_sha256,
            account_scope=route.account_scope,
            expected_account_id=route.expected_account_id,
            account_identity_sha256=admission.account_identity_sha256,
            session_id=route.session_id,
            symbol=route.symbol,
            client_order_id=intent.client_order_id,
            binder_id=intent.binder_id,
            action_claim_token=intent.symbol_claim_token,
            reservation_id=admission.reservation_id,
            decision_packet_sha256=admission.decision_packet_sha256,
            reservation_request_sha256=admission.reservation_request_sha256,
            admission_evidence_sha256=(
                admission.adaptive_input_evidence_sha256
            ),
            broker_request_sha256=admission.order_request_sha256,
            opportunity_key_sha256=(
                intent.opportunity_key.opportunity_key_sha256
                if intent.opportunity_key is not None
                else None
            ),
        )
        return cls(
            request=request,
            authority=authority,
            order_request=order,
            order_request_sha256=admission.order_request_sha256,
        )

    @classmethod
    def from_durable_bundle(
        cls,
        bundle: CapturedPaperDurableTransportBundle,
    ) -> "CapturedPaperTransportInstruction":
        if type(bundle) is not CapturedPaperDurableTransportBundle:
            raise CapturedPaperTransportContractError(
                "durable_transport_bundle_type_invalid"
            )
        instruction = cls(
            request=bundle.request,
            authority=bundle.authority,
            order_request=bundle.order_request,
            order_request_sha256=bundle.order_request_sha256,
            instruction_sha256=bundle.transport_instruction_sha256,
        )
        if instruction._content_payload() != bundle.transport_instruction:
            raise CapturedPaperTransportContractError(
                "durable_transport_instruction_payload_mismatch"
            )
        return instruction

    @property
    def account_scope(self) -> str:
        return self.authority.account_scope

    @property
    def expected_account_id(self) -> str:
        return self.authority.expected_account_id

    @property
    def client_order_id(self) -> str:
        return self.authority.client_order_id

    @property
    def symbol(self) -> str:
        return self.authority.symbol

    @property
    def quantity_shares(self) -> int:
        return int(self.order_request["qty"])

    @property
    def limit_price(self) -> str:
        return str(self.order_request["limit_price"])

    @property
    def time_in_force(self) -> str:
        return str(self.order_request["time_in_force"])

    @property
    def extended_hours(self) -> bool:
        return bool(self.order_request["extended_hours"])

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSPORT_INSTRUCTION_SCHEMA_VERSION,
            "completion_sha256": self.request.completion_sha256,
            "transport_authority_sha256": self.authority.authority_sha256,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "client_order_id": self.client_order_id,
            "reservation_id": self.authority.reservation_id,
            "decision_packet_sha256": self.authority.decision_packet_sha256,
            "reservation_request_sha256": (
                self.authority.reservation_request_sha256
            ),
            "order_request_sha256": self.order_request_sha256,
        }

    def adapter_kwargs(self) -> dict[str, Any]:
        """Deterministic adapter transformation; no caller-supplied fields."""

        order = self.order_request
        return {
            "product_id": order["symbol"],
            "side": order["side"],
            "base_size": order["qty"],
            "limit_price": order["limit_price"],
            "client_order_id": order["client_order_id"],
            "extended_hours": order["extended_hours"],
            "position_intent": order["position_intent"],
            "time_in_force": order["time_in_force"],
            "asset_class": order["asset_class"],
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperCommittedLease:
    completion_sha256: str
    lease_token: str
    lease_owner_id: str
    lease_expires_at: datetime
    reconciliation_only: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "completion_sha256",
            _sha(self.completion_sha256, field_name="lease_completion_sha256"),
        )
        object.__setattr__(
            self,
            "lease_token",
            _canonical_uuid(self.lease_token, field_name="lease_token"),
        )
        object.__setattr__(
            self,
            "lease_owner_id",
            _canonical_uuid(self.lease_owner_id, field_name="lease_owner_id"),
        )
        object.__setattr__(
            self,
            "lease_expires_at",
            _aware_utc(self.lease_expires_at, field_name="lease_expires_at"),
        )
        if type(self.reconciliation_only) is not bool:
            raise CapturedPaperTransportContractError(
                "lease_reconciliation_only_invalid"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportStart:
    lease: CapturedPaperCommittedLease
    instruction_sha256: str
    transport_authority_sha256: str
    started_at: datetime

    def __post_init__(self) -> None:
        if type(self.lease) is not CapturedPaperCommittedLease:
            raise CapturedPaperTransportContractError(
                "transport_start_lease_invalid"
            )
        if self.lease.reconciliation_only:
            raise CapturedPaperTransportContractError(
                "reconciliation_lease_cannot_start_transport"
            )
        for name in ("instruction_sha256", "transport_authority_sha256"):
            object.__setattr__(
                self, name, _sha(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "started_at",
            _aware_utc(self.started_at, field_name="transport_started_at"),
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperExactBrokerOrderObservation:
    """Exact broker-returned entry economics; no instruction-derived fallback."""

    account_scope: str
    expected_account_id: str
    verified_adapter_account_id: str
    account_binding_source: str
    broker_account_id: str | None
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    asset_class: str
    quantity_shares: int
    broker_quantity_echo: str
    broker_filled_quantity_echo: str | None
    cumulative_filled_quantity_shares: int | None
    limit_price: str
    broker_limit_price_echo: str
    time_in_force: str
    extended_hours: bool
    position_intent_echo: str | None
    broker_order_status: str
    broker_order_status_echo: str
    broker_connection_generation: str
    broker_order_evidence_sha256: str
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperTransportContractError(
                "broker_order_account_scope_invalid"
            )
        for name in ("expected_account_id", "verified_adapter_account_id"):
            object.__setattr__(
                self,
                name,
                _canonical_uuid(getattr(self, name), field_name=name),
            )
        if self.account_binding_source != EXACT_PAPER_ACCOUNT_BINDING_SOURCE:
            raise CapturedPaperTransportContractError(
                "broker_order_account_binding_source_invalid"
            )
        if self.broker_account_id is not None:
            object.__setattr__(
                self,
                "broker_account_id",
                _canonical_uuid(
                    self.broker_account_id,
                    field_name="broker_account_id_echo",
                ),
            )
        if not (
            self.verified_adapter_account_id == self.expected_account_id
            and self.broker_account_id
            in {None, self.verified_adapter_account_id}
        ):
            raise CapturedPaperTransportContractError(
                "broker_order_account_binding_mismatch"
            )
        for name in (
            "client_order_id",
            "broker_order_id",
            "broker_connection_generation",
        ):
            object.__setattr__(
                self, name, _identifier(getattr(self, name), field_name=name)
            )
        symbol = str(self.symbol or "").strip().upper()
        if symbol != self.symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            raise CapturedPaperTransportContractError(
                "broker_order_symbol_invalid"
            )
        for name, expected in (
            ("side", "buy"),
            ("order_type", "limit"),
            ("asset_class", "us_equity"),
        ):
            normalized = str(getattr(self, name) or "").strip().lower()
            if normalized != expected:
                raise CapturedPaperTransportContractError(
                    f"broker_order_{name}_invalid"
                )
            object.__setattr__(self, name, normalized)
        raw_quantity = str(self.broker_quantity_echo or "").strip()
        quantity = _nonnegative_whole_share_count(
            raw_quantity, field_name="broker_order_quantity_echo"
        )
        if quantity <= 0 or self.quantity_shares != quantity:
            raise CapturedPaperTransportContractError(
                "broker_order_quantity_invalid"
            )
        object.__setattr__(self, "quantity_shares", quantity)
        object.__setattr__(self, "broker_quantity_echo", raw_quantity)

        raw_fill = self.broker_filled_quantity_echo
        parsed_fill: Decimal | None = None
        parsed_whole_fill: int | None = None
        if raw_fill is not None:
            raw_fill = str(raw_fill).strip()
            try:
                parsed_fill = Decimal(raw_fill)
            except (InvalidOperation, ValueError) as exc:
                raise CapturedPaperTransportContractError(
                    "broker_order_filled_quantity_invalid"
                ) from exc
            if not parsed_fill.is_finite() or parsed_fill < 0:
                raise CapturedPaperTransportContractError(
                    "broker_order_filled_quantity_invalid"
                )
            if parsed_fill == parsed_fill.to_integral_value():
                parsed_whole_fill = int(parsed_fill)
            object.__setattr__(self, "broker_filled_quantity_echo", raw_fill)
        if self.cumulative_filled_quantity_shares != parsed_whole_fill:
            raise CapturedPaperTransportContractError(
                "broker_order_cumulative_fill_projection_mismatch"
            )

        raw_limit = str(self.broker_limit_price_echo or "").strip()
        try:
            raw_limit_decimal = Decimal(raw_limit)
            canonical_limit = quantize_alpaca_equity_limit_price(raw_limit, "buy")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CapturedPaperTransportContractError(
                "broker_order_limit_price_invalid"
            ) from exc
        if (
            not raw_limit_decimal.is_finite()
            or raw_limit_decimal <= 0
            or Decimal(canonical_limit) != raw_limit_decimal
            or str(self.limit_price).strip() != canonical_limit
        ):
            raise CapturedPaperTransportContractError(
                "broker_order_limit_price_not_canonical"
            )
        object.__setattr__(self, "limit_price", canonical_limit)
        object.__setattr__(self, "broker_limit_price_echo", raw_limit)

        tif = str(self.time_in_force or "").strip().lower()
        if tif not in {"day", "gtc"}:
            raise CapturedPaperTransportContractError(
                "broker_order_time_in_force_invalid"
            )
        if type(self.extended_hours) is not bool or (
            self.extended_hours and tif != "day"
        ):
            raise CapturedPaperTransportContractError(
                "broker_order_extended_hours_invalid"
            )
        object.__setattr__(self, "time_in_force", tif)
        if self.position_intent_echo is not None:
            position_intent = str(self.position_intent_echo).strip().lower()
            if position_intent != "buy_to_open":
                raise CapturedPaperTransportContractError(
                    "broker_order_position_intent_mismatch"
                )
            object.__setattr__(self, "position_intent_echo", position_intent)
        raw_status = str(self.broker_order_status_echo or "").strip()
        status = raw_status.lower()
        _identifier(status, field_name="broker_order_status")
        if str(self.broker_order_status or "").strip().lower() != status:
            raise CapturedPaperTransportContractError(
                "broker_order_status_projection_mismatch"
            )
        object.__setattr__(self, "broker_order_status", status)
        object.__setattr__(self, "broker_order_status_echo", raw_status)
        object.__setattr__(
            self,
            "broker_order_evidence_sha256",
            _sha(
                self.broker_order_evidence_sha256,
                field_name="broker_order_evidence_sha256",
            ),
        )
        observed = _aware_utc(
            self.observed_at, field_name="broker_order_observed_at"
        )
        available = _aware_utc(
            self.available_at, field_name="broker_order_available_at"
        )
        if observed > available:
            raise CapturedPaperTransportContractError(
                "broker_order_clock_order_invalid"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)

    def verify_for_instruction(
        self, instruction: CapturedPaperTransportInstruction
    ) -> None:
        expected = {
            "account_scope": instruction.account_scope,
            "expected_account_id": instruction.expected_account_id,
            "verified_adapter_account_id": instruction.expected_account_id,
            "account_binding_source": EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
            "client_order_id": instruction.client_order_id,
            "symbol": instruction.symbol,
            "side": "buy",
            "order_type": "limit",
            "asset_class": "us_equity",
            "quantity_shares": instruction.quantity_shares,
            "limit_price": instruction.limit_price,
            "time_in_force": instruction.time_in_force,
            "extended_hours": instruction.extended_hours,
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise CapturedPaperTransportContractError(
                "broker_order_instruction_mismatch"
            )

    def broker_echo_payload(self) -> dict[str, Any]:
        """Return broker-originated fields only; never fill from instruction."""

        return {
            "broker_account_id_echo": self.broker_account_id,
            "broker_order_id_echo": self.broker_order_id,
            "broker_client_order_id_echo": self.client_order_id,
            "broker_symbol_echo": self.symbol,
            "broker_side_echo": self.side,
            "broker_order_type_echo": self.order_type,
            "broker_asset_class_echo": self.asset_class,
            "broker_quantity_echo": self.broker_quantity_echo,
            "broker_limit_price_echo": self.broker_limit_price_echo,
            "broker_time_in_force_echo": self.time_in_force,
            "broker_extended_hours_echo": self.extended_hours,
            "broker_position_intent_echo": self.position_intent_echo,
            "broker_order_status_echo": self.broker_order_status_echo,
            "broker_filled_quantity_echo": (
                self.broker_filled_quantity_echo
            ),
            "broker_cumulative_filled_quantity_projection": (
                self.cumulative_filled_quantity_shares
            ),
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperPositiveOrderObservation:
    order: CapturedPaperExactBrokerOrderObservation

    def __post_init__(self) -> None:
        if type(self.order) is not CapturedPaperExactBrokerOrderObservation:
            raise CapturedPaperTransportContractError(
                "positive_order_exact_echo_invalid"
            )
        if not (
            self.order.broker_order_status
            in _POSITIVE_ZERO_FILL_BROKER_STATUSES
            and self.order.cumulative_filled_quantity_shares == 0
            and self.order.broker_filled_quantity_echo is not None
        ):
            raise CapturedPaperTransportContractError(
                "positive_order_requires_authoritative_zero_fill"
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.order, name)

    def verify_for_instruction(
        self, instruction: CapturedPaperTransportInstruction
    ) -> None:
        self.order.verify_for_instruction(instruction)


@dataclass(frozen=True, slots=True)
class CapturedPaperFillReconciliationRequiredObservation:
    order: CapturedPaperExactBrokerOrderObservation

    def __post_init__(self) -> None:
        if type(self.order) is not CapturedPaperExactBrokerOrderObservation:
            raise CapturedPaperTransportContractError(
                "fill_reconciliation_exact_echo_invalid"
            )
        raw_fill = self.order.broker_filled_quantity_echo
        positive_fill = bool(
            raw_fill is not None and Decimal(raw_fill) > 0
        )
        if not (
            positive_fill
            or self.order.broker_order_status
            in _FILL_BEARING_BROKER_STATUSES
        ):
            raise CapturedPaperTransportContractError(
                "fill_reconciliation_not_required"
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.order, name)

    def verify_for_instruction(
        self, instruction: CapturedPaperTransportInstruction
    ) -> None:
        self.order.verify_for_instruction(instruction)


@dataclass(frozen=True, slots=True)
class CapturedPaperTerminalZeroFillObservation:
    """Exact terminal broker order with authoritative cumulative zero."""

    order: CapturedPaperExactBrokerOrderObservation

    def __post_init__(self) -> None:
        if type(self.order) is not CapturedPaperExactBrokerOrderObservation:
            raise CapturedPaperTransportContractError(
                "terminal_zero_fill_exact_echo_invalid"
            )
        if not (
            self.order.broker_order_status
            in _TERMINAL_ZERO_FILL_BROKER_STATUSES
            and self.order.cumulative_filled_quantity_shares == 0
            and self.order.broker_filled_quantity_echo is not None
        ):
            raise CapturedPaperTransportContractError(
                "terminal_zero_fill_requires_authoritative_zero"
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.order, name)

    def verify_for_instruction(
        self, instruction: CapturedPaperTransportInstruction
    ) -> None:
        self.order.verify_for_instruction(instruction)


@dataclass(frozen=True, slots=True)
class CapturedPaperUnresolvedObservation:
    reason: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        allowed = {
            "transport_timeout",
            "transport_server_error",
            "transport_rejected_or_ambiguous",
            "transport_exception",
            "cid_absent",
            "cid_unreadable",
            "cid_response_ambiguous",
        }
        if self.reason not in allowed:
            raise CapturedPaperTransportContractError(
                "unresolved_observation_reason_invalid"
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha(self.evidence_sha256, field_name="unresolved_evidence_sha256"),
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperFillReadAuthority:
    account_scope: str
    expected_account_id: str
    reservation_id: str
    client_order_id: str
    broker_order_id: str
    query_receipt_sha256: str
    observation_sha256: str
    exact_activity_count: int
    positive_fill_observed: bool
    pagination_complete: bool
    available_at: datetime
    schema_version: str = FILL_READ_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version != FILL_READ_AUTHORITY_SCHEMA_VERSION
            or self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE
        ):
            raise CapturedPaperTransportContractError(
                "fill_read_authority_scope_invalid"
            )
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id, field_name="fill_read_account_id"
            ),
        )
        object.__setattr__(
            self,
            "reservation_id",
            _canonical_uuid(self.reservation_id, field_name="fill_reservation_id"),
        )
        for name in ("client_order_id", "broker_order_id"):
            object.__setattr__(
                self, name, _identifier(getattr(self, name), field_name=name)
            )
        for name in ("query_receipt_sha256", "observation_sha256"):
            object.__setattr__(
                self, name, _sha(getattr(self, name), field_name=name)
            )
        if (
            isinstance(self.exact_activity_count, bool)
            or not isinstance(self.exact_activity_count, int)
            or self.exact_activity_count < 0
            or self.exact_activity_count > 1_000_000
            or type(self.positive_fill_observed) is not bool
            or self.positive_fill_observed
            != (self.exact_activity_count > 0)
        ):
            raise CapturedPaperTransportContractError(
                "fill_read_activity_projection_invalid"
            )
        if self.pagination_complete is not True:
            raise CapturedPaperTransportContractError(
                "fill_read_pagination_incomplete"
            )
        object.__setattr__(
            self,
            "available_at",
            _aware_utc(self.available_at, field_name="fill_read_available_at"),
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperFillAppendReceipt:
    observation_sha256: str
    durable_receipt_sha256: str
    committed_at: datetime
    positive_fill_handoff_committed: bool = False
    fill_handoff_proof_sha256: str | None = None
    outbox_fill_handoff_receipt_sha256: str | None = None
    schema_version: str = FILL_APPEND_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FILL_APPEND_RECEIPT_SCHEMA_VERSION:
            raise CapturedPaperTransportContractError(
                "fill_append_receipt_schema_invalid"
            )
        for name in ("observation_sha256", "durable_receipt_sha256"):
            object.__setattr__(
                self, name, _sha(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "committed_at",
            _aware_utc(self.committed_at, field_name="fill_append_committed_at"),
        )
        if type(self.positive_fill_handoff_committed) is not bool:
            raise CapturedPaperTransportContractError(
                "fill_append_handoff_flag_invalid"
            )
        handoff_hashes = (
            self.fill_handoff_proof_sha256,
            self.outbox_fill_handoff_receipt_sha256,
        )
        if self.positive_fill_handoff_committed:
            if any(value is None for value in handoff_hashes):
                raise CapturedPaperTransportContractError(
                    "fill_append_handoff_receipt_missing"
                )
            for name, value in zip(
                (
                    "fill_handoff_proof_sha256",
                    "outbox_fill_handoff_receipt_sha256",
                ),
                handoff_hashes,
            ):
                object.__setattr__(
                    self,
                    name,
                    _sha(value, field_name=name),
                )
        elif any(value is not None for value in handoff_hashes):
            raise CapturedPaperTransportContractError(
                "fill_append_handoff_receipt_unexpected"
            )


class CapturedPaperTransactionStore(Protocol):
    """Every database phase commits before returning or yielding to I/O."""

    def load_instruction(
        self,
        completion_sha256: str,
    ) -> CapturedPaperTransportInstruction: ...

    def verify_committed_instruction(
        self,
        instruction: CapturedPaperTransportInstruction,
    ) -> CapturedPaperTransportInstruction: ...

    def next_due_initial_instruction(
        self,
    ) -> CapturedPaperTransportInstruction | None: ...

    def next_due_reconciliation_instruction(
        self,
        *,
        recovery_limit: int,
    ) -> CapturedPaperTransportInstruction | None: ...

    def lease_initial(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperCommittedLease | None: ...

    def start_transport(
        self,
        instruction: CapturedPaperTransportInstruction,
        lease: CapturedPaperCommittedLease,
    ) -> CapturedPaperTransportStart: ...

    def authorize_transport_invocation(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
    ) -> CapturedPaperTransportInvocationAuthority: ...

    def record_financial_breaker_authority(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        receipt: CapturedPaperFinancialBreakerReceipt,
    ) -> CapturedPaperFinancialBreakerReceipt: ...

    def consume_dispatch_authority(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
    ) -> CapturedPaperTransportDispatchAuthority: ...

    def acquire_dispatch_linearization(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
        dispatch_authority: CapturedPaperTransportDispatchAuthority,
    ) -> ContextManager[None]: ...

    def mark_transport_indeterminate(
        self,
        start: CapturedPaperTransportStart,
        *,
        evidence_sha256: str,
    ) -> None: ...

    def complete_direct_acceptance(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        acceptance: CapturedPaperBrokerAcceptanceProof,
    ) -> None: ...

    def lease_reconciliation(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperCommittedLease | None: ...

    def mark_reconciliation_pending(
        self,
        lease: CapturedPaperCommittedLease,
        *,
        evidence_sha256: str,
    ) -> None: ...

    def complete_reconciliation_acceptance(
        self,
        instruction: CapturedPaperTransportInstruction,
        lease: CapturedPaperCommittedLease,
        acceptance: CapturedPaperBrokerAcceptanceProof,
    ) -> None: ...


class CapturedPaperBrokerTransport(Protocol):
    """Network-only PAPER transport; implementations receive no DB handle."""

    def preflight(self, instruction: CapturedPaperTransportInstruction) -> None: ...

    def prepare_limit_buy(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
    ) -> CapturedPaperTransportPreDispatchEvidence: ...

    def post_limit_buy(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
        dispatch_authority: CapturedPaperTransportDispatchAuthority,
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
        | CapturedPaperUnresolvedObservation
    ): ...

    def lookup_same_cid(
        self, instruction: CapturedPaperTransportInstruction
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
        | CapturedPaperUnresolvedObservation
    ): ...


class CapturedPaperFinancialBreakerIssuer(Protocol):
    """Fresh, external-I/O authority evaluated only after the durable fence."""

    def issue_for_request(
        self,
        request: CapturedPaperPostCommitRequest,
        *,
        phase: str,
        transport_instruction_sha256: str | None = None,
        transport_invocation_authority_sha256: str | None = None,
        authority_valid_until: datetime | None = None,
    ) -> CapturedPaperFinancialBreakerReceipt: ...


class CapturedPaperPositiveAcceptanceRecorder(Protocol):
    """Persist positive broker truth and return only after its tx commits."""

    def persist_positive_acceptance(
        self,
        instruction: CapturedPaperTransportInstruction,
        observation: CapturedPaperPositiveOrderObservation,
        *,
        acceptance_kind: str,
    ) -> CapturedPaperBrokerAcceptanceProof: ...


class CapturedPaperFillCapture(Protocol):
    """Migration-340 seam: network read first, append in a later transaction."""

    def read_exact_order_fills(
        self,
        instruction: CapturedPaperTransportInstruction,
        observation: (
            CapturedPaperPositiveOrderObservation
            | CapturedPaperFillReconciliationRequiredObservation
            | CapturedPaperTerminalZeroFillObservation
        ),
    ) -> CapturedPaperFillReadAuthority: ...

    def append_fill_read(
        self,
        read: CapturedPaperFillReadAuthority,
        *,
        instruction: CapturedPaperTransportInstruction,
        fill_handoff_required: bool,
    ) -> CapturedPaperFillAppendReceipt: ...


def _committed_lease(
    raw: CapturedPaperOutboxLease,
    *,
    instruction: CapturedPaperTransportInstruction,
) -> CapturedPaperCommittedLease:
    if type(raw) is not CapturedPaperOutboxLease:
        raise CapturedPaperTransportContractError("outbox_lease_type_invalid")
    if (
        raw.record.request.to_canonical_json()
        != instruction.request.to_canonical_json()
        or raw.record.completion_sha256 != instruction.request.completion_sha256
        or raw.lease_token != raw.record.lease_token
        or raw.lease_owner_id != raw.record.lease_owner_id
        or raw.lease_expires_at != raw.record.lease_expires_at
    ):
        raise CapturedPaperTransportContractError("outbox_lease_lineage_mismatch")
    return CapturedPaperCommittedLease(
        completion_sha256=raw.record.completion_sha256,
        lease_token=raw.lease_token,
        lease_owner_id=raw.lease_owner_id,
        lease_expires_at=raw.lease_expires_at,
        reconciliation_only=raw.reconciliation_only,
    )


class SqlAlchemyCapturedPaperTransportStore:
    """Production transaction boundary around the durable outbox API."""

    def __init__(self, bind: Engine) -> None:
        if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
            raise CapturedPaperTransportContractError(
                "captured_paper_transport_postgresql_engine_required"
            )
        self._bind = bind
        self._factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

    def _transaction(self, operation: Callable[[Any], Any]) -> Any:
        db = self._factory()
        try:
            with db.begin():
                return operation(db)
        finally:
            db.close()

    def load_instruction(
        self,
        completion_sha256: str,
    ) -> CapturedPaperTransportInstruction:
        bundle = self._transaction(
            lambda db: load_captured_paper_durable_transport_bundle(
                db,
                completion_sha256=completion_sha256,
            )
        )
        return CapturedPaperTransportInstruction.from_durable_bundle(bundle)

    def verify_committed_instruction(
        self,
        instruction: CapturedPaperTransportInstruction,
    ) -> CapturedPaperTransportInstruction:
        if type(instruction) is not CapturedPaperTransportInstruction:
            raise CapturedPaperTransportContractError(
                "committed_instruction_type_invalid"
            )
        durable = self.load_instruction(instruction.request.completion_sha256)
        if not (
            durable.instruction_sha256 == instruction.instruction_sha256
            and durable.authority.authority_sha256
            == instruction.authority.authority_sha256
            and durable.order_request == instruction.order_request
        ):
            raise CapturedPaperTransportContractError(
                "committed_admission_durable_instruction_mismatch"
            )
        return durable

    def next_due_initial_instruction(
        self,
    ) -> CapturedPaperTransportInstruction | None:
        completion_sha256 = self._transaction(
            find_next_due_captured_paper_completion
        )
        if completion_sha256 is None:
            return None
        return self.load_instruction(completion_sha256)

    def next_due_reconciliation_instruction(
        self,
        *,
        recovery_limit: int,
    ) -> CapturedPaperTransportInstruction | None:
        def recover_and_find(db: Any) -> str | None:
            recover_expired_captured_paper_leases(
                db,
                limit=recovery_limit,
            )
            return find_next_due_captured_paper_reconciliation(db)

        completion_sha256 = self._transaction(recover_and_find)
        if completion_sha256 is None:
            return None
        return self.load_instruction(completion_sha256)

    def lease_initial(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperCommittedLease | None:
        raw = self._transaction(
            lambda db: lease_captured_paper_completion(
                db,
                completion_sha256=instruction.request.completion_sha256,
                lease_owner_id=lease_owner_id,
                lease_seconds=lease_seconds,
            )
        )
        if raw is None:
            return None
        lease = _committed_lease(raw, instruction=instruction)
        if lease.reconciliation_only:
            raise CapturedPaperTransportContractError(
                "initial_lease_is_reconciliation_only"
            )
        return lease

    def start_transport(
        self,
        instruction: CapturedPaperTransportInstruction,
        lease: CapturedPaperCommittedLease,
    ) -> CapturedPaperTransportStart:
        if lease.completion_sha256 != instruction.request.completion_sha256:
            raise CapturedPaperTransportContractError(
                "transport_start_completion_mismatch"
            )
        record = self._transaction(
            lambda db: mark_captured_paper_transport_started(
                db,
                completion_sha256=lease.completion_sha256,
                lease_token=lease.lease_token,
                lease_owner_id=lease.lease_owner_id,
                authority=instruction.authority,
            )
        )
        if (
            record.status != OUTBOX_STATUS_TRANSPORT_STARTED
            or record.transport_started_at is None
            or record.transport_evidence_sha256
            != instruction.authority.authority_sha256
        ):
            raise CapturedPaperTransportUnavailable(
                "transport_start_commit_unconfirmed"
            )
        return CapturedPaperTransportStart(
            lease=lease,
            instruction_sha256=instruction.instruction_sha256,
            transport_authority_sha256=instruction.authority.authority_sha256,
            started_at=record.transport_started_at,
        )

    def authorize_transport_invocation(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
    ) -> CapturedPaperTransportInvocationAuthority:
        if not (
            start.lease.completion_sha256
            == instruction.request.completion_sha256
            and start.instruction_sha256 == instruction.instruction_sha256
            and start.transport_authority_sha256
            == instruction.authority.authority_sha256
        ):
            raise CapturedPaperTransportContractError(
                "transport_invocation_start_binding_mismatch"
            )
        receipt = self._transaction(
            lambda db: authorize_captured_paper_transport_invocation(
                db,
                completion_sha256=start.lease.completion_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
                authority=instruction.authority,
            )
        )
        receipt.verify_for(
            instruction.authority,
            transport_instruction_sha256=instruction.instruction_sha256,
            lease_token=start.lease.lease_token,
            lease_owner_id=start.lease.lease_owner_id,
        )
        if receipt.transport_started_at != start.started_at:
            raise CapturedPaperTransportUnavailable(
                "transport_invocation_start_time_mismatch"
            )
        return receipt

    def record_financial_breaker_authority(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        receipt: CapturedPaperFinancialBreakerReceipt,
    ) -> CapturedPaperFinancialBreakerReceipt:
        if not (
            start.lease.completion_sha256
            == instruction.request.completion_sha256
            and start.instruction_sha256 == instruction.instruction_sha256
            and start.transport_authority_sha256
            == instruction.authority.authority_sha256
        ):
            raise CapturedPaperTransportContractError(
                "transport_financial_breaker_start_binding_mismatch"
            )
        committed = self._transaction(
            lambda db: record_captured_paper_transport_financial_breaker(
                db,
                completion_sha256=start.lease.completion_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
                authority=instruction.authority,
                invocation_authority=invocation_authority,
                receipt=receipt,
            )
        )
        if type(committed) is not CapturedPaperFinancialBreakerReceipt:
            raise CapturedPaperTransportUnavailable(
                "transport_financial_breaker_commit_unconfirmed"
            )
        committed.verify_for_request(
            instruction.request,
            phase="pre_post",
            now=committed.issued_at,
            require_allowed=False,
            transport_instruction_sha256=instruction.instruction_sha256,
            transport_invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
        )
        if committed.receipt_sha256 != receipt.receipt_sha256:
            raise CapturedPaperTransportUnavailable(
                "transport_financial_breaker_commit_mismatch"
            )
        return committed

    def consume_dispatch_authority(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
    ) -> CapturedPaperTransportDispatchAuthority:
        if not (
            start.lease.completion_sha256
            == instruction.request.completion_sha256
            and start.instruction_sha256 == instruction.instruction_sha256
            and start.transport_authority_sha256
            == instruction.authority.authority_sha256
        ):
            raise CapturedPaperTransportContractError(
                "transport_dispatch_start_binding_mismatch"
            )
        receipt = self._transaction(
            lambda db: consume_captured_paper_transport_dispatch_authority(
                db,
                completion_sha256=start.lease.completion_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
                authority=instruction.authority,
                invocation_authority=invocation_authority,
                financial_breaker_receipt=financial_breaker_receipt,
                pre_dispatch_evidence=pre_dispatch_evidence,
            )
        )
        if type(receipt) is not CapturedPaperTransportDispatchAuthority:
            raise CapturedPaperTransportUnavailable(
                "transport_dispatch_authority_commit_unconfirmed"
            )
        receipt.verify_for(
            instruction.authority,
            invocation_authority,
            financial_breaker_receipt,
            pre_dispatch_evidence,
            transport_instruction_sha256=instruction.instruction_sha256,
        )
        return receipt

    @contextmanager
    def acquire_dispatch_linearization(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
        dispatch_authority: CapturedPaperTransportDispatchAuthority,
    ) -> Iterator[None]:
        """Linearize final live authority against cooperating DB writers.

        PostgreSQL session advisory locks use the exact one-key Alpaca action
        and two-key adaptive-risk identities already taken transactionally by
        authority writers.  After both are held, a fresh *short* transaction
        repeats the complete canonical lock walk and commits.  Only session
        advisory locks remain during the synchronous broker call; no row lock
        and no open database transaction crosses network I/O.

        If an invalidation commits first, this revalidation observes it and the
        caller performs zero POSTs.  If this context acquires both locks first,
        the broker invocation is the linearization point and the invalidating
        writer waits until its outcome is unknown or durably observed.
        """

        if not (
            type(instruction) is CapturedPaperTransportInstruction
            and type(start) is CapturedPaperTransportStart
            and type(invocation_authority)
            is CapturedPaperTransportInvocationAuthority
            and type(financial_breaker_receipt)
            is CapturedPaperFinancialBreakerReceipt
            and type(pre_dispatch_evidence)
            is CapturedPaperTransportPreDispatchEvidence
            and type(dispatch_authority)
            is CapturedPaperTransportDispatchAuthority
        ):
            raise CapturedPaperTransportContractError(
                "transport_dispatch_linearization_input_type_invalid"
            )
        if not (
            start.lease.completion_sha256
            == instruction.request.completion_sha256
            and start.instruction_sha256 == instruction.instruction_sha256
            and start.transport_authority_sha256
            == instruction.authority.authority_sha256
        ):
            raise CapturedPaperTransportContractError(
                "transport_dispatch_linearization_start_binding_mismatch"
            )
        dispatch_authority.verify_for(
            instruction.authority,
            invocation_authority,
            financial_breaker_receipt,
            pre_dispatch_evidence,
            transport_instruction_sha256=instruction.instruction_sha256,
        )

        lock_identity = AdaptiveRiskAccountLockIdentity.for_scope(
            instruction.account_scope
        )
        connection = self._bind.connect()
        action_locked = False
        adaptive_locked = False
        release_failure: Exception | None = None
        try:
            with connection.begin():
                action_locked = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": lock_identity.action_advisory_key},
                    ).scalar_one()
                )
                if not action_locked:
                    raise CapturedPaperTransportUnavailable(
                        "transport_dispatch_action_lock_unavailable"
                    )
                adaptive_locked = bool(
                    connection.execute(
                        text(
                            "SELECT pg_try_advisory_lock("
                            ":namespace, hashtext(:account_scope))"
                        ),
                        {
                            "namespace": (
                                lock_identity.adaptive_advisory_namespace
                            ),
                            "account_scope": lock_identity.account_scope,
                        },
                    ).scalar_one()
                )
                if not adaptive_locked:
                    raise CapturedPaperTransportUnavailable(
                        "transport_dispatch_adaptive_lock_unavailable"
                    )
            # Session locks survive the completed context above.  No implicit
            # acquisition transaction remains open.

            db = self._factory(bind=connection)
            try:
                with db.begin():
                    confirmed = (
                        revalidate_captured_paper_transport_dispatch_authority(
                            db,
                            completion_sha256=(
                                start.lease.completion_sha256
                            ),
                            lease_token=start.lease.lease_token,
                            lease_owner_id=start.lease.lease_owner_id,
                            authority=instruction.authority,
                            invocation_authority=invocation_authority,
                            financial_breaker_receipt=(
                                financial_breaker_receipt
                            ),
                            pre_dispatch_evidence=pre_dispatch_evidence,
                            dispatch_authority=dispatch_authority,
                        )
                    )
            finally:
                db.close()
            if (
                type(confirmed) is not CapturedPaperTransportDispatchAuthority
                or confirmed.dispatch_authority_sha256
                != dispatch_authority.dispatch_authority_sha256
            ):
                raise CapturedPaperTransportUnavailable(
                    "transport_dispatch_linearization_commit_unconfirmed"
                )
            if connection.in_transaction():
                raise CapturedPaperTransportUnavailable(
                    "transport_dispatch_linearization_transaction_leaked"
                )
            yield
        finally:
            try:
                if connection.in_transaction():
                    connection.rollback()
                with connection.begin():
                    if adaptive_locked:
                        released = connection.execute(
                            text(
                                "SELECT pg_advisory_unlock("
                                ":namespace, hashtext(:account_scope))"
                            ),
                            {
                                "namespace": (
                                    lock_identity.adaptive_advisory_namespace
                                ),
                                "account_scope": lock_identity.account_scope,
                            },
                        ).scalar_one()
                        if released is not True:
                            raise CapturedPaperTransportUnavailable(
                                "transport_dispatch_adaptive_lock_release_failed"
                            )
                    if action_locked:
                        released = connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"),
                            {"key": lock_identity.action_advisory_key},
                        ).scalar_one()
                        if released is not True:
                            raise CapturedPaperTransportUnavailable(
                                "transport_dispatch_action_lock_release_failed"
                            )
            except Exception as exc:
                release_failure = exc
                try:
                    connection.invalidate()
                except Exception:
                    pass
            finally:
                connection.close()
            if release_failure is not None:
                raise release_failure

    def mark_transport_indeterminate(
        self,
        start: CapturedPaperTransportStart,
        *,
        evidence_sha256: str,
    ) -> None:
        record = self._transaction(
            lambda db: mark_captured_paper_transport_indeterminate(
                db,
                completion_sha256=start.lease.completion_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
                indeterminate_evidence_sha256=evidence_sha256,
            )
        )
        if record.status != OUTBOX_STATUS_TRANSPORT_INDETERMINATE:
            raise CapturedPaperTransportUnavailable(
                "transport_indeterminate_commit_unconfirmed"
            )

    def complete_direct_acceptance(
        self,
        instruction: CapturedPaperTransportInstruction,
        start: CapturedPaperTransportStart,
        acceptance: CapturedPaperBrokerAcceptanceProof,
    ) -> None:
        record = self._transaction(
            lambda db: mark_captured_paper_completion_accepted(
                db,
                completion_sha256=start.lease.completion_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
                authority=instruction.authority,
                acceptance=acceptance,
            )
        )
        if record.status != OUTBOX_STATUS_COMPLETED:
            raise CapturedPaperTransportUnavailable(
                "direct_acceptance_commit_unconfirmed"
            )

    def lease_reconciliation(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperCommittedLease | None:
        raw = self._transaction(
            lambda db: lease_captured_paper_indeterminate_reconciliation(
                db,
                completion_sha256=instruction.request.completion_sha256,
                lease_owner_id=lease_owner_id,
                lease_seconds=lease_seconds,
            )
        )
        if raw is None:
            return None
        lease = _committed_lease(raw, instruction=instruction)
        if not lease.reconciliation_only:
            raise CapturedPaperTransportContractError(
                "reconciliation_lease_not_reconciliation_only"
            )
        return lease

    def mark_reconciliation_pending(
        self,
        lease: CapturedPaperCommittedLease,
        *,
        evidence_sha256: str,
    ) -> None:
        record = self._transaction(
            lambda db: mark_captured_paper_reconciliation_pending(
                db,
                completion_sha256=lease.completion_sha256,
                lease_token=lease.lease_token,
                lease_owner_id=lease.lease_owner_id,
                reconciliation_evidence_sha256=evidence_sha256,
            )
        )
        if record.status != OUTBOX_STATUS_TRANSPORT_INDETERMINATE:
            raise CapturedPaperTransportUnavailable(
                "reconciliation_pending_commit_unconfirmed"
            )

    def complete_reconciliation_acceptance(
        self,
        instruction: CapturedPaperTransportInstruction,
        lease: CapturedPaperCommittedLease,
        acceptance: CapturedPaperBrokerAcceptanceProof,
    ) -> None:
        record = self._transaction(
            lambda db: mark_captured_paper_reconciliation_accepted(
                db,
                completion_sha256=lease.completion_sha256,
                lease_token=lease.lease_token,
                lease_owner_id=lease.lease_owner_id,
                authority=instruction.authority,
                acceptance=acceptance,
            )
        )
        if record.status != OUTBOX_STATUS_COMPLETED:
            raise CapturedPaperTransportUnavailable(
                "reconciliation_acceptance_commit_unconfirmed"
            )


@dataclass(frozen=True, slots=True)
class _VerifiedAlpacaPaperConnectionReceipt:
    receipt_sha256: str
    adapter_build_sha256: str
    available_at: datetime
    verified_at: datetime


class ExactAlpacaPaperEntryTransport:
    """Exact class-pinned Alpaca transport plus fresh external risk authority."""

    def __init__(
        self,
        *,
        adapter: Any,
        expected_account_id: str,
        broker_connection_generation: str,
        observation_clock: Callable[[], datetime],
        acquire_external_dispatch_authority: Callable[[], ContextManager[None]],
        connection_receipt_max_age_seconds: float = 5.0,
    ) -> None:
        self._adapter = adapter
        self._expected_account_id = _canonical_uuid(
            expected_account_id, field_name="transport_adapter_account_id"
        )
        self._connection_generation = _identifier(
            broker_connection_generation,
            field_name="transport_adapter_connection_generation",
        )
        if not callable(observation_clock):
            raise CapturedPaperTransportContractError(
                "transport_observation_clock_invalid"
            )
        if not callable(acquire_external_dispatch_authority):
            raise CapturedPaperTransportContractError(
                "transport_external_dispatch_authority_invalid"
            )
        self._clock = observation_clock
        self._acquire_external_dispatch_authority = (
            acquire_external_dispatch_authority
        )
        try:
            receipt_max_age = Decimal(
                str(connection_receipt_max_age_seconds)
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CapturedPaperTransportContractError(
                "transport_connection_receipt_max_age_invalid"
            ) from exc
        if (
            not receipt_max_age.is_finite()
            or receipt_max_age <= 0
            or receipt_max_age > Decimal("60")
        ):
            raise CapturedPaperTransportContractError(
                "transport_connection_receipt_max_age_invalid"
            )
        self._connection_receipt_max_age_seconds = float(receipt_max_age)
        self._assert_adapter_binding()

    def _assert_exact_adapter_method_identity(self) -> None:
        adapter_class = _alpaca_spot.AlpacaSpotAdapter
        broker_environment = adapter_class.broker_environment
        expected_methods = (
            (adapter_class.bind_account_id, _EXACT_ALPACA_BIND_ACCOUNT_METHOD),
            (adapter_class.is_enabled, _EXACT_ALPACA_IS_ENABLED_METHOD),
            (
                broker_environment,
                _EXACT_ALPACA_BROKER_ENVIRONMENT_PROPERTY,
            ),
            (
                adapter_class.get_paper_connection_generation_receipt,
                _EXACT_ALPACA_CONNECTION_RECEIPT_METHOD,
            ),
            (
                adapter_class.place_limit_order_gtc,
                _EXACT_ALPACA_ENTRY_POST_METHOD,
            ),
            (adapter_class._submit, _EXACT_ALPACA_ENTRY_SUBMIT_METHOD),
            (
                adapter_class.get_order_by_client_order_id_truth,
                _EXACT_ALPACA_CID_LOOKUP_METHOD,
            ),
            (
                adapter_class._account_client,
                _EXACT_ALPACA_ACCOUNT_CLIENT_METHOD,
            ),
            (
                adapter_class._normalize_order,
                _EXACT_ALPACA_NORMALIZE_ORDER_METHOD,
            ),
            (
                adapter_class._resolve_position_intent,
                _EXACT_ALPACA_POSITION_INTENT_METHOD,
            ),
        )
        if (
            adapter_class is not _EXACT_ALPACA_SPOT_ADAPTER_CLASS
            or _alpaca_spot._EXACT_PAPER_CONNECTION_RECEIPT_METHOD
            is not _EXACT_ALPACA_CONNECTION_RECEIPT_METHOD
            or _alpaca_spot._EXACT_FILL_ACCOUNT_CLIENT_METHOD
            is not _EXACT_ALPACA_ACCOUNT_CLIENT_METHOD
            or any(current is not frozen for current, frozen in expected_methods)
        ):
            raise CapturedPaperTransportContractError(
                "transport_adapter_method_identity_changed"
            )
        shadowed = {
            "bind_account_id",
            "broker_environment",
            "get_order_by_client_order_id_truth",
            "get_paper_connection_generation_receipt",
            "is_enabled",
            "place_limit_order_gtc",
            "_account_client",
            "_normalize_order",
            "_resolve_position_intent",
            "_submit",
        }.intersection(getattr(self._adapter, "__dict__", {}))
        if shadowed:
            raise CapturedPaperTransportContractError(
                "transport_adapter_instance_method_shadowed"
            )

    def _assert_adapter_binding(self) -> None:
        if type(self._adapter) is not _EXACT_ALPACA_SPOT_ADAPTER_CLASS:
            raise CapturedPaperTransportContractError(
                "transport_adapter_exact_class_required"
            )
        self._assert_exact_adapter_method_identity()
        if (
            _EXACT_ALPACA_BROKER_ENVIRONMENT_PROPERTY.fget(
                self._adapter
            )
            != "paper"
            or str(getattr(self._adapter, "bound_account_id", "") or "").strip()
            != self._expected_account_id
        ):
            raise CapturedPaperTransportContractError(
                "transport_adapter_paper_account_mismatch"
            )
        # ``bind_account_id`` rechecks the current configured UUID.  Because
        # the exact adapter is already bound above, this is an idempotent
        # configuration-generation fence rather than a late mutation.
        if (
            _EXACT_ALPACA_BIND_ACCOUNT_METHOD(
                self._adapter, self._expected_account_id
            )
            is not True
        ):
            raise CapturedPaperTransportContractError(
                "transport_adapter_configured_account_mismatch"
            )
        if _EXACT_ALPACA_IS_ENABLED_METHOD(self._adapter) is not True:
            raise CapturedPaperTransportContractError(
                "transport_adapter_paper_execution_disabled"
            )

    def preflight(self, instruction: CapturedPaperTransportInstruction) -> None:
        if type(instruction) is not CapturedPaperTransportInstruction:
            raise CapturedPaperTransportContractError(
                "transport_preflight_instruction_invalid"
            )
        self._assert_adapter_binding()
        if (
            instruction.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE
            or instruction.expected_account_id != self._expected_account_id
        ):
            raise CapturedPaperTransportContractError(
                "transport_preflight_account_mismatch"
            )
        # Rebuild the deterministic adapter mapping before permission is leased.
        kwargs = instruction.adapter_kwargs()
        if (
            kwargs["side"] != "buy"
            or kwargs["position_intent"] != "buy_to_open"
            or type(kwargs["extended_hours"]) is not bool
            or kwargs["time_in_force"] not in {"day", "gtc"}
            or (
                kwargs["extended_hours"] is True
                and kwargs["time_in_force"] != "day"
            )
        ):
            raise CapturedPaperTransportContractError(
                "transport_preflight_instruction_not_certified"
            )

    def _clock_now(self, *, field_name: str) -> datetime:
        return _aware_utc(self._clock(), field_name=field_name)

    def _verify_fresh_connection_receipt_before_io(
        self,
        *,
        operation: str,
    ) -> _VerifiedAlpacaPaperConnectionReceipt:
        """Mint and verify the exact authenticated PAPER generation.

        This deliberately runs only after the durable transport marker (or a
        reconciliation lease) has been committed.  ``preflight`` remains a
        local/no-network shape and posture check.  A receipt failure therefore
        cannot leave a marker-free row eligible for another POST.
        """

        self._assert_adapter_binding()
        requested_at = self._clock_now(
            field_name=f"{operation}_connection_receipt_requested_at"
        )
        try:
            raw_receipt = _EXACT_ALPACA_CONNECTION_RECEIPT_METHOD(
                self._adapter
            )
        except Exception as exc:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_unavailable"
            ) from exc
        verified_at = self._clock_now(
            field_name=f"{operation}_connection_receipt_verified_at"
        )
        if verified_at < requested_at:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_clock_order_invalid"
            )
        if type(raw_receipt) is not dict:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_shape_invalid"
            )
        payload_keys = (
            "schema_version",
            "broker_environment",
            "asset_class",
            "provider_account_id",
            "adapter_connection_generation",
            "adapter_build_sha256",
            "available_at",
        )
        if set(raw_receipt) != {
            *payload_keys,
            "receipt_canonical_json",
            "receipt_sha256",
        }:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_shape_invalid"
            )
        receipt_json = raw_receipt.get("receipt_canonical_json")
        if not isinstance(receipt_json, str) or not receipt_json:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_canonical_json_invalid"
            )
        try:
            payload = json.loads(receipt_json)
        except (TypeError, ValueError) as exc:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_canonical_json_invalid"
            ) from exc
        expected_payload = {key: raw_receipt.get(key) for key in payload_keys}
        if (
            type(payload) is not dict
            or payload != expected_payload
            or receipt_json != _canonical_json(payload)
        ):
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_content_mismatch"
            )
        receipt_sha256 = _sha(
            raw_receipt.get("receipt_sha256"),
            field_name="broker_connection_receipt_sha256",
        )
        if hashlib.sha256(receipt_json.encode("utf-8")).hexdigest() != (
            receipt_sha256
        ):
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_hash_mismatch"
            )
        if (
            payload.get("schema_version")
            != "chili.alpaca-paper-connection-generation.v1"
            or payload.get("broker_environment") != "paper"
            or payload.get("asset_class") != "us_equity"
            or _canonical_uuid(
                payload.get("provider_account_id"),
                field_name="broker_connection_receipt_account_id",
            )
            != self._expected_account_id
            or _identifier(
                payload.get("adapter_connection_generation"),
                field_name="broker_connection_receipt_generation",
            )
            != self._connection_generation
            or _sha(
                payload.get("adapter_build_sha256"),
                field_name="broker_connection_receipt_adapter_build_sha256",
            )
            != _EXACT_ALPACA_ADAPTER_BUILD_SHA256
            or str(getattr(self._adapter, "bound_account_id", "") or "").strip()
            != self._expected_account_id
        ):
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_binding_mismatch"
            )
        raw_available_at = payload.get("available_at")
        if not isinstance(raw_available_at, str):
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_available_at_invalid"
            )
        try:
            parsed_available_at = datetime.fromisoformat(raw_available_at)
        except ValueError as exc:
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_available_at_invalid"
            ) from exc
        available_at = _aware_utc(
            parsed_available_at,
            field_name="broker_connection_receipt_available_at",
        )
        if (
            parsed_available_at.utcoffset() != timedelta(0)
            or raw_available_at != parsed_available_at.isoformat()
            or available_at > verified_at + timedelta(seconds=1)
            or verified_at - available_at
            > timedelta(seconds=self._connection_receipt_max_age_seconds)
        ):
            raise CapturedPaperTransportContractError(
                "broker_connection_receipt_stale_or_future"
            )
        # Recheck local method identity after the receipt and dispatch only the
        # frozen function object.  This rejects late class/instance drift while
        # avoiding mutable bound-method lookup for the order I/O itself.
        self._assert_exact_adapter_method_identity()
        return _VerifiedAlpacaPaperConnectionReceipt(
            receipt_sha256=receipt_sha256,
            adapter_build_sha256=str(payload["adapter_build_sha256"]),
            available_at=available_at,
            verified_at=verified_at,
        )

    def _unresolved(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        reason: str,
        requested_at: datetime,
        available_at: datetime,
        metadata: Mapping[str, Any],
        connection_receipt_sha256: str,
        invocation_authority_sha256: str | None = None,
        financial_breaker_receipt_sha256: str | None = None,
    ) -> CapturedPaperUnresolvedObservation:
        evidence = _sha256_json(
            {
                "schema_version": TRANSPORT_OBSERVATION_SCHEMA_VERSION,
                "observation_kind": "unresolved",
                "reason": reason,
                "instruction_sha256": instruction.instruction_sha256,
                "account_scope": instruction.account_scope,
                "expected_account_id": instruction.expected_account_id,
                "client_order_id": instruction.client_order_id,
                "broker_connection_generation": self._connection_generation,
                "broker_connection_receipt_sha256": _sha(
                    connection_receipt_sha256,
                    field_name="unresolved_connection_receipt_sha256",
                ),
                "transport_invocation_authority_sha256": (
                    _sha(
                        invocation_authority_sha256,
                        field_name="unresolved_invocation_authority_sha256",
                    )
                    if invocation_authority_sha256 is not None
                    else None
                ),
                "financial_breaker_receipt_sha256": (
                    _sha(
                        financial_breaker_receipt_sha256,
                        field_name=(
                            "unresolved_financial_breaker_receipt_sha256"
                        ),
                    )
                    if financial_breaker_receipt_sha256 is not None
                    else None
                ),
                "requested_at": requested_at.isoformat(),
                "available_at": available_at.isoformat(),
                "metadata": dict(metadata),
            }
        )
        return CapturedPaperUnresolvedObservation(
            reason=reason,
            evidence_sha256=evidence,
        )

    def _classify_exact_order_echo(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        broker_echo: Mapping[str, Any],
        observed_at: datetime,
        available_at: datetime,
        evidence_context: str,
        connection_receipt_sha256: str,
        invocation_authority_sha256: str | None = None,
        financial_breaker_receipt_sha256: str | None = None,
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
    ):
        actual_echo = dict(broker_echo)
        quantity = _nonnegative_whole_share_count(
            actual_echo.get("broker_quantity_echo"),
            field_name="broker_echo_quantity",
        )
        if quantity <= 0:
            raise CapturedPaperTransportContractError(
                "broker_echo_quantity_invalid"
            )
        raw_fill = actual_echo.get("broker_filled_quantity_echo")
        whole_fill: int | None = None
        if raw_fill is not None:
            try:
                parsed_fill = Decimal(str(raw_fill))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise CapturedPaperTransportContractError(
                    "broker_echo_filled_quantity_invalid"
                ) from exc
            if not parsed_fill.is_finite() or parsed_fill < 0:
                raise CapturedPaperTransportContractError(
                    "broker_echo_filled_quantity_invalid"
                )
            if parsed_fill == parsed_fill.to_integral_value():
                whole_fill = int(parsed_fill)
        raw_limit = str(actual_echo.get("broker_limit_price_echo") or "").strip()
        try:
            canonical_limit = quantize_alpaca_equity_limit_price(
                raw_limit, "buy"
            )
        except (TypeError, ValueError) as exc:
            raise CapturedPaperTransportContractError(
                "broker_echo_limit_price_invalid"
            ) from exc
        evidence = _sha256_json(
            {
                "schema_version": TRANSPORT_OBSERVATION_SCHEMA_VERSION,
                "observation_kind": "exact_broker_order_echo",
                "evidence_context": evidence_context,
                "instruction_sha256": instruction.instruction_sha256,
                "account_scope": instruction.account_scope,
                "expected_account_id": instruction.expected_account_id,
                "verified_adapter_account_id": str(
                    getattr(self._adapter, "bound_account_id", "") or ""
                ).strip(),
                "account_binding_source": (
                    EXACT_PAPER_ACCOUNT_BINDING_SOURCE
                ),
                "broker_echo": actual_echo,
                "broker_connection_generation": self._connection_generation,
                "broker_connection_receipt_sha256": _sha(
                    connection_receipt_sha256,
                    field_name="broker_order_connection_receipt_sha256",
                ),
                "transport_invocation_authority_sha256": (
                    _sha(
                        invocation_authority_sha256,
                        field_name=(
                            "broker_order_invocation_authority_sha256"
                        ),
                    )
                    if invocation_authority_sha256 is not None
                    else None
                ),
                "financial_breaker_receipt_sha256": (
                    _sha(
                        financial_breaker_receipt_sha256,
                        field_name=(
                            "broker_order_financial_breaker_receipt_sha256"
                        ),
                    )
                    if financial_breaker_receipt_sha256 is not None
                    else None
                ),
                "observed_at": observed_at.isoformat(),
                "available_at": available_at.isoformat(),
            }
        )
        exact = CapturedPaperExactBrokerOrderObservation(
            account_scope=instruction.account_scope,
            expected_account_id=instruction.expected_account_id,
            verified_adapter_account_id=str(
                getattr(self._adapter, "bound_account_id", "") or ""
            ).strip(),
            account_binding_source=EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
            broker_account_id=actual_echo.get("broker_account_id_echo"),
            client_order_id=actual_echo.get("broker_client_order_id_echo"),
            broker_order_id=actual_echo.get("broker_order_id_echo"),
            symbol=actual_echo.get("broker_symbol_echo"),
            side=actual_echo.get("broker_side_echo"),
            order_type=actual_echo.get("broker_order_type_echo"),
            asset_class=actual_echo.get("broker_asset_class_echo"),
            quantity_shares=quantity,
            broker_quantity_echo=str(
                actual_echo.get("broker_quantity_echo") or ""
            ).strip(),
            broker_filled_quantity_echo=(
                str(raw_fill).strip() if raw_fill is not None else None
            ),
            cumulative_filled_quantity_shares=whole_fill,
            limit_price=canonical_limit,
            broker_limit_price_echo=raw_limit,
            time_in_force=actual_echo.get("broker_time_in_force_echo"),
            extended_hours=actual_echo.get("broker_extended_hours_echo"),
            position_intent_echo=actual_echo.get("broker_position_intent_echo"),
            broker_order_status=actual_echo.get("broker_order_status_echo"),
            broker_order_status_echo=str(
                actual_echo.get("broker_order_status_echo") or ""
            ).strip(),
            broker_connection_generation=self._connection_generation,
            broker_order_evidence_sha256=evidence,
            observed_at=observed_at,
            available_at=available_at,
        )
        exact.verify_for_instruction(instruction)
        if (
            exact.broker_order_status in _POSITIVE_ZERO_FILL_BROKER_STATUSES
            and exact.cumulative_filled_quantity_shares == 0
        ):
            return CapturedPaperPositiveOrderObservation(order=exact)
        raw_fill_decimal = (
            Decimal(exact.broker_filled_quantity_echo)
            if exact.broker_filled_quantity_echo is not None
            else None
        )
        if (
            exact.broker_order_status in _FILL_BEARING_BROKER_STATUSES
            or (raw_fill_decimal is not None and raw_fill_decimal > 0)
        ):
            return CapturedPaperFillReconciliationRequiredObservation(
                order=exact
            )
        raise CapturedPaperTransportContractError(
            "broker_echo_not_positive_or_fill_bearing"
        )

    def _interpret_direct_post_result(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        raw: Any,
        requested_at: datetime,
        available_at: datetime,
        connection_receipt_sha256: str,
        invocation_authority_sha256: str | None = None,
        financial_breaker_receipt_sha256: str | None = None,
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
        | CapturedPaperUnresolvedObservation
    ):
        """Interpret already-observed broker bytes without performing I/O."""

        if available_at < requested_at:
            raise CapturedPaperTransportContractError(
                "transport_post_clock_order_invalid"
            )
        if type(raw) is not dict:
            return self._unresolved(
                instruction,
                reason="transport_rejected_or_ambiguous",
                requested_at=requested_at,
                available_at=available_at,
                metadata={"response_type": type(raw).__name__},
                connection_receipt_sha256=connection_receipt_sha256,
                invocation_authority_sha256=(
                    invocation_authority_sha256
                ),
                financial_breaker_receipt_sha256=(
                    financial_breaker_receipt_sha256
                ),
            )
        if raw.get("ok") is True:
            resolved_intent = str(
                raw.get("position_intent") or ""
            ).strip().lower()
            broker_echo_matches = bool(
                "position_intent_echo" not in raw
                or str(raw.get("position_intent_echo") or "").strip().lower()
                == "buy_to_open"
            )
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
            echo_oid = str(broker_echo["broker_order_id_echo"] or "").strip()
            echo_cid = str(
                broker_echo["broker_client_order_id_echo"] or ""
            ).strip()
            try:
                classified = self._classify_exact_order_echo(
                    instruction,
                    broker_echo=broker_echo,
                    observed_at=available_at,
                    available_at=available_at,
                    evidence_context="direct_post_response",
                    connection_receipt_sha256=connection_receipt_sha256,
                    invocation_authority_sha256=(
                        invocation_authority_sha256
                    ),
                    financial_breaker_receipt_sha256=(
                        financial_breaker_receipt_sha256
                    ),
                )
                cumulative_projection = raw.get(
                    "broker_cumulative_filled_quantity"
                )
                if cumulative_projection is not None and (
                    classified.cumulative_filled_quantity_shares
                    != _nonnegative_whole_share_count(
                        cumulative_projection,
                        field_name="direct_post_cumulative_fill",
                    )
                ):
                    raise CapturedPaperTransportContractError(
                        "direct_post_fill_projection_mismatch"
                    )
                if not (
                    str(raw.get("order_id") or "").strip() == echo_oid
                    and str(raw.get("client_order_id") or "").strip()
                    == echo_cid
                    and resolved_intent == "buy_to_open"
                    and broker_echo_matches
                ):
                    raise CapturedPaperTransportContractError(
                        "direct_post_local_echo_mismatch"
                    )
                return classified
            except CapturedPaperTransportContractError:
                pass
        try:
            http_status = int(raw.get("http_status"))
        except (TypeError, ValueError):
            http_status = None
        if http_status == 408 or raw.get("submit_outcome") == "indeterminate":
            reason = "transport_timeout"
        elif http_status is not None and http_status >= 500:
            reason = "transport_server_error"
        else:
            # Even a 4xx response is retained here.  This coordinator has no
            # call-local terminalization contract, and CID absence later is not
            # global proof that Alpaca never accepted the POST.
            reason = "transport_rejected_or_ambiguous"
        return self._unresolved(
            instruction,
            reason=reason,
            requested_at=requested_at,
            available_at=available_at,
            metadata={
                "http_status": http_status,
                "definitive_reject": raw.get("definitive_reject") is True,
                "duplicate_client_order_id": (
                    raw.get("duplicate_client_order_id") is True
                ),
                "pre_submit_blocked": raw.get("pre_submit_blocked") is True,
            },
            connection_receipt_sha256=connection_receipt_sha256,
            invocation_authority_sha256=invocation_authority_sha256,
            financial_breaker_receipt_sha256=(
                financial_breaker_receipt_sha256
            ),
        )

    def prepare_limit_buy(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
    ) -> CapturedPaperTransportPreDispatchEvidence:
        """Refresh exact PAPER account/generation truth without order I/O."""

        if type(invocation_authority) is not (
            CapturedPaperTransportInvocationAuthority
        ):
            raise CapturedPaperTransportContractError(
                "transport_invocation_authority_type_invalid"
            )
        invocation_authority.verify_for(
            instruction.authority,
            transport_instruction_sha256=instruction.instruction_sha256,
            lease_token=invocation_authority.lease_token,
            lease_owner_id=invocation_authority.lease_owner_id,
        )
        self.preflight(instruction)
        connection_receipt = self._verify_fresh_connection_receipt_before_io(
            operation="transport_post"
        )
        prepared_at = self._clock_now(field_name="transport_post_prepared_at")
        if (
            invocation_authority.verified_at
            > prepared_at
            or prepared_at >= invocation_authority.valid_until
            or prepared_at - invocation_authority.verified_at
            > timedelta(seconds=self._connection_receipt_max_age_seconds)
        ):
            raise CapturedPaperTransportContractError(
                "transport_invocation_authority_stale_or_future"
            )
        valid_until = min(
            invocation_authority.valid_until,
            connection_receipt.available_at
            + timedelta(seconds=self._connection_receipt_max_age_seconds),
        )
        if prepared_at >= valid_until:
            raise CapturedPaperTransportContractError(
                "transport_pre_dispatch_evidence_expired"
            )
        return CapturedPaperTransportPreDispatchEvidence(
            completion_sha256=instruction.request.completion_sha256,
            transport_authority_sha256=instruction.authority.authority_sha256,
            transport_instruction_sha256=instruction.instruction_sha256,
            invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
            connection_receipt_sha256=connection_receipt.receipt_sha256,
            account_scope=instruction.account_scope,
            expected_account_id=instruction.expected_account_id,
            broker_connection_generation=self._connection_generation,
            adapter_build_sha256=connection_receipt.adapter_build_sha256,
            connection_available_at=connection_receipt.available_at,
            prepared_at=prepared_at,
            valid_until=valid_until,
        )

    def post_limit_buy(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
        dispatch_authority: CapturedPaperTransportDispatchAuthority,
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
        | CapturedPaperUnresolvedObservation
    ):
        self.preflight(instruction)
        if not (
            type(pre_dispatch_evidence)
            is CapturedPaperTransportPreDispatchEvidence
            and type(dispatch_authority)
            is CapturedPaperTransportDispatchAuthority
        ):
            raise CapturedPaperTransportContractError(
                "transport_dispatch_authority_type_invalid"
            )
        dispatch_authority.verify_for(
            instruction.authority,
            invocation_authority,
            financial_breaker_receipt,
            pre_dispatch_evidence,
            transport_instruction_sha256=instruction.instruction_sha256,
        )
        dispatch_at = self._clock_now(field_name="transport_post_dispatch_at")
        try:
            financial_breaker_receipt.verify_for_request(
                instruction.request,
                phase="pre_post",
                now=dispatch_at,
                require_allowed=True,
                transport_instruction_sha256=instruction.instruction_sha256,
                transport_invocation_authority_sha256=(
                    invocation_authority.invocation_authority_sha256
                ),
            )
        except CapturedPaperFinancialBreakerError as exc:
            raise _CapturedPaperTransportFinancialBreakerError(
                "transport_financial_breaker_stale_before_io",
                receipt=financial_breaker_receipt,
            ) from exc
        if not (
            pre_dispatch_evidence.prepared_at
            <= dispatch_authority.verified_at
            <= dispatch_at
            < dispatch_authority.valid_until
        ):
            raise CapturedPaperTransportContractError(
                "transport_dispatch_authority_stale_or_future"
            )
        if (
            pre_dispatch_evidence.broker_connection_generation
            != self._connection_generation
            or pre_dispatch_evidence.adapter_build_sha256
            != _EXACT_ALPACA_ADAPTER_BUILD_SHA256
            or pre_dispatch_evidence.expected_account_id
            != self._expected_account_id
        ):
            raise CapturedPaperTransportContractError(
                "transport_pre_dispatch_runtime_binding_mismatch"
            )
        # Linearize the irreversible call against host rollback.  Rollback
        # publishes its tombstone while holding this same interprocess lock;
        # therefore any POST inside the context is definitively pre-revocation.
        dispatch_context = self._acquire_external_dispatch_authority()
        if not (
            callable(getattr(dispatch_context, "__enter__", None))
            and callable(getattr(dispatch_context, "__exit__", None))
        ):
            raise CapturedPaperTransportContractError(
                "transport_external_dispatch_context_invalid"
            )
        with dispatch_context:
            invocation_at = self._clock_now(
                field_name="transport_post_lock_acquired_at"
            )
            try:
                financial_breaker_receipt.verify_for_request(
                    instruction.request,
                    phase="pre_post",
                    now=invocation_at,
                    require_allowed=True,
                    transport_instruction_sha256=(
                        instruction.instruction_sha256
                    ),
                    transport_invocation_authority_sha256=(
                        invocation_authority.invocation_authority_sha256
                    ),
                )
            except CapturedPaperFinancialBreakerError as exc:
                raise _CapturedPaperTransportFinancialBreakerError(
                    "transport_financial_breaker_expired_while_waiting_for_host_lock",
                    receipt=financial_breaker_receipt,
                ) from exc
            dispatch_authority.verify_for(
                instruction.authority,
                invocation_authority,
                financial_breaker_receipt,
                pre_dispatch_evidence,
                transport_instruction_sha256=instruction.instruction_sha256,
            )
            if not (
                invocation_authority.verified_at
                <= pre_dispatch_evidence.prepared_at
                <= dispatch_authority.verified_at
                <= invocation_at
                < invocation_authority.valid_until
                and invocation_at < pre_dispatch_evidence.valid_until
                and invocation_at < dispatch_authority.valid_until
                and pre_dispatch_evidence.broker_connection_generation
                == self._connection_generation
                and pre_dispatch_evidence.adapter_build_sha256
                == _EXACT_ALPACA_ADAPTER_BUILD_SHA256
                and pre_dispatch_evidence.expected_account_id
                == self._expected_account_id
            ):
                raise CapturedPaperTransportContractError(
                    "transport_authority_expired_while_waiting_for_host_lock"
                )
            self._assert_exact_adapter_method_identity()
            _consume_transport_dispatch_process_attestation(dispatch_authority)
            requested_at = invocation_at
            try:
                raw = _EXACT_ALPACA_ENTRY_POST_METHOD(
                    self._adapter,
                    **instruction.adapter_kwargs()
                )
            except Exception as exc:
                available_at = self._clock_now(
                    field_name="transport_post_exception_available_at"
                )
                return self._unresolved(
                    instruction,
                    reason="transport_exception",
                    requested_at=requested_at,
                    available_at=available_at,
                    metadata={"exception_type": type(exc).__name__},
                    connection_receipt_sha256=(
                        pre_dispatch_evidence.connection_receipt_sha256
                    ),
                    invocation_authority_sha256=(
                        invocation_authority.invocation_authority_sha256
                    ),
                    financial_breaker_receipt_sha256=(
                        financial_breaker_receipt.receipt_sha256
                    ),
                )
        available_at = self._clock_now(field_name="transport_post_available_at")
        return self._interpret_direct_post_result(
            instruction,
            raw=raw,
            requested_at=requested_at,
            available_at=available_at,
            connection_receipt_sha256=(
                pre_dispatch_evidence.connection_receipt_sha256
            ),
            invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
            financial_breaker_receipt_sha256=(
                financial_breaker_receipt.receipt_sha256
            ),
        )

    def lookup_same_cid(
        self, instruction: CapturedPaperTransportInstruction
    ) -> (
        CapturedPaperPositiveOrderObservation
        | CapturedPaperFillReconciliationRequiredObservation
        | CapturedPaperUnresolvedObservation
    ):
        self.preflight(instruction)
        requested_at = self._clock_now(field_name="cid_lookup_requested_at")
        connection_receipt = self._verify_fresh_connection_receipt_before_io(
            operation="cid_lookup"
        )
        try:
            truth = _EXACT_ALPACA_CID_LOOKUP_METHOD(
                self._adapter,
                instruction.client_order_id
            )
        except Exception as exc:
            available_at = self._clock_now(
                field_name="cid_lookup_exception_available_at"
            )
            return self._unresolved(
                instruction,
                reason="cid_unreadable",
                requested_at=requested_at,
                available_at=available_at,
                metadata={"exception_type": type(exc).__name__},
                connection_receipt_sha256=(
                    connection_receipt.receipt_sha256
                ),
            )
        available_at = self._clock_now(field_name="cid_lookup_available_at")
        if available_at < requested_at:
            raise CapturedPaperTransportContractError(
                "cid_lookup_clock_order_invalid"
            )
        if type(truth) is not dict or truth.get("readable") is not True:
            return self._unresolved(
                instruction,
                reason="cid_unreadable",
                requested_at=requested_at,
                available_at=available_at,
                metadata={"truth_shape": type(truth).__name__},
                connection_receipt_sha256=(
                    connection_receipt.receipt_sha256
                ),
            )
        if truth.get("found") is not True:
            return self._unresolved(
                instruction,
                reason="cid_absent",
                requested_at=requested_at,
                available_at=available_at,
                metadata={"explicit_lookup_absence": True},
                connection_receipt_sha256=(
                    connection_receipt.receipt_sha256
                ),
            )
        order = truth.get("order")
        raw = getattr(order, "raw", None)
        raw = raw if type(raw) is dict else {}
        cid = str(getattr(order, "client_order_id", "") or "").strip()
        order_id = str(getattr(order, "order_id", "") or "").strip()
        symbol = str(getattr(order, "product_id", "") or "").strip().upper()
        side = str(getattr(order, "side", "") or "").strip().lower()
        status = str(getattr(order, "status", "") or "").strip().lower()
        order_type = str(getattr(order, "order_type", "") or "").strip().lower()
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
            classified = self._classify_exact_order_echo(
                instruction,
                broker_echo=broker_echo,
                observed_at=available_at,
                available_at=available_at,
                evidence_context="same_cid_reconciliation",
                connection_receipt_sha256=(
                    connection_receipt.receipt_sha256
                ),
            )
            normalized_fill = raw.get("filled_size")
            if normalized_fill is not None and (
                classified.cumulative_filled_quantity_shares
                != _nonnegative_whole_share_count(
                    normalized_fill,
                    field_name="cid_lookup_normalized_fill",
                )
            ):
                raise CapturedPaperTransportContractError(
                    "cid_lookup_fill_projection_mismatch"
                )
            if not (
                cid == classified.client_order_id
                and order_id == classified.broker_order_id
                and symbol == classified.symbol
                and side == classified.side
                and order_type == classified.order_type
                and _IDENTIFIER_RE.fullmatch(status) is not None
                and (
                    type(classified)
                    is CapturedPaperFillReconciliationRequiredObservation
                    or raw.get("fill_truth_readable") is True
                )
            ):
                raise CapturedPaperTransportContractError(
                    "cid_lookup_normalized_echo_mismatch"
                )
            return classified
        except CapturedPaperTransportContractError:
            return self._unresolved(
                instruction,
                reason="cid_response_ambiguous",
                requested_at=requested_at,
                available_at=available_at,
                metadata={"found_but_economic_binding_failed": True},
                connection_receipt_sha256=(
                    connection_receipt.receipt_sha256
                ),
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportOutcome:
    status: str
    completion_sha256: str
    client_order_id: str
    broker_order_id: str | None = None
    evidence_sha256: str | None = None
    fill_status: str = "not_applicable"
    fill_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        allowed = {
            "no_work",
            "transport_indeterminate",
            "reconciliation_pending",
            "fill_reconciliation_required",
            "accepted",
        }
        if self.status not in allowed:
            raise CapturedPaperTransportContractError(
                "transport_outcome_status_invalid"
            )
        object.__setattr__(
            self,
            "completion_sha256",
            _sha(self.completion_sha256, field_name="outcome_completion_sha256"),
        )
        object.__setattr__(
            self,
            "client_order_id",
            _identifier(self.client_order_id, field_name="outcome_client_order_id"),
        )
        if self.broker_order_id is not None:
            object.__setattr__(
                self,
                "broker_order_id",
                _identifier(self.broker_order_id, field_name="outcome_broker_order_id"),
            )
        if self.evidence_sha256 is not None:
            object.__setattr__(
                self,
                "evidence_sha256",
                _sha(self.evidence_sha256, field_name="outcome_evidence_sha256"),
            )
        if self.fill_status not in {
            "not_applicable",
            "coverage_unavailable",
            "durably_appended",
            "fill_handoff_committed",
        }:
            raise CapturedPaperTransportContractError(
                "transport_outcome_fill_status_invalid"
            )
        if self.fill_receipt_sha256 is not None:
            object.__setattr__(
                self,
                "fill_receipt_sha256",
                _sha(
                    self.fill_receipt_sha256,
                    field_name="outcome_fill_receipt_sha256",
                ),
            )


class CapturedPaperTransportCoordinator:
    """One-POST initial owner plus positive-only same-CID reconciliation."""

    def __init__(
        self,
        *,
        store: CapturedPaperTransactionStore,
        broker_transport: CapturedPaperBrokerTransport,
        financial_breaker_issuer: CapturedPaperFinancialBreakerIssuer,
        acceptance_recorder: CapturedPaperPositiveAcceptanceRecorder,
        fill_capture: CapturedPaperFillCapture | None,
        assert_external_authority_current: Callable[[], None],
    ) -> None:
        required = {
            "store": (
                store,
                (
                    "load_instruction",
                    "verify_committed_instruction",
                    "next_due_initial_instruction",
                    "next_due_reconciliation_instruction",
                    "lease_initial",
                    "start_transport",
                    "authorize_transport_invocation",
                    "record_financial_breaker_authority",
                    "consume_dispatch_authority",
                    "acquire_dispatch_linearization",
                    "mark_transport_indeterminate",
                    "complete_direct_acceptance",
                    "lease_reconciliation",
                    "mark_reconciliation_pending",
                    "complete_reconciliation_acceptance",
                ),
            ),
            "broker_transport": (
                broker_transport,
                (
                    "preflight",
                    "prepare_limit_buy",
                    "post_limit_buy",
                    "lookup_same_cid",
                ),
            ),
            "acceptance_recorder": (
                acceptance_recorder,
                ("persist_positive_acceptance",),
            ),
            "financial_breaker_issuer": (
                financial_breaker_issuer,
                ("issue_for_request",),
            ),
        }
        for component_name, (component, method_names) in required.items():
            if any(
                not callable(getattr(component, method_name, None))
                for method_name in method_names
            ):
                raise CapturedPaperTransportContractError(
                    f"{component_name}_capability_unavailable"
                )
        if fill_capture is not None and any(
            not callable(getattr(fill_capture, method_name, None))
            for method_name in ("read_exact_order_fills", "append_fill_read")
        ):
            raise CapturedPaperTransportContractError(
                "fill_capture_capability_unavailable"
            )
        if not callable(assert_external_authority_current):
            raise CapturedPaperTransportContractError(
                "transport_external_authority_guard_invalid"
            )
        self._store = store
        self._broker = broker_transport
        self._financial_breaker = financial_breaker_issuer
        self._acceptance = acceptance_recorder
        self._fill = fill_capture
        self._assert_external_authority_current = (
            assert_external_authority_current
        )

    @staticmethod
    def _exception_evidence(
        instruction: CapturedPaperTransportInstruction,
        *,
        phase: str,
        exc: Exception,
    ) -> str:
        return _sha256_json(
            {
                "schema_version": TRANSPORT_OBSERVATION_SCHEMA_VERSION,
                "observation_kind": "local_exception",
                "phase": phase,
                "exception_type": type(exc).__name__,
                "contract_reason": (
                    exc.reason
                    if isinstance(exc, CapturedPaperTransportError)
                    else None
                ),
                "financial_breaker_receipt_sha256": getattr(
                    exc,
                    "financial_breaker_receipt_sha256",
                    None,
                ),
                "financial_breaker_evidence_sha256": getattr(
                    exc,
                    "financial_breaker_evidence_sha256",
                    None,
                ),
                "financial_breaker_blocker": getattr(
                    exc,
                    "financial_breaker_blocker",
                    None,
                ),
                "instruction_sha256": instruction.instruction_sha256,
                "completion_sha256": instruction.request.completion_sha256,
                "client_order_id": instruction.client_order_id,
            }
        )

    def _capture_fills(
        self,
        instruction: CapturedPaperTransportInstruction,
        observation: (
            CapturedPaperPositiveOrderObservation
            | CapturedPaperFillReconciliationRequiredObservation
        ),
    ) -> tuple[str, str | None]:
        if self._fill is None:
            return "coverage_unavailable", None
        try:
            # This read is intentionally outside every store/acceptance tx.
            read = self._fill.read_exact_order_fills(instruction, observation)
            if type(read) is not CapturedPaperFillReadAuthority:
                raise CapturedPaperTransportContractError(
                    "fill_read_authority_type_invalid"
                )
            if not (
                read.account_scope == instruction.account_scope
                and read.expected_account_id == instruction.expected_account_id
                and read.reservation_id == instruction.authority.reservation_id
                and read.client_order_id == instruction.client_order_id
                and read.broker_order_id == observation.broker_order_id
            ):
                raise CapturedPaperTransportContractError(
                    "fill_read_authority_lineage_mismatch"
                )
            # The implementation of this method owns a later migration-340
            # append transaction.  The network read object is immutable input.
            receipt = self._fill.append_fill_read(
                read,
                instruction=instruction,
                fill_handoff_required=read.positive_fill_observed,
            )
            if type(receipt) is not CapturedPaperFillAppendReceipt:
                raise CapturedPaperTransportContractError(
                    "fill_append_receipt_type_invalid"
                )
            if receipt.observation_sha256 != read.observation_sha256:
                raise CapturedPaperTransportContractError(
                    "fill_append_observation_mismatch"
                )
            fill_handoff_required = read.positive_fill_observed
            if receipt.positive_fill_handoff_committed != fill_handoff_required:
                raise CapturedPaperTransportContractError(
                    "fill_append_handoff_commit_mismatch"
                )
        except Exception:
            return "coverage_unavailable", None
        if receipt.positive_fill_handoff_committed:
            return (
                "fill_handoff_committed",
                receipt.outbox_fill_handoff_receipt_sha256,
            )
        return "durably_appended", receipt.durable_receipt_sha256

    def submit_once(
        self,
        admission: CommittedCapturedPaperAdmission,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperTransportOutcome:
        proposed = CapturedPaperTransportInstruction.from_admission(admission)
        instruction = self._store.verify_committed_instruction(proposed)
        return self._submit_instruction_once(
            instruction,
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )

    def _submit_instruction_once(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperTransportOutcome:
        # Exact PAPER/account/instruction binding is checked before a lease is
        # consumed.  Implementations must not perform network I/O in preflight.
        self._broker.preflight(instruction)
        # A host revocation published before fresh work leaves the durable row
        # untouched. Reconciliation has its own path and remains available.
        self._assert_external_authority_current()
        lease = self._store.lease_initial(
            instruction,
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return CapturedPaperTransportOutcome(
                status="no_work",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
            )
        start = self._store.start_transport(instruction, lease)
        try:
            invocation_authority = (
                self._store.authorize_transport_invocation(
                    instruction, start
                )
            )
            if type(invocation_authority) is not (
                CapturedPaperTransportInvocationAuthority
            ):
                raise CapturedPaperTransportContractError(
                    "transport_invocation_authority_type_invalid"
                )
            invocation_authority.verify_for(
                instruction.authority,
                transport_instruction_sha256=instruction.instruction_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
            )
            if invocation_authority.transport_started_at != start.started_at:
                raise CapturedPaperTransportContractError(
                    "transport_invocation_start_time_mismatch"
                )
        except Exception as exc:
            evidence = self._exception_evidence(
                instruction,
                phase="pre_invocation_authority",
                exc=exc,
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        try:
            # Refresh exact PAPER account/generation truth first.  The final
            # financial breaker is intentionally evaluated *after* this
            # external read so a kill/daily-loss change during refresh cannot
            # inherit an earlier still-fresh allowed receipt.
            pre_dispatch_evidence = self._broker.prepare_limit_buy(
                instruction,
                invocation_authority=invocation_authority,
            )
            if type(pre_dispatch_evidence) is not (
                CapturedPaperTransportPreDispatchEvidence
            ):
                raise CapturedPaperTransportContractError(
                    "transport_pre_dispatch_evidence_type_invalid"
                )
        except Exception as exc:
            evidence = self._exception_evidence(
                instruction,
                phase="pre_dispatch_broker_account",
                exc=exc,
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        financial_receipt: CapturedPaperFinancialBreakerReceipt | None = None
        try:
            financial_receipt = self._financial_breaker.issue_for_request(
                instruction.request,
                phase="pre_post",
                transport_instruction_sha256=instruction.instruction_sha256,
                transport_invocation_authority_sha256=(
                    invocation_authority.invocation_authority_sha256
                ),
                authority_valid_until=invocation_authority.valid_until,
            )
            if type(financial_receipt) is not (
                CapturedPaperFinancialBreakerReceipt
            ):
                raise CapturedPaperFinancialBreakerError(
                    "financial_breaker_receipt_type_invalid"
                )
            financial_receipt.verify_for_request(
                instruction.request,
                phase="pre_post",
                now=financial_receipt.issued_at,
                require_allowed=False,
                transport_instruction_sha256=instruction.instruction_sha256,
                transport_invocation_authority_sha256=(
                    invocation_authority.invocation_authority_sha256
                ),
            )
            financial_receipt = (
                self._store.record_financial_breaker_authority(
                    instruction,
                    start,
                    invocation_authority,
                    financial_receipt,
                )
            )
            if not financial_receipt.allowed:
                raise _CapturedPaperTransportFinancialBreakerError(
                    financial_receipt.reason
                    or "transport_financial_breaker_denied",
                    receipt=financial_receipt,
                )
        except Exception as exc:
            wrapped = (
                exc
                if isinstance(
                    exc, _CapturedPaperTransportFinancialBreakerError
                )
                else _CapturedPaperTransportFinancialBreakerError(
                    "transport_financial_breaker_authority_unavailable",
                    receipt=financial_receipt,
                )
            )
            evidence = self._exception_evidence(
                instruction,
                phase="pre_post_financial_breaker",
                exc=wrapped,
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        try:
            # Close revocation that races the broker/account + financial reads
            # before any irreversible dispatch authority is consumed.
            self._assert_external_authority_current()
            dispatch_authority = self._store.consume_dispatch_authority(
                instruction,
                start,
                invocation_authority,
                financial_receipt,
                pre_dispatch_evidence,
            )
            if type(dispatch_authority) is not (
                CapturedPaperTransportDispatchAuthority
            ):
                raise CapturedPaperTransportContractError(
                    "transport_dispatch_authority_type_invalid"
                )
        except Exception as exc:
            evidence = self._exception_evidence(
                instruction,
                phase="final_pre_dispatch_authority",
                exc=exc,
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        try:
            # The consumed event is a prepared one-shot fence, not permission
            # to trust stale DB state forever.  Enter the store's live
            # linearization context, which commits a fresh canonical lock walk
            # and then retains only PostgreSQL session advisory locks while the
            # exact synchronous POST runs.  No row transaction crosses I/O.
            with self._store.acquire_dispatch_linearization(
                instruction,
                start,
                invocation_authority,
                financial_receipt,
                pre_dispatch_evidence,
                dispatch_authority,
            ):
                # A host tombstone published after the consumed DB event must
                # also win before the exact transport obtains its host lock.
                self._assert_external_authority_current()
                observation = self._broker.post_limit_buy(
                    instruction,
                    invocation_authority=invocation_authority,
                    financial_breaker_receipt=financial_receipt,
                    pre_dispatch_evidence=pre_dispatch_evidence,
                    dispatch_authority=dispatch_authority,
                )
        except Exception as exc:
            evidence = self._exception_evidence(
                instruction, phase="direct_post", exc=exc
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        if type(observation) is CapturedPaperUnresolvedObservation:
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=observation.evidence_sha256
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=observation.evidence_sha256,
            )
        if type(observation) is CapturedPaperFillReconciliationRequiredObservation:
            observation.verify_for_instruction(instruction)
            self._store.mark_transport_indeterminate(
                start,
                evidence_sha256=observation.broker_order_evidence_sha256,
            )
            fill_status, fill_receipt = self._capture_fills(
                instruction, observation
            )
            return CapturedPaperTransportOutcome(
                status="fill_reconciliation_required",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                broker_order_id=observation.broker_order_id,
                evidence_sha256=observation.broker_order_evidence_sha256,
                fill_status=fill_status,
                fill_receipt_sha256=fill_receipt,
            )
        if type(observation) is not CapturedPaperPositiveOrderObservation:
            evidence = _sha256_json(
                {
                    "schema_version": TRANSPORT_OBSERVATION_SCHEMA_VERSION,
                    "observation_kind": "invalid_transport_result",
                    "result_type": type(observation).__name__,
                    "instruction_sha256": instruction.instruction_sha256,
                }
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        observation.verify_for_instruction(instruction)
        try:
            acceptance = self._acceptance.persist_positive_acceptance(
                instruction,
                observation,
                acceptance_kind="post_response",
            )
            acceptance.verify_for_authority(
                instruction.authority, expected_kind="post_response"
            )
            self._store.complete_direct_acceptance(
                instruction, start, acceptance
            )
        except Exception as exc:
            # A positive response whose durable adoption is not confirmed stays
            # reconciliation-only.  This path never invokes POST again.
            evidence = self._exception_evidence(
                instruction, phase="direct_acceptance_persistence", exc=exc
            )
            self._store.mark_transport_indeterminate(
                start, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="transport_indeterminate",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                broker_order_id=observation.broker_order_id,
                evidence_sha256=evidence,
            )
        fill_status, fill_receipt = self._capture_fills(
            instruction, observation
        )
        return CapturedPaperTransportOutcome(
            status="accepted",
            completion_sha256=instruction.request.completion_sha256,
            client_order_id=instruction.client_order_id,
            broker_order_id=observation.broker_order_id,
            evidence_sha256=observation.broker_order_evidence_sha256,
            fill_status=fill_status,
            fill_receipt_sha256=fill_receipt,
        )

    def submit_next_due_after_restart(
        self,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperTransportOutcome | None:
        """Resume an exact phase-one row; only marker-free work may POST."""

        instruction = self._store.next_due_initial_instruction()
        if instruction is None:
            return None
        return self._submit_instruction_once(
            instruction,
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )

    def reconcile_once(
        self,
        admission: CommittedCapturedPaperAdmission,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperTransportOutcome:
        proposed = CapturedPaperTransportInstruction.from_admission(admission)
        instruction = self._store.verify_committed_instruction(proposed)
        return self._reconcile_instruction_once(
            instruction,
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )

    def _reconcile_instruction_once(
        self,
        instruction: CapturedPaperTransportInstruction,
        *,
        lease_owner_id: str,
        lease_seconds: int,
    ) -> CapturedPaperTransportOutcome:
        self._broker.preflight(instruction)
        lease = self._store.lease_reconciliation(
            instruction,
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return CapturedPaperTransportOutcome(
                status="no_work",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
            )
        try:
            observation = self._broker.lookup_same_cid(instruction)
        except Exception as exc:
            evidence = self._exception_evidence(
                instruction, phase="same_cid_lookup", exc=exc
            )
            self._store.mark_reconciliation_pending(
                lease, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="reconciliation_pending",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        if type(observation) is CapturedPaperUnresolvedObservation:
            # Explicit CID absence and unreadability are both nonterminal.
            self._store.mark_reconciliation_pending(
                lease, evidence_sha256=observation.evidence_sha256
            )
            return CapturedPaperTransportOutcome(
                status="reconciliation_pending",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=observation.evidence_sha256,
            )
        if type(observation) is CapturedPaperFillReconciliationRequiredObservation:
            observation.verify_for_instruction(instruction)
            self._store.mark_reconciliation_pending(
                lease,
                evidence_sha256=observation.broker_order_evidence_sha256,
            )
            fill_status, fill_receipt = self._capture_fills(
                instruction, observation
            )
            return CapturedPaperTransportOutcome(
                status="fill_reconciliation_required",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                broker_order_id=observation.broker_order_id,
                evidence_sha256=observation.broker_order_evidence_sha256,
                fill_status=fill_status,
                fill_receipt_sha256=fill_receipt,
            )
        if type(observation) is not CapturedPaperPositiveOrderObservation:
            evidence = _sha256_json(
                {
                    "schema_version": TRANSPORT_OBSERVATION_SCHEMA_VERSION,
                    "observation_kind": "invalid_reconciliation_result",
                    "result_type": type(observation).__name__,
                    "instruction_sha256": instruction.instruction_sha256,
                }
            )
            self._store.mark_reconciliation_pending(
                lease, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="reconciliation_pending",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                evidence_sha256=evidence,
            )
        observation.verify_for_instruction(instruction)
        try:
            acceptance = self._acceptance.persist_positive_acceptance(
                instruction,
                observation,
                acceptance_kind="same_cid_reconciliation",
            )
            acceptance.verify_for_authority(
                instruction.authority,
                expected_kind="same_cid_reconciliation",
            )
            self._store.complete_reconciliation_acceptance(
                instruction, lease, acceptance
            )
        except Exception as exc:
            evidence = self._exception_evidence(
                instruction,
                phase="reconciliation_acceptance_persistence",
                exc=exc,
            )
            self._store.mark_reconciliation_pending(
                lease, evidence_sha256=evidence
            )
            return CapturedPaperTransportOutcome(
                status="reconciliation_pending",
                completion_sha256=instruction.request.completion_sha256,
                client_order_id=instruction.client_order_id,
                broker_order_id=observation.broker_order_id,
                evidence_sha256=evidence,
            )
        fill_status, fill_receipt = self._capture_fills(
            instruction, observation
        )
        return CapturedPaperTransportOutcome(
            status="accepted",
            completion_sha256=instruction.request.completion_sha256,
            client_order_id=instruction.client_order_id,
            broker_order_id=observation.broker_order_id,
            evidence_sha256=observation.broker_order_evidence_sha256,
            fill_status=fill_status,
            fill_receipt_sha256=fill_receipt,
        )

    def reconcile_next_due_after_restart(
        self,
        *,
        lease_owner_id: str,
        lease_seconds: int,
        recovery_limit: int,
    ) -> CapturedPaperTransportOutcome | None:
        """Recover expired fences and perform one same-CID lookup only."""

        instruction = self._store.next_due_reconciliation_instruction(
            recovery_limit=recovery_limit,
        )
        if instruction is None:
            return None
        return self._reconcile_instruction_once(
            instruction,
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )

    def resume_restart_once(
        self,
        *,
        lease_owner_id: str,
        lease_seconds: int,
        recovery_limit: int,
    ) -> CapturedPaperTransportOutcome | None:
        """Prefer unresolved broker truth before consuming any fresh POST."""

        reconciled = self.reconcile_next_due_after_restart(
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
            recovery_limit=recovery_limit,
        )
        if reconciled is not None:
            return reconciled
        return self.submit_next_due_after_restart(
            lease_owner_id=lease_owner_id,
            lease_seconds=lease_seconds,
        )


__all__ = (
    "CapturedPaperBrokerTransport",
    "CapturedPaperCommittedLease",
    "CapturedPaperFillAppendReceipt",
    "CapturedPaperFillCapture",
    "CapturedPaperFillReconciliationRequiredObservation",
    "CapturedPaperFillReadAuthority",
    "CapturedPaperExactBrokerOrderObservation",
    "CapturedPaperPositiveAcceptanceRecorder",
    "CapturedPaperPositiveOrderObservation",
    "CapturedPaperTerminalZeroFillObservation",
    "CapturedPaperTransactionStore",
    "CapturedPaperTransportContractError",
    "CapturedPaperTransportCoordinator",
    "CapturedPaperTransportDispatchAuthority",
    "CapturedPaperTransportError",
    "CapturedPaperTransportInstruction",
    "CapturedPaperTransportInvocationAuthority",
    "CapturedPaperTransportOutcome",
    "CapturedPaperTransportStart",
    "CapturedPaperTransportPreDispatchEvidence",
    "CapturedPaperTransportUnavailable",
    "CapturedPaperUnresolvedObservation",
    "EXACT_PAPER_ACCOUNT_BINDING_SOURCE",
    "ExactAlpacaPaperEntryTransport",
    "SqlAlchemyCapturedPaperTransportStore",
)
