"""Typed, non-runnable first-session foundation for captured Alpaca PAPER.

This module deliberately does **not** discover a symbol, fetch market data,
select a strategy, run the FSM, reserve risk, consume an opportunity, or create
an order/outbox row.  A separately injected capture owner must first produce a
content-addressed :class:`CapturedPaperInitialSessionMaterial`.  This boundary
then does one narrow thing: under the canonical Alpaca action/adaptive account
locks, atomically bind that exact material to a non-runnable PREOWNER session
and its pre-HTTP action claim.

The PREOWNER state is intentionally outside ``LIVE_RUNNER_RUNNABLE_STATES``.
Promotion requires a later sealed production-material read and captured host
tick; this module cannot manufacture the durable post-tick owner marker.
Its material-SHA PREOWNER claim is deliberately **not** the normal execution
claim: it does not manufacture ``confirmed_arm_generation``, a canonical UUID
``arm_token``, or the required ``arm-{uuid}`` claim token.  Therefore a PREOWNER
session is not promotable by this module alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ....models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from .adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
    acquire_adaptive_risk_account_locks,
)
from .adaptive_risk_policy import (
    ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
    ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION,
    AdaptiveRiskContractError,
    AdaptiveRiskPolicy,
    AdaptiveRiskPolicySettingsReceipt,
)
from .alpaca_orphan_claims import acquire_action_claim, read_action_claim
from .captured_adaptive_risk_source import (
    CapturedAdaptiveRiskCoverageUnavailable,
    CapturedAdaptiveRiskPolicySpec,
)


INITIAL_SESSION_MATERIAL_SCHEMA_VERSION = (
    "chili.captured-paper-initial-session-material.v1"
)
INITIAL_READ_INVENTORY_SCHEMA_VERSION = (
    "chili.captured-paper-initial-read-inventory.v1"
)
INITIAL_PREOWNER_MARKER_SCHEMA_VERSION = (
    "chili.captured-paper-session-preowner.v1"
)
INITIAL_PREOWNER_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-session-preowner-receipt.v1"
)
INITIAL_PREOWNER_RISK_SNAPSHOT_SCHEMA_VERSION = (
    "chili.captured-paper-preowner-risk-snapshot.v1"
)
INITIAL_RUNNER_RISK_TEMPLATE_SCHEMA_VERSION = (
    "chili.captured-paper-initial-runner-risk-template.v1"
)

ALPACA_PAPER_ACCOUNT_SCOPE = "alpaca:paper"
ALPACA_SPOT_EXECUTION_FAMILY = "alpaca_spot"
CAPTURED_PAPER_PREOWNER_STATE = "captured_paper_preowner"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,35}")
_PREOWNER_KEY = "captured_paper_session_preowner"
INITIAL_PREOWNER_MATERIAL_KEY = "captured_paper_initial_material"
_RUNNER_RISK_TEMPLATE_KEY = "captured_paper_initial_runner_risk_template"
_SOURCE_NODE = "captured_paper_initial_admission"
_RUNNER_RISK_REQUIRED_KEYS = frozenset(
    {
        "momentum_risk_policy_summary",
        "momentum_risk_policy_resolved_utc",
        "momentum_risk",
        "viability_brief",
        "execution_readiness_subset",
        "momentum_policy_caps",
    }
)
_RUNNER_RISK_OPTIONAL_KEYS = frozenset(
    {"momentum_policy_caps_derivation"}
)
_RUNNER_RISK_SOURCE_KEYS = frozenset(
    {
        "adaptive_policy_settings",
        "capture_config",
        "execution_readiness",
        "momentum_policy_caps",
        "momentum_risk_evaluation",
        "viability_snapshot",
    }
)
_RUNNER_RISK_FORBIDDEN_FIELD_FRAGMENTS = (
    "live_exec",
    "live_execution",
    "order",
    "position",
    "opportunity",
    "reservation",
    "outbox",
    "transport",
)
_ACTIVATION_ONLY_FIELD_FRAGMENTS = (
    "activation_only",
    "paper_only",
    "one_symbol",
    "single_symbol",
    "max_symbols",
    "max_concurrent",
    "symbol_limit",
    "fixed_dollar",
)


class CapturedPaperInitialAdmissionError(RuntimeError):
    """One first-session event failed closed before runnable exposure."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_initial_admission_rejected")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperInitialAdmissionError(reason)


