"""Immutable Alpaca PAPER fill-activity capture for adaptive cycles.

The low-level prepare/verify/append helpers remain broker-inert.  The exact
production boundary is split deliberately: ``read_verified_alpaca_paper_fill_batch``
performs broker I/O without a database session, while
``append_prepared_alpaca_paper_fill_batch`` publishes the immutable handoff
without an adapter or network seam. The older combined entry point is disabled;
it cannot safely preserve this transaction/I/O split or durable exit ownership.

The current official ``TradeActivity`` contract supplies ``id``, ``account_id``,
``transaction_time``, ``price``, ``qty``, ``side``, ``symbol``, ``leaves_qty``,
``order_id``, ``cum_qty`` and ``order_status``.  It does *not* supply a separate
event clock, client-order id, or per-fill fee.  Those facts therefore have
explicit ``provider_unavailable`` states; this module never substitutes the
transaction clock or a numeric zero. Version 1 intentionally has no sealed
capture issuer. Caller-supplied order/event/fee mappings are persisted only as
``unverified_mapping`` and can never make settlement complete.  Version 2
accepts only an exact PAPER order/activity batch from that pinned read seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, Any, Mapping, Sequence
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
    AdaptiveRiskReservationEvent,
    AlpacaPaperFillActivity,
    AlpacaPaperFillObservationActivity,
    AlpacaPaperFillObservationPage,
    AlpacaPaperFillPageObject,
    AlpacaPaperFillQueryObservation,
    AlpacaPaperCycleSettlement,
    AlpacaPaperPostSettlementFillContradiction,
    AlpacaPaperTerminalFillObservationReceipt,
)
from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .adaptive_risk_account_lock import acquire_adaptive_risk_account_locks
from .alpaca_fill_read_capability import (
    AlpacaFillReadCapability,
    AlpacaFillReadCapabilityError,
    verify_alpaca_fill_read_capability,
)

if TYPE_CHECKING:
    from .adaptive_risk_reservation import AdaptiveReservationState


UTC = timezone.utc
CAPTURE_SCHEMA_VERSION = "chili.alpaca-paper-fill-activity.v1"
AUTHORITATIVE_CAPTURE_SCHEMA_VERSION = "chili.alpaca-paper-fill-activity.v2"
PREPARED_FILL_BATCH_SCHEMA_VERSION = "chili.alpaca-paper-fill-batch.v2"
FILL_QUERY_RECEIPT_SCHEMA_VERSION = "chili.alpaca-paper-fill-query-receipt.v1"
FILL_READ_CAPABILITY_SCHEMA_VERSION = "chili.alpaca-paper-fill-read-capability.v1"
FILL_READ_BINDING_SCHEMA_VERSION = "chili.alpaca-paper-fill-read-binding.v1"
POST_SETTLEMENT_FILL_CONTRADICTION_SCHEMA_VERSION = (
    "chili.alpaca-paper-post-settlement-fill-contradiction.v1"
)
POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND = (
    "committed_alpaca_post_settlement_fill_contradiction"
)
ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION = (
    "chili.alpaca-paper-entry-fill-handoff.v1"
)
_MONEY_QUANTUM = Decimal("0.0000000001")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNAVAILABLE = "provider_unavailable"
_UNVERIFIED_MAPPING = "unverified_mapping"
_CAPTURE_UNVERIFIED = "unverified"
_ENTRY_ORDER_BOUND = "reservation_bound"
_ORDER_UNVERIFIED = "unverified"
_AUTHORITATIVE = "authoritative"
_CAPTURE_VERIFIED = "verified"


class AlpacaFillActivityError(RuntimeError):
    """Base class for a fail-closed capture contract violation."""


class AlpacaFillActivityConflict(AlpacaFillActivityError):
    """A provider id or cycle identity was reused for different content."""


class AlpacaFillActivityCorruption(AlpacaFillActivityError):
    """Already-durable canonical text/hash/lineage no longer verifies."""


class AlpacaFillObservationPersistenceUnavailable(AlpacaFillActivityError):
    """A distinct broker observation cannot yet be retained without loss."""


class AlpacaPostSettlementFillRequired(AlpacaFillActivityConflict):
    """A late fill must use the separate settled-cycle contradiction ledger."""


def _required_text(
    value: Any,
    field: str,
    *,
    lower: bool = False,
    upper: bool = False,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise AlpacaFillActivityError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise AlpacaFillActivityError(f"{field} is required")
    if lower:
        normalized = normalized.lower()
    if upper:
        normalized = normalized.upper()
    if max_length is not None and len(normalized) > max_length:
        raise AlpacaFillActivityError(f"{field} exceeds {max_length} characters")
    return normalized


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha(value: Any, field: str) -> str:
    normalized = _required_text(value, field, lower=True)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise AlpacaFillActivityError(f"{field} must be lowercase SHA-256")
    return normalized


def _json_value(value: Any, field: str) -> Any:
    """Copy a value while rejecting lossy/non-JSON SDK representations."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AlpacaFillActivityError(f"{field} contains non-finite JSON")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{field}[]") for item in value]
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AlpacaFillActivityError(
                    f"{field} contains a non-string object key"
                )
            copied[key] = _json_value(item, f"{field}.{key}")
        return copied
    raise AlpacaFillActivityError(
        f"{field} must already be JSON-compatible; serialize SDK models in JSON mode"
    )


def _canonical_json_text(value: Mapping[str, Any], field: str) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise AlpacaFillActivityError(f"{field} must be a JSON object")
    copied = _json_value(value, field)
    try:
        canonical = json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        reparsed = json.loads(canonical)
        recanonical = json.dumps(
            reparsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AlpacaFillActivityError(f"{field} is not canonical JSON") from exc
    if recanonical != canonical:
        raise AlpacaFillActivityError(f"{field} failed canonical JSON reparse")
    return canonical, _sha256_text(canonical)


def _reparse_canonical_json(
    canonical: Any,
    expected_sha256: Any,
    field: str,
) -> dict[str, Any]:
    if not isinstance(canonical, str) or not canonical:
        raise AlpacaFillActivityCorruption(f"{field} canonical JSON is missing")
    expected = _require_sha(expected_sha256, f"{field}_sha256")
    if _sha256_text(canonical) != expected:
        raise AlpacaFillActivityCorruption(f"{field} content hash mismatch")
    try:
        parsed = json.loads(canonical)
    except json.JSONDecodeError as exc:
        raise AlpacaFillActivityCorruption(f"{field} does not reparse") from exc
    if not isinstance(parsed, dict):
        raise AlpacaFillActivityCorruption(f"{field} is not a JSON object")
    recanonical, reparsed_sha = _canonical_json_text(parsed, field)
    if recanonical != canonical or reparsed_sha != expected:
        raise AlpacaFillActivityCorruption(
            f"{field} changed during canonical reparse"
        )
    return parsed


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlpacaFillActivityError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _provider_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise AlpacaFillActivityError(f"{field} must be an ISO-8601 string")
    raw = value.strip()
    if not raw:
        raise AlpacaFillActivityError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlpacaFillActivityError(f"{field} is not ISO-8601") from exc
    return _utc(parsed, field)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="microseconds")


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AlpacaFillActivityError(f"{field} must be a finite decimal")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlpacaFillActivityError(f"{field} must be a finite decimal") from exc
    if not normalized.is_finite():
        raise AlpacaFillActivityError(f"{field} must be a finite decimal")
    try:
        quantized = normalized.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise AlpacaFillActivityError(f"{field} exceeds supported precision") from exc
    if quantized != normalized:
        raise AlpacaFillActivityError(
            f"{field} exceeds the exact 10-decimal storage precision"
        )
    if positive and quantized <= 0:
        raise AlpacaFillActivityError(f"{field} must be positive")
    return quantized


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(_MONEY_QUANTUM), "f")


def _whole_share_quantity(value: Any, field: str, *, positive: bool = False) -> int:
    parsed = _decimal(value, field, positive=positive)
    if parsed != parsed.to_integral_value():
        raise AlpacaFillActivityError(f"{field} must be a whole-share quantity")
    return int(parsed)


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AlpacaFillActivityError(f"{field} must be a UUID") from exc


@dataclass(frozen=True)
class AlpacaPaperFillCycleBinding:
    """Immutable adaptive reservation/packet identity for one long cycle."""

    reservation_id: uuid.UUID
    decision_packet_sha256: str
    reservation_request_sha256: str
    account_scope: str
    account_identity_sha256: str
    account_snapshot_sha256: str
    account_snapshot_generation: str
    broker_connection_generation: str
    execution_family: str
    position_direction: str
    cycle_client_order_id: str
    entry_provider_order_id: str
    symbol: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reservation_id", _uuid(self.reservation_id, "reservation_id")
        )
        for field in (
            "decision_packet_sha256",
            "reservation_request_sha256",
            "account_identity_sha256",
            "account_snapshot_sha256",
        ):
            object.__setattr__(self, field, _require_sha(getattr(self, field), field))
        object.__setattr__(
            self,
            "account_scope",
            _required_text(self.account_scope, "account_scope", lower=True),
        )
        if self.account_scope != "alpaca:paper":
            raise AlpacaFillActivityError("fill cycle must be exact alpaca:paper")
        for field in (
            "account_snapshot_generation",
            "broker_connection_generation",
            "cycle_client_order_id",
            "entry_provider_order_id",
        ):
            object.__setattr__(
                self,
                field,
                _required_text(getattr(self, field), field, max_length=160),
            )
        object.__setattr__(
            self,
            "execution_family",
            _required_text(self.execution_family, "execution_family", lower=True),
        )
        object.__setattr__(
            self,
            "position_direction",
            _required_text(
                self.position_direction, "position_direction", lower=True
            ),
        )
        if self.execution_family != "alpaca_spot":
            raise AlpacaFillActivityError(
                "fill capture is scoped only to execution_family=alpaca_spot"
            )
        if self.position_direction != "long":
            raise AlpacaFillActivityError(
                "fill capture v1 is long-only; short needs a new reviewed schema"
            )
        object.__setattr__(
            self,
            "symbol",
            _required_text(self.symbol, "symbol", upper=True, max_length=36),
        )

    @classmethod
    def _validated_identity_kwargs(
        cls,
        reservation: AdaptiveRiskReservation,
        packet: AdaptiveRiskDecisionPacket,
    ) -> dict[str, Any]:
        comparisons = {
            "decision_packet_sha256": (
                reservation.decision_packet_sha256,
                packet.decision_packet_sha256,
            ),
            "account_scope": (reservation.account_scope, packet.account_scope),
            "symbol": (reservation.symbol, packet.symbol),
        }
        changed = sorted(
            name for name, (left, right) in comparisons.items() if left != right
        )
        if changed:
            raise AlpacaFillActivityConflict(
                "adaptive reservation/packet mismatch: " + ",".join(changed)
            )
        if not bool(packet.resolver_valid) or not bool(packet.admission_accepted):
            raise AlpacaFillActivityConflict(
                "a provider fill cannot bind to a rejected decision packet"
            )
        if (
            packet.execution_surface != "alpaca_paper"
            or packet.execution_family != "alpaca_spot"
            or packet.broker_environment != "paper"
        ):
            raise AlpacaFillActivityConflict(
                "decision packet is not the intended Alpaca PAPER policy"
            )
        snapshot = packet.account_snapshot_json
        if not isinstance(snapshot, Mapping):
            raise AlpacaFillActivityConflict("immutable account snapshot is missing")
        snapshot_checks = {
            "account_scope": (snapshot.get("account_scope"), packet.account_scope),
            "execution_family": (
                snapshot.get("execution_family"),
                packet.execution_family,
            ),
            "broker_environment": (
                snapshot.get("broker_environment"),
                packet.broker_environment,
            ),
            "venue": (snapshot.get("venue"), "alpaca"),
            "account_identity_sha256": (
                snapshot.get("account_identity_sha256"),
                packet.account_identity_sha256,
            ),
            "provider_generation": (
                snapshot.get("provider_generation"),
                packet.account_snapshot_generation,
            ),
            "snapshot_sha256": (
                snapshot.get("snapshot_sha256"),
                packet.account_snapshot_sha256,
            ),
        }
        snapshot_changed = sorted(
            name
            for name, (left, right) in snapshot_checks.items()
            if left != right
        )
        if snapshot_changed:
            raise AlpacaFillActivityConflict(
                "immutable account snapshot mismatch: "
                + ",".join(snapshot_changed)
            )
        return {
            "reservation_id": reservation.reservation_id,
            "decision_packet_sha256": packet.decision_packet_sha256,
            "reservation_request_sha256": packet.reservation_request_sha256,
            "account_scope": packet.account_scope,
            "account_identity_sha256": packet.account_identity_sha256,
            "account_snapshot_sha256": packet.account_snapshot_sha256,
            "account_snapshot_generation": (
                packet.account_snapshot_generation
            ),
            "execution_family": packet.execution_family,
            "position_direction": "long",
            "cycle_client_order_id": packet.client_order_id,
            "symbol": packet.symbol,
        }

    @classmethod
    def from_rows(
        cls,
        reservation: AdaptiveRiskReservation,
        packet: AdaptiveRiskDecisionPacket,
    ) -> "AlpacaPaperFillCycleBinding":
        """Rebuild a cycle only when mutable and immutable rows still agree."""

        kwargs = cls._validated_identity_kwargs(reservation, packet)
        generation = reservation.broker_connection_generation
        if not isinstance(generation, str) or not generation.strip():
            raise AlpacaFillActivityConflict(
                "reservation lacks an authoritative broker connection generation"
            )
        return cls(
            **kwargs,
            broker_connection_generation=generation,
            entry_provider_order_id=reservation.broker_order_id,
        )

    @classmethod
    def from_unbound_fill_bearing_rows(
        cls,
        reservation: AdaptiveRiskReservation,
        packet: AdaptiveRiskDecisionPacket,
        *,
        broker_connection_generation: str,
        entry_provider_order_id: str,
    ) -> "AlpacaPaperFillCycleBinding":
        """Build the prospective cycle used by one exact positive-fill read.

        This helper does not bind or mutate the reservation.  The publisher
        may stage the complete broker identity only inside the same savepoint
        that appends the verified positive fill and advances its adaptive
        watermark; any failure rolls the entire bootstrap back.
        """

        kwargs = cls._validated_identity_kwargs(reservation, packet)
        unbound_fields = {
            "broker_source": reservation.broker_source,
            "broker_connection_generation": (
                reservation.broker_connection_generation
            ),
            "broker_order_id": reservation.broker_order_id,
            "last_broker_observed_at": reservation.last_broker_observed_at,
            "last_broker_available_at": reservation.last_broker_available_at,
            "last_source_event_content_sha256": (
                reservation.last_source_event_content_sha256
            ),
        }
        if any(value is not None for value in unbound_fields.values()):
            raise AlpacaFillActivityConflict(
                "prospective fill-bearing cycle requires a fully unbound reservation"
            )
        if str(reservation.state or "").strip().lower() not in {
            "reserved",
            "submit_indeterminate",
        }:
            raise AlpacaFillActivityConflict(
                "prospective fill-bearing cycle requires a retained entry state"
            )
        zero_dimensions = {
            "cumulative_filled_quantity_shares": (
                reservation.cumulative_filled_quantity_shares
            ),
            "open_quantity_shares": reservation.open_quantity_shares,
            "open_structural_risk_usd": reservation.open_structural_risk_usd,
            "open_gross_notional_usd": reservation.open_gross_notional_usd,
            "open_buying_power_impact_usd": (
                reservation.open_buying_power_impact_usd
            ),
        }
        if any(Decimal(value) != 0 for value in zero_dimensions.values()):
            raise AlpacaFillActivityConflict(
                "prospective fill-bearing cycle already has durable exposure"
            )
        pending_pairs = (
            (
                reservation.pending_structural_risk_usd,
                reservation.planned_structural_risk_usd,
            ),
            (
                reservation.pending_gross_notional_usd,
                reservation.planned_gross_notional_usd,
            ),
            (
                reservation.pending_buying_power_impact_usd,
                reservation.planned_buying_power_impact_usd,
            ),
        )
        if any(Decimal(pending) != Decimal(planned) for pending, planned in pending_pairs):
            raise AlpacaFillActivityConflict(
                "prospective fill-bearing cycle risk reservation is incomplete"
            )
        if (
            reservation.first_fill_at is not None
            or reservation.lifecycle_contradiction_source_state is not None
            or reservation.lifecycle_contradiction_at is not None
            or reservation.lifecycle_contradiction_evidence_sha256 is not None
        ):
            raise AlpacaFillActivityConflict(
                "prospective fill-bearing cycle has prior fill/contradiction state"
            )
        return cls(
            **kwargs,
            broker_connection_generation=broker_connection_generation,
            entry_provider_order_id=entry_provider_order_id,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.reservation_id),
            "decision_packet_sha256": self.decision_packet_sha256,
            "reservation_request_sha256": self.reservation_request_sha256,
            "account_scope": self.account_scope,
            "account_identity_sha256": self.account_identity_sha256,
            "account_snapshot_sha256": self.account_snapshot_sha256,
            "account_snapshot_generation": self.account_snapshot_generation,
            "broker_connection_generation": self.broker_connection_generation,
            "execution_family": self.execution_family,
            "position_direction": self.position_direction,
            "cycle_client_order_id": self.cycle_client_order_id,
            "entry_provider_order_id": self.entry_provider_order_id,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class PreparedAlpacaPaperFillActivity:
    capture_schema_version: str
    cycle: AlpacaPaperFillCycleBinding
    capture_authority_status: str
    order_role: str
    order_ownership_status: str
    provider_activity_id: str
    provider_account_id_sha256: str
    provider_activity_type: str
    provider_trade_type: str
    provider_order_id: str
    provider_client_order_id_status: str
    provider_client_order_id: str | None
    provider_order_status: str
    side: str
    quantity: Decimal
    price: Decimal
    leaves_quantity: Decimal
    cumulative_quantity: Decimal
    fee_status: str
    fee_usd: Decimal | None
    fee_evidence_canonical_json: str | None
    fee_evidence_sha256: str | None
    provider_event_clock_status: str
    provider_event_clock_field: str | None
    provider_event_at: datetime | None
    provider_transaction_at: datetime
    received_at: datetime
    available_at: datetime
    provider_payload_canonical_json: str
    provider_payload_sha256: str
    provider_order_payload_canonical_json: str | None
    provider_order_payload_sha256: str | None
    order_binding_sha256: str

    def immutable_fill_identity_body(self) -> dict[str, Any]:
        """Provider execution identity, excluding mutable observation state.

        Alpaca can return the same immutable TradeActivity under a later query
        clock and a newer order projection/status.  Those observations are
        evidence and must be retained, but they are not a second execution and
        therefore must not change the fill's idempotency key.
        """

        return {
            "schema_version": "chili.alpaca-paper-fill-identity.v1",
            "reservation_id": str(self.cycle.reservation_id),
            "account_scope": self.cycle.account_scope,
            "account_identity_sha256": self.cycle.account_identity_sha256,
            "provider_activity_id": self.provider_activity_id,
            "provider_account_id_sha256": self.provider_account_id_sha256,
            "provider_activity_type": self.provider_activity_type,
            "provider_trade_type": self.provider_trade_type,
            "provider_order_id": self.provider_order_id,
            "provider_client_order_id": self.provider_client_order_id,
            "symbol": self.cycle.symbol,
            "side": self.side,
            "quantity": _decimal_text(self.quantity),
            "price": _decimal_text(self.price),
            "provider_transaction_at": _iso(self.provider_transaction_at),
            "fee_status": self.fee_status,
            "fee_usd": (
                _decimal_text(self.fee_usd) if self.fee_usd is not None else None
            ),
        }

    @property
    def immutable_fill_identity_sha256(self) -> str:
        _canonical, digest = _canonical_json_text(
            self.immutable_fill_identity_body(), "immutable_fill_identity"
        )
        return digest

    def content_body(self) -> dict[str, Any]:
        return {
            "capture_schema_version": self.capture_schema_version,
            "capture_authority_status": self.capture_authority_status,
            "cycle": self.cycle.to_payload(),
            "order_role": self.order_role,
            "order_ownership_status": self.order_ownership_status,
            "provider": {
                "activity_id": self.provider_activity_id,
                "account_id_sha256": self.provider_account_id_sha256,
                "activity_type": self.provider_activity_type,
                "trade_type": self.provider_trade_type,
                "order_id": self.provider_order_id,
                "client_order_id_status": self.provider_client_order_id_status,
                "client_order_id": self.provider_client_order_id,
                "order_status": self.provider_order_status,
                "side": self.side,
                "quantity": _decimal_text(self.quantity),
                "price": _decimal_text(self.price),
                "leaves_quantity": _decimal_text(self.leaves_quantity),
                "cumulative_quantity": _decimal_text(self.cumulative_quantity),
            },
            "fee": {
                "status": self.fee_status,
                "usd": (
                    _decimal_text(self.fee_usd)
                    if self.fee_usd is not None
                    else None
                ),
                "evidence_sha256": self.fee_evidence_sha256,
            },
            "clocks": {
                "provider_event_status": self.provider_event_clock_status,
                "provider_event_field": self.provider_event_clock_field,
                "provider_event_at": (
                    _iso(self.provider_event_at)
                    if self.provider_event_at is not None
                    else None
                ),
                "provider_transaction_at": _iso(self.provider_transaction_at),
                "received_at": _iso(self.received_at),
                "available_at": _iso(self.available_at),
            },
            "provider_payload_sha256": self.provider_payload_sha256,
            "provider_order_payload_sha256": self.provider_order_payload_sha256,
            "order_binding_sha256": self.order_binding_sha256,
        }

    @property
    def record_content_sha256(self) -> str:
        canonical, digest = _canonical_json_text(
            self.content_body(), "record_content"
        )
        if not canonical:
            raise AlpacaFillActivityError("record content cannot be empty")
        return digest

    def model_kwargs(
        self,
        *,
        sequence: int,
        previous_event_sha256: str | None,
    ) -> dict[str, Any]:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise AlpacaFillActivityError("sequence must be a positive integer")
        previous = previous_event_sha256
        if sequence == 1:
            if previous is not None:
                raise AlpacaFillActivityError("first fill cannot have a predecessor")
        else:
            previous = _require_sha(previous, "previous_event_sha256")
        content_sha = self.record_content_sha256
        event_body = {
            "capture_schema_version": self.capture_schema_version,
            "reservation_id": str(self.cycle.reservation_id),
            "sequence": sequence,
            "previous_event_sha256": previous,
            "record_content_sha256": content_sha,
        }
        _event_json, event_sha = _canonical_json_text(event_body, "lineage_event")
        return {
            "capture_schema_version": self.capture_schema_version,
            "capture_authority_status": self.capture_authority_status,
            "reservation_id": self.cycle.reservation_id,
            "decision_packet_sha256": self.cycle.decision_packet_sha256,
            "reservation_request_sha256": self.cycle.reservation_request_sha256,
            "account_scope": self.cycle.account_scope,
            "account_identity_sha256": self.cycle.account_identity_sha256,
            "account_snapshot_sha256": self.cycle.account_snapshot_sha256,
            "account_snapshot_generation": self.cycle.account_snapshot_generation,
            "broker_connection_generation": self.cycle.broker_connection_generation,
            "execution_family": self.cycle.execution_family,
            "position_direction": self.cycle.position_direction,
            "cycle_client_order_id": self.cycle.cycle_client_order_id,
            "entry_provider_order_id": self.cycle.entry_provider_order_id,
            "symbol": self.cycle.symbol,
            "order_role": self.order_role,
            "order_ownership_status": self.order_ownership_status,
            "provider_activity_id": self.provider_activity_id,
            "provider_account_id_sha256": self.provider_account_id_sha256,
            "provider_activity_type": self.provider_activity_type,
            "provider_trade_type": self.provider_trade_type,
            "provider_order_id": self.provider_order_id,
            "provider_client_order_id_status": (
                self.provider_client_order_id_status
            ),
            "provider_client_order_id": self.provider_client_order_id,
            "provider_order_status": self.provider_order_status,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "leaves_quantity": self.leaves_quantity,
            "cumulative_quantity": self.cumulative_quantity,
            "fee_status": self.fee_status,
            "fee_usd": self.fee_usd,
            "fee_evidence_sha256": self.fee_evidence_sha256,
            "fee_evidence_canonical_json": self.fee_evidence_canonical_json,
            "provider_event_clock_status": self.provider_event_clock_status,
            "provider_event_clock_field": self.provider_event_clock_field,
            "provider_event_at": self.provider_event_at,
            "provider_transaction_at": self.provider_transaction_at,
            "received_at": self.received_at,
            "available_at": self.available_at,
            "provider_payload_canonical_json": (
                self.provider_payload_canonical_json
            ),
            "provider_payload_sha256": self.provider_payload_sha256,
            "immutable_fill_identity_sha256": (
                self.immutable_fill_identity_sha256
                if self.capture_schema_version
                == AUTHORITATIVE_CAPTURE_SCHEMA_VERSION
                else None
            ),
            "provider_order_payload_canonical_json": (
                self.provider_order_payload_canonical_json
            ),
            "provider_order_payload_sha256": (
                self.provider_order_payload_sha256
            ),
            "order_binding_sha256": self.order_binding_sha256,
            "record_content_sha256": content_sha,
            "sequence": sequence,
            "previous_event_sha256": previous,
            "event_sha256": event_sha,
        }


