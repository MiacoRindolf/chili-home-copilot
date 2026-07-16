"""Fail-closed restart ownership for the captured Alpaca PAPER service.

This module is deliberately split from service orchestration.  It has no
provider or broker client and never writes persistence.  The database reader
rehashes the immutable captured-PAPER outbox bytes and binds their current
reservation/action/session/fill-watch projections.  The pure classifier then
compares that durable inventory with two complete, account-pinned broker
censuses.

An empty broker inventory is the only first-cutover result.  Non-empty broker
inventory is accepted only as an *owned recovery* envelope; it never grants
new-entry authority.  Ambiguous, foreign, partial, unreadable, or mismatched
evidence raises :class:`CapturedPaperRestartInventoryError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ....models.trading import AlpacaPaperFillActivity
from .alpaca_fill_activity import (
    AUTHORITATIVE_CAPTURE_SCHEMA_VERSION,
    verify_alpaca_paper_fill_activity_chain,
)
from .captured_paper_outbox import load_captured_paper_outbox


UTC = timezone.utc
ALPACA_PAPER_ACCOUNT_SCOPE = "alpaca:paper"
RESTART_INVENTORY_SCHEMA_VERSION = (
    "chili.captured-paper-restart-inventory.v1"
)
RESTART_LINEAGE_SCHEMA_VERSION = (
    "chili.captured-paper-restart-lineage.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONNECTION_RE = re.compile(r"^alpaca-paper-rest:[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,35}$")
_ACTIVE_OUTBOX_STATES = frozenset(
    {
        "pending",
        "leased",
        "retry_wait",
        "retry_exhausted",
        "transport_started",
        "transport_indeterminate",
        "reconciling",
        "fill_handoff_committed",
        "completed",
    }
)
_ACTIVE_RESERVATION_STATES = frozenset(
    {
        "reserved",
        "submitted",
        "submit_indeterminate",
        "partially_filled",
        "filled",
        "flat_pending_settlement",
        "exposure_quarantined",
        "released",
        "closed",
    }
)
_POSITION_OWNING_STATES = frozenset(
    {"partially_filled", "filled", "exposure_quarantined"}
)
_OPEN_ORDER_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "partially_filled",
        "stopped",
        "held",
        "pending_cancel",
        "pending_replace",
        "done_for_day",
        "suspended",
    }
)


class CapturedPaperRestartInventoryError(RuntimeError):
    """The current broker inventory cannot be proven CHILI-owned."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_not_canonical_json"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return normalized


def _canonical_uuid(value: Any, *, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        ) from exc


def _connection_generation(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if _CONNECTION_RE.fullmatch(normalized) is None:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return normalized


def _symbol(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip().upper()
    if _SYMBOL_RE.fullmatch(normalized) is None:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return normalized


def _positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        ) from exc
    if parsed <= 0 or parsed > 2_147_483_647:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return parsed


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        ) from exc
    if parsed < 0 or parsed > 2_147_483_647:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return parsed


def _decimal(value: Any, *, field_name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        ) from exc
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return parsed


def _whole_shares(value: Any, *, field_name: str, allow_zero: bool) -> int:
    parsed = _decimal(value, field_name=field_name)
    if parsed != parsed.to_integral_value():
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_not_whole_shares"
        )
    integer = int(parsed)
    if integer < (0 if allow_zero else 1):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_invalid"
        )
    return integer