def _sha(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _reject(f"{field_name}_invalid")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int:
        _reject(f"{field_name}_invalid")
    parsed = value
    if parsed <= 0:
        _reject(f"{field_name}_invalid")
    return parsed


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _reject(f"{field_name}_invalid")
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        _reject(f"{field_name}_invalid")
    if value != canonical:
        _reject(f"{field_name}_invalid")
    return canonical


def _symbol(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _SYMBOL_RE.fullmatch(value) is None
        or value.endswith(".")
        or ".." in value
    ):
        _reject("initial_symbol_invalid")
    return value


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(f"{field_name}_invalid")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _reject("nonfinite_canonical_value")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        aware = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return _iso_utc(aware)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    _reject("noncanonical_json_value")


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            _json_value(dict(payload)),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperInitialAdmissionError(
            "canonical_json_unavailable"
        ) from exc


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _read_inventory_body(read_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "schema_version": INITIAL_READ_INVENTORY_SCHEMA_VERSION,
        "read_ids": list(read_ids),
    }


def _strict_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(
        type(key) is not str for key in value
    ):
        _reject(f"{field_name}_invalid")
    normalized = _json_value(value)
    if type(normalized) is not dict:
        _reject(f"{field_name}_invalid")
    return normalized


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in sorted(
                    value.items(), key=lambda row: str(row[0])
                )
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _canonical_utc_payload_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _reject(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _reject(f"{field_name}_invalid")
    if _iso_utc(_aware_utc(parsed, field_name)) != value:
        _reject(f"{field_name}_invalid")
    return value


def _reject_forbidden_runner_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if any(
                fragment in normalized
                for fragment in _RUNNER_RISK_FORBIDDEN_FIELD_FRAGMENTS
            ):
                _reject("initial_runner_risk_template_execution_field_forbidden")
            if any(
                fragment in normalized
                for fragment in _ACTIVATION_ONLY_FIELD_FRAGMENTS
            ):
                _reject("initial_runner_risk_template_activation_field_forbidden")
            _reject_forbidden_runner_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_runner_fields(item)


def _reconstruct_adaptive_policy_settings_receipt(
    projection_value: Any,
) -> AdaptiveRiskPolicySettingsReceipt:
    projection = _strict_json_object(
        projection_value,
        "adaptive_policy_settings_projection",
    )
    expected_top_level = {
        "schema_version",
        "policy_field_bindings",
        "settings",
        "policy_snapshot",
        "policy_sha256",
        "settings_projection_sha256",
    }
    if set(projection) != expected_top_level:
        _reject("adaptive_policy_settings_projection_keys_invalid")
    if (
        projection.get("schema_version")
        != ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION
    ):
        _reject("adaptive_policy_settings_projection_schema_invalid")
    bindings = projection.get("policy_field_bindings")
    expected_bindings = dict(ADAPTIVE_RISK_POLICY_SETTING_BINDINGS)
    if type(bindings) is not dict or bindings != expected_bindings:
        _reject("adaptive_policy_settings_bindings_mismatch")
    settings_values = projection.get("settings")
    policy_snapshot = projection.get("policy_snapshot")
    if type(settings_values) is not dict or type(policy_snapshot) is not dict:
        _reject("adaptive_policy_settings_projection_payload_invalid")
    expected_setting_names = {
        setting_name
        for _policy_name, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
    }
    expected_policy_names = {
        policy_name
        for policy_name, _setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
    }
    if set(settings_values) != expected_setting_names:
        _reject("adaptive_policy_settings_names_mismatch")
    if set(policy_snapshot) != expected_policy_names:
        _reject("adaptive_policy_snapshot_names_mismatch")
    for policy_name, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS:
        policy_value = policy_snapshot[policy_name]
        setting_value = settings_values[setting_name]
        if policy_name in {"policy_version", "policy_source"}:
            if type(policy_value) is not str or type(setting_value) is not str:
                _reject("adaptive_policy_settings_value_type_invalid")
        elif type(policy_value) not in {int, float} or type(setting_value) not in {
            int,
            float,
        }:
            _reject("adaptive_policy_settings_value_type_invalid")
    try:
        policy = AdaptiveRiskPolicy(**policy_snapshot)
        receipt = AdaptiveRiskPolicySettingsReceipt(
            policy=policy,
            setting_values=tuple(
                (setting_name, settings_values[setting_name])
                for _policy_name, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
            ),
        )
    except (AdaptiveRiskContractError, TypeError, ValueError) as exc:
        raise CapturedPaperInitialAdmissionError(
            "adaptive_policy_settings_projection_invalid"
        ) from exc
    claimed_projection_sha256 = _sha(
        projection.get("settings_projection_sha256"),
        "settings_projection_sha256",
    )
    claimed_policy_sha256 = _sha(
        projection.get("policy_sha256"),
        "policy_sha256",
    )
    if (
        claimed_projection_sha256 != receipt.settings_projection_sha256
        or claimed_policy_sha256 != receipt.policy.policy_sha256
        or projection != receipt.to_settings_projection()
    ):
        _reject("adaptive_policy_settings_projection_digest_mismatch")
    return receipt


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialRunnerRiskTemplate:
    """Frozen non-executable runner-risk payload and its source receipts."""

    payload: Mapping[str, Any]
    payload_sha256: str
    source_receipt_sha256s: Mapping[str, str]
    template_sha256: str = field(init=False)
    schema_version: str = INITIAL_RUNNER_RISK_TEMPLATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INITIAL_RUNNER_RISK_TEMPLATE_SCHEMA_VERSION:
            _reject("initial_runner_risk_template_schema_invalid")
        payload = _strict_json_object(
            self.payload,
            "initial_runner_risk_template_payload",
        )
        keys = frozenset(payload)
        if (
            not _RUNNER_RISK_REQUIRED_KEYS.issubset(keys)
            or not keys.issubset(
                _RUNNER_RISK_REQUIRED_KEYS | _RUNNER_RISK_OPTIONAL_KEYS
            )
        ):
            _reject("initial_runner_risk_template_keys_invalid")
        _canonical_utc_payload_text(
            payload["momentum_risk_policy_resolved_utc"],
            "momentum_risk_policy_resolved_utc",
        )
        for key in _RUNNER_RISK_REQUIRED_KEYS - {
            "momentum_risk_policy_resolved_utc"
        }:
            if type(payload.get(key)) is not dict or not payload[key]:
                _reject(f"initial_runner_risk_template_{key}_invalid")
        derivation = payload.get("momentum_policy_caps_derivation")
        if derivation is not None and (
            type(derivation) is not dict or not derivation
        ):
            _reject("initial_runner_risk_template_derivation_invalid")
        _reject_forbidden_runner_fields(payload)
        payload_sha256 = _sha(
            self.payload_sha256,
            "initial_runner_risk_template_payload_sha256",
        )
        if payload_sha256 != _sha256_json(payload):
            _reject("initial_runner_risk_template_payload_hash_mismatch")
        source_receipts = _strict_json_object(
            self.source_receipt_sha256s,
            "initial_runner_risk_template_source_receipts",
        )
        if set(source_receipts) != _RUNNER_RISK_SOURCE_KEYS:
            _reject("initial_runner_risk_template_source_receipts_invalid")
        source_receipts = {
            key: _sha(value, f"initial_runner_risk_template_source_{key}")
            for key, value in source_receipts.items()
        }
        object.__setattr__(self, "payload", _freeze_json(payload))
        object.__setattr__(self, "payload_sha256", payload_sha256)
        object.__setattr__(
            self,
            "source_receipt_sha256s",
            MappingProxyType(dict(sorted(source_receipts.items()))),
        )
        object.__setattr__(self, "template_sha256", _sha256_json(self.to_body()))

    def to_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "payload": _json_value(self.payload),
            "payload_sha256": self.payload_sha256,
            "source_receipt_sha256s": dict(self.source_receipt_sha256s),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_body(), "template_sha256": self.template_sha256}

    def verify(self) -> None:
        if (
            _sha256_json(_json_value(self.payload)) != self.payload_sha256
            or _sha256_json(self.to_body()) != self.template_sha256
        ):
            _reject("initial_runner_risk_template_mutated")


def _verify_initial_adaptive_policy_contract(
    *,
    adaptive_policy_settings_projection: Any,
    settings_projection_sha256: str,
    policy_sha256: str,
    code_build_sha256: str,
    config_sha256: str,
    feature_flags_sha256: str,
    adaptive_policy_provenance_sha256: str,
    runner_risk_template: Any,
    viability_snapshot_sha256: str,
    symbol: str,
) -> dict[str, Any]:
    receipt = _reconstruct_adaptive_policy_settings_receipt(
        adaptive_policy_settings_projection
    )
    if settings_projection_sha256 != receipt.settings_projection_sha256:
        _reject("initial_adaptive_settings_projection_mismatch")
    if policy_sha256 != receipt.policy.policy_sha256:
        _reject("initial_adaptive_policy_hash_mismatch")
    try:
        policy_spec = CapturedAdaptiveRiskPolicySpec(
            policy=receipt.policy,
            code_build_sha256=code_build_sha256,
            effective_config_sha256=settings_projection_sha256,
            feature_flags_sha256=feature_flags_sha256,
        )
    except (
        CapturedAdaptiveRiskCoverageUnavailable,
        AdaptiveRiskContractError,
        TypeError,
        ValueError,
    ) as exc:
        raise CapturedPaperInitialAdmissionError(
            "initial_adaptive_policy_spec_invalid"
        ) from exc
    if policy_spec.provenance_sha256 != adaptive_policy_provenance_sha256:
        _reject("initial_adaptive_policy_provenance_mismatch")
    if type(runner_risk_template) is not CapturedPaperInitialRunnerRiskTemplate:
        _reject("initial_runner_risk_template_type_invalid")
    runner_risk_template.verify()
    sources = runner_risk_template.source_receipt_sha256s
    if (
        sources.get("adaptive_policy_settings")
        != receipt.settings_projection_sha256
        or sources.get("capture_config") != config_sha256
        or sources.get("viability_snapshot") != viability_snapshot_sha256
        or sources.get("execution_readiness") != viability_snapshot_sha256
    ):
        _reject("initial_runner_risk_template_source_mismatch")
    summary = runner_risk_template.payload.get(
        "momentum_risk_policy_summary"
    )
    expected_summary = {
        "adaptive_policy_sha256": receipt.policy.policy_sha256,
        "adaptive_policy_provenance_sha256": policy_spec.provenance_sha256,
        "settings_projection_sha256": receipt.settings_projection_sha256,
        "code_build_sha256": code_build_sha256,
        "capture_config_sha256": config_sha256,
        "feature_flags_sha256": feature_flags_sha256,
        "applies_to_execution_surfaces": ["alpaca_paper", "replay"],
    }
    if not isinstance(summary, Mapping) or any(
        _json_value(summary.get(key)) != value
        for key, value in expected_summary.items()
    ):
        _reject("initial_runner_risk_template_policy_summary_mismatch")
    viability_brief = runner_risk_template.payload.get("viability_brief")
    if (
        not isinstance(viability_brief, Mapping)
        or viability_brief.get("scope") != "symbol"
        or viability_brief.get("symbol") != symbol
    ):
        _reject("initial_runner_risk_template_viability_scope_mismatch")
    return receipt.to_settings_projection()


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialSessionMaterial:
    """Immutable output of a future sealed pre-session evidence producer."""

    symbol: str
    user_id: int
    variant_id: int
    account_scope: str
    expected_account_id: str
    runtime_generation: str
    execution_family: str
    code_build_sha256: str
    config_sha256: str
    capture_receipt_sha256: str
    policy_sha256: str
    adaptive_policy_settings_projection: Mapping[str, Any]
    settings_projection_sha256: str
    feature_flags_sha256: str
    adaptive_policy_provenance_sha256: str
    runner_risk_template: CapturedPaperInitialRunnerRiskTemplate
    trigger_read_receipt_sha256: str
    captured_input_attestation_sha256: str
    captured_read_ids: tuple[str, ...]
    captured_read_inventory_sha256: str
    selection_receipt_sha256: str
    strategy_variant_sha256: str
    viability_snapshot_sha256: str
    decision_at: datetime
    expires_at: datetime
    material_sha256: str = field(init=False)
    schema_version: str = INITIAL_SESSION_MATERIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INITIAL_SESSION_MATERIAL_SCHEMA_VERSION:
            _reject("initial_material_schema_invalid")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(
            self, "user_id", _positive_int(self.user_id, "initial_user_id")
        )
        object.__setattr__(
            self,
            "variant_id",
            _positive_int(self.variant_id, "initial_variant_id"),
        )
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            _reject("initial_account_scope_invalid")
        if self.execution_family != ALPACA_SPOT_EXECUTION_FAMILY:
            _reject("initial_execution_family_invalid")
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(self.expected_account_id, "initial_account_id"),
        )
        object.__setattr__(
            self,
            "runtime_generation",
            _canonical_uuid(self.runtime_generation, "initial_runtime_generation"),
        )
        for name in (
            "code_build_sha256",
            "config_sha256",
            "capture_receipt_sha256",
            "policy_sha256",
            "settings_projection_sha256",
            "feature_flags_sha256",
            "adaptive_policy_provenance_sha256",
            "trigger_read_receipt_sha256",
            "captured_input_attestation_sha256",
            "captured_read_inventory_sha256",
            "selection_receipt_sha256",
            "strategy_variant_sha256",
            "viability_snapshot_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        read_ids = tuple(str(value or "").strip() for value in self.captured_read_ids)
        if (
            not read_ids
            or read_ids != tuple(sorted(read_ids))
            or len(read_ids) != len(set(read_ids))
            or any(not value or len(value) > 256 for value in read_ids)
        ):
            _reject("captured_read_ids_invalid")
        object.__setattr__(self, "captured_read_ids", read_ids)
        expected_inventory = _sha256_json(_read_inventory_body(read_ids))
        if self.captured_read_inventory_sha256 != expected_inventory:
            _reject("captured_read_inventory_hash_mismatch")
        decision = _aware_utc(self.decision_at, "initial_decision_at")
        expires = _aware_utc(self.expires_at, "initial_expires_at")
        if expires <= decision:
            _reject("initial_material_expiry_invalid")
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "expires_at", expires)
        verified_projection = _verify_initial_adaptive_policy_contract(
            adaptive_policy_settings_projection=(
                self.adaptive_policy_settings_projection
            ),
            settings_projection_sha256=self.settings_projection_sha256,
            policy_sha256=self.policy_sha256,
            code_build_sha256=self.code_build_sha256,
            config_sha256=self.config_sha256,
            feature_flags_sha256=self.feature_flags_sha256,
            adaptive_policy_provenance_sha256=(
                self.adaptive_policy_provenance_sha256
            ),
            runner_risk_template=self.runner_risk_template,
            viability_snapshot_sha256=self.viability_snapshot_sha256,
            symbol=self.symbol,
        )
        object.__setattr__(
            self,
            "adaptive_policy_settings_projection",
            _freeze_json(verified_projection),
        )
        object.__setattr__(self, "material_sha256", _sha256_json(self.to_body()))

    def to_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "user_id": self.user_id,
            "variant_id": self.variant_id,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "runtime_generation": self.runtime_generation,
            "execution_family": self.execution_family,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "capture_receipt_sha256": self.capture_receipt_sha256,
            "policy_sha256": self.policy_sha256,
            "adaptive_policy_settings_projection": _json_value(
                self.adaptive_policy_settings_projection
            ),
            "settings_projection_sha256": self.settings_projection_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "adaptive_policy_provenance_sha256": (
                self.adaptive_policy_provenance_sha256
            ),
            "runner_risk_template": self.runner_risk_template.to_dict(),
            "runner_risk_template_sha256": (
                self.runner_risk_template.template_sha256
            ),
            "trigger_read_receipt_sha256": self.trigger_read_receipt_sha256,
            "captured_input_attestation_sha256": (
                self.captured_input_attestation_sha256
            ),
            "captured_read_ids": list(self.captured_read_ids),
            "captured_read_inventory_sha256": (
                self.captured_read_inventory_sha256
            ),
            "selection_receipt_sha256": self.selection_receipt_sha256,
            "strategy_variant_sha256": self.strategy_variant_sha256,
            "viability_snapshot_sha256": self.viability_snapshot_sha256,
            "decision_at": _iso_utc(self.decision_at),
            "expires_at": _iso_utc(self.expires_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_body(), "material_sha256": self.material_sha256}

    def verify(self) -> None:
        _verify_initial_adaptive_policy_contract(
            adaptive_policy_settings_projection=(
                self.adaptive_policy_settings_projection
            ),
            settings_projection_sha256=self.settings_projection_sha256,
            policy_sha256=self.policy_sha256,
            code_build_sha256=self.code_build_sha256,
            config_sha256=self.config_sha256,
            feature_flags_sha256=self.feature_flags_sha256,
            adaptive_policy_provenance_sha256=(
                self.adaptive_policy_provenance_sha256
            ),
            runner_risk_template=self.runner_risk_template,
            viability_snapshot_sha256=self.viability_snapshot_sha256,
            symbol=self.symbol,
        )
        if (
            _sha256_json(self.to_body()) != self.material_sha256
            or _sha256_json(_read_inventory_body(self.captured_read_ids))
            != self.captured_read_inventory_sha256
        ):
            _reject("initial_material_mutated")