@dataclass(frozen=True)
class PreparedAlpacaPaperFillBatch:
    """Immutable broker-read result passed into the database publication seam.

    The stored digest is intentionally not recomputed by ``__post_init__``.
    Publication independently rebuilds it, so a copied/replaced object whose
    typed fields changed cannot silently acquire new authority.
    """

    batch_schema_version: str
    cycle: AlpacaPaperFillCycleBinding
    provider_order_id: str
    expected_client_order_id: str
    order_role: str
    query_after: datetime
    query_until: datetime
    received_at: datetime
    available_at: datetime
    expires_at: datetime
    broker_environment: str
    asset_class: str
    provider_account_id_sha256: str
    adapter_connection_generation: str
    adapter_build_sha256: str
    provider_order_payload_canonical_json: str
    provider_order_payload_sha256: str
    query_receipt_canonical_json: str
    query_receipt_sha256: str
    read_binding_canonical_json: str
    read_binding_sha256: str
    activities: tuple[PreparedAlpacaPaperFillActivity, ...]
    batch_content_sha256: str
    read_capability: AlpacaFillReadCapability = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cycle, AlpacaPaperFillCycleBinding):
            raise AlpacaFillActivityError("prepared batch cycle binding is malformed")
        if not isinstance(self.activities, tuple):
            raise AlpacaFillActivityError(
                "prepared batch activities must be an immutable tuple"
            )
        if not all(
            isinstance(item, PreparedAlpacaPaperFillActivity)
            for item in self.activities
        ):
            raise AlpacaFillActivityError(
                "prepared batch contains a malformed activity"
            )
        _require_sha(self.provider_order_payload_sha256, "provider_order_payload_sha256")
        _require_sha(self.provider_account_id_sha256, "provider_account_id_sha256")
        _require_sha(self.adapter_build_sha256, "adapter_build_sha256")
        _require_sha(self.query_receipt_sha256, "query_receipt_sha256")
        _require_sha(self.read_binding_sha256, "read_binding_sha256")
        _require_sha(self.batch_content_sha256, "batch_content_sha256")

    def content_body(self) -> dict[str, Any]:
        return {
            "batch_schema_version": self.batch_schema_version,
            "cycle": self.cycle.to_payload(),
            "provider_order_id": self.provider_order_id,
            "expected_client_order_id": self.expected_client_order_id,
            "order_role": self.order_role,
            "query": {
                "after": _iso(self.query_after),
                "until": _iso(self.query_until),
            },
            "clocks": {
                "received_at": _iso(self.received_at),
                "available_at": _iso(self.available_at),
                "expires_at": _iso(self.expires_at),
            },
            "broker_environment": self.broker_environment,
            "asset_class": self.asset_class,
            "provider_account_id_sha256": self.provider_account_id_sha256,
            "adapter_connection_generation": self.adapter_connection_generation,
            "adapter_build_sha256": self.adapter_build_sha256,
            "provider_order_payload_sha256": self.provider_order_payload_sha256,
            "query_receipt_sha256": self.query_receipt_sha256,
            "read_binding_sha256": self.read_binding_sha256,
            "activities": [
                {
                    "provider_activity_id": item.provider_activity_id,
                    "immutable_fill_identity_sha256": (
                        item.immutable_fill_identity_sha256
                    ),
                    "observation_record_content_sha256": item.record_content_sha256,
                }
                for item in self.activities
            ],
        }

    def computed_batch_content_sha256(self) -> str:
        _canonical, digest = _canonical_json_text(
            self.content_body(), "prepared_fill_batch"
        )
        return digest

    def capability_payload(self) -> dict[str, Any]:
        return {
            "schema_version": FILL_READ_CAPABILITY_SCHEMA_VERSION,
            "broker_environment": self.broker_environment,
            "asset_class": self.asset_class,
            "provider_account_id": _provider_account_id_from_query_receipt(self),
            "provider_order_id": self.provider_order_id,
            "adapter_connection_generation": self.adapter_connection_generation,
            "adapter_build_sha256": self.adapter_build_sha256,
            "query_receipt_sha256": self.query_receipt_sha256,
            "read_binding_sha256": self.read_binding_sha256,
            # Match the exact adapter representation. The public batch stores
            # typed UTC datetimes; the private HMAC binds their original values.
            "available_at": _utc(self.available_at, "available_at").isoformat(),
            "expires_at": _utc(self.expires_at, "expires_at").isoformat(),
        }


def _provider_account_id_from_query_receipt(
    batch: PreparedAlpacaPaperFillBatch,
) -> str:
    receipt = _reparse_canonical_json(
        batch.query_receipt_canonical_json,
        batch.query_receipt_sha256,
        "query_receipt",
    )
    return _required_text(
        receipt.get("provider_account_id"), "query_receipt.provider_account_id"
    )


def _fill_read_binding(
    cycle: AlpacaPaperFillCycleBinding,
    *,
    provider_order_id: str,
    expected_client_order_id: str,
    order_role: str,
) -> dict[str, Any]:
    """Build the exact reservation/cycle scope authorized for one broker read."""

    return {
        "schema_version": FILL_READ_BINDING_SCHEMA_VERSION,
        "cycle": cycle.to_payload(),
        "provider_order_id": provider_order_id,
        "expected_client_order_id": expected_client_order_id,
        "order_role": order_role,
    }


def _verify_read_binding(batch: PreparedAlpacaPaperFillBatch) -> dict[str, Any]:
    binding = _reparse_canonical_json(
        batch.read_binding_canonical_json,
        batch.read_binding_sha256,
        "read_binding",
    )
    expected = _fill_read_binding(
        batch.cycle,
        provider_order_id=batch.provider_order_id,
        expected_client_order_id=batch.expected_client_order_id,
        order_role=batch.order_role,
    )
    expected_json, expected_sha256 = _canonical_json_text(
        expected, "expected_read_binding"
    )
    if (
        batch.read_binding_canonical_json != expected_json
        or batch.read_binding_sha256 != expected_sha256
        or binding != expected
    ):
        raise AlpacaFillActivityCorruption(
            "prepared fill read binding differs from immutable cycle/order scope"
        )
    return binding