def _iso_utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_clock_not_aware"
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{field_name}_not_aware"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CapturedPaperRestartLineage:
    """One rehashed outbox joined to its current durable ownership state."""

    completion_sha256: str
    payload_sha256: str
    route_token_sha256: str
    runtime_generation: str
    expected_account_id: str
    session_id: int
    symbol: str
    client_order_id: str
    binder_id: str
    action_claim_token: str
    reservation_id: str
    order_request_sha256: str
    order_request: Mapping[str, Any]
    outbox_status: str
    outbox_transport_started: bool
    action_claim_phase: str
    action_claim_client_order_id: str
    action_claim_broker_order_id: str | None
    reservation_state: str
    planned_quantity_shares: int
    cumulative_filled_quantity_shares: int
    open_quantity_shares: int
    reservation_broker_order_id: str | None
    reservation_broker_connection_generation: str | None
    fill_activity_inventory_sha256: str
    verified_entry_fill_quantity_shares: int
    verified_exit_fill_quantity_shares: int
    verified_entry_average_price: str | None
    session_state: str
    fill_watch_state: str | None
    fill_watch_broker_order_id: str | None
    lineage_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "completion_sha256",
            "payload_sha256",
            "route_token_sha256",
            "order_request_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "runtime_generation",
            _canonical_uuid(
                self.runtime_generation, field_name="runtime_generation"
            ),
        )
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id, field_name="expected_account_id"
            ),
        )
        object.__setattr__(
            self,
            "binder_id",
            _canonical_uuid(self.binder_id, field_name="binder_id"),
        )
        object.__setattr__(
            self,
            "reservation_id",
            _canonical_uuid(self.reservation_id, field_name="reservation_id"),
        )
        object.__setattr__(
            self,
            "session_id",
            _positive_int(self.session_id, field_name="session_id"),
        )
        object.__setattr__(
            self,
            "symbol",
            _symbol(self.symbol, field_name="symbol"),
        )
        cid = str(self.client_order_id or "").strip()
        if not cid or cid != str(self.action_claim_client_order_id or "").strip():
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_action_claim_cid_mismatch"
            )
        object.__setattr__(self, "client_order_id", cid)
        token = str(self.action_claim_token or "").strip()
        if not token.startswith("arm-"):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_action_claim_token_invalid"
            )
        _canonical_uuid(token[4:], field_name="action_claim_generation")
        object.__setattr__(self, "action_claim_token", token)
        if self.outbox_status not in _ACTIVE_OUTBOX_STATES:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_outbox_status_invalid"
            )
        if self.action_claim_phase not in {
            "claimed",
            "submit_indeterminate",
            "submitted",
            "resolved",
        }:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_action_claim_phase_invalid"
            )
        if self.reservation_state not in _ACTIVE_RESERVATION_STATES:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_reservation_state_invalid"
            )
        planned = _positive_int(
            self.planned_quantity_shares,
            field_name="planned_quantity_shares",
        )
        cumulative = _nonnegative_int(
            self.cumulative_filled_quantity_shares,
            field_name="cumulative_filled_quantity_shares",
        )
        opened = _nonnegative_int(
            self.open_quantity_shares,
            field_name="open_quantity_shares",
        )
        if cumulative > planned or opened > cumulative:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_reservation_quantity_mismatch"
            )
        object.__setattr__(self, "planned_quantity_shares", planned)
        object.__setattr__(self, "cumulative_filled_quantity_shares", cumulative)
        object.__setattr__(self, "open_quantity_shares", opened)
        order = dict(self.order_request)
        if _sha256_json(order) != self.order_request_sha256:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_order_request_hash_mismatch"
            )
        expected_order = {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": "buy",
            "type": "limit",
            "asset_class": "us_equity",
            "position_intent": "buy_to_open",
            "qty": planned,
        }
        if any(order.get(name) != expected for name, expected in expected_order.items()):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_order_request_binding_mismatch"
            )
        _decimal(order.get("limit_price"), field_name="entry_limit_price", positive=True)
        if (
            order.get("time_in_force") not in {"day", "gtc"}
            or type(order.get("extended_hours")) is not bool
            or (
                order.get("extended_hours") is True
                and order.get("time_in_force") != "day"
            )
        ):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_order_request_routing_invalid"
            )
        object.__setattr__(self, "order_request", order)
        for name in (
            "action_claim_broker_order_id",
            "reservation_broker_order_id",
            "fill_watch_broker_order_id",
        ):
            value = getattr(self, name)
            normalized = None if value is None else str(value).strip()
            if normalized == "":
                normalized = None
            object.__setattr__(self, name, normalized)
        prior_generation = self.reservation_broker_connection_generation
        if prior_generation is not None:
            object.__setattr__(
                self,
                "reservation_broker_connection_generation",
                _connection_generation(
                    prior_generation,
                    field_name="reservation_broker_connection_generation",
                ),
            )
        if self.fill_watch_state not in {
            None,
            "pending",
            "leased",
            "retry_wait",
            "terminal_zero_fill",
            "fill_handoff_committed",
        }:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_fill_watch_state_invalid"
            )
        object.__setattr__(
            self,
            "fill_activity_inventory_sha256",
            _digest(
                self.fill_activity_inventory_sha256,
                field_name="fill_activity_inventory_sha256",
            ),
        )
        entry_filled = _nonnegative_int(
            self.verified_entry_fill_quantity_shares,
            field_name="verified_entry_fill_quantity_shares",
        )
        exited = _nonnegative_int(
            self.verified_exit_fill_quantity_shares,
            field_name="verified_exit_fill_quantity_shares",
        )
        if (
            entry_filled != cumulative
            or exited > entry_filled
            or entry_filled - exited != opened
        ):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_verified_fill_quantity_mismatch"
            )
        object.__setattr__(
            self, "verified_entry_fill_quantity_shares", entry_filled
        )
        object.__setattr__(
            self, "verified_exit_fill_quantity_shares", exited
        )
        if entry_filled == 0:
            if self.verified_entry_average_price is not None:
                raise CapturedPaperRestartInventoryError(
                    "restart_inventory_zero_fill_has_average_price"
                )
        else:
            average = _decimal(
                self.verified_entry_average_price,
                field_name="verified_entry_average_price",
                positive=True,
            )
            object.__setattr__(
                self,
                "verified_entry_average_price",
                format(average, "f"),
            )
        object.__setattr__(
            self,
            "lineage_sha256",
            _sha256_json(self.to_payload(include_digest=False)),
        )

    @property
    def terminal_late_fill_quarantined(self) -> bool:
        return self.reservation_state == "exposure_quarantined"

    def to_payload(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": RESTART_LINEAGE_SCHEMA_VERSION,
            "completion_sha256": self.completion_sha256,
            "payload_sha256": self.payload_sha256,
            "route_token_sha256": self.route_token_sha256,
            "runtime_generation": self.runtime_generation,
            "expected_account_id": self.expected_account_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "client_order_id": self.client_order_id,
            "binder_id": self.binder_id,
            "action_claim_token": self.action_claim_token,
            "reservation_id": self.reservation_id,
            "order_request_sha256": self.order_request_sha256,
            "order_request": dict(self.order_request),
            "outbox_status": self.outbox_status,
            "outbox_transport_started": self.outbox_transport_started,
            "action_claim_phase": self.action_claim_phase,
            "action_claim_client_order_id": self.action_claim_client_order_id,
            "action_claim_broker_order_id": self.action_claim_broker_order_id,
            "reservation_state": self.reservation_state,
            "planned_quantity_shares": self.planned_quantity_shares,
            "cumulative_filled_quantity_shares": (
                self.cumulative_filled_quantity_shares
            ),
            "open_quantity_shares": self.open_quantity_shares,
            "reservation_broker_order_id": self.reservation_broker_order_id,
            "reservation_broker_connection_generation": (
                self.reservation_broker_connection_generation
            ),
            "fill_activity_inventory_sha256": (
                self.fill_activity_inventory_sha256
            ),
            "verified_entry_fill_quantity_shares": (
                self.verified_entry_fill_quantity_shares
            ),
            "verified_exit_fill_quantity_shares": (
                self.verified_exit_fill_quantity_shares
            ),
            "verified_entry_average_price": (
                self.verified_entry_average_price
            ),
            "session_state": self.session_state,
            "fill_watch_state": self.fill_watch_state,
            "fill_watch_broker_order_id": self.fill_watch_broker_order_id,
            "terminal_late_fill_quarantined": (
                self.terminal_late_fill_quarantined
            ),
        }
        if include_digest:
            payload["lineage_sha256"] = self.lineage_sha256
        return payload


