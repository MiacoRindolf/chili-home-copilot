"""Pure contracts for a two-phase captured Alpaca PAPER entry.

This module is deliberately incapable of opening a database transaction,
reserving risk, claiming an opportunity, constructing a broker adapter, or
submitting an order.  Phase one may persist a ``CapturedPaperEntryIntent`` and
commit its owning automation-session transaction.  A future, separately wired
phase-two owner may accept the matching ``CapturedPaperPostCommitRequest`` only
after it revalidates the route token against the freshly locked durable row.

The contracts are content-addressed and deeply scalar.  They intentionally do
not carry a quantity: final adaptive sizing belongs to the later account-locked
admission transaction, never to the preliminary route/planning transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from typing import Any, Protocol

from ..execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SPOT,
    normalize_execution_family,
)


UTC = timezone.utc
CAPTURED_PAPER_ROUTE_TOKEN_SCHEMA_VERSION = (
    "chili.captured-paper-route-token.v1"
)
CAPTURED_PAPER_CONFIRMED_ARM_SCHEMA_VERSION = (
    "chili.captured-paper-confirmed-arm.v1"
)
CAPTURED_PAPER_OPPORTUNITY_KEY_SCHEMA_VERSION = (
    "chili.captured-paper-opportunity-key.v1"
)
CAPTURED_PAPER_ENTRY_INTENT_SCHEMA_VERSION = (
    "chili.captured-paper-entry-intent.v2"
)
CAPTURED_PAPER_POST_COMMIT_SCHEMA_VERSION = (
    "chili.captured-paper-post-commit.v2"
)
ALPACA_PAPER_ACCOUNT_SCOPE = "alpaca:paper"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,35}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SETUP_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_FIRST_DIP_POLICY_MODES = frozenset({"baseline", "candidate", "promoted"})
_FIRST_DIP_SETUP_FAMILY = "first_dip_reclaim"


class CapturedPaperIntentContractError(ValueError):
    """A content-addressed PAPER contract is malformed or was mutated."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_contract_invalid")
        super().__init__(self.reason)


class CapturedPaperRouteDriftError(CapturedPaperIntentContractError):
    """The later durable route no longer matches its preliminary token."""


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _require_exact_keys(
    payload: Any,
    expected: frozenset[str],
    *,
    field_name: str,
) -> dict[str, Any]:
    if type(payload) is not dict or frozenset(payload) != expected:
        raise CapturedPaperIntentContractError(f"{field_name}_shape_invalid")
    return payload