def _verify_query_receipt(batch: PreparedAlpacaPaperFillBatch) -> dict[str, Any]:
    """Verify the full page/token/hash/terminal proof carried by a batch."""

    receipt = _reparse_canonical_json(
        batch.query_receipt_canonical_json,
        batch.query_receipt_sha256,
        "query_receipt",
    )
    fixed = {
        "schema_version": (
            receipt.get("schema_version"),
            FILL_QUERY_RECEIPT_SCHEMA_VERSION,
        ),
        "broker_environment": (
            receipt.get("broker_environment"),
            batch.broker_environment,
        ),
        "asset_class": (receipt.get("asset_class"), batch.asset_class),
        "provider_order_id": (
            receipt.get("provider_order_id"),
            batch.provider_order_id,
        ),
        "provider_order_payload_sha256": (
            receipt.get("provider_order_payload_sha256"),
            batch.provider_order_payload_sha256,
        ),
        "read_binding_sha256": (
            receipt.get("read_binding_sha256"),
            batch.read_binding_sha256,
        ),
        "adapter_connection_generation": (
            receipt.get("adapter_connection_generation"),
            batch.adapter_connection_generation,
        ),
        "adapter_build_sha256": (
            receipt.get("adapter_build_sha256"),
            batch.adapter_build_sha256,
        ),
        "method": (receipt.get("method"), "GET"),
        "path": (receipt.get("path"), "/account/activities"),
        "api_version": (receipt.get("api_version"), "v2"),
        "query_after": (receipt.get("query_after"), _iso(batch.query_after)),
        "query_until": (receipt.get("query_until"), _iso(batch.query_until)),
        "direction": (receipt.get("direction"), "asc"),
    }
    # Adapter query timestamps may use an equivalent ISO representation rather
    # than our canonical microsecond rendering. Compare typed instants here.
    fixed.pop("query_after")
    fixed.pop("query_until")
    if _provider_time(
        receipt.get("query_after"), "query_receipt.query_after"
    ) != batch.query_after or _provider_time(
        receipt.get("query_until"), "query_receipt.query_until"
    ) != batch.query_until:
        raise AlpacaFillActivityCorruption("query receipt window changed")
    changed = sorted(name for name, pair in fixed.items() if pair[0] != pair[1])
    if changed:
        raise AlpacaFillActivityCorruption(
            "query receipt binding changed: " + ",".join(changed)
        )
    provider_account_id = _required_text(
        receipt.get("provider_account_id"), "query_receipt.provider_account_id"
    )
    try:
        identity_sha256 = alpaca_paper_account_identity_sha256(provider_account_id)
    except ValueError as exc:
        raise AlpacaFillActivityCorruption(
            "query receipt PAPER account UUID is malformed"
        ) from exc
    if identity_sha256 != batch.provider_account_id_sha256:
        raise AlpacaFillActivityCorruption(
            "query receipt PAPER account identity changed"
        )

    page_size = receipt.get("page_size")
    max_pages = receipt.get("max_pages")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size <= 0
        or isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or max_pages <= 0
    ):
        raise AlpacaFillActivityCorruption("query receipt page bounds are invalid")
    pages = receipt.get("pages")
    if not isinstance(pages, list) or not pages or len(pages) > max_pages:
        raise AlpacaFillActivityCorruption("query receipt pages are incomplete")
    expected_request_token: str | None = None
    raw_count = 0
    observed_exact_hashes: list[dict[str, str]] = []
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            raise AlpacaFillActivityCorruption("query receipt page is malformed")
        if page.get("page_index") != index:
            raise AlpacaFillActivityCorruption("query receipt page index changed")
        if page.get("request_page_token") != expected_request_token:
            raise AlpacaFillActivityCorruption("query receipt page-token chain changed")
        page_requested_at = _provider_time(
            page.get("requested_at"), "query_receipt.page.requested_at"
        )
        page_received_at = _provider_time(
            page.get("received_at"), "query_receipt.page.received_at"
        )
        page_available_at = _provider_time(
            page.get("available_at"), "query_receipt.page.available_at"
        )
        if not (
            batch.query_until
            <= page_requested_at
            <= page_received_at
            <= page_available_at
            <= batch.received_at
        ):
            raise AlpacaFillActivityCorruption(
                "query receipt page clocks are not causal"
            )
        request_json = page.get("request_canonical_json")
        request_sha = _require_sha(
            page.get("request_sha256"), "query_receipt.request_sha256"
        )
        if not isinstance(request_json, str) or _sha256_text(request_json) != request_sha:
            raise AlpacaFillActivityCorruption("query receipt request hash mismatch")
        try:
            request = json.loads(request_json)
        except json.JSONDecodeError as exc:
            raise AlpacaFillActivityCorruption(
                "query receipt request does not reparse"
            ) from exc
        expected_request = {
            "activity_types": "FILL",
            "after": receipt["query_after"],
            "until": receipt["query_until"],
            "direction": "asc",
            "page_size": page_size,
        }
        if expected_request_token is not None:
            expected_request["page_token"] = expected_request_token
        request_canonical = json.dumps(
            expected_request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if request != expected_request or request_json != request_canonical:
            raise AlpacaFillActivityCorruption("query receipt request changed")

        response_json = page.get("response_canonical_json")
        response_sha = _require_sha(
            page.get("response_sha256"), "query_receipt.response_sha256"
        )
        if not isinstance(response_json, str) or _sha256_text(response_json) != response_sha:
            raise AlpacaFillActivityCorruption("query receipt page hash mismatch")
        try:
            response = json.loads(response_json)
        except json.JSONDecodeError as exc:
            raise AlpacaFillActivityCorruption(
                "query receipt page does not reparse"
            ) from exc
        response_canonical = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if (
            not isinstance(response, list)
            or any(not isinstance(item, Mapping) for item in response)
            or response_json != response_canonical
            or page.get("response_count") != len(response)
        ):
            raise AlpacaFillActivityCorruption("query receipt page content changed")
        raw_count += len(response)
        for item in response:
            if str(item.get("order_id") or "").strip() != batch.provider_order_id:
                continue
            activity_id = _required_text(
                item.get("id"), "query_receipt.response.provider_activity_id"
            )
            item_json = json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            observed_exact_hashes.append(
                {
                    "provider_activity_id": activity_id,
                    "provider_payload_sha256": _sha256_text(item_json),
                }
            )
        terminal = page.get("terminal") is True
        if index < len(pages) - 1 and terminal:
            raise AlpacaFillActivityCorruption("query receipt terminates before last page")
        if index == len(pages) - 1:
            if not terminal or len(response) >= page_size:
                raise AlpacaFillActivityCorruption(
                    "query receipt lacks short-page terminal proof"
                )
            if page.get("next_page_token") is not None:
                raise AlpacaFillActivityCorruption(
                    "terminal query page carries a successor token"
                )
        else:
            next_token = _required_text(
                page.get("next_page_token"), "query_receipt.next_page_token"
            )
            if not response or str(response[-1].get("id") or "").strip() != next_token:
                raise AlpacaFillActivityCorruption(
                    "query receipt successor token lacks page evidence"
                )
            expected_request_token = next_token

    terminal = receipt.get("terminal_proof")
    last = pages[-1]
    expected_terminal = {
        "reason": "pagination_complete_short_page",
        "pagination_complete": True,
        "scope": "pagination_only_not_fill_absence_or_economic_completeness",
        "page_count": len(pages),
        "last_request_page_token": last.get("request_page_token"),
        "last_response_sha256": last.get("response_sha256"),
        "last_response_count": last.get("response_count"),
        "last_page_terminal": True,
    }
    if terminal != expected_terminal or receipt.get("raw_activity_count") != raw_count:
        raise AlpacaFillActivityCorruption("query receipt terminal proof changed")
    exact_hashes = receipt.get("exact_activity_hashes")
    expected_exact_hashes = [
        {
            "provider_activity_id": item.provider_activity_id,
            "provider_payload_sha256": item.provider_payload_sha256,
        }
        for item in batch.activities
    ]
    if (
        not isinstance(exact_hashes, list)
        or exact_hashes != observed_exact_hashes
        or sorted(
            exact_hashes,
            key=lambda item: str(item.get("provider_activity_id") or "")
            if isinstance(item, Mapping)
            else "",
        )
        != sorted(
            expected_exact_hashes,
            key=lambda item: item["provider_activity_id"],
        )
        or receipt.get("exact_activity_count") != len(batch.activities)
    ):
        raise AlpacaFillActivityCorruption(
            "query receipt exact activity inventory changed"
        )
    return receipt


def prepare_alpaca_paper_fill_activity(
    cycle: AlpacaPaperFillCycleBinding,
    *,
    provider_activity: Mapping[str, Any],
    authoritative_provider_account_id: str | None = None,
    received_at: datetime,
    available_at: datetime,
    provider_event_clock_status: str,
    provider_event_clock_field: str | None = None,
    provider_client_order_id_status: str,
    provider_order: Mapping[str, Any] | None = None,
    fee_status: str,
    fee_usd: Any = None,
    fee_evidence: Mapping[str, Any] | None = None,
) -> PreparedAlpacaPaperFillActivity:
    """Validate and content-address one already-observed provider activity.

    All status arguments are explicit to make omissions visible at call sites.
    The function reads no wall clock, database, environment, SDK, or network.
    """

    provider_json, provider_sha = _canonical_json_text(
        provider_activity, "provider_activity"
    )
    activity = json.loads(provider_json)
    provider_activity_id = _required_text(
        activity.get("id"), "provider_activity.id", max_length=192
    )
    raw_provider_account_id = activity.get("account_id")
    if raw_provider_account_id is None or not str(raw_provider_account_id).strip():
        provider_account_id = _required_text(
            authoritative_provider_account_id,
            "authoritative_provider_account_id",
        )
    else:
        provider_account_id = _required_text(
            raw_provider_account_id, "provider_activity.account_id"
        )
        if (
            authoritative_provider_account_id is not None
            and provider_account_id
            != _required_text(
                authoritative_provider_account_id,
                "authoritative_provider_account_id",
            )
        ):
            raise AlpacaFillActivityConflict(
                "provider activity account differs from query authority"
            )
    provider_account_sha = _sha256_text(provider_account_id)
    if provider_account_sha != cycle.account_identity_sha256:
        raise AlpacaFillActivityConflict(
            "provider account id does not match immutable account identity"
        )
    activity_type = _required_text(
        activity.get("activity_type"),
        "provider_activity.activity_type",
        lower=True,
    )
    trade_type = _required_text(
        activity.get("type"), "provider_activity.type", lower=True
    )
    if activity_type != "fill" or trade_type not in {"fill", "partial_fill"}:
        raise AlpacaFillActivityError("provider activity is not a fill activity")
    provider_transaction_at = _provider_time(
        activity.get("transaction_time"),
        "provider_activity.transaction_time",
    )
    provider_order_id = _required_text(
        activity.get("order_id"), "provider_activity.order_id", max_length=160
    )
    provider_order_status = _required_text(
        activity.get("order_status"),
        "provider_activity.order_status",
        lower=True,
        max_length=48,
    )
    symbol = _required_text(
        activity.get("symbol"), "provider_activity.symbol", upper=True
    )
    if symbol != cycle.symbol:
        raise AlpacaFillActivityConflict("provider fill symbol changed cycle")
    side = _required_text(activity.get("side"), "provider_activity.side", lower=True)
    if side not in {"buy", "sell"}:
        raise AlpacaFillActivityError("provider fill side must be buy or sell")
    order_role = "entry" if side == "buy" else "exit"
    if order_role == "entry":
        if provider_order_id != cycle.entry_provider_order_id:
            raise AlpacaFillActivityConflict(
                "entry fill order id differs from reservation-owned entry order"
            )
        order_ownership_status = _ENTRY_ORDER_BOUND
    else:
        if provider_order_id == cycle.entry_provider_order_id:
            raise AlpacaFillActivityConflict(
                "exit fill cannot alias the reservation entry order id"
            )
        # No immutable exit-order ownership receipt exists in v1. Preserve the
        # broker mapping for diagnostics but never let it certify this cycle.
        order_ownership_status = _ORDER_UNVERIFIED
    quantity = _decimal(activity.get("qty"), "provider_activity.qty", positive=True)
    price = _decimal(
        activity.get("price"), "provider_activity.price", positive=True
    )
    # Official alpaca-py declares leaves_qty: float, not Optional[float]. A
    # missing/null value is therefore coverage failure, not an inferred zero.
    leaves_quantity = _decimal(
        activity.get("leaves_qty"), "provider_activity.leaves_qty"
    )
    cumulative_quantity = _decimal(
        activity.get("cum_qty"), "provider_activity.cum_qty", positive=True
    )
    if leaves_quantity < 0 or cumulative_quantity < quantity:
        raise AlpacaFillActivityError("provider fill quantities are inconsistent")

    received = _utc(received_at, "received_at")
    available = _utc(available_at, "available_at")
    if provider_transaction_at > received or received > available:
        raise AlpacaFillActivityError(
            "provider transaction/received/available clocks are not causal"
        )
    event_status = _required_text(
        provider_event_clock_status,
        "provider_event_clock_status",
        lower=True,
    )
    if event_status == _UNVERIFIED_MAPPING:
        event_field = _required_text(
            provider_event_clock_field,
            "provider_event_clock_field",
            max_length=64,
        )
        if event_field == "transaction_time":
            raise AlpacaFillActivityError(
                "transaction_time cannot proxy a distinct provider event clock"
            )
        if event_field not in activity:
            raise AlpacaFillActivityError(
                "unverified provider event field is absent from activity payload"
            )
        provider_event_at = _provider_time(
            activity[event_field], f"provider_activity.{event_field}"
        )
        if provider_event_at > received:
            raise AlpacaFillActivityError(
                "provider event clock cannot postdate local receipt"
            )
    elif event_status == _UNAVAILABLE:
        if provider_event_clock_field is not None:
            raise AlpacaFillActivityError(
                "unavailable provider event clock cannot name a source field"
            )
        event_field = None
        provider_event_at = None
    else:
        raise AlpacaFillActivityError(
            "provider_event_clock_status must be unverified_mapping or "
            "provider_unavailable"
        )

    cid_status = _required_text(
        provider_client_order_id_status,
        "provider_client_order_id_status",
        lower=True,
    )
    if cid_status == _UNVERIFIED_MAPPING:
        if provider_order is None:
            raise AlpacaFillActivityError(
                "unverified client-order id requires provider order mapping"
            )
        provider_order_json, provider_order_sha = _canonical_json_text(
            provider_order, "provider_order"
        )
        order = json.loads(provider_order_json)
        if _required_text(order.get("id"), "provider_order.id") != provider_order_id:
            raise AlpacaFillActivityConflict(
                "provider order payload does not own the fill order id"
            )
        provider_cid = _required_text(
            order.get("client_order_id"),
            "provider_order.client_order_id",
            max_length=160,
        )
        if order_role == "entry" and provider_cid != cycle.cycle_client_order_id:
            raise AlpacaFillActivityConflict(
                "entry provider CID differs from immutable cycle CID"
            )
        if order.get("symbol") is not None and (
            _required_text(order.get("symbol"), "provider_order.symbol", upper=True)
            != cycle.symbol
        ):
            raise AlpacaFillActivityConflict("provider order symbol mismatch")
        if order.get("side") is not None and (
            _required_text(order.get("side"), "provider_order.side", lower=True)
            != side
        ):
            raise AlpacaFillActivityConflict("provider order side mismatch")
    elif cid_status == _UNAVAILABLE:
        if provider_order is not None:
            raise AlpacaFillActivityError(
                "provider order payload cannot be discarded behind unavailable CID"
            )
        provider_order_json = None
        provider_order_sha = None
        provider_cid = None
    else:
        raise AlpacaFillActivityError(
            "provider_client_order_id_status must be unverified_mapping or "
            "provider_unavailable"
        )

    normalized_fee_status = _required_text(fee_status, "fee_status", lower=True)
    if normalized_fee_status == _UNVERIFIED_MAPPING:
        normalized_fee = _decimal(fee_usd, "fee_usd")
        if normalized_fee < 0:
            raise AlpacaFillActivityError("fee_usd must be a non-negative cost")
        if fee_evidence is None:
            raise AlpacaFillActivityError(
                "unverified fee requires its exact caller-supplied evidence mapping"
            )
        fee_json, fee_sha = _canonical_json_text(fee_evidence, "fee_evidence")
        fee_payload = json.loads(fee_json)
        fee_checks = {
            "provider_activity_id": (
                fee_payload.get("provider_activity_id"),
                provider_activity_id,
            ),
            "provider_order_id": (
                fee_payload.get("provider_order_id"),
                provider_order_id,
            ),
            "currency": (
                str(fee_payload.get("currency") or "").strip().upper(),
                "USD",
            ),
        }
        bad_fee_fields = sorted(
            name for name, (left, right) in fee_checks.items() if left != right
        )
        try:
            evidence_fee = _decimal(fee_payload.get("fee_usd"), "fee_evidence.fee_usd")
        except AlpacaFillActivityError:
            bad_fee_fields.append("fee_usd")
            evidence_fee = None
        if evidence_fee != normalized_fee:
            bad_fee_fields.append("fee_usd")
        if bad_fee_fields:
            raise AlpacaFillActivityConflict(
                "fee evidence mismatch: " + ",".join(sorted(set(bad_fee_fields)))
            )
    elif normalized_fee_status == _UNAVAILABLE:
        if fee_usd is not None or fee_evidence is not None:
            raise AlpacaFillActivityError(
                "provider-unavailable fee must remain NULL, never zero"
            )
        normalized_fee = None
        fee_json = None
        fee_sha = None
    else:
        raise AlpacaFillActivityError(
            "fee_status must be unverified_mapping or provider_unavailable"
        )

    order_binding_body = {
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_authority_status": _CAPTURE_UNVERIFIED,
        "reservation_id": str(cycle.reservation_id),
        "decision_packet_sha256": cycle.decision_packet_sha256,
        "account_scope": cycle.account_scope,
        "account_identity_sha256": cycle.account_identity_sha256,
        "broker_connection_generation": cycle.broker_connection_generation,
        "cycle_client_order_id": cycle.cycle_client_order_id,
        "symbol": cycle.symbol,
        "order_role": order_role,
        "order_ownership_status": order_ownership_status,
        "entry_provider_order_id": cycle.entry_provider_order_id,
        "provider_order_id": provider_order_id,
        "provider_client_order_id_status": cid_status,
        "provider_client_order_id": provider_cid,
        "provider_order_payload_sha256": provider_order_sha,
    }
    _binding_json, order_binding_sha = _canonical_json_text(
        order_binding_body, "order_binding"
    )
    return PreparedAlpacaPaperFillActivity(
        capture_schema_version=CAPTURE_SCHEMA_VERSION,
        cycle=cycle,
        capture_authority_status=_CAPTURE_UNVERIFIED,
        order_role=order_role,
        order_ownership_status=order_ownership_status,
        provider_activity_id=provider_activity_id,
        provider_account_id_sha256=provider_account_sha,
        provider_activity_type=activity_type,
        provider_trade_type=trade_type,
        provider_order_id=provider_order_id,
        provider_client_order_id_status=cid_status,
        provider_client_order_id=provider_cid,
        provider_order_status=provider_order_status,
        side=side,
        quantity=quantity,
        price=price,
        leaves_quantity=leaves_quantity,
        cumulative_quantity=cumulative_quantity,
        fee_status=normalized_fee_status,
        fee_usd=normalized_fee,
        fee_evidence_canonical_json=fee_json,
        fee_evidence_sha256=fee_sha,
        provider_event_clock_status=event_status,
        provider_event_clock_field=event_field,
        provider_event_at=provider_event_at,
        provider_transaction_at=provider_transaction_at,
        received_at=received,
        available_at=available,
        provider_payload_canonical_json=provider_json,
        provider_payload_sha256=provider_sha,
        provider_order_payload_canonical_json=provider_order_json,
        provider_order_payload_sha256=provider_order_sha,
        order_binding_sha256=order_binding_sha,
    )


def prepare_verified_alpaca_paper_fill_activity(
    cycle: AlpacaPaperFillCycleBinding,
    *,
    provider_activity: Mapping[str, Any],
    provider_order: Mapping[str, Any],
    authoritative_provider_account_id: str | None = None,
    received_at: datetime,
    available_at: datetime,
    expected_exit_client_order_id: str | None = None,
    fee_usd: Any,
    fee_evidence: Mapping[str, Any],
) -> PreparedAlpacaPaperFillActivity:
    """Prepare one exact Alpaca PAPER activity from the broker-read seam.

    V2 is deliberately narrower than the diagnostic V1 constructor.  The
    activity's documented ``transaction_time`` is the execution clock, the
    exact provider order supplies the client id, and the PAPER fee contract is
    retained as a content-addressed evidence mapping.  An exit additionally
    requires the caller's already-verified durable owner CID; an arbitrary sell
    order can therefore never be promoted merely because it is in the same
    account and symbol.
    """

    raw_provider_account_id = provider_activity.get("account_id")
    if raw_provider_account_id is None or not str(raw_provider_account_id).strip():
        provider_account_id = _required_text(
            authoritative_provider_account_id,
            "authoritative_provider_account_id",
        )
    else:
        provider_account_id = _required_text(
            raw_provider_account_id,
            "provider_activity.account_id",
        )
        if (
            authoritative_provider_account_id is not None
            and provider_account_id
            != _required_text(
                authoritative_provider_account_id,
                "authoritative_provider_account_id",
            )
        ):
            raise AlpacaFillActivityConflict(
                "provider activity account differs from query authority"
            )
    try:
        provider_account_identity_sha256 = (
            alpaca_paper_account_identity_sha256(provider_account_id)
        )
    except ValueError as exc:
        raise AlpacaFillActivityConflict(
            "provider Alpaca PAPER account UUID is malformed"
        ) from exc
    if provider_account_identity_sha256 != cycle.account_identity_sha256:
        raise AlpacaFillActivityConflict(
            "provider account id does not match immutable PAPER identity"
        )
    if _required_text(
        provider_order.get("asset_class"),
        "provider_order.asset_class",
        lower=True,
    ) != "us_equity":
        raise AlpacaFillActivityError(
            "authoritative zero-fee fill is PAPER US-equity-only"
        )
    if provider_order.get("account_id") is not None and _required_text(
        provider_order.get("account_id"), "provider_order.account_id"
    ) != provider_account_id:
        raise AlpacaFillActivityConflict(
            "provider order account differs from fill account"
        )
    verified_fee = _decimal(fee_usd, "fee_usd")
    if verified_fee != Decimal("0"):
        raise AlpacaFillActivityError(
            "Alpaca PAPER US-equity fee contract requires exact zero"
        )
    if not isinstance(fee_evidence, Mapping):
        raise AlpacaFillActivityError("authoritative PAPER fee evidence is missing")
    fee_contract = {
        "schema_version": "chili.alpaca-paper-equity-fee-contract.v1",
        "provider_activity_id": _required_text(
            provider_activity.get("id"), "provider_activity.id"
        ),
        "provider_order_id": _required_text(
            provider_activity.get("order_id"), "provider_activity.order_id"
        ),
        "fee_usd": "0.0000000000",
        "currency": "USD",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "basis": "alpaca_paper_does_not_account_for_regulatory_fees",
        "source": "https://docs.alpaca.markets/us/docs/paper-trading",
    }
    fee_json, _fee_sha = _canonical_json_text(fee_evidence, "fee_evidence")
    expected_fee_json, _expected_fee_sha = _canonical_json_text(
        fee_contract, "expected_fee_evidence"
    )
    if fee_json != expected_fee_json:
        raise AlpacaFillActivityConflict(
            "Alpaca PAPER US-equity fee contract receipt changed"
        )
    # V1 rows are permanently bound to their historical bare-account hash.
    # Reuse their parsing/shape validator without changing that retained
    # contract, then restore the V2 domain-separated identity before any V2
    # content/binding hash is calculated.
    legacy_parse_cycle = replace(
        cycle,
        account_identity_sha256=_sha256_text(provider_account_id),
    )
    prepared = prepare_alpaca_paper_fill_activity(
        legacy_parse_cycle,
        provider_activity=provider_activity,
        authoritative_provider_account_id=provider_account_id,
        received_at=received_at,
        available_at=available_at,
        provider_event_clock_status=_UNAVAILABLE,
        provider_client_order_id_status=_UNVERIFIED_MAPPING,
        provider_order=provider_order,
        fee_status=_UNVERIFIED_MAPPING,
        fee_usd=verified_fee,
        fee_evidence=fee_evidence,
    )
    prepared = replace(
        prepared,
        cycle=cycle,
        provider_account_id_sha256=provider_account_identity_sha256,
    )
    if prepared.order_role == "exit":
        expected_cid = _required_text(
            expected_exit_client_order_id,
            "expected_exit_client_order_id",
            max_length=160,
        )
        if prepared.provider_client_order_id != expected_cid:
            raise AlpacaFillActivityConflict(
                "exit provider CID differs from durable exit owner"
            )
        ownership = _AUTHORITATIVE
    else:
        if expected_exit_client_order_id is not None:
            raise AlpacaFillActivityError(
                "entry fill cannot carry an exit-owner client id"
            )
        ownership = _ENTRY_ORDER_BOUND

    # The legacy REST activity contract documents transaction_time as the time
    # at which execution occurred.  Preserve the same value in both typed
    # columns; this is not a wall-clock or order-filled-at substitution.
    provider_event_at = prepared.provider_transaction_at
    authoritative = replace(
        prepared,
        capture_schema_version=AUTHORITATIVE_CAPTURE_SCHEMA_VERSION,
        capture_authority_status=_CAPTURE_VERIFIED,
        order_ownership_status=ownership,
        provider_client_order_id_status=_AUTHORITATIVE,
        fee_status=_AUTHORITATIVE,
        provider_event_clock_status=_AUTHORITATIVE,
        provider_event_clock_field="transaction_time",
        provider_event_at=provider_event_at,
    )
    order_binding_body = {
        "capture_schema_version": authoritative.capture_schema_version,
        "capture_authority_status": authoritative.capture_authority_status,
        "reservation_id": str(cycle.reservation_id),
        "decision_packet_sha256": cycle.decision_packet_sha256,
        "account_scope": cycle.account_scope,
        "account_identity_sha256": cycle.account_identity_sha256,
        "broker_connection_generation": cycle.broker_connection_generation,
        "cycle_client_order_id": cycle.cycle_client_order_id,
        "symbol": cycle.symbol,
        "order_role": authoritative.order_role,
        "order_ownership_status": authoritative.order_ownership_status,
        "entry_provider_order_id": cycle.entry_provider_order_id,
        "provider_order_id": authoritative.provider_order_id,
        "provider_client_order_id_status": _AUTHORITATIVE,
        "provider_client_order_id": authoritative.provider_client_order_id,
        "provider_order_payload_sha256": (
            authoritative.provider_order_payload_sha256
        ),
    }
    _binding_json, binding_sha = _canonical_json_text(
        order_binding_body, "order_binding"
    )
    return replace(authoritative, order_binding_sha256=binding_sha)


def _cycle_from_row(row: AlpacaPaperFillActivity) -> AlpacaPaperFillCycleBinding:
    return AlpacaPaperFillCycleBinding(
        reservation_id=row.reservation_id,
        decision_packet_sha256=row.decision_packet_sha256,
        reservation_request_sha256=row.reservation_request_sha256,
        account_scope=row.account_scope,
        account_identity_sha256=row.account_identity_sha256,
        account_snapshot_sha256=row.account_snapshot_sha256,
        account_snapshot_generation=row.account_snapshot_generation,
        broker_connection_generation=row.broker_connection_generation,
        execution_family=row.execution_family,
        position_direction=row.position_direction,
        cycle_client_order_id=row.cycle_client_order_id,
        entry_provider_order_id=row.entry_provider_order_id,
        symbol=row.symbol,
    )


def verify_alpaca_paper_fill_activity_row(
    row: AlpacaPaperFillActivity,
) -> PreparedAlpacaPaperFillActivity:
    """Reparse every retained payload and recompute typed content + event hash."""

    activity = _reparse_canonical_json(
        row.provider_payload_canonical_json,
        row.provider_payload_sha256,
        "provider_activity",
    )
    if row.provider_client_order_id_status in {
        _UNVERIFIED_MAPPING,
        _AUTHORITATIVE,
    }:
        order = _reparse_canonical_json(
            row.provider_order_payload_canonical_json,
            row.provider_order_payload_sha256,
            "provider_order",
        )
    else:
        if (
            row.provider_order_payload_canonical_json is not None
            or row.provider_order_payload_sha256 is not None
        ):
            raise AlpacaFillActivityCorruption(
                "unavailable provider CID retained hidden order payload"
            )
        order = None
    if row.fee_status in {_UNVERIFIED_MAPPING, _AUTHORITATIVE}:
        fee_evidence = _reparse_canonical_json(
            row.fee_evidence_canonical_json,
            row.fee_evidence_sha256,
            "fee_evidence",
        )
    else:
        if (
            row.fee_evidence_canonical_json is not None
            or row.fee_evidence_sha256 is not None
            or row.fee_usd is not None
        ):
            raise AlpacaFillActivityCorruption(
                "unavailable fee retained hidden numeric/evidence value"
            )
        fee_evidence = None
    try:
        cycle = _cycle_from_row(row)
        if row.capture_schema_version == AUTHORITATIVE_CAPTURE_SCHEMA_VERSION:
            prepared = prepare_verified_alpaca_paper_fill_activity(
                cycle,
                provider_activity=activity,
                provider_order=order,
                received_at=row.received_at,
                available_at=row.available_at,
                expected_exit_client_order_id=(
                    row.provider_client_order_id
                    if row.order_role == "exit"
                    else None
                ),
                fee_usd=row.fee_usd,
                fee_evidence=fee_evidence,
            )
        elif row.capture_schema_version == CAPTURE_SCHEMA_VERSION:
            prepared = prepare_alpaca_paper_fill_activity(
                cycle,
                provider_activity=activity,
                received_at=row.received_at,
                available_at=row.available_at,
                provider_event_clock_status=row.provider_event_clock_status,
                provider_event_clock_field=row.provider_event_clock_field,
                provider_client_order_id_status=(
                    row.provider_client_order_id_status
                ),
                provider_order=order,
                fee_status=row.fee_status,
                fee_usd=row.fee_usd,
                fee_evidence=fee_evidence,
            )
        else:
            raise AlpacaFillActivityCorruption(
                "unsupported fill capture schema"
            )
    except AlpacaFillActivityError as exc:
        raise AlpacaFillActivityCorruption(
            f"durable fill failed typed reconstruction: {exc}"
        ) from exc
    typed_comparisons = {
        "capture_authority_status": (
            row.capture_authority_status,
            prepared.capture_authority_status,
        ),
        "order_role": (row.order_role, prepared.order_role),
        "order_ownership_status": (
            row.order_ownership_status,
            prepared.order_ownership_status,
        ),
        "provider_activity_id": (
            row.provider_activity_id,
            prepared.provider_activity_id,
        ),
        "provider_account_id_sha256": (
            row.provider_account_id_sha256,
            prepared.provider_account_id_sha256,
        ),
        "provider_activity_type": (
            row.provider_activity_type,
            prepared.provider_activity_type,
        ),
        "provider_trade_type": (
            row.provider_trade_type,
            prepared.provider_trade_type,
        ),
        "provider_order_id": (row.provider_order_id, prepared.provider_order_id),
        "provider_client_order_id": (
            row.provider_client_order_id,
            prepared.provider_client_order_id,
        ),
        "provider_order_status": (
            row.provider_order_status,
            prepared.provider_order_status,
        ),
        "side": (row.side, prepared.side),
        "quantity": (_decimal(row.quantity, "row.quantity"), prepared.quantity),
        "price": (_decimal(row.price, "row.price"), prepared.price),
        "leaves_quantity": (
            _decimal(row.leaves_quantity, "row.leaves_quantity"),
            prepared.leaves_quantity,
        ),
        "cumulative_quantity": (
            _decimal(row.cumulative_quantity, "row.cumulative_quantity"),
            prepared.cumulative_quantity,
        ),
        "provider_event_at": (row.provider_event_at, prepared.provider_event_at),
        "provider_transaction_at": (
            _utc(row.provider_transaction_at, "row.provider_transaction_at"),
            prepared.provider_transaction_at,
        ),
        "order_binding_sha256": (
            row.order_binding_sha256,
            prepared.order_binding_sha256,
        ),
    }
    if row.capture_schema_version == AUTHORITATIVE_CAPTURE_SCHEMA_VERSION:
        typed_comparisons["immutable_fill_identity_sha256"] = (
            row.immutable_fill_identity_sha256,
            prepared.immutable_fill_identity_sha256,
        )
    changed = sorted(
        name for name, (left, right) in typed_comparisons.items() if left != right
    )
    if changed:
        raise AlpacaFillActivityCorruption(
            "durable typed fill differs from canonical payload: "
            + ",".join(changed)
        )
    if row.capture_schema_version != prepared.capture_schema_version:
        raise AlpacaFillActivityCorruption("fill capture schema changed")
    if row.record_content_sha256 != prepared.record_content_sha256:
        raise AlpacaFillActivityCorruption("fill record content hash mismatch")
    kwargs = prepared.model_kwargs(
        sequence=int(row.sequence),
        previous_event_sha256=row.previous_event_sha256,
    )
    if kwargs["event_sha256"] != row.event_sha256:
        raise AlpacaFillActivityCorruption("fill event lineage hash mismatch")
    return prepared


def verify_alpaca_paper_fill_activity_chain(
    rows: Sequence[AlpacaPaperFillActivity],
) -> None:
    """Verify one contiguous per-reservation append-only hash chain."""

    ordered = sorted(rows, key=lambda row: int(row.sequence))
    previous: str | None = None
    reservation_id: uuid.UUID | None = None
    for expected_sequence, row in enumerate(ordered, start=1):
        if int(row.sequence) != expected_sequence:
            raise AlpacaFillActivityCorruption("fill sequence contains a gap")
        current_reservation = _uuid(row.reservation_id, "row.reservation_id")
        if reservation_id is None:
            reservation_id = current_reservation
        elif current_reservation != reservation_id:
            raise AlpacaFillActivityCorruption("fill chain crosses reservations")
        if row.previous_event_sha256 != previous:
            raise AlpacaFillActivityCorruption("fill chain predecessor mismatch")
        verify_alpaca_paper_fill_activity_row(row)
        previous = row.event_sha256


@dataclass(frozen=True)
class AlpacaPaperFillAppendResult:
    row: AlpacaPaperFillActivity
    created: bool


def _append_alpaca_paper_fill_activity_under_locked_cycle(
    session: Session,
    prepared: PreparedAlpacaPaperFillActivity,
    *,
    reservation: AdaptiveRiskReservation,
    packet: AdaptiveRiskDecisionPacket,
) -> AlpacaPaperFillAppendResult:
    """Append after A1/A2 and the exact reservation row are already held.

    This private seam exists so a complete broker activity batch can use one
    account/reservation lock walk.  It must never acquire advisory locks or
    re-lock the reservation independently; callers establish that authority
    before entering this function.
    """

    retained_binding = AlpacaPaperFillCycleBinding.from_rows(
        reservation,
        packet,
    )
    if retained_binding != prepared.cycle:
        raise AlpacaFillActivityConflict(
            "prepared fill binding differs from locked adaptive cycle"
        )

    existing = session.scalar(
        select(AlpacaPaperFillActivity)
        .where(
            AlpacaPaperFillActivity.account_scope
            == prepared.cycle.account_scope,
            AlpacaPaperFillActivity.account_identity_sha256
            == prepared.cycle.account_identity_sha256,
            AlpacaPaperFillActivity.provider_activity_id
            == prepared.provider_activity_id,
        )
        .with_for_update()
    )
    if existing is not None:
        existing_prepared = verify_alpaca_paper_fill_activity_row(existing)
        if (
            existing_prepared.immutable_fill_identity_sha256
            != prepared.immutable_fill_identity_sha256
        ):
            raise AlpacaFillActivityConflict(
                "provider activity id was reused for a different fill identity"
            )
        # Retrieval clocks and mutable order projections belong to the
        # append-only query observation. Re-reading the same immutable provider
        # fill must reuse the fill fact while preserving the new observation.
        return AlpacaPaperFillAppendResult(row=existing, created=False)

    if reservation.state == "closed":
        raise AlpacaPostSettlementFillRequired(
            "settled cycle requires the post-settlement fill contradiction ledger"
        )

    prior_rows = list(
        session.scalars(
            select(AlpacaPaperFillActivity)
            .where(
                AlpacaPaperFillActivity.reservation_id
                == prepared.cycle.reservation_id
            )
            .order_by(AlpacaPaperFillActivity.sequence)
            .with_for_update()
        )
    )
    verify_alpaca_paper_fill_activity_chain(prior_rows)
    sequence = len(prior_rows) + 1
    previous = prior_rows[-1].event_sha256 if prior_rows else None
    row = AlpacaPaperFillActivity(
        **prepared.model_kwargs(
            sequence=sequence,
            previous_event_sha256=previous,
        )
    )
    session.add(row)
    session.flush([row])
    return AlpacaPaperFillAppendResult(row=row, created=True)


def append_alpaca_paper_fill_activity(
    session: Session,
    prepared: PreparedAlpacaPaperFillActivity,
) -> AlpacaPaperFillAppendResult:
    """Append exactly once inside a caller-owned PostgreSQL transaction.

    The shared account-risk advisory lock serializes account rotations and
    cross-reservation duplicate activity ids.  The reservation row lock then
    serializes this cycle's sequence/hash lineage.  This function flushes but
    never commits and never performs broker I/O.
    """

    if not session.in_transaction():
        raise AlpacaFillActivityError(
            "caller must own an explicit transaction before fill append"
        )
    # Join both legacy account advisory domains in canonical A1 -> A2 order.
    # Account-head locking is intentionally skipped for a pure fill append;
    # after the advisories, the row walk is reservation (stage 2) then fill
    # activity (stage 3), never the inverse.
    acquire_adaptive_risk_account_locks(
        session,
        account_scope=prepared.cycle.account_scope,
    )
    reservation = session.scalar(
        select(AdaptiveRiskReservation)
        .where(
            AdaptiveRiskReservation.reservation_id
            == prepared.cycle.reservation_id
        )
        .with_for_update()
    )
    if reservation is None:
        raise AlpacaFillActivityConflict("adaptive reservation is missing")
    packet = session.get(
        AdaptiveRiskDecisionPacket,
        reservation.decision_packet_sha256,
    )
    if packet is None:
        raise AlpacaFillActivityConflict("immutable decision packet is missing")
    return _append_alpaca_paper_fill_activity_under_locked_cycle(
        session,
        prepared,
        reservation=reservation,
        packet=packet,
    )


@dataclass(frozen=True)
class AlpacaPaperOrderFillCaptureResult:
    reservation_id: uuid.UUID
    provider_order_id: str
    observed_count: int
    created_count: int
    event_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class AlpacaPaperEntryFillHandoffProof:
    """Content-addressed bridge from a fill commit to transport continuation.

    The proof contains no broker-read assertion of its own.  It names the
    immutable fill/contradiction row, the exact adaptive lifecycle event that
    consumed that row, and the query observation that made the execution
    available.  A transport caller can therefore move an indeterminate outbox
    row into lifecycle continuation without either resending the POST or
    trusting a mutable order projection.
    """

    schema_version: str
    publication_kind: str
    reservation_id: uuid.UUID
    decision_packet_sha256: str
    account_scope: str
    account_identity_sha256: str
    client_order_id: str
    broker_order_id: str
    broker_connection_generation: str
    observation_sha256: str
    durability_kind: str
    source_record_table: str
    source_record_id: str
    terminal_evidence_sha256: str
    immutable_fill_identity_sha256: str
    cumulative_filled_quantity_shares: int
    lifecycle_provider_event_id: str
    lifecycle_event_sha256: str
    lifecycle_event_sequence: int
    resulting_reservation_state: str
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        schema_version = _required_text(
            self.schema_version,
            "handoff.schema_version",
        )
        if schema_version != ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION:
            raise AlpacaFillActivityError(
                "unsupported Alpaca PAPER fill-handoff schema"
            )
        object.__setattr__(self, "schema_version", schema_version)
        publication_kind = _required_text(
            self.publication_kind,
            "handoff.publication_kind",
            lower=True,
        )
        if publication_kind not in {
            "active_cycle_fill",
            "post_settlement_contradiction",
        }:
            raise AlpacaFillActivityError(
                "unsupported Alpaca PAPER fill-handoff publication kind"
            )
        object.__setattr__(self, "publication_kind", publication_kind)
        object.__setattr__(
            self,
            "reservation_id",
            _uuid(self.reservation_id, "handoff.reservation_id"),
        )
        for name in (
            "decision_packet_sha256",
            "account_identity_sha256",
            "observation_sha256",
            "source_record_id",
            "terminal_evidence_sha256",
            "immutable_fill_identity_sha256",
            "lifecycle_event_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _require_sha(getattr(self, name), f"handoff.{name}"),
            )
        for name in (
            "account_scope",
            "client_order_id",
            "broker_order_id",
            "broker_connection_generation",
            "source_record_table",
            "lifecycle_provider_event_id",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), f"handoff.{name}"),
            )
        if self.account_scope != "alpaca:paper":
            raise AlpacaFillActivityError(
                "fill-handoff account_scope is not Alpaca PAPER"
            )
        durability_kind = _required_text(
            self.durability_kind,
            "handoff.durability_kind",
            lower=True,
        )
        object.__setattr__(self, "durability_kind", durability_kind)
        resulting_state = _required_text(
            self.resulting_reservation_state,
            "handoff.resulting_reservation_state",
            lower=True,
        )
        if resulting_state not in {
            "partially_filled",
            "filled",
            "exposure_quarantined",
        }:
            raise AlpacaFillActivityError(
                "fill-handoff result is not a positive-fill reservation state"
            )
        object.__setattr__(
            self,
            "resulting_reservation_state",
            resulting_state,
        )
        if publication_kind == "active_cycle_fill":
            if not (
                durability_kind == "committed_alpaca_paper_fill"
                and self.source_record_table == "alpaca_paper_fill_activities"
                and self.lifecycle_provider_event_id
                == (
                    f"alpaca-fill:{self.source_record_id}:observation:"
                    f"{self.observation_sha256}"
                )
            ):
                raise AlpacaFillActivityError(
                    "active fill-handoff source binding is invalid"
                )
        else:
            if not (
                durability_kind
                == POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
                and self.source_record_table
                == "alpaca_paper_post_settlement_fill_contradictions"
                and self.lifecycle_provider_event_id
                == f"alpaca-post-settlement-fill:{self.source_record_id}"
                and resulting_state == "exposure_quarantined"
            ):
                raise AlpacaFillActivityError(
                    "post-settlement fill-handoff source binding is invalid"
                )
        quantity = self.cumulative_filled_quantity_shares
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise AlpacaFillActivityError(
                "fill-handoff cumulative quantity must be a positive integer"
            )
        sequence = self.lifecycle_event_sequence
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            raise AlpacaFillActivityError(
                "fill-handoff lifecycle sequence must be a positive integer"
            )
        observed_at = _utc(self.observed_at, "handoff.observed_at")
        available_at = _utc(self.available_at, "handoff.available_at")
        if available_at < observed_at:
            raise AlpacaFillActivityError(
                "fill-handoff availability precedes provider observation"
            )
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)

    def _canonical_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "publication_kind": self.publication_kind,
            "reservation_id": str(self.reservation_id),
            "decision_packet_sha256": self.decision_packet_sha256,
            "account_scope": self.account_scope,
            "account_identity_sha256": self.account_identity_sha256,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "broker_connection_generation": (
                self.broker_connection_generation
            ),
            "observation_sha256": self.observation_sha256,
            "durability_kind": self.durability_kind,
            "source_record_table": self.source_record_table,
            "source_record_id": self.source_record_id,
            "terminal_evidence_sha256": self.terminal_evidence_sha256,
            "immutable_fill_identity_sha256": (
                self.immutable_fill_identity_sha256
            ),
            "cumulative_filled_quantity_shares": (
                self.cumulative_filled_quantity_shares
            ),
            "lifecycle_provider_event_id": self.lifecycle_provider_event_id,
            "lifecycle_event_sha256": self.lifecycle_event_sha256,
            "lifecycle_event_sequence": self.lifecycle_event_sequence,
            "resulting_reservation_state": self.resulting_reservation_state,
            "observed_at": _iso(self.observed_at),
            "available_at": _iso(self.available_at),
        }

    def to_canonical_json(self) -> str:
        canonical, _ = _canonical_json_text(
            self._canonical_body(),
            "alpaca_paper_entry_fill_handoff",
        )
        return canonical

    @property
    def proof_canonical_json(self) -> str:
        return self.to_canonical_json()

    @property
    def proof_sha256(self) -> str:
        return _sha256_text(self.proof_canonical_json)

    def to_payload(self) -> dict[str, Any]:
        payload = self._canonical_body()
        payload["proof_sha256"] = self.proof_sha256
        return payload

    @classmethod
    def from_canonical_json(
        cls,
        value: str,
    ) -> "AlpacaPaperEntryFillHandoffProof":
        if not isinstance(value, str) or not value:
            raise AlpacaFillActivityError(
                "fill-handoff canonical JSON is required"
            )
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AlpacaFillActivityError(
                "fill-handoff canonical JSON does not parse"
            ) from exc
        if not isinstance(parsed, dict):
            raise AlpacaFillActivityError(
                "fill-handoff canonical JSON must be an object"
            )
        expected_fields = {
            "schema_version",
            "publication_kind",
            "reservation_id",
            "decision_packet_sha256",
            "account_scope",
            "account_identity_sha256",
            "client_order_id",
            "broker_order_id",
            "broker_connection_generation",
            "observation_sha256",
            "durability_kind",
            "source_record_table",
            "source_record_id",
            "terminal_evidence_sha256",
            "immutable_fill_identity_sha256",
            "cumulative_filled_quantity_shares",
            "lifecycle_provider_event_id",
            "lifecycle_event_sha256",
            "lifecycle_event_sequence",
            "resulting_reservation_state",
            "observed_at",
            "available_at",
        }
        if set(parsed) != expected_fields:
            raise AlpacaFillActivityError(
                "fill-handoff canonical JSON field set is invalid"
            )
        canonical, _ = _canonical_json_text(
            parsed,
            "alpaca_paper_entry_fill_handoff",
        )
        if canonical != value:
            raise AlpacaFillActivityError(
                "fill-handoff JSON is not canonical"
            )
        try:
            proof = cls(
                **{
                    **parsed,
                    "reservation_id": _uuid(
                        parsed["reservation_id"],
                        "handoff.reservation_id",
                    ),
                    "observed_at": _provider_time(
                        parsed["observed_at"],
                        "handoff.observed_at",
                    ),
                    "available_at": _provider_time(
                        parsed["available_at"],
                        "handoff.available_at",
                    ),
                }
            )
        except TypeError as exc:
            raise AlpacaFillActivityError(
                "fill-handoff canonical JSON is invalid"
            ) from exc
        if proof.to_canonical_json() != value:
            raise AlpacaFillActivityError(
                "fill-handoff canonical reconstruction changed"
            )
        return proof