@runtime_checkable
class CapturedPaperInitialSessionMaterialProvider(Protocol):
    """Injected producer; implementations must return already-captured facts."""

    def prepare_initial_session(
        self,
        *,
        symbol: str,
        trigger_read_receipt_sha256: str,
    ) -> CapturedPaperInitialSessionMaterial: ...


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialAdmissionCapability:
    """Route-bound wrapper preventing an injected producer from changing lanes."""

    provider: CapturedPaperInitialSessionMaterialProvider
    expected_account_id: str
    runtime_generation: str
    code_build_sha256: str
    config_sha256: str
    capture_receipt_sha256: str
    policy_sha256: str
    settings_projection_sha256: str
    feature_flags_sha256: str
    adaptive_policy_provenance_sha256: str

    def __post_init__(self) -> None:
        if not callable(getattr(self.provider, "prepare_initial_session", None)):
            _reject("initial_material_provider_unavailable")
        for name in (
            "code_build_sha256",
            "config_sha256",
            "capture_receipt_sha256",
            "policy_sha256",
            "settings_projection_sha256",
            "feature_flags_sha256",
            "adaptive_policy_provenance_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(self.expected_account_id, "initial_account_id"),
        )
        object.__setattr__(
            self,
            "runtime_generation",
            _canonical_uuid(self.runtime_generation, "initial_runtime_generation"),
        )

    def prepare(
        self,
        *,
        symbol: str,
        trigger_read_receipt_sha256: str,
    ) -> CapturedPaperInitialSessionMaterial:
        normalized_symbol = _symbol(symbol)
        trigger = _sha(
            trigger_read_receipt_sha256,
            "trigger_read_receipt_sha256",
        )
        material = self.provider.prepare_initial_session(
            symbol=normalized_symbol,
            trigger_read_receipt_sha256=trigger,
        )
        if type(material) is not CapturedPaperInitialSessionMaterial:
            _reject("initial_material_provider_result_invalid")
        material.verify()
        exact = {
            "symbol": normalized_symbol,
            "trigger_read_receipt_sha256": trigger,
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "execution_family": ALPACA_SPOT_EXECUTION_FAMILY,
            "expected_account_id": self.expected_account_id,
            "runtime_generation": self.runtime_generation,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "capture_receipt_sha256": self.capture_receipt_sha256,
            "policy_sha256": self.policy_sha256,
            "settings_projection_sha256": self.settings_projection_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "adaptive_policy_provenance_sha256": (
                self.adaptive_policy_provenance_sha256
            ),
        }
        if any(getattr(material, name) != value for name, value in exact.items()):
            _reject("initial_material_provider_route_mismatch")
        return material


