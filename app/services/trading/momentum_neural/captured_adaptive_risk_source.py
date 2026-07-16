"""Pure captured-input factory for adaptive Alpaca-PAPER entry economics.

This module is deliberately a construction boundary, not an input loader.  It
has no database, provider, broker, settings, or wall-clock access.  A caller
must supply one already captured and content-addressed decision boundary,
including the process-private input-prefix attestation issued by the active
capture runtime.  Consequently a missing/stale/mismatched input rejects only
that decision before a reservation claim or order can be created; there is no
current-DB or network fallback hidden in this factory.

The policy specification is shared by ``replay`` and ``alpaca_paper``.  Its
only sizing limits are the equity-relative fractions/multipliers represented by
``AdaptiveRiskPolicy``; this layer adds no activation-only dollar or serial
symbol cap.  First-dip remains a special additive dependency: its detector tape
receipt must be present in the private proof and its later final-tape authority
is still enforced by the existing first-dip final admission path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import re
from typing import Any, Mapping
import uuid

from .adaptive_risk_policy import (
    AdaptiveRiskContractError,
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    RiskInputEvidence,
)
from .adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    AdaptiveRiskBuilderSource,
    AdaptiveRiskRuntimeCaptureMaterial,
    FIRST_DIP_SETUP_FAMILY,
    adaptive_risk_capture_binding_from_active_attestation,
)
from .adaptive_risk_reservation import (
    ImmutableAccountRiskSnapshot,
    LockedAlpacaPaperAdmissionBundle,
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
    verify_locked_alpaca_paper_daily_pnl_attestation,
)
from .alpaca_paper_account_receipt import (
    ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS,
    ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION,
    ALPACA_PAPER_ACCOUNT_PROVIDER,
    ALPACA_PAPER_ACCOUNT_QUERY_KEYS,
    ALPACA_PAPER_ACCOUNT_READY_STATUS,
    ALPACA_PAPER_ACCOUNT_REQUESTED_FIELDS,
    alpaca_paper_account_capture_query,
)
from .alpaca_paper_identity import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    AlpacaPaperAccountIdentityError,
    alpaca_paper_account_identity_sha256,
    canonical_alpaca_paper_account_id,
)
from .replay_capture_contract import (
    ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION,
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureStream,
    sha256_json,
    verify_active_capture_input_attestation,
)


UTC = timezone.utc
CAPTURED_ADAPTIVE_RISK_FACT_SCHEMA_VERSION = (
    "chili.captured-adaptive-risk-fact.v1"
)
CAPTURED_ADAPTIVE_RISK_POLICY_SPEC_SCHEMA_VERSION = (
    "chili.captured-adaptive-risk-policy-spec.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SETUP_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SHARED_EXECUTION_SURFACES = frozenset({"replay", "alpaca_paper"})
_ALPACA_PAPER_NBBO_PROVIDER = "alpaca_market_data_paper"
_FACT_NAMES = frozenset(
    {
        "structural_stop",
        "setup_quality",
        "volatility",
        "liquidity",
        "correlation",
        "candidate_buying_power_estimate",
    }
)


class CapturedAdaptiveRiskCoverageUnavailable(AdaptiveRiskBuilderError):
    """Stable decision-local rejection for absent or unprovable capture input."""

    reason = "captured_adaptive_risk_coverage_unavailable"

    def __init__(self, detail: str) -> None:
        self.coverage_detail = str(detail or "unspecified")
        super().__init__(self.reason, self.coverage_detail)


def _coverage_unavailable(detail: str) -> None:
    raise CapturedAdaptiveRiskCoverageUnavailable(detail)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _coverage_unavailable(f"{field}_clock_invalid")
    return value.astimezone(UTC)


def _sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        _coverage_unavailable(f"{field}_sha256_invalid")
    return normalized


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        _coverage_unavailable(f"{field}_invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        _coverage_unavailable(f"{field}_invalid")
    if not math.isfinite(normalized) or (
        minimum is not None and normalized < minimum
    ):
        _coverage_unavailable(f"{field}_invalid")
    return normalized


def _payload_decimal(
    value: Any,
    field: str,
    *,
    minimum: Decimal | None = None,
    strictly_positive: bool = False,
) -> Decimal:
    if not isinstance(value, str) or value != value.strip() or not value:
        _coverage_unavailable(f"{field}_invalid")
    try:
        normalized = Decimal(value)
    except (InvalidOperation, ValueError):
        _coverage_unavailable(f"{field}_invalid")
    if not normalized.is_finite():
        _coverage_unavailable(f"{field}_invalid")
    if strictly_positive and normalized <= 0:
        _coverage_unavailable(f"{field}_invalid")
    if minimum is not None and normalized < minimum:
        _coverage_unavailable(f"{field}_invalid")
    return normalized


def _strict_json_object(raw: str, field: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        _coverage_unavailable(f"{field}_missing")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _coverage_unavailable(f"{field}_duplicate_key")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda _value: _coverage_unavailable(
                f"{field}_nonfinite"
            ),
        )
    except CapturedAdaptiveRiskCoverageUnavailable:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        _coverage_unavailable(f"{field}_invalid_json")
    if not isinstance(parsed, dict):
        _coverage_unavailable(f"{field}_not_object")
    return parsed


def _exact_utc_payload_clock(value: Any, field: str) -> datetime:
    """Parse the adapter's canonical ``Z`` clock without accepting aliases."""

    if not isinstance(value, str) or not value.endswith("Z"):
        _coverage_unavailable(f"{field}_clock_invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        _coverage_unavailable(f"{field}_clock_invalid")
    parsed = _utc(parsed, field)
    if parsed.isoformat().replace("+00:00", "Z") != value:
        _coverage_unavailable(f"{field}_clock_noncanonical")
    return parsed


@dataclass(frozen=True)
class CapturedAdaptiveRiskPolicySpec:
    """One explicitly proven economic policy for both replay and PAPER."""

    policy: AdaptiveRiskPolicy
    code_build_sha256: str
    effective_config_sha256: str
    feature_flags_sha256: str
    applies_to_execution_surfaces: tuple[str, ...] = (
        "alpaca_paper",
        "replay",
    )
    schema_version: str = CAPTURED_ADAPTIVE_RISK_POLICY_SPEC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_ADAPTIVE_RISK_POLICY_SPEC_SCHEMA_VERSION:
            _coverage_unavailable("policy_spec_schema_invalid")
        if type(self.policy) is not AdaptiveRiskPolicy:
            _coverage_unavailable("policy_spec_policy_invalid")
        for name in (
            "code_build_sha256",
            "effective_config_sha256",
            "feature_flags_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        surfaces = tuple(
            sorted(
                str(value or "").strip().lower()
                for value in self.applies_to_execution_surfaces
            )
        )
        if frozenset(surfaces) != _SHARED_EXECUTION_SURFACES or len(surfaces) != 2:
            _coverage_unavailable("policy_not_shared_by_replay_and_alpaca_paper")
        object.__setattr__(self, "applies_to_execution_surfaces", surfaces)

    @property
    def provenance_sha256(self) -> str:
        return sha256_json(
            {
                "schema_version": self.schema_version,
                "policy_sha256": self.policy.policy_sha256,
                "policy_version": self.policy.policy_version,
                "policy_source": self.policy.policy_source,
                "code_build_sha256": self.code_build_sha256,
                "effective_config_sha256": self.effective_config_sha256,
                "feature_flags_sha256": self.feature_flags_sha256,
                "applies_to_execution_surfaces": list(
                    self.applies_to_execution_surfaces
                ),
            }
        )


@dataclass(frozen=True)
class CapturedAdaptiveRiskDecisionIdentity:
    """Exact decision/CID identity shared by detector, capture and reservation."""

    execution_surface: str
    run_id: str
    generation: int
    decision_id: str
    symbol: str
    setup_family: str
    correlation_cluster: str
    account_scope: str
    decision_at: datetime
    execution_family: str = "alpaca_spot"
    venue: str = "alpaca"
    broker_environment: str = "paper"

    def __post_init__(self) -> None:
        surface = str(self.execution_surface or "").strip().lower()
        if surface not in _SHARED_EXECUTION_SURFACES:
            _coverage_unavailable("execution_surface_invalid")
        object.__setattr__(self, "execution_surface", surface)
        try:
            run_id = str(uuid.UUID(str(self.run_id or "").strip()))
        except (ValueError, AttributeError):
            _coverage_unavailable("run_id_invalid")
        object.__setattr__(self, "run_id", run_id)
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            _coverage_unavailable("generation_invalid")
        object.__setattr__(self, "generation", int(self.generation))
        decision_id = str(self.decision_id or "").strip()
        if not decision_id:
            _coverage_unavailable("decision_id_missing")
        object.__setattr__(self, "decision_id", decision_id)
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            _coverage_unavailable("symbol_missing")
        object.__setattr__(self, "symbol", symbol)
        setup_family = str(self.setup_family or "").strip().lower()
        if _SETUP_FAMILY_RE.fullmatch(setup_family) is None:
            _coverage_unavailable("setup_family_invalid")
        object.__setattr__(self, "setup_family", setup_family)
        cluster = str(self.correlation_cluster or "").strip().lower()
        if not cluster:
            _coverage_unavailable("correlation_cluster_missing")
        object.__setattr__(self, "correlation_cluster", cluster)
        scope = str(self.account_scope or "").strip()
        if scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            _coverage_unavailable("account_scope_not_alpaca_paper")
        object.__setattr__(self, "account_scope", scope)
        object.__setattr__(self, "decision_at", _utc(self.decision_at, "decision"))
        expected = {
            "execution_family": "alpaca_spot",
            "venue": "alpaca",
            "broker_environment": "paper",
        }
        for name, required in expected.items():
            value = str(getattr(self, name) or "").strip().lower()
            if value != required:
                _coverage_unavailable(f"{name}_invalid")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class CapturedExactBbo:
    """Exact quote payload plus the private-proof receipt/event selectors."""

    read_id: str
    source_event_sha256: str
    payload_json: str

    def __post_init__(self) -> None:
        try:
            read_id = str(uuid.UUID(str(self.read_id or "").strip()))
        except (ValueError, AttributeError):
            _coverage_unavailable("bbo_read_id_invalid")
        object.__setattr__(self, "read_id", read_id)
        object.__setattr__(
            self,
            "source_event_sha256",
            _sha(self.source_event_sha256, "bbo_source_event"),
        )
        # Parse now so malformed/non-canonical JSON cannot sit in a typed boundary.
        _strict_json_object(self.payload_json, "bbo_payload")


@dataclass(frozen=True)
class CapturedAccountRiskReceipt:
    """Per-decision account read bound into the same private input proof."""

    read_id: str
    source_event_sha256: str
    payload_json: str

    def __post_init__(self) -> None:
        try:
            read_id = str(uuid.UUID(str(self.read_id or "").strip()))
        except (ValueError, AttributeError):
            _coverage_unavailable("account_read_id_invalid")
        object.__setattr__(self, "read_id", read_id)
        object.__setattr__(
            self,
            "source_event_sha256",
            _sha(self.source_event_sha256, "account_source_event"),
        )
        _strict_json_object(self.payload_json, "account_payload")


@dataclass(frozen=True)
class CapturedAdaptiveRiskEconomicInputs:
    """Economic values only; provenance is supplied separately and hash-bound."""

    structural_stop: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    fees_per_share_usd: float
    setup_quality: float
    realized_volatility_fraction: float
    average_daily_volume_shares: float
    recent_volume_shares: float
    executable_depth_shares: float
    candidate_buying_power_impact_per_share_usd: float

    def __post_init__(self) -> None:
        positive = (
            "structural_stop",
            "realized_volatility_fraction",
            "candidate_buying_power_impact_per_share_usd",
        )
        nonnegative = (
            "entry_slippage_bps",
            "exit_slippage_bps",
            "fees_per_share_usd",
            "average_daily_volume_shares",
            "recent_volume_shares",
            "executable_depth_shares",
        )
        for name in positive:
            value = _finite(getattr(self, name), name, minimum=0.0)
            if value <= 0:
                _coverage_unavailable(f"{name}_not_positive")
            object.__setattr__(self, name, value)
        for name in nonnegative:
            object.__setattr__(
                self,
                name,
                _finite(getattr(self, name), name, minimum=0.0),
            )
        quality = _finite(self.setup_quality, "setup_quality")
        if not 0.0 <= quality <= 1.0:
            _coverage_unavailable("setup_quality_out_of_range")
        object.__setattr__(self, "setup_quality", quality)


def captured_adaptive_risk_fact_payloads(
    identity: CapturedAdaptiveRiskDecisionIdentity,
    economics: CapturedAdaptiveRiskEconomicInputs,
) -> dict[str, dict[str, Any]]:
    """Canonical payloads producers hash when handing derived facts to the factory."""

    if type(identity) is not CapturedAdaptiveRiskDecisionIdentity or type(
        economics
    ) is not CapturedAdaptiveRiskEconomicInputs:
        _coverage_unavailable("derived_fact_boundary_invalid")
    common = {
        "schema_version": CAPTURED_ADAPTIVE_RISK_FACT_SCHEMA_VERSION,
        "decision_id": identity.decision_id,
        "symbol": identity.symbol,
        "setup_family": identity.setup_family,
    }
    return {
        "structural_stop": {
            **common,
            "fact": "structural_stop",
            "structural_stop": economics.structural_stop,
        },
        "setup_quality": {
            **common,
            "fact": "setup_quality",
            "setup_quality": economics.setup_quality,
        },
        "volatility": {
            **common,
            "fact": "volatility",
            "realized_volatility_fraction": (
                economics.realized_volatility_fraction
            ),
        },
        "liquidity": {
            **common,
            "fact": "liquidity",
            "average_daily_volume_shares": (
                economics.average_daily_volume_shares
            ),
            "recent_volume_shares": economics.recent_volume_shares,
            "executable_depth_shares": economics.executable_depth_shares,
            "entry_slippage_bps": economics.entry_slippage_bps,
            "exit_slippage_bps": economics.exit_slippage_bps,
            "fees_per_share_usd": economics.fees_per_share_usd,
        },
        "correlation": {
            **common,
            "fact": "correlation",
            "correlation_cluster": identity.correlation_cluster,
        },
        "candidate_buying_power_estimate": {
            **common,
            "fact": "candidate_buying_power_estimate",
            "candidate_buying_power_impact_per_share_usd": (
                economics.candidate_buying_power_impact_per_share_usd
            ),
        },
    }


@dataclass(frozen=True)
class CapturedAdaptiveRiskFactProvenance:
    """Content hash and captured-read lineage for one derived economic fact."""

    source: str
    observed_at: datetime
    available_at: datetime
    content_sha256: str
    provider_generation: str
    source_read_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        generation = str(self.provider_generation or "").strip()
        if not source or not generation:
            _coverage_unavailable("derived_fact_source_or_generation_missing")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "provider_generation", generation)
        observed = _utc(self.observed_at, "derived_fact_observed")
        available = _utc(self.available_at, "derived_fact_available")
        if available < observed:
            _coverage_unavailable("derived_fact_availability_precedes_observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(
            self, "content_sha256", _sha(self.content_sha256, "derived_fact")
        )
        read_ids: list[str] = []
        for value in self.source_read_ids:
            try:
                read_ids.append(str(uuid.UUID(str(value or "").strip())))
            except (ValueError, AttributeError):
                _coverage_unavailable("derived_fact_source_read_id_invalid")
        normalized = tuple(sorted(read_ids))
        if not normalized or len(normalized) != len(set(normalized)):
            _coverage_unavailable("derived_fact_source_reads_empty_or_duplicated")
        object.__setattr__(self, "source_read_ids", normalized)

    @classmethod
    def create(
        cls,
        *,
        payload: Mapping[str, Any],
        source: str,
        observed_at: datetime,
        available_at: datetime,
        provider_generation: str,
        source_read_ids: tuple[str, ...],
    ) -> "CapturedAdaptiveRiskFactProvenance":
        return cls(
            source=source,
            observed_at=observed_at,
            available_at=available_at,
            content_sha256=sha256_json(dict(payload)),
            provider_generation=provider_generation,
            source_read_ids=source_read_ids,
        )

    def to_risk_evidence(self) -> RiskInputEvidence:
        return RiskInputEvidence(
            source=self.source,
            observed_at=self.observed_at,
            available_at=self.available_at,
            content_sha256=self.content_sha256,
            provider_generation=self.provider_generation,
        )