@dataclass(frozen=True)
class AlpacaPaperEntryFillPublicationResult:
    """One-transaction result for exact entry fills and their risk watermark."""

    capture: AlpacaPaperOrderFillCaptureResult | None
    reservation_state: "AdaptiveReservationState"
    publication_kind: str = "active_cycle_fill"
    contradiction_sha256s: tuple[str, ...] = ()
    settlement_sha256: str | None = None
    handoff_proof: AlpacaPaperEntryFillHandoffProof | None = None


_POST_SETTLEMENT_CONTRADICTION_BODY_FIELDS = (
    "contradiction_schema_version",
    "authority_status",
    "durability_kind",
    "reservation_id",
    "settlement_sha256",
    "decision_packet_sha256",
    "account_scope",
    "account_identity_sha256",
    "broker_environment",
    "execution_family",
    "symbol",
    "expected_client_order_id",
    "broker_order_id",
    "broker_connection_generation",
    "source_state",
    "settlement_terminal_fill_event_sha256",
    "settlement_source_fill_count",
    "settlement_entry_quantity",
    "settlement_exit_quantity",
    "settlement_net_realized_pnl_usd",
    "contradiction_sequence",
    "previous_contradiction_sha256",
    "batch_activity_ordinal",
    "batch_activity_count",
    "is_projection_terminal",
    "batch_content_sha256",
    "observation_sha256",
    "provider_order_payload_sha256",
    "query_receipt_sha256",
    "read_binding_sha256",
    "adapter_connection_generation",
    "adapter_build_sha256",
    "capability_authority_status",
    "capability_verified_at",
    "query_after",
    "query_until",
    "query_received_at",
    "query_available_at",
    "query_expires_at",
    "provider_activity_id",
    "immutable_fill_identity_sha256",
    "provider_order_id",
    "provider_payload_sha256",
    "provider_transaction_at",
    "provider_available_at",
    "side",
    "order_role",
    "order_ownership_status",
    "quantity",
    "price",
    "leaves_quantity",
    "prior_recorded_cumulative_quantity",
    "broker_observed_cumulative_quantity",
    "positive_fill_delta",
    "projection_prior_cumulative_quantity",
    "projection_positive_fill_delta",
    "projected_open_quantity_shares",
    "projected_open_structural_risk_usd",
    "projected_open_gross_notional_usd",
    "projected_open_buying_power_impact_usd",
    "fee_status",
    "fee_usd",
    "fee_evidence_sha256",
)
_POST_SETTLEMENT_CONTRADICTION_DECIMAL_FIELDS = frozenset(
    {
        "settlement_entry_quantity",
        "settlement_exit_quantity",
        "settlement_net_realized_pnl_usd",
        "quantity",
        "price",
        "leaves_quantity",
        "prior_recorded_cumulative_quantity",
        "broker_observed_cumulative_quantity",
        "positive_fill_delta",
        "projection_prior_cumulative_quantity",
        "projection_positive_fill_delta",
        "projected_open_structural_risk_usd",
        "projected_open_gross_notional_usd",
        "projected_open_buying_power_impact_usd",
        "fee_usd",
    }
)
_POST_SETTLEMENT_CONTRADICTION_TIME_FIELDS = frozenset(
    {
        "capability_verified_at",
        "query_after",
        "query_until",
        "query_received_at",
        "query_available_at",
        "query_expires_at",
        "provider_transaction_at",
        "provider_available_at",
    }
)


def _post_settlement_contradiction_content_body(
    values: Mapping[str, Any] | AlpacaPaperPostSettlementFillContradiction,
) -> dict[str, Any]:
    def read(name: str) -> Any:
        if isinstance(values, Mapping):
            return values.get(name)
        return getattr(values, name)

    body: dict[str, Any] = {}
    for name in _POST_SETTLEMENT_CONTRADICTION_BODY_FIELDS:
        value = read(name)
        if value is None:
            body[name] = None
        elif name == "reservation_id":
            body[name] = str(_uuid(value, name))
        elif name in _POST_SETTLEMENT_CONTRADICTION_DECIMAL_FIELDS:
            body[name] = _decimal_text(_decimal(value, name))
        elif name in _POST_SETTLEMENT_CONTRADICTION_TIME_FIELDS:
            body[name] = _iso(_utc(value, name))
        else:
            body[name] = value
    return body


def verify_alpaca_paper_post_settlement_fill_contradiction_row(
    row: AlpacaPaperPostSettlementFillContradiction,
) -> None:
    """Rebuild one immutable late-fill contradiction and all byte hashes."""

    if not isinstance(row, AlpacaPaperPostSettlementFillContradiction):
        raise AlpacaFillActivityCorruption(
            "post-settlement fill contradiction row is malformed"
        )
    if not (
        row.contradiction_schema_version
        == POST_SETTLEMENT_FILL_CONTRADICTION_SCHEMA_VERSION
        and row.authority_status == "verified"
        and row.durability_kind
        == POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
        and row.capability_authority_status
        == "process_private_hmac_verified_before_commit"
    ):
        raise AlpacaFillActivityCorruption(
            "post-settlement fill contradiction authority changed"
        )
    canonical_pairs = (
        (
            row.batch_content_canonical_json,
            row.batch_content_sha256,
            "contradiction.batch_content",
        ),
        (
            row.observation_content_canonical_json,
            row.observation_sha256,
            "contradiction.observation_content",
        ),
        (
            row.provider_order_payload_canonical_json,
            row.provider_order_payload_sha256,
            "contradiction.provider_order",
        ),
        (
            row.query_receipt_canonical_json,
            row.query_receipt_sha256,
            "contradiction.query_receipt",
        ),
        (
            row.read_binding_canonical_json,
            row.read_binding_sha256,
            "contradiction.read_binding",
        ),
        (
            row.provider_payload_canonical_json,
            row.provider_payload_sha256,
            "contradiction.provider_activity",
        ),
        (
            row.fee_evidence_canonical_json,
            row.fee_evidence_sha256,
            "contradiction.fee_evidence",
        ),
    )
    for canonical, digest, field in canonical_pairs:
        _reparse_canonical_json(canonical, digest, field)
    if not (
        row.batch_content_canonical_json
        == row.observation_content_canonical_json
        and row.batch_content_sha256 == row.observation_sha256
    ):
        raise AlpacaFillActivityCorruption(
            "post-settlement batch/observation content differs"
        )
    body = _post_settlement_contradiction_content_body(row)
    canonical, digest = _canonical_json_text(
        body, "post_settlement_fill_contradiction"
    )
    if not (
        row.contradiction_content_canonical_json == canonical
        and row.contradiction_content_sha256 == digest
        and row.contradiction_sha256 == digest
    ):
        raise AlpacaFillActivityCorruption(
            "post-settlement fill contradiction content changed"
        )


def _exact_alpaca_paper_fill_reader(adapter: Any) -> tuple[Any, str]:
    """Return the exact underlying adapter and canonical bound account UUID."""

    # Imports stay local so importing this inert evidence module cannot start a
    # venue client or capture coordinator.
    from ..venue.alpaca_spot import AlpacaSpotAdapter
    from .captured_alpaca_paper_adapter import CapturedAlpacaPaperAdapter

    if type(adapter) is AlpacaSpotAdapter:
        authoritative_adapter = adapter
    elif type(adapter) is CapturedAlpacaPaperAdapter:
        authoritative_adapter = getattr(adapter, "_adapter", None)
        if type(authoritative_adapter) is not AlpacaSpotAdapter:
            authoritative_adapter = None
    else:
        authoritative_adapter = None
    if authoritative_adapter is None:
        raise AlpacaFillActivityError(
            "Alpaca PAPER fill-activity reader is unavailable"
        )
    bound_account_id = str(
        getattr(authoritative_adapter, "bound_account_id", "") or ""
    ).strip()
    if not bound_account_id:
        raise AlpacaFillActivityError(
            "Alpaca PAPER fill reader is not account-generation bound"
        )
    return authoritative_adapter, bound_account_id