def _verify_census(
    census: Mapping[str, Any],
    *,
    kind: str,
    expected_account_id: str,
    expected_connection_generation: str,
    expected_adapter_build_sha256: str,
    expected_read_binding_sha256: str,
    observed_at: datetime,
) -> tuple[list[dict[str, Any]], str, str, str]:
    if not isinstance(census, Mapping):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{kind}_census_unavailable"
        )
    expected_schema = {
        "orders": "chili.alpaca-paper-open-order-census.v1",
        "positions": "chili.alpaca-paper-position-census.v1",
    }[kind]
    expected_query = {
        "orders": {
            "status": "open",
            "limit": 500,
            "direction": "asc",
            "nested": False,
        },
        "positions": {
            "resource": "account_positions",
            "asset_class": "us_equity",
            "pagination": "not_applicable",
        },
    }[kind]
    expected_terminal_reason = {
        "orders": "complete_short_page",
        "positions": "complete_non_pageable_account_positions",
    }[kind]
    expected_count_field = {
        "orders": "exact_order_count",
        "positions": "exact_position_count",
    }[kind]
    expected_build = _digest(
        expected_adapter_build_sha256,
        field_name="expected_adapter_build_sha256",
    )
    expected_binding = _digest(
        expected_read_binding_sha256,
        field_name="expected_read_binding_sha256",
    )
    inventory = census.get(kind)
    if not (
        census.get("readable") is True
        and census.get("pagination_complete") is True
        and census.get("broker_environment") == "paper"
        and census.get("asset_class") == "us_equity"
        and census.get("provider_account_id") == expected_account_id
        and census.get("adapter_connection_generation")
        == expected_connection_generation
        and census.get("adapter_build_sha256") == expected_build
        and census.get("read_binding_sha256") == expected_binding
        and isinstance(inventory, list)
    ):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{kind}_census_incomplete"
        )
    inventory_sha = _digest(
        census.get("inventory_sha256"),
        field_name=f"{kind}_inventory_sha256",
    )
    receipt_sha = _digest(
        census.get("query_receipt_sha256"),
        field_name=f"{kind}_query_receipt_sha256",
    )
    receipt_json = str(census.get("query_receipt_canonical_json") or "")
    inventory_json = str(census.get("inventory_canonical_json") or "")
    binding_json = str(census.get("read_binding_canonical_json") or "")
    adapter_build_sha256 = _digest(
        census.get("adapter_build_sha256"),
        field_name=f"{kind}_adapter_build_sha256",
    )
    requested_at = _aware_utc(
        census.get("requested_at"), field_name=f"{kind}_requested_at"
    )
    received_at = _aware_utc(
        census.get("received_at"), field_name=f"{kind}_received_at"
    )
    available_at = _aware_utc(
        census.get("available_at"), field_name=f"{kind}_available_at"
    )
    expires_at = _aware_utc(
        census.get("expires_at"), field_name=f"{kind}_expires_at"
    )
    decision_at = _aware_utc(observed_at, field_name="observed_at")
    try:
        receipt = json.loads(receipt_json)
        decoded_inventory = json.loads(inventory_json)
        decoded_binding = json.loads(binding_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{kind}_census_json_invalid"
        ) from exc
    expected_query_json = _canonical_json(expected_query)
    expected_query_sha256 = hashlib.sha256(
        expected_query_json.encode("utf-8")
    ).hexdigest()
    pages = receipt.get("pages") if isinstance(receipt, dict) else None
    page = pages[0] if isinstance(pages, list) and len(pages) == 1 else None
    terminal = receipt.get("terminal_proof") if isinstance(receipt, dict) else None
    if not (
        isinstance(receipt, dict)
        and _canonical_json(receipt) == receipt_json
        and _canonical_json(decoded_inventory) == inventory_json
        and _canonical_json(decoded_binding) == binding_json
        and hashlib.sha256(binding_json.encode("utf-8")).hexdigest()
        == expected_binding
        and receipt.get("schema_version") == expected_schema
        and receipt.get("broker_environment") == "paper"
        and receipt.get("asset_class") == "us_equity"
        and receipt.get("provider_account_id") == expected_account_id
        and receipt.get("adapter_connection_generation")
        == expected_connection_generation
        and receipt.get("adapter_build_sha256") == expected_build
        and adapter_build_sha256 == expected_build
        and receipt.get("read_binding_sha256") == expected_binding
        and receipt.get("query") == expected_query
        and receipt.get("inventory_sha256") == inventory_sha
        and receipt.get(expected_count_field) == len(inventory)
        and isinstance(page, dict)
        and page.get("page_index") == 0
        and page.get("request_page_token") is None
        and page.get("request_canonical_json") == expected_query_json
        and page.get("request_sha256") == expected_query_sha256
        and page.get("requested_at") == requested_at.isoformat()
        and page.get("received_at") == received_at.isoformat()
        and page.get("available_at") == available_at.isoformat()
        and page.get("response_count") == len(inventory)
        and page.get("response_canonical_json") == inventory_json
        and page.get("response_sha256") == inventory_sha
        and page.get("next_page_token") is None
        and page.get("terminal") is True
        and isinstance(terminal, dict)
        and terminal.get("pagination_complete") is True
        and terminal.get("reason") == expected_terminal_reason
        and terminal.get("page_count") == 1
        and terminal.get("last_response_sha256") == inventory_sha
        and terminal.get("last_response_count") == len(inventory)
        and terminal.get("last_page_terminal") is True
        and hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        == receipt_sha
        and hashlib.sha256(inventory_json.encode("utf-8")).hexdigest()
        == inventory_sha
        and decoded_inventory == inventory
        and requested_at <= received_at <= available_at <= decision_at <= expires_at
    ):
        raise CapturedPaperRestartInventoryError(
            f"restart_inventory_{kind}_inventory_hash_mismatch"
        )
    return (
        [dict(item) for item in inventory],
        inventory_sha,
        receipt_sha,
        adapter_build_sha256,
    )


