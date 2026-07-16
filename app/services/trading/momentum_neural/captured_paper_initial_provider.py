"""Pure capture-backed material provider for the first Alpaca PAPER session.

The controller owns every mutable step before this boundary: it resolves the
strict IQFeed trigger, retains the exact process-private ``CapturedReadResult``,
and asks the live capture runtime to issue an ``ActiveCaptureInputPrefixAttestation``.
This module only verifies those already-issued objects and an injected
read-only strategy/viability snapshot.  It has no database, provider, broker,
opportunity, reservation, outbox, or order capability.

Missing, stale, ambiguous, or mismatched evidence is decision-local
``COVERAGE_UNAVAILABLE``.  The provider never converts an opaque digest into
authority: the trigger read, attestation inventory, complete considered set,
selected rows, capture configuration, feature flags, and shared adaptive
policy are all revalidated and content-addressed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import math
import re
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
import uuid

from .adaptive_risk_policy import AdaptiveRiskPolicySettingsReceipt
from .captured_adaptive_risk_source import CapturedAdaptiveRiskPolicySpec
from .captured_paper_initial_admission import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    ALPACA_SPOT_EXECUTION_FAMILY,
    INITIAL_READ_INVENTORY_SCHEMA_VERSION,
    CapturedPaperInitialRunnerRiskTemplate,
    CapturedPaperInitialSessionMaterial,
    captured_paper_initial_variant_sha256,
    captured_paper_initial_viability_sha256,
)
from .captured_paper_iqfeed_trigger import (
    CapturedPaperIqfeedTriggerReceipt,
    IqfeedTriggerResolution,
    IqfeedTriggerStatus,
)
from .live_replay_capture import (
    CaptureIdentityEvidence,
    CaptureSessionState,
    CapturedReadResult,
)
from .replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureEventRef,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureRunIdentity,
    CaptureStream,
    canonical_json_bytes,
    sha256_json,
    verify_active_capture_input_attestation,
)


UTC = timezone.utc
INITIAL_PROVIDER_CANDIDATE_READ_SCHEMA_VERSION = (
    "chili.captured-paper-initial-candidate-read.v1"
)
INITIAL_PROVIDER_CONSIDERED_SET_SCHEMA_VERSION = (
    "chili.captured-paper-initial-considered-set.v1"
)
INITIAL_PROVIDER_SELECTION_SCHEMA_VERSION = (
    "chili.captured-paper-initial-selection.v1"
)
INITIAL_PROVIDER_CAPTURE_CHECKPOINT_SCHEMA_VERSION = (
    "chili.captured-paper-initial-capture-checkpoint.v1"
)
INITIAL_PROVIDER_STATUS_COVERAGE_UNAVAILABLE = "COVERAGE_UNAVAILABLE"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,35}")
_SELECTION_ALGORITHM = (
    "viability_score_desc/readiness_freshness_desc/variant_id_asc/viability_id_asc"
)


class CapturedPaperInitialProviderCoverageUnavailable(CaptureContractError):
    """Typed, local rejection which grants no session or execution authority."""

    status = INITIAL_PROVIDER_STATUS_COVERAGE_UNAVAILABLE

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "initial_provider_coverage_unavailable")
        super().__init__(self.reason)


def _unavailable(reason: str) -> None:
    raise CapturedPaperInitialProviderCoverageUnavailable(reason)


def _positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        _unavailable(f"{field_name}_invalid")
    return value


def _sha(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _unavailable(f"{field_name}_invalid")
    return value


def _symbol(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _SYMBOL_RE.fullmatch(value) is None
        or value.endswith(".")
        or ".." in value
    ):
        _unavailable("initial_provider_symbol_invalid")
    return value


def _uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _unavailable(f"{field_name}_invalid")
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        _unavailable(f"{field_name}_invalid")
    if normalized != value:
        _unavailable(f"{field_name}_invalid")
    return normalized


def _utc(value: Any, field_name: str, *, allow_naive: bool = False) -> datetime:
    if not isinstance(value, datetime):
        _unavailable(f"{field_name}_clock_unavailable")
    if value.tzinfo is None:
        if not allow_naive:
            _unavailable(f"{field_name}_clock_unavailable")
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        _unavailable(f"{field_name}_invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        _unavailable(f"{field_name}_invalid")
    if not math.isfinite(normalized):
        _unavailable(f"{field_name}_invalid")
    return normalized


def _canonical_value(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _unavailable("initial_provider_nonfinite_json")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso(_utc(value, "initial_provider_json", allow_naive=True))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            _unavailable("initial_provider_json_key_invalid")
        return {
            key: _canonical_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    _unavailable("initial_provider_json_value_invalid")


def _canonical_json(value: Any) -> str:
    try:
        return canonical_json_bytes(_canonical_value(value)).decode("utf-8")
    except (CaptureContractError, TypeError, ValueError, UnicodeError) as exc:
        raise CapturedPaperInitialProviderCoverageUnavailable(
            "initial_provider_canonical_json_unavailable"
        ) from exc


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialCandidateRow:
    """One detached strategy/viability pair returned by the injected reader."""

    variant: Any
    viability: Any


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialCandidateRead:
    """Exact user/symbol-scoped read result; it carries rows, never authority."""

    user_id: int
    symbol: str
    read_at: datetime
    rows: tuple[CapturedPaperInitialCandidateRow, ...]
    schema_version: str = INITIAL_PROVIDER_CANDIDATE_READ_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INITIAL_PROVIDER_CANDIDATE_READ_SCHEMA_VERSION:
            _unavailable("initial_candidate_read_schema_invalid")
        object.__setattr__(
            self, "user_id", _positive_int(self.user_id, "initial_candidate_user_id")
        )
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(
            self,
            "read_at",
            _utc(self.read_at, "initial_candidate_read_at"),
        )
        try:
            rows = tuple(self.rows)
        except TypeError as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_candidate_rows_invalid"
            ) from exc
        if any(type(row) is not CapturedPaperInitialCandidateRow for row in rows):
            _unavailable("initial_candidate_rows_invalid")
        object.__setattr__(self, "rows", rows)


@runtime_checkable
class CapturedPaperInitialCandidateReadPort(Protocol):
    """Only injected data seam; implementations may read but must not mutate."""

    @property
    def network_fallback_allowed(self) -> bool: ...

    @property
    def mutation_allowed(self) -> bool: ...

    def read_candidates(
        self,
        *,
        user_id: int,
        symbol: str,
        decision_at: datetime,
    ) -> CapturedPaperInitialCandidateRead: ...


def _variant_body(row: Any) -> dict[str, Any]:
    try:
        body = {
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
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperInitialProviderCoverageUnavailable(
            "initial_candidate_variant_unavailable"
        ) from exc
    if body["id"] <= 0 or not body["family"] or not body["variant_key"]:
        _unavailable("initial_candidate_variant_unavailable")
    canonical = _canonical_value(body)
    try:
        helper_sha256 = captured_paper_initial_variant_sha256(row)
    except Exception as exc:
        raise CapturedPaperInitialProviderCoverageUnavailable(
            "initial_candidate_variant_unavailable"
        ) from exc
    if _hash_json(canonical) != helper_sha256:
        _unavailable("initial_candidate_variant_snapshot_mismatch")
    return canonical


def _viability_body(row: Any) -> dict[str, Any]:
    score = _finite(
        getattr(row, "viability_score", None),
        "initial_candidate_viability_score",
    )
    try:
        body = {
            "schema_version": "chili.captured-paper-initial-viability.v1",
            "id": int(getattr(row, "id", 0) or 0),
            "symbol": str(getattr(row, "symbol", "") or ""),
            "scope": str(getattr(row, "scope", "") or ""),
            "variant_id": int(getattr(row, "variant_id", 0) or 0),
            "viability_score": score,
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
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperInitialProviderCoverageUnavailable(
            "initial_candidate_viability_unavailable"
        ) from exc
    if body["id"] <= 0 or body["variant_id"] <= 0 or not body["symbol"]:
        _unavailable("initial_candidate_viability_unavailable")
    canonical = _canonical_value(body)
    try:
        helper_sha256 = captured_paper_initial_viability_sha256(row)
    except Exception as exc:
        raise CapturedPaperInitialProviderCoverageUnavailable(
            "initial_candidate_viability_unavailable"
        ) from exc
    if _hash_json(canonical) != helper_sha256:
        _unavailable("initial_candidate_viability_snapshot_mismatch")
    return canonical


def _candidate_snapshot(
    row: CapturedPaperInitialCandidateRow,
    *,
    symbol: str,
    decision_at: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    variant = _variant_body(row.variant)
    viability = _viability_body(row.viability)
    if viability["symbol"] != symbol or viability["scope"] != "symbol":
        _unavailable("initial_candidate_scope_mismatch")
    if viability["variant_id"] != variant["id"]:
        _unavailable("initial_candidate_variant_pair_mismatch")
    freshness = _utc(
        getattr(row.viability, "freshness_ts", None),
        "initial_candidate_freshness",
        allow_naive=True,
    )
    age_seconds = (decision_at - freshness).total_seconds()
    reasons: list[str] = []
    if age_seconds < 0.0:
        reasons.append("viability_from_future")
    elif age_seconds > max_age_seconds:
        reasons.append("viability_stale")
    if not variant["is_active"]:
        reasons.append("variant_inactive")
    if variant["execution_family"] != ALPACA_SPOT_EXECUTION_FAMILY:
        reasons.append("execution_family_mismatch")
    if not viability["paper_eligible"]:
        reasons.append("paper_ineligible")
    if not viability["live_eligible"]:
        reasons.append("live_policy_ineligible")
    readiness = viability["execution_readiness_json"]
    if type(readiness) is not dict or not readiness:
        reasons.append("execution_readiness_unavailable")
    variant_sha256 = _hash_json(variant)
    viability_sha256 = _hash_json(viability)
    return {
        "variant": variant,
        "variant_sha256": variant_sha256,
        "viability": viability,
        "viability_sha256": viability_sha256,
        "readiness_freshness_at": _iso(freshness),
        "readiness_sha256": _hash_json(readiness),
        "eligible": not reasons,
        "rejection_reasons": sorted(reasons),
    }


def _selection_rank(candidate: Mapping[str, Any]) -> tuple[float, float, int, int]:
    viability = candidate["viability"]
    freshness = datetime.fromisoformat(
        str(candidate["readiness_freshness_at"]).replace("Z", "+00:00")
    )
    return (
        -float(viability["viability_score"]),
        -freshness.timestamp(),
        int(candidate["variant"]["id"]),
        int(viability["id"]),
    )


def _capture_checkpoint_body(
    proof: ActiveCaptureInputPrefixAttestation,
) -> dict[str, Any]:
    return {
        "schema_version": INITIAL_PROVIDER_CAPTURE_CHECKPOINT_SCHEMA_VERSION,
        "attestation_sha256": proof.attestation_sha256,
        "run_id": proof.run_id,
        "generation": proof.generation,
        "decision_id": proof.decision_id,
        "identity_sha256": proof.identity_sha256,
        "input_prefix_sequence": proof.input_prefix_sequence,
        "input_prefix_root_sha256": proof.input_prefix_root_sha256,
        "attested_available_at": _iso(proof.attested_available_at),
        "expires_at": _iso(proof.expires_at),
        "dependency_profile": proof.dependency_profile.to_dict(),
        "dependency_profile_sha256": proof.dependency_profile.profile_sha256,
        "account_identity_sha256": proof.account_identity_sha256,
        "code_build_sha256": proof.code_build_sha256,
        "config_sha256": proof.config_sha256,
        "feature_flags_sha256": proof.feature_flags_sha256,
        "resource_binding_sha256": proof.resource_binding_sha256,
        "producer_generations": dict(proof.producer_generations),
        "required_read_ids": list(proof.required_read_ids),
        "read_evidence": [
            evidence.to_evidence_dict() for evidence in proof.read_evidence
        ],
        "read_evidence_inventory_sha256": (
            proof.read_evidence_inventory_sha256
        ),
        "continuity_evidence": [
            evidence.to_evidence_dict() for evidence in proof.continuity_evidence
        ],
        "continuity_evidence_inventory_sha256": (
            proof.continuity_evidence_inventory_sha256
        ),
    }


class CaptureBackedPaperInitialSessionMaterialProvider:
    """Verify one already-issued capture authority and build immutable material."""

    def __init__(
        self,
        *,
        user_id: int,
        account_scope: str,
        expected_account_id: str,
        runtime_generation: str,
        code_build_sha256: str,
        capture_receipt_sha256: str,
        trigger_resolution: IqfeedTriggerResolution,
        active_input_attestation: ActiveCaptureInputPrefixAttestation,
        capture_coordinator: Any,
        capture_identity_evidence: CaptureIdentityEvidence,
        capture_config_sha256_resolver: Callable[[str], str],
        candidate_reader: CapturedPaperInitialCandidateReadPort,
        adaptive_policy_settings_receipt: AdaptiveRiskPolicySettingsReceipt,
        adaptive_policy_spec: CapturedAdaptiveRiskPolicySpec,
        material_ttl_seconds: float,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self.user_id = _positive_int(user_id, "initial_provider_user_id")
        if account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            _unavailable("initial_provider_account_scope_invalid")
        self.account_scope = account_scope
        self.expected_account_id = _uuid(
            expected_account_id,
            "initial_provider_expected_account_id",
        )
        self.runtime_generation = _uuid(
            runtime_generation,
            "initial_provider_runtime_generation",
        )
        self.code_build_sha256 = _sha(
            code_build_sha256,
            "initial_provider_code_build_sha256",
        )
        self.capture_receipt_sha256 = _sha(
            capture_receipt_sha256,
            "initial_provider_capture_receipt_sha256",
        )
        if type(trigger_resolution) is not IqfeedTriggerResolution:
            _unavailable("initial_provider_trigger_resolution_invalid")
        if type(active_input_attestation) is not ActiveCaptureInputPrefixAttestation:
            _unavailable("initial_provider_input_attestation_invalid")
        if not callable(capture_config_sha256_resolver):
            _unavailable("initial_provider_capture_config_resolver_invalid")
        if not isinstance(candidate_reader, CapturedPaperInitialCandidateReadPort):
            _unavailable("initial_provider_candidate_reader_invalid")
        if not callable(wall_clock):
            _unavailable("initial_provider_clock_invalid")
        if type(adaptive_policy_settings_receipt) is not AdaptiveRiskPolicySettingsReceipt:
            _unavailable("initial_provider_policy_receipt_invalid")
        if type(adaptive_policy_spec) is not CapturedAdaptiveRiskPolicySpec:
            _unavailable("initial_provider_policy_spec_invalid")
        if type(capture_identity_evidence) is not CaptureIdentityEvidence:
            _unavailable("initial_provider_capture_identity_evidence_invalid")
        ttl = _finite(material_ttl_seconds, "initial_provider_ttl_seconds")
        if ttl <= 0.0:
            _unavailable("initial_provider_ttl_seconds_invalid")
        max_context_age = float(
            adaptive_policy_settings_receipt.policy.context_data_max_age_seconds
        )
        if ttl > max_context_age:
            _unavailable("initial_provider_ttl_exceeds_policy_context_age")
        self.trigger_resolution = trigger_resolution
        self.active_input_attestation = active_input_attestation
        self.capture_coordinator = capture_coordinator
        self.capture_identity_evidence = capture_identity_evidence
        self.capture_config_sha256_resolver = capture_config_sha256_resolver
        self.candidate_reader = candidate_reader
        self.policy_receipt = adaptive_policy_settings_receipt
        self.policy_spec = adaptive_policy_spec
        self.material_ttl_seconds = ttl
        self.wall_clock = wall_clock
        self._validate_policy_binding()

    def _validate_policy_binding(self) -> None:
        receipt = self.policy_receipt
        spec = self.policy_spec
        if (
            spec.policy.policy_sha256 != receipt.policy.policy_sha256
            or spec.code_build_sha256 != self.code_build_sha256
            or spec.effective_config_sha256
            != receipt.settings_projection_sha256
            or spec.applies_to_execution_surfaces
            != ("alpaca_paper", "replay")
        ):
            _unavailable("initial_provider_adaptive_policy_binding_mismatch")

    def _validate_capture_authority(
        self,
        *,
        symbol: str,
        trigger_read_receipt_sha256: str,
        decision_at: datetime,
    ) -> tuple[
        CapturedPaperIqfeedTriggerReceipt,
        CapturedReadResult,
        ActiveCaptureInputPrefixAttestation,
        CaptureRunIdentity,
        str,
        dict[str, Any],
        dict[str, Any],
    ]:
        resolution = self.trigger_resolution
        if (
            resolution.status is not IqfeedTriggerStatus.READY
            or not resolution.ready
            or type(resolution.receipt) is not CapturedPaperIqfeedTriggerReceipt
            or type(resolution.captured_read) is not CapturedReadResult
        ):
            _unavailable("initial_provider_trigger_not_ready")
        trigger = resolution.receipt
        captured = resolution.captured_read
        assert trigger is not None and captured is not None
        if (
            trigger.content_sha256 != trigger_read_receipt_sha256
            or resolution.notify_sha256 != trigger.notify_sha256
            or trigger.symbol != symbol
        ):
            _unavailable("initial_provider_trigger_route_mismatch")
        try:
            proof = verify_active_capture_input_attestation(
                self.active_input_attestation
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_input_attestation_invalid"
            ) from exc
        coordinator = self.capture_coordinator
        identity = getattr(coordinator, "identity", None)
        if type(identity) is not CaptureRunIdentity:
            _unavailable("initial_provider_capture_identity_invalid")
        if (
            getattr(coordinator, "state", None) is not CaptureSessionState.RUNNING
            or str(getattr(coordinator, "certification_symbol", "") or "")
            != symbol
        ):
            _unavailable("initial_provider_capture_coordinator_unavailable")
        binding_sha256 = getattr(
            getattr(coordinator, "resource_binding", None),
            "binding_sha256",
            None,
        )
        if (
            proof.run_id != identity.run_id
            or proof.generation != identity.generation
            or proof.identity_sha256 != identity.identity_sha256
            or proof.account_identity_sha256
            != identity.account_identity_sha256
            or proof.code_build_sha256 != identity.code_build_sha256
            or proof.config_sha256 != identity.config_sha256
            or proof.feature_flags_sha256 != identity.feature_flags_sha256
            or proof.resource_binding_sha256 != binding_sha256
            or proof.decision_id != trigger.decision_id
            or trigger.capture_identity_sha256 != identity.identity_sha256
        ):
            _unavailable("initial_provider_capture_attestation_identity_mismatch")
        if identity.broker != "alpaca" or identity.broker_environment != "paper":
            _unavailable("initial_provider_capture_broker_scope_mismatch")
        if identity.code_build_sha256 != self.code_build_sha256:
            _unavailable("initial_provider_code_build_mismatch")
        evidence = self.capture_identity_evidence
        try:
            evidence.validate_for(identity, certification_symbol=symbol)
            account = dict(evidence.account_identity)
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_capture_identity_evidence_mismatch"
            ) from exc
        if account != {
            "broker": "alpaca",
            "environment": "paper",
            "account_id": self.expected_account_id,
        }:
            _unavailable("initial_provider_expected_account_mismatch")
        identity_evidence_snapshot = {
            "code_build": _canonical_value(evidence.code_build),
            "config": _canonical_value(evidence.config),
            "feature_flags": _canonical_value(evidence.feature_flags),
            "account_identity": _canonical_value(evidence.account_identity),
        }
        if (
            _hash_json(identity_evidence_snapshot["code_build"])
            != identity.code_build_sha256
            or _hash_json(identity_evidence_snapshot["config"])
            != identity.config_sha256
            or _hash_json(identity_evidence_snapshot["feature_flags"])
            != identity.feature_flags_sha256
            or _hash_json(identity_evidence_snapshot["account_identity"])
            != identity.account_identity_sha256
        ):
            _unavailable("initial_provider_capture_identity_evidence_mismatch")
        if self.policy_spec.feature_flags_sha256 != identity.feature_flags_sha256:
            _unavailable("initial_provider_feature_provenance_mismatch")
        try:
            config_sha256 = _sha(
                self.capture_config_sha256_resolver(symbol),
                "initial_provider_capture_config_sha256",
            )
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_capture_config_unavailable"
            ) from exc
        if (
            config_sha256 != identity.config_sha256
            or config_sha256 != sha256_json(evidence.config)
            or config_sha256 == self.policy_receipt.settings_projection_sha256
        ):
            _unavailable("initial_provider_capture_config_identity_mismatch")

        if (
            not captured.durable
            or captured.receipt is None
            or captured.receipt_submission is None
            or captured.receipt_submission.event is None
        ):
            _unavailable("initial_provider_trigger_read_unavailable")
        read_evidence = tuple(
            row
            for row in proof.read_evidence
            if row.receipt.read_id == trigger.captured_read_id
        )
        if len(read_evidence) != 1:
            _unavailable("initial_provider_trigger_read_attestation_missing")
        active_read = read_evidence[0]
        receipt_event = captured.receipt_submission.event
        if (
            active_read.receipt is not captured.receipt
            or active_read.receipt_sha256
            != trigger.captured_read_receipt_sha256
            or active_read.receipt_event_sha256
            != trigger.captured_read_receipt_event_sha256
            or active_read.receipt_event_sequence
            != trigger.captured_read_receipt_event_sequence
            or active_read.receipt_event_sha256 != receipt_event.event_sha256
            or active_read.receipt_event_sequence != receipt_event.sequence
            or active_read.receipt_committed_available_at
            != receipt_event.clocks.available_at
            or captured.receipt.read_id != trigger.captured_read_id
            or captured.receipt.decision_id != trigger.decision_id
            or captured.receipt.identity_sha256
            != trigger.capture_identity_sha256
            or captured.receipt.query_sha256
            != trigger.captured_read_query_sha256
            or captured.receipt.result_sha256
            != trigger.captured_read_result_sha256
            or captured.receipt.stream is not CaptureStream.IQFEED_PRINT
            or captured.receipt.provider != "iqfeed"
            or captured.receipt.symbol != symbol
            or captured.receipt.requested_at != trigger.notify_available_at
            or captured.receipt.replay_network_fallback_used
            or not captured.receipt.content_verified
        ):
            _unavailable("initial_provider_trigger_read_attestation_mismatch")
        if len(captured.source_events) != 1 or len(active_read.source_event_refs) != 1:
            _unavailable("initial_provider_trigger_source_ambiguous")
        source = captured.source_events[0]
        source_ref = active_read.source_event_refs[0]
        expected_ref = CaptureEventRef.from_event(source)
        if (
            source_ref != expected_ref
            or source_ref.event_sha256 != trigger.source_event_sha256
            or source_ref.sequence != trigger.source_event_sequence
            or source_ref.payload_sha256 != trigger.source_payload_sha256
            or source_ref.provider_event_at
            != trigger.source_provider_event_at
            or source_ref.received_at != trigger.source_received_at
            or source_ref.available_at != trigger.source_available_at
        ):
            _unavailable("initial_provider_trigger_source_attestation_mismatch")
        try:
            query = CaptureMicrostructureReadQuery.from_dict(
                dict(captured.receipt.query or {})
            )
            dependency = proof.dependency_profile.dependency_for(
                CaptureStream.IQFEED_PRINT
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_trigger_dependency_profile_invalid"
            ) from exc
        policy = self.policy_receipt.policy
        market_max_age = policy.market_data_max_age_seconds
        context_max_age = policy.context_data_max_age_seconds
        if (
            query.operation is not CaptureMicrostructureOperation.TRADE_FLOW
            or query.stream is not CaptureStream.IQFEED_PRINT
            or query.provider != "iqfeed"
            or query.symbol != symbol
            or query.event_end_inclusive != trigger.source_provider_event_at
            or query.available_at_most != captured.receipt.returned_at
            or not dependency.exact_provider_event_at_required
            or dependency.market_reference_at_required
            or dependency.max_source_age_seconds > market_max_age
            or dependency.coverage_start_at > query.event_start_exclusive
        ):
            _unavailable("initial_provider_trigger_dependency_profile_mismatch")
        continuity = tuple(
            row
            for row in proof.continuity_evidence
            if row.coverage.stream is CaptureStream.IQFEED_PRINT
        )
        if len(continuity) != 1:
            _unavailable("initial_provider_trigger_continuity_unavailable")
        exact_continuity = continuity[0]
        coverage = exact_continuity.coverage
        watermark = coverage.watermark
        if (
            watermark is None
            or exact_continuity.producer_id != active_read.producer_id
            or exact_continuity.producer_generation
            != active_read.producer_generation
            or coverage.identity_sha256 != identity.identity_sha256
            or coverage.provider != "iqfeed"
            or coverage.symbol != symbol
            or not coverage.exact_event_clock_complete
            or not coverage.content_verified
            or not coverage.continuity_complete
            or coverage.event_count <= 0
            or coverage.first_available_at > source_ref.available_at
            or coverage.last_available_at < source_ref.available_at
            or watermark.identity_sha256 != identity.identity_sha256
            or watermark.provider != "iqfeed"
            or watermark.symbol != symbol
            or watermark.generation != active_read.producer_generation
            or watermark.event_watermark_at < query.event_end_inclusive
            or exact_continuity.source_frontier_sequence < source_ref.sequence
            or exact_continuity.coverage_committed_available_at
            > proof.attested_available_at
        ):
            _unavailable("initial_provider_trigger_continuity_mismatch")
        clocks = (
            trigger.source_provider_event_at,
            trigger.source_received_at,
            trigger.source_available_at,
            captured.receipt.returned_at,
            active_read.receipt_committed_available_at,
            proof.attested_available_at,
        )
        if any(value > decision_at for value in clocks):
            _unavailable("initial_provider_capture_clock_from_future")
        if not (
            trigger.source_provider_event_at
            <= trigger.source_received_at
            <= trigger.source_available_at
            <= captured.receipt.returned_at
            <= active_read.receipt_committed_available_at
            <= proof.attested_available_at
            < proof.expires_at
        ):
            _unavailable("initial_provider_capture_clock_mismatch")
        if (
            (decision_at - trigger.source_available_at).total_seconds()
            > market_max_age
            or any(
                (decision_at - value).total_seconds() > context_max_age
                for value in (
                    captured.receipt.returned_at,
                    active_read.receipt_committed_available_at,
                    proof.attested_available_at,
                )
            )
        ):
            _unavailable("initial_provider_capture_authority_stale")
        return (
            trigger,
            captured,
            proof,
            identity,
            config_sha256,
            _capture_checkpoint_body(proof),
            identity_evidence_snapshot,
        )

    def _read_and_select_candidates(
        self,
        *,
        symbol: str,
        decision_at: datetime,
    ) -> tuple[
        CapturedPaperInitialCandidateRead,
        tuple[dict[str, Any], ...],
        dict[str, Any],
        str,
    ]:
        reader = self.candidate_reader
        try:
            if reader.network_fallback_allowed is not False:
                _unavailable("initial_candidate_network_fallback_forbidden")
            if reader.mutation_allowed is not False:
                _unavailable("initial_candidate_mutation_forbidden")
            result = reader.read_candidates(
                user_id=self.user_id,
                symbol=symbol,
                decision_at=decision_at,
            )
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_candidate_read_unavailable"
            ) from exc
        if type(result) is not CapturedPaperInitialCandidateRead:
            _unavailable("initial_candidate_read_result_invalid")
        if result.user_id != self.user_id or result.symbol != symbol:
            _unavailable("initial_candidate_read_route_mismatch")
        read_age = (decision_at - result.read_at).total_seconds()
        max_age = self.policy_receipt.policy.context_data_max_age_seconds
        if read_age < 0.0:
            _unavailable("initial_candidate_read_from_future")
        if read_age > max_age:
            _unavailable("initial_candidate_read_stale")
        snapshots = tuple(
            _candidate_snapshot(
                row,
                symbol=symbol,
                decision_at=decision_at,
                max_age_seconds=max_age,
            )
            for row in result.rows
        )
        ordered = tuple(
            sorted(
                snapshots,
                key=lambda row: (
                    int(row["variant"]["id"]),
                    int(row["viability"]["id"]),
                    str(row["variant_sha256"]),
                    str(row["viability_sha256"]),
                ),
            )
        )
        keys = tuple(
            (int(row["variant"]["id"]), int(row["viability"]["id"]))
            for row in ordered
        )
        if len(keys) != len(set(keys)):
            _unavailable("initial_candidate_set_duplicated")
        eligible = [row for row in ordered if row["eligible"]]
        if not eligible:
            _unavailable("initial_candidate_selection_coverage_unavailable")
        selected = min(eligible, key=_selection_rank)
        considered_body = {
            "schema_version": INITIAL_PROVIDER_CONSIDERED_SET_SCHEMA_VERSION,
            "user_id": self.user_id,
            "symbol": symbol,
            "read_at": _iso(result.read_at),
            "candidates": list(ordered),
        }
        return result, ordered, selected, _hash_json(considered_body)

    def prepare_initial_session(
        self,
        *,
        symbol: str,
        trigger_read_receipt_sha256: str,
    ) -> CapturedPaperInitialSessionMaterial:
        normalized_symbol = _symbol(symbol)
        trigger_sha256 = _sha(
            trigger_read_receipt_sha256,
            "initial_provider_trigger_read_receipt_sha256",
        )
        try:
            decision_at = _utc(
                self.wall_clock(),
                "initial_provider_decision_at",
            )
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_clock_unavailable"
            ) from exc
        (
            trigger,
            _captured,
            proof,
            identity,
            config_sha256,
            checkpoint_body,
            identity_evidence_snapshot,
        ) = self._validate_capture_authority(
            symbol=normalized_symbol,
            trigger_read_receipt_sha256=trigger_sha256,
            decision_at=decision_at,
        )
        candidate_read, considered, selected, considered_sha256 = (
            self._read_and_select_candidates(
                symbol=normalized_symbol,
                decision_at=decision_at,
            )
        )
        considered_body = {
            "schema_version": INITIAL_PROVIDER_CONSIDERED_SET_SCHEMA_VERSION,
            "user_id": self.user_id,
            "symbol": normalized_symbol,
            "read_at": _iso(candidate_read.read_at),
            "candidates": list(considered),
        }
        if _hash_json(considered_body) != considered_sha256:
            _unavailable("initial_candidate_set_hash_mismatch")
        selection_body = {
            "schema_version": INITIAL_PROVIDER_SELECTION_SCHEMA_VERSION,
            "selection_algorithm": _SELECTION_ALGORITHM,
            "user_id": self.user_id,
            "symbol": normalized_symbol,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "runtime_generation": self.runtime_generation,
            "decision_at": _iso(decision_at),
            "trigger_receipt": trigger.to_dict(),
            "trigger_receipt_sha256": trigger_sha256,
            "capture_receipt_sha256": self.capture_receipt_sha256,
            "capture_checkpoint": checkpoint_body,
            "capture_config": identity_evidence_snapshot["config"],
            "capture_config_sha256": config_sha256,
            "code_build": identity_evidence_snapshot["code_build"],
            "code_build_sha256": self.code_build_sha256,
            "feature_flags": identity_evidence_snapshot["feature_flags"],
            "feature_flags_sha256": identity.feature_flags_sha256,
            "account_identity": identity_evidence_snapshot["account_identity"],
            "account_identity_sha256": identity.account_identity_sha256,
            "adaptive_policy_settings_projection": (
                self.policy_receipt.to_settings_projection()
            ),
            "adaptive_policy_provenance_sha256": (
                self.policy_spec.provenance_sha256
            ),
            "considered_set": considered_body,
            "considered_set_sha256": considered_sha256,
            "selected_candidate": selected,
        }
        selection_sha256 = _hash_json(selection_body)
        considered_json = _canonical_json(considered_body)
        selected_json = _canonical_json(selected)
        selection_json = _canonical_json(selection_body)
        checkpoint_json = _canonical_json(checkpoint_body)
        readiness = selected["viability"]["execution_readiness_json"]
        readiness_json = _canonical_json(readiness)

        policy = self.policy_receipt.policy
        policy_caps = {
            "source": "shared_adaptive_policy",
            "risk_fraction_of_equity": policy.risk_fraction_of_equity,
            "daily_risk_fraction_of_equity": (
                policy.daily_risk_fraction_of_equity
            ),
            "portfolio_risk_fraction_of_equity": (
                policy.portfolio_risk_fraction_of_equity
            ),
            "cluster_risk_fraction_of_equity": (
                policy.cluster_risk_fraction_of_equity
            ),
            "symbol_risk_fraction_of_equity": (
                policy.symbol_risk_fraction_of_equity
            ),
            "max_notional_fraction_of_equity": (
                policy.max_notional_fraction_of_equity
            ),
            "max_buying_power_fraction_for_notional": (
                policy.max_buying_power_fraction_for_notional
            ),
            "max_portfolio_gross_fraction_of_equity": (
                policy.max_portfolio_gross_fraction_of_equity
            ),
            "max_adv_participation": policy.max_adv_participation,
            "max_recent_volume_participation": (
                policy.max_recent_volume_participation
            ),
            "max_executable_depth_participation": (
                policy.max_executable_depth_participation
            ),
        }
        initial_risk = {
            "status": "captured_policy_bound_sizing_pending",
            "policy_sha256": policy.policy_sha256,
            "settings_projection_sha256": (
                self.policy_receipt.settings_projection_sha256
            ),
            "selection_receipt_sha256": selection_sha256,
            "economic_resolution_deferred_to_captured_runtime": True,
        }
        payload = {
            "momentum_risk_policy_summary": {
                "adaptive_policy_sha256": policy.policy_sha256,
                "adaptive_policy_provenance_sha256": (
                    self.policy_spec.provenance_sha256
                ),
                "settings_projection_sha256": (
                    self.policy_receipt.settings_projection_sha256
                ),
                "code_build_sha256": self.code_build_sha256,
                "capture_config_sha256": config_sha256,
                "feature_flags_sha256": identity.feature_flags_sha256,
                "applies_to_execution_surfaces": ["alpaca_paper", "replay"],
                "policy_version": policy.policy_version,
                "policy_source": policy.policy_source,
            },
            "momentum_risk_policy_resolved_utc": _iso(decision_at),
            "momentum_risk": initial_risk,
            "viability_brief": {
                "scope": "symbol",
                "symbol": normalized_symbol,
                "viability_score": selected["viability"]["viability_score"],
                "paper_eligible": selected["viability"]["paper_eligible"],
                "live_eligible": selected["viability"]["live_eligible"],
                "readiness_freshness_at": selected[
                    "readiness_freshness_at"
                ],
                "considered_set_canonical_json": considered_json,
                "considered_set_sha256": considered_sha256,
                "selected_candidate_canonical_json": selected_json,
                "selected_candidate_sha256": _hash_json(selected),
                "selection_receipt_canonical_json": selection_json,
                "selection_receipt_sha256": selection_sha256,
            },
            "execution_readiness_subset": {
                "selected_readiness_canonical_json": readiness_json,
                "selected_readiness_sha256": _hash_json(readiness),
                "capture_checkpoint_canonical_json": checkpoint_json,
                "capture_checkpoint_sha256": _hash_json(checkpoint_body),
                "trigger_captured_read_id": trigger.captured_read_id,
                "trigger_captured_read_receipt_sha256": (
                    trigger.captured_read_receipt_sha256
                ),
            },
            "momentum_policy_caps": policy_caps,
            "momentum_policy_caps_derivation": {
                "source": "shared_adaptive_policy",
                "equity_relative": True,
                "structural_stop_required": True,
                "setup_quality_required": True,
                "volatility_required": True,
                "liquidity_required": True,
                "correlation_required": True,
                "policy_sha256": policy.policy_sha256,
            },
        }
        selected_viability_sha256 = str(selected["viability_sha256"])
        try:
            runner_template = CapturedPaperInitialRunnerRiskTemplate(
                payload=payload,
                payload_sha256=_hash_json(payload),
                source_receipt_sha256s={
                    "adaptive_policy_settings": (
                        self.policy_receipt.settings_projection_sha256
                    ),
                    "capture_config": config_sha256,
                    "execution_readiness": selected_viability_sha256,
                    "momentum_policy_caps": _hash_json(policy_caps),
                    "momentum_risk_evaluation": _hash_json(initial_risk),
                    "viability_snapshot": selected_viability_sha256,
                },
            )
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_runner_template_unavailable"
            ) from exc
        read_ids = tuple(sorted(proof.required_read_ids))
        inventory_sha256 = _hash_json(
            {
                "schema_version": INITIAL_READ_INVENTORY_SCHEMA_VERSION,
                "read_ids": list(read_ids),
            }
        )
        context_max_age = policy.context_data_max_age_seconds
        market_max_age = policy.market_data_max_age_seconds
        selected_freshness = datetime.fromisoformat(
            str(selected["readiness_freshness_at"]).replace("Z", "+00:00")
        )
        expires_at = min(
            decision_at + timedelta(seconds=self.material_ttl_seconds),
            proof.expires_at,
            trigger.source_available_at + timedelta(seconds=market_max_age),
            candidate_read.read_at + timedelta(seconds=context_max_age),
            selected_freshness + timedelta(seconds=context_max_age),
        )
        if expires_at <= decision_at:
            _unavailable("initial_provider_material_expired")
        try:
            final_as_of = _utc(
                self.wall_clock(),
                "initial_provider_final_as_of",
            )
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_clock_unavailable"
            ) from exc
        if final_as_of < decision_at:
            _unavailable("initial_provider_clock_regressed")
        if final_as_of >= expires_at:
            _unavailable("initial_provider_material_expired")
        try:
            final_config_sha256 = _sha(
                self.capture_config_sha256_resolver(normalized_symbol),
                "initial_provider_capture_config_sha256",
            )
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_capture_config_unavailable"
            ) from exc
        try:
            self.capture_identity_evidence.validate_for(
                identity,
                certification_symbol=normalized_symbol,
            )
            final_identity_evidence_snapshot = {
                "code_build": _canonical_value(
                    self.capture_identity_evidence.code_build
                ),
                "config": _canonical_value(self.capture_identity_evidence.config),
                "feature_flags": _canonical_value(
                    self.capture_identity_evidence.feature_flags
                ),
                "account_identity": _canonical_value(
                    self.capture_identity_evidence.account_identity
                ),
            }
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_capture_identity_drifted"
            ) from exc
        final_binding_sha256 = getattr(
            getattr(self.capture_coordinator, "resource_binding", None),
            "binding_sha256",
            None,
        )
        try:
            final_reader_network_fallback = (
                self.candidate_reader.network_fallback_allowed
            )
            final_reader_mutation = self.candidate_reader.mutation_allowed
            self._validate_policy_binding()
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_final_authority_unavailable"
            ) from exc
        if (
            final_config_sha256 != config_sha256
            or final_identity_evidence_snapshot != identity_evidence_snapshot
            or getattr(self.capture_coordinator, "state", None)
            is not CaptureSessionState.RUNNING
            or getattr(self.capture_coordinator, "identity", None) is not identity
            or final_binding_sha256 != proof.resource_binding_sha256
            or final_reader_network_fallback is not False
            or final_reader_mutation is not False
        ):
            _unavailable("initial_provider_capture_identity_drifted")
        try:
            return CapturedPaperInitialSessionMaterial(
                symbol=normalized_symbol,
                user_id=self.user_id,
                variant_id=int(selected["variant"]["id"]),
                account_scope=self.account_scope,
                expected_account_id=self.expected_account_id,
                runtime_generation=self.runtime_generation,
                execution_family=ALPACA_SPOT_EXECUTION_FAMILY,
                code_build_sha256=self.code_build_sha256,
                config_sha256=config_sha256,
                capture_receipt_sha256=self.capture_receipt_sha256,
                policy_sha256=policy.policy_sha256,
                adaptive_policy_settings_projection=(
                    self.policy_receipt.to_settings_projection()
                ),
                settings_projection_sha256=(
                    self.policy_receipt.settings_projection_sha256
                ),
                feature_flags_sha256=identity.feature_flags_sha256,
                adaptive_policy_provenance_sha256=(
                    self.policy_spec.provenance_sha256
                ),
                runner_risk_template=runner_template,
                trigger_read_receipt_sha256=trigger_sha256,
                captured_input_attestation_sha256=proof.attestation_sha256,
                captured_read_ids=read_ids,
                captured_read_inventory_sha256=inventory_sha256,
                selection_receipt_sha256=selection_sha256,
                strategy_variant_sha256=str(selected["variant_sha256"]),
                viability_snapshot_sha256=selected_viability_sha256,
                decision_at=decision_at,
                expires_at=expires_at,
            )
        except CapturedPaperInitialProviderCoverageUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperInitialProviderCoverageUnavailable(
                "initial_provider_material_construction_failed"
            ) from exc


__all__ = [
    "CaptureBackedPaperInitialSessionMaterialProvider",
    "CapturedPaperInitialCandidateRead",
    "CapturedPaperInitialCandidateReadPort",
    "CapturedPaperInitialCandidateRow",
    "CapturedPaperInitialProviderCoverageUnavailable",
    "INITIAL_PROVIDER_CANDIDATE_READ_SCHEMA_VERSION",
    "INITIAL_PROVIDER_CAPTURE_CHECKPOINT_SCHEMA_VERSION",
    "INITIAL_PROVIDER_CONSIDERED_SET_SCHEMA_VERSION",
    "INITIAL_PROVIDER_SELECTION_SCHEMA_VERSION",
    "INITIAL_PROVIDER_STATUS_COVERAGE_UNAVAILABLE",
]