def read_verified_alpaca_paper_fill_batch(
    adapter: Any,
    *,
    cycle: AlpacaPaperFillCycleBinding,
    provider_order_id: str,
    expected_client_order_id: str,
) -> PreparedAlpacaPaperFillBatch:
    """Perform the entire broker read and return an immutable verified batch.

    This function has no ``Session`` argument and performs no database access.
    It validates the exact adapter/account generation, pagination-completeness
    receipt, OID/CID ownership, raw order and activity bytes, fee receipt, and
    causal clocks before content-addressing the handoff.
    """

    if not isinstance(cycle, AlpacaPaperFillCycleBinding):
        raise AlpacaFillActivityError("cycle must be an immutable fill binding")
    order_id = _required_text(
        provider_order_id, "provider_order_id", max_length=160
    )
    expected_cid = _required_text(
        expected_client_order_id,
        "expected_client_order_id",
        max_length=160,
    )
    order_role = "entry" if order_id == cycle.entry_provider_order_id else "exit"
    if order_role == "entry":
        if expected_cid != cycle.cycle_client_order_id:
            raise AlpacaFillActivityConflict(
                "entry expected CID differs from immutable cycle CID"
            )
    else:
        # A string supplied by the caller is not durable order ownership. The
        # forthcoming lifecycle transaction must load and seal the exact exit
        # owner receipt, then this reader can consume that opaque capability.
        # Until that issuer is wired, exit capture fails before broker I/O.
        raise AlpacaFillActivityError(
            "sealed durable exit-order ownership receipt is required"
        )

    authoritative_adapter, bound_account_id = _exact_alpaca_paper_fill_reader(adapter)
    try:
        bound_identity_sha256 = alpaca_paper_account_identity_sha256(
            bound_account_id
        )
    except ValueError as exc:
        raise AlpacaFillActivityConflict(
            "bound Alpaca PAPER account UUID is malformed"
        ) from exc
    if bound_identity_sha256 != cycle.account_identity_sha256:
        raise AlpacaFillActivityConflict(
            "bound Alpaca PAPER account differs from adaptive cycle"
        )

    # Dispatch through the exact registered class method. An instance-level
    # monkeypatch can no longer manufacture a complete-looking broker batch.
    from ..venue.alpaca_spot import AlpacaSpotAdapter

    expected_read_binding = _fill_read_binding(
        cycle,
        provider_order_id=order_id,
        expected_client_order_id=expected_cid,
        order_role=order_role,
    )
    expected_read_binding_json, expected_read_binding_sha256 = (
        _canonical_json_text(expected_read_binding, "read_binding")
    )
    batch = AlpacaSpotAdapter.get_paper_fill_activity_batch(
        authoritative_adapter,
        order_id,
        read_binding=expected_read_binding,
    )
    if not isinstance(batch, Mapping):
        raise AlpacaFillActivityError("Alpaca PAPER fill batch is unreadable")
    if (
        batch.get("readable") is not True
        or batch.get("pagination_complete") is not True
    ):
        raise AlpacaFillActivityError(
            "Alpaca PAPER fill pagination is incomplete or unreadable"
        )
    broker_environment = _required_text(
        batch.get("broker_environment"), "batch.broker_environment", lower=True
    )
    asset_class = _required_text(
        batch.get("asset_class"), "batch.asset_class", lower=True
    )
    if broker_environment != "paper" or asset_class != "us_equity":
        raise AlpacaFillActivityError(
            "authoritative fill batch must be Alpaca PAPER US equity"
        )
    provider_account_id = _required_text(
        batch.get("provider_account_id"), "batch.provider_account_id"
    )
    if provider_account_id != bound_account_id:
        raise AlpacaFillActivityConflict(
            "fill batch account differs from bound PAPER account"
        )
    adapter_connection_generation = _required_text(
        batch.get("adapter_connection_generation"),
        "batch.adapter_connection_generation",
        max_length=160,
    )
    adapter_build_sha256 = _require_sha(
        batch.get("adapter_build_sha256"), "batch.adapter_build_sha256"
    )
    query_receipt_json = batch.get("query_receipt_canonical_json")
    query_receipt_sha256 = _require_sha(
        batch.get("query_receipt_sha256"), "batch.query_receipt_sha256"
    )
    if (
        not isinstance(query_receipt_json, str)
        or _sha256_text(query_receipt_json) != query_receipt_sha256
    ):
        raise AlpacaFillActivityCorruption("fill query receipt hash mismatch")
    read_binding_json = batch.get("read_binding_canonical_json")
    read_binding_sha256 = _require_sha(
        batch.get("read_binding_sha256"), "batch.read_binding_sha256"
    )
    if (
        read_binding_json != expected_read_binding_json
        or read_binding_sha256 != expected_read_binding_sha256
        or not isinstance(read_binding_json, str)
        or _sha256_text(read_binding_json) != read_binding_sha256
    ):
        raise AlpacaFillActivityCorruption(
            "fill read binding differs from immutable cycle/order scope"
        )
    available_capability_at = _utc(batch.get("available_at"), "batch.available_at")
    expires_at = _utc(batch.get("expires_at"), "batch.expires_at")
    capability_payload = {
        "schema_version": FILL_READ_CAPABILITY_SCHEMA_VERSION,
        "broker_environment": broker_environment,
        "asset_class": asset_class,
        "provider_account_id": provider_account_id,
        "provider_order_id": order_id,
        "adapter_connection_generation": adapter_connection_generation,
        "adapter_build_sha256": adapter_build_sha256,
        "query_receipt_sha256": query_receipt_sha256,
        "read_binding_sha256": read_binding_sha256,
        "available_at": available_capability_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    try:
        verify_alpaca_fill_read_capability(
            batch.get("_capture_capability"),
            expected_payload=capability_payload,
            verified_at=datetime.now(UTC),
        )
    except AlpacaFillReadCapabilityError as exc:
        raise AlpacaFillActivityCorruption(
            f"Alpaca fill read capability rejected: {exc}"
        ) from exc

    provider_order = batch.get("provider_order")
    if not isinstance(provider_order, Mapping):
        raise AlpacaFillActivityError("exact provider order payload is missing")
    provider_order_json, provider_order_sha = _canonical_json_text(
        provider_order, "provider_order"
    )
    order = json.loads(provider_order_json)
    if _required_text(order.get("id"), "provider_order.id") != order_id:
        raise AlpacaFillActivityConflict("fill batch provider order id changed")
    provider_cid = _required_text(
        order.get("client_order_id"),
        "provider_order.client_order_id",
        max_length=160,
    )
    if provider_cid != expected_cid:
        raise AlpacaFillActivityConflict(
            "fill batch provider CID differs from durable order owner"
        )
    provider_symbol = _required_text(
        order.get("symbol"), "provider_order.symbol", upper=True, max_length=36
    )
    if provider_symbol != cycle.symbol:
        raise AlpacaFillActivityConflict("fill batch provider order symbol changed")
    provider_side = _required_text(
        order.get("side"), "provider_order.side", lower=True, max_length=16
    )
    expected_side = "buy" if order_role == "entry" else "sell"
    if provider_side != expected_side:
        raise AlpacaFillActivityConflict(
            "fill batch provider order side differs from owned order role"
        )
    _required_text(order.get("status"), "provider_order.status", lower=True)
    if order.get("account_id") is not None and _required_text(
        order.get("account_id"), "provider_order.account_id"
    ) != bound_account_id:
        raise AlpacaFillActivityConflict(
            "fill batch provider order account differs from bound PAPER account"
        )

    query_after = _provider_time(batch.get("query_after"), "batch.query_after")
    query_until = _provider_time(batch.get("query_until"), "batch.query_until")
    received_at = _utc(batch.get("received_at"), "batch.received_at")
    available_at = available_capability_at
    if not query_after <= query_until <= received_at <= available_at < expires_at:
        raise AlpacaFillActivityError(
            "fill batch query/received/available/expiry clocks are not causal"
        )
    observations = batch.get("activities")
    if not isinstance(observations, list):
        raise AlpacaFillActivityError("Alpaca activity inventory is malformed")

    # Validate and deterministically order the complete provider batch before
    # acquiring any database lock. Broker I/O and malformed payload handling
    # must not extend the account-risk critical section.
    normalized: list[
        tuple[datetime, str, Mapping[str, Any], Any, Mapping[str, Any]]
    ] = []
    seen_activity_ids: set[str] = set()
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping):
            raise AlpacaFillActivityError(
                f"activities[{index}] is not a broker activity envelope"
            )
        activity = item.get("provider_activity")
        fee_evidence = item.get("fee_evidence")
        if not isinstance(activity, Mapping) or not isinstance(
            fee_evidence, Mapping
        ):
            raise AlpacaFillActivityError(
                f"activities[{index}] lacks activity/fee evidence"
            )
        if _required_text(
            activity.get("order_id"),
            f"activities[{index}].provider_activity.order_id",
        ) != order_id:
            raise AlpacaFillActivityConflict(
                "fill batch contains another provider order"
            )
        transaction_at = _provider_time(
            activity.get("transaction_time"),
            f"activities[{index}].provider_activity.transaction_time",
        )
        if not query_after <= transaction_at <= query_until:
            raise AlpacaFillActivityError(
                "fill activity falls outside the complete broker query window"
            )
        activity_id = _required_text(
            activity.get("id"),
            f"activities[{index}].provider_activity.id",
        )
        if activity_id in seen_activity_ids:
            raise AlpacaFillActivityConflict(
                "fill batch repeats a provider activity id"
            )
        seen_activity_ids.add(activity_id)
        normalized.append(
            (
                transaction_at,
                activity_id,
                activity,
                item.get("fee_usd"),
                fee_evidence,
            )
        )

    prepared_items: list[PreparedAlpacaPaperFillActivity] = []
    for _at, _activity_id, activity, fee_usd, fee_evidence in sorted(
        normalized, key=lambda value: (value[0], value[1])
    ):
        prepared_items.append(
            prepare_verified_alpaca_paper_fill_activity(
                cycle,
                provider_activity=activity,
                provider_order=order,
                authoritative_provider_account_id=provider_account_id,
                received_at=received_at,
                available_at=available_at,
                expected_exit_client_order_id=(
                    expected_cid if order_role == "exit" else None
                ),
                fee_usd=fee_usd,
                fee_evidence=fee_evidence,
            )
        )

    # The query begins at the order-creation day's UTC boundary and is
    # pagination-complete through ``query_until``.  Consequently an exact
    # order's activity list must reconstruct its current cumulative fill; a
    # newer order projection or retrieval clock is not allowed to manufacture
    # that watermark.  CHILI's equity policy is whole-share-only.
    provider_quantity = _whole_share_quantity(
        order.get("qty"), "provider_order.qty", positive=True
    )
    provider_filled = _whole_share_quantity(
        order.get("filled_qty"), "provider_order.filled_qty"
    )
    if provider_filled > provider_quantity:
        raise AlpacaFillActivityConflict(
            "provider order cumulative fill exceeds order quantity"
        )
    reconstructed = 0
    for ordinal, item in enumerate(prepared_items):
        fill_quantity = _whole_share_quantity(
            item.quantity,
            f"activities[{ordinal}].quantity",
            positive=True,
        )
        cumulative = _whole_share_quantity(
            item.cumulative_quantity,
            f"activities[{ordinal}].cumulative_quantity",
            positive=True,
        )
        leaves = _whole_share_quantity(
            item.leaves_quantity,
            f"activities[{ordinal}].leaves_quantity",
        )
        reconstructed += fill_quantity
        if cumulative != reconstructed:
            raise AlpacaFillActivityConflict(
                "provider fill cumulative sequence is not exact"
            )
        if leaves != provider_quantity - cumulative or leaves < 0:
            raise AlpacaFillActivityConflict(
                "provider fill leaves quantity differs from exact order"
            )
    if reconstructed != provider_filled:
        raise AlpacaFillActivityConflict(
            "provider order fill projection differs from complete activities"
        )

    provisional = PreparedAlpacaPaperFillBatch(
        batch_schema_version=PREPARED_FILL_BATCH_SCHEMA_VERSION,
        cycle=cycle,
        provider_order_id=order_id,
        expected_client_order_id=expected_cid,
        order_role=order_role,
        query_after=query_after,
        query_until=query_until,
        received_at=received_at,
        available_at=available_at,
        expires_at=expires_at,
        broker_environment=broker_environment,
        asset_class=asset_class,
        provider_account_id_sha256=bound_identity_sha256,
        adapter_connection_generation=adapter_connection_generation,
        adapter_build_sha256=adapter_build_sha256,
        provider_order_payload_canonical_json=provider_order_json,
        provider_order_payload_sha256=provider_order_sha,
        query_receipt_canonical_json=query_receipt_json,
        query_receipt_sha256=query_receipt_sha256,
        read_binding_canonical_json=read_binding_json,
        read_binding_sha256=read_binding_sha256,
        activities=tuple(prepared_items),
        batch_content_sha256="0" * 64,
        read_capability=batch.get("_capture_capability"),
    )
    prepared_batch = replace(
        provisional,
        batch_content_sha256=provisional.computed_batch_content_sha256(),
    )
    _verify_query_receipt(prepared_batch)
    return prepared_batch


def _verify_prepared_alpaca_paper_fill_batch(
    batch: PreparedAlpacaPaperFillBatch,
) -> None:
    """Rebuild every byte/typed binding before any publication lock is taken."""

    if not isinstance(batch, PreparedAlpacaPaperFillBatch):
        raise AlpacaFillActivityCorruption("prepared fill batch is malformed")
    try:
        if batch.batch_schema_version != PREPARED_FILL_BATCH_SCHEMA_VERSION:
            raise AlpacaFillActivityError("prepared fill batch schema is unsupported")
        try:
            verify_alpaca_fill_read_capability(
                batch.read_capability,
                expected_payload=batch.capability_payload(),
                verified_at=datetime.now(UTC),
            )
        except AlpacaFillReadCapabilityError as exc:
            raise AlpacaFillActivityCorruption(
                f"prepared fill batch lacks broker-read authority: {exc}"
            ) from exc
        if batch.computed_batch_content_sha256() != batch.batch_content_sha256:
            raise AlpacaFillActivityCorruption(
                "prepared fill batch content hash mismatch"
            )
        _verify_read_binding(batch)
        query_receipt = _verify_query_receipt(batch)
        authoritative_provider_account_id = _required_text(
            query_receipt.get("provider_account_id"),
            "query_receipt.provider_account_id",
        )
        order = _reparse_canonical_json(
            batch.provider_order_payload_canonical_json,
            batch.provider_order_payload_sha256,
            "provider_order",
        )
        order_id = _required_text(order.get("id"), "provider_order.id")
        order_cid = _required_text(
            order.get("client_order_id"),
            "provider_order.client_order_id",
            max_length=160,
        )
        order_side = _required_text(
            order.get("side"), "provider_order.side", lower=True
        )
        order_symbol = _required_text(
            order.get("symbol"), "provider_order.symbol", upper=True
        )
        expected_role = (
            "entry"
            if batch.provider_order_id == batch.cycle.entry_provider_order_id
            else "exit"
        )
        if not (
            order_id == batch.provider_order_id
            and order_cid == batch.expected_client_order_id
            and order_symbol == batch.cycle.symbol
            and batch.order_role == expected_role
            and order_side == ("buy" if expected_role == "entry" else "sell")
        ):
            raise AlpacaFillActivityCorruption(
                "prepared fill batch order ownership binding changed"
            )
        if expected_role == "entry":
            if batch.expected_client_order_id != batch.cycle.cycle_client_order_id:
                raise AlpacaFillActivityCorruption(
                    "prepared entry batch CID differs from immutable cycle"
                )
            expected_exit_cid = None
        else:
            if batch.expected_client_order_id == batch.cycle.cycle_client_order_id:
                raise AlpacaFillActivityCorruption(
                    "prepared exit batch aliases immutable entry CID"
                )
            expected_exit_cid = batch.expected_client_order_id
        query_after = _utc(batch.query_after, "batch.query_after")
        query_until = _utc(batch.query_until, "batch.query_until")
        received_at = _utc(batch.received_at, "batch.received_at")
        available_at = _utc(batch.available_at, "batch.available_at")
        expires_at = _utc(batch.expires_at, "batch.expires_at")
        if not query_after <= query_until <= received_at <= available_at < expires_at:
            raise AlpacaFillActivityCorruption(
                "prepared fill batch clocks/expiry are not causal"
            )
        if (
            batch.broker_environment != "paper"
            or batch.asset_class != "us_equity"
            or batch.provider_account_id_sha256
            != batch.cycle.account_identity_sha256
        ):
            raise AlpacaFillActivityCorruption(
                "prepared fill batch PAPER US-equity identity changed"
            )

        reconstructed: list[PreparedAlpacaPaperFillActivity] = []
        seen_ids: set[str] = set()
        for prepared in batch.activities:
            activity = _reparse_canonical_json(
                prepared.provider_payload_canonical_json,
                prepared.provider_payload_sha256,
                "provider_activity",
            )
            fee_evidence = _reparse_canonical_json(
                prepared.fee_evidence_canonical_json,
                prepared.fee_evidence_sha256,
                "fee_evidence",
            )
            rebuilt = prepare_verified_alpaca_paper_fill_activity(
                batch.cycle,
                provider_activity=activity,
                provider_order=order,
                authoritative_provider_account_id=(
                    authoritative_provider_account_id
                ),
                received_at=received_at,
                available_at=available_at,
                expected_exit_client_order_id=expected_exit_cid,
                fee_usd=prepared.fee_usd,
                fee_evidence=fee_evidence,
            )
            if rebuilt != prepared:
                raise AlpacaFillActivityCorruption(
                    "prepared fill differs from canonical broker payload"
                )
            if prepared.provider_activity_id in seen_ids:
                raise AlpacaFillActivityCorruption(
                    "prepared fill batch repeats a provider activity id"
                )
            seen_ids.add(prepared.provider_activity_id)
            if not query_after <= prepared.provider_transaction_at <= query_until:
                raise AlpacaFillActivityCorruption(
                    "prepared fill falls outside its broker query window"
                )
            reconstructed.append(rebuilt)
        expected_order = tuple(
            sorted(
                reconstructed,
                key=lambda item: (
                    item.provider_transaction_at,
                    item.provider_activity_id,
                ),
            )
        )
        if tuple(batch.activities) != expected_order:
            raise AlpacaFillActivityCorruption(
                "prepared fill batch activity order is not deterministic"
            )
    except AlpacaFillActivityCorruption:
        raise
    except AlpacaFillActivityError as exc:
        raise AlpacaFillActivityCorruption(
            f"prepared fill batch failed verification: {exc}"
        ) from exc


def _observation_content(
    batch: PreparedAlpacaPaperFillBatch,
) -> tuple[str, str]:
    canonical, digest = _canonical_json_text(
        batch.content_body(), "fill_query_observation_content"
    )
    if digest != batch.batch_content_sha256:
        raise AlpacaFillActivityCorruption(
            "fill query observation differs from prepared batch content"
        )
    return canonical, digest