def captured_paper_initial_variant_sha256(row: Any) -> str:
    """Hash the exact durable strategy row selected by the sealed producer."""

    if row is None:
        _reject("initial_strategy_variant_unavailable")
    payload = {
        "schema_version": "chili.captured-paper-initial-strategy-variant.v1",
        "id": int(getattr(row, "id", 0) or 0),
        "family": str(getattr(row, "family", "") or ""),
        "variant_key": str(getattr(row, "variant_key", "") or ""),
        "version": int(getattr(row, "version", 0) or 0),
        "label": str(getattr(row, "label", "") or ""),
        "params_json": dict(getattr(row, "params_json", None) or {}),
        "is_active": bool(getattr(row, "is_active", False)),
        "execution_family": str(
            getattr(row, "execution_family", "") or ""
        ),
        "parent_variant_id": getattr(row, "parent_variant_id", None),
        "refinement_meta_json": dict(
            getattr(row, "refinement_meta_json", None) or {}
        ),
        "scan_pattern_id": getattr(row, "scan_pattern_id", None),
        "created_at": getattr(row, "created_at", None),
        "updated_at": getattr(row, "updated_at", None),
    }
    if payload["id"] <= 0 or not payload["family"] or not payload["variant_key"]:
        _reject("initial_strategy_variant_unavailable")
    return _sha256_json(payload)