def _provider_order_identity(order: Mapping[str, Any]) -> dict[str, Any]:
    oid = str(order.get("id") or "").strip()
    cid = str(order.get("client_order_id") or "").strip()
    symbol = _symbol(order.get("symbol"), field_name="provider_order_symbol")
    side = str(order.get("side") or "").strip().lower()
    order_type = str(
        order.get("type") or order.get("order_type") or ""
    ).strip().lower()
    status = str(order.get("status") or "").strip().lower()
    asset_class = str(order.get("asset_class") or "").strip().lower()
    tif = str(order.get("time_in_force") or "").strip().lower()
    position_intent = str(order.get("position_intent") or "").strip().lower()
    extended = order.get("extended_hours")
    if (
        not oid
        or not cid
        or side not in {"buy", "sell"}
        or not order_type
        or status not in _OPEN_ORDER_STATUSES
        or asset_class != "us_equity"
        or tif not in {"day", "gtc"}
        or type(extended) is not bool
    ):
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_provider_order_malformed"
        )
    quantity = _whole_shares(
        order.get("qty"), field_name="provider_order_quantity", allow_zero=False
    )
    filled = _whole_shares(
        order.get("filled_qty"),
        field_name="provider_order_filled_quantity",
        allow_zero=True,
    )
    if filled > quantity:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_provider_order_fill_exceeds_quantity"
        )
    limit_raw = order.get("limit_price")
    limit_price = (
        None
        if limit_raw is None
        else _decimal(
            limit_raw, field_name="provider_order_limit_price", positive=True
        )
    )
    return {
        "broker_order_id": oid,
        "client_order_id": cid,
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "status": status,
        "asset_class": asset_class,
        "time_in_force": tif,
        "extended_hours": extended,
        "position_intent": position_intent or None,
        "quantity_shares": quantity,
        "filled_quantity_shares": filled,
        "limit_price": None if limit_price is None else format(limit_price, "f"),
        "provider_order_sha256": _sha256_json(dict(order)),
    }


def _provider_position_identity(position: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _symbol(
        position.get("symbol"), field_name="provider_position_symbol"
    )
    if str(position.get("asset_class") or "").strip().lower() != "us_equity":
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_provider_position_asset_class_invalid"
        )
    quantity = _whole_shares(
        position.get("qty"),
        field_name="provider_position_quantity",
        allow_zero=False,
    )
    average = _decimal(
        position.get("avg_entry_price"),
        field_name="provider_position_avg_entry_price",
        positive=True,
    )
    return {
        "symbol": symbol,
        "quantity_shares": quantity,
        "average_entry_price": format(average, "f"),
        "provider_position_sha256": _sha256_json(dict(position)),
    }