def _page_rows_from_receipt(
    *,
    observation_sha256: str,
    page: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    page_index = page.get("page_index")
    if isinstance(page_index, bool) or not isinstance(page_index, int):
        raise AlpacaFillActivityCorruption("fill observation page index is invalid")
    object_body = {
        "page_schema_version": "chili.alpaca-paper-fill-page-object.v1",
        "request_sha256": _require_sha(
            page.get("request_sha256"), "page.request_sha256"
        ),
        "response_sha256": _require_sha(
            page.get("response_sha256"), "page.response_sha256"
        ),
        "response_count": page.get("response_count"),
    }
    _object_json, page_object_sha256 = _canonical_json_text(
        object_body, "fill_page_object"
    )
    requested_at = _provider_time(page.get("requested_at"), "page.requested_at")
    received_at = _provider_time(page.get("received_at"), "page.received_at")
    available_at = _provider_time(page.get("available_at"), "page.available_at")
    mapping_body = {
        "observation_sha256": observation_sha256,
        "page_index": page_index,
        "page_object_sha256": page_object_sha256,
        "request_page_token": page.get("request_page_token"),
        "next_page_token": page.get("next_page_token"),
        "requested_at": _iso(requested_at),
        "received_at": _iso(received_at),
        "available_at": _iso(available_at),
        "terminal": page.get("terminal") is True,
    }
    _mapping_json, mapping_sha256 = _canonical_json_text(
        mapping_body, "fill_observation_page_mapping"
    )
    object_kwargs = {
        "page_object_sha256": page_object_sha256,
        "page_schema_version": object_body["page_schema_version"],
        "request_canonical_json": page.get("request_canonical_json"),
        "request_sha256": object_body["request_sha256"],
        "response_canonical_json": page.get("response_canonical_json"),
        "response_sha256": object_body["response_sha256"],
        "response_count": object_body["response_count"],
    }
    mapping_kwargs = {
        "observation_sha256": observation_sha256,
        "page_index": page_index,
        "page_object_sha256": page_object_sha256,
        "request_page_token": page.get("request_page_token"),
        "next_page_token": page.get("next_page_token"),
        "requested_at": requested_at,
        "received_at": received_at,
        "available_at": available_at,
        "terminal": page.get("terminal") is True,
        "mapping_sha256": mapping_sha256,
    }
    return object_kwargs, mapping_kwargs


def _activity_mapping_kwargs(
    *,
    observation_sha256: str,
    activity_ordinal: int,
    prepared: PreparedAlpacaPaperFillActivity,
    fill_event_sha256: str,
) -> dict[str, Any]:
    body = {
        "observation_sha256": observation_sha256,
        "activity_ordinal": activity_ordinal,
        "fill_event_sha256": fill_event_sha256,
        "immutable_fill_identity_sha256": (
            prepared.immutable_fill_identity_sha256
        ),
        "provider_activity_id": prepared.provider_activity_id,
        "provider_payload_sha256": prepared.provider_payload_sha256,
    }
    _canonical, mapping_sha256 = _canonical_json_text(
        body, "fill_observation_activity_mapping"
    )
    return {**body, "mapping_sha256": mapping_sha256}


def append_prepared_alpaca_paper_fill_batch(
    session: Session,
    batch: PreparedAlpacaPaperFillBatch,
) -> AlpacaPaperOrderFillCaptureResult:
    """Append a complete prepared batch with no adapter or network access.

    Verification occurs before touching the ``Session``. Publication then uses
    A1 -> A2 -> reservation -> fill ordering. The fill loop owns a savepoint,
    so an error after one insert cannot expose a prefix to the caller's outer
    transaction; a previously committed exact prefix remains retryable.
    """

    _verify_prepared_alpaca_paper_fill_batch(batch)
    try:
        pending = {
            "new": bool(session.new),
            "dirty": bool(session.dirty),
            "deleted": bool(session.deleted),
        }
    except (AttributeError, TypeError) as exc:
        raise AlpacaFillActivityError(
            "fill batch appender requires a real inspectable SQLAlchemy Session"
        ) from exc
    contaminated = sorted(name for name, present in pending.items() if present)
    if contaminated:
        raise AlpacaFillActivityError(
            "fill batch appender requires a pristine Session; pending "
            + ",".join(contaminated)
        )
    if not session.in_transaction():
        raise AlpacaFillActivityError(
            "caller must own an explicit transaction before fill batch append"
        )
    acquire_adaptive_risk_account_locks(
        session,
        account_scope=batch.cycle.account_scope,
    )
    reservation = session.scalar(
        select(AdaptiveRiskReservation)
        .where(
            AdaptiveRiskReservation.reservation_id == batch.cycle.reservation_id
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if reservation is None:
        raise AlpacaFillActivityConflict("adaptive reservation is missing")
    packet = session.get(
        AdaptiveRiskDecisionPacket,
        reservation.decision_packet_sha256,
    )
    if packet is None:
        raise AlpacaFillActivityConflict("immutable decision packet is missing")
    retained_cycle = AlpacaPaperFillCycleBinding.from_rows(
        reservation,
        packet,
    )
    if retained_cycle != batch.cycle:
        raise AlpacaFillActivityConflict(
            "prepared fill batch differs from locked adaptive cycle"
        )

    receipt = _verify_query_receipt(batch)
    pages = receipt.get("pages")
    if not isinstance(pages, list) or not pages:
        raise AlpacaFillActivityCorruption("fill observation has no page proof")
    observation_json, observation_sha256 = _observation_content(batch)
    terminal = receipt.get("terminal_proof")
    if not isinstance(terminal, Mapping):
        raise AlpacaFillActivityCorruption(
            "fill observation terminal proof is missing"
        )

    created = 0
    event_hashes: list[str] = []
    with session.begin_nested():
        existing_observation = session.scalar(
            select(AlpacaPaperFillQueryObservation)
            .where(
                AlpacaPaperFillQueryObservation.observation_sha256
                == observation_sha256
            )
            .with_for_update()
        )
        if existing_observation is not None:
            retained = {
                "observation_content_canonical_json": (
                    existing_observation.observation_content_canonical_json
                ),
                "query_receipt_canonical_json": (
                    existing_observation.query_receipt_canonical_json
                ),
                "read_binding_canonical_json": (
                    existing_observation.read_binding_canonical_json
                ),
                "provider_order_payload_canonical_json": (
                    existing_observation.provider_order_payload_canonical_json
                ),
            }
            expected = {
                "observation_content_canonical_json": observation_json,
                "query_receipt_canonical_json": batch.query_receipt_canonical_json,
                "read_binding_canonical_json": batch.read_binding_canonical_json,
                "provider_order_payload_canonical_json": (
                    batch.provider_order_payload_canonical_json
                ),
            }
            if retained != expected:
                raise AlpacaFillActivityCorruption(
                    "durable fill observation hash aliases different content"
                )
            page_mappings = list(
                session.scalars(
                    select(AlpacaPaperFillObservationPage)
                    .where(
                        AlpacaPaperFillObservationPage.observation_sha256
                        == observation_sha256
                    )
                    .order_by(AlpacaPaperFillObservationPage.page_index)
                    .with_for_update()
                )
            )
            if len(page_mappings) != len(pages):
                raise AlpacaFillActivityCorruption(
                    "durable fill observation page mapping is incomplete"
                )
            for retained_mapping, receipt_page in zip(
                page_mappings, pages, strict=True
            ):
                object_kwargs, mapping_kwargs = _page_rows_from_receipt(
                    observation_sha256=observation_sha256,
                    page=receipt_page,
                )
                page_object = session.get(
                    AlpacaPaperFillPageObject,
                    object_kwargs["page_object_sha256"],
                )
                if page_object is None or any(
                    getattr(page_object, key) != value
                    for key, value in object_kwargs.items()
                ):
                    raise AlpacaFillActivityCorruption(
                        "durable content-addressed fill page changed"
                    )
                if any(
                    getattr(retained_mapping, key) != value
                    for key, value in mapping_kwargs.items()
                ):
                    raise AlpacaFillActivityCorruption(
                        "durable fill observation page mapping changed"
                    )
            activity_mappings = list(
                session.scalars(
                    select(AlpacaPaperFillObservationActivity)
                    .where(
                        AlpacaPaperFillObservationActivity.observation_sha256
                        == observation_sha256
                    )
                    .order_by(
                        AlpacaPaperFillObservationActivity.activity_ordinal
                    )
                    .with_for_update()
                )
            )
            if len(activity_mappings) != len(batch.activities):
                raise AlpacaFillActivityCorruption(
                    "durable fill observation activity mapping is incomplete"
                )
            for mapping, prepared in zip(
                activity_mappings, batch.activities, strict=True
            ):
                fill_row = session.scalar(
                    select(AlpacaPaperFillActivity)
                    .where(
                        AlpacaPaperFillActivity.event_sha256
                        == mapping.fill_event_sha256
                    )
                    .with_for_update()
                )
                if fill_row is None:
                    raise AlpacaFillActivityCorruption(
                        "durable fill observation points to a missing fill"
                    )
                retained_prepared = verify_alpaca_paper_fill_activity_row(fill_row)
                expected_mapping = _activity_mapping_kwargs(
                    observation_sha256=observation_sha256,
                    activity_ordinal=int(mapping.activity_ordinal),
                    prepared=prepared,
                    fill_event_sha256=mapping.fill_event_sha256,
                )
                if (
                    retained_prepared.immutable_fill_identity_sha256
                    != prepared.immutable_fill_identity_sha256
                    or any(
                        getattr(mapping, key) != value
                        for key, value in expected_mapping.items()
                    )
                ):
                    raise AlpacaFillActivityCorruption(
                        "durable fill observation activity mapping changed"
                    )
                event_hashes.append(mapping.fill_event_sha256)
            return AlpacaPaperOrderFillCaptureResult(
                reservation_id=batch.cycle.reservation_id,
                provider_order_id=batch.provider_order_id,
                observed_count=len(batch.activities),
                created_count=0,
                event_sha256s=tuple(event_hashes),
            )

        observation = AlpacaPaperFillQueryObservation(
            observation_sha256=observation_sha256,
            observation_schema_version=(
                "chili.alpaca-paper-fill-query-observation.v1"
            ),
            observation_authority_status="verified",
            reservation_id=batch.cycle.reservation_id,
            decision_packet_sha256=batch.cycle.decision_packet_sha256,
            account_scope=batch.cycle.account_scope,
            account_identity_sha256=batch.cycle.account_identity_sha256,
            provider_account_id_sha256=batch.provider_account_id_sha256,
            broker_environment=batch.broker_environment,
            asset_class=batch.asset_class,
            execution_family=batch.cycle.execution_family,
            position_direction=batch.cycle.position_direction,
            symbol=batch.cycle.symbol,
            provider_order_id=batch.provider_order_id,
            expected_client_order_id=batch.expected_client_order_id,
            order_role=batch.order_role,
            account_snapshot_generation=batch.cycle.account_snapshot_generation,
            cycle_broker_connection_generation=(
                batch.cycle.broker_connection_generation
            ),
            adapter_connection_generation=batch.adapter_connection_generation,
            adapter_build_sha256=batch.adapter_build_sha256,
            provider_order_payload_canonical_json=(
                batch.provider_order_payload_canonical_json
            ),
            provider_order_payload_sha256=batch.provider_order_payload_sha256,
            read_binding_canonical_json=batch.read_binding_canonical_json,
            read_binding_sha256=batch.read_binding_sha256,
            query_receipt_canonical_json=batch.query_receipt_canonical_json,
            query_receipt_sha256=batch.query_receipt_sha256,
            observation_content_canonical_json=observation_json,
            observation_content_sha256=observation_sha256,
            query_after=batch.query_after,
            query_until=batch.query_until,
            received_at=batch.received_at,
            available_at=batch.available_at,
            expires_at=batch.expires_at,
            exact_activity_count=len(batch.activities),
            page_count=len(pages),
            pagination_complete=True,
            pagination_scope=_required_text(
                terminal.get("scope"), "terminal_proof.scope"
            ),
        )
        session.add(observation)
        session.flush([observation])

        for page in pages:
            object_kwargs, mapping_kwargs = _page_rows_from_receipt(
                observation_sha256=observation_sha256,
                page=page,
            )
            page_object = session.get(
                AlpacaPaperFillPageObject,
                object_kwargs["page_object_sha256"],
            )
            if page_object is None:
                page_object = AlpacaPaperFillPageObject(**object_kwargs)
                session.add(page_object)
                session.flush([page_object])
            else:
                retained_page = {
                    key: getattr(page_object, key) for key in object_kwargs
                }
                if retained_page != object_kwargs:
                    raise AlpacaFillActivityCorruption(
                        "content-addressed fill page aliases different bytes"
                    )
            session.add(AlpacaPaperFillObservationPage(**mapping_kwargs))

        for ordinal, prepared in enumerate(batch.activities):
            appended = _append_alpaca_paper_fill_activity_under_locked_cycle(
                session,
                prepared,
                reservation=reservation,
                packet=packet,
            )
            created += int(appended.created)
            event_hashes.append(appended.row.event_sha256)
            session.add(
                AlpacaPaperFillObservationActivity(
                    **_activity_mapping_kwargs(
                        observation_sha256=observation_sha256,
                        activity_ordinal=ordinal,
                        prepared=prepared,
                        fill_event_sha256=appended.row.event_sha256,
                    )
                )
            )
        session.flush()
    return AlpacaPaperOrderFillCaptureResult(
        reservation_id=batch.cycle.reservation_id,
        provider_order_id=batch.provider_order_id,
        observed_count=len(batch.activities),
        created_count=created,
        event_sha256s=tuple(event_hashes),
    )


_LIFECYCLE_EVIDENCE_PAYLOAD_FIELDS = frozenset(
    {
        "event_kind",
        "durability_kind",
        "provider_event_id",
        "broker_source",
        "connection_generation",
        "account_scope",
        "execution_family",
        "broker_environment",
        "account_identity_sha256",
        "client_order_id",
        "broker_order_id",
        "observed_at",
        "available_at",
        "event_content_sha256",
        "cumulative_filled_quantity",
        "source_record_table",
        "source_record_id",
        "order_status",
        "remaining_open_quantity",
        "evidence_sha256",
    }
)


def _durable_lifecycle_evidence_from_event(
    event: AdaptiveRiskReservationEvent,
) -> "DurableOrderLifecycleEvidence":
    from .adaptive_risk_reservation import DurableOrderLifecycleEvidence
    from .adaptive_risk_policy import AdaptiveRiskContractError

    payload = event.payload_json
    if not isinstance(payload, Mapping):
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle event payload is not an object"
        )
    details = payload.get("details")
    if not isinstance(details, Mapping):
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle event details are missing"
        )
    evidence_payload = details.get("lifecycle_evidence")
    if not isinstance(evidence_payload, Mapping):
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle evidence is missing"
        )
    if set(evidence_payload) != _LIFECYCLE_EVIDENCE_PAYLOAD_FIELDS:
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle evidence field set changed"
        )
    supplied_sha = _require_sha(
        evidence_payload.get("evidence_sha256"),
        "fill_handoff.lifecycle_evidence.evidence_sha256",
    )
    try:
        evidence = DurableOrderLifecycleEvidence(
            event_kind=evidence_payload.get("event_kind"),
            durability_kind=evidence_payload.get("durability_kind"),
            provider_event_id=evidence_payload.get("provider_event_id"),
            broker_source=evidence_payload.get("broker_source"),
            connection_generation=evidence_payload.get(
                "connection_generation"
            ),
            account_scope=evidence_payload.get("account_scope"),
            execution_family=evidence_payload.get("execution_family"),
            broker_environment=evidence_payload.get("broker_environment"),
            account_identity_sha256=evidence_payload.get(
                "account_identity_sha256"
            ),
            client_order_id=evidence_payload.get("client_order_id"),
            broker_order_id=evidence_payload.get("broker_order_id"),
            observed_at=_provider_time(
                evidence_payload.get("observed_at"),
                "fill_handoff.lifecycle_evidence.observed_at",
            ),
            available_at=_provider_time(
                evidence_payload.get("available_at"),
                "fill_handoff.lifecycle_evidence.available_at",
            ),
            event_content_sha256=evidence_payload.get(
                "event_content_sha256"
            ),
            cumulative_filled_quantity=evidence_payload.get(
                "cumulative_filled_quantity"
            ),
            source_record_table=evidence_payload.get("source_record_table"),
            source_record_id=evidence_payload.get("source_record_id"),
            order_status=evidence_payload.get("order_status"),
            remaining_open_quantity=evidence_payload.get(
                "remaining_open_quantity"
            ),
        )
    except (
        AdaptiveRiskContractError,
        AlpacaFillActivityError,
        TypeError,
        ValueError,
    ) as exc:
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle evidence failed typed reconstruction"
        ) from exc
    if (
        evidence.evidence_sha256 != supplied_sha
        or evidence.to_payload() != dict(evidence_payload)
    ):
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle evidence content hash changed"
        )
    return evidence


def _verify_adaptive_reservation_event_chain(
    session: Session,
    reservation: AdaptiveRiskReservation,
) -> dict[str, AdaptiveRiskReservationEvent]:
    rows = list(
        session.scalars(
            select(AdaptiveRiskReservationEvent)
            .where(
                AdaptiveRiskReservationEvent.reservation_id
                == reservation.reservation_id
            )
            .order_by(AdaptiveRiskReservationEvent.sequence)
            .with_for_update()
        )
    )
    previous: str | None = None
    by_sha: dict[str, AdaptiveRiskReservationEvent] = {}
    for sequence, row in enumerate(rows, start=1):
        if not isinstance(row.payload_json, Mapping):
            raise AlpacaFillActivityCorruption(
                "adaptive reservation event payload is not an object"
            )
        _, computed_sha = _canonical_json_text(
            row.payload_json,
            "adaptive_reservation_event",
        )
        payload = row.payload_json
        if not (
            int(row.sequence) == sequence
            and payload.get("sequence") == sequence
            and payload.get("reservation_id") == str(reservation.reservation_id)
            and payload.get("previous_event_sha256") == previous
            and row.previous_event_sha256 == previous
            and row.event_sha256 == computed_sha
            and payload.get("event_type") == row.event_type
            and payload.get("broker_event_id") == row.broker_event_id
        ):
            raise AlpacaFillActivityCorruption(
                "adaptive reservation event chain changed"
            )
        if row.event_sha256 in by_sha:
            raise AlpacaFillActivityCorruption(
                "adaptive reservation event hash is duplicated"
            )
        by_sha[row.event_sha256] = row
        previous = row.event_sha256
    if not (
        len(rows) == int(reservation.event_sequence)
        and reservation.last_event_sha256 == previous
    ):
        raise AlpacaFillActivityCorruption(
            "adaptive reservation event head changed"
        )
    return by_sha


