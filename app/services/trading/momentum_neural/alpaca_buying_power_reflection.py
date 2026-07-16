"""Typed double-census authority for Alpaca PAPER buying-power reflection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from .alpaca_bp_census_capability import (
    AlpacaBuyingPowerCensusCapabilityError,
    verify_alpaca_bp_census_capability,
)
from .alpaca_paper_identity import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    alpaca_paper_account_identity_sha256,
    canonical_alpaca_paper_account_id,
)


UTC = timezone.utc
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CENSUS_BINDING_SCHEMA = "chili.alpaca-paper-open-order-census-binding.v1"
_BATCH_SCHEMA = "chili.alpaca-paper-bp-double-census.v1"


class AlpacaBuyingPowerReflectionError(RuntimeError):
    """A stable exact broker-order/account bracket could not be proven."""


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlpacaBuyingPowerReflectionError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise AlpacaBuyingPowerReflectionError(f"{field} is required")
    return normalized


def _sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA_RE.fullmatch(normalized) is None:
        raise AlpacaBuyingPowerReflectionError(f"{field} must be SHA-256")
    return normalized


def _canonical(value: Any, field: str) -> tuple[str, str]:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AlpacaBuyingPowerReflectionError(
            f"{field} is not canonical JSON"
        ) from exc
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _binding(*, decision_id: str, account_id: str, phase: str) -> dict[str, Any]:
    if phase not in {"before_account", "after_account"}:
        raise AlpacaBuyingPowerReflectionError("open-order census phase is invalid")
    return {
        "schema_version": _CENSUS_BINDING_SCHEMA,
        "decision_id": _text(decision_id, "decision_id"),
        "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
        "expected_account_id": canonical_alpaca_paper_account_id(account_id),
        "phase": phase,
    }


@dataclass(frozen=True)
class PreparedAlpacaPaperOpenOrderCensus:
    phase: str
    decision_id: str
    provider_account_id: str
    account_identity_sha256: str
    adapter_connection_generation: str
    adapter_build_sha256: str
    read_binding_canonical_json: str
    read_binding_sha256: str
    query_receipt_canonical_json: str
    query_receipt_sha256: str
    inventory_canonical_json: str
    inventory_sha256: str
    exact_order_count: int
    requested_at: datetime
    received_at: datetime
    available_at: datetime
    expires_at: datetime
    census_content_sha256: str
    _capture_capability: object = field(repr=False, compare=False)

    @property
    def orders(self) -> tuple[dict[str, Any], ...]:
        decoded = json.loads(self.inventory_canonical_json)
        if not isinstance(decoded, list) or any(
            not isinstance(row, dict) for row in decoded
        ):
            raise AlpacaBuyingPowerReflectionError(
                "open-order census inventory is malformed"
            )
        return tuple(decoded)

    def public_body(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.alpaca-paper-open-order-census-prepared.v1",
            "phase": self.phase,
            "decision_id": self.decision_id,
            "provider_account_id": self.provider_account_id,
            "account_identity_sha256": self.account_identity_sha256,
            "adapter_connection_generation": self.adapter_connection_generation,
            "adapter_build_sha256": self.adapter_build_sha256,
            "read_binding_canonical_json": self.read_binding_canonical_json,
            "read_binding_sha256": self.read_binding_sha256,
            "query_receipt_canonical_json": self.query_receipt_canonical_json,
            "query_receipt_sha256": self.query_receipt_sha256,
            "inventory_canonical_json": self.inventory_canonical_json,
            "inventory_sha256": self.inventory_sha256,
            "exact_order_count": self.exact_order_count,
            "requested_at": self.requested_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    def __reduce__(self):
        raise TypeError("prepared Alpaca PAPER census cannot be serialized")


@dataclass(frozen=True)
class PreparedAlpacaPaperBuyingPowerDoubleCensus:
    account_authority: Any = field(repr=False, compare=False)
    before: PreparedAlpacaPaperOpenOrderCensus
    after: PreparedAlpacaPaperOpenOrderCensus
    batch_content_sha256: str

    def public_body(self) -> dict[str, Any]:
        authority = self.account_authority
        return {
            "schema_version": _BATCH_SCHEMA,
            "decision_id": authority.decision_id,
            "run_id": authority.run_id,
            "generation": int(authority.generation),
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "account_id": authority.account_id,
            "account_identity_sha256": authority.account_identity_sha256,
            "account_snapshot_id": authority.snapshot_id,
            "account_payload_sha256": authority.account_payload_sha256,
            "account_read_receipt_sha256": authority.account_read_receipt_sha256,
            "account_observed_at": authority.observed_at.isoformat(),
            "account_available_at": authority.available_at.isoformat(),
            "account_buying_power_usd": str(authority.buying_power_usd),
            "before_census_content_sha256": self.before.census_content_sha256,
            "after_census_content_sha256": self.after.census_content_sha256,
        }

    def __reduce__(self):
        raise TypeError("Alpaca PAPER double census cannot be serialized")


def _exact_adapter(adapter: Any) -> tuple[Any, str]:
    from ..venue.alpaca_spot import AlpacaSpotAdapter
    from .captured_alpaca_paper_adapter import CapturedAlpacaPaperAdapter

    if type(adapter) is AlpacaSpotAdapter:
        exact = adapter
    elif type(adapter) is CapturedAlpacaPaperAdapter:
        exact = getattr(adapter, "_adapter", None)
        if type(exact) is not AlpacaSpotAdapter:
            exact = None
    else:
        exact = None
    if exact is None:
        raise AlpacaBuyingPowerReflectionError(
            "exact Alpaca PAPER adapter is required for open-order census"
        )
    account_id = canonical_alpaca_paper_account_id(
        getattr(exact, "bound_account_id", None)
    )
    return exact, account_id


def _capability_payload(census: PreparedAlpacaPaperOpenOrderCensus) -> dict[str, Any]:
    return {
        "schema_version": "chili.alpaca-paper-bp-census-capability.v1",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "provider_account_id": census.provider_account_id,
        "adapter_connection_generation": census.adapter_connection_generation,
        "adapter_build_sha256": census.adapter_build_sha256,
        "read_binding_sha256": census.read_binding_sha256,
        "query_receipt_sha256": census.query_receipt_sha256,
        "inventory_sha256": census.inventory_sha256,
        "available_at": census.available_at.isoformat(),
        "expires_at": census.expires_at.isoformat(),
    }


def verify_alpaca_paper_open_order_census(
    census: PreparedAlpacaPaperOpenOrderCensus,
    *,
    verified_at: datetime,
) -> PreparedAlpacaPaperOpenOrderCensus:
    if type(census) is not PreparedAlpacaPaperOpenOrderCensus:
        raise AlpacaBuyingPowerReflectionError("open-order census type is invalid")
    expected_binding = _binding(
        decision_id=census.decision_id,
        account_id=census.provider_account_id,
        phase=census.phase,
    )
    binding_json, binding_sha = _canonical(expected_binding, "census binding")
    if (
        census.read_binding_canonical_json != binding_json
        or census.read_binding_sha256 != binding_sha
        or hashlib.sha256(
            census.query_receipt_canonical_json.encode("utf-8")
        ).hexdigest()
        != census.query_receipt_sha256
        or hashlib.sha256(
            census.inventory_canonical_json.encode("utf-8")
        ).hexdigest()
        != census.inventory_sha256
        or census.account_identity_sha256
        != alpaca_paper_account_identity_sha256(census.provider_account_id)
        or len(census.orders) != int(census.exact_order_count)
        or not (
            _utc(census.requested_at, "census.requested_at")
            <= _utc(census.received_at, "census.received_at")
            <= _utc(census.available_at, "census.available_at")
            < _utc(census.expires_at, "census.expires_at")
        )
    ):
        raise AlpacaBuyingPowerReflectionError(
            "open-order census public evidence changed"
        )
    _sha(census.adapter_build_sha256, "adapter_build_sha256")
    _, content_sha = _canonical(census.public_body(), "census content")
    if content_sha != census.census_content_sha256:
        raise AlpacaBuyingPowerReflectionError("open-order census content changed")
    try:
        verify_alpaca_bp_census_capability(
            census._capture_capability,
            expected_payload=_capability_payload(census),
            verified_at=verified_at,
        )
    except AlpacaBuyingPowerCensusCapabilityError as exc:
        raise AlpacaBuyingPowerReflectionError(
            "open-order census private authority failed"
        ) from exc
    return census


def read_verified_alpaca_paper_open_order_census(
    adapter: Any,
    *,
    decision_id: str,
    phase: str,
) -> PreparedAlpacaPaperOpenOrderCensus:
    exact, account_id = _exact_adapter(adapter)
    binding = _binding(
        decision_id=decision_id,
        account_id=account_id,
        phase=phase,
    )
    from ..venue.alpaca_spot import AlpacaSpotAdapter

    raw = AlpacaSpotAdapter.get_paper_open_order_census(
        exact,
        read_binding=binding,
    )
    if not isinstance(raw, Mapping) or not (
        raw.get("readable") is True
        and raw.get("pagination_complete") is True
        and raw.get("broker_environment") == "paper"
        and raw.get("asset_class") == "us_equity"
    ):
        raise AlpacaBuyingPowerReflectionError(
            "Alpaca PAPER open-order census is unavailable"
        )
    binding_json, binding_sha = _canonical(binding, "census binding")
    inventory_json = _text(
        raw.get("inventory_canonical_json"), "inventory_canonical_json"
    )
    orders = raw.get("orders")
    if not isinstance(orders, list):
        raise AlpacaBuyingPowerReflectionError("open-order inventory is malformed")
    canonical_orders, inventory_sha = _canonical(orders, "open-order inventory")
    if canonical_orders != inventory_json:
        raise AlpacaBuyingPowerReflectionError("open-order inventory bytes changed")
    census = PreparedAlpacaPaperOpenOrderCensus(
        phase=phase,
        decision_id=_text(decision_id, "decision_id"),
        provider_account_id=canonical_alpaca_paper_account_id(
            raw.get("provider_account_id")
        ),
        account_identity_sha256=alpaca_paper_account_identity_sha256(account_id),
        adapter_connection_generation=_text(
            raw.get("adapter_connection_generation"),
            "adapter_connection_generation",
        ),
        adapter_build_sha256=_sha(
            raw.get("adapter_build_sha256"), "adapter_build_sha256"
        ),
        read_binding_canonical_json=_text(
            raw.get("read_binding_canonical_json"),
            "read_binding_canonical_json",
        ),
        read_binding_sha256=_sha(
            raw.get("read_binding_sha256"), "read_binding_sha256"
        ),
        query_receipt_canonical_json=_text(
            raw.get("query_receipt_canonical_json"),
            "query_receipt_canonical_json",
        ),
        query_receipt_sha256=_sha(
            raw.get("query_receipt_sha256"), "query_receipt_sha256"
        ),
        inventory_canonical_json=inventory_json,
        inventory_sha256=_sha(raw.get("inventory_sha256"), "inventory_sha256"),
        exact_order_count=len(orders),
        requested_at=_utc(raw.get("requested_at"), "requested_at"),
        received_at=_utc(raw.get("received_at"), "received_at"),
        available_at=_utc(raw.get("available_at"), "available_at"),
        expires_at=_utc(raw.get("expires_at"), "expires_at"),
        census_content_sha256="0" * 64,
        _capture_capability=raw.get("_capture_capability"),
    )
    if (
        census.provider_account_id != account_id
        or census.read_binding_canonical_json != binding_json
        or census.read_binding_sha256 != binding_sha
        or census.inventory_sha256 != inventory_sha
    ):
        raise AlpacaBuyingPowerReflectionError(
            "open-order census differs from its exact binding"
        )
    _, content_sha = _canonical(census.public_body(), "census content")
    object.__setattr__(census, "census_content_sha256", content_sha)
    return verify_alpaca_paper_open_order_census(
        census,
        verified_at=datetime.now(UTC),
    )


def prepare_alpaca_paper_buying_power_double_census(
    *,
    account_authority: Any,
    before: PreparedAlpacaPaperOpenOrderCensus,
    after: PreparedAlpacaPaperOpenOrderCensus,
    verified_at: datetime | None = None,
) -> PreparedAlpacaPaperBuyingPowerDoubleCensus:
    from .captured_alpaca_paper_adapter import (
        verify_captured_alpaca_paper_account_authority,
    )

    authority = verify_captured_alpaca_paper_account_authority(account_authority)
    checked_at = datetime.now(UTC) if verified_at is None else _utc(
        verified_at, "verified_at"
    )
    verify_alpaca_paper_open_order_census(before, verified_at=checked_at)
    verify_alpaca_paper_open_order_census(after, verified_at=checked_at)
    if not (
        before.phase == "before_account"
        and after.phase == "after_account"
        and before.decision_id == after.decision_id == authority.decision_id
        and before.provider_account_id
        == after.provider_account_id
        == authority.account_id
        and before.account_identity_sha256
        == after.account_identity_sha256
        == authority.account_identity_sha256
        and before.adapter_connection_generation
        == after.adapter_connection_generation
        and before.adapter_build_sha256 == after.adapter_build_sha256
        and before.inventory_sha256 == after.inventory_sha256
        and before.inventory_canonical_json == after.inventory_canonical_json
        and before.exact_order_count == after.exact_order_count
        and before.available_at
        <= authority.observed_at
        <= authority.available_at
        <= after.requested_at
        <= checked_at
        <= authority.expires_at
    ):
        raise AlpacaBuyingPowerReflectionError(
            "Alpaca PAPER account/open-order double census is unstable or non-causal"
        )
    provisional = PreparedAlpacaPaperBuyingPowerDoubleCensus(
        account_authority=authority,
        before=before,
        after=after,
        batch_content_sha256="0" * 64,
    )
    _, digest = _canonical(provisional.public_body(), "double census content")
    return PreparedAlpacaPaperBuyingPowerDoubleCensus(
        account_authority=authority,
        before=before,
        after=after,
        batch_content_sha256=digest,
    )


def verify_alpaca_paper_buying_power_double_census(
    value: PreparedAlpacaPaperBuyingPowerDoubleCensus,
    *,
    verified_at: datetime,
) -> PreparedAlpacaPaperBuyingPowerDoubleCensus:
    if type(value) is not PreparedAlpacaPaperBuyingPowerDoubleCensus:
        raise AlpacaBuyingPowerReflectionError("double census type is invalid")
    rebuilt = prepare_alpaca_paper_buying_power_double_census(
        account_authority=value.account_authority,
        before=value.before,
        after=value.after,
        verified_at=verified_at,
    )
    if rebuilt.batch_content_sha256 != value.batch_content_sha256:
        raise AlpacaBuyingPowerReflectionError("double census content changed")
    return value


__all__ = (
    "AlpacaBuyingPowerReflectionError",
    "PreparedAlpacaPaperBuyingPowerDoubleCensus",
    "PreparedAlpacaPaperOpenOrderCensus",
    "prepare_alpaca_paper_buying_power_double_census",
    "read_verified_alpaca_paper_open_order_census",
    "verify_alpaca_paper_buying_power_double_census",
    "verify_alpaca_paper_open_order_census",
)