@dataclass(frozen=True)
class CapturedAdaptiveRiskEvidenceSet:
    structural_stop: CapturedAdaptiveRiskFactProvenance
    setup_quality: CapturedAdaptiveRiskFactProvenance
    volatility: CapturedAdaptiveRiskFactProvenance
    liquidity: CapturedAdaptiveRiskFactProvenance
    correlation: CapturedAdaptiveRiskFactProvenance
    candidate_buying_power_estimate: CapturedAdaptiveRiskFactProvenance

    def as_mapping(self) -> dict[str, CapturedAdaptiveRiskFactProvenance]:
        result = {name: getattr(self, name) for name in sorted(_FACT_NAMES)}
        if any(
            type(value) is not CapturedAdaptiveRiskFactProvenance
            for value in result.values()
        ):
            _coverage_unavailable("derived_fact_provenance_incomplete")
        return result


@dataclass(frozen=True)
class CapturedAdaptiveRiskDecisionBoundary:
    identity: CapturedAdaptiveRiskDecisionIdentity
    exact_bbo: CapturedExactBbo
    economics: CapturedAdaptiveRiskEconomicInputs
    fact_evidence: CapturedAdaptiveRiskEvidenceSet
    account_snapshot: ImmutableAccountRiskSnapshot
    account_receipt: CapturedAccountRiskReceipt
    reservation_ledger_snapshot: LockedAdaptiveRiskAdmissionSnapshot
    active_capture_attestation: ActiveCaptureInputPrefixAttestation
    locked_alpaca_paper_admission_bundle: (
        LockedAlpacaPaperAdmissionBundle | None
    ) = None