def captured_paper_initial_viability_sha256(row: Any) -> str:
    """Hash the exact durable viability generation authorized for PREOWNER."""

    if row is None:
        _reject("initial_viability_unavailable")
    score = getattr(row, "viability_score", None)
    if isinstance(score, bool):
        _reject("initial_viability_unavailable")
    try:
        normalized_score = float(score)
    except (TypeError, ValueError, OverflowError):
        _reject("initial_viability_unavailable")
    if not math.isfinite(normalized_score):
        _reject("initial_viability_unavailable")
    payload = {
        "schema_version": "chili.captured-paper-initial-viability.v1",
        "id": int(getattr(row, "id", 0) or 0),
        "symbol": str(getattr(row, "symbol", "") or ""),
        "scope": str(getattr(row, "scope", "") or ""),
        "variant_id": int(getattr(row, "variant_id", 0) or 0),
        "viability_score": normalized_score,
        "paper_eligible": bool(getattr(row, "paper_eligible", False)),
        "live_eligible": bool(getattr(row, "live_eligible", False)),
        "freshness_ts": getattr(row, "freshness_ts", None),
        "regime_snapshot_json": dict(
            getattr(row, "regime_snapshot_json", None) or {}
        ),
        "execution_readiness_json": dict(
            getattr(row, "execution_readiness_json", None) or {}
        ),
        "explain_json": dict(getattr(row, "explain_json", None) or {}),
        "evidence_window_json": dict(
            getattr(row, "evidence_window_json", None) or {}
        ),
        "source_node_id": getattr(row, "source_node_id", None),
        "correlation_id": getattr(row, "correlation_id", None),
        "created_at": getattr(row, "created_at", None),
        "updated_at": getattr(row, "updated_at", None),
    }
    if (
        payload["id"] <= 0
        or payload["variant_id"] <= 0
        or not payload["symbol"]
    ):
        _reject("initial_viability_unavailable")
    return _sha256_json(payload)


def _preowner_marker(
    material: CapturedPaperInitialSessionMaterial,
    *,
    session_id: int,
    claim_token: str,
) -> dict[str, Any]:
    body = {
        "schema_version": INITIAL_PREOWNER_MARKER_SCHEMA_VERSION,
        "session_id": _positive_int(session_id, "preowner_session_id"),
        "symbol": material.symbol,
        "user_id": material.user_id,
        "variant_id": material.variant_id,
        "account_scope": material.account_scope,
        "expected_account_id": material.expected_account_id,
        "runtime_generation": material.runtime_generation,
        "execution_family": material.execution_family,
        "code_build_sha256": material.code_build_sha256,
        "config_sha256": material.config_sha256,
        "capture_receipt_sha256": material.capture_receipt_sha256,
        "policy_sha256": material.policy_sha256,
        "settings_projection_sha256": material.settings_projection_sha256,
        "feature_flags_sha256": material.feature_flags_sha256,
        "adaptive_policy_provenance_sha256": (
            material.adaptive_policy_provenance_sha256
        ),
        "runner_risk_template_sha256": (
            material.runner_risk_template.template_sha256
        ),
        "trigger_read_receipt_sha256": material.trigger_read_receipt_sha256,
        "captured_input_attestation_sha256": (
            material.captured_input_attestation_sha256
        ),
        "captured_read_inventory_sha256": (
            material.captured_read_inventory_sha256
        ),
        "selection_receipt_sha256": material.selection_receipt_sha256,
        "strategy_variant_sha256": material.strategy_variant_sha256,
        "viability_snapshot_sha256": material.viability_snapshot_sha256,
        "initial_material_sha256": material.material_sha256,
        # Keep the exact typed input bytes in the durable PREOWNER generation.
        # A crash after this commit must be recoverable without consulting a
        # provider, current configuration, or mutable strategy state.
        INITIAL_PREOWNER_MATERIAL_KEY: material.to_dict(),
        "decision_at": _iso_utc(material.decision_at),
        "expires_at": _iso_utc(material.expires_at),
        "claim_token": claim_token,
        "opportunity_consumed": False,
        "risk_reserved": False,
        "outbox_created": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }
    return {**body, "content_sha256": _sha256_json(body)}