def classify_captured_paper_restart_inventory(
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_connection_generation: str,
    expected_adapter_build_sha256: str,
    expected_read_binding_sha256: str,
    open_order_census: Mapping[str, Any],
    position_census: Mapping[str, Any],
    durable_lineages: Sequence[CapturedPaperRestartLineage],
    observed_at: datetime,
) -> dict[str, Any]:
    """Return a content-addressed first-cutover or owned-recovery receipt."""

    account_id = _canonical_uuid(
        expected_account_id, field_name="expected_account_id"
    )
    runtime_generation = _canonical_uuid(
        expected_runtime_generation, field_name="runtime_generation"
    )
    connection_generation = _connection_generation(
        expected_connection_generation, field_name="connection_generation"
    )
    adapter_build_sha256 = _digest(
        expected_adapter_build_sha256,
        field_name="expected_adapter_build_sha256",
    )
    read_binding_sha256 = _digest(
        expected_read_binding_sha256,
        field_name="expected_read_binding_sha256",
    )
    orders, order_inventory_sha, order_census_sha, order_adapter_build = _verify_census(
        open_order_census,
        kind="orders",
        expected_account_id=account_id,
        expected_connection_generation=connection_generation,
        expected_adapter_build_sha256=adapter_build_sha256,
        expected_read_binding_sha256=read_binding_sha256,
        observed_at=observed_at,
    )
    positions, position_inventory_sha, position_census_sha, position_adapter_build = _verify_census(
        position_census,
        kind="positions",
        expected_account_id=account_id,
        expected_connection_generation=connection_generation,
        expected_adapter_build_sha256=adapter_build_sha256,
        expected_read_binding_sha256=read_binding_sha256,
        observed_at=observed_at,
    )
    if order_adapter_build != position_adapter_build:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_broker_adapter_build_mismatch"
        )
    lineages = tuple(durable_lineages)
    if any(type(item) is not CapturedPaperRestartLineage for item in lineages):
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_durable_lineage_type_invalid"
        )
    if any(
        item.expected_account_id != account_id
        or item.runtime_generation != runtime_generation
        for item in lineages
    ):
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_durable_generation_mismatch"
        )
    cids = [item.client_order_id for item in lineages]
    completions = [item.completion_sha256 for item in lineages]
    reservations = [item.reservation_id for item in lineages]
    if (
        len(cids) != len(set(cids))
        or len(completions) != len(set(completions))
        or len(reservations) != len(set(reservations))
    ):
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_durable_identity_ambiguous"
        )
    lineage_by_cid = {item.client_order_id: item for item in lineages}

    owned_orders: list[dict[str, Any]] = []
    seen_order_ids: set[str] = set()
    seen_order_cids: set[str] = set()
    for raw in orders:
        order = _provider_order_identity(raw)
        oid = order["broker_order_id"]
        cid = order["client_order_id"]
        if oid in seen_order_ids or cid in seen_order_cids:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_provider_order_identity_ambiguous"
            )
        seen_order_ids.add(oid)
        seen_order_cids.add(cid)
        lineage = lineage_by_cid.get(cid)
        if lineage is None:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_foreign_open_order"
            )
        local = lineage.order_request
        local_limit = format(
            _decimal(
                local.get("limit_price"),
                field_name="local_entry_limit_price",
                positive=True,
            ),
            "f",
        )
        bound_oids = {
            value
            for value in (
                lineage.action_claim_broker_order_id,
                lineage.reservation_broker_order_id,
                lineage.fill_watch_broker_order_id,
            )
            if value is not None
        }
        if len(bound_oids) > 1 or (bound_oids and oid not in bound_oids):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_broker_order_id_mismatch"
            )
        if not (
            lineage.outbox_transport_started is True
            and lineage.outbox_status
            in {
                "transport_started",
                "transport_indeterminate",
                "reconciling",
                "fill_handoff_committed",
                "completed",
            }
            and lineage.action_claim_phase
            in {"submit_indeterminate", "submitted", "resolved"}
            and order["symbol"] == lineage.symbol
            and order["side"] == local["side"]
            and order["type"] == local["type"]
            and order["quantity_shares"] == lineage.planned_quantity_shares
            and order["filled_quantity_shares"]
            == lineage.cumulative_filled_quantity_shares
            and order["limit_price"] is not None
            and Decimal(order["limit_price"]) == Decimal(local_limit)
            and order["time_in_force"] == local["time_in_force"]
            and order["extended_hours"] is local["extended_hours"]
            and order["position_intent"] in {None, local["position_intent"]}
        ):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_open_order_lineage_mismatch"
            )
        owned_orders.append(
            {
                **order,
                "completion_sha256": lineage.completion_sha256,
                "reservation_id": lineage.reservation_id,
                "lineage_sha256": lineage.lineage_sha256,
            }
        )

    expected_positions: dict[str, int] = {}
    expected_position_cost: dict[str, Decimal] = {}
    position_lineages: dict[str, list[CapturedPaperRestartLineage]] = {}
    for lineage in lineages:
        if lineage.open_quantity_shares <= 0:
            continue
        if (
            lineage.reservation_state not in _POSITION_OWNING_STATES
            or lineage.reservation_broker_order_id is None
            or lineage.reservation_broker_connection_generation is None
            or lineage.cumulative_filled_quantity_shares <= 0
        ):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_open_position_lineage_incomplete"
            )
        expected_positions[lineage.symbol] = (
            expected_positions.get(lineage.symbol, 0)
            + lineage.open_quantity_shares
        )
        assert lineage.verified_entry_average_price is not None
        expected_position_cost[lineage.symbol] = (
            expected_position_cost.get(lineage.symbol, Decimal("0"))
            + Decimal(lineage.verified_entry_average_price)
            * Decimal(lineage.open_quantity_shares)
        )
        position_lineages.setdefault(lineage.symbol, []).append(lineage)

    owned_positions: list[dict[str, Any]] = []
    seen_position_symbols: set[str] = set()
    for raw in positions:
        position = _provider_position_identity(raw)
        symbol = position["symbol"]
        if symbol in seen_position_symbols:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_provider_position_ambiguous"
            )
        seen_position_symbols.add(symbol)
        expected_quantity = expected_positions.get(symbol)
        if expected_quantity is None:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_foreign_position"
            )
        if position["quantity_shares"] != expected_quantity:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_position_quantity_mismatch"
            )
        expected_average = (
            expected_position_cost[symbol] / Decimal(expected_quantity)
        )
        provider_average = Decimal(position["average_entry_price"])
        if provider_average != expected_average:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_position_average_price_mismatch"
            )
        owners = sorted(
            position_lineages[symbol], key=lambda item: item.reservation_id
        )
        owned_positions.append(
            {
                **position,
                "reservation_ids": [item.reservation_id for item in owners],
                "lineage_sha256s": [item.lineage_sha256 for item in owners],
                "terminal_late_fill_quarantined": any(
                    item.terminal_late_fill_quarantined for item in owners
                ),
            }
        )
    if set(expected_positions) != seen_position_symbols:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_durable_position_missing_at_broker"
        )

    broker_nonempty = bool(orders or positions)
    if broker_nonempty and not lineages:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_nonempty_without_durable_lineage"
        )
    terminal_quarantines = [
        {
            "completion_sha256": item.completion_sha256,
            "reservation_id": item.reservation_id,
            "symbol": item.symbol,
            "lineage_sha256": item.lineage_sha256,
        }
        for item in sorted(lineages, key=lambda row: row.completion_sha256)
        if item.terminal_late_fill_quarantined
    ]
    recovery_required = bool(broker_nonempty or lineages)
    disposition = (
        "owned_restart_recovery"
        if recovery_required
        else "strict_flat_first_cutover"
    )
    body = {
        "schema_version": RESTART_INVENTORY_SCHEMA_VERSION,
        "disposition": disposition,
        "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
        "expected_account_id": account_id,
        # This is CHILI's immutable route generation, not a broker claim.
        "runtime_generation": runtime_generation,
        # Both broker censuses independently bind this current REST generation.
        "broker_connection_generation": connection_generation,
        "broker_adapter_build_sha256": order_adapter_build,
        "broker_read_binding_sha256": read_binding_sha256,
        "open_order_census_sha256": order_census_sha,
        "open_order_inventory_sha256": order_inventory_sha,
        "position_census_sha256": position_census_sha,
        "position_inventory_sha256": position_inventory_sha,
        "durable_inventory_sha256": _sha256_json(
            [
                item.to_payload()
                for item in sorted(lineages, key=lambda row: row.completion_sha256)
            ]
        ),
        "owned_open_orders": sorted(
            owned_orders, key=lambda item: item["broker_order_id"]
        ),
        "owned_positions": sorted(
            owned_positions, key=lambda item: item["symbol"]
        ),
        "terminal_late_fill_quarantines": terminal_quarantines,
        "recovery_required": recovery_required,
        "new_admissions_quarantined": recovery_required,
        "exposure_decreasing_only": recovery_required,
        "broker_inventory_flat": not broker_nonempty,
        "observed_at": _iso_utc(observed_at),
        "paper_execution_only": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    receipt_sha = _sha256_json(body)
    return {
        **body,
        "receipt_canonical_json": _canonical_json(body),
        "receipt_sha256": receipt_sha,
    }


_RESTART_ROWS_SQL = text(
    """
    SELECT o.completion_sha256,
           r.reservation_id::text AS reservation_id,
           r.state AS reservation_state,
           r.planned_quantity_shares,
           r.cumulative_filled_quantity_shares,
           r.open_quantity_shares,
           r.broker_order_id AS reservation_broker_order_id,
           r.broker_connection_generation,
           c.claim_token, c.action AS claim_action, c.phase AS claim_phase,
           c.client_order_id AS claim_client_order_id,
           c.broker_order_id AS claim_broker_order_id,
           c.owner_session_id, c.metadata_json AS claim_metadata,
           s.mode AS session_mode, s.execution_family,
           s.symbol AS session_symbol, s.state AS session_state,
           s.ended_at AS session_ended_at,
           s.risk_snapshot_json,
           w.state AS fill_watch_state,
           w.broker_order_id AS fill_watch_broker_order_id
      FROM captured_paper_post_commit_outbox o
      JOIN adaptive_risk_reservations r
        ON r.reservation_id = CAST(
             o.transport_authority_canonical_json::jsonb
                 ->> 'reservation_id' AS UUID
           )
      JOIN broker_symbol_action_claims c
        ON c.account_scope = o.account_scope AND c.symbol = o.symbol
      JOIN trading_automation_sessions s ON s.id = o.session_id
 LEFT JOIN captured_paper_completed_fill_watch w
        ON w.completion_sha256 = o.completion_sha256
     WHERE o.account_scope = 'alpaca:paper'
       AND o.expected_account_id = CAST(:expected_account_id AS UUID)
       AND (
            o.status <> 'completed'
            OR r.state IN (
                'reserved', 'submitted', 'submit_indeterminate',
                'partially_filled', 'filled', 'flat_pending_settlement',
                'exposure_quarantined'
            )
            OR r.open_quantity_shares > 0
            OR w.state IN ('pending', 'leased', 'retry_wait')
       )
  ORDER BY o.completion_sha256
    """
)


def _verified_fill_inventory(
    db: Session,
    *,
    reservation_id: str,
    expected_account_identity_sha256: str,
    expected_client_order_id: str,
    expected_symbol: str,
    expected_entry_broker_order_id: str | None,
    expected_broker_connection_generation: str | None,
) -> tuple[str, int, int, str | None]:
    """Verify one reservation's immutable fill chain and economic projection."""

    try:
        rows = list(
            db.scalars(
                select(AlpacaPaperFillActivity)
                .where(
                    AlpacaPaperFillActivity.reservation_id
                    == uuid.UUID(reservation_id)
                )
                .order_by(AlpacaPaperFillActivity.sequence)
            )
        )
        verify_alpaca_paper_fill_activity_chain(rows)
    except Exception as exc:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_fill_activity_chain_invalid"
        ) from exc
    inventory: list[dict[str, Any]] = []
    entry_quantity = 0
    exit_quantity = 0
    entry_notional = Decimal("0")
    for row in rows:
        if not (
            row.capture_schema_version == AUTHORITATIVE_CAPTURE_SCHEMA_VERSION
            and row.capture_authority_status == "verified"
            and row.account_scope == ALPACA_PAPER_ACCOUNT_SCOPE
            and row.account_identity_sha256
            == expected_account_identity_sha256
            and row.execution_family == "alpaca_spot"
            and row.position_direction == "long"
            and row.cycle_client_order_id == expected_client_order_id
            and row.symbol == expected_symbol
            and row.provider_event_clock_status == "authoritative"
            and row.provider_client_order_id_status == "authoritative"
            and row.fee_status == "authoritative"
            and row.immutable_fill_identity_sha256 is not None
        ):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_fill_activity_not_authoritative"
            )
        if (
            expected_broker_connection_generation is not None
            and row.broker_connection_generation
            != expected_broker_connection_generation
        ):
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_fill_connection_generation_mismatch"
            )
        quantity = _whole_shares(
            row.quantity,
            field_name="verified_fill_quantity",
            allow_zero=False,
        )
        price = _decimal(
            row.price, field_name="verified_fill_price", positive=True
        )
        if row.order_role == "entry":
            if not (
                row.side == "buy"
                and row.order_ownership_status == "reservation_bound"
                and row.provider_client_order_id == expected_client_order_id
                and row.provider_order_id == row.entry_provider_order_id
                and (
                    expected_entry_broker_order_id is None
                    or row.provider_order_id == expected_entry_broker_order_id
                )
            ):
                raise CapturedPaperRestartInventoryError(
                    "restart_inventory_entry_fill_owner_mismatch"
                )
            entry_quantity += quantity
            entry_notional += price * Decimal(quantity)
        elif row.order_role == "exit":
            if not (
                row.side == "sell"
                and row.order_ownership_status == "authoritative"
                and row.provider_order_id != row.entry_provider_order_id
            ):
                raise CapturedPaperRestartInventoryError(
                    "restart_inventory_exit_fill_owner_mismatch"
                )
            exit_quantity += quantity
        else:
            raise CapturedPaperRestartInventoryError(
                "restart_inventory_fill_order_role_invalid"
            )
        inventory.append(
            {
                "sequence": int(row.sequence),
                "event_sha256": _digest(
                    row.event_sha256, field_name="fill_event_sha256"
                ),
                "record_content_sha256": _digest(
                    row.record_content_sha256,
                    field_name="fill_record_content_sha256",
                ),
                "immutable_fill_identity_sha256": _digest(
                    row.immutable_fill_identity_sha256,
                    field_name="immutable_fill_identity_sha256",
                ),
                "order_role": row.order_role,
                "provider_order_id": row.provider_order_id,
                "quantity": quantity,
                "price": format(price, "f"),
            }
        )
    average = (
        None
        if entry_quantity == 0
        else format(entry_notional / Decimal(entry_quantity), "f")
    )
    return _sha256_json(inventory), entry_quantity, exit_quantity, average