def _validate_freshness(
    *,
    observed_at: datetime,
    available_at: datetime,
    decision_at: datetime,
    max_age_seconds: float,
    field: str,
) -> None:
    observed = _utc(observed_at, f"{field}_observed")
    available = _utc(available_at, f"{field}_available")
    if available < observed or observed > decision_at or available > decision_at:
        _coverage_unavailable(f"{field}_from_future_or_clock_mismatch")
    if (
        (decision_at - observed).total_seconds() > max_age_seconds
        or (decision_at - available).total_seconds() > max_age_seconds
    ):
        _coverage_unavailable(f"{field}_stale")


class CapturedAdaptiveRiskSourceFactory:
    """Build one no-fetch adaptive source/material from an exact capture boundary."""

    def __init__(self, policy_spec: CapturedAdaptiveRiskPolicySpec) -> None:
        if type(policy_spec) is not CapturedAdaptiveRiskPolicySpec:
            _coverage_unavailable("policy_spec_missing")
        self._policy_spec = policy_spec

    @property
    def policy_spec(self) -> CapturedAdaptiveRiskPolicySpec:
        return self._policy_spec

    def build(
        self, boundary: CapturedAdaptiveRiskDecisionBoundary
    ) -> AdaptiveRiskRuntimeCaptureMaterial:
        if type(boundary) is not CapturedAdaptiveRiskDecisionBoundary:
            _coverage_unavailable("decision_boundary_invalid")
        identity = boundary.identity
        if type(identity) is not CapturedAdaptiveRiskDecisionIdentity:
            _coverage_unavailable("decision_identity_invalid")
        if identity.execution_surface not in self._policy_spec.applies_to_execution_surfaces:
            _coverage_unavailable("execution_surface_not_in_policy_spec")
        try:
            proof = verify_active_capture_input_attestation(
                boundary.active_capture_attestation
            )
        except CaptureContractError as exc:
            raise CapturedAdaptiveRiskCoverageUnavailable(
                "active_capture_attestation_invalid"
            ) from exc
        self._validate_attestation(identity, proof)

        locked_paper_bundle = boundary.locked_alpaca_paper_admission_bundle
        if identity.execution_surface == "alpaca_paper":
            if type(locked_paper_bundle) is not LockedAlpacaPaperAdmissionBundle:
                _coverage_unavailable("locked_alpaca_paper_admission_unavailable")
            try:
                locked_paper_bundle.verify()
                verify_locked_alpaca_paper_daily_pnl_attestation(
                    locked_paper_bundle.attestation
                )
            except (AdaptiveRiskContractError, TypeError, ValueError) as exc:
                raise CapturedAdaptiveRiskCoverageUnavailable(
                    "locked_alpaca_paper_admission_invalid"
                ) from exc
            attestation = locked_paper_bundle.attestation
            expected_locked_identity = {
                "decision_id": (attestation.decision_id, identity.decision_id),
                "run_id": (attestation.run_id, identity.run_id),
                "generation": (attestation.generation, identity.generation),
                "decision_at": (
                    locked_paper_bundle.decision_as_of,
                    identity.decision_at,
                ),
                "account_identity": (
                    attestation.account_identity_sha256,
                    proof.account_identity_sha256,
                ),
                "active_input_attestation": (
                    locked_paper_bundle.active_input_attestation_sha256,
                    proof.attestation_sha256,
                ),
            }
            changed = sorted(
                name
                for name, (actual, required) in expected_locked_identity.items()
                if actual != required
            )
            if changed:
                _coverage_unavailable(
                    "locked_alpaca_paper_admission_mismatch:"
                    + ",".join(changed)
                )
            if not (
                attestation.decision_as_of
                <= attestation.expires_at
                <= proof.expires_at
            ):
                _coverage_unavailable(
                    "locked_alpaca_paper_admission_mismatch:capture_expiry"
                )
            account = locked_paper_bundle.account_snapshot
            ledger = locked_paper_bundle.locked_risk_snapshot
            if (
                boundary.account_snapshot.snapshot_sha256
                != account.snapshot_sha256
                or boundary.reservation_ledger_snapshot.content_sha256
                != ledger.content_sha256
            ):
                _coverage_unavailable(
                    "caller_snapshot_differs_from_locked_alpaca_bundle"
                )
        else:
            if locked_paper_bundle is not None:
                _coverage_unavailable(
                    "locked_alpaca_paper_admission_unexpected_for_replay"
                )
            account = boundary.account_snapshot
            ledger = boundary.reservation_ledger_snapshot
        if type(account) is not ImmutableAccountRiskSnapshot:
            _coverage_unavailable("account_snapshot_missing")
        if type(ledger) is not LockedAdaptiveRiskAdmissionSnapshot:
            _coverage_unavailable("reservation_ledger_snapshot_missing")
        try:
            ledger.verify()
        except (TypeError, ValueError) as exc:
            raise CapturedAdaptiveRiskCoverageUnavailable(
                "reservation_ledger_snapshot_invalid"
            ) from exc
        self._validate_account_and_ledger(identity, account, ledger, proof)
        captured_account_evidence = self._validate_account_receipt(
            identity,
            account,
            boundary.account_receipt,
            proof,
        )
        if locked_paper_bundle is not None:
            matching_account_reads = tuple(
                row
                for row in proof.read_evidence
                if row.receipt.read_id == boundary.account_receipt.read_id
            )
            if (
                captured_account_evidence.content_sha256
                != locked_paper_bundle.account_payload_sha256
                or len(matching_account_reads) != 1
                or matching_account_reads[0].receipt_sha256
                != locked_paper_bundle.account_read_receipt_sha256
            ):
                _coverage_unavailable(
                    "locked_alpaca_paper_account_capture_mismatch"
                )
        account_evidence = (
            captured_account_evidence
            if identity.execution_surface == "alpaca_paper"
            else RiskInputEvidence(
                source=account.source,
                observed_at=account.observed_at,
                available_at=account.available_at,
                content_sha256=account.snapshot_sha256,
                provider_generation=account.provider_generation,
            )
        )

        bid, ask, bbo_evidence = self._resolve_exact_bbo(
            identity, boundary.exact_bbo, proof
        )
        economics = boundary.economics
        if type(economics) is not CapturedAdaptiveRiskEconomicInputs:
            _coverage_unavailable("economic_inputs_invalid")
        if economics.structural_stop >= ask:
            _coverage_unavailable("structural_stop_not_below_captured_ask")
        derived_payloads = captured_adaptive_risk_fact_payloads(identity, economics)
        if type(boundary.fact_evidence) is not CapturedAdaptiveRiskEvidenceSet:
            _coverage_unavailable("derived_fact_provenance_incomplete")
        fact_evidence = boundary.fact_evidence.as_mapping()
        read_inventory = {row.receipt.read_id: row for row in proof.read_evidence}
        resolved_fact_evidence: dict[str, RiskInputEvidence] = {}
        for fact_name, expected_payload in derived_payloads.items():
            provenance = fact_evidence[fact_name]
            if provenance.content_sha256 != sha256_json(expected_payload):
                _coverage_unavailable(f"{fact_name}_content_mismatch")
            if not set(provenance.source_read_ids).issubset(read_inventory):
                _coverage_unavailable(f"{fact_name}_capture_reads_missing")
            for read_id in provenance.source_read_ids:
                row = read_inventory[read_id]
                if (
                    not row.receipt.content_verified
                    or row.receipt.replay_network_fallback_used
                    or row.receipt.returned_at > identity.decision_at
                    or row.receipt_committed_available_at > identity.decision_at
                ):
                    _coverage_unavailable(f"{fact_name}_capture_read_unusable")
            _validate_freshness(
                observed_at=provenance.observed_at,
                available_at=provenance.available_at,
                decision_at=identity.decision_at,
                max_age_seconds=(
                    self._policy_spec.policy.market_data_max_age_seconds
                    if fact_name in {"structural_stop", "volatility", "liquidity"}
                    else self._policy_spec.policy.context_data_max_age_seconds
                ),
                field=fact_name,
            )
            resolved_fact_evidence[fact_name] = provenance.to_risk_evidence()

        binding = adaptive_risk_capture_binding_from_active_attestation(proof)
        evidence = self._build_evidence(
            identity=identity,
            proof=proof,
            account=account,
            ledger=ledger,
            binding=binding,
            account_evidence=account_evidence,
            daily_pnl_evidence=(
                locked_paper_bundle.daily_pnl_evidence
                if locked_paper_bundle is not None
                else None
            ),
            bbo_evidence=bbo_evidence,
            fact_evidence=resolved_fact_evidence,
        )
        aggregates = ledger.aggregates
        inputs = AdaptiveRiskInputs(
            decision_id=identity.decision_id,
            replay_or_paper_run_id=identity.run_id,
            generation=identity.generation,
            execution_surface=identity.execution_surface,
            execution_family=identity.execution_family,
            venue=identity.venue,
            broker_environment=identity.broker_environment,
            symbol=identity.symbol,
            side="long",
            as_of=identity.decision_at,
            account_identity_sha256=account.account_identity_sha256,
            code_build_sha256=proof.code_build_sha256,
            effective_config_sha256=proof.config_sha256,
            feature_flags_sha256=proof.feature_flags_sha256,
            capture_prefix_root_sha256=proof.input_prefix_root_sha256,
            equity_usd=account.equity_usd,
            buying_power_usd=account.buying_power_usd,
            broker_day_change_usd=account.broker_day_change_usd,
            local_realized_pnl_usd=account.local_realized_pnl_usd,
            open_structural_risk_usd=aggregates["open_structural_risk_usd"],
            pending_reserved_risk_usd=aggregates["pending_reserved_risk_usd"],
            existing_same_symbol_structural_risk_usd=aggregates[
                "existing_same_symbol_structural_risk_usd"
            ],
            pending_same_symbol_structural_risk_usd=aggregates[
                "pending_same_symbol_structural_risk_usd"
            ],
            current_cluster_structural_risk_usd=aggregates[
                "current_cluster_structural_risk_usd"
            ],
            pending_correlation_cluster_risk_usd=aggregates[
                "pending_correlation_cluster_risk_usd"
            ],
            portfolio_gross_notional_usd=aggregates[
                "portfolio_gross_notional_usd"
            ],
            pending_portfolio_gross_notional_usd=aggregates[
                "pending_portfolio_gross_notional_usd"
            ],
            policy_buying_power_capacity_usd=(
                ledger.policy_buying_power_capacity_usd
            ),
            open_buying_power_impact_usd=aggregates[
                "open_buying_power_impact_usd"
            ],
            pending_buying_power_impact_usd=aggregates[
                "pending_buying_power_impact_usd"
            ],
            candidate_buying_power_impact_per_share_usd=(
                economics.candidate_buying_power_impact_per_share_usd
            ),
            bid=bid,
            ask=ask,
            structural_stop=economics.structural_stop,
            entry_slippage_bps=economics.entry_slippage_bps,
            exit_slippage_bps=economics.exit_slippage_bps,
            fees_per_share_usd=economics.fees_per_share_usd,
            setup_quality=economics.setup_quality,
            realized_volatility_fraction=(
                economics.realized_volatility_fraction
            ),
            average_daily_volume_shares=(
                economics.average_daily_volume_shares
            ),
            recent_volume_shares=economics.recent_volume_shares,
            executable_depth_shares=economics.executable_depth_shares,
            correlation_cluster_id=identity.correlation_cluster,
            evidence=evidence,
        )
        source = AdaptiveRiskBuilderSource(
            policy=self._policy_spec.policy,
            inputs=inputs,
            account_snapshot=account,
            capture_binding=binding,
            account_scope=identity.account_scope,
            setup_family=identity.setup_family,
            correlation_cluster=identity.correlation_cluster,
            broker_account_evidence=(
                account_evidence
                if identity.execution_surface == "alpaca_paper"
                else None
            ),
            settled_daily_pnl_evidence=(
                locked_paper_bundle.daily_pnl_evidence
                if locked_paper_bundle is not None
                else None
            ),
        )
        return AdaptiveRiskRuntimeCaptureMaterial(
            source=source,
            active_capture_attestation=proof,
            locked_alpaca_paper_admission_bundle=locked_paper_bundle,
        )

    def _validate_attestation(
        self,
        identity: CapturedAdaptiveRiskDecisionIdentity,
        proof: ActiveCaptureInputPrefixAttestation,
    ) -> None:
        expected = {
            "run_id": (proof.run_id, identity.run_id),
            "generation": (proof.generation, identity.generation),
            "decision_id": (proof.decision_id, identity.decision_id),
            "code_build_sha256": (
                proof.code_build_sha256,
                self._policy_spec.code_build_sha256,
            ),
            "config_sha256": (
                proof.config_sha256,
                self._policy_spec.effective_config_sha256,
            ),
            "feature_flags_sha256": (
                proof.feature_flags_sha256,
                self._policy_spec.feature_flags_sha256,
            ),
        }
        changed = sorted(
            name for name, (actual, required) in expected.items() if actual != required
        )
        if changed:
            _coverage_unavailable("attestation_boundary_mismatch:" + ",".join(changed))
        if not (
            proof.attested_available_at
            <= identity.decision_at
            <= proof.expires_at
        ):
            _coverage_unavailable("active_capture_attestation_stale_or_from_future")
        final_first_dip_fields = (
            proof.first_dip_prior_detector_reference_sha256,
            proof.first_dip_adaptive_request_sha256,
            proof.first_dip_opportunity_key_sha256,
        )
        if any(value is not None for value in final_first_dip_fields):
            _coverage_unavailable("first_dip_final_authority_used_as_generic_source")
        has_first_dip_tape = proof.first_dip_tape_read_id is not None
        if (identity.setup_family == FIRST_DIP_SETUP_FAMILY) != has_first_dip_tape:
            _coverage_unavailable("first_dip_tape_dependency_mismatch")

    def _validate_account_and_ledger(
        self,
        identity: CapturedAdaptiveRiskDecisionIdentity,
        account: ImmutableAccountRiskSnapshot,
        ledger: LockedAdaptiveRiskAdmissionSnapshot,
        proof: ActiveCaptureInputPrefixAttestation,
    ) -> None:
        expected = {
            "account_scope": (account.account_scope, identity.account_scope),
            "execution_family": (
                account.execution_family,
                identity.execution_family,
            ),
            "venue": (account.venue, identity.venue),
            "broker_environment": (
                account.broker_environment,
                identity.broker_environment,
            ),
            "account_identity_sha256": (
                account.account_identity_sha256,
                proof.account_identity_sha256,
            ),
            "ledger_account_scope": (ledger.account_scope, identity.account_scope),
            "ledger_symbol": (ledger.symbol, identity.symbol),
            "ledger_cluster": (
                ledger.correlation_cluster,
                identity.correlation_cluster,
            ),
            "ledger_account_snapshot": (
                ledger.account_snapshot_sha256,
                account.snapshot_sha256,
            ),
        }
        changed = sorted(
            name for name, (actual, required) in expected.items() if actual != required
        )
        if changed:
            _coverage_unavailable("account_or_ledger_boundary_mismatch:" + ",".join(changed))
        _validate_freshness(
            observed_at=account.observed_at,
            available_at=account.available_at,
            decision_at=identity.decision_at,
            max_age_seconds=self._policy_spec.policy.account_data_max_age_seconds,
            field="account_snapshot",
        )
        _validate_freshness(
            observed_at=ledger.observed_at,
            available_at=ledger.observed_at,
            decision_at=identity.decision_at,
            max_age_seconds=self._policy_spec.policy.reservation_data_max_age_seconds,
            field="reservation_ledger",
        )
        if ledger.ledger_payload.get("pending_settlements"):
            _coverage_unavailable("reservation_ledger_pending_settlement")
        if ledger.ledger_payload.get("quarantined_exposures"):
            _coverage_unavailable("reservation_ledger_exposure_quarantined")
        aggregates = ledger.aggregates
        pending_buying_power = float(
            aggregates["pending_buying_power_impact_usd"]
        )
        if (
            pending_buying_power > 1e-9
            or not math.isclose(
                account.pending_policy_buying_power_reflected_usd,
                0.0,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            # Neither the broker account payload nor the reservation ledger can
            # prove whether a non-zero pending order is already reflected in
            # Alpaca's buying-power number.  Preserve the decision-local
            # fail-closed boundary until a typed reflection receipt exists.
            _coverage_unavailable(
                "pending_policy_buying_power_reflection_unavailable"
            )
        expected_capacity = (
            account.buying_power_usd
            + float(aggregates["open_buying_power_impact_usd"])
        )
        if not math.isclose(
            ledger.policy_buying_power_capacity_usd,
            expected_capacity,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            _coverage_unavailable("policy_buying_power_capacity_mismatch")

    def _resolve_exact_bbo(
        self,
        identity: CapturedAdaptiveRiskDecisionIdentity,
        exact_bbo: CapturedExactBbo,
        proof: ActiveCaptureInputPrefixAttestation,
    ) -> tuple[float, float, RiskInputEvidence]:
        if type(exact_bbo) is not CapturedExactBbo:
            _coverage_unavailable("exact_bbo_missing")
        reads = [
            row
            for row in proof.read_evidence
            if row.receipt.read_id == exact_bbo.read_id
        ]
        if len(reads) != 1:
            _coverage_unavailable("bbo_receipt_missing_from_attestation")
        read = reads[0]
        receipt = read.receipt
        if (
            receipt.stream is not CaptureStream.ALPACA_NBBO_QUOTE
            or receipt.provider != _ALPACA_PAPER_NBBO_PROVIDER
            or receipt.symbol != identity.symbol
            or receipt.decision_id != identity.decision_id
            or receipt.identity_sha256 != proof.identity_sha256
            or receipt.empty_result
            or not receipt.content_verified
            or receipt.replay_network_fallback_used
            or receipt.source_event_sha256s
            != (exact_bbo.source_event_sha256,)
        ):
            _coverage_unavailable("bbo_receipt_not_authoritative")
        refs = [
            ref
            for ref in read.source_event_refs
            if ref.event_sha256 == exact_bbo.source_event_sha256
        ]
        if len(refs) != 1:
            _coverage_unavailable("bbo_source_event_missing_from_receipt")
        ref = refs[0]
        if (
            ref.stream is not CaptureStream.ALPACA_NBBO_QUOTE
            or ref.provider != _ALPACA_PAPER_NBBO_PROVIDER
            or ref.symbol != identity.symbol
            or ref.identity_sha256 != proof.identity_sha256
            or ref.provider_event_at is None
            or ref.provider_event_at > identity.decision_at
            or ref.received_at > identity.decision_at
            or ref.available_at > identity.decision_at
            or receipt.returned_at > identity.decision_at
            or read.receipt_committed_available_at > identity.decision_at
        ):
            _coverage_unavailable("bbo_exact_clock_or_identity_unavailable")
        payload = _strict_json_object(exact_bbo.payload_json, "bbo_payload")
        expected_payload_fields = {
            "schema_version",
            "symbol",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "size_unit",
            "feed",
            "provider_event_at",
            "received_at",
            "account_scope",
        }
        if (
            set(payload) != expected_payload_fields
            or payload.get("schema_version")
            != ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION
            or str(payload.get("symbol") or "").strip().upper() != identity.symbol
            or payload.get("account_scope") != ALPACA_PAPER_ACCOUNT_SCOPE
            or payload.get("size_unit") != "shares"
            or sha256_json(payload) != ref.payload_sha256
        ):
            _coverage_unavailable("bbo_payload_content_mismatch")
        feed = str(payload.get("feed") or "").strip()
        if not feed or "iqfeed" in feed.lower():
            _coverage_unavailable("bbo_payload_feed_not_alpaca_authoritative")
        provider_event_at = _exact_utc_payload_clock(
            payload.get("provider_event_at"),
            "bbo_provider_event_at",
        )
        received_at = _exact_utc_payload_clock(
            payload.get("received_at"),
            "bbo_received_at",
        )
        if (
            provider_event_at != ref.provider_event_at
            or received_at != ref.received_at
        ):
            _coverage_unavailable("bbo_payload_clock_mismatch")
        for size_field in ("bid_size", "ask_size"):
            if payload.get(size_field) is not None:
                _finite(payload.get(size_field), size_field, minimum=0.0)
        query = receipt.query
        expected_query_fields = {
            "schema_version",
            "operation",
            "symbol",
            "feed",
            "max_age_seconds",
            "account_scope",
        }
        if (
            not isinstance(query, Mapping)
            or set(query) != expected_query_fields
            or query.get("schema_version")
            != ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION
            or query.get("operation") != "get_execution_bbo"
            or str(query.get("symbol") or "").strip().upper() != identity.symbol
            or query.get("feed") != feed
            or query.get("account_scope") != ALPACA_PAPER_ACCOUNT_SCOPE
            or receipt.query_sha256 != ref.query_sha256
        ):
            _coverage_unavailable("bbo_query_content_mismatch")
        max_age_seconds = _finite(
            query.get("max_age_seconds"),
            "bbo_query_max_age_seconds",
            minimum=0.0,
        )
        if max_age_seconds <= 0:
            _coverage_unavailable("bbo_query_max_age_seconds_invalid")
        bid = _finite(payload.get("bid"), "bid", minimum=0.0)
        ask = _finite(payload.get("ask"), "ask", minimum=0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            _coverage_unavailable("bbo_invalid")
        _validate_freshness(
            observed_at=ref.provider_event_at,
            available_at=ref.available_at,
            decision_at=identity.decision_at,
            max_age_seconds=self._policy_spec.policy.market_data_max_age_seconds,
            field="bbo",
        )
        return (
            bid,
            ask,
            RiskInputEvidence(
                source=f"capture:{ref.provider}:alpaca_nbbo_quote",
                observed_at=ref.provider_event_at,
                available_at=ref.available_at,
                content_sha256=ref.payload_sha256,
                provider_generation=(
                    f"{read.producer_id}:{read.producer_generation}"
                ),
            ),
        )

    def _validate_account_receipt(
        self,
        identity: CapturedAdaptiveRiskDecisionIdentity,
        account: ImmutableAccountRiskSnapshot,
        account_receipt: CapturedAccountRiskReceipt,
        proof: ActiveCaptureInputPrefixAttestation,
    ) -> RiskInputEvidence:
        if type(account_receipt) is not CapturedAccountRiskReceipt:
            _coverage_unavailable("account_receipt_missing")
        reads = [
            row
            for row in proof.read_evidence
            if row.receipt.read_id == account_receipt.read_id
        ]
        if len(reads) != 1:
            _coverage_unavailable("account_receipt_missing_from_attestation")
        read = reads[0]
        receipt = read.receipt
        if (
            receipt.stream is not CaptureStream.ACCOUNT_RISK_SNAPSHOT
            or receipt.symbol is not None
            or receipt.decision_id != identity.decision_id
            or receipt.identity_sha256 != proof.identity_sha256
            or receipt.empty_result
            or not receipt.content_verified
            or receipt.replay_network_fallback_used
            or receipt.provider != ALPACA_PAPER_ACCOUNT_PROVIDER
        ):
            _coverage_unavailable("account_receipt_not_authoritative")
        refs = [
            ref
            for ref in read.source_event_refs
            if ref.event_sha256 == account_receipt.source_event_sha256
        ]
        if len(refs) != 1:
            _coverage_unavailable("account_source_event_missing_from_receipt")
        ref = refs[0]
        if (
            ref.stream is not CaptureStream.ACCOUNT_RISK_SNAPSHOT
            or ref.symbol is not None
            or ref.identity_sha256 != proof.identity_sha256
            or ref.provider != ALPACA_PAPER_ACCOUNT_PROVIDER
            or ref.provider_event_at is not None
            or ref.market_reference_at is None
            or ref.market_reference_at != ref.available_at
            or receipt.returned_at != ref.available_at
            or receipt.requested_at > ref.received_at
            or ref.received_at > ref.available_at
            or ref.available_at > identity.decision_at
            or read.receipt_committed_available_at > identity.decision_at
        ):
            _coverage_unavailable("account_receipt_clock_or_identity_unavailable")
        payload = _strict_json_object(
            account_receipt.payload_json,
            "account_payload",
        )
        if (
            set(payload) != ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS
            or payload.get("schema_version")
            != ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION
            or sha256_json(payload) != ref.payload_sha256
        ):
            _coverage_unavailable("account_payload_content_mismatch")

        try:
            account_id = canonical_alpaca_paper_account_id(payload.get("account_id"))
            expected_account_identity = alpaca_paper_account_identity_sha256(
                account_id
            )
        except AlpacaPaperAccountIdentityError:
            _coverage_unavailable("account_payload_uuid_invalid")
        if (
            payload.get("account_scope") != ALPACA_PAPER_ACCOUNT_SCOPE
            or payload.get("paper") is not True
            or payload.get("status") != ALPACA_PAPER_ACCOUNT_READY_STATUS
            or payload.get("account_blocked") is not False
            or payload.get("trading_blocked") is not False
            or payload.get("trade_suspended_by_user") is not False
            or payload.get("account_identity_sha256")
            != expected_account_identity
            or expected_account_identity != proof.account_identity_sha256
            or expected_account_identity != account.account_identity_sha256
        ):
            _coverage_unavailable("account_payload_identity_or_posture_mismatch")

        equity = _payload_decimal(
            payload.get("equity_usd"),
            "account_equity_usd",
            strictly_positive=True,
        )
        last_equity = _payload_decimal(
            payload.get("last_equity_usd"),
            "account_last_equity_usd",
            strictly_positive=True,
        )
        buying_power = _payload_decimal(
            payload.get("buying_power_usd"),
            "account_buying_power_usd",
            minimum=Decimal("0"),
        )
        if payload.get("cash_usd") is not None:
            _payload_decimal(payload.get("cash_usd"), "account_cash_usd")
        received_at = _exact_utc_payload_clock(
            payload.get("received_at"),
            "account_received_at",
        )
        if received_at != ref.received_at:
            _coverage_unavailable("account_payload_clock_mismatch")

        query = receipt.query
        expected_query = alpaca_paper_account_capture_query(account_id)
        if (
            not isinstance(query, Mapping)
            or set(query) != ALPACA_PAPER_ACCOUNT_QUERY_KEYS
            or query.get("schema_version")
            != expected_query["schema_version"]
            or query.get("operation") != expected_query["operation"]
            or query.get("account_scope") != expected_query["account_scope"]
            or query.get("expected_account_id")
            != expected_query["expected_account_id"]
            or tuple(query.get("fields") or ())
            != ALPACA_PAPER_ACCOUNT_REQUESTED_FIELDS
            or receipt.query_sha256 != sha256_json(expected_query)
            or ref.query_sha256 != receipt.query_sha256
        ):
            _coverage_unavailable("account_query_content_mismatch")

        provider_generation = f"{read.producer_id}:{read.producer_generation}"
        # The flat broker receipt authorizes only the broker-derived fields
        # below.  It does not attest local settled P&L, and this validation must
        # not make that separate supplied field look broker-authoritative.
        if (
            account.source != ALPACA_PAPER_ACCOUNT_PROVIDER
            or account.provider_generation != provider_generation
            or account.observed_at != ref.received_at
            or account.available_at != ref.available_at
            or Decimal(str(account.equity_usd)) != equity
            or Decimal(str(account.buying_power_usd)) != buying_power
            or Decimal(str(account.broker_day_change_usd))
            != equity - last_equity
        ):
            _coverage_unavailable("account_snapshot_broker_fields_mismatch")
        return RiskInputEvidence(
            source=account.source,
            observed_at=account.observed_at,
            available_at=account.available_at,
            content_sha256=ref.payload_sha256,
            provider_generation=provider_generation,
        )

    def _build_evidence(
        self,
        *,
        identity: CapturedAdaptiveRiskDecisionIdentity,
        proof: ActiveCaptureInputPrefixAttestation,
        account: ImmutableAccountRiskSnapshot,
        ledger: LockedAdaptiveRiskAdmissionSnapshot,
        binding: Any,
        account_evidence: RiskInputEvidence,
        daily_pnl_evidence: RiskInputEvidence | None,
        bbo_evidence: RiskInputEvidence,
        fact_evidence: Mapping[str, RiskInputEvidence],
    ) -> dict[str, RiskInputEvidence]:
        if type(account_evidence) is not RiskInputEvidence:
            _coverage_unavailable("broker_account_evidence_invalid")
        if daily_pnl_evidence is None:
            # Sealed replay fixtures created before the live PAPER settlement
            # authority may still carry one composite snapshot.  Live PAPER is
            # never allowed through this compatibility projection.
            if identity.execution_surface == "alpaca_paper":
                _coverage_unavailable("settled_daily_pnl_evidence_missing")
            daily_pnl_evidence = RiskInputEvidence(
                source=account.source,
                observed_at=account.observed_at,
                available_at=account.available_at,
                content_sha256=account.snapshot_sha256,
                provider_generation=account.provider_generation,
            )
        if type(daily_pnl_evidence) is not RiskInputEvidence:
            _coverage_unavailable("settled_daily_pnl_evidence_invalid")
        ledger_evidence = RiskInputEvidence(
            source="postgresql:adaptive_risk_reservations",
            observed_at=ledger.observed_at,
            available_at=ledger.observed_at,
            content_sha256=ledger.ledger_sha256,
            provider_generation=RESERVATION_LEDGER_GENERATION,
        )
        capture_evidence = RiskInputEvidence(
            source="live-replay-capture:active-input-prefix",
            observed_at=binding.observed_at,
            available_at=binding.available_at,
            content_sha256=binding.input_prefix_root_sha256,
            provider_generation=binding.verifier_generation,
        )
        control_generation = (
            f"active-capture:{identity.generation}:"
            f"{self._policy_spec.provenance_sha256}"
        )
        control = {
            "code_build": proof.code_build_sha256,
            "effective_config": proof.config_sha256,
            "feature_flags": proof.feature_flags_sha256,
        }
        result = {
            "account": account_evidence,
            "daily_pnl": daily_pnl_evidence,
            "bbo": bbo_evidence,
            "portfolio_heat": ledger_evidence,
            "reservation_ledger": ledger_evidence,
            "capture_prefix": capture_evidence,
        }
        result.update(fact_evidence)
        for name, content_sha256 in control.items():
            result[name] = RiskInputEvidence(
                source=f"active_capture_attestation:{name}",
                observed_at=proof.attested_available_at,
                available_at=proof.attested_available_at,
                content_sha256=content_sha256,
                provider_generation=control_generation,
            )
        return result


__all__ = [
    "CAPTURED_ADAPTIVE_RISK_FACT_SCHEMA_VERSION",
    "CAPTURED_ADAPTIVE_RISK_POLICY_SPEC_SCHEMA_VERSION",
    "CapturedAdaptiveRiskCoverageUnavailable",
    "CapturedAccountRiskReceipt",
    "CapturedAdaptiveRiskDecisionBoundary",
    "CapturedAdaptiveRiskDecisionIdentity",
    "CapturedAdaptiveRiskEconomicInputs",
    "CapturedAdaptiveRiskEvidenceSet",
    "CapturedAdaptiveRiskFactProvenance",
    "CapturedAdaptiveRiskPolicySpec",
    "CapturedAdaptiveRiskSourceFactory",
    "CapturedExactBbo",
    "captured_adaptive_risk_fact_payloads",
]