def _risk_snapshot(
    material: CapturedPaperInitialSessionMaterial,
    marker: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": INITIAL_PREOWNER_RISK_SNAPSHOT_SCHEMA_VERSION,
        "alpaca_account_scope": material.account_scope,
        "alpaca_account_id": material.expected_account_id,
        "captured_paper_runtime_generation": material.runtime_generation,
        "captured_paper_initial_material_sha256": material.material_sha256,
        "captured_paper_settings_projection_sha256": (
            material.settings_projection_sha256
        ),
        "captured_paper_feature_flags_sha256": material.feature_flags_sha256,
        "captured_paper_adaptive_policy_provenance_sha256": (
            material.adaptive_policy_provenance_sha256
        ),
        "captured_paper_adaptive_policy_settings_projection": _json_value(
            material.adaptive_policy_settings_projection
        ),
        "captured_paper_initial_runner_risk_template_sha256": (
            material.runner_risk_template.template_sha256
        ),
        _RUNNER_RISK_TEMPLATE_KEY: material.runner_risk_template.to_dict(),
        _PREOWNER_KEY: dict(marker),
    }


@dataclass(frozen=True, slots=True)
class CommittedCapturedPaperInitialPreowner:
    """Content-addressed acknowledgement of one non-runnable atomic commit."""

    session_id: int
    initial_material_sha256: str
    preowner_marker: Mapping[str, Any]
    claim_token: str
    account_lock_identity: AdaptiveRiskAccountLockIdentity
    created: bool
    receipt_sha256: str = field(init=False)
    schema_version: str = INITIAL_PREOWNER_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _positive_int(self.session_id, "preowner_session_id")
        _sha(self.initial_material_sha256, "initial_material_sha256")
        _sha(self.claim_token, "preowner_claim_token")
        if type(self.account_lock_identity) is not AdaptiveRiskAccountLockIdentity:
            _reject("preowner_account_lock_identity_invalid")
        if type(self.created) is not bool:
            _reject("preowner_created_flag_invalid")
        marker = MappingProxyType(dict(self.preowner_marker))
        object.__setattr__(self, "preowner_marker", marker)
        object.__setattr__(self, "receipt_sha256", _sha256_json(self.to_body()))

    def to_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "initial_material_sha256": self.initial_material_sha256,
            "preowner_marker": dict(self.preowner_marker),
            "claim_token": self.claim_token,
            "account_lock_identity": {
                "schema_version": self.account_lock_identity.schema_version,
                "account_scope": self.account_lock_identity.account_scope,
                "action_advisory_key": (
                    self.account_lock_identity.action_advisory_key
                ),
                "adaptive_advisory_namespace": (
                    self.account_lock_identity.adaptive_advisory_namespace
                ),
            },
            "created": self.created,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_body(), "receipt_sha256": self.receipt_sha256}


def _verify_material_time(
    material: CapturedPaperInitialSessionMaterial,
    verification_at: datetime,
) -> datetime:
    material.verify()
    verified_at = _aware_utc(verification_at, "initial_verification_at")
    if not material.decision_at <= verified_at <= material.expires_at:
        _reject("initial_material_stale_or_future")
    return verified_at


def _assert_initial_service_fence_held(
    assertion: Callable[[], None] | None,
) -> None:
    if not callable(assertion):
        _reject("initial_service_fence_capability_unavailable")
    try:
        result = assertion()
    except Exception as exc:
        raise CapturedPaperInitialAdmissionError(
            "initial_service_fence_not_held"
        ) from exc
    if result is not None:
        _reject("initial_service_fence_assertion_invalid")


def _validate_variant_and_viability(
    db: Session,
    material: CapturedPaperInitialSessionMaterial,
) -> None:
    variant = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id == material.variant_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        variant is None
        or not bool(variant.is_active)
        or str(variant.execution_family or "") != material.execution_family
    ):
        _reject("initial_strategy_variant_unavailable")
    if captured_paper_initial_variant_sha256(variant) != material.strategy_variant_sha256:
        _reject("initial_strategy_variant_mismatch")
    viability = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == material.symbol,
            MomentumSymbolViability.variant_id == material.variant_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        viability is None
        or str(viability.scope or "") != "symbol"
        or not bool(viability.paper_eligible)
        or not bool(viability.live_eligible)
    ):
        _reject("initial_viability_unavailable")
    freshness = getattr(viability, "freshness_ts", None)
    if not isinstance(freshness, datetime):
        _reject("initial_viability_clock_unavailable")
    freshness_utc = (
        freshness.replace(tzinfo=timezone.utc)
        if freshness.tzinfo is None
        else freshness.astimezone(timezone.utc)
    )
    freshness_age_seconds = (
        material.decision_at - freshness_utc
    ).total_seconds()
    if freshness_age_seconds < 0:
        _reject("initial_viability_from_future")
    policy_receipt = _reconstruct_adaptive_policy_settings_receipt(
        material.adaptive_policy_settings_projection
    )
    if (
        freshness_age_seconds
        > policy_receipt.policy.context_data_max_age_seconds
    ):
        _reject("initial_viability_stale")
    if captured_paper_initial_viability_sha256(viability) != material.viability_snapshot_sha256:
        _reject("initial_viability_mismatch")


