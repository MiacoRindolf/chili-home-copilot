"""Broker-incapable viability producer for the dedicated captured PAPER lane.

The only external input is an injected, read-only captured-queue port.  This
module has no order adapter, live runner, dispatcher, provider SDK, or network
fallback.  One PostgreSQL transaction publishes every viability row and moves
the generation-bound causal frontier with an exact compare-and-swap event.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.captured_paper_selection_frontier import (
    CapturedPaperSelectionFrontier,
    CapturedPaperSelectionFrontierEvent,
    CapturedPaperSelectionRouteState,
)
from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability

from .captured_paper_initial_admission import (
    captured_paper_initial_variant_sha256,
)
from .captured_paper_variant_binding import BINDING_META_KEY


UTC = timezone.utc
ACCOUNT_SCOPE = "alpaca:paper"
EXECUTION_FAMILY = "alpaca_spot"
SOURCE_NODE_ID = "captured_paper_selection_producer"
PROVENANCE_KEY = "captured_paper_selection_producer"

AUTHORITY_SCHEMA_VERSION = "chili.captured-paper-selection-authority.v1"
OBSERVATION_SCHEMA_VERSION = "chili.captured-paper-selection-observation.v1"
BATCH_SCHEMA_VERSION = "chili.captured-paper-selection-batch.v2"
FRONTIER_SCHEMA_VERSION = "chili.captured-paper-selection-frontier.v1"
EVENT_SCHEMA_VERSION = "chili.captured-paper-selection-frontier-event.v1"
GAP_SCHEMA_VERSION = "chili.captured-paper-selection-gap.v1"
ROUTE_STATE_UPDATE_SCHEMA_VERSION = (
    "chili.captured-paper-selection-route-state-update.v1"
)
ROUTE_STATE_SCHEMA_VERSION = "chili.captured-paper-selection-route-state.v1"

ROUTE_ELIGIBLE = "eligible"
ROUTE_COVERAGE_UNAVAILABLE = "coverage_unavailable"
_ROUTE_STATES = frozenset({ROUTE_ELIGIBLE, ROUTE_COVERAGE_UNAVAILABLE})

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.:-]{0,35}$")
_GAP_CODES = frozenset(
    {
        "input_contract_invalid",
        "port_capability_unsafe",
        "provider_unavailable",
        "queue_unavailable",
    }
)
_FRONTIER_INIT_LOCK_NAMESPACE = 0x43505346  # ``CPSF``
_FRONTIER_INIT_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(:namespace, hashtext(:generation))"
)


class CapturedPaperSelectionProducerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


class CapturedPaperSelectionProviderUnavailable(RuntimeError):
    pass


class CapturedPaperSelectionQueueUnavailable(RuntimeError):
    pass


def _reject(code: str, message: str) -> None:
    raise CapturedPaperSelectionProducerError(code, message)


def _sha(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if normalized != value or _SHA_RE.fullmatch(normalized) is None:
        _reject("CONTRACT_INVALID", f"{field_name} must be a lowercase SHA-256")
    return normalized


def _uuid(value: Any, field_name: str) -> str:
    raw = str(value or "").strip()
    try:
        normalized = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionProducerError(
            "CONTRACT_INVALID", f"{field_name} must be a UUID"
        ) from exc
    if raw != normalized:
        _reject("CONTRACT_INVALID", f"{field_name} must be canonical")
    return normalized


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject("CONTRACT_INVALID", f"{field_name} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CapturedPaperSelectionProducerError(
            "CONTRACT_INVALID", f"{field_name} clock is invalid"
        ) from exc
    if offset is None:
        _reject("CONTRACT_INVALID", f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _db_utc(value: Any, field_name: str) -> datetime:
    """Normalize PostgreSQL/legacy ORM timestamps without inventing a clock."""

    if not isinstance(value, datetime):
        _reject("CONTRACT_INVALID", f"{field_name} must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return _utc(value, field_name)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(_utc(value, "canonical_datetime"))
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            _reject("CONTRACT_INVALID", "JSON object keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        _reject("CONTRACT_INVALID", "non-finite JSON number is forbidden")
    if value is None or type(value) in {str, int, float, bool}:
        return value
    _reject("CONTRACT_INVALID", f"unsupported JSON type {type(value).__name__}")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_object(value: Any, field_name: str, *, nonempty: bool = False) -> dict:
    if not isinstance(value, Mapping):
        _reject("CONTRACT_INVALID", f"{field_name} must be an object")
    normalized = _canonical_value(value)
    if type(normalized) is not dict or (nonempty and not normalized):
        _reject("CONTRACT_INVALID", f"{field_name} must be a non-empty object")
    return normalized


def _positive_int(value: Any, field_name: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        _reject("CONTRACT_INVALID", f"{field_name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionVariantBinding:
    variant_id: int
    family: str
    version: int
    variant_key: str
    target_after_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "variant_id", _positive_int(self.variant_id, "variant_id")
        )
        family = str(self.family or "")
        key = str(self.variant_key or "")
        if (
            not family
            or family != family.strip()
            or key != f"captured_paper:{family}"
            or len(key) > 64
        ):
            _reject("CONTRACT_INVALID", "variant binding route is invalid")
        object.__setattr__(
            self, "version", _positive_int(self.version, "variant_version")
        )
        object.__setattr__(
            self,
            "target_after_sha256",
            _sha(self.target_after_sha256, "target_after_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "family": self.family,
            "version": self.version,
            "variant_key": self.variant_key,
            "target_after_sha256": self.target_after_sha256,
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionAuthority:
    expected_account_id: str
    activation_generation: str
    policy_sha256: str
    settings_projection_sha256: str
    code_build_sha256: str
    variant_bindings: tuple[CapturedPaperSelectionVariantBinding, ...]
    account_scope: str = ACCOUNT_SCOPE
    execution_family: str = EXECUTION_FAMILY
    variant_set_sha256: str = field(init=False)
    authority_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.account_scope != ACCOUNT_SCOPE or self.execution_family != EXECUTION_FAMILY:
            _reject("CONTRACT_INVALID", "selection authority route is not Alpaca PAPER")
        object.__setattr__(
            self,
            "expected_account_id",
            _uuid(self.expected_account_id, "expected_account_id"),
        )
        object.__setattr__(
            self,
            "activation_generation",
            _uuid(self.activation_generation, "activation_generation"),
        )
        for name in (
            "policy_sha256",
            "settings_projection_sha256",
            "code_build_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        bindings = tuple(
            sorted(
                self.variant_bindings,
                key=lambda item: (item.family, item.version, item.variant_id),
            )
        )
        if (
            not bindings
            or any(type(item) is not CapturedPaperSelectionVariantBinding for item in bindings)
            or len({item.variant_id for item in bindings}) != len(bindings)
            or len({item.family for item in bindings}) != len(bindings)
        ):
            _reject("CONTRACT_INVALID", "variant bindings must be exact and unique")
        object.__setattr__(self, "variant_bindings", bindings)
        variant_set = _hash_json(
            {
                "schema_version": "chili.captured-paper-selection-variant-set.v1",
                "variants": [item.to_dict() for item in bindings],
            }
        )
        object.__setattr__(self, "variant_set_sha256", variant_set)
        object.__setattr__(self, "authority_sha256", _hash_json(self.body()))

    @property
    def variant_ids(self) -> tuple[int, ...]:
        return tuple(item.variant_id for item in self.variant_bindings)

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "account_scope": self.account_scope,
            "execution_family": self.execution_family,
            "expected_account_id": self.expected_account_id,
            "activation_generation": self.activation_generation,
            "policy_sha256": self.policy_sha256,
            "settings_projection_sha256": self.settings_projection_sha256,
            "code_build_sha256": self.code_build_sha256,
            "variant_set_sha256": self.variant_set_sha256,
            "variant_bindings": [item.to_dict() for item in self.variant_bindings],
            "paper_only_strategy_override": False,
            "live_cash_authorized": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "authority_sha256": self.authority_sha256}


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionObservation:
    source_sequence: int
    source_event_at: datetime
    source_available_at: datetime
    symbol: str
    variant_id: int
    viability_score: float
    paper_eligible: bool
    live_eligible: bool
    regime_snapshot_json: Mapping[str, Any]
    execution_readiness_json: Mapping[str, Any]
    explain_json: Mapping[str, Any]
    evidence_window_json: Mapping[str, Any]
    correlation_id: str
    observation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_sequence",
            _positive_int(self.source_sequence, "source_sequence"),
        )
        event_at = _utc(self.source_event_at, "source_event_at")
        available_at = _utc(self.source_available_at, "source_available_at")
        if available_at < event_at:
            _reject("CONTRACT_INVALID", "source availability precedes event time")
        object.__setattr__(self, "source_event_at", event_at)
        object.__setattr__(self, "source_available_at", available_at)
        symbol = str(self.symbol or "").strip().upper()
        if symbol != self.symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            _reject("CONTRACT_INVALID", "symbol is not canonical")
        object.__setattr__(
            self, "variant_id", _positive_int(self.variant_id, "variant_id")
        )
        if type(self.viability_score) not in {int, float}:
            _reject("CONTRACT_INVALID", "viability score is invalid")
        score = float(self.viability_score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            _reject("CONTRACT_INVALID", "viability score is outside [0,1]")
        object.__setattr__(self, "viability_score", score)
        if type(self.paper_eligible) is not bool or type(self.live_eligible) is not bool:
            _reject("CONTRACT_INVALID", "eligibility flags must be booleans")
        if self.paper_eligible != self.live_eligible:
            _reject("POLICY_PARITY_MISMATCH", "PAPER and intended policy eligibility differ")
        for name, nonempty in (
            ("regime_snapshot_json", False),
            ("execution_readiness_json", True),
            ("explain_json", False),
            ("evidence_window_json", True),
        ):
            normalized = _json_object(getattr(self, name), name, nonempty=nonempty)
            if PROVENANCE_KEY in normalized:
                _reject("CONTRACT_INVALID", f"{name} contains reserved provenance")
            object.__setattr__(self, name, normalized)
        correlation = str(self.correlation_id or "").strip()
        if correlation != self.correlation_id or not correlation or len(correlation) > 64:
            _reject("CONTRACT_INVALID", "correlation_id is invalid")
        object.__setattr__(self, "observation_sha256", _hash_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "source_sequence": self.source_sequence,
            "source_event_at": _iso(self.source_event_at),
            "source_available_at": _iso(self.source_available_at),
            "symbol": self.symbol,
            "variant_id": self.variant_id,
            "viability_score": self.viability_score,
            "paper_eligible": self.paper_eligible,
            "live_eligible": self.live_eligible,
            "regime_snapshot_json": dict(self.regime_snapshot_json),
            "execution_readiness_json": dict(self.execution_readiness_json),
            "explain_json": dict(self.explain_json),
            "evidence_window_json": dict(self.evidence_window_json),
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionRouteStateUpdate:
    """One sealed scorer outcome which supersedes an older route state."""

    source_sequence: int
    source_event_at: datetime
    source_available_at: datetime
    symbol: str
    variant_id: int
    state: str
    evidence_sha256: str
    bundle_sha256: str
    scoring_authority_sha256: str
    score_result_sha256: str
    reason_codes: tuple[str, ...] = ()
    update_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_sequence",
            _positive_int(self.source_sequence, "route_state.source_sequence"),
        )
        event_at = _utc(self.source_event_at, "route_state.source_event_at")
        available_at = _utc(
            self.source_available_at, "route_state.source_available_at"
        )
        if available_at < event_at:
            _reject(
                "CONTRACT_INVALID",
                "route-state availability precedes event time",
            )
        object.__setattr__(self, "source_event_at", event_at)
        object.__setattr__(self, "source_available_at", available_at)
        symbol = str(self.symbol or "").strip().upper()
        if symbol != self.symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            _reject("CONTRACT_INVALID", "route-state symbol is not canonical")
        object.__setattr__(
            self,
            "variant_id",
            _positive_int(self.variant_id, "route_state.variant_id"),
        )
        if self.state not in _ROUTE_STATES:
            _reject("CONTRACT_INVALID", "route-state value is invalid")
        for name in (
            "evidence_sha256",
            "bundle_sha256",
            "scoring_authority_sha256",
            "score_result_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        reasons = tuple(
            dict.fromkeys(str(value or "").strip() for value in self.reason_codes)
        )
        if any(not value or len(value) > 128 for value in reasons):
            _reject("CONTRACT_INVALID", "route-state reason code is invalid")
        if (self.state == ROUTE_ELIGIBLE) != (not reasons):
            _reject(
                "CONTRACT_INVALID",
                "route-state reason codes do not match its state",
            )
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "update_sha256", _hash_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": ROUTE_STATE_UPDATE_SCHEMA_VERSION,
            "source_sequence": self.source_sequence,
            "source_event_at": _iso(self.source_event_at),
            "source_available_at": _iso(self.source_available_at),
            "symbol": self.symbol,
            "variant_id": self.variant_id,
            "state": self.state,
            "evidence_sha256": self.evidence_sha256,
            "bundle_sha256": self.bundle_sha256,
            "scoring_authority_sha256": self.scoring_authority_sha256,
            "score_result_sha256": self.score_result_sha256,
            "reason_codes": list(self.reason_codes),
        }


def _frontier_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "account_scope": values["account_scope"],
        "expected_account_id": values["expected_account_id"],
        "activation_generation": values["activation_generation"],
        "execution_family": values["execution_family"],
        "authority_sha256": values["authority_sha256"],
        "policy_sha256": values["policy_sha256"],
        "settings_projection_sha256": values["settings_projection_sha256"],
        "code_build_sha256": values["code_build_sha256"],
        "variant_set_sha256": values["variant_set_sha256"],
        "last_source_sequence": values["last_source_sequence"],
        "last_source_event_at": _iso(values["last_source_event_at"]),
        "last_source_available_at": _iso(values["last_source_available_at"]),
        "last_batch_sha256": values["last_batch_sha256"],
        "status": values["status"],
        "gap_count": values["gap_count"],
        "version": values["version"],
        "event_sequence": values["event_sequence"],
        "last_event_sha256": values["last_event_sha256"],
    }


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionFrontierReceipt:
    frontier_id: int
    account_scope: str
    expected_account_id: str
    activation_generation: str
    execution_family: str
    authority_sha256: str
    policy_sha256: str
    settings_projection_sha256: str
    code_build_sha256: str
    variant_set_sha256: str
    last_source_sequence: int
    last_source_event_at: datetime | None
    last_source_available_at: datetime | None
    last_batch_sha256: str | None
    status: str
    gap_count: int
    version: int
    event_sequence: int
    last_event_sha256: str | None
    frontier_sha256: str

    def body(self) -> dict[str, Any]:
        return _frontier_body(self.__dict__ if hasattr(self, "__dict__") else {
            name: getattr(self, name)
            for name in (
                "account_scope", "expected_account_id", "activation_generation",
                "execution_family", "authority_sha256", "policy_sha256",
                "settings_projection_sha256", "code_build_sha256",
                "variant_set_sha256", "last_source_sequence",
                "last_source_event_at", "last_source_available_at",
                "last_batch_sha256", "status", "gap_count", "version",
                "event_sequence", "last_event_sha256",
            )
        })

    def verify(self) -> None:
        if self.account_scope != ACCOUNT_SCOPE or self.execution_family != EXECUTION_FAMILY:
            _reject("FRONTIER_INVALID", "frontier route mismatch")
        if _hash_json(self.body()) != self.frontier_sha256:
            _reject("FRONTIER_INVALID", "frontier hash mismatch")


def _receipt_from_values(frontier_id: int, values: Mapping[str, Any]) -> CapturedPaperSelectionFrontierReceipt:
    receipt = CapturedPaperSelectionFrontierReceipt(
        frontier_id=frontier_id,
        **{name: values[name] for name in (
            "account_scope", "expected_account_id", "activation_generation",
            "execution_family", "authority_sha256", "policy_sha256",
            "settings_projection_sha256", "code_build_sha256",
            "variant_set_sha256", "last_source_sequence",
            "last_source_event_at", "last_source_available_at",
            "last_batch_sha256", "status", "gap_count", "version",
            "event_sequence", "last_event_sha256", "frontier_sha256",
        )},
    )
    receipt.verify()
    return receipt


def _receipt_from_row(row: CapturedPaperSelectionFrontier) -> CapturedPaperSelectionFrontierReceipt:
    return _receipt_from_values(
        int(row.id),
        {name: getattr(row, name) for name in (
            "account_scope", "expected_account_id", "activation_generation",
            "execution_family", "authority_sha256", "policy_sha256",
            "settings_projection_sha256", "code_build_sha256",
            "variant_set_sha256", "last_source_sequence",
            "last_source_event_at", "last_source_available_at",
            "last_batch_sha256", "status", "gap_count", "version",
            "event_sequence", "last_event_sha256", "frontier_sha256",
        )},
    )


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionBatch:
    authority_sha256: str
    expected_frontier: CapturedPaperSelectionFrontierReceipt
    source_name: str
    source_generation: str
    queue_receipt_sha256: str
    coverage_receipt_sha256: str
    source_sequence_from: int
    source_sequence_through: int
    watermark_at: datetime
    read_at: datetime
    observations: tuple[CapturedPaperSelectionObservation, ...]
    route_state_updates: tuple[CapturedPaperSelectionRouteStateUpdate, ...]
    batch_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "authority_sha256", _sha(self.authority_sha256, "authority_sha256")
        )
        if type(self.expected_frontier) is not CapturedPaperSelectionFrontierReceipt:
            _reject("CONTRACT_INVALID", "expected frontier is invalid")
        self.expected_frontier.verify()
        source_name = str(self.source_name or "").strip().lower()
        if source_name != self.source_name or _TOKEN_RE.fullmatch(source_name) is None:
            _reject("CONTRACT_INVALID", "source_name is invalid")
        object.__setattr__(
            self, "source_generation", _uuid(self.source_generation, "source_generation")
        )
        for name in ("queue_receipt_sha256", "coverage_receipt_sha256"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        start = _positive_int(
            self.source_sequence_from, "source_sequence_from", allow_zero=True
        )
        through = _positive_int(self.source_sequence_through, "source_sequence_through")
        if start != self.expected_frontier.last_source_sequence or through <= start:
            _reject("CONTRACT_INVALID", "batch sequence is not contiguous with frontier")
        watermark = _utc(self.watermark_at, "watermark_at")
        read_at = _utc(self.read_at, "read_at")
        if watermark > read_at:
            _reject("CONTRACT_INVALID", "watermark is after queue read")
        object.__setattr__(self, "watermark_at", watermark)
        object.__setattr__(self, "read_at", read_at)
        observations = tuple(sorted(self.observations, key=lambda item: item.source_sequence))
        if (
            any(type(item) is not CapturedPaperSelectionObservation for item in observations)
            or len({item.source_sequence for item in observations}) != len(observations)
            or len({(item.symbol, item.variant_id) for item in observations})
            != len(observations)
            or any(
                item.source_sequence <= start
                or item.source_sequence > through
                or item.source_event_at > watermark
                or item.source_available_at > read_at
                or _hash_json(item.body()) != item.observation_sha256
                for item in observations
            )
        ):
            _reject("CONTRACT_INVALID", "batch observations violate coverage bounds")
        object.__setattr__(self, "observations", observations)
        route_updates = tuple(
            sorted(self.route_state_updates, key=lambda item: item.source_sequence)
        )
        expected_sequences = tuple(range(start + 1, through + 1))
        eligible_by_sequence = {
            item.source_sequence: item for item in observations
        }
        if (
            any(
                type(item) is not CapturedPaperSelectionRouteStateUpdate
                for item in route_updates
            )
            or tuple(item.source_sequence for item in route_updates)
            != expected_sequences
            or len({(item.symbol, item.variant_id) for item in route_updates})
            != len(route_updates)
            or any(
                item.source_event_at > watermark
                or item.source_available_at > read_at
                or _hash_json(item.body()) != item.update_sha256
                for item in route_updates
            )
        ):
            _reject("CONTRACT_INVALID", "batch route states violate coverage bounds")
        for item in route_updates:
            observation = eligible_by_sequence.get(item.source_sequence)
            if item.state == ROUTE_ELIGIBLE:
                if not (
                    observation is not None
                    and observation.symbol == item.symbol
                    and observation.variant_id == item.variant_id
                    and observation.source_event_at == item.source_event_at
                    and observation.source_available_at == item.source_available_at
                    and observation.observation_sha256 == item.evidence_sha256
                ):
                    _reject(
                        "CONTRACT_INVALID",
                        "eligible route state differs from its observation",
                    )
            elif observation is not None:
                _reject(
                    "CONTRACT_INVALID",
                    "coverage-unavailable route cannot carry an observation",
                )
        if set(eligible_by_sequence) != {
            item.source_sequence
            for item in route_updates
            if item.state == ROUTE_ELIGIBLE
        }:
            _reject(
                "CONTRACT_INVALID",
                "each observation requires one eligible route state",
            )
        object.__setattr__(self, "route_state_updates", route_updates)
        object.__setattr__(self, "batch_sha256", _hash_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": BATCH_SCHEMA_VERSION,
            "authority_sha256": self.authority_sha256,
            "expected_frontier_sha256": self.expected_frontier.frontier_sha256,
            "expected_frontier_version": self.expected_frontier.version,
            "source_name": self.source_name,
            "source_generation": self.source_generation,
            "queue_receipt_sha256": self.queue_receipt_sha256,
            "coverage_receipt_sha256": self.coverage_receipt_sha256,
            "source_sequence_from": self.source_sequence_from,
            "source_sequence_through": self.source_sequence_through,
            "watermark_at": _iso(self.watermark_at),
            "read_at": _iso(self.read_at),
            "observations": [
                {**item.body(), "observation_sha256": item.observation_sha256}
                for item in self.observations
            ],
            "route_state_updates": [
                {**item.body(), "update_sha256": item.update_sha256}
                for item in self.route_state_updates
            ],
        }


@runtime_checkable
class CapturedPaperSelectionInputPort(Protocol):
    network_fallback_allowed: bool
    broker_access_allowed: bool
    mutation_allowed: bool

    def read_batch(
        self,
        *,
        frontier: CapturedPaperSelectionFrontierReceipt,
        authority: CapturedPaperSelectionAuthority,
    ) -> CapturedPaperSelectionBatch | None: ...


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionTickResult:
    status: str
    frontier: CapturedPaperSelectionFrontierReceipt
    batch_sha256: str | None = None
    gap_sha256: str | None = None
    viability_upserts: int = 0
    route_state_upserts: int = 0
    idempotent: bool = False


def _validate_bound_variants(
    db: Session,
    authority: CapturedPaperSelectionAuthority,
    *,
    lock: bool,
) -> None:
    query = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id.in_(authority.variant_ids))
        .order_by(MomentumStrategyVariant.id.asc())
    )
    if lock:
        query = query.with_for_update()
    rows = query.all()
    if len(rows) != len(authority.variant_bindings):
        _reject("VARIANT_AUTHORITY_DRIFT", "a bound PAPER variant is unavailable")
    by_id = {int(row.id): row for row in rows}
    for binding in authority.variant_bindings:
        row = by_id.get(binding.variant_id)
        marker = (
            dict(row.refinement_meta_json or {}).get(BINDING_META_KEY)
            if row is not None
            else None
        )
        if not (
            row is not None
            and bool(row.is_active)
            and str(row.execution_family or "") == EXECUTION_FAMILY
            and str(row.family or "") == binding.family
            and str(row.variant_key or "") == binding.variant_key
            and int(row.version or 0) == binding.version
            and captured_paper_initial_variant_sha256(row)
            == binding.target_after_sha256
            and isinstance(marker, Mapping)
            and marker.get("account_scope") == ACCOUNT_SCOPE
            and marker.get("expected_account_id") == authority.expected_account_id
            and marker.get("activation_generation") == authority.activation_generation
            and marker.get("policy_sha256") == authority.policy_sha256
            and marker.get("settings_projection_sha256")
            == authority.settings_projection_sha256
            and marker.get("code_build_sha256") == authority.code_build_sha256
            and marker.get("strategy_params_overridden") is False
            and marker.get("live_cash_authorized") is False
            and marker.get("real_money_authorized") is False
        ):
            _reject(
                "VARIANT_AUTHORITY_DRIFT",
                f"PAPER variant binding changed id={binding.variant_id}",
            )


def _validate_authority_integrity(
    authority: CapturedPaperSelectionAuthority,
) -> None:
    if (
        type(authority) is not CapturedPaperSelectionAuthority
        or _hash_json(authority.body()) != authority.authority_sha256
        or _hash_json(
            {
                "schema_version": "chili.captured-paper-selection-variant-set.v1",
                "variants": [
                    item.to_dict() for item in authority.variant_bindings
                ],
            }
        )
        != authority.variant_set_sha256
    ):
        _reject("CONTRACT_INVALID", "selection authority was mutated")


def _validate_batch_integrity(batch: CapturedPaperSelectionBatch) -> None:
    if (
        type(batch) is not CapturedPaperSelectionBatch
        or _hash_json(batch.body()) != batch.batch_sha256
        or any(
            _hash_json(item.body()) != item.observation_sha256
            for item in batch.observations
        )
        or any(
            _hash_json(item.body()) != item.update_sha256
            for item in batch.route_state_updates
        )
    ):
        _reject("CONTRACT_INVALID", "selection batch was mutated")


def _authority_values(authority: CapturedPaperSelectionAuthority) -> dict[str, Any]:
    return {
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "execution_family": authority.execution_family,
        "authority_sha256": authority.authority_sha256,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "variant_set_sha256": authority.variant_set_sha256,
    }


def ensure_captured_paper_selection_frontier(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    initialized_at: datetime,
) -> CapturedPaperSelectionFrontierReceipt:
    if not isinstance(db, Session) or type(authority) is not CapturedPaperSelectionAuthority:
        _reject("CONTRACT_INVALID", "exact Session and authority are required")
    _validate_authority_integrity(authority)
    at = _utc(initialized_at, "initialized_at")
    db.execute(
        _FRONTIER_INIT_LOCK_SQL,
        {
            "namespace": _FRONTIER_INIT_LOCK_NAMESPACE,
            "generation": authority.activation_generation,
        },
    )
    # Every path which needs both resources takes the generation frontier
    # before strategy variants.  The previous ensure path used the reverse
    # order and could deadlock an in-flight apply during restart overlap.
    row = (
        db.query(CapturedPaperSelectionFrontier)
        .filter(
            CapturedPaperSelectionFrontier.account_scope == ACCOUNT_SCOPE,
            CapturedPaperSelectionFrontier.expected_account_id
            == authority.expected_account_id,
            CapturedPaperSelectionFrontier.activation_generation
            == authority.activation_generation,
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        values = {
            **_authority_values(authority),
            "last_source_sequence": 0,
            "last_source_event_at": None,
            "last_source_available_at": None,
            "last_batch_sha256": None,
            "status": "ready",
            "gap_count": 0,
            "version": 1,
            "event_sequence": 0,
            "last_event_sha256": None,
        }
        values["frontier_sha256"] = _hash_json(_frontier_body(values))
        row = CapturedPaperSelectionFrontier(
            **values,
            created_at=at,
            updated_at=at,
        )
        db.add(row)
        db.flush()
    _validate_bound_variants(db, authority, lock=True)
    receipt = _receipt_from_row(row)
    expected = _authority_values(authority)
    if any(getattr(receipt, key) != value for key, value in expected.items()):
        _reject("FRONTIER_AUTHORITY_DRIFT", "frontier authority does not match")
    return receipt


def _locked_frontier(
    db: Session,
    authority: CapturedPaperSelectionAuthority,
) -> CapturedPaperSelectionFrontier:
    row = (
        db.query(CapturedPaperSelectionFrontier)
        .filter(
            CapturedPaperSelectionFrontier.account_scope == ACCOUNT_SCOPE,
            CapturedPaperSelectionFrontier.expected_account_id
            == authority.expected_account_id,
            CapturedPaperSelectionFrontier.activation_generation
            == authority.activation_generation,
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        _reject("FRONTIER_UNAVAILABLE", "selection frontier is not initialized")
    receipt = _receipt_from_row(row)
    expected = _authority_values(authority)
    if any(getattr(receipt, key) != value for key, value in expected.items()):
        _reject("FRONTIER_AUTHORITY_DRIFT", "selection frontier authority changed")
    return row


def _viability_provenance(
    authority: CapturedPaperSelectionAuthority,
    batch: CapturedPaperSelectionBatch,
    observation: CapturedPaperSelectionObservation,
) -> dict[str, Any]:
    return {
        "schema_version": "chili.captured-paper-selection-viability-provenance.v1",
        "account_scope": ACCOUNT_SCOPE,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "authority_sha256": authority.authority_sha256,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "variant_set_sha256": authority.variant_set_sha256,
        "variant_id": observation.variant_id,
        "batch_sha256": batch.batch_sha256,
        "observation_sha256": observation.observation_sha256,
        "source_name": batch.source_name,
        "source_generation": batch.source_generation,
        "source_sequence": observation.source_sequence,
        "queue_receipt_sha256": batch.queue_receipt_sha256,
        "coverage_receipt_sha256": batch.coverage_receipt_sha256,
        "paper_only_strategy_override": False,
        "live_cash_authorized": False,
    }


def _upsert_viability(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    batch: CapturedPaperSelectionBatch,
    observation: CapturedPaperSelectionObservation,
    recorded_at: datetime,
) -> None:
    provenance = _viability_provenance(authority, batch, observation)
    readiness = dict(observation.execution_readiness_json)
    explain = dict(observation.explain_json)
    evidence = dict(observation.evidence_window_json)
    readiness[PROVENANCE_KEY] = provenance
    explain[PROVENANCE_KEY] = provenance
    evidence[PROVENANCE_KEY] = provenance
    values = {
        "symbol": observation.symbol,
        "scope": "symbol",
        "variant_id": observation.variant_id,
        "viability_score": observation.viability_score,
        "paper_eligible": observation.paper_eligible,
        "live_eligible": observation.live_eligible,
        "freshness_ts": _naive(observation.source_available_at),
        "regime_snapshot_json": dict(observation.regime_snapshot_json),
        "execution_readiness_json": readiness,
        "explain_json": explain,
        "evidence_window_json": evidence,
        "source_node_id": SOURCE_NODE_ID,
        "correlation_id": observation.correlation_id,
        "updated_at": _naive(recorded_at),
    }
    insert = pg_insert(MomentumSymbolViability.__table__).values(
        **values,
        created_at=_naive(recorded_at),
    )
    db.execute(
        insert.on_conflict_do_update(
            index_elements=["symbol", "variant_id"],
            set_=values,
        )
    )


def _route_state_body(
    *,
    authority: CapturedPaperSelectionAuthority,
    update_row: CapturedPaperSelectionRouteStateUpdate,
    batch_sha256: str,
    version: int,
) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_STATE_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "execution_family": authority.execution_family,
        "authority_sha256": authority.authority_sha256,
        "symbol": update_row.symbol,
        "variant_id": update_row.variant_id,
        "latest_source_sequence": update_row.source_sequence,
        "state": update_row.state,
        "evidence_sha256": update_row.evidence_sha256,
        "batch_sha256": batch_sha256,
        "source_event_at": _iso(update_row.source_event_at),
        "source_available_at": _iso(update_row.source_available_at),
        "version": version,
    }


def _route_state_row_body(row: CapturedPaperSelectionRouteState) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_STATE_SCHEMA_VERSION,
        "account_scope": row.account_scope,
        "expected_account_id": row.expected_account_id,
        "activation_generation": row.activation_generation,
        "execution_family": row.execution_family,
        "authority_sha256": row.authority_sha256,
        "symbol": row.symbol,
        "variant_id": row.variant_id,
        "latest_source_sequence": row.latest_source_sequence,
        "state": row.state,
        "evidence_sha256": row.evidence_sha256,
        "batch_sha256": row.batch_sha256,
        "source_event_at": _iso(_utc(row.source_event_at, "route state event_at")),
        "source_available_at": _iso(
            _utc(row.source_available_at, "route state available_at")
        ),
        "version": row.version,
    }


def _apply_route_state_update(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    batch: CapturedPaperSelectionBatch,
    update_row: CapturedPaperSelectionRouteStateUpdate,
    recorded_at: datetime,
) -> None:
    row = (
        db.query(CapturedPaperSelectionRouteState)
        .filter(
            CapturedPaperSelectionRouteState.account_scope
            == authority.account_scope,
            CapturedPaperSelectionRouteState.expected_account_id
            == authority.expected_account_id,
            CapturedPaperSelectionRouteState.activation_generation
            == authority.activation_generation,
            CapturedPaperSelectionRouteState.symbol == update_row.symbol,
            CapturedPaperSelectionRouteState.variant_id == update_row.variant_id,
        )
        .with_for_update()
        .one_or_none()
    )
    version = 1
    created_at = recorded_at
    if row is not None:
        if not (
            row.execution_family == authority.execution_family
            and row.authority_sha256 == authority.authority_sha256
            and type(row.version) is int
            and row.version > 0
            and type(row.latest_source_sequence) is int
            and row.latest_source_sequence < update_row.source_sequence
            and _utc(row.source_event_at, "route state event_at")
            <= update_row.source_event_at
            and _utc(row.source_available_at, "route state available_at")
            <= update_row.source_available_at
            and _hash_json(_route_state_row_body(row)) == row.state_sha256
        ):
            _reject(
                "ROUTE_STATE_CAS_CONFLICT",
                "captured selection route state changed or regressed",
            )
        version = row.version + 1
        created_at = _utc(row.created_at, "route state created_at")
    body = _route_state_body(
        authority=authority,
        update_row=update_row,
        batch_sha256=batch.batch_sha256,
        version=version,
    )
    values = {
        key: body[key]
        for key in (
            "account_scope",
            "expected_account_id",
            "activation_generation",
            "execution_family",
            "authority_sha256",
            "symbol",
            "variant_id",
            "latest_source_sequence",
            "state",
            "evidence_sha256",
            "batch_sha256",
            "version",
        )
    }
    values.update(
        source_event_at=update_row.source_event_at,
        source_available_at=update_row.source_available_at,
        state_sha256=_hash_json(body),
        created_at=created_at,
        updated_at=recorded_at,
    )
    if row is None:
        statement = pg_insert(CapturedPaperSelectionRouteState.__table__).values(
            **values
        )
        result = db.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    "account_scope",
                    "expected_account_id",
                    "activation_generation",
                    "symbol",
                    "variant_id",
                ]
            )
        )
    else:
        result = db.execute(
            update(CapturedPaperSelectionRouteState)
            .where(
                CapturedPaperSelectionRouteState.id == row.id,
                CapturedPaperSelectionRouteState.version == row.version,
                CapturedPaperSelectionRouteState.state_sha256 == row.state_sha256,
                CapturedPaperSelectionRouteState.latest_source_sequence
                == row.latest_source_sequence,
            )
            .values(
                **{
                    key: values[key]
                    for key in (
                        "latest_source_sequence",
                        "state",
                        "evidence_sha256",
                        "batch_sha256",
                        "source_event_at",
                        "source_available_at",
                        "version",
                        "state_sha256",
                        "updated_at",
                    )
                }
            )
        )
    if result.rowcount != 1:
        _reject(
            "ROUTE_STATE_CAS_CONFLICT",
            "captured selection route state changed concurrently",
        )


def _verify_idempotent_route_states(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    batch: CapturedPaperSelectionBatch,
) -> None:
    for update_row in batch.route_state_updates:
        row = (
            db.query(CapturedPaperSelectionRouteState)
            .filter(
                CapturedPaperSelectionRouteState.account_scope
                == authority.account_scope,
                CapturedPaperSelectionRouteState.expected_account_id
                == authority.expected_account_id,
                CapturedPaperSelectionRouteState.activation_generation
                == authority.activation_generation,
                CapturedPaperSelectionRouteState.symbol == update_row.symbol,
                CapturedPaperSelectionRouteState.variant_id
                == update_row.variant_id,
            )
            .one_or_none()
        )
        if not (
            row is not None
            and row.execution_family == authority.execution_family
            and row.authority_sha256 == authority.authority_sha256
            and row.latest_source_sequence == update_row.source_sequence
            and row.state == update_row.state
            and row.evidence_sha256 == update_row.evidence_sha256
            and row.batch_sha256 == batch.batch_sha256
            and _hash_json(_route_state_row_body(row)) == row.state_sha256
        ):
            _reject("IDEMPOTENCY_DRIFT", "committed route state changed")


def _verify_idempotent_rows(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    batch: CapturedPaperSelectionBatch,
    recorded_at: datetime,
) -> None:
    exact_recorded_at = _utc(recorded_at, "idempotent batch recorded_at")
    for observation in batch.observations:
        row = (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.symbol == observation.symbol,
                MomentumSymbolViability.variant_id == observation.variant_id,
            )
            .one_or_none()
        )
        provenance = _viability_provenance(authority, batch, observation)
        readiness = dict(observation.execution_readiness_json)
        explain = dict(observation.explain_json)
        evidence = dict(observation.evidence_window_json)
        readiness[PROVENANCE_KEY] = provenance
        explain[PROVENANCE_KEY] = provenance
        evidence[PROVENANCE_KEY] = provenance
        if not (
            row is not None
            and type(row.id) is int
            and row.symbol == observation.symbol
            and row.scope == "symbol"
            and row.variant_id == observation.variant_id
            and float(row.viability_score) == observation.viability_score
            and bool(row.paper_eligible) == observation.paper_eligible
            and bool(row.live_eligible) == observation.live_eligible
            and _db_utc(row.freshness_ts, "idempotent viability freshness")
            == observation.source_available_at
            and dict(row.regime_snapshot_json or {})
            == dict(observation.regime_snapshot_json)
            and dict(row.execution_readiness_json or {}) == readiness
            and dict(row.explain_json or {}) == explain
            and dict(row.evidence_window_json or {}) == evidence
            and row.source_node_id == SOURCE_NODE_ID
            and row.correlation_id == observation.correlation_id
            and _db_utc(row.updated_at, "idempotent viability updated_at")
            == exact_recorded_at
            and _db_utc(row.created_at, "idempotent viability created_at")
            <= exact_recorded_at
        ):
            _reject("IDEMPOTENCY_DRIFT", "committed viability row changed")


def _idempotent_batch_recorded_at(
    db: Session,
    *,
    current: CapturedPaperSelectionFrontierReceipt,
    batch: CapturedPaperSelectionBatch,
) -> datetime:
    event = (
        db.query(CapturedPaperSelectionFrontierEvent)
        .filter(
            CapturedPaperSelectionFrontierEvent.frontier_id
            == current.frontier_id,
            CapturedPaperSelectionFrontierEvent.event_sha256
            == current.last_event_sha256,
            CapturedPaperSelectionFrontierEvent.batch_sha256
            == batch.batch_sha256,
        )
        .one_or_none()
    )
    if event is None:
        _reject("IDEMPOTENCY_DRIFT", "committed batch event is unavailable")
    raw = str(event.detail_canonical_json or "")
    try:
        detail = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapturedPaperSelectionProducerError(
            "IDEMPOTENCY_DRIFT", "committed batch event JSON is invalid"
        ) from exc
    recorded_at = _db_utc(event.recorded_at, "idempotent event recorded_at")
    if not (
        event.event_type == "batch_applied"
        and event.next_frontier_sha256 == current.frontier_sha256
        and event.source_sequence_from == batch.source_sequence_from
        and event.source_sequence_through == batch.source_sequence_through
        and raw == _canonical_json(detail)
        and hashlib.sha256(raw.encode("utf-8")).hexdigest()
        == event.event_sha256
        and isinstance(detail, Mapping)
        and detail.get("recorded_at") == _iso(recorded_at)
    ):
        _reject("IDEMPOTENCY_DRIFT", "committed batch event changed")
    return recorded_at


def _transition_frontier(
    db: Session,
    *,
    row: CapturedPaperSelectionFrontier,
    current: CapturedPaperSelectionFrontierReceipt,
    next_state: dict[str, Any],
    event_type: str,
    batch_sha256: str | None,
    gap_sha256: str | None,
    source_sequence_from: int,
    source_sequence_through: int,
    detail: Mapping[str, Any],
    recorded_at: datetime,
) -> CapturedPaperSelectionFrontierReceipt:
    event_body = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "frontier_id": current.frontier_id,
        "event_sequence": current.event_sequence + 1,
        "event_type": event_type,
        "expected_version": current.version,
        "next_version": current.version + 1,
        "expected_frontier_sha256": current.frontier_sha256,
        "previous_event_sha256": current.last_event_sha256,
        "batch_sha256": batch_sha256,
        "gap_sha256": gap_sha256,
        "source_sequence_from": source_sequence_from,
        "source_sequence_through": source_sequence_through,
        "detail": dict(detail),
        "recorded_at": _iso(recorded_at),
        "next_state": dict(next_state),
    }
    event_sha = _hash_json(event_body)
    values = {
        **_authority_values_from_receipt(current),
        **next_state,
        "version": current.version + 1,
        "event_sequence": current.event_sequence + 1,
        "last_event_sha256": event_sha,
    }
    values["frontier_sha256"] = _hash_json(_frontier_body(values))
    db.add(
        CapturedPaperSelectionFrontierEvent(
            frontier_id=current.frontier_id,
            event_sequence=values["event_sequence"],
            event_type=event_type,
            expected_version=current.version,
            next_version=values["version"],
            expected_frontier_sha256=current.frontier_sha256,
            next_frontier_sha256=values["frontier_sha256"],
            previous_event_sha256=current.last_event_sha256,
            event_sha256=event_sha,
            batch_sha256=batch_sha256,
            gap_sha256=gap_sha256,
            source_sequence_from=source_sequence_from,
            source_sequence_through=source_sequence_through,
            detail_canonical_json=_canonical_json(event_body),
            recorded_at=recorded_at,
        )
    )
    db.flush()
    result = db.execute(
        update(CapturedPaperSelectionFrontier)
        .where(
            CapturedPaperSelectionFrontier.id == current.frontier_id,
            CapturedPaperSelectionFrontier.version == current.version,
            CapturedPaperSelectionFrontier.frontier_sha256
            == current.frontier_sha256,
            CapturedPaperSelectionFrontier.authority_sha256
            == current.authority_sha256,
        )
        .values(
            **{key: values[key] for key in (
                "last_source_sequence", "last_source_event_at",
                "last_source_available_at", "last_batch_sha256", "status",
                "gap_count", "version", "event_sequence", "frontier_sha256",
                "last_event_sha256",
            )},
            updated_at=recorded_at,
        )
    )
    if result.rowcount != 1:
        _reject("FRONTIER_CAS_CONFLICT", "selection frontier changed concurrently")
    db.expire(row)
    return _receipt_from_values(current.frontier_id, values)


def _authority_values_from_receipt(
    receipt: CapturedPaperSelectionFrontierReceipt,
) -> dict[str, Any]:
    return {key: getattr(receipt, key) for key in (
        "account_scope", "expected_account_id", "activation_generation",
        "execution_family", "authority_sha256", "policy_sha256",
        "settings_projection_sha256", "code_build_sha256",
        "variant_set_sha256",
    )}


def apply_captured_paper_selection_batch(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    batch: CapturedPaperSelectionBatch,
    recorded_at: datetime,
) -> CapturedPaperSelectionTickResult:
    if (
        not isinstance(db, Session)
        or type(authority) is not CapturedPaperSelectionAuthority
        or type(batch) is not CapturedPaperSelectionBatch
    ):
        _reject("CONTRACT_INVALID", "exact apply types are required")
    at = _utc(recorded_at, "recorded_at")
    _validate_authority_integrity(authority)
    _validate_batch_integrity(batch)
    if at < batch.read_at:
        _reject("CONTRACT_INVALID", "recorded_at precedes the captured queue read")
    if batch.authority_sha256 != authority.authority_sha256:
        _reject("BATCH_AUTHORITY_MISMATCH", "batch authority does not match")
    allowed_ids = set(authority.variant_ids)
    if any(item.variant_id not in allowed_ids for item in batch.observations) or any(
        item.variant_id not in allowed_ids for item in batch.route_state_updates
    ):
        _reject("BATCH_AUTHORITY_MISMATCH", "observation variant is not bound")

    with db.begin_nested():
        row = _locked_frontier(db, authority)
        _validate_bound_variants(db, authority, lock=True)
        current = _receipt_from_row(row)
        if (
            current.last_batch_sha256 == batch.batch_sha256
            and current.last_source_sequence == batch.source_sequence_through
        ):
            committed_at = _idempotent_batch_recorded_at(
                db,
                current=current,
                batch=batch,
            )
            _verify_idempotent_rows(
                db,
                authority=authority,
                batch=batch,
                recorded_at=committed_at,
            )
            _verify_idempotent_route_states(
                db,
                authority=authority,
                batch=batch,
            )
            return CapturedPaperSelectionTickResult(
                status="applied",
                frontier=current,
                batch_sha256=batch.batch_sha256,
                viability_upserts=0,
                route_state_upserts=0,
                idempotent=True,
            )
        expected = batch.expected_frontier
        if (
            expected.frontier_id != current.frontier_id
            or expected.frontier_sha256 != current.frontier_sha256
            or expected.version != current.version
            or batch.source_sequence_from != current.last_source_sequence
        ):
            _reject("FRONTIER_CAS_CONFLICT", "batch was read from a stale frontier")
        for observation in batch.observations:
            _upsert_viability(
                db,
                authority=authority,
                batch=batch,
                observation=observation,
                recorded_at=at,
            )
        for route_update in batch.route_state_updates:
            _apply_route_state_update(
                db,
                authority=authority,
                batch=batch,
                update_row=route_update,
                recorded_at=at,
            )
        latest_event = current.last_source_event_at
        latest_available = current.last_source_available_at
        if batch.route_state_updates:
            latest_event = max(
                item.source_event_at for item in batch.route_state_updates
            )
            latest_available = max(
                item.source_available_at for item in batch.route_state_updates
            )
        next_state = {
            "last_source_sequence": batch.source_sequence_through,
            "last_source_event_at": latest_event,
            "last_source_available_at": latest_available,
            "last_batch_sha256": batch.batch_sha256,
            "status": "ready",
            "gap_count": current.gap_count,
        }
        frontier = _transition_frontier(
            db,
            row=row,
            current=current,
            next_state=next_state,
            event_type="batch_applied",
            batch_sha256=batch.batch_sha256,
            gap_sha256=None,
            source_sequence_from=batch.source_sequence_from,
            source_sequence_through=batch.source_sequence_through,
            detail={
                "authority_sha256": authority.authority_sha256,
                "source_name": batch.source_name,
                "source_generation": batch.source_generation,
                "queue_receipt_sha256": batch.queue_receipt_sha256,
                "coverage_receipt_sha256": batch.coverage_receipt_sha256,
                "watermark_at": _iso(batch.watermark_at),
                "read_at": _iso(batch.read_at),
                "observation_sha256s": [
                    item.observation_sha256 for item in batch.observations
                ],
                "route_state_updates": [
                    {**item.body(), "update_sha256": item.update_sha256}
                    for item in batch.route_state_updates
                ],
            },
            recorded_at=at,
        )
        return CapturedPaperSelectionTickResult(
            status="applied",
            frontier=frontier,
            batch_sha256=batch.batch_sha256,
            viability_upserts=len(batch.observations),
            route_state_upserts=len(batch.route_state_updates),
        )


def record_captured_paper_selection_gap(
    db: Session,
    *,
    authority: CapturedPaperSelectionAuthority,
    expected_frontier: CapturedPaperSelectionFrontierReceipt,
    reason_code: str,
    observed_at: datetime,
) -> CapturedPaperSelectionTickResult:
    if reason_code not in _GAP_CODES:
        _reject("CONTRACT_INVALID", "gap reason is not allowlisted")
    _validate_authority_integrity(authority)
    at = _utc(observed_at, "observed_at")
    with db.begin_nested():
        row = _locked_frontier(db, authority)
        _validate_bound_variants(db, authority, lock=True)
        current = _receipt_from_row(row)
        if (
            expected_frontier.frontier_id != current.frontier_id
            or expected_frontier.frontier_sha256 != current.frontier_sha256
            or expected_frontier.version != current.version
        ):
            _reject("FRONTIER_CAS_CONFLICT", "gap belongs to a stale frontier")
        gap_body = {
            "schema_version": GAP_SCHEMA_VERSION,
            "authority_sha256": authority.authority_sha256,
            "expected_frontier_sha256": current.frontier_sha256,
            "expected_frontier_version": current.version,
            "reason_code": reason_code,
            "observed_at": _iso(at),
            "source_sequence": current.last_source_sequence,
            "coverage_available": False,
            "opportunity_consumed": False,
            "risk_reserved": False,
            "order_posted": False,
        }
        gap_sha = _hash_json(gap_body)
        next_state = {
            "last_source_sequence": current.last_source_sequence,
            "last_source_event_at": current.last_source_event_at,
            "last_source_available_at": current.last_source_available_at,
            "last_batch_sha256": current.last_batch_sha256,
            "status": "gap",
            "gap_count": current.gap_count + 1,
        }
        frontier = _transition_frontier(
            db,
            row=row,
            current=current,
            next_state=next_state,
            event_type="coverage_gap",
            batch_sha256=None,
            gap_sha256=gap_sha,
            source_sequence_from=current.last_source_sequence,
            source_sequence_through=current.last_source_sequence,
            detail=gap_body,
            recorded_at=at,
        )
        return CapturedPaperSelectionTickResult(
            status="gap",
            frontier=frontier,
            gap_sha256=gap_sha,
        )


class CapturedPaperSelectionProducer:
    """Two-transaction queue reader; queue I/O never holds a database lock."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        authority: CapturedPaperSelectionAuthority,
        input_port: CapturedPaperSelectionInputPort,
        wall_clock: Callable[[], datetime],
    ) -> None:
        if not callable(session_factory) or not callable(wall_clock):
            _reject("CONTRACT_INVALID", "producer factories are invalid")
        if type(authority) is not CapturedPaperSelectionAuthority:
            _reject("CONTRACT_INVALID", "producer authority is invalid")
        self.session_factory = session_factory
        self.authority = authority
        self.input_port = input_port
        self.wall_clock = wall_clock

    def _frontier(self) -> CapturedPaperSelectionFrontierReceipt:
        db = self.session_factory()
        try:
            with db.begin():
                return ensure_captured_paper_selection_frontier(
                    db,
                    authority=self.authority,
                    initialized_at=self.wall_clock(),
                )
        finally:
            db.close()

    def _gap(
        self,
        frontier: CapturedPaperSelectionFrontierReceipt,
        reason_code: str,
    ) -> CapturedPaperSelectionTickResult:
        db = self.session_factory()
        try:
            with db.begin():
                return record_captured_paper_selection_gap(
                    db,
                    authority=self.authority,
                    expected_frontier=frontier,
                    reason_code=reason_code,
                    observed_at=self.wall_clock(),
                )
        finally:
            db.close()

    def tick(self) -> CapturedPaperSelectionTickResult:
        frontier = self._frontier()
        port = self.input_port
        try:
            unsafe_port = not isinstance(
                port, CapturedPaperSelectionInputPort
            ) or any(
                getattr(port, name, None) is not False
                for name in (
                    "network_fallback_allowed",
                    "broker_access_allowed",
                    "mutation_allowed",
                )
            )
        except Exception:
            unsafe_port = True
        if unsafe_port:
            return self._gap(frontier, "port_capability_unsafe")
        try:
            batch = port.read_batch(frontier=frontier, authority=self.authority)
        except CapturedPaperSelectionProviderUnavailable:
            return self._gap(frontier, "provider_unavailable")
        except CapturedPaperSelectionQueueUnavailable:
            return self._gap(frontier, "queue_unavailable")
        except Exception:
            return self._gap(frontier, "input_contract_invalid")
        if batch is None:
            return CapturedPaperSelectionTickResult(status="idle", frontier=frontier)
        if (
            type(batch) is not CapturedPaperSelectionBatch
            or batch.authority_sha256 != self.authority.authority_sha256
            or batch.expected_frontier.frontier_sha256 != frontier.frontier_sha256
        ):
            return self._gap(frontier, "input_contract_invalid")
        try:
            _validate_batch_integrity(batch)
            if any(
                item.variant_id not in set(self.authority.variant_ids)
                for item in (
                    *batch.observations,
                    *batch.route_state_updates,
                )
            ):
                raise CapturedPaperSelectionProducerError(
                    "BATCH_AUTHORITY_MISMATCH",
                    "observation variant is not bound",
                )
        except CapturedPaperSelectionProducerError:
            return self._gap(frontier, "input_contract_invalid")
        db = self.session_factory()
        try:
            with db.begin():
                return apply_captured_paper_selection_batch(
                    db,
                    authority=self.authority,
                    batch=batch,
                    recorded_at=self.wall_clock(),
                )
        finally:
            db.close()

    def record_gap(self, reason_code: str) -> CapturedPaperSelectionTickResult:
        """Persist an explicit local-queue loss which happened before a read."""

        if reason_code not in _GAP_CODES:
            _reject("CONTRACT_INVALID", "selection gap reason is invalid")
        return self._gap(self._frontier(), reason_code)


__all__ = [
    "ACCOUNT_SCOPE",
    "CapturedPaperSelectionAuthority",
    "CapturedPaperSelectionBatch",
    "CapturedPaperSelectionFrontierReceipt",
    "CapturedPaperSelectionInputPort",
    "CapturedPaperSelectionObservation",
    "CapturedPaperSelectionRouteStateUpdate",
    "CapturedPaperSelectionProducer",
    "CapturedPaperSelectionProducerError",
    "CapturedPaperSelectionProviderUnavailable",
    "CapturedPaperSelectionQueueUnavailable",
    "CapturedPaperSelectionTickResult",
    "CapturedPaperSelectionVariantBinding",
    "PROVENANCE_KEY",
    "ROUTE_COVERAGE_UNAVAILABLE",
    "ROUTE_ELIGIBLE",
    "ROUTE_STATE_SCHEMA_VERSION",
    "apply_captured_paper_selection_batch",
    "ensure_captured_paper_selection_frontier",
    "record_captured_paper_selection_gap",
]