def _canonical_uuid(value: str, *, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperIntentContractError(
            f"{field_name}_invalid"
        ) from exc
    canonical = str(parsed)
    if raw != canonical:
        raise CapturedPaperIntentContractError(f"{field_name}_invalid")
    return canonical


def _sha256(value: str, *, field_name: str) -> str:
    digest = str(value or "").strip()
    if _SHA256_RE.fullmatch(digest) is None:
        raise CapturedPaperIntentContractError(f"{field_name}_invalid")
    return digest


def _identifier(
    value: str,
    *,
    field_name: str,
    pattern: re.Pattern[str],
) -> str:
    text = str(value or "").strip()
    if text != value or pattern.fullmatch(text) is None:
        raise CapturedPaperIntentContractError(f"{field_name}_invalid")
    return text


def _positive_decimal(value: Any, *, field_name: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperIntentContractError(
            f"{field_name}_invalid"
        ) from exc
    if not number.is_finite() or number <= 0:
        raise CapturedPaperIntentContractError(f"{field_name}_invalid")
    canonical = format(number.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if not canonical or len(canonical) > 96:
        raise CapturedPaperIntentContractError(f"{field_name}_invalid")
    return canonical


def _decision_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperIntentContractError("decision_at_invalid")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CapturedPaperIntentContractError("decision_at_invalid") from exc
    if offset is None:
        raise CapturedPaperIntentContractError("decision_at_invalid")
    return value.astimezone(UTC)


def _date(value: date, *, field_name: str) -> date:
    if isinstance(value, datetime) or type(value) is not date:
        raise CapturedPaperIntentContractError(f"{field_name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class CapturedPaperConfirmedArmGeneration:
    """Exact final-lock Alpaca PAPER arm generation carried into phase two."""

    session_id: int
    arm_token: str
    expires_at: datetime
    symbol_claim_token: str
    account_scope: str
    expected_account_id: str
    confirmed_at: datetime
    confirmed_arm_generation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.session_id, bool) or int(self.session_id) <= 0:
            raise CapturedPaperIntentContractError(
                "confirmed_arm_session_id_invalid"
            )
        object.__setattr__(self, "session_id", int(self.session_id))
        arm_token = _canonical_uuid(
            self.arm_token,
            field_name="confirmed_arm_token",
        )
        object.__setattr__(self, "arm_token", arm_token)
        expected_claim = f"arm-{arm_token}"
        if self.symbol_claim_token != expected_claim:
            raise CapturedPaperIntentContractError(
                "confirmed_arm_symbol_claim_token_invalid"
            )
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperIntentContractError(
                "confirmed_arm_account_scope_invalid"
            )
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id,
                field_name="confirmed_arm_account_id",
            ),
        )
        expires_at = _decision_at(self.expires_at)
        confirmed_at = _decision_at(self.confirmed_at)
        if confirmed_at >= expires_at:
            raise CapturedPaperIntentContractError(
                "confirmed_arm_clock_order_invalid"
            )
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(
            self,
            "confirmed_arm_generation_sha256",
            _sha256_json(self._content_payload()),
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURED_PAPER_CONFIRMED_ARM_SCHEMA_VERSION,
            "session_id": self.session_id,
            "arm_token": self.arm_token,
            "expires_at": self.expires_at.isoformat(),
            "symbol_claim_token": self.symbol_claim_token,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "confirmed_at": self.confirmed_at.isoformat(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "confirmed_arm_generation_sha256": (
                self.confirmed_arm_generation_sha256
            ),
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CapturedPaperConfirmedArmGeneration":
        data = _require_exact_keys(
            payload,
            frozenset(
                {
                    "schema_version",
                    "session_id",
                    "arm_token",
                    "expires_at",
                    "symbol_claim_token",
                    "account_scope",
                    "expected_account_id",
                    "confirmed_at",
                    "confirmed_arm_generation_sha256",
                }
            ),
            field_name="confirmed_arm_payload",
        )
        if data["schema_version"] != CAPTURED_PAPER_CONFIRMED_ARM_SCHEMA_VERSION:
            raise CapturedPaperIntentContractError(
                "confirmed_arm_schema_version_invalid"
            )
        try:
            value = cls(
                session_id=data["session_id"],
                arm_token=data["arm_token"],
                expires_at=datetime.fromisoformat(data["expires_at"]),
                symbol_claim_token=data["symbol_claim_token"],
                account_scope=data["account_scope"],
                expected_account_id=data["expected_account_id"],
                confirmed_at=datetime.fromisoformat(data["confirmed_at"]),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CapturedPaperIntentContractError):
                raise
            raise CapturedPaperIntentContractError(
                "confirmed_arm_payload_invalid"
            ) from exc
        supplied = _sha256(
            data["confirmed_arm_generation_sha256"],
            field_name="confirmed_arm_generation_sha256",
        )
        if supplied != value.confirmed_arm_generation_sha256:
            raise CapturedPaperIntentContractError(
                "confirmed_arm_generation_hash_mismatch"
            )
        return value

    def verify(self) -> None:
        canonical = self.from_payload(self.to_payload())
        if (
            canonical.confirmed_arm_generation_sha256
            != self.confirmed_arm_generation_sha256
        ):
            raise CapturedPaperIntentContractError(
                "confirmed_arm_generation_hash_mismatch"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperOpportunityKey:
    """Typed once-per-ET-day key; present only for first-dip decisions."""

    account_scope: str
    symbol: str
    trading_date: date
    setup_family: str
    opportunity_key_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperIntentContractError(
                "opportunity_account_scope_invalid"
            )
        symbol = str(self.symbol or "").strip().upper()
        if self.symbol != symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            raise CapturedPaperIntentContractError(
                "opportunity_symbol_invalid"
            )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self,
            "trading_date",
            _date(self.trading_date, field_name="opportunity_trading_date"),
        )
        if self.setup_family != _FIRST_DIP_SETUP_FAMILY:
            raise CapturedPaperIntentContractError(
                "opportunity_setup_family_invalid"
            )
        object.__setattr__(
            self,
            "opportunity_key_sha256",
            _sha256_json(self._content_payload()),
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURED_PAPER_OPPORTUNITY_KEY_SCHEMA_VERSION,
            "account_scope": self.account_scope,
            "symbol": self.symbol,
            "trading_date": self.trading_date.isoformat(),
            "setup_family": self.setup_family,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "opportunity_key_sha256": self.opportunity_key_sha256,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CapturedPaperOpportunityKey":
        data = _require_exact_keys(
            payload,
            frozenset(
                {
                    "schema_version",
                    "account_scope",
                    "symbol",
                    "trading_date",
                    "setup_family",
                    "opportunity_key_sha256",
                }
            ),
            field_name="opportunity_payload",
        )
        if data["schema_version"] != CAPTURED_PAPER_OPPORTUNITY_KEY_SCHEMA_VERSION:
            raise CapturedPaperIntentContractError(
                "opportunity_schema_version_invalid"
            )
        try:
            value = cls(
                account_scope=data["account_scope"],
                symbol=data["symbol"],
                trading_date=date.fromisoformat(data["trading_date"]),
                setup_family=data["setup_family"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CapturedPaperIntentContractError):
                raise
            raise CapturedPaperIntentContractError(
                "opportunity_payload_invalid"
            ) from exc
        supplied = _sha256(
            data["opportunity_key_sha256"],
            field_name="opportunity_key_sha256",
        )
        if supplied != value.opportunity_key_sha256:
            raise CapturedPaperIntentContractError(
                "opportunity_key_hash_mismatch"
            )
        return value

    def verify(self) -> None:
        self.from_payload(self.to_payload())


@dataclass(frozen=True, slots=True)
class CapturedPaperRouteToken:
    """Immutable result of the preliminary, deliberately unlocked route read."""

    session_id: int
    symbol: str
    execution_family: str
    account_scope: str
    expected_account_id: str
    code_build_sha256: str
    config_sha256: str
    capture_receipt_sha256: str
    runtime_generation: str
    first_dip_policy_mode: str
    route_token_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.session_id, bool) or int(self.session_id) <= 0:
            raise CapturedPaperIntentContractError("route_session_id_invalid")
        object.__setattr__(self, "session_id", int(self.session_id))
        symbol = str(self.symbol or "").strip().upper()
        if self.symbol != symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            raise CapturedPaperIntentContractError("route_symbol_invalid")
        object.__setattr__(self, "symbol", symbol)
        family = normalize_execution_family(self.execution_family)
        if family != EXECUTION_FAMILY_ALPACA_SPOT:
            raise CapturedPaperIntentContractError(
                "route_execution_family_invalid"
            )
        object.__setattr__(self, "execution_family", family)
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperIntentContractError("route_account_scope_invalid")
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id,
                field_name="route_account_id",
            ),
        )
        for name in (
            "code_build_sha256",
            "config_sha256",
            "capture_receipt_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=f"route_{name}"),
            )
        object.__setattr__(
            self,
            "runtime_generation",
            _canonical_uuid(
                self.runtime_generation,
                field_name="route_runtime_generation",
            ),
        )
        mode = str(self.first_dip_policy_mode or "").strip().lower()
        if mode != self.first_dip_policy_mode or mode not in _FIRST_DIP_POLICY_MODES:
            raise CapturedPaperIntentContractError("route_policy_mode_invalid")
        object.__setattr__(self, "first_dip_policy_mode", mode)
        object.__setattr__(
            self,
            "route_token_sha256",
            _sha256_json(self._content_payload()),
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURED_PAPER_ROUTE_TOKEN_SCHEMA_VERSION,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "execution_family": self.execution_family,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "capture_receipt_sha256": self.capture_receipt_sha256,
            "runtime_generation": self.runtime_generation,
            "first_dip_policy_mode": self.first_dip_policy_mode,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "route_token_sha256": self.route_token_sha256,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CapturedPaperRouteToken":
        data = _require_exact_keys(
            payload,
            frozenset(
                {
                    "schema_version",
                    "session_id",
                    "symbol",
                    "execution_family",
                    "account_scope",
                    "expected_account_id",
                    "code_build_sha256",
                    "config_sha256",
                    "capture_receipt_sha256",
                    "runtime_generation",
                    "first_dip_policy_mode",
                    "route_token_sha256",
                }
            ),
            field_name="route_token_payload",
        )
        if data["schema_version"] != CAPTURED_PAPER_ROUTE_TOKEN_SCHEMA_VERSION:
            raise CapturedPaperIntentContractError(
                "route_token_schema_version_invalid"
            )
        try:
            value = cls(
                session_id=data["session_id"],
                symbol=data["symbol"],
                execution_family=data["execution_family"],
                account_scope=data["account_scope"],
                expected_account_id=data["expected_account_id"],
                code_build_sha256=data["code_build_sha256"],
                config_sha256=data["config_sha256"],
                capture_receipt_sha256=data["capture_receipt_sha256"],
                runtime_generation=data["runtime_generation"],
                first_dip_policy_mode=data["first_dip_policy_mode"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CapturedPaperIntentContractError):
                raise
            raise CapturedPaperIntentContractError(
                "route_token_payload_invalid"
            ) from exc
        supplied = _sha256(
            data["route_token_sha256"],
            field_name="route_token_sha256",
        )
        if supplied != value.route_token_sha256:
            raise CapturedPaperIntentContractError(
                "route_token_content_hash_mismatch"
            )
        return value

    def verify(self) -> None:
        canonical = self.from_payload(self.to_payload())
        if canonical.route_token_sha256 != self.route_token_sha256:
            raise CapturedPaperIntentContractError(
                "route_token_content_hash_mismatch"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperEntryIntent:
    """Phase-one setup intent; never final sizing or order authority."""

    route_token: CapturedPaperRouteToken
    confirmed_arm_generation: CapturedPaperConfirmedArmGeneration
    symbol_claim_token: str
    binder_id: str
    opportunity_key: CapturedPaperOpportunityKey | None
    intent_generation: str
    decision_id: str
    client_order_id: str
    setup_family: str
    decision_at: datetime
    structural_stop_price: str
    entry_limit_ceiling_price: str
    account_receipt_sha256: str
    bbo_receipt_sha256: str
    setup_evidence_sha256: str
    policy_sha256: str
    feature_flags_sha256: str
    intent_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.route_token) is not CapturedPaperRouteToken:
            raise CapturedPaperIntentContractError("intent_route_token_invalid")
        self.route_token.verify()
        if type(self.confirmed_arm_generation) is not CapturedPaperConfirmedArmGeneration:
            raise CapturedPaperIntentContractError(
                "intent_confirmed_arm_generation_invalid"
            )
        self.confirmed_arm_generation.verify()
        arm = self.confirmed_arm_generation
        if (
            arm.session_id != self.route_token.session_id
            or arm.account_scope != self.route_token.account_scope
            or arm.expected_account_id != self.route_token.expected_account_id
        ):
            raise CapturedPaperIntentContractError(
                "intent_confirmed_arm_route_mismatch"
            )
        if self.symbol_claim_token != arm.symbol_claim_token:
            raise CapturedPaperIntentContractError(
                "intent_symbol_claim_token_mismatch"
            )
        object.__setattr__(
            self,
            "binder_id",
            _canonical_uuid(self.binder_id, field_name="intent_binder_id"),
        )
        object.__setattr__(
            self,
            "intent_generation",
            _canonical_uuid(
                self.intent_generation,
                field_name="intent_generation",
            ),
        )
        object.__setattr__(
            self,
            "decision_id",
            _identifier(
                self.decision_id,
                field_name="decision_id",
                pattern=_IDENTIFIER_RE,
            ),
        )
        object.__setattr__(
            self,
            "client_order_id",
            _identifier(
                self.client_order_id,
                field_name="client_order_id",
                pattern=_CLIENT_ORDER_ID_RE,
            ),
        )
        if self.decision_id != self.client_order_id:
            raise CapturedPaperIntentContractError(
                "decision_client_order_id_mismatch"
            )
        setup = str(self.setup_family or "").strip().lower()
        if setup != self.setup_family or _SETUP_FAMILY_RE.fullmatch(setup) is None:
            raise CapturedPaperIntentContractError("setup_family_invalid")
        object.__setattr__(self, "setup_family", setup)
        decision_at = _decision_at(self.decision_at)
        if not (arm.confirmed_at <= decision_at <= arm.expires_at):
            raise CapturedPaperIntentContractError(
                "entry_intent_arm_clock_invalid"
            )
        object.__setattr__(self, "decision_at", decision_at)
        if setup == _FIRST_DIP_SETUP_FAMILY:
            if type(self.opportunity_key) is not CapturedPaperOpportunityKey:
                raise CapturedPaperIntentContractError(
                    "first_dip_opportunity_key_missing"
                )
            self.opportunity_key.verify()
            if (
                self.opportunity_key.account_scope != self.route_token.account_scope
                or self.opportunity_key.symbol != self.route_token.symbol
                or self.opportunity_key.setup_family != setup
            ):
                raise CapturedPaperIntentContractError(
                    "first_dip_opportunity_key_mismatch"
                )
        elif self.opportunity_key is not None:
            raise CapturedPaperIntentContractError(
                "non_first_dip_opportunity_key_prohibited"
            )
        stop = _positive_decimal(
            self.structural_stop_price,
            field_name="structural_stop_price",
        )
        ceiling = _positive_decimal(
            self.entry_limit_ceiling_price,
            field_name="entry_limit_ceiling_price",
        )
        if Decimal(stop) >= Decimal(ceiling):
            raise CapturedPaperIntentContractError(
                "entry_intent_price_order_invalid"
            )
        object.__setattr__(self, "structural_stop_price", stop)
        object.__setattr__(self, "entry_limit_ceiling_price", ceiling)
        for name in (
            "account_receipt_sha256",
            "bbo_receipt_sha256",
            "setup_evidence_sha256",
            "policy_sha256",
            "feature_flags_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "intent_sha256",
            _sha256_json(self._content_payload()),
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURED_PAPER_ENTRY_INTENT_SCHEMA_VERSION,
            "route_token_sha256": self.route_token.route_token_sha256,
            "confirmed_arm_generation_sha256": (
                self.confirmed_arm_generation.confirmed_arm_generation_sha256
            ),
            "symbol_claim_token": self.symbol_claim_token,
            "binder_id": self.binder_id,
            "opportunity_key_sha256": (
                self.opportunity_key.opportunity_key_sha256
                if self.opportunity_key is not None
                else None
            ),
            "intent_generation": self.intent_generation,
            "decision_id": self.decision_id,
            "client_order_id": self.client_order_id,
            "setup_family": self.setup_family,
            "decision_at": self.decision_at.isoformat(),
            "structural_stop_price": self.structural_stop_price,
            "entry_limit_ceiling_price": self.entry_limit_ceiling_price,
            "account_receipt_sha256": self.account_receipt_sha256,
            "bbo_receipt_sha256": self.bbo_receipt_sha256,
            "setup_evidence_sha256": self.setup_evidence_sha256,
            "policy_sha256": self.policy_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "route_token": self.route_token.to_payload(),
            "confirmed_arm_generation": (
                self.confirmed_arm_generation.to_payload()
            ),
            "opportunity_key": (
                self.opportunity_key.to_payload()
                if self.opportunity_key is not None
                else None
            ),
            "intent_sha256": self.intent_sha256,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CapturedPaperEntryIntent":
        data = _require_exact_keys(
            payload,
            frozenset(
                {
                    "schema_version",
                    "route_token_sha256",
                    "confirmed_arm_generation_sha256",
                    "symbol_claim_token",
                    "binder_id",
                    "opportunity_key_sha256",
                    "intent_generation",
                    "decision_id",
                    "client_order_id",
                    "setup_family",
                    "decision_at",
                    "structural_stop_price",
                    "entry_limit_ceiling_price",
                    "account_receipt_sha256",
                    "bbo_receipt_sha256",
                    "setup_evidence_sha256",
                    "policy_sha256",
                    "feature_flags_sha256",
                    "route_token",
                    "confirmed_arm_generation",
                    "opportunity_key",
                    "intent_sha256",
                }
            ),
            field_name="entry_intent_payload",
        )
        if data["schema_version"] != CAPTURED_PAPER_ENTRY_INTENT_SCHEMA_VERSION:
            raise CapturedPaperIntentContractError(
                "entry_intent_schema_version_invalid"
            )
        route = CapturedPaperRouteToken.from_payload(data["route_token"])
        arm = CapturedPaperConfirmedArmGeneration.from_payload(
            data["confirmed_arm_generation"]
        )
        raw_opportunity = data["opportunity_key"]
        opportunity = (
            None
            if raw_opportunity is None
            else CapturedPaperOpportunityKey.from_payload(raw_opportunity)
        )
        supplied_route_hash = _sha256(
            data["route_token_sha256"], field_name="route_token_sha256"
        )
        supplied_arm_hash = _sha256(
            data["confirmed_arm_generation_sha256"],
            field_name="confirmed_arm_generation_sha256",
        )
        raw_opportunity_hash = data["opportunity_key_sha256"]
        if raw_opportunity_hash is None:
            supplied_opportunity_hash = None
        else:
            supplied_opportunity_hash = _sha256(
                raw_opportunity_hash,
                field_name="opportunity_key_sha256",
            )
        if supplied_route_hash != route.route_token_sha256:
            raise CapturedPaperIntentContractError(
                "entry_intent_route_hash_mismatch"
            )
        if supplied_arm_hash != arm.confirmed_arm_generation_sha256:
            raise CapturedPaperIntentContractError(
                "entry_intent_arm_hash_mismatch"
            )
        if supplied_opportunity_hash != (
            opportunity.opportunity_key_sha256 if opportunity is not None else None
        ):
            raise CapturedPaperIntentContractError(
                "entry_intent_opportunity_hash_mismatch"
            )
        try:
            value = cls(
                route_token=route,
                confirmed_arm_generation=arm,
                symbol_claim_token=data["symbol_claim_token"],
                binder_id=data["binder_id"],
                opportunity_key=opportunity,
                intent_generation=data["intent_generation"],
                decision_id=data["decision_id"],
                client_order_id=data["client_order_id"],
                setup_family=data["setup_family"],
                decision_at=datetime.fromisoformat(data["decision_at"]),
                structural_stop_price=data["structural_stop_price"],
                entry_limit_ceiling_price=data["entry_limit_ceiling_price"],
                account_receipt_sha256=data["account_receipt_sha256"],
                bbo_receipt_sha256=data["bbo_receipt_sha256"],
                setup_evidence_sha256=data["setup_evidence_sha256"],
                policy_sha256=data["policy_sha256"],
                feature_flags_sha256=data["feature_flags_sha256"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CapturedPaperIntentContractError):
                raise
            raise CapturedPaperIntentContractError(
                "entry_intent_payload_invalid"
            ) from exc
        supplied_intent_hash = _sha256(
            data["intent_sha256"], field_name="intent_sha256"
        )
        if supplied_intent_hash != value.intent_sha256:
            raise CapturedPaperIntentContractError(
                "entry_intent_hash_mismatch"
            )
        return value

    def verify(self) -> None:
        if type(self.route_token) is not CapturedPaperRouteToken:
            raise CapturedPaperIntentContractError("intent_route_token_invalid")
        self.route_token.verify()
        canonical = self.from_payload(self.to_payload())
        if canonical.intent_sha256 != self.intent_sha256:
            raise CapturedPaperIntentContractError("entry_intent_hash_mismatch")


@dataclass(frozen=True, slots=True)
class CapturedPaperPostCommitRequest:
    """Typed handoff to a future completion owner after phase-one commit."""

    intent: CapturedPaperEntryIntent
    completion_generation: str
    completion_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.intent) is not CapturedPaperEntryIntent:
            raise CapturedPaperIntentContractError(
                "post_commit_intent_invalid"
            )
        self.intent.verify()
        object.__setattr__(
            self,
            "completion_generation",
            _canonical_uuid(
                self.completion_generation,
                field_name="completion_generation",
            ),
        )
        object.__setattr__(
            self,
            "completion_sha256",
            _sha256_json(self._content_payload()),
        )

    @property
    def route_token(self) -> CapturedPaperRouteToken:
        return self.intent.route_token

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURED_PAPER_POST_COMMIT_SCHEMA_VERSION,
            "route_token_sha256": self.route_token.route_token_sha256,
            "intent_sha256": self.intent.intent_sha256,
            "completion_generation": self.completion_generation,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "intent": self.intent.to_payload(),
            "completion_sha256": self.completion_sha256,
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_payload())

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CapturedPaperPostCommitRequest":
        data = _require_exact_keys(
            payload,
            frozenset(
                {
                    "schema_version",
                    "route_token_sha256",
                    "intent_sha256",
                    "completion_generation",
                    "intent",
                    "completion_sha256",
                }
            ),
            field_name="post_commit_payload",
        )
        if data["schema_version"] != CAPTURED_PAPER_POST_COMMIT_SCHEMA_VERSION:
            raise CapturedPaperIntentContractError(
                "post_commit_schema_version_invalid"
            )
        intent = CapturedPaperEntryIntent.from_payload(data["intent"])
        supplied_route_hash = _sha256(
            data["route_token_sha256"], field_name="route_token_sha256"
        )
        supplied_intent_hash = _sha256(
            data["intent_sha256"], field_name="intent_sha256"
        )
        if supplied_route_hash != intent.route_token.route_token_sha256:
            raise CapturedPaperIntentContractError(
                "post_commit_route_hash_mismatch"
            )
        if supplied_intent_hash != intent.intent_sha256:
            raise CapturedPaperIntentContractError(
                "post_commit_intent_hash_mismatch"
            )
        try:
            value = cls(
                intent=intent,
                completion_generation=data["completion_generation"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CapturedPaperIntentContractError):
                raise
            raise CapturedPaperIntentContractError(
                "post_commit_payload_invalid"
            ) from exc
        supplied_completion_hash = _sha256(
            data["completion_sha256"], field_name="completion_sha256"
        )
        if supplied_completion_hash != value.completion_sha256:
            raise CapturedPaperIntentContractError(
                "post_commit_content_hash_mismatch"
            )
        return value

    @classmethod
    def from_canonical_json(
        cls,
        canonical_json: str,
    ) -> "CapturedPaperPostCommitRequest":
        if type(canonical_json) is not str or not canonical_json:
            raise CapturedPaperIntentContractError(
                "post_commit_canonical_json_invalid"
            )

        def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise CapturedPaperIntentContractError(
                        "post_commit_canonical_json_duplicate_key"
                    )
                result[key] = value
            return result

        def reject_nonfinite(value: str) -> None:
            raise CapturedPaperIntentContractError(
                "post_commit_canonical_json_nonfinite"
            )

        try:
            payload = json.loads(
                canonical_json,
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=reject_nonfinite,
            )
        except CapturedPaperIntentContractError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapturedPaperIntentContractError(
                "post_commit_canonical_json_invalid"
            ) from exc
        if _canonical_json(payload) != canonical_json:
            raise CapturedPaperIntentContractError(
                "post_commit_canonical_json_not_canonical"
            )
        return cls.from_payload(payload)

    def verify(self) -> None:
        if type(self.intent) is not CapturedPaperEntryIntent:
            raise CapturedPaperIntentContractError(
                "post_commit_intent_invalid"
            )
        self.intent.verify()
        canonical = self.from_payload(self.to_payload())
        if canonical.completion_sha256 != self.completion_sha256:
            raise CapturedPaperIntentContractError(
                "post_commit_content_hash_mismatch"
            )


class CapturedPaperPostCommitHandler(Protocol):
    """Future phase-two owner; intentionally receives no outer DB session."""

    def __call__(self, request: CapturedPaperPostCommitRequest) -> Any: ...


def revalidate_captured_paper_route_token(
    token: CapturedPaperRouteToken,
    locked_session: Any,
    runtime: Any,
) -> CapturedPaperRouteToken:
    """Revalidate a preliminary token against the later locked durable row.

    Acquiring the row lock remains the caller's responsibility.  This pure
    function checks every route/account/provenance field and returns the same
    verified token.  It never reads ambient configuration or persistence.
    """

    if type(token) is not CapturedPaperRouteToken:
        raise CapturedPaperRouteDriftError("captured_paper_route_token_invalid")
    try:
        token.verify()
    except CapturedPaperIntentContractError as exc:
        raise CapturedPaperRouteDriftError(exc.reason) from exc

    resolver = getattr(runtime, "resolve_config_sha256", None)
    try:
        runtime_config_sha256 = (
            resolver(token.symbol)
            if callable(resolver)
            else getattr(runtime, "config_sha256", None)
        )
    except Exception as exc:
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_runtime_provenance_drift"
        ) from exc
    runtime_fields = {
        "account_scope": token.account_scope,
        "expected_account_id": token.expected_account_id,
        "code_build_sha256": token.code_build_sha256,
        "capture_receipt_sha256": token.capture_receipt_sha256,
        "runtime_generation": token.runtime_generation,
        "first_dip_policy_mode": token.first_dip_policy_mode,
    }
    if (
        runtime_config_sha256 != token.config_sha256
        or any(
            getattr(runtime, name, None) != expected
            for name, expected in runtime_fields.items()
        )
    ):
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_runtime_provenance_drift"
        )

    try:
        session_id = int(getattr(locked_session, "id"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_session_id_drift"
        ) from exc
    if session_id != token.session_id:
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_session_id_drift"
        )
    if str(getattr(locked_session, "mode", "") or "") != "live":
        raise CapturedPaperRouteDriftError("captured_paper_route_mode_drift")
    if normalize_execution_family(
        getattr(locked_session, "execution_family", None)
    ) != token.execution_family:
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_execution_family_drift"
        )
    symbol = str(getattr(locked_session, "symbol", "") or "").strip().upper()
    if symbol != token.symbol:
        raise CapturedPaperRouteDriftError("captured_paper_route_symbol_drift")
    snapshot = getattr(locked_session, "risk_snapshot_json", None)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if str(snapshot.get("alpaca_account_scope") or "").strip() != token.account_scope:
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_account_scope_drift"
        )
    if str(snapshot.get("alpaca_account_id") or "").strip() != token.expected_account_id:
        raise CapturedPaperRouteDriftError(
            "captured_paper_route_account_id_drift"
        )
    return token


__all__ = (
    "ALPACA_PAPER_ACCOUNT_SCOPE",
    "CAPTURED_PAPER_CONFIRMED_ARM_SCHEMA_VERSION",
    "CAPTURED_PAPER_ENTRY_INTENT_SCHEMA_VERSION",
    "CAPTURED_PAPER_OPPORTUNITY_KEY_SCHEMA_VERSION",
    "CAPTURED_PAPER_POST_COMMIT_SCHEMA_VERSION",
    "CAPTURED_PAPER_ROUTE_TOKEN_SCHEMA_VERSION",
    "CapturedPaperConfirmedArmGeneration",
    "CapturedPaperEntryIntent",
    "CapturedPaperIntentContractError",
    "CapturedPaperOpportunityKey",
    "CapturedPaperPostCommitHandler",
    "CapturedPaperPostCommitRequest",
    "CapturedPaperRouteDriftError",
    "CapturedPaperRouteToken",
    "revalidate_captured_paper_route_token",
)