def verify_alpaca_paper_entry_fill_handoff(
    session: Session,
    proof: AlpacaPaperEntryFillHandoffProof,
) -> AlpacaPaperEntryFillHandoffProof:
    """Verify a fill-to-transport handoff without broker or provider access."""

    if type(proof) is not AlpacaPaperEntryFillHandoffProof:
        raise AlpacaFillActivityError(
            "fill-handoff proof must be the exact typed contract"
        )
    canonical_proof = AlpacaPaperEntryFillHandoffProof.from_canonical_json(
        proof.to_canonical_json()
    )
    if canonical_proof != proof:
        raise AlpacaFillActivityCorruption(
            "fill-handoff proof changed during canonical reconstruction"
        )
    if not session.in_transaction():
        raise AlpacaFillActivityError(
            "fill-handoff verification requires a caller-owned transaction"
        )

    from .adaptive_risk_reservation import AdaptiveRiskReservationStore

    acquire_adaptive_risk_account_locks(
        session,
        account_scope=proof.account_scope,
    )
    reservation = session.scalar(
        select(AdaptiveRiskReservation)
        .where(
            AdaptiveRiskReservation.reservation_id == proof.reservation_id
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if reservation is None:
        raise AlpacaFillActivityConflict(
            "fill-handoff adaptive reservation is missing"
        )
    packet = session.scalar(
        select(AdaptiveRiskDecisionPacket)
        .where(
            AdaptiveRiskDecisionPacket.decision_packet_sha256
            == reservation.decision_packet_sha256
        )
    )
    if packet is None:
        raise AlpacaFillActivityConflict(
            "fill-handoff immutable decision packet is missing"
        )
    event_chain = _verify_adaptive_reservation_event_chain(
        session,
        reservation,
    )
    event = event_chain.get(proof.lifecycle_event_sha256)
    if event is None:
        raise AlpacaFillActivityConflict(
            "fill-handoff lifecycle event is missing"
        )
    evidence = _durable_lifecycle_evidence_from_event(event)
    event_payload = dict(event.payload_json or {})
    allowed_event_types = {
        "cumulative_fill_advanced",
        "late_cumulative_fill_quarantined",
        "cumulative_overfill_quarantined",
        "quarantined_cumulative_fill_advanced",
    }
    exact = {
        "decision_packet_sha256": (
            proof.decision_packet_sha256,
            packet.decision_packet_sha256,
        ),
        "reservation_packet": (
            reservation.decision_packet_sha256,
            packet.decision_packet_sha256,
        ),
        "account_scope": (proof.account_scope, packet.account_scope),
        "account_identity_sha256": (
            proof.account_identity_sha256,
            packet.account_identity_sha256,
        ),
        "client_order_id": (proof.client_order_id, packet.client_order_id),
        "broker_order_id": (
            proof.broker_order_id,
            reservation.broker_order_id,
        ),
        "broker_connection_generation": (
            proof.broker_connection_generation,
            reservation.broker_connection_generation,
        ),
        "event_reservation_id": (
            event.reservation_id,
            reservation.reservation_id,
        ),
        "event_sequence": (
            int(event.sequence),
            proof.lifecycle_event_sequence,
        ),
        "event_type": (event.event_type in allowed_event_types, True),
        "event_provider_id": (
            event.broker_event_id,
            proof.lifecycle_provider_event_id,
        ),
        "event_payload_state": (
            event_payload.get("state"),
            proof.resulting_reservation_state,
        ),
        "event_payload_cumulative": (
            event_payload.get("cumulative_filled_quantity_shares"),
            proof.cumulative_filled_quantity_shares,
        ),
        "evidence_sha256": (
            evidence.evidence_sha256,
            proof.terminal_evidence_sha256,
        ),
        "evidence_provider_id": (
            evidence.provider_event_id,
            proof.lifecycle_provider_event_id,
        ),
        "evidence_durability": (
            evidence.durability_kind,
            proof.durability_kind,
        ),
        "evidence_source_table": (
            evidence.source_record_table,
            proof.source_record_table,
        ),
        "evidence_source_id": (
            evidence.source_record_id,
            proof.source_record_id,
        ),
        "evidence_cumulative": (
            evidence.cumulative_filled_quantity,
            proof.cumulative_filled_quantity_shares,
        ),
        "evidence_observed_at": (evidence.observed_at, proof.observed_at),
        "evidence_available_at": (evidence.available_at, proof.available_at),
        "reservation_cumulative_floor": (
            int(reservation.cumulative_filled_quantity_shares)
            >= proof.cumulative_filled_quantity_shares,
            True,
        ),
    }
    if int(reservation.event_sequence) == proof.lifecycle_event_sequence:
        exact["reservation_result_state"] = (
            reservation.state,
            proof.resulting_reservation_state,
        )
        exact["reservation_result_cumulative"] = (
            int(reservation.cumulative_filled_quantity_shares),
            proof.cumulative_filled_quantity_shares,
        )
    changed = sorted(
        name for name, (actual, expected) in exact.items()
        if actual != expected
    )
    if changed:
        raise AlpacaFillActivityConflict(
            "fill-handoff durable identity changed: " + ",".join(changed)
        )

    if proof.publication_kind == "active_cycle_fill":
        source = session.scalar(
            select(AlpacaPaperFillActivity)
            .where(
                AlpacaPaperFillActivity.event_sha256
                == proof.source_record_id
            )
            .with_for_update()
        )
        mapping = session.scalar(
            select(AlpacaPaperFillObservationActivity)
            .where(
                AlpacaPaperFillObservationActivity.observation_sha256
                == proof.observation_sha256,
                AlpacaPaperFillObservationActivity.fill_event_sha256
                == proof.source_record_id,
            )
            .with_for_update()
        )
        observation = session.scalar(
            select(AlpacaPaperFillQueryObservation)
            .where(
                AlpacaPaperFillQueryObservation.observation_sha256
                == proof.observation_sha256
            )
            .with_for_update()
        )
        if source is None or mapping is None or observation is None:
            raise AlpacaFillActivityConflict(
                "fill-handoff exact fill observation is missing"
            )
        verify_alpaca_paper_fill_activity_row(source)
        if not (
            source.immutable_fill_identity_sha256
            == proof.immutable_fill_identity_sha256
            and mapping.immutable_fill_identity_sha256
            == proof.immutable_fill_identity_sha256
            and observation.observation_sha256 == proof.observation_sha256
        ):
            raise AlpacaFillActivityConflict(
                "fill-handoff immutable execution identity changed"
            )
    else:
        source = session.scalar(
            select(AlpacaPaperPostSettlementFillContradiction)
            .where(
                AlpacaPaperPostSettlementFillContradiction.contradiction_sha256
                == proof.source_record_id
            )
            .with_for_update()
        )
        if source is None:
            raise AlpacaFillActivityConflict(
                "fill-handoff contradiction source is missing"
            )
        verify_alpaca_paper_post_settlement_fill_contradiction_row(source)
        if not (
            bool(source.is_projection_terminal)
            and source.observation_sha256 == proof.observation_sha256
            and source.immutable_fill_identity_sha256
            == proof.immutable_fill_identity_sha256
        ):
            raise AlpacaFillActivityConflict(
                "fill-handoff contradiction execution identity changed"
            )

    bind = session.get_bind()
    store = AdaptiveRiskReservationStore(getattr(bind, "engine", bind))
    store._validate_lifecycle_evidence(
        session,
        reservation,
        evidence,
        expected_event_kind="cumulative_fill",
        idempotent_replay=True,
    )
    store._verify_existing_lifecycle_event(
        event,
        evidence,
        expected_event_type=event.event_type,
    )
    return proof


def _build_alpaca_paper_entry_fill_handoff(
    session: Session,
    *,
    publication_kind: str,
    reservation_id: uuid.UUID,
    lifecycle_provider_event_id: str,
    observation_sha256: str,
    immutable_fill_identity_sha256: str,
) -> AlpacaPaperEntryFillHandoffProof:
    session.flush()
    reservation = session.scalar(
        select(AdaptiveRiskReservation)
        .where(AdaptiveRiskReservation.reservation_id == reservation_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if reservation is None:
        raise AlpacaFillActivityCorruption(
            "fill-handoff reservation disappeared after publication"
        )
    packet = session.get(
        AdaptiveRiskDecisionPacket,
        reservation.decision_packet_sha256,
    )
    event = session.scalar(
        select(AdaptiveRiskReservationEvent)
        .where(
            AdaptiveRiskReservationEvent.reservation_id == reservation_id,
            AdaptiveRiskReservationEvent.broker_event_id
            == lifecycle_provider_event_id,
        )
        .with_for_update()
    )
    if packet is None or event is None:
        raise AlpacaFillActivityCorruption(
            "fill-handoff lifecycle publication is incomplete"
        )
    evidence = _durable_lifecycle_evidence_from_event(event)
    state = (event.payload_json or {}).get("state")
    proof = AlpacaPaperEntryFillHandoffProof(
        schema_version=ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION,
        publication_kind=publication_kind,
        reservation_id=reservation.reservation_id,
        decision_packet_sha256=packet.decision_packet_sha256,
        account_scope=packet.account_scope,
        account_identity_sha256=packet.account_identity_sha256,
        client_order_id=packet.client_order_id,
        broker_order_id=evidence.broker_order_id,
        broker_connection_generation=evidence.connection_generation,
        observation_sha256=observation_sha256,
        durability_kind=evidence.durability_kind,
        source_record_table=evidence.source_record_table,
        source_record_id=evidence.source_record_id,
        terminal_evidence_sha256=evidence.evidence_sha256,
        immutable_fill_identity_sha256=immutable_fill_identity_sha256,
        cumulative_filled_quantity_shares=(
            evidence.cumulative_filled_quantity
        ),
        lifecycle_provider_event_id=evidence.provider_event_id,
        lifecycle_event_sha256=event.event_sha256,
        lifecycle_event_sequence=int(event.sequence),
        resulting_reservation_state=state,
        observed_at=evidence.observed_at,
        available_at=evidence.available_at,
    )
    return verify_alpaca_paper_entry_fill_handoff(session, proof)


def _publish_post_settlement_entry_fill_contradictions(
    session: Session,
    batch: PreparedAlpacaPaperFillBatch,
    *,
    reservation: AdaptiveRiskReservation,
    packet: AdaptiveRiskDecisionPacket,
    settlement: AlpacaPaperCycleSettlement,
) -> AlpacaPaperEntryFillPublicationResult:
    """Append an exact late-fill lineage without touching the settled chain."""

    from .adaptive_risk_reservation import AdaptiveRiskReservationStore
    from .alpaca_cycle_settlement import verify_cycle_settlement_content

    verify_cycle_settlement_content(settlement)
    retained_cycle = AlpacaPaperFillCycleBinding.from_rows(reservation, packet)
    if retained_cycle != batch.cycle:
        raise AlpacaFillActivityConflict(
            "post-settlement batch differs from locked adaptive cycle"
        )
    if reservation.state not in {"closed", "exposure_quarantined"}:
        raise AlpacaFillActivityConflict(
            "post-settlement fill requires a closed or quarantined cycle"
        )
    if not (
        settlement.reservation_id == reservation.reservation_id
        and settlement.decision_packet_sha256 == packet.decision_packet_sha256
        and settlement.account_scope == packet.account_scope == "alpaca:paper"
        and settlement.account_identity_sha256
        == packet.account_identity_sha256
        and settlement.broker_connection_generation
        == reservation.broker_connection_generation
        and Decimal(settlement.entry_quantity)
        == Decimal(settlement.exit_quantity)
    ):
        raise AlpacaFillActivityConflict(
            "post-settlement fill differs from immutable settlement"
        )
    if not batch.activities:
        raise AlpacaFillActivityConflict(
            "post-settlement fill batch has no execution"
        )
    provider_order = _reparse_canonical_json(
        batch.provider_order_payload_canonical_json,
        batch.provider_order_payload_sha256,
        "post_settlement.provider_order",
    )
    provider_quantity = _whole_share_quantity(
        provider_order.get("qty"),
        "post_settlement.provider_order.qty",
        positive=True,
    )
    if provider_quantity != int(reservation.planned_quantity_shares):
        raise AlpacaFillActivityConflict(
            "post-settlement provider quantity differs from adaptive plan"
        )

    capability_verified_at = datetime.now(UTC)
    try:
        verify_alpaca_fill_read_capability(
            batch.read_capability,
            expected_payload=batch.capability_payload(),
            verified_at=capability_verified_at,
        )
    except AlpacaFillReadCapabilityError as exc:
        raise AlpacaFillActivityCorruption(
            "post-settlement fill lacks current process-private read authority"
        ) from exc
    receipt = _verify_query_receipt(batch)
    observation_json, observation_sha256 = _observation_content(batch)
    if observation_sha256 != batch.batch_content_sha256:
        raise AlpacaFillActivityCorruption(
            "post-settlement observation differs from prepared batch"
        )

    settled_rows = list(
        session.scalars(
            select(AlpacaPaperFillActivity)
            .where(
                AlpacaPaperFillActivity.reservation_id
                == reservation.reservation_id
            )
            .order_by(AlpacaPaperFillActivity.sequence)
            .with_for_update()
        )
    )
    verify_alpaca_paper_fill_activity_chain(settled_rows)
    contradiction_rows = list(
        session.scalars(
            select(AlpacaPaperPostSettlementFillContradiction)
            .where(
                AlpacaPaperPostSettlementFillContradiction.reservation_id
                == reservation.reservation_id
            )
            .order_by(
                AlpacaPaperPostSettlementFillContradiction.contradiction_sequence
            )
            .with_for_update()
        )
    )
    previous_contradiction: str | None = None
    for sequence, row in enumerate(contradiction_rows, start=1):
        verify_alpaca_paper_post_settlement_fill_contradiction_row(row)
        if not (
            int(row.contradiction_sequence) == sequence
            and row.previous_contradiction_sha256 == previous_contradiction
            and row.settlement_sha256 == settlement.settlement_sha256
        ):
            raise AlpacaFillActivityCorruption(
                "post-settlement contradiction lineage changed"
            )
        previous_contradiction = row.contradiction_sha256

    known: dict[str, str] = {}
    for row in settled_rows:
        if row.order_role != "entry" or row.provider_order_id != batch.provider_order_id:
            continue
        if row.provider_activity_id in known:
            raise AlpacaFillActivityCorruption(
                "settled entry fill identity is duplicated"
            )
        known[row.provider_activity_id] = row.immutable_fill_identity_sha256
    for row in contradiction_rows:
        retained = known.setdefault(
            row.provider_activity_id,
            row.immutable_fill_identity_sha256,
        )
        if retained != row.immutable_fill_identity_sha256:
            raise AlpacaFillActivityCorruption(
                "post-settlement provider activity aliases another execution"
            )

    prepared_by_id = {
        item.provider_activity_id: item for item in batch.activities
    }
    if len(prepared_by_id) != len(batch.activities) or not set(known).issubset(
        prepared_by_id
    ):
        raise AlpacaFillActivityConflict(
            "post-settlement complete broker history omits durable executions"
        )
    new_items: list[PreparedAlpacaPaperFillActivity] = []
    missing_started = False
    for item in batch.activities:
        retained_identity = known.get(item.provider_activity_id)
        if retained_identity is None:
            missing_started = True
            new_items.append(item)
            continue
        if missing_started:
            raise AlpacaFillActivityConflict(
                "post-settlement broker history is not a contiguous suffix"
            )
        if retained_identity != item.immutable_fill_identity_sha256:
            raise AlpacaFillActivityConflict(
                "post-settlement provider activity identity changed"
            )

    store = AdaptiveRiskReservationStore(session.get_bind())
    if not new_items:
        if reservation.state == "closed":
            raise AlpacaFillActivityConflict(
                "closed cycle has no new post-settlement execution"
            )
        if not contradiction_rows or not bool(
            contradiction_rows[-1].is_projection_terminal
        ):
            raise AlpacaFillActivityCorruption(
                "quarantined cycle lacks its terminal contradiction"
            )
        terminal_row = contradiction_rows[-1]
        state = store.apply_post_settlement_fill_contradiction(
            reservation.reservation_id,
            contradiction_sha256=terminal_row.contradiction_sha256,
            session=session,
        )
        provider_event_id = (
            f"alpaca-post-settlement-fill:{terminal_row.contradiction_sha256}"
        )
        proof = _build_alpaca_paper_entry_fill_handoff(
            session,
            publication_kind="post_settlement_contradiction",
            reservation_id=reservation.reservation_id,
            lifecycle_provider_event_id=provider_event_id,
            observation_sha256=terminal_row.observation_sha256,
            immutable_fill_identity_sha256=(
                terminal_row.immutable_fill_identity_sha256
            ),
        )
        return AlpacaPaperEntryFillPublicationResult(
            capture=None,
            reservation_state=state,
            publication_kind="post_settlement_contradiction",
            contradiction_sha256s=(),
            settlement_sha256=settlement.settlement_sha256,
            handoff_proof=proof,
        )

    projection_prior = int(reservation.cumulative_filled_quantity_shares)
    terminal_cumulative = _whole_share_quantity(
        new_items[-1].cumulative_quantity,
        "post_settlement.terminal_cumulative_quantity",
        positive=True,
    )
    projection_delta = terminal_cumulative - projection_prior
    if projection_delta <= 0:
        raise AlpacaFillActivityConflict(
            "post-settlement fill does not increase durable exposure"
        )
    first_prior = _whole_share_quantity(
        new_items[0].cumulative_quantity,
        "post_settlement.first_cumulative_quantity",
        positive=True,
    ) - _whole_share_quantity(
        new_items[0].quantity,
        "post_settlement.first_quantity",
        positive=True,
    )
    if first_prior != projection_prior:
        raise AlpacaFillActivityConflict(
            "post-settlement fill suffix does not start at durable cumulative truth"
        )

    planned = Decimal(int(reservation.planned_quantity_shares))

    def project(planned_value: Any, open_value: Any) -> Decimal:
        increment = (
            Decimal(planned_value) / planned * Decimal(projection_delta)
        ).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return (Decimal(open_value) + increment).quantize(
            _MONEY_QUANTUM, rounding=ROUND_HALF_EVEN
        )

    projected_open_quantity = int(reservation.open_quantity_shares) + projection_delta
    projected_open_risk = project(
        reservation.planned_structural_risk_usd,
        reservation.open_structural_risk_usd,
    )
    projected_open_gross = project(
        reservation.planned_gross_notional_usd,
        reservation.open_gross_notional_usd,
    )
    projected_open_bp = project(
        reservation.planned_buying_power_impact_usd,
        reservation.open_buying_power_impact_usd,
    )

    next_sequence = len(contradiction_rows) + 1
    prior_cumulative = projection_prior
    previous_sha = previous_contradiction
    created_hashes: list[str] = []
    for ordinal, item in enumerate(new_items):
        quantity = _whole_share_quantity(
            item.quantity,
            f"post_settlement.activities[{ordinal}].quantity",
            positive=True,
        )
        cumulative = _whole_share_quantity(
            item.cumulative_quantity,
            f"post_settlement.activities[{ordinal}].cumulative_quantity",
            positive=True,
        )
        if cumulative != prior_cumulative + quantity:
            raise AlpacaFillActivityConflict(
                "post-settlement fill suffix cumulative quantity is not exact"
            )
        terminal = ordinal == len(new_items) - 1
        values = {
            "contradiction_schema_version": (
                POST_SETTLEMENT_FILL_CONTRADICTION_SCHEMA_VERSION
            ),
            "authority_status": "verified",
            "durability_kind": (
                POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
            ),
            "reservation_id": reservation.reservation_id,
            "settlement_sha256": settlement.settlement_sha256,
            "decision_packet_sha256": packet.decision_packet_sha256,
            "account_scope": packet.account_scope,
            "account_identity_sha256": packet.account_identity_sha256,
            "broker_environment": packet.broker_environment,
            "execution_family": packet.execution_family,
            "symbol": packet.symbol,
            "expected_client_order_id": packet.client_order_id,
            "broker_order_id": reservation.broker_order_id,
            "broker_connection_generation": (
                reservation.broker_connection_generation
            ),
            "source_state": reservation.state,
            "settlement_terminal_fill_event_sha256": (
                settlement.terminal_fill_event_sha256
            ),
            "settlement_source_fill_count": int(settlement.source_fill_count),
            "settlement_entry_quantity": Decimal(settlement.entry_quantity),
            "settlement_exit_quantity": Decimal(settlement.exit_quantity),
            "settlement_net_realized_pnl_usd": Decimal(
                settlement.net_realized_pnl_usd
            ),
            "contradiction_sequence": next_sequence + ordinal,
            "previous_contradiction_sha256": previous_sha,
            "batch_activity_ordinal": ordinal,
            "batch_activity_count": len(new_items),
            "is_projection_terminal": terminal,
            "batch_content_sha256": batch.batch_content_sha256,
            "observation_sha256": observation_sha256,
            "provider_order_payload_sha256": (
                batch.provider_order_payload_sha256
            ),
            "query_receipt_sha256": batch.query_receipt_sha256,
            "read_binding_sha256": batch.read_binding_sha256,
            "adapter_connection_generation": (
                batch.adapter_connection_generation
            ),
            "adapter_build_sha256": batch.adapter_build_sha256,
            "capability_authority_status": (
                "process_private_hmac_verified_before_commit"
            ),
            "capability_verified_at": capability_verified_at,
            "query_after": batch.query_after,
            "query_until": batch.query_until,
            "query_received_at": batch.received_at,
            "query_available_at": batch.available_at,
            "query_expires_at": batch.expires_at,
            "provider_activity_id": item.provider_activity_id,
            "immutable_fill_identity_sha256": (
                item.immutable_fill_identity_sha256
            ),
            "provider_order_id": item.provider_order_id,
            "provider_payload_sha256": item.provider_payload_sha256,
            "provider_transaction_at": item.provider_transaction_at,
            "provider_available_at": batch.available_at,
            "side": item.side,
            "order_role": item.order_role,
            "order_ownership_status": item.order_ownership_status,
            "quantity": item.quantity,
            "price": item.price,
            "leaves_quantity": item.leaves_quantity,
            "prior_recorded_cumulative_quantity": Decimal(prior_cumulative),
            "broker_observed_cumulative_quantity": Decimal(cumulative),
            "positive_fill_delta": Decimal(quantity),
            "projection_prior_cumulative_quantity": (
                Decimal(projection_prior) if terminal else None
            ),
            "projection_positive_fill_delta": (
                Decimal(projection_delta) if terminal else None
            ),
            "projected_open_quantity_shares": (
                projected_open_quantity if terminal else None
            ),
            "projected_open_structural_risk_usd": (
                projected_open_risk if terminal else None
            ),
            "projected_open_gross_notional_usd": (
                projected_open_gross if terminal else None
            ),
            "projected_open_buying_power_impact_usd": (
                projected_open_bp if terminal else None
            ),
            "fee_status": item.fee_status,
            "fee_usd": item.fee_usd,
            "fee_evidence_sha256": item.fee_evidence_sha256,
        }
        body = _post_settlement_contradiction_content_body(values)
        contradiction_json, contradiction_sha = _canonical_json_text(
            body, "post_settlement_fill_contradiction"
        )
        row = AlpacaPaperPostSettlementFillContradiction(
            **values,
            batch_content_canonical_json=observation_json,
            observation_content_canonical_json=observation_json,
            provider_order_payload_canonical_json=(
                batch.provider_order_payload_canonical_json
            ),
            query_receipt_canonical_json=batch.query_receipt_canonical_json,
            read_binding_canonical_json=batch.read_binding_canonical_json,
            provider_payload_canonical_json=(
                item.provider_payload_canonical_json
            ),
            fee_evidence_canonical_json=item.fee_evidence_canonical_json,
            contradiction_content_canonical_json=contradiction_json,
            contradiction_content_sha256=contradiction_sha,
            contradiction_sha256=contradiction_sha,
        )
        verify_alpaca_paper_post_settlement_fill_contradiction_row(row)
        session.add(row)
        session.flush([row])
        created_hashes.append(contradiction_sha)
        previous_sha = contradiction_sha
        prior_cumulative = cumulative

    terminal_row = session.get(
        AlpacaPaperPostSettlementFillContradiction,
        created_hashes[-1],
    )
    if terminal_row is None:
        raise AlpacaFillActivityCorruption(
            "terminal post-settlement contradiction disappeared"
        )
    state = store.apply_post_settlement_fill_contradiction(
        reservation.reservation_id,
        contradiction_sha256=terminal_row.contradiction_sha256,
        session=session,
    )
    provider_event_id = (
        f"alpaca-post-settlement-fill:{terminal_row.contradiction_sha256}"
    )
    proof = _build_alpaca_paper_entry_fill_handoff(
        session,
        publication_kind="post_settlement_contradiction",
        reservation_id=reservation.reservation_id,
        lifecycle_provider_event_id=provider_event_id,
        observation_sha256=terminal_row.observation_sha256,
        immutable_fill_identity_sha256=(
            terminal_row.immutable_fill_identity_sha256
        ),
    )
    return AlpacaPaperEntryFillPublicationResult(
        capture=None,
        reservation_state=state,
        publication_kind="post_settlement_contradiction",
        contradiction_sha256s=tuple(created_hashes),
        settlement_sha256=settlement.settlement_sha256,
        handoff_proof=proof,
    )


def publish_prepared_alpaca_paper_entry_fill_batch(
    session: Session,
    batch: PreparedAlpacaPaperFillBatch,
) -> AlpacaPaperEntryFillPublicationResult:
    """Atomically publish exact entry fills and advance adaptive fill truth.

    The broker read has already completed and is carried by ``batch``.  This
    function has no adapter/network seam.  A savepoint encloses both immutable
    observation publication and the reservation/opportunity watermark, so a
    caller that catches an exception cannot commit one without the other.
    Duplicate pages/fills replay idempotently through their existing hashes.
    """

    _verify_prepared_alpaca_paper_fill_batch(batch)
    if batch.order_role != "entry":
        raise AlpacaFillActivityError(
            "entry-fill publication rejects exit orders; exit lineage is separate"
        )
    if not session.in_transaction():
        raise AlpacaFillActivityError(
            "entry-fill publication requires a caller-owned transaction"
        )

    from .adaptive_risk_reservation import (
        AdaptiveRiskReservationStore,
        DurableOrderLifecycleEvidence,
    )

    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    store = AdaptiveRiskReservationStore(engine)
    with session.begin_nested():
        acquire_adaptive_risk_account_locks(
            session,
            account_scope=batch.cycle.account_scope,
        )
        reservation = session.scalar(
            select(AdaptiveRiskReservation)
            .where(
                AdaptiveRiskReservation.reservation_id
                == batch.cycle.reservation_id
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if reservation is None:
            raise AlpacaFillActivityConflict("adaptive reservation is missing")
        packet = session.get(
            AdaptiveRiskDecisionPacket,
            reservation.decision_packet_sha256,
        )
        if packet is None:
            raise AlpacaFillActivityConflict(
                "immutable decision packet is missing"
            )
        settlement = session.scalar(
            select(AlpacaPaperCycleSettlement)
            .where(
                AlpacaPaperCycleSettlement.reservation_id
                == reservation.reservation_id
            )
            .with_for_update()
        )
        if settlement is not None:
            return _publish_post_settlement_entry_fill_contradictions(
                session,
                batch,
                reservation=reservation,
                packet=packet,
                settlement=settlement,
            )

        provider_order = _reparse_canonical_json(
            batch.provider_order_payload_canonical_json,
            batch.provider_order_payload_sha256,
            "provider_order",
        )
        provider_quantity = _whole_share_quantity(
            provider_order.get("qty"), "provider_order.qty", positive=True
        )
        if provider_quantity != int(reservation.planned_quantity_shares):
            raise AlpacaFillActivityConflict(
                "provider order quantity differs from adaptive reservation"
            )

        broker_bindings = (
            reservation.broker_source,
            reservation.broker_connection_generation,
            reservation.broker_order_id,
            reservation.last_broker_observed_at,
            reservation.last_broker_available_at,
            reservation.last_source_event_content_sha256,
        )
        fully_unbound = all(value is None for value in broker_bindings)
        fully_bound = all(value is not None for value in broker_bindings)
        if not fully_unbound and not fully_bound:
            raise AlpacaFillActivityConflict(
                "adaptive broker lifecycle binding is partial"
            )
        if fully_unbound:
            prospective = (
                AlpacaPaperFillCycleBinding.from_unbound_fill_bearing_rows(
                    reservation,
                    packet,
                    broker_connection_generation=(
                        batch.cycle.broker_connection_generation
                    ),
                    entry_provider_order_id=batch.provider_order_id,
                )
            )
            if prospective != batch.cycle:
                raise AlpacaFillActivityConflict(
                    "fill-bearing cycle differs from unbound adaptive identity"
                )
            if not batch.activities:
                state = store.lock_reservation(
                    batch.cycle.reservation_id,
                    session=session,
                )
                return AlpacaPaperEntryFillPublicationResult(
                    capture=None,
                    reservation_state=state,
                )

            # PostgreSQL's immediate fill-cycle guard deliberately refuses an
            # execution whose OID/generation differs from the locked adaptive
            # reservation.  Stage the *complete* broker identity from this
            # already verified positive-fill batch before appending it.  The
            # enclosing savepoint also contains the immutable observation,
            # fill rows and adaptive cumulative watermark, so no staged-only
            # identity can commit if any later proof or projection fails.
            terminal_prepared = batch.activities[-1]
            reservation.broker_source = "alpaca"
            reservation.broker_connection_generation = (
                batch.cycle.broker_connection_generation
            )
            reservation.broker_order_id = batch.provider_order_id
            reservation.last_broker_observed_at = (
                terminal_prepared.provider_transaction_at
            )
            reservation.last_broker_available_at = batch.available_at
            reservation.last_source_event_content_sha256 = (
                batch.batch_content_sha256
            )
            session.flush([reservation])

        captured = append_prepared_alpaca_paper_fill_batch(
            session,
            batch,
        )

        if not batch.activities:
            state = store.lock_reservation(
                batch.cycle.reservation_id,
                session=session,
            )
            return AlpacaPaperEntryFillPublicationResult(
                capture=captured,
                reservation_state=state,
            )
        if len(captured.event_sha256s) != len(batch.activities):
            raise AlpacaFillActivityCorruption(
                "entry-fill publication lost an activity mapping"
            )
        terminal_event_sha256 = captured.event_sha256s[-1]
        terminal_fill = session.scalar(
            select(AlpacaPaperFillActivity)
            .where(
                AlpacaPaperFillActivity.event_sha256
                == terminal_event_sha256
            )
            .with_for_update()
        )
        if terminal_fill is None:
            raise AlpacaFillActivityCorruption(
                "entry-fill publication terminal activity is missing"
            )
        observation = session.scalar(
            select(AlpacaPaperFillQueryObservation)
            .where(
                AlpacaPaperFillQueryObservation.observation_sha256
                == batch.batch_content_sha256
            )
            .with_for_update()
        )
        if observation is None:
            raise AlpacaFillActivityCorruption(
                "entry-fill publication exact observation is missing"
            )
        prepared_terminal = verify_alpaca_paper_fill_activity_row(terminal_fill)
        cumulative = _whole_share_quantity(
            terminal_fill.cumulative_quantity,
            "terminal_fill.cumulative_quantity",
            positive=True,
        )
        if (
            prepared_terminal.order_role != "entry"
            or prepared_terminal.provider_order_id != batch.provider_order_id
            or prepared_terminal.provider_client_order_id
            != batch.expected_client_order_id
        ):
            raise AlpacaFillActivityConflict(
                "terminal entry fill differs from prepared order binding"
            )
        evidence = DurableOrderLifecycleEvidence(
            event_kind="cumulative_fill",
            durability_kind="committed_alpaca_paper_fill",
            provider_event_id=(
                f"alpaca-fill:{terminal_fill.event_sha256}:observation:"
                f"{observation.observation_sha256}"
            ),
            broker_source="alpaca",
            connection_generation=batch.cycle.broker_connection_generation,
            account_scope=batch.cycle.account_scope,
            execution_family=batch.cycle.execution_family,
            broker_environment="paper",
            account_identity_sha256=batch.cycle.account_identity_sha256,
            client_order_id=batch.expected_client_order_id,
            broker_order_id=batch.provider_order_id,
            observed_at=terminal_fill.provider_transaction_at,
            available_at=observation.available_at,
            event_content_sha256=terminal_fill.event_sha256,
            cumulative_filled_quantity=cumulative,
            source_record_table="alpaca_paper_fill_activities",
            source_record_id=terminal_fill.event_sha256,
            order_status=(
                "filled"
                if cumulative >= int(reservation.planned_quantity_shares)
                else "partially_filled"
            ),
        )
        state = store.apply_cumulative_fill(
            batch.cycle.reservation_id,
            evidence=evidence,
            session=session,
        )
        proof = _build_alpaca_paper_entry_fill_handoff(
            session,
            publication_kind="active_cycle_fill",
            reservation_id=batch.cycle.reservation_id,
            lifecycle_provider_event_id=evidence.provider_event_id,
            observation_sha256=observation.observation_sha256,
            immutable_fill_identity_sha256=(
                terminal_fill.immutable_fill_identity_sha256
            ),
        )
        return AlpacaPaperEntryFillPublicationResult(
            capture=captured,
            reservation_state=state,
            handoff_proof=proof,
        )


def capture_verified_alpaca_paper_order_fills(
    session: Session,
    *,
    adapter: Any,
    reservation_id: uuid.UUID,
    provider_order_id: str,
    expected_exit_client_order_id: str | None = None,
) -> AlpacaPaperOrderFillCaptureResult:
    """Disabled legacy composition.

    This shape necessarily carries a live ``Session`` across broker I/O and
    accepts only a caller CID for exit ownership. Production callers must be
    migrated to explicit read -> commit-free handoff -> pristine append phases
    with a sealed durable exit-owner receipt.
    """

    del session, adapter, reservation_id, provider_order_id
    del expected_exit_client_order_id
    raise AlpacaFillActivityError(
        "combined Alpaca fill capture is unsafe and disabled; use split phases"
    )


def _terminal_fill_observation_content(
    *,
    settlement: AlpacaPaperCycleSettlement,
    observation: AlpacaPaperFillQueryObservation,
    terminal_fill: AlpacaPaperFillActivity,
) -> tuple[str, str]:
    identity_sha256 = _require_sha(
        terminal_fill.immutable_fill_identity_sha256,
        "terminal_fill.immutable_fill_identity_sha256",
    )
    body = {
        "receipt_schema_version": (
            "chili.alpaca-paper-terminal-fill-observation.v1"
        ),
        "authority_status": "verified",
        "settlement_sha256": settlement.settlement_sha256,
        "reservation_id": str(settlement.reservation_id),
        "account_scope": settlement.account_scope,
        "account_identity_sha256": settlement.account_identity_sha256,
        "observation_sha256": observation.observation_sha256,
        "terminal_fill_event_sha256": terminal_fill.event_sha256,
        "immutable_fill_identity_sha256": identity_sha256,
        "query_receipt_sha256": observation.query_receipt_sha256,
        "read_binding_sha256": observation.read_binding_sha256,
        "adapter_connection_generation": (
            observation.adapter_connection_generation
        ),
        "adapter_build_sha256": observation.adapter_build_sha256,
        "terminal_fill_observed_at": _iso(terminal_fill.provider_transaction_at),
        "terminal_fill_available_at": _iso(observation.available_at),
    }
    return _canonical_json_text(body, "terminal_fill_observation_receipt")


def verify_alpaca_paper_terminal_fill_observation_receipt(
    receipt: AlpacaPaperTerminalFillObservationReceipt,
    *,
    settlement: AlpacaPaperCycleSettlement,
    observation: AlpacaPaperFillQueryObservation,
    terminal_fill: AlpacaPaperFillActivity,
    mapping: AlpacaPaperFillObservationActivity,
) -> None:
    """Rebuild the terminal-fill/query join used by settled daily P&L."""

    verify_alpaca_paper_fill_activity_row(terminal_fill)
    canonical, digest = _terminal_fill_observation_content(
        settlement=settlement,
        observation=observation,
        terminal_fill=terminal_fill,
    )
    expected = {
        "receipt_schema_version": (
            "chili.alpaca-paper-terminal-fill-observation.v1"
        ),
        "authority_status": "verified",
        "settlement_sha256": settlement.settlement_sha256,
        "reservation_id": settlement.reservation_id,
        "account_scope": settlement.account_scope,
        "account_identity_sha256": settlement.account_identity_sha256,
        "observation_sha256": observation.observation_sha256,
        "terminal_fill_event_sha256": terminal_fill.event_sha256,
        "immutable_fill_identity_sha256": (
            terminal_fill.immutable_fill_identity_sha256
        ),
        "query_receipt_sha256": observation.query_receipt_sha256,
        "read_binding_sha256": observation.read_binding_sha256,
        "adapter_connection_generation": (
            observation.adapter_connection_generation
        ),
        "adapter_build_sha256": observation.adapter_build_sha256,
        "terminal_fill_observed_at": _utc(
            terminal_fill.provider_transaction_at,
            "terminal_fill.provider_transaction_at",
        ),
        "terminal_fill_available_at": _utc(
            observation.available_at, "observation.available_at"
        ),
        "receipt_content_canonical_json": canonical,
        "receipt_content_sha256": digest,
        "receipt_sha256": digest,
    }
    changed = sorted(
        name for name, value in expected.items() if getattr(receipt, name) != value
    )
    if changed:
        raise AlpacaFillActivityCorruption(
            "terminal fill observation receipt changed: " + ",".join(changed)
        )
    if not (
        mapping.observation_sha256 == observation.observation_sha256
        and mapping.fill_event_sha256 == terminal_fill.event_sha256
        and mapping.immutable_fill_identity_sha256
        == terminal_fill.immutable_fill_identity_sha256
        and settlement.terminal_fill_event_sha256 == terminal_fill.event_sha256
        and settlement.reservation_id == observation.reservation_id
        == terminal_fill.reservation_id
    ):
        raise AlpacaFillActivityCorruption(
            "terminal fill observation receipt predecessors changed"
        )


def prepare_alpaca_paper_terminal_fill_observation_receipt(
    *,
    settlement: AlpacaPaperCycleSettlement,
    observation: AlpacaPaperFillQueryObservation,
    terminal_fill: AlpacaPaperFillActivity,
    mapping: AlpacaPaperFillObservationActivity,
) -> AlpacaPaperTerminalFillObservationReceipt:
    """Build one deterministic typed receipt without database side effects."""

    canonical, digest = _terminal_fill_observation_content(
        settlement=settlement,
        observation=observation,
        terminal_fill=terminal_fill,
    )
    receipt = AlpacaPaperTerminalFillObservationReceipt(
        receipt_sha256=digest,
        receipt_schema_version=(
            "chili.alpaca-paper-terminal-fill-observation.v1"
        ),
        authority_status="verified",
        settlement_sha256=settlement.settlement_sha256,
        reservation_id=settlement.reservation_id,
        account_scope=settlement.account_scope,
        account_identity_sha256=settlement.account_identity_sha256,
        observation_sha256=observation.observation_sha256,
        terminal_fill_event_sha256=terminal_fill.event_sha256,
        immutable_fill_identity_sha256=(
            terminal_fill.immutable_fill_identity_sha256
        ),
        query_receipt_sha256=observation.query_receipt_sha256,
        read_binding_sha256=observation.read_binding_sha256,
        adapter_connection_generation=observation.adapter_connection_generation,
        adapter_build_sha256=observation.adapter_build_sha256,
        terminal_fill_observed_at=terminal_fill.provider_transaction_at,
        terminal_fill_available_at=observation.available_at,
        receipt_content_canonical_json=canonical,
        receipt_content_sha256=digest,
    )
    verify_alpaca_paper_terminal_fill_observation_receipt(
        receipt,
        settlement=settlement,
        observation=observation,
        terminal_fill=terminal_fill,
        mapping=mapping,
    )
    return receipt


def append_alpaca_paper_terminal_fill_observation_receipt(
    session: Session,
    *,
    settlement: AlpacaPaperCycleSettlement,
) -> AlpacaPaperTerminalFillObservationReceipt:
    """Append or verify the receipt joining settlement P&L to its broker poll."""

    if not session.in_transaction():
        raise AlpacaFillActivityError(
            "terminal fill receipt requires a caller-owned transaction"
        )
    terminal_fill = session.scalar(
        select(AlpacaPaperFillActivity)
        .where(
            AlpacaPaperFillActivity.event_sha256
            == settlement.terminal_fill_event_sha256
        )
        .with_for_update()
    )
    if terminal_fill is None:
        raise AlpacaFillActivityCorruption("settlement terminal fill is missing")
    candidates = list(
        session.execute(
            select(
                AlpacaPaperFillObservationActivity,
                AlpacaPaperFillQueryObservation,
            )
            .join(
                AlpacaPaperFillQueryObservation,
                AlpacaPaperFillQueryObservation.observation_sha256
                == AlpacaPaperFillObservationActivity.observation_sha256,
            )
            .where(
                AlpacaPaperFillObservationActivity.fill_event_sha256
                == terminal_fill.event_sha256,
                AlpacaPaperFillQueryObservation.reservation_id
                == settlement.reservation_id,
            )
            .order_by(
                AlpacaPaperFillQueryObservation.available_at,
                AlpacaPaperFillQueryObservation.observation_sha256,
            )
            .with_for_update()
        ).all()
    )
    if not candidates:
        raise AlpacaFillActivityCorruption(
            "settlement terminal fill lacks an authoritative query observation"
        )
    mapping, observation = candidates[0]
    prepared_receipt = prepare_alpaca_paper_terminal_fill_observation_receipt(
        settlement=settlement,
        observation=observation,
        terminal_fill=terminal_fill,
        mapping=mapping,
    )
    existing = session.scalar(
        select(AlpacaPaperTerminalFillObservationReceipt)
        .where(
            AlpacaPaperTerminalFillObservationReceipt.settlement_sha256
            == settlement.settlement_sha256
        )
        .with_for_update()
    )
    if existing is not None:
        verify_alpaca_paper_terminal_fill_observation_receipt(
            existing,
            settlement=settlement,
            observation=observation,
            terminal_fill=terminal_fill,
            mapping=mapping,
        )
        return existing
    receipt = prepared_receipt
    session.add(receipt)
    session.flush([receipt])
    verify_alpaca_paper_terminal_fill_observation_receipt(
        receipt,
        settlement=settlement,
        observation=observation,
        terminal_fill=terminal_fill,
        mapping=mapping,
    )
    return receipt


@dataclass(frozen=True)
class AlpacaPaperCycleSettlementCoverage:
    reservation_id: uuid.UUID
    status: str
    pending_reasons: tuple[str, ...]
    entry_quantity: Decimal
    exit_quantity: Decimal
    gross_realized_pnl_usd: Decimal | None
    fees_usd: Decimal | None
    net_realized_pnl_usd: Decimal | None

    @property
    def pending(self) -> bool:
        return self.status == "pending"


def evaluate_alpaca_paper_cycle_settlement(
    *,
    reservation_id: uuid.UUID,
    rows: Sequence[AlpacaPaperFillActivity],
    expected_entry_quantity: Any | None = None,
) -> AlpacaPaperCycleSettlementCoverage:
    """Derive a fail-closed settlement marker without changing risk policy."""

    cycle_id = _uuid(reservation_id, "reservation_id")
    verify_alpaca_paper_fill_activity_chain(rows)
    if any(_uuid(row.reservation_id, "row.reservation_id") != cycle_id for row in rows):
        raise AlpacaFillActivityConflict("settlement rows cross cycle identity")
    entry_quantity = sum(
        (_decimal(row.quantity, "row.quantity") for row in rows if row.side == "buy"),
        Decimal("0"),
    )
    exit_quantity = sum(
        (_decimal(row.quantity, "row.quantity") for row in rows if row.side == "sell"),
        Decimal("0"),
    )
    if exit_quantity > entry_quantity:
        raise AlpacaFillActivityConflict("captured exits exceed captured entries")
    reasons: set[str] = set()
    if entry_quantity <= 0:
        reasons.add("entry_fill_missing")
    if exit_quantity <= 0:
        reasons.add("exit_fill_missing")
    if entry_quantity != exit_quantity:
        reasons.add("position_not_flat")
    if expected_entry_quantity is not None:
        expected = _decimal(
            expected_entry_quantity,
            "expected_entry_quantity",
            positive=True,
        )
        if entry_quantity != expected:
            reasons.add("entry_quantity_incomplete")
    # v1 accepts no sealed capture-authority receipt. Mapping shape and hashes
    # prove immutability, not who observed the bytes or when they became
    # available. Consequently even syntactically complete fabricated mappings
    # remain diagnostic and settlement must stay pending.
    if not rows or any(
        row.capture_authority_status != "verified" for row in rows
    ):
        reasons.add("capture_authority_unverified")
    if any(row.fee_status != "authoritative" for row in rows):
        reasons.add("fee_truth_unavailable")
    if any(row.provider_event_clock_status != "authoritative" for row in rows):
        reasons.add("provider_event_clock_unavailable")
    if any(
        row.provider_client_order_id_status != "authoritative" for row in rows
    ):
        reasons.add("provider_client_order_id_unavailable")
    if any(
        row.order_role == "exit"
        and row.order_ownership_status != "authoritative"
        for row in rows
    ):
        reasons.add("exit_order_ownership_unverified")

    gross: Decimal | None = None
    if entry_quantity > 0 and entry_quantity == exit_quantity:
        buy_cost = sum(
            (
                _decimal(row.quantity, "row.quantity")
                * _decimal(row.price, "row.price")
                for row in rows
                if row.side == "buy"
            ),
            Decimal("0"),
        )
        sell_proceeds = sum(
            (
                _decimal(row.quantity, "row.quantity")
                * _decimal(row.price, "row.price")
                for row in rows
                if row.side == "sell"
            ),
            Decimal("0"),
        )
        gross = (sell_proceeds - buy_cost).quantize(_MONEY_QUANTUM)
    fees: Decimal | None = None
    if rows and all(
        row.capture_authority_status == "verified"
        and row.fee_status == "authoritative"
        for row in rows
    ):
        fees = sum(
            (_decimal(row.fee_usd, "row.fee_usd") for row in rows),
            Decimal("0"),
        ).quantize(_MONEY_QUANTUM)
    net = (
        (gross - fees).quantize(_MONEY_QUANTUM)
        if gross is not None and fees is not None
        else None
    )
    return AlpacaPaperCycleSettlementCoverage(
        reservation_id=cycle_id,
        status="pending" if reasons else "complete",
        pending_reasons=tuple(sorted(reasons)),
        entry_quantity=entry_quantity.quantize(_MONEY_QUANTUM),
        exit_quantity=exit_quantity.quantize(_MONEY_QUANTUM),
        gross_realized_pnl_usd=gross,
        fees_usd=fees,
        net_realized_pnl_usd=net,
    )


def query_pending_alpaca_paper_cycle_settlements(
    session: Session,
    *,
    account_scope: str,
    account_identity_sha256: str,
) -> tuple[AlpacaPaperCycleSettlementCoverage, ...]:
    """Read-only seam for flat adaptive cycles lacking exact settlement."""

    scope = _required_text(account_scope, "account_scope", lower=True)
    identity = _require_sha(account_identity_sha256, "account_identity_sha256")
    if scope != "alpaca:paper":
        raise AlpacaFillActivityError("pending-settlement query is PAPER-only")
    reservations = list(
        session.scalars(
            select(AdaptiveRiskReservation)
            .join(
                AdaptiveRiskDecisionPacket,
                AdaptiveRiskDecisionPacket.decision_packet_sha256
                == AdaptiveRiskReservation.decision_packet_sha256,
            )
            .where(
                AdaptiveRiskReservation.account_scope == scope,
                AdaptiveRiskReservation.state == "flat_pending_settlement",
                AdaptiveRiskDecisionPacket.execution_surface == "alpaca_paper",
                AdaptiveRiskDecisionPacket.execution_family == "alpaca_spot",
                AdaptiveRiskDecisionPacket.broker_environment == "paper",
                AdaptiveRiskDecisionPacket.account_identity_sha256 == identity,
            )
            .order_by(AdaptiveRiskReservation.reservation_id)
        )
    )
    pending: list[AlpacaPaperCycleSettlementCoverage] = []
    for reservation in reservations:
        rows = list(
            session.scalars(
                select(AlpacaPaperFillActivity)
                .where(
                    AlpacaPaperFillActivity.reservation_id
                    == reservation.reservation_id,
                    AlpacaPaperFillActivity.account_scope == scope,
                    AlpacaPaperFillActivity.account_identity_sha256 == identity,
                )
                .order_by(AlpacaPaperFillActivity.sequence)
            )
        )
        coverage = evaluate_alpaca_paper_cycle_settlement(
            reservation_id=reservation.reservation_id,
            rows=rows,
            expected_entry_quantity=reservation.cumulative_filled_quantity_shares,
        )
        if coverage.pending:
            pending.append(coverage)
    return tuple(pending)


__all__ = [
    "AlpacaFillActivityConflict",
    "AlpacaFillActivityCorruption",
    "AlpacaFillActivityError",
    "AlpacaPaperCycleSettlementCoverage",
    "AlpacaPaperEntryFillHandoffProof",
    "AlpacaPaperEntryFillPublicationResult",
    "AlpacaPaperFillAppendResult",
    "AlpacaPaperFillCycleBinding",
    "AlpacaPaperOrderFillCaptureResult",
    "AUTHORITATIVE_CAPTURE_SCHEMA_VERSION",
    "ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION",
    "POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND",
    "PREPARED_FILL_BATCH_SCHEMA_VERSION",
    "PreparedAlpacaPaperFillActivity",
    "PreparedAlpacaPaperFillBatch",
    "append_alpaca_paper_fill_activity",
    "append_alpaca_paper_terminal_fill_observation_receipt",
    "append_prepared_alpaca_paper_fill_batch",
    "capture_verified_alpaca_paper_order_fills",
    "evaluate_alpaca_paper_cycle_settlement",
    "prepare_alpaca_paper_fill_activity",
    "prepare_alpaca_paper_terminal_fill_observation_receipt",
    "prepare_verified_alpaca_paper_fill_activity",
    "publish_prepared_alpaca_paper_entry_fill_batch",
    "query_pending_alpaca_paper_cycle_settlements",
    "read_verified_alpaca_paper_fill_batch",
    "verify_alpaca_paper_fill_activity_chain",
    "verify_alpaca_paper_fill_activity_row",
    "verify_alpaca_paper_entry_fill_handoff",
    "verify_alpaca_paper_terminal_fill_observation_receipt",
]