def _validate_existing_preowner(
    db: Session,
    *,
    material: CapturedPaperInitialSessionMaterial,
    claim: Mapping[str, Any],
    claim_token: str,
    lock_identity: AdaptiveRiskAccountLockIdentity,
) -> CommittedCapturedPaperInitialPreowner:
    owner_session_id = claim.get("owner_session_id")
    if isinstance(owner_session_id, bool) or not isinstance(owner_session_id, int):
        _reject("initial_preowner_claim_incomplete")
    session = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == owner_session_id)
        .with_for_update()
        .one_or_none()
    )
    if session is None:
        _reject("initial_preowner_session_unavailable")
    expected_marker = _preowner_marker(
        material,
        session_id=owner_session_id,
        claim_token=claim_token,
    )
    expected_snapshot = _risk_snapshot(material, expected_marker)
    snapshot = session.risk_snapshot_json
    metadata = dict(claim.get("metadata") or {})
    conflicting_session = (
        db.query(TradingAutomationSession.id)
        .filter(
            TradingAutomationSession.id != owner_session_id,
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.execution_family
            == ALPACA_SPOT_EXECUTION_FAMILY,
            TradingAutomationSession.symbol == material.symbol,
            TradingAutomationSession.ended_at.is_(None),
        )
        .with_for_update()
        .first()
    )
    if (
        session.mode != "live"
        or session.venue != "alpaca"
        or session.execution_family != material.execution_family
        or session.state != CAPTURED_PAPER_PREOWNER_STATE
        or session.symbol != material.symbol
        or int(session.variant_id) != material.variant_id
        or int(session.user_id or 0) != material.user_id
        or session.ended_at is not None
        or type(snapshot) is not dict
        or snapshot != expected_snapshot
        or session.allocation_decision_json != {}
        or session.correlation_id != material.material_sha256
        or session.source_node_id != _SOURCE_NODE
        or conflicting_session is not None
        or claim.get("phase") != "claimed"
        or claim.get("action") != "entry"
        or claim.get("claim_token") != claim_token
        or claim.get("client_order_id") is not None
        or claim.get("broker_order_id") is not None
        or metadata.get("schema_version")
        != INITIAL_PREOWNER_MARKER_SCHEMA_VERSION
        or metadata.get("stage") != CAPTURED_PAPER_PREOWNER_STATE
        or metadata.get("initial_material_sha256") != material.material_sha256
        or metadata.get("settings_projection_sha256")
        != material.settings_projection_sha256
        or metadata.get("feature_flags_sha256")
        != material.feature_flags_sha256
        or metadata.get("adaptive_policy_provenance_sha256")
        != material.adaptive_policy_provenance_sha256
        or metadata.get("runner_risk_template_sha256")
        != material.runner_risk_template.template_sha256
        or metadata.get("preowner_marker_sha256")
        != expected_marker["content_sha256"]
        or "entry_transport_started" in metadata
        or "owner_transport" in metadata
    ):
        _reject("initial_preowner_identity_mismatch")
    _validate_variant_and_viability(db, material)
    return CommittedCapturedPaperInitialPreowner(
        session_id=owner_session_id,
        initial_material_sha256=material.material_sha256,
        preowner_marker=expected_marker,
        claim_token=claim_token,
        account_lock_identity=lock_identity,
        created=False,
    )