def load_captured_paper_restart_lineages(
    bind: Engine,
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
) -> tuple[CapturedPaperRestartLineage, ...]:
    """Read and rehash the complete relevant durable restart inventory.

    PostgreSQL ``REPEATABLE READ READ ONLY`` makes the multi-table inventory one
    snapshot.  This function never commits a mutation and invokes only the
    outbox's verified read loader for each immutable completion.
    """

    if not isinstance(bind, Engine):
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_database_engine_invalid"
        )
    account_id = _canonical_uuid(
        expected_account_id, field_name="expected_account_id"
    )
    runtime_generation = _canonical_uuid(
        expected_runtime_generation, field_name="runtime_generation"
    )
    try:
        with Session(bind=bind) as db:
            db.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
            )
            rows = db.execute(
                _RESTART_ROWS_SQL,
                {"expected_account_id": account_id},
            ).mappings().all()
            orphan_reservations = db.execute(
                text(
                    """
                    SELECT r.reservation_id::text
                      FROM adaptive_risk_reservations r
                     WHERE r.account_scope = 'alpaca:paper'
                       AND (
                            r.state IN (
                                'reserved', 'submitted', 'submit_indeterminate',
                                'partially_filled', 'filled',
                                'flat_pending_settlement',
                                'exposure_quarantined'
                            )
                            OR r.open_quantity_shares > 0
                       )
                       AND NOT EXISTS (
                            SELECT 1
                              FROM captured_paper_post_commit_outbox o
                             WHERE o.account_scope = 'alpaca:paper'
                               AND o.expected_account_id =
                                   CAST(:expected_account_id AS UUID)
                               AND CAST(
                                    o.transport_authority_canonical_json::jsonb
                                        ->> 'reservation_id' AS UUID
                                   ) = r.reservation_id
                       )
                  ORDER BY r.reservation_id
                    """
                ),
                {"expected_account_id": account_id},
            ).scalars().all()
            if orphan_reservations:
                raise CapturedPaperRestartInventoryError(
                    "restart_inventory_unbound_active_reservation"
                )

            lineages: list[CapturedPaperRestartLineage] = []
            for row in rows:
                record = load_captured_paper_outbox(
                    db,
                    completion_sha256=str(row["completion_sha256"]),
                    for_update=False,
                )
                request = record.request
                route = request.intent.route_token
                authority = record.durable_transport.authority
                order_request = dict(record.durable_transport.order_request)
                claim_metadata = row["claim_metadata"]
                claim_metadata = (
                    dict(claim_metadata)
                    if isinstance(claim_metadata, Mapping)
                    else {}
                )
                session_snapshot = row["risk_snapshot_json"]
                session_snapshot = (
                    dict(session_snapshot)
                    if isinstance(session_snapshot, Mapping)
                    else {}
                )
                admission = session_snapshot.get("captured_paper_admission")
                admission = (
                    dict(admission) if isinstance(admission, Mapping) else {}
                )
                if not (
                    route.expected_account_id == account_id
                    and route.runtime_generation == runtime_generation
                    and authority.expected_account_id == account_id
                    and authority.reservation_id == row["reservation_id"]
                    and row["claim_action"] == "entry"
                    and row["claim_token"] == request.intent.symbol_claim_token
                    and row["claim_client_order_id"]
                    == request.intent.client_order_id
                    and int(row["owner_session_id"] or 0) == route.session_id
                    and claim_metadata.get("alpaca_account_id") == account_id
                    and claim_metadata.get("captured_paper_runtime_generation")
                    == runtime_generation
                    and claim_metadata.get("captured_paper_completion_sha256")
                    == request.completion_sha256
                    and claim_metadata.get("adaptive_risk_reservation_id")
                    == row["reservation_id"]
                    and claim_metadata.get("order_request") == order_request
                    and row["session_mode"] == "live"
                    and row["execution_family"] == "alpaca_spot"
                    and str(row["session_symbol"] or "").strip().upper()
                    == route.symbol
                    and row["session_ended_at"] is None
                    and session_snapshot.get("alpaca_account_scope")
                    == ALPACA_PAPER_ACCOUNT_SCOPE
                    and session_snapshot.get("alpaca_account_id") == account_id
                    and admission.get("completion_sha256")
                    == request.completion_sha256
                    and admission.get("runtime_generation")
                    == runtime_generation
                    and admission.get("reservation_id") == row["reservation_id"]
                    and admission.get("order_request_sha256")
                    == record.durable_transport.order_request_sha256
                ):
                    raise CapturedPaperRestartInventoryError(
                        "restart_inventory_durable_lineage_mismatch"
                    )
                (
                    fill_inventory_sha256,
                    entry_fill_quantity,
                    exit_fill_quantity,
                    entry_average_price,
                ) = _verified_fill_inventory(
                    db,
                    reservation_id=authority.reservation_id,
                    expected_account_identity_sha256=(
                        authority.account_identity_sha256
                    ),
                    expected_client_order_id=request.intent.client_order_id,
                    expected_symbol=route.symbol,
                    expected_entry_broker_order_id=(
                        None
                        if row["reservation_broker_order_id"] is None
                        else str(row["reservation_broker_order_id"])
                    ),
                    expected_broker_connection_generation=(
                        None
                        if row["broker_connection_generation"] is None
                        else str(row["broker_connection_generation"])
                    ),
                )
                lineages.append(
                    CapturedPaperRestartLineage(
                        completion_sha256=request.completion_sha256,
                        payload_sha256=record.payload_sha256,
                        route_token_sha256=route.route_token_sha256,
                        runtime_generation=route.runtime_generation,
                        expected_account_id=route.expected_account_id,
                        session_id=route.session_id,
                        symbol=route.symbol,
                        client_order_id=request.intent.client_order_id,
                        binder_id=request.intent.binder_id,
                        action_claim_token=request.intent.symbol_claim_token,
                        reservation_id=authority.reservation_id,
                        order_request_sha256=(
                            record.durable_transport.order_request_sha256
                        ),
                        order_request=order_request,
                        outbox_status=record.status,
                        outbox_transport_started=(
                            record.transport_started_at is not None
                        ),
                        action_claim_phase=str(row["claim_phase"]),
                        action_claim_client_order_id=str(
                            row["claim_client_order_id"]
                        ),
                        action_claim_broker_order_id=(
                            None
                            if row["claim_broker_order_id"] is None
                            else str(row["claim_broker_order_id"])
                        ),
                        reservation_state=str(row["reservation_state"]),
                        planned_quantity_shares=int(
                            row["planned_quantity_shares"]
                        ),
                        cumulative_filled_quantity_shares=int(
                            row["cumulative_filled_quantity_shares"]
                        ),
                        open_quantity_shares=int(row["open_quantity_shares"]),
                        reservation_broker_order_id=(
                            None
                            if row["reservation_broker_order_id"] is None
                            else str(row["reservation_broker_order_id"])
                        ),
                        reservation_broker_connection_generation=(
                            None
                            if row["broker_connection_generation"] is None
                            else str(row["broker_connection_generation"])
                        ),
                        fill_activity_inventory_sha256=(
                            fill_inventory_sha256
                        ),
                        verified_entry_fill_quantity_shares=(
                            entry_fill_quantity
                        ),
                        verified_exit_fill_quantity_shares=(
                            exit_fill_quantity
                        ),
                        verified_entry_average_price=entry_average_price,
                        session_state=str(row["session_state"] or ""),
                        fill_watch_state=(
                            None
                            if row["fill_watch_state"] is None
                            else str(row["fill_watch_state"])
                        ),
                        fill_watch_broker_order_id=(
                            None
                            if row["fill_watch_broker_order_id"] is None
                            else str(row["fill_watch_broker_order_id"])
                        ),
                    )
                )
            return tuple(lineages)
    except CapturedPaperRestartInventoryError:
        raise
    except Exception as exc:
        raise CapturedPaperRestartInventoryError(
            "restart_inventory_database_read_failed"
        ) from exc


