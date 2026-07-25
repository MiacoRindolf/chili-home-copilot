"""Hermetic captured-input adapter for the exact momentum viability scorer.

PAPER and ReplayV3 may pass either the same typed bundle or its canonical
serialized form to :func:`score_captured_viability`.  This module deliberately
has no database, provider, broker, cache, or process-settings fallback.  A
bundle which cannot prove an exact causal input frontier is classified
``COVERAGE_UNAVAILABLE`` and never produces a selection observation.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from .captured_paper_selection_producer import CapturedPaperSelectionObservation
from .context import (
    ChopExpansionRegime,
    MomentumRegimeContext,
    VolatilityRegime,
)
from .features import ExecutionReadinessFeatures
from .replay_capture_contract import (
    STREAM_POLICIES,
    CaptureContractError,
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureStream,
    CoverageGap,
    CoverageMode,
    FSMDependencyProfile,
    ProviderWatermark,
    StreamCoverage,
    captured_read_result_sha256,
    sha256_json,
)
from .variants import MomentumStrategyFamily
from .viability import (
    ViabilityExternalInputs,
    ViabilityResult,
    ViabilitySettingsProjection,
    score_viability_explicit,
)


UTC = timezone.utc
BUNDLE_SCHEMA_VERSION = "chili.captured-viability-input-bundle.v1"
INVENTORY_SCHEMA_VERSION = "chili.captured-viability-dependency-inventory.v1"
BINDING_SCHEMA_VERSION = "chili.captured-viability-dependency-binding.v1"
AUTHORITY_SCHEMA_VERSION = "chili.captured-viability-scoring-authority.v2"
RESULT_SCHEMA_VERSION = "chili.captured-viability-score-result.v1"
REGIME_SNAPSHOT_SCHEMA_VERSION = "chili.captured-viability-regime-snapshot.v1"
READINESS_SCHEMA_VERSION = "chili.captured-viability-readiness.v1"
EXPLAIN_SCHEMA_VERSION = "chili.captured-viability-explain.v1"
EVIDENCE_SCHEMA_VERSION = "chili.captured-viability-evidence.v1"
POST_SCORE_SCHEMA_VERSION = "chili.captured-viability-post-score.v1"

SCORED = "SCORED"
COVERAGE_UNAVAILABLE = "COVERAGE_UNAVAILABLE"
EXECUTABLE_ELIGIBILITY_BASIS = "intended_live_eligible"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.:-]{0,35}$")

# Every value which can alter the explicit scorer or its persisted policy
# projection is bound exactly once by the dependency inventory.  A binding may
# cite several source events/read receipts and one source may legitimately
# explain several derived components.
REQUIRED_COMPONENTS = (
    "symbol",
    "variant",
    "family",
    "regime_context",
    "execution_readiness",
    "settings_projection",
    "external_inputs",
    "post_score_adjustment",
    "clock.event_at",
    "clock.available_at",
    "clock.read_at",
    "capture_identity",
    "policy",
    "config",
    "code_build",
)

_BOOLEAN_SETTING_FIELDS = frozenset(
    {
        "chili_momentum_exclude_leveraged_etfs",
        "chili_momentum_exclude_fund_structures_enabled",
        "chili_momentum_thin_spread_squeeze_lane_enabled",
        "chili_momentum_live_eligible_allow_extreme_explosive",
        "chili_momentum_a_setup_quality_floor_enabled",
        "chili_momentum_no_signal_derank_enabled",
        "chili_momentum_catalyst_grade_gate_enabled",
        "chili_momentum_dilution_history_derate_enabled",
        "chili_momentum_theme_sympathy_enabled",
        "chili_momentum_thick_tape_veto_enabled",
        "chili_momentum_nonmonotonic_volume_enabled",
        "chili_momentum_explosive_prequal_floor_enabled",
    }
)
_NUMERIC_SETTING_FIELDS = frozenset(ViabilitySettingsProjection.__dataclass_fields__) - (
    _BOOLEAN_SETTING_FIELDS
)
_BOOLEAN_EXTERNAL_FIELDS = frozenset(
    {"leveraged_etf", "excluded_fund", "below_explosive_floor"}
)
_OPTIONAL_NUMERIC_EXTERNAL_FIELDS = frozenset(
    {
        "ross_rvol",
        "ross_change_pct",
        "ross_float_shares",
        "squeeze_fuel_rank_pct",
    }
)
_NUMERIC_EXTERNAL_FIELDS = (
    frozenset(ViabilityExternalInputs.__dataclass_fields__)
    - _BOOLEAN_EXTERNAL_FIELDS
    - _OPTIONAL_NUMERIC_EXTERNAL_FIELDS
)
_NUMERIC_FEATURE_FIELDS = frozenset(
    {
        "spread_bps",
        "bid_ask_drift_bps",
        "book_imbalance",
        "ofi",
        "micro_price_edge",
        "trade_flow",
        "tape_velocity_z",
        "slippage_estimate_bps",
        "fee_to_target_ratio",
        "float_rotation",
        "projected_rotation_at_eod",
    }
)


class CapturedViabilityContractError(ValueError):
    """A serialized or typed viability input is not safe to score."""


def _contract(message: str) -> None:
    raise CapturedViabilityContractError(message)


def _sha(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if normalized != value or _SHA_RE.fullmatch(normalized) is None:
        _contract(f"{field_name} must be a lowercase SHA-256")
    return normalized


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _contract(f"{field_name} must be timezone-aware")
    try:
        if value.utcoffset() is None:
            _contract(f"{field_name} must be timezone-aware")
    except Exception as exc:
        raise CapturedViabilityContractError(
            f"{field_name} has an invalid timezone"
        ) from exc
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        _contract(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedViabilityContractError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    return _utc(parsed, field_name)


def _uuid(value: Any, field_name: str) -> str:
    raw = str(value or "").strip()
    try:
        normalized = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedViabilityContractError(f"{field_name} must be a UUID") from exc
    if raw != normalized:
        _contract(f"{field_name} must be canonical")
    return normalized


def _strict_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        _contract(f"{field_name} must be boolean")
    return value


def _finite_number(value: Any, field_name: str, *, optional: bool = False) -> Any:
    if value is None and optional:
        return None
    if type(value) not in {int, float}:
        _contract(f"{field_name} must be a finite number")
    if not math.isfinite(float(value)):
        _contract(f"{field_name} must be a finite number")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        _contract(f"{field_name} must be a positive integer")
    return value


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _contract(f"{field_name} must be an object")
    return value


def _exact_fields(raw: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    if set(raw) != expected:
        _contract(f"{field_name} fields do not match schema")


def _encode_value(value: Any) -> Any:
    """Lossless deterministic JSON encoding for nested scorer inputs."""

    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _contract("non-finite scorer input is forbidden")
        return value
    if isinstance(value, datetime):
        return {"$kind": "datetime", "value": _iso(value)}
    if isinstance(value, Enum):
        _contract("nested enum scorer inputs are not replayable")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            _contract("scorer mapping keys must be strings")
        return {
            "$kind": "mapping",
            "items": [
                [key, _encode_value(value[key])]
                for key in sorted(value)
            ],
        }
    if type(value) is list:
        return {"$kind": "list", "items": [_encode_value(item) for item in value]}
    if type(value) is tuple:
        return {"$kind": "tuple", "items": [_encode_value(item) for item in value]}
    if type(value) in {set, frozenset}:
        encoded = [_encode_value(item) for item in value]
        encoded.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return {
            "$kind": "frozenset" if type(value) is frozenset else "set",
            "items": encoded,
        }
    _contract(f"unsupported scorer input type {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _contract("non-finite scorer input is forbidden")
        return value
    raw = _mapping(value, "encoded_value")
    kind = raw.get("$kind")
    if kind == "datetime":
        _exact_fields(raw, {"$kind", "value"}, "encoded_datetime")
        return _parse_utc(raw.get("value"), "encoded_datetime.value")
    if kind == "enum":
        # Enums inside nested meta are preserved for hashing but deliberately
        # not dynamically imported.  The scorer's supported nested payloads are
        # primitive containers; an enum there cannot be replayed hermetically.
        _contract("nested enum scorer inputs are not replayable")
    if kind == "mapping":
        _exact_fields(raw, {"$kind", "items"}, "encoded_mapping")
        items = raw.get("items")
        if not isinstance(items, list):
            _contract("encoded mapping items are malformed")
        output: dict[str, Any] = {}
        for row in items:
            if not isinstance(row, list) or len(row) != 2 or type(row[0]) is not str:
                _contract("encoded mapping row is malformed")
            if row[0] in output:
                _contract("encoded mapping contains duplicate keys")
            output[row[0]] = _decode_value(row[1])
        if list(output) != sorted(output):
            _contract("encoded mapping keys are not canonical")
        return output
    if kind in {"list", "tuple", "set", "frozenset"}:
        _exact_fields(raw, {"$kind", "items"}, f"encoded_{kind}")
        items = raw.get("items")
        if not isinstance(items, list):
            _contract(f"encoded {kind} items are malformed")
        decoded = [_decode_value(item) for item in items]
        if kind == "list":
            return decoded
        if kind == "tuple":
            return tuple(decoded)
        try:
            return frozenset(decoded) if kind == "frozenset" else set(decoded)
        except TypeError as exc:
            raise CapturedViabilityContractError(
                f"encoded {kind} contains an unhashable value"
            ) from exc
    _contract("encoded scorer input kind is unsupported")


def _family_body(family: MomentumStrategyFamily) -> dict[str, Any]:
    if type(family) is not MomentumStrategyFamily:
        _contract("family must be MomentumStrategyFamily")
    if type(family.version) is not int or family.version <= 0:
        _contract("family version must be a positive integer")
    for name in (
        "family_id",
        "label",
        "entry_style",
        "default_stop_logic",
        "default_exit_logic",
    ):
        value = getattr(family, name)
        if type(value) is not str or not value or value != value.strip():
            _contract(f"family {name} is invalid")
    return {
        "family_id": family.family_id,
        "version": family.version,
        "label": family.label,
        "entry_style": family.entry_style,
        "default_stop_logic": family.default_stop_logic,
        "default_exit_logic": family.default_exit_logic,
    }


def _context_body(context: MomentumRegimeContext) -> dict[str, Any]:
    if type(context) is not MomentumRegimeContext:
        _contract("context must be MomentumRegimeContext")
    if type(context.utc_iso) is not str:
        _contract("context utc_iso is invalid")
    _parse_utc(context.utc_iso, "context.utc_iso")
    if type(context.utc_hour) is not int or not 0 <= context.utc_hour <= 23:
        _contract("context utc_hour is invalid")
    if type(context.vol_regime) is not VolatilityRegime:
        _contract("context volatility regime type is invalid")
    if type(context.chop_expansion) is not ChopExpansionRegime:
        _contract("context chop/expansion type is invalid")
    for name in (
        "session_label",
        "spread_regime",
        "fee_burden_regime",
        "liquidity_regime",
        "exhaustion_cooldown",
        "rolling_range_state",
        "breakout_continuity",
    ):
        if type(getattr(context, name)) is not str:
            _contract(f"context {name} type is invalid")
    if type(context.meta) is not dict:
        _contract("context meta must be an exact dict")
    return {
        "utc_iso": context.utc_iso,
        "utc_hour": context.utc_hour,
        "session_label": context.session_label,
        "vol_regime": context.vol_regime.value,
        "chop_expansion": context.chop_expansion.value,
        "spread_regime": context.spread_regime,
        "fee_burden_regime": context.fee_burden_regime,
        "liquidity_regime": context.liquidity_regime,
        "exhaustion_cooldown": context.exhaustion_cooldown,
        "rolling_range_state": context.rolling_range_state,
        "breakout_continuity": context.breakout_continuity,
        "meta": _encode_value(context.meta),
    }


def _features_body(features: ExecutionReadinessFeatures) -> dict[str, Any]:
    if type(features) is not ExecutionReadinessFeatures:
        _contract("features must be ExecutionReadinessFeatures")
    for name in _NUMERIC_FEATURE_FIELDS:
        _finite_number(getattr(features, name), f"features.{name}", optional=True)
    if features.product_tradable is not None:
        _strict_bool(features.product_tradable, "features.product_tradable")
    if features.meta is not None and type(features.meta) is not dict:
        _contract("features meta must be an exact dict or None")
    return {
        "spread_bps": _encode_value(features.spread_bps),
        "bid_ask_drift_bps": _encode_value(features.bid_ask_drift_bps),
        "book_imbalance": _encode_value(features.book_imbalance),
        "ofi": _encode_value(features.ofi),
        "micro_price_edge": _encode_value(features.micro_price_edge),
        "trade_flow": _encode_value(features.trade_flow),
        "tape_velocity_z": _encode_value(features.tape_velocity_z),
        "slippage_estimate_bps": _encode_value(features.slippage_estimate_bps),
        "fee_to_target_ratio": _encode_value(features.fee_to_target_ratio),
        "product_tradable": _encode_value(features.product_tradable),
        "float_rotation": _encode_value(features.float_rotation),
        "projected_rotation_at_eod": _encode_value(
            features.projected_rotation_at_eod
        ),
        "meta": _encode_value(features.meta),
    }


def _settings_body(settings: ViabilitySettingsProjection) -> dict[str, Any]:
    if type(settings) is not ViabilitySettingsProjection:
        _contract("settings must be ViabilitySettingsProjection")
    if (
        _BOOLEAN_SETTING_FIELDS | _NUMERIC_SETTING_FIELDS
        != frozenset(settings.__dataclass_fields__)
    ):
        _contract("settings validation schema is incomplete")
    for name in _BOOLEAN_SETTING_FIELDS:
        _strict_bool(getattr(settings, name), f"settings.{name}")
    for name in _NUMERIC_SETTING_FIELDS:
        _finite_number(getattr(settings, name), f"settings.{name}")
    return {key: _encode_value(value) for key, value in settings.to_dict().items()}


def _external_body(external: ViabilityExternalInputs) -> dict[str, Any]:
    if type(external) is not ViabilityExternalInputs:
        _contract("external inputs must be ViabilityExternalInputs")
    if (
        _BOOLEAN_EXTERNAL_FIELDS
        | _OPTIONAL_NUMERIC_EXTERNAL_FIELDS
        | _NUMERIC_EXTERNAL_FIELDS
        != frozenset(external.__dataclass_fields__)
    ):
        _contract("external input validation schema is incomplete")
    for name in _BOOLEAN_EXTERNAL_FIELDS:
        _strict_bool(getattr(external, name), f"external.{name}")
    for name in _OPTIONAL_NUMERIC_EXTERNAL_FIELDS:
        _finite_number(getattr(external, name), f"external.{name}", optional=True)
    for name in _NUMERIC_EXTERNAL_FIELDS:
        _finite_number(getattr(external, name), f"external.{name}")
    return {key: _encode_value(value) for key, value in external.to_dict().items()}


@dataclass(frozen=True, slots=True)
class CapturedViabilityPostScoreAdjustment:
    """Exact pipeline adjustment applied after ``score_viability``.

    The legacy persistence path rounds the core viability to four decimals and
    then optionally adds ``tenbeat_entry_tilt_weight * breakout_score``.  An
    enabled crypto lookup must carry its exact captured read id; an equity is
    explicitly inapplicable and a zero-weight policy is explicitly disabled.
    """

    tenbeat_entry_tilt_weight: float
    tenbeat_breakout_score: float | None
    lookup_status: str
    source_read_id: str | None
    adjustment_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        weight = _finite_number(
            self.tenbeat_entry_tilt_weight,
            "tenbeat_entry_tilt_weight",
        )
        weight = float(weight)
        if not math.isfinite(weight) or weight < 0:
            _contract("tenbeat entry tilt weight must be finite and non-negative")
        object.__setattr__(self, "tenbeat_entry_tilt_weight", weight)
        score = self.tenbeat_breakout_score
        if score is not None:
            score = float(
                _finite_number(score, "tenbeat_breakout_score")
            )
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                _contract("tenbeat breakout score must be within [0,1]")
            object.__setattr__(self, "tenbeat_breakout_score", score)
        status = str(self.lookup_status or "").strip().lower()
        if status not in {
            "disabled",
            "inapplicable_non_crypto",
            "captured_value",
            "captured_empty",
        }:
            _contract("tenbeat lookup status is invalid")
        object.__setattr__(self, "lookup_status", status)
        read_id = self.source_read_id
        if read_id is not None:
            read_id = _uuid(read_id, "post_score.source_read_id")
            object.__setattr__(self, "source_read_id", read_id)
        if status == "disabled" and not (
            weight == 0.0 and score is None and read_id is None
        ):
            _contract("disabled tenbeat adjustment is internally inconsistent")
        if status == "inapplicable_non_crypto" and not (
            weight > 0.0 and score is None and read_id is None
        ):
            _contract("inapplicable tenbeat adjustment is internally inconsistent")
        if status == "captured_value" and not (
            weight > 0.0 and score is not None and read_id is not None
        ):
            _contract("captured tenbeat value is internally inconsistent")
        if status == "captured_empty" and not (
            weight > 0.0 and score is None and read_id is not None
        ):
            _contract("captured empty tenbeat read is internally inconsistent")
        object.__setattr__(self, "adjustment_sha256", sha256_json(self.body()))

    @property
    def applied_delta(self) -> float:
        if self.lookup_status != "captured_value":
            return 0.0
        assert self.tenbeat_breakout_score is not None
        return self.tenbeat_entry_tilt_weight * self.tenbeat_breakout_score

    def persisted_viability(self, result: ViabilityResult) -> float:
        # This intentionally matches pipeline.py: to_public_dict() rounds the
        # core score before the post-score tilt, then min(1.0, base + delta).
        public_base = round(float(result.viability), 4)
        if self.lookup_status == "captured_value":
            return min(1.0, public_base + self.applied_delta)
        return public_base

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": POST_SCORE_SCHEMA_VERSION,
            "tenbeat_entry_tilt_weight": self.tenbeat_entry_tilt_weight,
            "tenbeat_breakout_score": self.tenbeat_breakout_score,
            "lookup_status": self.lookup_status,
            "source_read_id": self.source_read_id,
            "applied_delta": self.applied_delta,
            "public_delta": round(self.applied_delta, 4),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "adjustment_sha256": self.adjustment_sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapturedViabilityPostScoreAdjustment":
        _exact_fields(
            raw,
            {
                "schema_version",
                "tenbeat_entry_tilt_weight",
                "tenbeat_breakout_score",
                "lookup_status",
                "source_read_id",
                "applied_delta",
                "public_delta",
                "adjustment_sha256",
            },
            "post_score_adjustment",
        )
        if raw.get("schema_version") != POST_SCORE_SCHEMA_VERSION:
            _contract("post-score adjustment schema is unsupported")
        row = cls(
            tenbeat_entry_tilt_weight=raw.get("tenbeat_entry_tilt_weight"),
            tenbeat_breakout_score=raw.get("tenbeat_breakout_score"),
            lookup_status=str(raw.get("lookup_status") or ""),
            source_read_id=raw.get("source_read_id"),
        )
        if (
            raw.get("applied_delta") != row.applied_delta
            or raw.get("public_delta") != round(row.applied_delta, 4)
            or raw.get("adjustment_sha256") != row.adjustment_sha256
        ):
            _contract("post-score adjustment content hash mismatch")
        if raw != row.to_dict():
            _contract("post-score adjustment encoding is not canonical")
        return row


def _component_hash(value: Any, name: str) -> str:
    return sha256_json(
        {
            "schema_version": "chili.captured-viability-component.v1",
            "component": name,
            "value": value,
        }
    )


def captured_viability_component_sha256s(
    *,
    symbol: str,
    variant_id: int,
    family: MomentumStrategyFamily,
    context: MomentumRegimeContext,
    features: ExecutionReadinessFeatures,
    settings: ViabilitySettingsProjection,
    external: ViabilityExternalInputs,
    post_score_adjustment: CapturedViabilityPostScoreAdjustment,
    event_at: datetime,
    available_at: datetime,
    read_at: datetime,
    capture_identity_sha256: str,
    policy_sha256: str,
    config_sha256: str,
    code_sha256: str,
) -> dict[str, str]:
    """Return the exhaustive component roots required by an inventory."""

    symbol_value = str(symbol or "").strip().upper()
    if symbol_value != symbol or _SYMBOL_RE.fullmatch(symbol_value) is None:
        _contract("symbol is not canonical")
    variant_value = _positive_int(variant_id, "variant_id")
    event = _utc(event_at, "event_at")
    available = _utc(available_at, "available_at")
    read = _utc(read_at, "read_at")
    if not event <= available <= read:
        _contract("bundle clocks are not causal")
    identity = _sha(capture_identity_sha256, "capture_identity_sha256")
    policy = _sha(policy_sha256, "policy_sha256")
    config = _sha(config_sha256, "config_sha256")
    code = _sha(code_sha256, "code_sha256")
    if type(post_score_adjustment) is not CapturedViabilityPostScoreAdjustment:
        _contract("post_score_adjustment type is invalid")
    values = {
        "symbol": symbol_value,
        "variant": variant_value,
        "family": _family_body(family),
        "regime_context": _context_body(context),
        "execution_readiness": _features_body(features),
        "settings_projection": _settings_body(settings),
        "external_inputs": _external_body(external),
        "post_score_adjustment": post_score_adjustment.to_dict(),
        "clock.event_at": _iso(event),
        "clock.available_at": _iso(available),
        "clock.read_at": _iso(read),
        "capture_identity": identity,
        "policy": policy,
        "config": config,
        "code_build": code,
    }
    return {name: _component_hash(values[name], name) for name in REQUIRED_COMPONENTS}


def captured_viability_read_receipt_sha256(receipt: CaptureReadReceipt) -> str:
    if type(receipt) is not CaptureReadReceipt:
        _contract("receipt must be CaptureReadReceipt")
    return sha256_json(
        {
            "schema_version": "chili.captured-viability-read-receipt.v1",
            "receipt": receipt.to_dict(),
        }
    )


def _event_ref_envelope(ref: CaptureEventRef) -> dict[str, Any]:
    payload = ref.to_dict()
    return {"event_ref": payload, "event_ref_sha256": sha256_json(payload)}


def _receipt_envelope(receipt: CaptureReadReceipt) -> dict[str, Any]:
    return {
        "receipt": receipt.to_dict(),
        "receipt_sha256": captured_viability_read_receipt_sha256(receipt),
    }


@dataclass(frozen=True, slots=True)
class CapturedViabilityDependencyBinding:
    component: str
    component_sha256: str
    source_event_sha256s: tuple[str, ...]
    read_receipt_sha256s: tuple[str, ...]
    binding_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        component = str(self.component or "").strip()
        if component not in REQUIRED_COMPONENTS:
            _contract("dependency binding component is unknown")
        object.__setattr__(self, "component", component)
        object.__setattr__(
            self,
            "component_sha256",
            _sha(self.component_sha256, "component_sha256"),
        )
        event_hashes = tuple(
            sorted(_sha(value, "source_event_sha256") for value in self.source_event_sha256s)
        )
        receipt_hashes = tuple(
            sorted(
                _sha(value, "read_receipt_sha256")
                for value in self.read_receipt_sha256s
            )
        )
        if (
            len(event_hashes) != len(set(event_hashes))
            or len(receipt_hashes) != len(set(receipt_hashes))
            or not (event_hashes or receipt_hashes)
        ):
            _contract("dependency binding evidence is empty or duplicated")
        object.__setattr__(self, "source_event_sha256s", event_hashes)
        object.__setattr__(self, "read_receipt_sha256s", receipt_hashes)
        object.__setattr__(self, "binding_sha256", sha256_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": BINDING_SCHEMA_VERSION,
            "component": self.component,
            "component_sha256": self.component_sha256,
            "source_event_sha256s": list(self.source_event_sha256s),
            "read_receipt_sha256s": list(self.read_receipt_sha256s),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "binding_sha256": self.binding_sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapturedViabilityDependencyBinding":
        _exact_fields(
            raw,
            {
                "schema_version",
                "component",
                "component_sha256",
                "source_event_sha256s",
                "read_receipt_sha256s",
                "binding_sha256",
            },
            "dependency_binding",
        )
        if raw.get("schema_version") != BINDING_SCHEMA_VERSION:
            _contract("dependency binding schema is unsupported")
        events = raw.get("source_event_sha256s")
        receipts = raw.get("read_receipt_sha256s")
        if not isinstance(events, list) or not isinstance(receipts, list):
            _contract("dependency binding evidence arrays are malformed")
        row = cls(
            component=str(raw.get("component") or ""),
            component_sha256=str(raw.get("component_sha256") or ""),
            source_event_sha256s=tuple(str(value) for value in events),
            read_receipt_sha256s=tuple(str(value) for value in receipts),
        )
        if raw.get("binding_sha256") != row.binding_sha256:
            _contract("dependency binding content hash mismatch")
        if raw != row.to_dict():
            _contract("dependency binding encoding is not canonical")
        return row


@dataclass(frozen=True, slots=True)
class CapturedViabilityDependencyInventory:
    dependency_profile: FSMDependencyProfile
    bindings: tuple[CapturedViabilityDependencyBinding, ...]
    inventory_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.dependency_profile) is not FSMDependencyProfile:
            _contract("dependency profile type is invalid")
        bindings = tuple(sorted(self.bindings, key=lambda item: item.component))
        if any(type(item) is not CapturedViabilityDependencyBinding for item in bindings):
            _contract("dependency bindings are malformed")
        if tuple(item.component for item in bindings) != tuple(sorted(REQUIRED_COMPONENTS)):
            _contract("dependency inventory is missing or has extra components")
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "inventory_sha256", sha256_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "dependency_profile": self.dependency_profile.to_dict(),
            "dependency_profile_sha256": self.dependency_profile.profile_sha256,
            "bindings": [item.to_dict() for item in self.bindings],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "inventory_sha256": self.inventory_sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapturedViabilityDependencyInventory":
        _exact_fields(
            raw,
            {
                "schema_version",
                "dependency_profile",
                "dependency_profile_sha256",
                "bindings",
                "inventory_sha256",
            },
            "dependency_inventory",
        )
        if raw.get("schema_version") != INVENTORY_SCHEMA_VERSION:
            _contract("dependency inventory schema is unsupported")
        raw_profile = _mapping(raw.get("dependency_profile"), "dependency_profile")
        raw_bindings = raw.get("bindings")
        if not isinstance(raw_bindings, list):
            _contract("dependency bindings are malformed")
        try:
            profile = FSMDependencyProfile.from_dict(raw_profile)
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedViabilityContractError("dependency profile is invalid") from exc
        if raw.get("dependency_profile_sha256") != profile.profile_sha256:
            _contract("dependency profile hash mismatch")
        row = cls(
            dependency_profile=profile,
            bindings=tuple(
                CapturedViabilityDependencyBinding.from_dict(
                    _mapping(value, "dependency_binding")
                )
                for value in raw_bindings
            ),
        )
        if raw.get("inventory_sha256") != row.inventory_sha256:
            _contract("dependency inventory hash mismatch")
        if raw != row.to_dict():
            _contract("dependency inventory encoding is not canonical")
        return row


@dataclass(frozen=True, slots=True)
class CapturedViabilityScoringAuthority:
    """Hash-pinned scorer inputs plus the enclosing activation authority.

    ``policy/config/code/settings`` bind the exact bundle consumed by the
    scorer.  The explicit ``activation_*`` fields and selection-authority hash
    bind that scorer invocation to the selected PAPER activation.  Keeping the
    two domains separate prevents a bundle-local content hash from being
    mistaken for an activation policy/build/settings receipt.
    """

    capture_identity_sha256: str
    policy_sha256: str
    config_sha256: str
    code_sha256: str
    settings_projection_sha256: str
    family_sha256: str
    dependency_profile_sha256: str
    activation_policy_sha256: str
    activation_settings_projection_sha256: str
    activation_code_build_sha256: str
    selection_authority_sha256: str
    variant_id: int
    family_id: str
    family_version: int
    executable_eligibility_basis: str = EXECUTABLE_ELIGIBILITY_BASIS
    paper_only_strategy_override: bool = False
    live_cash_authorized: bool = False
    real_money_authorized: bool = False
    authority_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "capture_identity_sha256",
            "policy_sha256",
            "config_sha256",
            "code_sha256",
            "settings_projection_sha256",
            "family_sha256",
            "dependency_profile_sha256",
            "activation_policy_sha256",
            "activation_settings_projection_sha256",
            "activation_code_build_sha256",
            "selection_authority_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "variant_id", _positive_int(self.variant_id, "variant_id"))
        family_id = str(self.family_id or "").strip()
        if not family_id or family_id != self.family_id:
            _contract("authority family_id is invalid")
        object.__setattr__(
            self,
            "family_version",
            _positive_int(self.family_version, "family_version"),
        )
        if self.executable_eligibility_basis != EXECUTABLE_ELIGIBILITY_BASIS:
            _contract("authority executable eligibility basis is unsupported")
        for name in (
            "paper_only_strategy_override",
            "live_cash_authorized",
            "real_money_authorized",
        ):
            if _strict_bool(getattr(self, name), name):
                _contract(f"{name} must remain false")
        object.__setattr__(self, "authority_sha256", sha256_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "capture_identity_sha256": self.capture_identity_sha256,
            "policy_sha256": self.policy_sha256,
            "config_sha256": self.config_sha256,
            "code_sha256": self.code_sha256,
            "settings_projection_sha256": self.settings_projection_sha256,
            "family_sha256": self.family_sha256,
            "dependency_profile_sha256": self.dependency_profile_sha256,
            "activation_policy_sha256": self.activation_policy_sha256,
            "activation_settings_projection_sha256": (
                self.activation_settings_projection_sha256
            ),
            "activation_code_build_sha256": self.activation_code_build_sha256,
            "selection_authority_sha256": self.selection_authority_sha256,
            "variant_id": self.variant_id,
            "family_id": self.family_id,
            "family_version": self.family_version,
            "executable_eligibility_basis": self.executable_eligibility_basis,
            "paper_only_strategy_override": self.paper_only_strategy_override,
            "live_cash_authorized": self.live_cash_authorized,
            "real_money_authorized": self.real_money_authorized,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "authority_sha256": self.authority_sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapturedViabilityScoringAuthority":
        _exact_fields(
            raw,
            {
                "schema_version",
                "capture_identity_sha256",
                "policy_sha256",
                "config_sha256",
                "code_sha256",
                "settings_projection_sha256",
                "family_sha256",
                "dependency_profile_sha256",
                "activation_policy_sha256",
                "activation_settings_projection_sha256",
                "activation_code_build_sha256",
                "selection_authority_sha256",
                "variant_id",
                "family_id",
                "family_version",
                "executable_eligibility_basis",
                "paper_only_strategy_override",
                "live_cash_authorized",
                "real_money_authorized",
                "authority_sha256",
            },
            "scoring_authority",
        )
        if raw.get("schema_version") != AUTHORITY_SCHEMA_VERSION:
            _contract("scoring authority schema is unsupported")
        row = cls(
            capture_identity_sha256=str(raw.get("capture_identity_sha256") or ""),
            policy_sha256=str(raw.get("policy_sha256") or ""),
            config_sha256=str(raw.get("config_sha256") or ""),
            code_sha256=str(raw.get("code_sha256") or ""),
            settings_projection_sha256=str(
                raw.get("settings_projection_sha256") or ""
            ),
            family_sha256=str(raw.get("family_sha256") or ""),
            dependency_profile_sha256=str(
                raw.get("dependency_profile_sha256") or ""
            ),
            activation_policy_sha256=str(
                raw.get("activation_policy_sha256") or ""
            ),
            activation_settings_projection_sha256=str(
                raw.get("activation_settings_projection_sha256") or ""
            ),
            activation_code_build_sha256=str(
                raw.get("activation_code_build_sha256") or ""
            ),
            selection_authority_sha256=str(
                raw.get("selection_authority_sha256") or ""
            ),
            variant_id=raw.get("variant_id"),
            family_id=str(raw.get("family_id") or ""),
            family_version=raw.get("family_version"),
            executable_eligibility_basis=str(
                raw.get("executable_eligibility_basis") or ""
            ),
            paper_only_strategy_override=raw.get("paper_only_strategy_override"),
            live_cash_authorized=raw.get("live_cash_authorized"),
            real_money_authorized=raw.get("real_money_authorized"),
        )
        if raw.get("authority_sha256") != row.authority_sha256:
            _contract("scoring authority hash mismatch")
        if raw != row.to_dict():
            _contract("scoring authority encoding is not canonical")
        return row


def _event_ref_from_dict(raw: Mapping[str, Any]) -> CaptureEventRef:
    _exact_fields(
        raw,
        {
            "identity_sha256",
            "event_sha256",
            "sequence",
            "stream",
            "received_at",
            "available_at",
            "payload_sha256",
            "query_sha256",
            "provider",
            "symbol",
            "provider_event_at",
            "market_reference_at",
        },
        "event_ref",
    )
    try:
        return CaptureEventRef(
            identity_sha256=str(raw.get("identity_sha256") or ""),
            event_sha256=str(raw.get("event_sha256") or ""),
            sequence=raw.get("sequence"),
            stream=CaptureStream(str(raw.get("stream") or "")),
            received_at=_parse_utc(raw.get("received_at"), "event_ref.received_at"),
            available_at=_parse_utc(
                raw.get("available_at"), "event_ref.available_at"
            ),
            payload_sha256=str(raw.get("payload_sha256") or ""),
            query_sha256=raw.get("query_sha256"),
            provider=str(raw.get("provider") or ""),
            symbol=raw.get("symbol"),
            provider_event_at=(
                _parse_utc(raw.get("provider_event_at"), "provider_event_at")
                if raw.get("provider_event_at") is not None
                else None
            ),
            market_reference_at=(
                _parse_utc(raw.get("market_reference_at"), "market_reference_at")
                if raw.get("market_reference_at") is not None
                else None
            ),
        )
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise CapturedViabilityContractError("event reference is invalid") from exc


def _gap_from_dict(raw: Mapping[str, Any]) -> CoverageGap:
    _exact_fields(
        raw,
        {
            "stream",
            "reason",
            "first_available_at",
            "last_available_at",
            "lost_count",
            "symbol",
        },
        "coverage_gap",
    )
    try:
        return CoverageGap(
            stream=CaptureStream(str(raw.get("stream") or "")),
            reason=str(raw.get("reason") or ""),
            first_available_at=_parse_utc(
                raw.get("first_available_at"), "gap.first_available_at"
            ),
            last_available_at=_parse_utc(
                raw.get("last_available_at"), "gap.last_available_at"
            ),
            lost_count=raw.get("lost_count"),
            symbol=raw.get("symbol"),
        )
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise CapturedViabilityContractError("coverage gap is invalid") from exc


def _family_from_dict(raw: Mapping[str, Any]) -> MomentumStrategyFamily:
    _exact_fields(
        raw,
        {
            "family_id",
            "version",
            "label",
            "entry_style",
            "default_stop_logic",
            "default_exit_logic",
        },
        "family",
    )
    return MomentumStrategyFamily(
        family_id=str(raw.get("family_id") or ""),
        version=_positive_int(raw.get("version"), "family.version"),
        label=str(raw.get("label") or ""),
        entry_style=str(raw.get("entry_style") or ""),
        default_stop_logic=str(raw.get("default_stop_logic") or ""),
        default_exit_logic=str(raw.get("default_exit_logic") or ""),
    )


def _context_from_dict(raw: Mapping[str, Any]) -> MomentumRegimeContext:
    _exact_fields(
        raw,
        {
            "utc_iso",
            "utc_hour",
            "session_label",
            "vol_regime",
            "chop_expansion",
            "spread_regime",
            "fee_burden_regime",
            "liquidity_regime",
            "exhaustion_cooldown",
            "rolling_range_state",
            "breakout_continuity",
            "meta",
        },
        "regime_context",
    )
    meta = _decode_value(raw.get("meta"))
    if type(meta) is not dict:
        _contract("regime context meta is not a mapping")
    utc_hour = raw.get("utc_hour")
    if type(utc_hour) is not int:
        _contract("regime context utc_hour is not canonical")
    try:
        return MomentumRegimeContext(
            utc_iso=str(raw.get("utc_iso") or ""),
            utc_hour=utc_hour,
            session_label=str(raw.get("session_label") or ""),
            vol_regime=VolatilityRegime(str(raw.get("vol_regime") or "")),
            chop_expansion=ChopExpansionRegime(
                str(raw.get("chop_expansion") or "")
            ),
            spread_regime=str(raw.get("spread_regime") or ""),
            fee_burden_regime=str(raw.get("fee_burden_regime") or ""),
            liquidity_regime=str(raw.get("liquidity_regime") or ""),
            exhaustion_cooldown=str(raw.get("exhaustion_cooldown") or ""),
            rolling_range_state=str(raw.get("rolling_range_state") or ""),
            breakout_continuity=str(raw.get("breakout_continuity") or ""),
            meta=meta,
        )
    except (TypeError, ValueError) as exc:
        raise CapturedViabilityContractError("regime context is invalid") from exc


def _features_from_dict(raw: Mapping[str, Any]) -> ExecutionReadinessFeatures:
    expected = {
        "spread_bps",
        "bid_ask_drift_bps",
        "book_imbalance",
        "ofi",
        "micro_price_edge",
        "trade_flow",
        "tape_velocity_z",
        "slippage_estimate_bps",
        "fee_to_target_ratio",
        "product_tradable",
        "float_rotation",
        "projected_rotation_at_eod",
        "meta",
    }
    _exact_fields(raw, expected, "execution_readiness")
    decoded = {name: _decode_value(raw.get(name)) for name in expected}
    meta = decoded.pop("meta")
    if meta is not None and type(meta) is not dict:
        _contract("execution readiness meta is not a mapping")
    return ExecutionReadinessFeatures(**decoded, meta=meta)


def _projection_from_dict(raw: Mapping[str, Any]) -> ViabilitySettingsProjection:
    expected = set(ViabilitySettingsProjection.__dataclass_fields__)
    _exact_fields(raw, expected, "settings_projection")
    return ViabilitySettingsProjection(
        **{name: _decode_value(raw.get(name)) for name in expected}
    )


def _external_from_dict(raw: Mapping[str, Any]) -> ViabilityExternalInputs:
    expected = set(ViabilityExternalInputs.__dataclass_fields__)
    _exact_fields(raw, expected, "external_inputs")
    return ViabilityExternalInputs(
        **{name: _decode_value(raw.get(name)) for name in expected}
    )


@dataclass(frozen=True, slots=True)
class CapturedViabilityInputBundle:
    source_sequence: int
    event_at: datetime
    available_at: datetime
    read_at: datetime
    symbol: str
    variant_id: int
    family: MomentumStrategyFamily
    context: MomentumRegimeContext
    features: ExecutionReadinessFeatures
    settings: ViabilitySettingsProjection
    external: ViabilityExternalInputs
    post_score_adjustment: CapturedViabilityPostScoreAdjustment
    capture_identity_sha256: str
    policy_sha256: str
    config_sha256: str
    code_sha256: str
    dependency_inventory: CapturedViabilityDependencyInventory
    source_refs: tuple[CaptureEventRef, ...]
    read_receipts: tuple[CaptureReadReceipt, ...]
    stream_coverages: tuple[StreamCoverage, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    correlation_id: str
    component_sha256s: tuple[tuple[str, str], ...] = field(init=False)
    bundle_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_sequence", _positive_int(self.source_sequence, "source_sequence")
        )
        event = _utc(self.event_at, "event_at")
        available = _utc(self.available_at, "available_at")
        read = _utc(self.read_at, "read_at")
        if not event <= available <= read:
            _contract("bundle clocks are not causal")
        object.__setattr__(self, "event_at", event)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "read_at", read)
        symbol = str(self.symbol or "").strip().upper()
        if symbol != self.symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            _contract("symbol is not canonical")
        object.__setattr__(self, "variant_id", _positive_int(self.variant_id, "variant_id"))
        _family_body(self.family)
        _context_body(self.context)
        context_at = _parse_utc(self.context.utc_iso, "context.utc_iso")
        if context_at != event or self.context.utc_hour != event.hour:
            _contract("regime context clock does not match the captured event clock")
        _features_body(self.features)
        _settings_body(self.settings)
        _external_body(self.external)
        if type(self.post_score_adjustment) is not CapturedViabilityPostScoreAdjustment:
            _contract("post_score_adjustment type is invalid")
        for name in (
            "capture_identity_sha256",
            "policy_sha256",
            "config_sha256",
            "code_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        if type(self.dependency_inventory) is not CapturedViabilityDependencyInventory:
            _contract("dependency inventory type is invalid")
        refs = tuple(sorted(self.source_refs, key=lambda item: item.sequence))
        receipts = tuple(sorted(self.read_receipts, key=lambda item: item.read_id))
        coverages = tuple(
            sorted(
                self.stream_coverages,
                key=lambda item: (item.stream.value, item.provider, item.symbol or ""),
            )
        )
        gaps = tuple(
            sorted(
                self.coverage_gaps,
                key=lambda item: (
                    item.stream.value,
                    item.first_available_at,
                    item.last_available_at,
                    item.reason,
                ),
            )
        )
        if (
            any(type(item) is not CaptureEventRef for item in refs)
            or any(type(item) is not CaptureReadReceipt for item in receipts)
            or any(type(item) is not StreamCoverage for item in coverages)
            or any(type(item) is not CoverageGap for item in gaps)
        ):
            _contract("captured provenance types are invalid")
        if (
            not refs
            or len({item.sequence for item in refs}) != len(refs)
            or len({item.event_sha256 for item in refs}) != len(refs)
            or len({item.read_id for item in receipts}) != len(receipts)
            or len({item.stream for item in coverages}) != len(coverages)
        ):
            _contract("captured provenance is empty or duplicated")
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "read_receipts", receipts)
        object.__setattr__(self, "stream_coverages", coverages)
        object.__setattr__(self, "coverage_gaps", gaps)
        correlation = str(self.correlation_id or "").strip()
        if not correlation or correlation != self.correlation_id or len(correlation) > 64:
            _contract("correlation_id is invalid")
        roots = captured_viability_component_sha256s(
            symbol=self.symbol,
            variant_id=self.variant_id,
            family=self.family,
            context=self.context,
            features=self.features,
            settings=self.settings,
            external=self.external,
            post_score_adjustment=self.post_score_adjustment,
            event_at=self.event_at,
            available_at=self.available_at,
            read_at=self.read_at,
            capture_identity_sha256=self.capture_identity_sha256,
            policy_sha256=self.policy_sha256,
            config_sha256=self.config_sha256,
            code_sha256=self.code_sha256,
        )
        object.__setattr__(self, "component_sha256s", tuple(sorted(roots.items())))
        object.__setattr__(self, "bundle_sha256", sha256_json(self.body()))

    @property
    def component_roots(self) -> dict[str, str]:
        return dict(self.component_sha256s)

    @property
    def settings_projection_sha256(self) -> str:
        return self.component_roots["settings_projection"]

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "source_sequence": self.source_sequence,
            "event_at": _iso(self.event_at),
            "available_at": _iso(self.available_at),
            "read_at": _iso(self.read_at),
            "symbol": self.symbol,
            "variant_id": self.variant_id,
            "family": _family_body(self.family),
            "context": _context_body(self.context),
            "features": _features_body(self.features),
            "settings": _settings_body(self.settings),
            "external": _external_body(self.external),
            "post_score_adjustment": self.post_score_adjustment.to_dict(),
            "capture_identity_sha256": self.capture_identity_sha256,
            "policy_sha256": self.policy_sha256,
            "config_sha256": self.config_sha256,
            "code_sha256": self.code_sha256,
            "dependency_inventory": self.dependency_inventory.to_dict(),
            "source_refs": [_event_ref_envelope(item) for item in self.source_refs],
            "read_receipts": [_receipt_envelope(item) for item in self.read_receipts],
            "stream_coverages": [item.to_dict() for item in self.stream_coverages],
            "coverage_gaps": [item.to_dict() for item in self.coverage_gaps],
            "correlation_id": self.correlation_id,
            "component_sha256s": self.component_roots,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "bundle_sha256": self.bundle_sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapturedViabilityInputBundle":
        expected = {
            "schema_version",
            "source_sequence",
            "event_at",
            "available_at",
            "read_at",
            "symbol",
            "variant_id",
            "family",
            "context",
            "features",
            "settings",
            "external",
            "post_score_adjustment",
            "capture_identity_sha256",
            "policy_sha256",
            "config_sha256",
            "code_sha256",
            "dependency_inventory",
            "source_refs",
            "read_receipts",
            "stream_coverages",
            "coverage_gaps",
            "correlation_id",
            "component_sha256s",
            "bundle_sha256",
        }
        _exact_fields(raw, expected, "captured_viability_bundle")
        if raw.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            _contract("captured viability bundle schema is unsupported")
        raw_refs = raw.get("source_refs")
        raw_receipts = raw.get("read_receipts")
        raw_coverages = raw.get("stream_coverages")
        raw_gaps = raw.get("coverage_gaps")
        if not all(
            isinstance(value, list)
            for value in (raw_refs, raw_receipts, raw_coverages, raw_gaps)
        ):
            _contract("captured provenance arrays are malformed")
        refs: list[CaptureEventRef] = []
        for value in raw_refs:
            envelope = _mapping(value, "event_ref_envelope")
            _exact_fields(
                envelope,
                {"event_ref", "event_ref_sha256"},
                "event_ref_envelope",
            )
            ref_body = _mapping(envelope.get("event_ref"), "event_ref")
            if envelope.get("event_ref_sha256") != sha256_json(ref_body):
                _contract("event reference content hash mismatch")
            refs.append(_event_ref_from_dict(ref_body))
        receipts: list[CaptureReadReceipt] = []
        for value in raw_receipts:
            envelope = _mapping(value, "read_receipt_envelope")
            _exact_fields(
                envelope,
                {"receipt", "receipt_sha256"},
                "read_receipt_envelope",
            )
            receipt_body = _mapping(envelope.get("receipt"), "read_receipt")
            try:
                receipt = CaptureReadReceipt.from_dict(receipt_body)
            except (CaptureContractError, TypeError, ValueError) as exc:
                raise CapturedViabilityContractError("read receipt is invalid") from exc
            if envelope.get(
                "receipt_sha256"
            ) != captured_viability_read_receipt_sha256(receipt):
                _contract("read receipt content hash mismatch")
            receipts.append(receipt)
        try:
            coverages = tuple(
                StreamCoverage.from_dict(_mapping(value, "stream_coverage"))
                for value in raw_coverages
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedViabilityContractError("stream coverage is invalid") from exc
        row = cls(
            source_sequence=raw.get("source_sequence"),
            event_at=_parse_utc(raw.get("event_at"), "event_at"),
            available_at=_parse_utc(raw.get("available_at"), "available_at"),
            read_at=_parse_utc(raw.get("read_at"), "read_at"),
            symbol=str(raw.get("symbol") or ""),
            variant_id=raw.get("variant_id"),
            family=_family_from_dict(_mapping(raw.get("family"), "family")),
            context=_context_from_dict(_mapping(raw.get("context"), "context")),
            features=_features_from_dict(_mapping(raw.get("features"), "features")),
            settings=_projection_from_dict(_mapping(raw.get("settings"), "settings")),
            external=_external_from_dict(_mapping(raw.get("external"), "external")),
            post_score_adjustment=CapturedViabilityPostScoreAdjustment.from_dict(
                _mapping(raw.get("post_score_adjustment"), "post_score_adjustment")
            ),
            capture_identity_sha256=str(raw.get("capture_identity_sha256") or ""),
            policy_sha256=str(raw.get("policy_sha256") or ""),
            config_sha256=str(raw.get("config_sha256") or ""),
            code_sha256=str(raw.get("code_sha256") or ""),
            dependency_inventory=CapturedViabilityDependencyInventory.from_dict(
                _mapping(raw.get("dependency_inventory"), "dependency_inventory")
            ),
            source_refs=tuple(refs),
            read_receipts=tuple(receipts),
            stream_coverages=coverages,
            coverage_gaps=tuple(
                _gap_from_dict(_mapping(value, "coverage_gap"))
                for value in raw_gaps
            ),
            correlation_id=str(raw.get("correlation_id") or ""),
        )
        roots = _mapping(raw.get("component_sha256s"), "component_sha256s")
        if roots != row.component_roots:
            _contract("component root inventory mismatch")
        if raw.get("bundle_sha256") != row.bundle_sha256:
            _contract("captured viability bundle hash mismatch")
        if raw != row.to_dict():
            _contract("captured viability bundle encoding is not canonical")
        return row


@dataclass(frozen=True, slots=True)
class CapturedViabilityScoreResult:
    status: str
    reasons: tuple[str, ...]
    bundle_sha256: str | None
    authority_sha256: str | None
    viability: ViabilityResult | None = None
    observation: CapturedPaperSelectionObservation | None = None
    coverage_available: bool = field(init=False)
    opportunity_consumed: bool = False
    risk_reserved: bool = False
    order_posted: bool = False

    def __post_init__(self) -> None:
        if self.status not in {SCORED, COVERAGE_UNAVAILABLE}:
            _contract("score result status is invalid")
        reasons = tuple(dict.fromkeys(str(value or "").strip() for value in self.reasons))
        if any(not value for value in reasons):
            _contract("score result contains an empty reason")
        object.__setattr__(self, "reasons", reasons)
        if self.bundle_sha256 is not None:
            object.__setattr__(
                self, "bundle_sha256", _sha(self.bundle_sha256, "bundle_sha256")
            )
        if self.authority_sha256 is not None:
            object.__setattr__(
                self,
                "authority_sha256",
                _sha(self.authority_sha256, "authority_sha256"),
            )
        for name in ("opportunity_consumed", "risk_reserved", "order_posted"):
            if _strict_bool(getattr(self, name), name):
                _contract(f"{name} must remain false at the scoring boundary")
        scored = self.status == SCORED
        if scored != (
            type(self.viability) is ViabilityResult
            and type(self.observation) is CapturedPaperSelectionObservation
            and not reasons
        ):
            _contract("score result payload does not match status")
        if not scored and (self.viability is not None or self.observation is not None):
            _contract("coverage-unavailable result cannot contain a score")
        if not scored and not reasons:
            _contract("coverage-unavailable result must explain why coverage failed")
        object.__setattr__(self, "coverage_available", scored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "status": self.status,
            "reasons": list(self.reasons),
            "bundle_sha256": self.bundle_sha256,
            "authority_sha256": self.authority_sha256,
            "coverage_available": self.coverage_available,
            "opportunity_consumed": self.opportunity_consumed,
            "risk_reserved": self.risk_reserved,
            "order_posted": self.order_posted,
            "viability": (
                _viability_result_body(self.viability)
                if self.viability is not None
                else None
            ),
            "observation": (
                {
                    **self.observation.body(),
                    "observation_sha256": self.observation.observation_sha256,
                }
                if self.observation is not None
                else None
            ),
        }


def _unavailable(
    reason: str | Sequence[str],
    *,
    bundle: CapturedViabilityInputBundle | None = None,
    authority: CapturedViabilityScoringAuthority | None = None,
) -> CapturedViabilityScoreResult:
    reasons = (reason,) if isinstance(reason, str) else tuple(reason)
    return CapturedViabilityScoreResult(
        status=COVERAGE_UNAVAILABLE,
        reasons=tuple(dict.fromkeys(reasons)),
        bundle_sha256=bundle.bundle_sha256 if bundle is not None else None,
        authority_sha256=(
            authority.authority_sha256 if authority is not None else None
        ),
    )


def _viability_result_body(result: ViabilityResult) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "family_id": result.family_id,
        "family_version": result.family_version,
        "viability": result.viability,
        "paper_eligible": result.paper_eligible,
        "live_eligible": result.live_eligible,
        "freshness_hint": result.freshness_hint,
        "regime_fit": result.regime_fit,
        "rationale": result.rationale,
        "warnings": list(result.warnings),
        "extreme_vol_risk_bounded": result.extreme_vol_risk_bounded,
    }


def _current_component_roots(bundle: CapturedViabilityInputBundle) -> dict[str, str]:
    return captured_viability_component_sha256s(
        symbol=bundle.symbol,
        variant_id=bundle.variant_id,
        family=bundle.family,
        context=bundle.context,
        features=bundle.features,
        settings=bundle.settings,
        external=bundle.external,
        post_score_adjustment=bundle.post_score_adjustment,
        event_at=bundle.event_at,
        available_at=bundle.available_at,
        read_at=bundle.read_at,
        capture_identity_sha256=bundle.capture_identity_sha256,
        policy_sha256=bundle.policy_sha256,
        config_sha256=bundle.config_sha256,
        code_sha256=bundle.code_sha256,
    )


def _coverage_reasons(
    bundle: CapturedViabilityInputBundle,
    authority: CapturedViabilityScoringAuthority,
    evaluation_at: datetime,
) -> tuple[str, ...]:
    reasons: list[str] = []

    def reason(value: str) -> None:
        if value not in reasons:
            reasons.append(value)

    try:
        if sha256_json(bundle.body()) != bundle.bundle_sha256:
            reason("bundle_hash_mismatch")
        if sha256_json(authority.body()) != authority.authority_sha256:
            reason("authority_hash_mismatch")
        roots = _current_component_roots(bundle)
    except (CapturedViabilityContractError, CaptureContractError, TypeError, ValueError):
        return ("bundle_content_invalid",)
    if roots != bundle.component_roots:
        reason("component_root_mismatch")

    profile = bundle.dependency_inventory.dependency_profile
    if (
        sha256_json(bundle.dependency_inventory.body())
        != bundle.dependency_inventory.inventory_sha256
    ):
        reason("dependency_inventory_hash_mismatch")
    if profile.profile_sha256 != authority.dependency_profile_sha256:
        reason("dependency_profile_authority_mismatch")
    expected_authority = {
        "capture_identity_sha256": bundle.capture_identity_sha256,
        "policy_sha256": bundle.policy_sha256,
        "config_sha256": bundle.config_sha256,
        "code_sha256": bundle.code_sha256,
        "settings_projection_sha256": roots.get("settings_projection"),
        "family_sha256": roots.get("family"),
        "variant_id": bundle.variant_id,
        "family_id": bundle.family.family_id,
        "family_version": bundle.family.version,
    }
    for name, value in expected_authority.items():
        if getattr(authority, name) != value:
            reason(f"authority_mismatch:{name}")
    if (
        authority.executable_eligibility_basis != EXECUTABLE_ELIGIBILITY_BASIS
        or authority.paper_only_strategy_override
        or authority.live_cash_authorized
        or authority.real_money_authorized
    ):
        reason("executable_policy_parity_mismatch")

    bindings = {item.component: item for item in bundle.dependency_inventory.bindings}
    if set(bindings) != set(REQUIRED_COMPONENTS):
        reason("dependency_component_set_mismatch")
    for component in REQUIRED_COMPONENTS:
        row = bindings.get(component)
        if row is None or row.component_sha256 != roots.get(component):
            reason(f"dependency_component_hash_mismatch:{component}")
        elif sha256_json(row.body()) != row.binding_sha256:
            reason(f"dependency_binding_hash_mismatch:{component}")

    adjustment = bundle.post_score_adjustment
    is_crypto = bundle.symbol.endswith("-USD")
    if adjustment.lookup_status == "inapplicable_non_crypto" and is_crypto:
        reason("post_score_inapplicable_symbol_mismatch")
    if (
        adjustment.tenbeat_entry_tilt_weight > 0
        and not is_crypto
        and adjustment.lookup_status != "inapplicable_non_crypto"
    ):
        reason("post_score_non_crypto_lookup_mismatch")
    if (
        adjustment.tenbeat_entry_tilt_weight > 0
        and is_crypto
        and adjustment.lookup_status
        not in {"captured_value", "captured_empty"}
    ):
        reason("post_score_capture_receipt_missing")

    refs_by_hash = {item.event_sha256: item for item in bundle.source_refs}
    receipt_by_hash = {
        captured_viability_read_receipt_sha256(item): item
        for item in bundle.read_receipts
    }
    bound_events = {
        value
        for row in bundle.dependency_inventory.bindings
        for value in row.source_event_sha256s
    }
    bound_receipts = {
        value
        for row in bundle.dependency_inventory.bindings
        for value in row.read_receipt_sha256s
    }
    if bound_events != set(refs_by_hash):
        reason("dependency_source_event_set_mismatch")
    if bound_receipts != set(receipt_by_hash):
        reason("dependency_read_receipt_set_mismatch")
    if adjustment.source_read_id is not None:
        adjustment_receipt = next(
            (
                item
                for item in bundle.read_receipts
                if item.read_id == adjustment.source_read_id
            ),
            None,
        )
        if adjustment_receipt is None:
            reason("post_score_read_receipt_missing")
        else:
            adjustment_binding = bindings.get("post_score_adjustment")
            adjustment_receipt_sha = captured_viability_read_receipt_sha256(
                adjustment_receipt
            )
            if (
                adjustment_binding is None
                or adjustment_receipt_sha
                not in adjustment_binding.read_receipt_sha256s
            ):
                reason("post_score_read_receipt_unbound")
            if (
                adjustment.lookup_status == "captured_value"
                and adjustment_receipt.empty_result
            ):
                reason("post_score_value_receipt_empty")
            if (
                adjustment.lookup_status == "captured_empty"
                and not adjustment_receipt.empty_result
            ):
                reason("post_score_empty_receipt_nonempty")

    required_streams = set(profile.required_streams)
    for identity_stream in (
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.CODE_BUILD,
    ):
        if identity_stream not in required_streams:
            reason(f"required_identity_stream_missing:{identity_stream.value}")
    coverage_by_stream = {item.stream: item for item in bundle.stream_coverages}
    if set(coverage_by_stream) != required_streams:
        reason("stream_coverage_set_mismatch")
    if {item.read_id for item in bundle.read_receipts} != set(
        profile.required_read_ids
    ):
        reason("required_read_set_mismatch")

    for ref in bundle.source_refs:
        if ref.identity_sha256 != bundle.capture_identity_sha256:
            reason(f"event_identity_mismatch:{ref.event_sha256}")
        if ref.stream not in required_streams:
            reason(f"extra_event_stream:{ref.stream.value}")
        if ref.symbol not in (None, bundle.symbol):
            reason(f"event_symbol_mismatch:{ref.event_sha256}")
        if ref.available_at > bundle.available_at:
            reason(f"event_available_after_bundle:{ref.event_sha256}")
        if ref.received_at > bundle.read_at:
            reason(f"event_received_after_read:{ref.event_sha256}")
        if ref.provider_event_at is not None and ref.provider_event_at > bundle.event_at:
            reason(f"event_clock_after_frontier:{ref.event_sha256}")
        if (
            ref.market_reference_at is not None
            and ref.market_reference_at > bundle.event_at
        ):
            reason(f"reference_clock_after_frontier:{ref.event_sha256}")

    for receipt in bundle.read_receipts:
        receipt_hash = captured_viability_read_receipt_sha256(receipt)
        if receipt.identity_sha256 != bundle.capture_identity_sha256:
            reason(f"receipt_identity_mismatch:{receipt_hash}")
        if receipt.stream not in required_streams:
            reason(f"extra_receipt_stream:{receipt.stream.value}")
        if receipt.symbol not in (None, bundle.symbol):
            reason(f"receipt_symbol_mismatch:{receipt_hash}")
        if not receipt.content_verified:
            reason(f"receipt_content_unverified:{receipt_hash}")
        if receipt.replay_network_fallback_used:
            reason(f"receipt_network_fallback:{receipt_hash}")
        if receipt.query is None:
            reason(f"receipt_query_payload_missing:{receipt_hash}")
        if receipt.returned_at > bundle.available_at:
            reason(f"receipt_available_after_bundle:{receipt_hash}")
        if len(receipt.source_event_sha256s) != len(
            set(receipt.source_event_sha256s)
        ):
            reason(f"receipt_duplicate_source:{receipt_hash}")
        returned_refs: list[CaptureEventRef] = []
        for event_hash in receipt.source_event_sha256s:
            ref = refs_by_hash.get(event_hash)
            if ref is None:
                reason(f"receipt_source_missing:{receipt_hash}")
                continue
            returned_refs.append(ref)
            if ref.stream is not receipt.stream or ref.provider != receipt.provider:
                reason(f"receipt_source_route_mismatch:{receipt_hash}")
            if ref.query_sha256 != receipt.query_sha256:
                reason(f"receipt_query_mismatch:{receipt_hash}")
        if len(returned_refs) == len(receipt.source_event_sha256s):
            if captured_read_result_sha256(tuple(returned_refs)) != receipt.result_sha256:
                reason(f"receipt_result_hash_mismatch:{receipt_hash}")

    for stream in sorted(required_streams, key=lambda item: item.value):
        coverage = coverage_by_stream.get(stream)
        if coverage is None:
            continue
        dependency = profile.dependency_for(stream)
        stream_refs = [item for item in bundle.source_refs if item.stream is stream]
        stream_receipts = [
            item for item in bundle.read_receipts if item.stream is stream
        ]
        if coverage.identity_sha256 != bundle.capture_identity_sha256:
            reason(f"coverage_identity_mismatch:{stream.value}")
        if coverage.symbol not in (None, bundle.symbol):
            reason(f"coverage_symbol_mismatch:{stream.value}")
        if any(item.provider != coverage.provider for item in stream_refs):
            reason(f"coverage_event_provider_mismatch:{stream.value}")
        if any(item.provider != coverage.provider for item in stream_receipts):
            reason(f"coverage_receipt_provider_mismatch:{stream.value}")
        if coverage.first_available_at > dependency.coverage_start_at:
            reason(f"warmup_coverage_missing:{stream.value}")
        if coverage.last_available_at > bundle.read_at:
            reason(f"coverage_from_future:{stream.value}")
        if coverage.last_available_at > bundle.available_at:
            reason(f"coverage_after_bundle_available:{stream.value}")
        if coverage.event_count != len(stream_refs):
            reason(f"coverage_event_count_mismatch:{stream.value}")
        if any(
            item.available_at < coverage.first_available_at
            for item in stream_refs
        ) or any(
            item.returned_at < coverage.first_available_at
            for item in stream_receipts
        ):
            reason(f"stream_evidence_before_coverage:{stream.value}")
        if not coverage.content_verified:
            reason(f"coverage_content_unverified:{stream.value}")
        if not coverage.continuity_complete:
            reason(f"coverage_continuity_unproven:{stream.value}")
        if dependency.exact_provider_event_at_required:
            if not coverage.exact_event_clock_complete or any(
                item.provider_event_at is None for item in stream_refs
            ):
                reason(f"exact_event_clock_missing:{stream.value}")
        if dependency.market_reference_at_required and any(
            item.market_reference_at is None for item in stream_refs
        ):
            reason(f"market_reference_clock_missing:{stream.value}")

        latest_candidates = [item.available_at for item in stream_refs]
        latest_candidates.extend(item.returned_at for item in stream_receipts)
        if coverage.watermark is not None:
            latest_candidates.append(coverage.watermark.emitted_available_at)
        if not latest_candidates:
            reason(f"stream_evidence_missing:{stream.value}")
        else:
            latest = max(latest_candidates)
            if coverage.last_available_at < latest:
                reason(f"coverage_frontier_before_evidence:{stream.value}")
            age = (evaluation_at - latest).total_seconds()
            if age < 0:
                reason(f"stream_evidence_from_future:{stream.value}")
            elif age > dependency.max_source_age_seconds:
                reason(f"stream_evidence_stale:{stream.value}")

        policy = STREAM_POLICIES[stream]
        if policy.coverage_mode in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}:
            watermark = coverage.watermark
            if watermark is None:
                reason(f"provider_watermark_missing:{stream.value}")
            elif (
                watermark.identity_sha256 != bundle.capture_identity_sha256
                or watermark.provider != coverage.provider
                or watermark.symbol != coverage.symbol
            ):
                reason(f"provider_watermark_mismatch:{stream.value}")
            else:
                if watermark.event_watermark_at < bundle.event_at:
                    reason(f"provider_watermark_before_event:{stream.value}")
                if watermark.emitted_available_at > bundle.read_at:
                    reason(f"provider_watermark_after_read:{stream.value}")
                if watermark.emitted_available_at > bundle.available_at:
                    reason(
                        f"provider_watermark_after_bundle_available:{stream.value}"
                    )
        elif policy.coverage_mode is CoverageMode.QUERY_RECEIPT:
            if not stream_receipts:
                reason(f"query_receipt_missing:{stream.value}")
            returned_event_hashes = {
                event_sha256
                for receipt in stream_receipts
                for event_sha256 in receipt.source_event_sha256s
            }
            if returned_event_hashes != {
                ref.event_sha256 for ref in stream_refs
            }:
                reason(f"query_event_set_mismatch:{stream.value}")
            if coverage.query_receipt_count != len(stream_receipts):
                reason(f"query_receipt_count_mismatch:{stream.value}")
        elif policy.coverage_mode is CoverageMode.IDENTITY:
            expected_payload = {
                CaptureStream.CONFIG_SNAPSHOT: bundle.config_sha256,
                CaptureStream.FEATURE_FLAG_SNAPSHOT: bundle.policy_sha256,
                CaptureStream.CODE_BUILD: bundle.code_sha256,
            }.get(stream)
            if not stream_refs:
                reason(f"identity_event_missing:{stream.value}")
            if expected_payload is not None and not any(
                item.payload_sha256 == expected_payload for item in stream_refs
            ):
                reason(f"identity_payload_mismatch:{stream.value}")

    for gap in bundle.coverage_gaps:
        if gap.stream not in required_streams:
            reason(f"extra_gap_stream:{gap.stream.value}")
            continue
        if gap.symbol not in (None, bundle.symbol):
            reason(f"gap_symbol_mismatch:{gap.stream.value}")
            continue
        dependency = profile.dependency_for(gap.stream)
        if gap.intersects(dependency.coverage_start_at, bundle.available_at):
            reason(f"coverage_gap:{gap.stream.value}:{gap.reason}:{gap.lost_count}")

    return tuple(reasons)


def _selection_observation(
    bundle: CapturedViabilityInputBundle,
    authority: CapturedViabilityScoringAuthority,
    result: ViabilityResult,
) -> CapturedPaperSelectionObservation:
    result_body = _viability_result_body(result)
    persisted_viability = bundle.post_score_adjustment.persisted_viability(result)
    policy_parity = {
        "executable_eligibility_basis": authority.executable_eligibility_basis,
        "legacy_scorer_paper_eligible": result.paper_eligible,
        "intended_live_eligible": result.live_eligible,
        "executable_paper_eligible": result.live_eligible,
        "executable_live_eligible": result.live_eligible,
        "paper_only_strategy_override": authority.paper_only_strategy_override,
        "live_cash_authorized": authority.live_cash_authorized,
        "real_money_authorized": authority.real_money_authorized,
        "policy_parity": True,
        "post_score_adjustment_sha256": (
            bundle.post_score_adjustment.adjustment_sha256
        ),
        "persisted_viability": persisted_viability,
    }
    regime = {
        "schema_version": REGIME_SNAPSHOT_SCHEMA_VERSION,
        "context": _context_body(bundle.context),
        "context_sha256": bundle.component_roots["regime_context"],
        "bundle_sha256": bundle.bundle_sha256,
    }
    readiness = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "features": _features_body(bundle.features),
        "features_sha256": bundle.component_roots["execution_readiness"],
        "external_inputs": _external_body(bundle.external),
        "external_inputs_sha256": bundle.component_roots["external_inputs"],
        "bundle_sha256": bundle.bundle_sha256,
    }
    explain = {
        "schema_version": EXPLAIN_SCHEMA_VERSION,
        "scorer_output": result_body,
        "family": _family_body(bundle.family),
        "family_sha256": bundle.component_roots["family"],
        "settings_projection": _settings_body(bundle.settings),
        "settings_projection_sha256": bundle.settings_projection_sha256,
        "post_score_adjustment": bundle.post_score_adjustment.to_dict(),
        "policy_parity": policy_parity,
        "authority_sha256": authority.authority_sha256,
        "bundle_sha256": bundle.bundle_sha256,
    }
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "capture_identity_sha256": bundle.capture_identity_sha256,
        "policy_sha256": bundle.policy_sha256,
        "config_sha256": bundle.config_sha256,
        "code_sha256": bundle.code_sha256,
        "event_at": _iso(bundle.event_at),
        "available_at": _iso(bundle.available_at),
        "read_at": _iso(bundle.read_at),
        "component_sha256s": bundle.component_roots,
        "post_score_adjustment": bundle.post_score_adjustment.to_dict(),
        "dependency_inventory": bundle.dependency_inventory.to_dict(),
        "source_refs": [_event_ref_envelope(item) for item in bundle.source_refs],
        "read_receipts": [
            _receipt_envelope(item) for item in bundle.read_receipts
        ],
        "stream_coverages": [item.to_dict() for item in bundle.stream_coverages],
        "coverage_gaps": [item.to_dict() for item in bundle.coverage_gaps],
        "policy_parity": policy_parity,
        "bundle_sha256": bundle.bundle_sha256,
    }
    return CapturedPaperSelectionObservation(
        source_sequence=bundle.source_sequence,
        source_event_at=bundle.event_at,
        source_available_at=bundle.available_at,
        symbol=bundle.symbol,
        variant_id=bundle.variant_id,
        viability_score=persisted_viability,
        # The legacy ``paper_eligible`` field was a permissive diagnostic lane.
        # Captured PAPER deliberately executes the exact intended-live decision
        # on both sides; the legacy value is still preserved losslessly above.
        paper_eligible=result.live_eligible,
        live_eligible=result.live_eligible,
        regime_snapshot_json=regime,
        execution_readiness_json=readiness,
        explain_json=explain,
        evidence_window_json=evidence,
        correlation_id=bundle.correlation_id,
    )


def score_captured_viability(
    bundle: CapturedViabilityInputBundle | Mapping[str, Any],
    *,
    authority: CapturedViabilityScoringAuthority | Mapping[str, Any],
    evaluation_at: datetime,
) -> CapturedViabilityScoreResult:
    """Score one sealed input with no fallback to current external state."""

    typed_bundle: CapturedViabilityInputBundle | None = None
    typed_authority: CapturedViabilityScoringAuthority | None = None
    try:
        typed_bundle = (
            bundle
            if type(bundle) is CapturedViabilityInputBundle
            else CapturedViabilityInputBundle.from_dict(
                _mapping(bundle, "captured_viability_bundle")
            )
        )
    except (CapturedViabilityContractError, CaptureContractError, TypeError, ValueError):
        return _unavailable("bundle_contract_invalid")
    try:
        typed_authority = (
            authority
            if type(authority) is CapturedViabilityScoringAuthority
            else CapturedViabilityScoringAuthority.from_dict(
                _mapping(authority, "scoring_authority")
            )
        )
    except (CapturedViabilityContractError, CaptureContractError, TypeError, ValueError):
        return _unavailable("authority_contract_invalid", bundle=typed_bundle)
    try:
        at = _utc(evaluation_at, "evaluation_at")
    except CapturedViabilityContractError:
        return _unavailable(
            "evaluation_clock_invalid",
            bundle=typed_bundle,
            authority=typed_authority,
        )
    if at != typed_bundle.read_at:
        return _unavailable(
            "evaluation_clock_not_bound_to_bundle_read",
            bundle=typed_bundle,
            authority=typed_authority,
        )
    reasons = _coverage_reasons(typed_bundle, typed_authority, at)
    if reasons:
        return _unavailable(
            reasons,
            bundle=typed_bundle,
            authority=typed_authority,
        )
    try:
        viability = score_viability_explicit(
            typed_bundle.symbol,
            typed_bundle.family,
            typed_bundle.context,
            typed_bundle.features,
            settings=typed_bundle.settings,
            external=typed_bundle.external,
        )
        if (
            viability.symbol != typed_bundle.symbol
            or viability.family_id != typed_bundle.family.family_id
            or viability.family_version != typed_bundle.family.version
        ):
            return _unavailable(
                "scorer_output_identity_mismatch",
                bundle=typed_bundle,
                authority=typed_authority,
            )
        observation = _selection_observation(
            typed_bundle, typed_authority, viability
        )
    except Exception:
        return _unavailable(
            "scorer_failed_closed",
            bundle=typed_bundle,
            authority=typed_authority,
        )
    return CapturedViabilityScoreResult(
        status=SCORED,
        reasons=(),
        bundle_sha256=typed_bundle.bundle_sha256,
        authority_sha256=typed_authority.authority_sha256,
        viability=viability,
        observation=observation,
    )


__all__ = [
    "AUTHORITY_SCHEMA_VERSION",
    "BUNDLE_SCHEMA_VERSION",
    "COVERAGE_UNAVAILABLE",
    "EXECUTABLE_ELIGIBILITY_BASIS",
    "REQUIRED_COMPONENTS",
    "SCORED",
    "CapturedViabilityContractError",
    "CapturedViabilityDependencyBinding",
    "CapturedViabilityDependencyInventory",
    "CapturedViabilityInputBundle",
    "CapturedViabilityPostScoreAdjustment",
    "CapturedViabilityScoreResult",
    "CapturedViabilityScoringAuthority",
    "captured_viability_component_sha256s",
    "captured_viability_read_receipt_sha256",
    "score_captured_viability",
]