def commit_captured_paper_initial_preowner(
    bind: Engine,
    *,
    material: CapturedPaperInitialSessionMaterial,
    verification_at: datetime,
    assert_service_fence_held: Callable[[], None] | None = None,
) -> CommittedCapturedPaperInitialPreowner:
    """Atomically persist one non-runnable PREOWNER and no execution authority.

    The function owns its transaction.  Any mismatch or exception rolls back
    the action claim, session, and audit event together.  It never commits an
    opportunity, adaptive reservation, outbox row, broker lifecycle, or order.
    """

    if not isinstance(bind, Engine):
        _reject("initial_admission_engine_invalid")
    if type(material) is not CapturedPaperInitialSessionMaterial:
        _reject("initial_material_type_invalid")
    _assert_initial_service_fence_held(assert_service_fence_held)
    observed_at = _verify_material_time(material, verification_at)
    claim_token = material.material_sha256
    result: CommittedCapturedPaperInitialPreowner | None = None

    with Session(bind=bind, expire_on_commit=False) as db:
        with db.begin():
            lock_identity = acquire_adaptive_risk_account_locks(
                db,
                account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
            )
            readable, existing_claim = read_action_claim(
                db,
                symbol=material.symbol,
                account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
                for_update=True,
            )
            if not readable:
                _reject("initial_action_claim_unreadable")
            if existing_claim is not None and existing_claim.get("phase") != "resolved":
                if existing_claim.get("claim_token") != claim_token:
                    _reject("initial_symbol_owned_by_other_generation")
                result = _validate_existing_preowner(
                    db,
                    material=material,
                    claim=existing_claim,
                    claim_token=claim_token,
                    lock_identity=lock_identity,
                )
            elif existing_claim is not None and existing_claim.get("claim_token") == claim_token:
                # A resolved claim cannot still authorize a PREOWNER retry.
                _reject("initial_preowner_claim_already_resolved")

            if result is None:
                # The process-lifetime fence may be lost after the preflight.
                # Re-prove it while the canonical account locks are held and
                # immediately before the first durable exposure-increasing seam.
                _assert_initial_service_fence_held(assert_service_fence_held)
                claim_metadata = {
                    "schema_version": INITIAL_PREOWNER_MARKER_SCHEMA_VERSION,
                    "stage": CAPTURED_PAPER_PREOWNER_STATE,
                    "initial_material_sha256": material.material_sha256,
                    "expected_account_id": material.expected_account_id,
                    "runtime_generation": material.runtime_generation,
                    "code_build_sha256": material.code_build_sha256,
                    "config_sha256": material.config_sha256,
                    "capture_receipt_sha256": material.capture_receipt_sha256,
                    "policy_sha256": material.policy_sha256,
                    "settings_projection_sha256": (
                        material.settings_projection_sha256
                    ),
                    "feature_flags_sha256": material.feature_flags_sha256,
                    "adaptive_policy_provenance_sha256": (
                        material.adaptive_policy_provenance_sha256
                    ),
                    "runner_risk_template_sha256": (
                        material.runner_risk_template.template_sha256
                    ),
                }
                claim_result = acquire_action_claim(
                    db,
                    symbol=material.symbol,
                    action="entry",
                    claim_token=claim_token,
                    owner_session_id=None,
                    client_order_id=None,
                    metadata=claim_metadata,
                    account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
                )
                if claim_result.get("ok") is not True:
                    _reject(
                        str(
                            claim_result.get("reason")
                            or "initial_action_claim_unavailable"
                        )
                    )

                active_sessions = (
                    db.query(TradingAutomationSession)
                    .filter(
                        TradingAutomationSession.mode == "live",
                        TradingAutomationSession.execution_family
                        == ALPACA_SPOT_EXECUTION_FAMILY,
                        TradingAutomationSession.symbol == material.symbol,
                        TradingAutomationSession.ended_at.is_(None),
                    )
                    .with_for_update()
                    .all()
                )
                if active_sessions:
                    _reject("initial_symbol_session_already_active")

                user_exists = db.execute(
                    text("SELECT id FROM users WHERE id = :user_id FOR KEY SHARE"),
                    {"user_id": material.user_id},
                ).scalar_one_or_none()
                if user_exists != material.user_id:
                    _reject("initial_user_unavailable")
                _validate_variant_and_viability(db, material)

                stored_at = observed_at.replace(tzinfo=None)
                session = TradingAutomationSession(
                    user_id=material.user_id,
                    venue="alpaca",
                    execution_family=material.execution_family,
                    mode="live",
                    symbol=material.symbol,
                    variant_id=material.variant_id,
                    state=CAPTURED_PAPER_PREOWNER_STATE,
                    risk_snapshot_json={},
                    allocation_decision_json={},
                    correlation_id=material.material_sha256,
                    source_node_id=_SOURCE_NODE,
                    started_at=stored_at,
                    created_at=stored_at,
                    updated_at=stored_at,
                )
                db.add(session)
                db.flush()
                marker = _preowner_marker(
                    material,
                    session_id=int(session.id),
                    claim_token=claim_token,
                )
                session.risk_snapshot_json = _risk_snapshot(material, marker)
                flag_modified(session, "risk_snapshot_json")
                db.flush()

                bound_claim = acquire_action_claim(
                    db,
                    symbol=material.symbol,
                    action="entry",
                    claim_token=claim_token,
                    owner_session_id=int(session.id),
                    client_order_id=None,
                    metadata={
                        **claim_metadata,
                        "preowner_marker_sha256": marker["content_sha256"],
                    },
                    account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
                )
                claim = bound_claim.get("claim") or {}
                if (
                    bound_claim.get("ok") is not True
                    or claim.get("phase") != "claimed"
                    or claim.get("claim_token") != claim_token
                    or claim.get("owner_session_id") != int(session.id)
                    or claim.get("client_order_id") is not None
                    or claim.get("broker_order_id") is not None
                ):
                    _reject("initial_action_claim_bind_failed")

                db.add(
                    TradingAutomationEvent(
                        session_id=int(session.id),
                        ts=stored_at,
                        event_type="captured_paper_initial_preowner_committed",
                        payload_json={
                            "schema_version": (
                                INITIAL_PREOWNER_MARKER_SCHEMA_VERSION
                            ),
                            "symbol": material.symbol,
                            "account_scope": material.account_scope,
                            "expected_account_id": material.expected_account_id,
                            "runtime_generation": material.runtime_generation,
                            "initial_material_sha256": material.material_sha256,
                            "preowner_marker_sha256": marker["content_sha256"],
                            "settings_projection_sha256": (
                                material.settings_projection_sha256
                            ),
                            "feature_flags_sha256": (
                                material.feature_flags_sha256
                            ),
                            "adaptive_policy_provenance_sha256": (
                                material.adaptive_policy_provenance_sha256
                            ),
                            "runner_risk_template_sha256": (
                                material.runner_risk_template.template_sha256
                            ),
                            "opportunity_consumed": False,
                            "risk_reserved": False,
                            "outbox_created": False,
                            "order_posted": False,
                            "broker_order_post_calls": 0,
                        },
                        correlation_id=material.material_sha256,
                        source_node_id=_SOURCE_NODE,
                    )
                )
                db.flush()
                result = CommittedCapturedPaperInitialPreowner(
                    session_id=int(session.id),
                    initial_material_sha256=material.material_sha256,
                    preowner_marker=marker,
                    claim_token=claim_token,
                    account_lock_identity=lock_identity,
                    created=True,
                )

    if result is None:
        _reject("initial_preowner_commit_unavailable")
    return result


__all__ = [
    "ALPACA_PAPER_ACCOUNT_SCOPE",
    "ALPACA_SPOT_EXECUTION_FAMILY",
    "CAPTURED_PAPER_PREOWNER_STATE",
    "INITIAL_PREOWNER_MATERIAL_KEY",
    "INITIAL_RUNNER_RISK_TEMPLATE_SCHEMA_VERSION",
    "CapturedPaperInitialAdmissionCapability",
    "CapturedPaperInitialAdmissionError",
    "CapturedPaperInitialRunnerRiskTemplate",
    "CapturedPaperInitialSessionMaterial",
    "CapturedPaperInitialSessionMaterialProvider",
    "CommittedCapturedPaperInitialPreowner",
    "captured_paper_initial_variant_sha256",
    "captured_paper_initial_viability_sha256",
    "commit_captured_paper_initial_preowner",
]