def build_captured_paper_restart_inventory_receipt(
    bind: Engine,
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_connection_generation: str,
    expected_adapter_build_sha256: str,
    expected_read_binding_sha256: str,
    open_order_census: Mapping[str, Any],
    position_census: Mapping[str, Any],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Read durable ownership then classify the two frozen broker censuses."""

    lineages = load_captured_paper_restart_lineages(
        bind,
        expected_account_id=expected_account_id,
        expected_runtime_generation=expected_runtime_generation,
    )
    return classify_captured_paper_restart_inventory(
        expected_account_id=expected_account_id,
        expected_runtime_generation=expected_runtime_generation,
        expected_connection_generation=expected_connection_generation,
        expected_adapter_build_sha256=expected_adapter_build_sha256,
        expected_read_binding_sha256=expected_read_binding_sha256,
        open_order_census=open_order_census,
        position_census=position_census,
        durable_lineages=lineages,
        observed_at=wall_clock(),
    )


__all__ = (
    "CapturedPaperRestartInventoryError",
    "CapturedPaperRestartLineage",
    "RESTART_INVENTORY_SCHEMA_VERSION",
    "build_captured_paper_restart_inventory_receipt",
    "classify_captured_paper_restart_inventory",
    "load_captured_paper_restart_lineages",
)
