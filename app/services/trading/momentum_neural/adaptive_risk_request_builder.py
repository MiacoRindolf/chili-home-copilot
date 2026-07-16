"""Content-addressed upstream builder for adaptive-risk entry requests.

The reservation stores deliberately do not fetch market/account data.  This
module is the shared boundary immediately upstream of them: a capture producer
supplies one immutable source bundle, the builder reconstructs every typed
input, verifies its content-addressed capture-prefix binding, reruns the pure
resolver, and only then creates the request/packet/claim used by a runner.

The serializable prefix binding in this module is deliberately diagnostic.  Its
digest detects mutation but is not a private verifier attestation.  Alpaca
paper additionally requires the exact process-local HMAC attestation issued by
the active capture runtime; a caller-supplied boolean, JSON payload, or
root-looking SHA is never treated as that proof.

The source bundle is *not* an already-resolved packet or reservation request.
Consequently DB paper, Alpaca paper, and ReplayV3 cannot be manually seeded with
economic quantity and call that runtime parity.  Missing capture provenance is
reported as ``builder_missing_capture_binding`` and never falls back to legacy
dollar/notional sizing.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re
import threading
from typing import Any, Callable, Iterator, Mapping
import uuid
from zoneinfo import ZoneInfo

from .adaptive_risk_policy import (
    AdaptiveRiskContractError,
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    ResolvedAdaptiveRisk,
    RiskInputEvidence,
    load_and_verify_adaptive_risk_decision_packet,
    resolve_adaptive_risk,
)
from .adaptive_risk_reservation import (
    AdaptiveRiskReservationRequest,
    ImmutableAccountRiskSnapshot,
    LockedAlpacaPaperAdmissionBundle,
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
    verify_locked_alpaca_paper_daily_pnl_attestation,
)
from .adaptive_risk_runtime_contract import (
    AdaptiveRiskReservationClaim,
    build_adaptive_risk_reservation_claim,
)
from .replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureStream,
    verify_active_capture_input_attestation,
)


UTC = timezone.utc
BUILDER_SOURCE_SCHEMA_VERSION = "chili.adaptive-risk-builder-source.v1"
CAPTURE_BINDING_SCHEMA_VERSION = (
    "chili.adaptive-risk-capture-prefix-binding.diagnostic.v1"
)
BUILDER_RESULT_SCHEMA_VERSION = "chili.adaptive-risk-builder-result.v1"
KEY_ADAPTIVE_RISK_BUILDER_SOURCE = "adaptive_risk_builder_source"
DB_PAPER_FINAL_ADMISSION_SCHEMA_VERSION = (
    "chili.db-paper-final-admission-observation.v2"
)
DB_PAPER_FINAL_ADMISSION_BUNDLE_SCHEMA_VERSION = (
    "chili.db-paper-final-admission-bundle.v2"
)
DB_PAPER_FINAL_ADMISSION_MATERIAL_SCHEMA_VERSION = (
    "chili.db-paper-final-admission-material.v1"
)
DB_PAPER_ADMISSION_RECEIPT_SCHEMA_VERSION = (
    "chili.db-paper-admission-receipt.v2"
)
DB_PAPER_EXECUTABLE_ADMISSION_SCHEMA_VERSION = (
    "chili.db-paper-executable-admission.v1"
)
KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT = "adaptive_risk_final_admission_receipt"
KEY_DB_PAPER_EXECUTABLE_ADMISSION = "adaptive_risk_executable_admission"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ET = ZoneInfo("America/New_York")


class AdaptiveRiskBuilderError(AdaptiveRiskContractError):
    """Fail-closed builder rejection with one stable machine-readable reason."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = str(reason or "adaptive_risk_builder_invalid")
        self.detail = str(detail or "")
        super().__init__(
            self.reason if not self.detail else f"{self.reason}: {self.detail}"
        )


FIRST_DIP_SETUP_FAMILY = "first_dip_reclaim"
FIRST_DIP_OWNERSHIP_MARKER = "first_dip_day_leg"


def resolve_detector_setup_family(
    debug: Mapping[str, Any] | None,
    *,
    fallback_setup_family: Any,
    expected_symbol: Any,
) -> str:
    """Resolve setup identity without allowing first-dip ownership downgrade.

    The detector's structural ownership marker and its once-per-day opportunity
    key are one bidirectional identity.  Persisting only one side is not a
    generic fallback: it is an incomplete boundary object and must fail before
    reservation or order construction.
    """

    payload = dict(debug) if isinstance(debug, Mapping) else {}
    marker = str(payload.get("front_side_via") or "").strip().lower()
    raw_opportunity = payload.get("opportunity_key")
    opportunity = (
        dict(raw_opportunity) if isinstance(raw_opportunity, Mapping) else {}
    )
    setup_family = str(
        opportunity.get("setup_family") or ""
    ).strip().lower()
    marker_claims_first_dip = marker == FIRST_DIP_OWNERSHIP_MARKER
    opportunity_claims_first_dip = setup_family == FIRST_DIP_SETUP_FAMILY

    if marker_claims_first_dip and not isinstance(raw_opportunity, Mapping):
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_boundary_mismatch",
            "first_dip_opportunity_key_missing",
        )
    if marker_claims_first_dip != opportunity_claims_first_dip:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_boundary_mismatch",
            "first_dip_marker_opportunity_mismatch",
        )

    if opportunity_claims_first_dip:
        symbol = str(opportunity.get("symbol") or "").strip().upper()
        expected = str(expected_symbol or "").strip().upper()
        if not expected or symbol != expected:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch",
                "first_dip_opportunity_symbol_mismatch",
            )
        trading_date = str(
            opportunity.get("trading_date") or ""
        ).strip()
        try:
            parsed_date = date.fromisoformat(trading_date)
        except ValueError as exc:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch",
                "first_dip_opportunity_trading_date_invalid",
            ) from exc
        if parsed_date.isoformat() != trading_date:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch",
                "first_dip_opportunity_trading_date_invalid",
            )
        return FIRST_DIP_SETUP_FAMILY

    if setup_family:
        return setup_family
    return str(fallback_setup_family or "").strip().lower()


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AdaptiveRiskBuilderError("builder_clock_invalid", field)
    return value.astimezone(UTC)


def _parse_utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field)
    if not isinstance(value, str) or not value.strip():
        raise AdaptiveRiskBuilderError("builder_clock_invalid", field)
    try:
        return _utc(
            datetime.fromisoformat(value.strip().replace("Z", "+00:00")), field
        )
    except ValueError as exc:
        raise AdaptiveRiskBuilderError("builder_clock_invalid", field) from exc


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value, "datetime").isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_noncanonical", str(exc)
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _json_safe(value: Any) -> Any:
    return json.loads(_canonical_json(value).decode("utf-8"))


def _is_sha256(value: Any) -> bool:
    return _SHA256_RE.fullmatch(str(value or "").strip().lower()) is not None


@dataclass(frozen=True)
class AdaptiveRiskDiagnosticCaptureBinding:
    """Content-addressed decision-prefix identity for diagnostic parity only.

    ``content_sha256`` protects serialization integrity; it is an ordinary
    unkeyed digest and is intentionally *not* named or treated as attestation.
    The fixed verification scope prevents a persisted payload from claiming
    runtime authority.  Replay may use this to prove deterministic packet
    reconstruction after its independent private-attested sealed-run checks;
    DB paper may use it diagnostically.  Alpaca order authorization may not.
    """

    schema_version: str
    run_id: str
    generation: int
    decision_id: str
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    identity_sha256: str
    observed_at: datetime
    available_at: datetime
    verifier_generation: str
    verification_scope: str
    content_sha256: str

    @classmethod
    def create_diagnostic(
        cls,
        *,
        run_id: str,
        generation: int,
        decision_id: str,
        input_prefix_sequence: int,
        input_prefix_root_sha256: str,
        identity_sha256: str,
        observed_at: datetime,
        available_at: datetime,
        verifier_generation: str,
    ) -> "AdaptiveRiskDiagnosticCaptureBinding":
        body = {
            "schema_version": CAPTURE_BINDING_SCHEMA_VERSION,
            "run_id": run_id,
            "generation": generation,
            "decision_id": decision_id,
            "input_prefix_sequence": input_prefix_sequence,
            "input_prefix_root_sha256": input_prefix_root_sha256,
            "identity_sha256": identity_sha256,
            "observed_at": observed_at,
            "available_at": available_at,
            "verifier_generation": verifier_generation,
            "verification_scope": "diagnostic_only",
        }
        return cls(**body, content_sha256=_sha256_json(body))

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_BINDING_SCHEMA_VERSION:
            raise AdaptiveRiskBuilderError("builder_capture_binding_schema_invalid")
        for name in ("run_id", "decision_id", "verifier_generation"):
            if not str(getattr(self, name) or "").strip():
                raise AdaptiveRiskBuilderError(
                    "builder_capture_binding_invalid", f"{name}_missing"
                )
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise AdaptiveRiskBuilderError(
                "builder_capture_binding_invalid", "generation_invalid"
            )
        if (
            isinstance(self.input_prefix_sequence, bool)
            or int(self.input_prefix_sequence) <= 0
        ):
            raise AdaptiveRiskBuilderError(
                "builder_capture_binding_invalid", "prefix_sequence_invalid"
            )
        for name in (
            "input_prefix_root_sha256",
            "identity_sha256",
            "content_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if not _is_sha256(value):
                raise AdaptiveRiskBuilderError(
                    "builder_capture_binding_invalid", f"{name}_invalid"
                )
            object.__setattr__(self, name, value)
        observed = _utc(self.observed_at, "capture_binding.observed_at")
        available = _utc(self.available_at, "capture_binding.available_at")
        if available < observed:
            raise AdaptiveRiskBuilderError(
                "builder_capture_binding_invalid", "availability_precedes_observation"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        if self.verification_scope != "diagnostic_only":
            raise AdaptiveRiskBuilderError(
                "builder_capture_binding_trust_scope_invalid"
            )
        expected_content_sha = _sha256_json(self.body_without_content_sha())
        if self.content_sha256 != expected_content_sha:
            raise AdaptiveRiskBuilderError(
                "builder_capture_binding_content_mismatch"
            )

    def body_without_content_sha(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("content_sha256", None)
        return body


@dataclass(frozen=True)
class AdaptiveRiskBuilderSource:
    """Raw typed decision sources; contains no resolved quantity/notional."""

    policy: AdaptiveRiskPolicy
    inputs: AdaptiveRiskInputs
    account_snapshot: ImmutableAccountRiskSnapshot
    capture_binding: AdaptiveRiskDiagnosticCaptureBinding
    account_scope: str
    setup_family: str
    correlation_cluster: str
    broker_account_evidence: RiskInputEvidence | None = None
    settled_daily_pnl_evidence: RiskInputEvidence | None = None

    def __post_init__(self) -> None:
        for name in ("account_scope", "setup_family", "correlation_cluster"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_source_invalid", f"{name}_missing"
                )
            if name != "account_scope":
                value = value.lower()
            object.__setattr__(self, name, value)
        for name in (
            "broker_account_evidence",
            "settled_daily_pnl_evidence",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not RiskInputEvidence:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_source_invalid", f"{name}_invalid"
                )

    def _body(self) -> dict[str, Any]:
        body = {
            "schema_version": BUILDER_SOURCE_SCHEMA_VERSION,
            "policy": asdict(self.policy),
            "inputs": asdict(self.inputs),
            "account_snapshot": self.account_snapshot.to_payload(),
            "capture_binding": asdict(self.capture_binding),
            "account_scope": self.account_scope,
            "setup_family": self.setup_family,
            "correlation_cluster": self.correlation_cluster,
        }
        if self.broker_account_evidence is not None:
            body["broker_account_evidence"] = asdict(
                self.broker_account_evidence
            )
        if self.settled_daily_pnl_evidence is not None:
            body["settled_daily_pnl_evidence"] = asdict(
                self.settled_daily_pnl_evidence
            )
        return body

    @property
    def source_sha256(self) -> str:
        return _sha256_json(self._body())

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(self._body())
        payload["source_sha256"] = self.source_sha256
        return payload


def db_paper_admission_component_sha256(payload: Mapping[str, Any]) -> str:
    """Return the canonical digest a capture producer must place on final facts.

    This intentionally accepts only a mapping so a producer and the paper
    consumer cannot disagree about whether an arbitrary scalar/list was the
    thing being attested.  The payload builders below are the public schema for
    the three final-boundary facts.
    """

    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_component_invalid", "mapping_required"
        )
    return _sha256_json(dict(payload))


def db_paper_bbo_evidence_payload(
    *,
    symbol: str,
    bid: float,
    ask: float,
    quote_source: str,
    observed_at: datetime,
    available_at: datetime,
    provider_generation: str,
) -> dict[str, Any]:
    return {
        "schema_version": "chili.db-paper-final-bbo.v1",
        "symbol": str(symbol or "").strip().upper(),
        "bid": float(bid),
        "ask": float(ask),
        "quote_source": str(quote_source or "").strip(),
        "observed_at": _utc(observed_at, "bbo.observed_at"),
        "available_at": _utc(available_at, "bbo.available_at"),
        "provider_generation": str(provider_generation or "").strip(),
    }


def db_paper_eligibility_evidence_payload(
    *,
    symbol: str,
    viability_id: int,
    variant_id: int,
    viability_score: float,
    paper_eligible: bool,
    observed_at: datetime,
    available_at: datetime,
    row_updated_at: datetime,
    execution_readiness: Mapping[str, Any],
    source: str,
    provider_generation: str,
) -> dict[str, Any]:
    return {
        "schema_version": "chili.db-paper-final-eligibility.v1",
        "symbol": str(symbol or "").strip().upper(),
        "viability_id": int(viability_id),
        "variant_id": int(variant_id),
        "viability_score": float(viability_score),
        "paper_eligible": bool(paper_eligible),
        "observed_at": _utc(observed_at, "eligibility.observed_at"),
        "available_at": _utc(available_at, "eligibility.available_at"),
        "row_updated_at": _utc(row_updated_at, "eligibility.row_updated_at"),
        "execution_readiness": _json_safe(dict(execution_readiness)),
        "source": str(source or "").strip(),
        "provider_generation": str(provider_generation or "").strip(),
    }


def db_paper_entry_gate_evidence_payload(
    *,
    symbol: str,
    allowed: bool,
    reason: str,
    debug: Mapping[str, Any],
    structural_stop: float,
    setup_family: str,
    opportunity_key: Mapping[str, Any],
    observed_at: datetime,
    available_at: datetime,
    source: str,
    provider_generation: str,
) -> dict[str, Any]:
    return {
        "schema_version": "chili.db-paper-final-entry-gate.v1",
        "symbol": str(symbol or "").strip().upper(),
        "allowed": bool(allowed),
        "reason": str(reason or "").strip(),
        "debug": _json_safe(dict(debug)),
        "structural_stop": float(structural_stop),
        "setup_family": str(setup_family or "").strip().lower(),
        "opportunity_key": _json_safe(dict(opportunity_key)),
        "observed_at": _utc(observed_at, "entry_gate.observed_at"),
        "available_at": _utc(available_at, "entry_gate.available_at"),
        "source": str(source or "").strip(),
        "provider_generation": str(provider_generation or "").strip(),
    }


def _require_final_evidence_age(
    *,
    name: str,
    evidence: RiskInputEvidence,
    decision_at: datetime,
    max_age_seconds: float,
) -> None:
    if evidence.available_at > decision_at:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_evidence_from_future", name
        )
    observed_age = (decision_at - evidence.observed_at).total_seconds()
    available_age = (decision_at - evidence.available_at).total_seconds()
    if observed_age < 0 or available_age < 0:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_evidence_from_future", name
        )
    if observed_age > max_age_seconds or available_age > max_age_seconds:
        raise AdaptiveRiskBuilderError("db_paper_final_evidence_stale", name)


def _normalize_db_paper_opportunity_key(
    source: AdaptiveRiskBuilderSource,
    opportunity_key: Mapping[str, Any] | None,
) -> dict[str, Any]:
    supplied = dict(opportunity_key or {})
    expected = {
        "account_scope": source.account_scope,
        "symbol": source.inputs.symbol,
        "trading_date": source.inputs.as_of.astimezone(ET).date().isoformat(),
        "setup_family": source.setup_family,
    }
    if source.setup_family == "first_dip_reclaim" and not supplied:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_opportunity_missing", source.setup_family
        )
    normalized = {
        "account_scope": str(
            supplied.get("account_scope") or source.account_scope
        ).strip(),
        "symbol": str(supplied.get("symbol") or source.inputs.symbol)
        .strip()
        .upper(),
        "trading_date": str(
            supplied.get("trading_date") or expected["trading_date"]
        ).strip(),
        "setup_family": str(
            supplied.get("setup_family") or source.setup_family
        )
        .strip()
        .lower(),
    }
    if normalized != expected:
        changed = sorted(
            name for name in expected if normalized.get(name) != expected[name]
        )
        raise AdaptiveRiskBuilderError(
            "db_paper_final_opportunity_mismatch", ",".join(changed)
        )
    return expected


def db_paper_execution_terms_payload(
    *,
    effective_config_sha256: str,
    stop_atr_mult: float,
    target_atr_mult: float,
    vol_floor_mult: float,
    reward_risk: float,
    entry_slippage_bps: float,
    exit_slippage_bps: float,
    fee_to_target_ratio: float,
) -> dict[str, Any]:
    values = {
        "stop_atr_mult": float(stop_atr_mult),
        "target_atr_mult": float(target_atr_mult),
        "vol_floor_mult": float(vol_floor_mult),
        "reward_risk": float(reward_risk),
        "entry_slippage_bps": float(entry_slippage_bps),
        "exit_slippage_bps": float(exit_slippage_bps),
        "fee_to_target_ratio": float(fee_to_target_ratio),
    }
    if (
        not _is_sha256(effective_config_sha256)
        or any(not math.isfinite(value) for value in values.values())
        or values["stop_atr_mult"] <= 0
        or values["vol_floor_mult"] <= 0
        or values["reward_risk"] <= 0
        or values["entry_slippage_bps"] < 0
        or values["exit_slippage_bps"] < 0
        or values["fee_to_target_ratio"] < 0
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_execution_terms_invalid"
        )
    return {
        "schema_version": "chili.db-paper-final-execution-terms.v1",
        "effective_config_sha256": str(effective_config_sha256).lower(),
        **values,
    }


def _normalize_execution_terms(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_final_execution_terms_missing")
    raw = dict(value)
    if raw.get("schema_version") != "chili.db-paper-final-execution-terms.v1":
        raise AdaptiveRiskBuilderError("db_paper_final_execution_terms_invalid")
    normalized = db_paper_execution_terms_payload(
        effective_config_sha256=raw.get("effective_config_sha256"),
        stop_atr_mult=raw.get("stop_atr_mult"),
        target_atr_mult=raw.get("target_atr_mult"),
        vol_floor_mult=raw.get("vol_floor_mult"),
        reward_risk=raw.get("reward_risk"),
        entry_slippage_bps=raw.get("entry_slippage_bps"),
        exit_slippage_bps=raw.get("exit_slippage_bps"),
        fee_to_target_ratio=raw.get("fee_to_target_ratio"),
    )
    if _canonical_json(raw) != _canonical_json(normalized):
        raise AdaptiveRiskBuilderError("db_paper_final_execution_terms_invalid")
    return normalized


@dataclass(frozen=True)
class DbPaperFinalAdmissionMaterial:
    """Capture-owned immutable market/gate/account material, before DB risk lock."""

    schema_version: str
    source: AdaptiveRiskBuilderSource
    quote_source: str
    gate_allowed: bool
    gate_reason: str
    gate_debug: Mapping[str, Any]
    opportunity_key: Mapping[str, Any]
    eligibility: Mapping[str, Any]
    execution_terms: Mapping[str, Any]
    content_sha256: str
    # Process-local evidence capabilities are deliberately excluded from every
    # payload/hash. A mapping or a deserialize/reload can preserve audit facts,
    # but can never reconstruct the credential required by first-dip admission.
    _first_dip_final_admission_envelope: object = field(
        default=None, repr=False, compare=False
    )
    _first_dip_final_admission_expectation: object = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def create(
        cls,
        source: AdaptiveRiskBuilderSource,
        *,
        quote_source: str,
        gate_allowed: bool,
        gate_reason: str,
        gate_debug: Mapping[str, Any],
        opportunity_key: Mapping[str, Any] | None,
        eligibility: Mapping[str, Any],
        execution_terms: Mapping[str, Any],
        first_dip_final_admission_envelope: object = None,
        first_dip_final_admission_expectation: object = None,
    ) -> "DbPaperFinalAdmissionMaterial":
        quote = str(quote_source or "").strip()
        if not quote or not isinstance(gate_debug, Mapping):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_material_invalid", "quote_or_gate"
            )
        normalized_opportunity = _normalize_db_paper_opportunity_key(
            source, opportunity_key
        )
        normalized_eligibility = _json_safe(dict(eligibility))
        normalized_terms = _normalize_execution_terms(execution_terms)
        if normalized_terms["effective_config_sha256"] != (
            source.inputs.effective_config_sha256
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_material_mismatch", "effective_config"
            )
        eligibility_evidence = source.inputs.evidence.get("paper_eligibility")
        if (
            not isinstance(eligibility_evidence, RiskInputEvidence)
            or eligibility_evidence.content_sha256
            != db_paper_admission_component_sha256(normalized_eligibility)
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_material_mismatch", "eligibility"
            )
        body = {
            "schema_version": DB_PAPER_FINAL_ADMISSION_MATERIAL_SCHEMA_VERSION,
            "source": source.to_payload(),
            "quote_source": quote,
            "gate_allowed": bool(gate_allowed),
            "gate_reason": str(gate_reason or "").strip(),
            "gate_debug": _json_safe(dict(gate_debug)),
            "opportunity_key": normalized_opportunity,
            "eligibility": normalized_eligibility,
            "execution_terms": normalized_terms,
        }
        return cls(
            schema_version=DB_PAPER_FINAL_ADMISSION_MATERIAL_SCHEMA_VERSION,
            source=source,
            quote_source=quote,
            gate_allowed=bool(gate_allowed),
            gate_reason=str(gate_reason or "").strip(),
            gate_debug=_json_safe(dict(gate_debug)),
            opportunity_key=normalized_opportunity,
            eligibility=normalized_eligibility,
            execution_terms=normalized_terms,
            content_sha256=_sha256_json(body),
            _first_dip_final_admission_envelope=(
                first_dip_final_admission_envelope
            ),
            _first_dip_final_admission_expectation=(
                first_dip_final_admission_expectation
            ),
        )

    def body_without_content_sha(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source.to_payload(),
            "quote_source": self.quote_source,
            "gate_allowed": self.gate_allowed,
            "gate_reason": self.gate_reason,
            "gate_debug": _json_safe(dict(self.gate_debug)),
            "opportunity_key": _json_safe(dict(self.opportunity_key)),
            "eligibility": _json_safe(dict(self.eligibility)),
            "execution_terms": _json_safe(dict(self.execution_terms)),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(self.body_without_content_sha())
        payload["content_sha256"] = self.content_sha256
        return payload


def load_db_paper_final_admission_material(
    payload: Mapping[str, Any],
) -> DbPaperFinalAdmissionMaterial:
    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_final_material_missing")
    raw = dict(payload)
    supplied_sha = str(raw.pop("content_sha256", "") or "").strip().lower()
    if raw.get("schema_version") != DB_PAPER_FINAL_ADMISSION_MATERIAL_SCHEMA_VERSION:
        raise AdaptiveRiskBuilderError("db_paper_final_material_schema_invalid")
    source_payload = raw.get("source")
    if not isinstance(source_payload, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_final_material_invalid", "source")
    material = DbPaperFinalAdmissionMaterial.create(
        load_adaptive_risk_builder_source(source_payload),
        quote_source=raw.get("quote_source"),
        gate_allowed=raw.get("gate_allowed") is True,
        gate_reason=raw.get("gate_reason"),
        gate_debug=raw.get("gate_debug") if isinstance(raw.get("gate_debug"), Mapping) else {},
        opportunity_key=raw.get("opportunity_key") if isinstance(raw.get("opportunity_key"), Mapping) else None,
        eligibility=raw.get("eligibility") if isinstance(raw.get("eligibility"), Mapping) else {},
        execution_terms=raw.get("execution_terms") if isinstance(raw.get("execution_terms"), Mapping) else {},
    )
    if not _is_sha256(supplied_sha) or supplied_sha != material.content_sha256:
        raise AdaptiveRiskBuilderError("db_paper_final_material_hash_mismatch")
    if _canonical_json(payload) != _canonical_json(material.to_payload()):
        raise AdaptiveRiskBuilderError("db_paper_final_material_canonical_mismatch")
    return material


@dataclass(frozen=True)
class DbPaperFinalAdmissionBundle:
    """The sole post-lock immutable source for DB-paper entry economics."""

    schema_version: str
    material_sha256: str
    source: AdaptiveRiskBuilderSource
    quote_source: str
    gate_allowed: bool
    gate_reason: str
    gate_debug: Mapping[str, Any]
    opportunity_key: Mapping[str, Any]
    eligibility: Mapping[str, Any]
    execution_terms: Mapping[str, Any]
    locked_risk_snapshot: Mapping[str, Any]
    content_sha256: str

    @classmethod
    def create(
        cls,
        material: DbPaperFinalAdmissionMaterial,
        source: AdaptiveRiskBuilderSource,
        *,
        locked_risk_snapshot: LockedAdaptiveRiskAdmissionSnapshot,
    ) -> "DbPaperFinalAdmissionBundle":
        if not isinstance(material, DbPaperFinalAdmissionMaterial) or not isinstance(
            locked_risk_snapshot, LockedAdaptiveRiskAdmissionSnapshot
        ):
            raise AdaptiveRiskBuilderError("db_paper_final_bundle_invalid")
        if (
            source.account_scope != locked_risk_snapshot.account_scope
            or source.inputs.symbol != locked_risk_snapshot.symbol
            or source.correlation_cluster
            != locked_risk_snapshot.correlation_cluster
            or source.account_snapshot != material.source.account_snapshot
            or source.policy != material.source.policy
            or source.capture_binding != material.source.capture_binding
            or source.inputs.effective_config_sha256
            != material.source.inputs.effective_config_sha256
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_bundle_mismatch", "identity_or_provenance"
            )
        aggregates = locked_risk_snapshot.aggregates
        exact_risk_values = {
            "as_of": locked_risk_snapshot.observed_at,
            "open_structural_risk_usd": aggregates["open_structural_risk_usd"],
            "pending_reserved_risk_usd": aggregates["pending_reserved_risk_usd"],
            "existing_same_symbol_structural_risk_usd": aggregates[
                "existing_same_symbol_structural_risk_usd"
            ],
            "pending_same_symbol_structural_risk_usd": aggregates[
                "pending_same_symbol_structural_risk_usd"
            ],
            "current_cluster_structural_risk_usd": aggregates[
                "current_cluster_structural_risk_usd"
            ],
            "pending_correlation_cluster_risk_usd": aggregates[
                "pending_correlation_cluster_risk_usd"
            ],
            "portfolio_gross_notional_usd": aggregates[
                "portfolio_gross_notional_usd"
            ],
            "pending_portfolio_gross_notional_usd": aggregates[
                "pending_portfolio_gross_notional_usd"
            ],
            "policy_buying_power_capacity_usd": (
                locked_risk_snapshot.policy_buying_power_capacity_usd
            ),
            "open_buying_power_impact_usd": aggregates[
                "open_buying_power_impact_usd"
            ],
            "pending_buying_power_impact_usd": aggregates[
                "pending_buying_power_impact_usd"
            ],
        }
        changed_risk = sorted(
            name
            for name, expected in exact_risk_values.items()
            if getattr(source.inputs, name) != expected
        )
        expected_ledger_evidence = RiskInputEvidence(
            source="postgresql:adaptive_risk_reservations",
            observed_at=locked_risk_snapshot.observed_at,
            available_at=locked_risk_snapshot.observed_at,
            content_sha256=locked_risk_snapshot.ledger_sha256,
            provider_generation=RESERVATION_LEDGER_GENERATION,
        )
        if source.inputs.evidence.get(
            "reservation_ledger"
        ) != expected_ledger_evidence:
            changed_risk.append("reservation_ledger_evidence")
        if changed_risk:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_bundle_mismatch",
                "locked_risk:" + ",".join(changed_risk),
            )
        opportunity = _normalize_db_paper_opportunity_key(
            source, material.opportunity_key
        )
        body = {
            "schema_version": DB_PAPER_FINAL_ADMISSION_BUNDLE_SCHEMA_VERSION,
            "material_sha256": material.content_sha256,
            "source": source.to_payload(),
            "quote_source": material.quote_source,
            "gate_allowed": material.gate_allowed,
            "gate_reason": material.gate_reason,
            "gate_debug": _json_safe(dict(material.gate_debug)),
            "opportunity_key": opportunity,
            "eligibility": _json_safe(dict(material.eligibility)),
            "execution_terms": _json_safe(dict(material.execution_terms)),
            "locked_risk_snapshot": locked_risk_snapshot.to_payload(),
        }
        return cls(
            schema_version=DB_PAPER_FINAL_ADMISSION_BUNDLE_SCHEMA_VERSION,
            material_sha256=material.content_sha256,
            source=source,
            quote_source=material.quote_source,
            gate_allowed=material.gate_allowed,
            gate_reason=material.gate_reason,
            gate_debug=_json_safe(dict(material.gate_debug)),
            opportunity_key=opportunity,
            eligibility=_json_safe(dict(material.eligibility)),
            execution_terms=_json_safe(dict(material.execution_terms)),
            locked_risk_snapshot=locked_risk_snapshot.to_payload(),
            content_sha256=_sha256_json(body),
        )

    def body_without_content_sha(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "material_sha256": self.material_sha256,
            "source": self.source.to_payload(),
            "quote_source": self.quote_source,
            "gate_allowed": self.gate_allowed,
            "gate_reason": self.gate_reason,
            "gate_debug": _json_safe(dict(self.gate_debug)),
            "opportunity_key": _json_safe(dict(self.opportunity_key)),
            "eligibility": _json_safe(dict(self.eligibility)),
            "execution_terms": _json_safe(dict(self.execution_terms)),
            "locked_risk_snapshot": _json_safe(dict(self.locked_risk_snapshot)),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(self.body_without_content_sha())
        payload["content_sha256"] = self.content_sha256
        return payload


def load_db_paper_final_admission_bundle(
    payload: Mapping[str, Any],
) -> DbPaperFinalAdmissionBundle:
    """Reload and rehash a finalized bundle before reservation or fill use."""

    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_final_bundle_missing")
    raw = dict(payload)
    supplied_sha = str(raw.pop("content_sha256", "") or "").strip().lower()
    if raw.get("schema_version") != DB_PAPER_FINAL_ADMISSION_BUNDLE_SCHEMA_VERSION:
        raise AdaptiveRiskBuilderError("db_paper_final_bundle_schema_invalid")
    source_payload = raw.get("source")
    locked_payload = raw.get("locked_risk_snapshot")
    if not isinstance(source_payload, Mapping) or not isinstance(
        locked_payload, Mapping
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_invalid", "nested_source_or_lock"
        )
    source = load_adaptive_risk_builder_source(source_payload)
    locked_values = dict(locked_payload)
    locked_values["observed_at"] = _parse_utc(
        locked_values.get("observed_at"), "locked_snapshot.observed_at"
    )
    try:
        locked = LockedAdaptiveRiskAdmissionSnapshot(**locked_values)
    except (TypeError, ValueError, AdaptiveRiskContractError) as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_invalid", "locked_risk_snapshot"
        ) from exc
    if _canonical_json(locked_payload) != _canonical_json(locked.to_payload()):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_canonical_mismatch", "locked_risk_snapshot"
        )
    try:
        bundle = DbPaperFinalAdmissionBundle(
            schema_version=raw["schema_version"],
            material_sha256=str(raw["material_sha256"]),
            source=source,
            quote_source=str(raw["quote_source"]),
            gate_allowed=raw["gate_allowed"] is True,
            gate_reason=str(raw["gate_reason"]),
            gate_debug=_json_safe(dict(raw["gate_debug"])),
            opportunity_key=_json_safe(dict(raw["opportunity_key"])),
            eligibility=_json_safe(dict(raw["eligibility"])),
            execution_terms=_normalize_execution_terms(
                raw["execution_terms"]
            ),
            locked_risk_snapshot=locked.to_payload(),
            content_sha256=supplied_sha,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError("db_paper_final_bundle_invalid") from exc
    if not _is_sha256(bundle.material_sha256) or not _is_sha256(supplied_sha):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_invalid", "sha256"
        )
    if (
        source.account_scope != locked.account_scope
        or source.inputs.symbol != locked.symbol
        or source.correlation_cluster != locked.correlation_cluster
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_mismatch", "locked_identity"
        )
    expected_values = {
        "as_of": locked.observed_at,
        "open_structural_risk_usd": locked.aggregates[
            "open_structural_risk_usd"
        ],
        "pending_reserved_risk_usd": locked.aggregates[
            "pending_reserved_risk_usd"
        ],
        "existing_same_symbol_structural_risk_usd": locked.aggregates[
            "existing_same_symbol_structural_risk_usd"
        ],
        "pending_same_symbol_structural_risk_usd": locked.aggregates[
            "pending_same_symbol_structural_risk_usd"
        ],
        "current_cluster_structural_risk_usd": locked.aggregates[
            "current_cluster_structural_risk_usd"
        ],
        "pending_correlation_cluster_risk_usd": locked.aggregates[
            "pending_correlation_cluster_risk_usd"
        ],
        "portfolio_gross_notional_usd": locked.aggregates[
            "portfolio_gross_notional_usd"
        ],
        "pending_portfolio_gross_notional_usd": locked.aggregates[
            "pending_portfolio_gross_notional_usd"
        ],
        "policy_buying_power_capacity_usd": (
            locked.policy_buying_power_capacity_usd
        ),
        "open_buying_power_impact_usd": locked.aggregates[
            "open_buying_power_impact_usd"
        ],
        "pending_buying_power_impact_usd": locked.aggregates[
            "pending_buying_power_impact_usd"
        ],
    }
    changed = sorted(
        name
        for name, expected in expected_values.items()
        if getattr(source.inputs, name) != expected
    )
    ledger_evidence = source.inputs.evidence.get("reservation_ledger")
    if (
        not isinstance(ledger_evidence, RiskInputEvidence)
        or ledger_evidence.content_sha256 != locked.ledger_sha256
        or ledger_evidence.observed_at != locked.observed_at
        or ledger_evidence.available_at != locked.observed_at
    ):
        changed.append("reservation_ledger_evidence")
    if changed:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_mismatch", ",".join(changed)
        )
    if bundle.content_sha256 != _sha256_json(bundle.body_without_content_sha()):
        raise AdaptiveRiskBuilderError("db_paper_final_bundle_hash_mismatch")
    if _canonical_json(payload) != _canonical_json(bundle.to_payload()):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_bundle_canonical_mismatch"
        )
    return bundle


@dataclass(frozen=True)
class DbPaperFinalAdmissionObservation:
    """Exact causal facts re-read immediately before reservation + fill.

    The ordinary adaptive source already binds the complete risk packet.  This
    additional object proves that DB paper did not reuse the detector's earlier
    tape/eligibility result: final BBO, final eligibility, and the final entry
    gate each have exact event/available clocks and a capture-produced digest.
    """

    schema_version: str
    source_sha256: str
    decision_at: datetime
    symbol: str
    setup_family: str
    opportunity_key: Mapping[str, Any]
    opportunity_sha256: str
    bbo_content_sha256: str
    eligibility_content_sha256: str
    entry_gate_content_sha256: str
    first_dip_final_admission_envelope_sha256: str | None
    content_sha256: str

    @classmethod
    def create(
        cls,
        source: AdaptiveRiskBuilderSource,
        *,
        decision_at: datetime,
        bid: float,
        ask: float,
        quote_source: str,
        viability_id: int,
        variant_id: int,
        viability_score: float,
        paper_eligible: bool,
        eligibility_observed_at: datetime,
        eligibility_available_at: datetime,
        eligibility_row_updated_at: datetime,
        execution_readiness: Mapping[str, Any],
        gate_allowed: bool,
        gate_reason: str,
        gate_debug: Mapping[str, Any],
        structural_stop: float,
        opportunity_key: Mapping[str, Any] | None,
        first_dip_final_admission_envelope: object = None,
        first_dip_final_admission_expectation: object = None,
    ) -> "DbPaperFinalAdmissionObservation":
        decision_clock = _utc(decision_at, "decision_at")
        if source.inputs.execution_surface != "db_paper":
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "execution_surface"
            )
        if source.inputs.broker_environment != "paper":
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "broker_environment"
            )
        if source.inputs.as_of != decision_clock:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "decision_clock"
            )
        finite_values = (bid, ask, viability_score, structural_stop)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in finite_values
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_invalid", "nonfinite"
            )
        if not gate_allowed:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_entry_gate_veto", str(gate_reason or "unknown")
            )
        if not paper_eligible:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_eligibility_veto", "paper_eligible_false"
            )
        if source.inputs.symbol != str(source.inputs.symbol).strip().upper():
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "symbol"
            )
        if abs(source.inputs.bid - float(bid)) > 1e-9:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "bid"
            )
        if abs(source.inputs.ask - float(ask)) > 1e-9:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "ask"
            )
        if abs(source.inputs.structural_stop - float(structural_stop)) > 1e-9:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "structural_stop"
            )

        normalized_opportunity = _normalize_db_paper_opportunity_key(
            source, opportunity_key
        )
        evidence = source.inputs.evidence
        bbo_evidence = evidence.get("bbo")
        eligibility_evidence = evidence.get("paper_eligibility")
        gate_evidence = evidence.get("paper_entry_gate")
        for name, value in (
            ("bbo", bbo_evidence),
            ("paper_eligibility", eligibility_evidence),
            ("paper_entry_gate", gate_evidence),
        ):
            if not isinstance(value, RiskInputEvidence):
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_evidence_missing", name
                )
        assert isinstance(bbo_evidence, RiskInputEvidence)
        assert isinstance(eligibility_evidence, RiskInputEvidence)
        assert isinstance(gate_evidence, RiskInputEvidence)

        eligibility_observed = _utc(
            eligibility_observed_at, "eligibility_observed_at"
        )
        eligibility_available = _utc(
            eligibility_available_at, "eligibility_available_at"
        )
        eligibility_row_updated = _utc(
            eligibility_row_updated_at, "eligibility_row_updated_at"
        )
        if (
            eligibility_evidence.observed_at != eligibility_observed
            or eligibility_evidence.available_at != eligibility_available
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "eligibility_clocks"
            )
        _require_final_evidence_age(
            name="bbo",
            evidence=bbo_evidence,
            decision_at=decision_clock,
            max_age_seconds=source.policy.market_data_max_age_seconds,
        )
        _require_final_evidence_age(
            name="paper_eligibility",
            evidence=eligibility_evidence,
            decision_at=decision_clock,
            max_age_seconds=source.policy.context_data_max_age_seconds,
        )
        _require_final_evidence_age(
            name="paper_entry_gate",
            evidence=gate_evidence,
            decision_at=decision_clock,
            max_age_seconds=source.policy.market_data_max_age_seconds,
        )

        bbo_payload = db_paper_bbo_evidence_payload(
            symbol=source.inputs.symbol,
            bid=bid,
            ask=ask,
            quote_source=quote_source,
            observed_at=bbo_evidence.observed_at,
            available_at=bbo_evidence.available_at,
            provider_generation=bbo_evidence.provider_generation,
        )
        eligibility_payload = db_paper_eligibility_evidence_payload(
            symbol=source.inputs.symbol,
            viability_id=viability_id,
            variant_id=variant_id,
            viability_score=viability_score,
            paper_eligible=paper_eligible,
            observed_at=eligibility_observed,
            available_at=eligibility_available,
            row_updated_at=eligibility_row_updated,
            execution_readiness=execution_readiness,
            source=eligibility_evidence.source,
            provider_generation=eligibility_evidence.provider_generation,
        )
        gate_payload = db_paper_entry_gate_evidence_payload(
            symbol=source.inputs.symbol,
            allowed=gate_allowed,
            reason=gate_reason,
            debug=gate_debug,
            structural_stop=structural_stop,
            setup_family=source.setup_family,
            opportunity_key=normalized_opportunity,
            observed_at=gate_evidence.observed_at,
            available_at=gate_evidence.available_at,
            source=gate_evidence.source,
            provider_generation=gate_evidence.provider_generation,
        )
        expected_hashes = {
            "bbo": db_paper_admission_component_sha256(bbo_payload),
            "paper_eligibility": db_paper_admission_component_sha256(
                eligibility_payload
            ),
            "paper_entry_gate": db_paper_admission_component_sha256(gate_payload),
        }
        actual_hashes = {
            "bbo": bbo_evidence.content_sha256,
            "paper_eligibility": eligibility_evidence.content_sha256,
            "paper_entry_gate": gate_evidence.content_sha256,
        }
        changed = sorted(
            name
            for name in expected_hashes
            if expected_hashes[name] != actual_hashes[name]
        )
        if changed:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_evidence_hash_mismatch", ",".join(changed)
            )

        first_dip_envelope_sha: str | None = None
        if source.setup_family == "first_dip_reclaim":
            # This is the final fallible evidence boundary before the caller
            # creates a reservation. Public gate debug is audit material only;
            # the process-local envelope and independently retained active
            # capture expectation must agree with both that audit and the
            # adaptive source's content-addressed prefix.
            from .first_dip_tape_decision import (
                FirstDipTapeDecisionProviderError,
                _FirstDipFinalAdmissionExpectation,
                _resolve_first_dip_final_admission,
                _verify_first_dip_final_admission_resolution,
            )

            expected = first_dip_final_admission_expectation
            if type(expected) is not _FirstDipFinalAdmissionExpectation:
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_entry_gate_veto",
                    "first_dip_final_admission_active_context_missing",
                )
            debug = dict(gate_debug)
            expected_public = {
                "execution_surface": "captured_db_paper",
                "symbol": source.inputs.symbol,
                "decision_at": gate_evidence.observed_at,
                "policy_sha256": str(
                    debug.get("first_dip_tape_policy_sha256") or ""
                ).strip().lower(),
                "evaluation_sha256": str(
                    debug.get("first_dip_tape_evaluation_sha256") or ""
                ).strip().lower(),
            }
            changed_expected = sorted(
                name
                for name, value in expected_public.items()
                if getattr(expected, name) != value
            )
            binding = expected.binding
            capture = source.capture_binding
            expected_capture = {
                "run_id": capture.run_id,
                "generation": capture.generation,
                "identity_sha256": capture.identity_sha256,
                "decision_id": capture.decision_id,
                "input_prefix_sequence": capture.input_prefix_sequence,
                "input_prefix_root_sha256": capture.input_prefix_root_sha256,
            }
            changed_capture = sorted(
                name
                for name, value in expected_capture.items()
                if getattr(binding, name) != value
            )
            if changed_expected or changed_capture:
                detail = ",".join(
                    [*(f"active.{name}" for name in changed_expected),
                     *(f"capture.{name}" for name in changed_capture)]
                )
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_entry_gate_veto",
                    "first_dip_final_admission_context_mismatch:" + detail,
                )
            try:
                final_resolution = _resolve_first_dip_final_admission(
                    execution_surface="captured_db_paper",
                    envelope=first_dip_final_admission_envelope,
                    expected=expected,
                )
                if final_resolution.admitted is not True:
                    raise AdaptiveRiskBuilderError(
                        "db_paper_final_entry_gate_veto",
                        final_resolution.reason,
                    )
                verified_resolution = (
                    _verify_first_dip_final_admission_resolution(
                        final_resolution,
                        require_admitted=True,
                    )
                )
            except FirstDipTapeDecisionProviderError as exc:
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_entry_gate_veto",
                    "first_dip_final_admission_unbound",
                ) from exc
            first_dip_envelope_sha = (
                verified_resolution.envelope_binding_sha256
            )
            if not _is_sha256(first_dip_envelope_sha):
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_entry_gate_veto",
                    "first_dip_final_admission_binding_invalid",
                )
        elif (
            first_dip_final_admission_envelope is not None
            or first_dip_final_admission_expectation is not None
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch",
                "unexpected_first_dip_final_admission_capability",
            )

        opportunity_sha = _sha256_json(normalized_opportunity)
        body = {
            "schema_version": DB_PAPER_FINAL_ADMISSION_SCHEMA_VERSION,
            "source_sha256": source.source_sha256,
            "decision_at": decision_clock,
            "symbol": source.inputs.symbol,
            "setup_family": source.setup_family,
            "opportunity_key": normalized_opportunity,
            "opportunity_sha256": opportunity_sha,
            "bbo_content_sha256": expected_hashes["bbo"],
            "eligibility_content_sha256": expected_hashes[
                "paper_eligibility"
            ],
            "entry_gate_content_sha256": expected_hashes["paper_entry_gate"],
            "first_dip_final_admission_envelope_sha256": (
                first_dip_envelope_sha
            ),
        }
        return cls(**body, content_sha256=_sha256_json(body))

    def body_without_content_sha(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("content_sha256", None)
        return body

    def to_payload(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def load_db_paper_final_admission_observation(
    payload: Mapping[str, Any],
) -> DbPaperFinalAdmissionObservation:
    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_final_observation_missing")
    values = dict(payload)
    if values.get("schema_version") != DB_PAPER_FINAL_ADMISSION_SCHEMA_VERSION:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_schema_invalid"
        )
    values["decision_at"] = _parse_utc(
        values.get("decision_at"), "final_observation.decision_at"
    )
    try:
        observation = DbPaperFinalAdmissionObservation(**values)
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_invalid", str(exc)
        ) from exc
    sha_fields = (
        "source_sha256",
        "opportunity_sha256",
        "bbo_content_sha256",
        "eligibility_content_sha256",
        "entry_gate_content_sha256",
        "content_sha256",
    )
    if any(not _is_sha256(getattr(observation, name)) for name in sha_fields):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_invalid", "sha256"
        )
    first_dip_sha = observation.first_dip_final_admission_envelope_sha256
    if observation.setup_family == "first_dip_reclaim":
        if not _is_sha256(first_dip_sha):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_observation_invalid",
                "first_dip_final_admission_envelope_sha256",
            )
    elif first_dip_sha is not None:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_invalid",
            "unexpected_first_dip_final_admission_envelope_sha256",
        )
    if observation.opportunity_sha256 != _sha256_json(
        dict(observation.opportunity_key)
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_hash_mismatch", "opportunity"
        )
    if observation.content_sha256 != _sha256_json(
        observation.body_without_content_sha()
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_hash_mismatch"
        )
    if _canonical_json(payload) != _canonical_json(observation.to_payload()):
        raise AdaptiveRiskBuilderError(
            "db_paper_final_observation_canonical_mismatch"
        )
    return observation


@dataclass(frozen=True)
class DbPaperExecutableAdmission:
    """Content-addressed executable economics approved under one DB lock."""

    schema_version: str
    final_bundle_sha256: str
    material_sha256: str
    source_sha256: str
    final_observation_sha256: str
    locked_risk_snapshot_sha256: str
    reservation_ledger_sha256: str
    request_sha256: str
    decision_packet_sha256: str
    reservation_id: str
    client_order_id: str
    account_scope: str
    account_identity_sha256: str
    symbol: str
    setup_family: str
    quantity_shares: int
    structural_risk_usd: float
    gross_notional_usd: float
    buying_power_impact_usd: float
    entry_price: float
    reference_price: float
    stop_price: float
    target_price: float
    fees_usd: float
    effective_atr: float
    execution_terms: Mapping[str, Any]
    execution_terms_sha256: str
    effective_config_sha256: str
    code_build_sha256: str
    feature_flags_sha256: str
    capture_prefix_root_sha256: str
    content_sha256: str

    @classmethod
    def create(
        cls,
        bundle: DbPaperFinalAdmissionBundle,
        observation: DbPaperFinalAdmissionObservation,
        request: AdaptiveRiskReservationRequest,
        resolution: ResolvedAdaptiveRisk,
        *,
        reservation_id: str | uuid.UUID,
        structural_risk_usd: float,
        gross_notional_usd: float,
        buying_power_impact_usd: float,
        entry_price: float,
        reference_price: float,
        stop_price: float,
        target_price: float,
        fees_usd: float,
        effective_atr: float,
    ) -> "DbPaperExecutableAdmission":
        if not isinstance(bundle, DbPaperFinalAdmissionBundle) or not isinstance(
            observation, DbPaperFinalAdmissionObservation
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_invalid", "final_bundle"
            )
        try:
            normalized_reservation_id = str(uuid.UUID(str(reservation_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_invalid", "reservation_id"
            ) from exc
        source = bundle.source
        locked = dict(bundle.locked_risk_snapshot)
        exact = {
            "source": (observation.source_sha256, source.source_sha256),
            "symbol": (observation.symbol, source.inputs.symbol),
            "setup_family": (observation.setup_family, source.setup_family),
            "opportunity": (
                observation.opportunity_sha256,
                _sha256_json(dict(bundle.opportunity_key)),
            ),
            "request_inputs": (request.inputs, source.inputs),
            "request_policy": (request.policy, source.policy),
            "request_account": (request.account_snapshot, source.account_snapshot),
            "request_scope": (request.account_scope, source.account_scope),
            "request_setup": (request.setup_family, source.setup_family),
            "request_cluster": (
                request.correlation_cluster,
                source.correlation_cluster,
            ),
            "resolution_policy": (
                resolution.policy_sha256,
                request.policy.policy_sha256,
            ),
            "resolution_inputs": (
                resolution.input_sha256,
                request.inputs.input_sha256,
            ),
            "resolution_valid": (resolution.valid, True),
            "resolution_rejections": (
                tuple(resolution.rejection_reasons),
                (),
            ),
            "resolution_policy_snapshot": (
                _canonical_json(resolution.policy_snapshot),
                _canonical_json(asdict(request.policy)),
            ),
            "resolution_input_snapshot": (
                _canonical_json(resolution.input_snapshot),
                _canonical_json(asdict(request.inputs)),
            ),
        }
        changed = sorted(
            name for name, (actual, expected) in exact.items() if actual != expected
        )
        if changed:
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_mismatch", ",".join(changed)
            )
        values = {
            "structural_risk_usd": float(structural_risk_usd),
            "gross_notional_usd": float(gross_notional_usd),
            "buying_power_impact_usd": float(buying_power_impact_usd),
            "entry_price": float(entry_price),
            "reference_price": float(reference_price),
            "stop_price": float(stop_price),
            "target_price": float(target_price),
            "fees_usd": float(fees_usd),
            "effective_atr": float(effective_atr),
        }
        if any(not math.isfinite(value) for value in values.values()):
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_invalid", "nonfinite"
            )
        if (
            int(resolution.quantity_shares) <= 0
            or values["entry_price"] <= 0
            or values["reference_price"] <= 0
            or values["stop_price"] <= 0
            or values["target_price"] <= values["entry_price"]
            or values["stop_price"] >= values["entry_price"]
            or values["fees_usd"] < 0
            or values["effective_atr"] <= 0
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_invalid", "economics"
            )
        economic_exact = {
            "entry_price": (values["entry_price"], float(request.entry_limit_price)),
            "resolved_entry_price": (
                values["entry_price"],
                float(resolution.effective_entry_price),
            ),
            "reference_price": (
                values["reference_price"],
                (float(source.inputs.bid) + float(source.inputs.ask)) / 2.0,
            ),
            "stop_price": (
                values["stop_price"],
                float(request.inputs.structural_stop),
            ),
            "structural_risk_usd": (
                values["structural_risk_usd"],
                float(resolution.planned_structural_risk_usd),
            ),
            "gross_notional_usd": (
                values["gross_notional_usd"],
                float(resolution.planned_notional_usd),
            ),
            "buying_power_impact_usd": (
                values["buying_power_impact_usd"],
                float(resolution.planned_buying_power_impact_usd),
            ),
        }
        economic_changed = sorted(
            name
            for name, (actual, expected) in economic_exact.items()
            if abs(actual - expected) > max(1e-9, max(abs(actual), abs(expected)) * 1e-12)
        )
        if economic_changed:
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_mismatch",
                ",".join(economic_changed),
            )
        expected_gross = values["entry_price"] * int(resolution.quantity_shares)
        if abs(values["gross_notional_usd"] - expected_gross) > max(
            1e-9, abs(expected_gross) * 1e-12
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_mismatch", "executable_notional"
            )
        for field in (
            "content_sha256",
            "ledger_sha256",
        ):
            if not _is_sha256(locked.get(field)):
                raise AdaptiveRiskBuilderError(
                    "db_paper_executable_admission_invalid",
                    f"locked_risk_snapshot.{field}",
                )
        terms = _normalize_execution_terms(bundle.execution_terms)
        if terms["effective_config_sha256"] != (
            request.inputs.effective_config_sha256
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_mismatch",
                "effective_config",
            )
        body = {
            "schema_version": DB_PAPER_EXECUTABLE_ADMISSION_SCHEMA_VERSION,
            "final_bundle_sha256": bundle.content_sha256,
            "material_sha256": bundle.material_sha256,
            "source_sha256": source.source_sha256,
            "final_observation_sha256": observation.content_sha256,
            "locked_risk_snapshot_sha256": locked["content_sha256"],
            "reservation_ledger_sha256": locked["ledger_sha256"],
            "request_sha256": request.request_sha256,
            "decision_packet_sha256": resolution.decision_packet_sha256,
            "reservation_id": normalized_reservation_id,
            "client_order_id": request.client_order_id,
            "account_scope": request.account_scope,
            "account_identity_sha256": request.inputs.account_identity_sha256,
            "symbol": request.inputs.symbol,
            "setup_family": request.setup_family,
            "quantity_shares": int(resolution.quantity_shares),
            **values,
            "execution_terms": terms,
            "execution_terms_sha256": _sha256_json(terms),
            "effective_config_sha256": request.inputs.effective_config_sha256,
            "code_build_sha256": request.inputs.code_build_sha256,
            "feature_flags_sha256": request.inputs.feature_flags_sha256,
            "capture_prefix_root_sha256": (
                request.inputs.capture_prefix_root_sha256
            ),
        }
        return cls(**body, content_sha256=_sha256_json(body))

    def body_without_content_sha(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("content_sha256", None)
        return body

    def to_payload(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def load_db_paper_executable_admission(
    payload: Mapping[str, Any],
) -> DbPaperExecutableAdmission:
    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_executable_admission_missing")
    values = dict(payload)
    if values.get("schema_version") != DB_PAPER_EXECUTABLE_ADMISSION_SCHEMA_VERSION:
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_schema_invalid"
        )
    try:
        admission = DbPaperExecutableAdmission(**values)
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", str(exc)
        ) from exc
    if type(admission.quantity_shares) is not int or admission.quantity_shares <= 0:
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "quantity_shares"
        )
    numeric_fields = (
        "structural_risk_usd",
        "gross_notional_usd",
        "buying_power_impact_usd",
        "entry_price",
        "reference_price",
        "stop_price",
        "target_price",
        "fees_usd",
        "effective_atr",
    )
    for name in numeric_fields:
        value = getattr(admission, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_invalid", name
            )
    if (
        float(admission.structural_risk_usd) <= 0
        or float(admission.gross_notional_usd) <= 0
        or float(admission.buying_power_impact_usd) <= 0
        or float(admission.entry_price) <= 0
        or float(admission.reference_price) <= 0
        or float(admission.stop_price) <= 0
        or float(admission.stop_price) >= float(admission.entry_price)
        or float(admission.target_price) <= float(admission.entry_price)
        or float(admission.fees_usd) < 0
        or float(admission.effective_atr) <= 0
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "economics"
        )
    expected_gross = float(admission.entry_price) * admission.quantity_shares
    if abs(float(admission.gross_notional_usd) - expected_gross) > max(
        1e-9, abs(expected_gross) * 1e-12
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "executable_notional"
        )
    try:
        normalized_reservation_id = str(uuid.UUID(admission.reservation_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "reservation_id"
        ) from exc
    if normalized_reservation_id != admission.reservation_id:
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "reservation_id"
        )
    for name in (
        "client_order_id",
        "account_scope",
        "symbol",
        "setup_family",
    ):
        value = getattr(admission, name)
        if not isinstance(value, str) or not value.strip():
            raise AdaptiveRiskBuilderError(
                "db_paper_executable_admission_invalid", name
            )
    if (
        not admission.account_scope.startswith("db-paper:")
        or admission.symbol != admission.symbol.strip().upper()
        or admission.setup_family != admission.setup_family.strip().lower()
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "identity"
        )
    sha_fields = (
        "final_bundle_sha256",
        "material_sha256",
        "source_sha256",
        "final_observation_sha256",
        "locked_risk_snapshot_sha256",
        "reservation_ledger_sha256",
        "request_sha256",
        "decision_packet_sha256",
        "account_identity_sha256",
        "execution_terms_sha256",
        "effective_config_sha256",
        "code_build_sha256",
        "feature_flags_sha256",
        "capture_prefix_root_sha256",
        "content_sha256",
    )
    if any(not _is_sha256(getattr(admission, name)) for name in sha_fields):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_invalid", "sha256"
        )
    normalized_terms = _normalize_execution_terms(admission.execution_terms)
    if (
        normalized_terms["effective_config_sha256"]
        != admission.effective_config_sha256
        or _canonical_json(normalized_terms)
        != _canonical_json(admission.execution_terms)
        or admission.execution_terms_sha256 != _sha256_json(normalized_terms)
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_hash_mismatch", "execution_terms"
        )
    if admission.content_sha256 != _sha256_json(
        admission.body_without_content_sha()
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_hash_mismatch"
        )
    if _canonical_json(payload) != _canonical_json(admission.to_payload()):
        raise AdaptiveRiskBuilderError(
            "db_paper_executable_admission_canonical_mismatch"
        )
    return admission


@dataclass(frozen=True)
class DbPaperAdmissionReceipt:
    """Content address joining final facts to the reserved canonical fill."""

    schema_version: str
    source_sha256: str
    final_observation_sha256: str
    final_bundle_sha256: str
    locked_risk_snapshot_sha256: str
    executable_admission_sha256: str
    request_sha256: str
    decision_packet_sha256: str
    reservation_id: str
    client_order_id: str
    account_scope: str
    account_identity_sha256: str
    broker_source: str
    broker_environment: str
    execution_family: str
    venue: str
    connection_generation: str
    replay_or_paper_run_id: str
    generation: int
    decision_id: str
    decision_at: datetime
    opportunity_sha256: str
    content_sha256: str

    @classmethod
    def create(
        cls,
        source: AdaptiveRiskBuilderSource,
        observation: DbPaperFinalAdmissionObservation,
        request: AdaptiveRiskReservationRequest,
        executable_admission: DbPaperExecutableAdmission,
        *,
        decision_packet_sha256: str,
        reservation_id: str | uuid.UUID,
        connection_generation: str,
    ) -> "DbPaperAdmissionReceipt":
        try:
            normalized_reservation_id = str(uuid.UUID(str(reservation_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_invalid", "reservation_id"
            ) from exc
        if not _is_sha256(decision_packet_sha256):
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_invalid", "decision_packet_sha256"
            )
        if observation.source_sha256 != source.source_sha256:
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_mismatch", "source"
            )
        executable_exact = {
            "source": (executable_admission.source_sha256, source.source_sha256),
            "observation": (
                executable_admission.final_observation_sha256,
                observation.content_sha256,
            ),
            "request": (
                executable_admission.request_sha256,
                request.request_sha256,
            ),
            "decision": (
                executable_admission.decision_packet_sha256,
                str(decision_packet_sha256).lower(),
            ),
            "reservation": (
                executable_admission.reservation_id,
                normalized_reservation_id,
            ),
        }
        executable_changed = sorted(
            name
            for name, (actual, expected) in executable_exact.items()
            if actual != expected
        )
        if executable_changed:
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_mismatch",
                "executable:" + ",".join(executable_changed),
            )
        exact = {
            "inputs": (request.inputs, source.inputs),
            "policy": (request.policy, source.policy),
            "account_snapshot": (
                request.account_snapshot,
                source.account_snapshot,
            ),
            "account_scope": (request.account_scope, source.account_scope),
            "setup_family": (request.setup_family, source.setup_family),
            "correlation_cluster": (
                request.correlation_cluster,
                source.correlation_cluster,
            ),
        }
        changed = sorted(
            name for name, (actual, expected) in exact.items() if actual != expected
        )
        if changed:
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_mismatch", ",".join(changed)
            )
        connection = str(connection_generation or "").strip()
        if not connection:
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_invalid", "connection_generation"
            )
        body = {
            "schema_version": DB_PAPER_ADMISSION_RECEIPT_SCHEMA_VERSION,
            "source_sha256": source.source_sha256,
            "final_observation_sha256": observation.content_sha256,
            "final_bundle_sha256": executable_admission.final_bundle_sha256,
            "locked_risk_snapshot_sha256": (
                executable_admission.locked_risk_snapshot_sha256
            ),
            "executable_admission_sha256": executable_admission.content_sha256,
            "request_sha256": request.request_sha256,
            "decision_packet_sha256": str(decision_packet_sha256).lower(),
            "reservation_id": normalized_reservation_id,
            "client_order_id": request.client_order_id,
            "account_scope": source.account_scope,
            "account_identity_sha256": source.inputs.account_identity_sha256,
            "broker_source": "db_paper",
            "broker_environment": source.inputs.broker_environment,
            "execution_family": source.inputs.execution_family,
            "venue": source.inputs.venue,
            "connection_generation": connection,
            "replay_or_paper_run_id": source.inputs.replay_or_paper_run_id,
            "generation": int(source.inputs.generation),
            "decision_id": source.inputs.decision_id,
            "decision_at": source.inputs.as_of,
            "opportunity_sha256": observation.opportunity_sha256,
        }
        return cls(**body, content_sha256=_sha256_json(body))

    def body_without_content_sha(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("content_sha256", None)
        return body

    def to_payload(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def load_db_paper_admission_receipt(
    payload: Mapping[str, Any],
) -> DbPaperAdmissionReceipt:
    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError("db_paper_admission_receipt_missing")
    values = dict(payload)
    if values.get("schema_version") != DB_PAPER_ADMISSION_RECEIPT_SCHEMA_VERSION:
        raise AdaptiveRiskBuilderError("db_paper_admission_receipt_schema_invalid")
    values["decision_at"] = _parse_utc(
        values.get("decision_at"), "receipt.decision_at"
    )
    try:
        receipt = DbPaperAdmissionReceipt(**values)
    except AdaptiveRiskBuilderError:
        raise
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_receipt_invalid", str(exc)
        ) from exc
    sha_fields = (
        "source_sha256",
        "final_observation_sha256",
        "final_bundle_sha256",
        "locked_risk_snapshot_sha256",
        "executable_admission_sha256",
        "request_sha256",
        "decision_packet_sha256",
        "account_identity_sha256",
        "opportunity_sha256",
        "content_sha256",
    )
    if any(not _is_sha256(getattr(receipt, name)) for name in sha_fields):
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_receipt_invalid", "sha256"
        )
    try:
        normalized_reservation_id = str(uuid.UUID(receipt.reservation_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_receipt_invalid", "reservation_id"
        ) from exc
    if normalized_reservation_id != receipt.reservation_id:
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_receipt_invalid", "reservation_id"
        )
    for name in (
        "client_order_id",
        "account_scope",
        "connection_generation",
        "replay_or_paper_run_id",
        "decision_id",
        "execution_family",
        "venue",
    ):
        value = getattr(receipt, name)
        if not isinstance(value, str) or not value.strip():
            raise AdaptiveRiskBuilderError(
                "db_paper_admission_receipt_invalid", name
            )
    if (
        receipt.broker_source != "db_paper"
        or receipt.broker_environment != "paper"
        or not receipt.account_scope.startswith("db-paper:")
        or type(receipt.generation) is not int
        or receipt.generation <= 0
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_receipt_invalid", "identity"
        )
    if receipt.content_sha256 != _sha256_json(receipt.body_without_content_sha()):
        raise AdaptiveRiskBuilderError("db_paper_admission_receipt_hash_mismatch")
    if _canonical_json(payload) != _canonical_json(receipt.to_payload()):
        raise AdaptiveRiskBuilderError(
            "db_paper_admission_receipt_canonical_mismatch"
        )
    return receipt


_SEALED_REPLAY_ADAPTIVE_RISK_ATTESTATION_TOKEN = object()


class _SealedReplayAdaptiveRiskAttestationLease:
    """One-shot/revocable claim shared by copied replay contexts."""

    __slots__ = ("_lock", "_active", "_claimed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = True
        self._claimed = False

    def claim(self) -> str | None:
        with self._lock:
            if not self._active:
                return "scope_revoked"
            if self._claimed:
                return "already_consumed"
            self._claimed = True
            return None

    def revoke(self) -> None:
        with self._lock:
            self._active = False

    @property
    def claimed(self) -> bool:
        with self._lock:
            return self._claimed


@dataclass(frozen=True)
class _SealedReplayAdaptiveRiskBuildAttestation:
    """Opaque permission to rebuild one exact recorded paper request.

    This is deliberately mechanics-only: it can satisfy the builder's replay
    provenance boundary, but it carries no reservation, order, or broker
    authority.  ReplayV3 mints it only after independently re-inventorying the
    final capture frontier from sealed bytes.
    """

    identity_sha256: str
    final_capture_seal_sha256: str
    coverage_manifest_sha256: str
    decision_checkpoint_sha256: str
    source_sha256: str
    expected_request_sha256: str
    decision_id: str
    symbol: str
    reservation_authority: bool = False
    order_authority: bool = False
    _token: object = field(default=None, repr=False, compare=False)
    _lease: _SealedReplayAdaptiveRiskAttestationLease = field(
        default_factory=_SealedReplayAdaptiveRiskAttestationLease,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._token is not _SEALED_REPLAY_ADAPTIVE_RISK_ATTESTATION_TOKEN:
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_origin_invalid"
            )
        for name in (
            "identity_sha256",
            "final_capture_seal_sha256",
            "coverage_manifest_sha256",
            "decision_checkpoint_sha256",
            "source_sha256",
            "expected_request_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if not _is_sha256(value):
                raise AdaptiveRiskBuilderError(
                    "sealed_replay_adaptive_risk_attestation_invalid", name
                )
            object.__setattr__(self, name, value)
        decision_id = str(self.decision_id or "").strip()
        symbol = str(self.symbol or "").strip().upper()
        if not decision_id or not symbol:
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_invalid", "identity"
            )
        if self.reservation_authority is not False or self.order_authority is not False:
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_authority_invalid"
            )
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "symbol", symbol)

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.sealed-replay-adaptive-risk-build-attestation.v1",
            "identity_sha256": self.identity_sha256,
            "final_capture_seal_sha256": self.final_capture_seal_sha256,
            "coverage_manifest_sha256": self.coverage_manifest_sha256,
            "decision_checkpoint_sha256": self.decision_checkpoint_sha256,
            "source_sha256": self.source_sha256,
            "expected_request_sha256": self.expected_request_sha256,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "reservation_authority": False,
            "order_authority": False,
        }

    @property
    def attestation_sha256(self) -> str:
        return _sha256_json(self.body())

    @property
    def consumed(self) -> bool:
        return self._lease.claimed

    def claim_exact(
        self,
        *,
        source: AdaptiveRiskBuilderSource,
        request: AdaptiveRiskReservationRequest,
    ) -> str:
        if (
            not isinstance(source, AdaptiveRiskBuilderSource)
            or type(request) is not AdaptiveRiskReservationRequest
            or source.source_sha256 != self.source_sha256
            or request.request_sha256 != self.expected_request_sha256
            or request.inputs.decision_id != self.decision_id
            or request.inputs.symbol != self.symbol
        ):
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_mismatch"
            )
        lease_error = self._lease.claim()
        if lease_error is not None:
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_" + lease_error
            )
        return self.attestation_sha256

    def revoke(self) -> None:
        self._lease.revoke()

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("sealed replay adaptive-risk attestation cannot be pickled")


def _issue_sealed_replay_adaptive_risk_build_attestation(
    *,
    source: AdaptiveRiskBuilderSource,
    expected_request: AdaptiveRiskReservationRequest,
    identity_sha256: str,
    final_capture_seal_sha256: str,
    coverage_manifest_sha256: str,
    decision_checkpoint_sha256: str,
) -> _SealedReplayAdaptiveRiskBuildAttestation:
    """Private ReplayV3 issuer; serialized hashes cannot recreate this object."""

    if (
        not isinstance(source, AdaptiveRiskBuilderSource)
        or type(expected_request) is not AdaptiveRiskReservationRequest
        or source.inputs.decision_id != expected_request.inputs.decision_id
        or source.inputs.symbol != expected_request.inputs.symbol
        or source.policy != expected_request.policy
        or source.inputs != expected_request.inputs
        or source.account_snapshot != expected_request.account_snapshot
        or source.account_scope != expected_request.account_scope
        or source.setup_family != expected_request.setup_family
        or source.correlation_cluster != expected_request.correlation_cluster
    ):
        raise AdaptiveRiskBuilderError(
            "sealed_replay_adaptive_risk_attestation_request_mismatch"
        )
    return _SealedReplayAdaptiveRiskBuildAttestation(
        identity_sha256=identity_sha256,
        final_capture_seal_sha256=final_capture_seal_sha256,
        coverage_manifest_sha256=coverage_manifest_sha256,
        decision_checkpoint_sha256=decision_checkpoint_sha256,
        source_sha256=source.source_sha256,
        expected_request_sha256=expected_request.request_sha256,
        decision_id=expected_request.inputs.decision_id,
        symbol=expected_request.inputs.symbol,
        _token=_SEALED_REPLAY_ADAPTIVE_RISK_ATTESTATION_TOKEN,
    )


@dataclass(frozen=True)
class BuiltAdaptiveRiskRequest:
    schema_version: str
    source_sha256: str
    request: AdaptiveRiskReservationRequest
    resolution: ResolvedAdaptiveRisk
    decision_packet: Mapping[str, Any]
    reservation_claim: AdaptiveRiskReservationClaim
    trusted_capture_attestation_sha256: str | None = None
    sealed_replay_attestation_sha256: str | None = None

    def audit_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "request_sha256": self.request.request_sha256,
            "opportunity_key": (
                self.request.opportunity_key.to_payload()
                if self.request.opportunity_key
                else None
            ),
            "opportunity_key_sha256": (
                self.request.opportunity_key.key_sha256
                if self.request.opportunity_key
                else None
            ),
            "decision_packet_sha256": self.resolution.decision_packet_sha256,
            "economic_input_sha256": self.resolution.economic_input_sha256,
            "economic_resolution_sha256": self.resolution.economic_resolution_sha256,
            "claim_sha256": self.reservation_claim.claim_sha256,
            "quantity_shares": int(self.resolution.quantity_shares),
            "planned_structural_risk_usd": float(
                self.resolution.planned_structural_risk_usd
            ),
            "planned_notional_usd": float(self.resolution.planned_notional_usd),
            "planned_buying_power_impact_usd": float(
                self.resolution.planned_buying_power_impact_usd
            ),
            "policy_sha256": self.policy_sha256,
            "capture_prefix_root_sha256": (
                self.request.inputs.capture_prefix_root_sha256
            ),
            "trusted_capture_attestation_sha256": (
                self.trusted_capture_attestation_sha256
            ),
            "sealed_replay_attestation_sha256": (
                self.sealed_replay_attestation_sha256
            ),
        }

    @property
    def policy_sha256(self) -> str:
        return self.request.policy.policy_sha256


@dataclass(frozen=True)
class BuiltAdaptiveRiskDecision:
    """Shared resolver output before any reservation-transport concerns."""

    policy: AdaptiveRiskPolicy
    inputs: AdaptiveRiskInputs
    capture_binding: AdaptiveRiskDiagnosticCaptureBinding
    resolution: ResolvedAdaptiveRisk
    decision_packet: Mapping[str, Any]

    @property
    def parity_payload(self) -> dict[str, Any]:
        return {
            "decision_packet_sha256": self.resolution.decision_packet_sha256,
            "economic_input_sha256": self.resolution.economic_input_sha256,
            "economic_resolution_sha256": self.resolution.economic_resolution_sha256,
            "quantity_shares": int(self.resolution.quantity_shares),
            "planned_structural_risk_usd": float(
                self.resolution.planned_structural_risk_usd
            ),
            "planned_notional_usd": float(self.resolution.planned_notional_usd),
            "planned_buying_power_impact_usd": float(
                self.resolution.planned_buying_power_impact_usd
            ),
            "capture_prefix_root_sha256": (
                self.capture_binding.input_prefix_root_sha256
            ),
        }


def _load_evidence(raw: Any) -> dict[str, RiskInputEvidence]:
    if not isinstance(raw, Mapping):
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_source_invalid", "evidence_missing"
        )
    result: dict[str, RiskInputEvidence] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_source_invalid", f"evidence_invalid:{name}"
            )
        values = dict(value)
        values["observed_at"] = _parse_utc(
            values.get("observed_at"), f"evidence.{name}.observed_at"
        )
        values["available_at"] = _parse_utc(
            values.get("available_at"), f"evidence.{name}.available_at"
        )
        try:
            result[str(name)] = RiskInputEvidence(**values)
        except (TypeError, ValueError) as exc:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_source_invalid", f"evidence_invalid:{name}"
            ) from exc
    return result


def load_adaptive_risk_builder_source(
    payload: Mapping[str, Any],
) -> AdaptiveRiskBuilderSource:
    """Strictly reconstruct a raw builder source and verify its content hash."""

    if not isinstance(payload, Mapping):
        raise AdaptiveRiskBuilderError("adaptive_risk_builder_source_missing")
    raw = dict(payload)
    supplied_sha = str(raw.pop("source_sha256", "") or "").strip().lower()
    if raw.get("schema_version") != BUILDER_SOURCE_SCHEMA_VERSION:
        raise AdaptiveRiskBuilderError("adaptive_risk_builder_source_schema_invalid")
    policy_raw = raw.get("policy")
    input_raw = raw.get("inputs")
    account_raw = raw.get("account_snapshot")
    capture_raw = raw.get("capture_binding")
    if not all(
        isinstance(value, Mapping)
        for value in (policy_raw, input_raw, account_raw, capture_raw)
    ):
        reason = (
            "builder_missing_capture_binding"
            if not isinstance(capture_raw, Mapping)
            else "adaptive_risk_builder_source_invalid"
        )
        raise AdaptiveRiskBuilderError(reason)
    try:
        policy = AdaptiveRiskPolicy(**dict(policy_raw))
        input_values = dict(input_raw)
        input_values["as_of"] = _parse_utc(input_values.get("as_of"), "inputs.as_of")
        input_values["evidence"] = _load_evidence(input_values.get("evidence"))
        inputs = AdaptiveRiskInputs(**input_values)

        account_values = dict(account_raw)
        supplied_account_sha = account_values.pop("snapshot_sha256", None)
        account_values["observed_at"] = _parse_utc(
            account_values.get("observed_at"), "account_snapshot.observed_at"
        )
        account_values["available_at"] = _parse_utc(
            account_values.get("available_at"), "account_snapshot.available_at"
        )
        account = ImmutableAccountRiskSnapshot(**account_values)
        if supplied_account_sha != account.snapshot_sha256:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_source_invalid", "account_snapshot_hash_mismatch"
            )

        capture_values = dict(capture_raw)
        capture_values["observed_at"] = _parse_utc(
            capture_values.get("observed_at"), "capture_binding.observed_at"
        )
        capture_values["available_at"] = _parse_utc(
            capture_values.get("available_at"), "capture_binding.available_at"
        )
        capture = AdaptiveRiskDiagnosticCaptureBinding(**capture_values)
        source = AdaptiveRiskBuilderSource(
            policy=policy,
            inputs=inputs,
            account_snapshot=account,
            capture_binding=capture,
            account_scope=raw.get("account_scope"),
            setup_family=raw.get("setup_family"),
            correlation_cluster=raw.get("correlation_cluster"),
            broker_account_evidence=(
                _load_evidence(
                    {"account": raw["broker_account_evidence"]}
                )["account"]
                if "broker_account_evidence" in raw
                else None
            ),
            settled_daily_pnl_evidence=(
                _load_evidence(
                    {"daily_pnl": raw["settled_daily_pnl_evidence"]}
                )["daily_pnl"]
                if "settled_daily_pnl_evidence" in raw
                else None
            ),
        )
    except AdaptiveRiskBuilderError:
        raise
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_source_invalid", str(exc)
        ) from exc
    if not _is_sha256(supplied_sha) or supplied_sha != source.source_sha256:
        raise AdaptiveRiskBuilderError("adaptive_risk_builder_source_hash_mismatch")
    if _canonical_json(raw) != _canonical_json(source._body()):
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_source_canonical_mismatch"
        )
    return source


def _validate_capture_binding(
    inputs: AdaptiveRiskInputs,
    capture: AdaptiveRiskDiagnosticCaptureBinding,
) -> None:
    exact = {
        "run_id": (inputs.replay_or_paper_run_id, capture.run_id),
        "generation": (int(inputs.generation), int(capture.generation)),
        "decision_id": (inputs.decision_id, capture.decision_id),
        "capture_prefix_root_sha256": (
            inputs.capture_prefix_root_sha256,
            capture.input_prefix_root_sha256,
        ),
    }
    for name, (actual, expected) in exact.items():
        if actual != expected:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_binding_mismatch", name
            )
    if capture.available_at > inputs.as_of:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_binding_mismatch", "capture_from_future"
        )
    prefix_evidence = inputs.evidence.get("capture_prefix")
    if not isinstance(prefix_evidence, RiskInputEvidence):
        raise AdaptiveRiskBuilderError("builder_missing_capture_binding")
    if (
        prefix_evidence.content_sha256 != capture.input_prefix_root_sha256
        or prefix_evidence.observed_at != capture.observed_at
        or prefix_evidence.available_at != capture.available_at
        or prefix_evidence.provider_generation != capture.verifier_generation
    ):
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_binding_mismatch", "capture_evidence"
        )


def build_adaptive_risk_decision(
    policy: AdaptiveRiskPolicy,
    inputs: AdaptiveRiskInputs,
    capture_binding: AdaptiveRiskDiagnosticCaptureBinding,
) -> BuiltAdaptiveRiskDecision:
    """Single causal resolver entry used by replay and reservation builders."""

    _validate_capture_binding(inputs, capture_binding)
    resolution = resolve_adaptive_risk(policy, inputs)
    if not resolution.valid or resolution.quantity_shares <= 0:
        detail = ",".join(resolution.rejection_reasons)
        raise AdaptiveRiskBuilderError("adaptive_risk_builder_resolution_rejected", detail)
    return BuiltAdaptiveRiskDecision(
        policy=policy,
        inputs=inputs,
        capture_binding=capture_binding,
        resolution=resolution,
        decision_packet=resolution.to_decision_packet(),
    )


def rebuild_adaptive_risk_decision_packet(
    packet: Mapping[str, Any],
    capture_binding: AdaptiveRiskDiagnosticCaptureBinding,
) -> BuiltAdaptiveRiskDecision:
    """Reconstruct raw packet sources and prove the shared builder is byte-identical."""

    verified = load_and_verify_adaptive_risk_decision_packet(packet)
    try:
        policy = AdaptiveRiskPolicy(**dict(verified.policy_snapshot))
        values = dict(verified.input_snapshot)
        values["as_of"] = _parse_utc(values.get("as_of"), "inputs.as_of")
        values["evidence"] = _load_evidence(values.get("evidence"))
        inputs = AdaptiveRiskInputs(**values)
    except AdaptiveRiskBuilderError:
        raise
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_source_invalid", str(exc)
        ) from exc
    built = build_adaptive_risk_decision(policy, inputs, capture_binding)
    if _canonical_json(built.decision_packet) != _canonical_json(dict(packet)):
        raise AdaptiveRiskBuilderError("adaptive_risk_builder_packet_parity_mismatch")
    return built


def _validate_source_binding(source: AdaptiveRiskBuilderSource) -> None:
    inputs = source.inputs
    account = source.account_snapshot
    capture = source.capture_binding
    _validate_capture_binding(inputs, capture)
    exact = {
        "account_scope": (source.account_scope, account.account_scope),
        "execution_family": (inputs.execution_family, account.execution_family),
        "venue": (inputs.venue, account.venue),
        "broker_environment": (
            inputs.broker_environment,
            account.broker_environment,
        ),
        "account_identity_sha256": (
            inputs.account_identity_sha256,
            account.account_identity_sha256,
        ),
        "correlation_cluster": (
            inputs.correlation_cluster_id,
            source.correlation_cluster,
        ),
    }
    for name, (actual, expected) in exact.items():
        if actual != expected:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_binding_mismatch", name
            )
    has_bound_paper_evidence = (
        source.broker_account_evidence is not None
        or source.settled_daily_pnl_evidence is not None
    )
    if inputs.execution_surface == "alpaca_paper" and has_bound_paper_evidence:
        expected_evidence = {
            "account": source.broker_account_evidence,
            "daily_pnl": source.settled_daily_pnl_evidence,
        }
        for name, expected in expected_evidence.items():
            if type(expected) is not RiskInputEvidence:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_binding_mismatch",
                    f"{name}_authority_missing",
                )
            if inputs.evidence.get(name) != expected:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_binding_mismatch", f"{name}_evidence"
                )
        if source.broker_account_evidence == source.settled_daily_pnl_evidence:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_binding_mismatch", "account_daily_pnl_alias"
            )
    else:
        # Source-only/retained PAPER fixtures can still exercise pure builder
        # mechanics.  They have no live authority: the mutable reservation
        # store requires a locked PAPER bundle before any risk is reserved.
        account_evidence = RiskInputEvidence(
            source=account.source,
            observed_at=account.observed_at,
            available_at=account.available_at,
            content_sha256=account.snapshot_sha256,
            provider_generation=account.provider_generation,
        )
        for name in ("account", "daily_pnl"):
            if inputs.evidence.get(name) != account_evidence:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_binding_mismatch", f"{name}_evidence"
                )


def adaptive_risk_capture_binding_from_active_attestation(
    attestation: ActiveCaptureInputPrefixAttestation,
) -> AdaptiveRiskDiagnosticCaptureBinding:
    """Derive the serializable parity binding from one private runtime proof.

    The returned object remains diagnostic.  The original opaque attestation is
    still required separately when Alpaca paper builds the adaptive request.
    """

    try:
        proof = verify_active_capture_input_attestation(attestation)
    except CaptureContractError as exc:
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_invalid"
        ) from exc
    observed_at = max(
        row.receipt.returned_at for row in proof.read_evidence
    )
    verifier_generation = _sha256_json(
        {
            "schema_version": "chili.active-capture-producer-roster.v1",
            "run_id": proof.run_id,
            "generation": proof.generation,
            "producer_generations": dict(proof.producer_generations),
            "resource_binding_sha256": proof.resource_binding_sha256,
        }
    )
    return AdaptiveRiskDiagnosticCaptureBinding.create_diagnostic(
        run_id=proof.run_id,
        generation=proof.generation,
        decision_id=proof.decision_id,
        input_prefix_sequence=proof.input_prefix_sequence,
        input_prefix_root_sha256=proof.input_prefix_root_sha256,
        identity_sha256=proof.identity_sha256,
        observed_at=observed_at,
        available_at=proof.attested_available_at,
        verifier_generation=verifier_generation,
    )


def _validate_alpaca_active_capture_attestation(
    source: AdaptiveRiskBuilderSource,
    attestation: ActiveCaptureInputPrefixAttestation,
) -> ActiveCaptureInputPrefixAttestation:
    try:
        proof = verify_active_capture_input_attestation(attestation)
    except CaptureContractError as exc:
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_invalid"
        ) from exc
    if any(
        value is not None
        for value in (
            proof.first_dip_prior_detector_reference_sha256,
            proof.first_dip_adaptive_request_sha256,
            proof.first_dip_opportunity_key_sha256,
        )
    ):
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_mismatch",
            "final_lineage_cannot_build_request",
        )

    derived = adaptive_risk_capture_binding_from_active_attestation(proof)
    if source.capture_binding != derived:
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_mismatch",
            "diagnostic_binding",
        )
    inputs = source.inputs
    exact = {
        "execution_surface": (inputs.execution_surface, "alpaca_paper"),
        "broker_environment": (inputs.broker_environment, "paper"),
        "run_id": (inputs.replay_or_paper_run_id, proof.run_id),
        "generation": (int(inputs.generation), int(proof.generation)),
        "decision_id": (inputs.decision_id, proof.decision_id),
        "capture_prefix_root_sha256": (
            inputs.capture_prefix_root_sha256,
            proof.input_prefix_root_sha256,
        ),
        "account_identity_sha256": (
            inputs.account_identity_sha256,
            proof.account_identity_sha256,
        ),
        "code_build_sha256": (
            inputs.code_build_sha256,
            proof.code_build_sha256,
        ),
        "effective_config_sha256": (
            inputs.effective_config_sha256,
            proof.config_sha256,
        ),
        "feature_flags_sha256": (
            inputs.feature_flags_sha256,
            proof.feature_flags_sha256,
        ),
    }
    changed = sorted(
        name for name, (actual, expected) in exact.items() if actual != expected
    )
    if changed:
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_mismatch",
            ",".join(changed),
        )
    if not (
        proof.attested_available_at <= inputs.as_of <= proof.expires_at
    ):
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_mismatch",
            "capture_attestation_expired_or_from_future",
        )

    has_first_dip_read = proof.first_dip_tape_read_id is not None
    if (source.setup_family == FIRST_DIP_SETUP_FAMILY) != has_first_dip_read:
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_mismatch",
            "setup_family_tape_dependency",
        )
    if has_first_dip_read:
        matching = tuple(
            row
            for row in proof.read_evidence
            if row.receipt.read_id == proof.first_dip_tape_read_id
        )
        if (
            len(matching) != 1
            or matching[0].receipt.stream is not CaptureStream.IQFEED_PRINT
            or matching[0].receipt.symbol != inputs.symbol
        ):
            raise AdaptiveRiskBuilderError(
                "builder_trusted_capture_attestation_mismatch",
                "first_dip_tape_identity",
            )
    return proof


def build_adaptive_risk_request(
    source: AdaptiveRiskBuilderSource,
    *,
    client_order_id: str,
    entry_limit_price: float,
    opportunity_key: Mapping[str, Any] | None = None,
    active_capture_attestation: ActiveCaptureInputPrefixAttestation | None = None,
    sealed_replay_attestation: (
        _SealedReplayAdaptiveRiskBuildAttestation | None
    ) = None,
) -> BuiltAdaptiveRiskRequest:
    """Resolve and build one strict request before any runner chooses quantity."""

    cid = str(client_order_id or "").strip()
    if not cid:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_boundary_mismatch", "client_order_id_missing"
        )
    trusted_capture_attestation_sha256: str | None = None
    sealed_replay_attestation_sha256: str | None = None
    if source.inputs.execution_surface == "alpaca_paper":
        if cid != source.inputs.decision_id:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch",
                "client_order_id_decision_id",
            )
        if (
            active_capture_attestation is not None
            and sealed_replay_attestation is not None
        ):
            raise AdaptiveRiskBuilderError(
                "builder_capture_attestation_ambiguous"
            )
        if (
            active_capture_attestation is None
            and sealed_replay_attestation is None
        ):
            raise AdaptiveRiskBuilderError(
                "builder_trusted_capture_attestation_unavailable"
            )
        if active_capture_attestation is not None:
            trusted = _validate_alpaca_active_capture_attestation(
                source,
                active_capture_attestation,
            )
            trusted_capture_attestation_sha256 = trusted.attestation_sha256
        elif type(sealed_replay_attestation) is not (
            _SealedReplayAdaptiveRiskBuildAttestation
        ):
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_invalid"
            )
    elif (
        active_capture_attestation is not None
        or sealed_replay_attestation is not None
    ):
        raise AdaptiveRiskBuilderError(
            "builder_trusted_capture_attestation_unexpected"
        )
    _validate_source_binding(source)
    decision = build_adaptive_risk_decision(
        source.policy, source.inputs, source.capture_binding
    )
    resolution = decision.resolution
    reservation_opportunity: dict[str, Any] | None = None
    if source.setup_family == "first_dip_reclaim":
        if not isinstance(opportunity_key, Mapping):
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch",
                "first_dip_opportunity_key_missing",
            )
        reservation_opportunity = dict(opportunity_key)
        reservation_opportunity.setdefault("account_scope", source.account_scope)
    try:
        request = AdaptiveRiskReservationRequest(
            policy=source.policy,
            inputs=source.inputs,
            account_snapshot=source.account_snapshot,
            account_scope=source.account_scope,
            setup_family=source.setup_family,
            correlation_cluster=source.correlation_cluster,
            client_order_id=cid,
            entry_limit_price=float(entry_limit_price),
            opportunity_key=reservation_opportunity,
            broker_account_evidence=(
                source.broker_account_evidence
                if source.inputs.execution_surface == "alpaca_paper"
                else None
            ),
            settled_daily_pnl_evidence=(
                source.settled_daily_pnl_evidence
                if source.inputs.execution_surface == "alpaca_paper"
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_boundary_mismatch", str(exc)
        ) from exc
    if sealed_replay_attestation is not None:
        sealed_replay_attestation_sha256 = (
            sealed_replay_attestation.claim_exact(
                source=source,
                request=request,
            )
        )
    if float(entry_limit_price) > float(resolution.effective_entry_price) + 1e-9:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_boundary_mismatch",
            "entry_limit_exceeds_resolved_effective_entry",
        )
    packet = dict(decision.decision_packet)
    claim = build_adaptive_risk_reservation_claim(packet, claim_id=cid)
    return BuiltAdaptiveRiskRequest(
        schema_version=BUILDER_RESULT_SCHEMA_VERSION,
        source_sha256=source.source_sha256,
        request=request,
        resolution=resolution,
        decision_packet=packet,
        reservation_claim=claim,
        trusted_capture_attestation_sha256=(
            trusted_capture_attestation_sha256
        ),
        sealed_replay_attestation_sha256=(
            sealed_replay_attestation_sha256
        ),
    )


@dataclass(frozen=True)
class AdaptiveRiskRuntimeCaptureMaterial:
    """Atomic process-local source plus its optional private capture proof.

    The proof is intentionally not part of ``AdaptiveRiskBuilderSource`` and
    has no mapping/deserialization path.  A live producer must return this
    exact wrapper from one callback invocation so the runner cannot combine a
    source from one capture generation with an attestation fetched separately.
    Serializable source-only providers remain useful for diagnostics and
    replay, but cannot authorize an Alpaca-paper request.
    """

    source: AdaptiveRiskBuilderSource
    active_capture_attestation: ActiveCaptureInputPrefixAttestation | None = None
    sealed_replay_attestation: (
        _SealedReplayAdaptiveRiskBuildAttestation | None
    ) = None
    locked_alpaca_paper_admission_bundle: (
        LockedAlpacaPaperAdmissionBundle | None
    ) = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, AdaptiveRiskBuilderSource):
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_source_invalid",
                "runtime_capture_material_source",
            )
        if self.active_capture_attestation is not None:
            try:
                verify_active_capture_input_attestation(
                    self.active_capture_attestation
                )
            except CaptureContractError as exc:
                raise AdaptiveRiskBuilderError(
                    "builder_trusted_capture_attestation_invalid"
                ) from exc
        if self.sealed_replay_attestation is not None and type(
            self.sealed_replay_attestation
        ) is not _SealedReplayAdaptiveRiskBuildAttestation:
            raise AdaptiveRiskBuilderError(
                "sealed_replay_adaptive_risk_attestation_invalid"
            )
        if (
            self.active_capture_attestation is not None
            and self.sealed_replay_attestation is not None
        ):
            raise AdaptiveRiskBuilderError("builder_capture_attestation_ambiguous")
        bundle = self.locked_alpaca_paper_admission_bundle
        if bundle is not None:
            if type(bundle) is not LockedAlpacaPaperAdmissionBundle:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_source_invalid",
                    "locked_alpaca_paper_admission_bundle",
                )
            try:
                bundle.verify()
                verify_locked_alpaca_paper_daily_pnl_attestation(
                    bundle.attestation
                )
            except AdaptiveRiskContractError as exc:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_source_invalid",
                    "locked_alpaca_paper_admission_bundle",
                ) from exc
            if not (
                self.source.inputs.execution_surface == "alpaca_paper"
                and self.source.inputs.as_of == bundle.decision_as_of
                and self.source.inputs.account_identity_sha256
                == bundle.account_snapshot.account_identity_sha256
                and self.source.account_snapshot.snapshot_sha256
                == bundle.account_snapshot.snapshot_sha256
                and self.source.broker_account_evidence
                == bundle.account_evidence
                and self.source.settled_daily_pnl_evidence
                == bundle.daily_pnl_evidence
            ):
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_binding_mismatch",
                    "locked_alpaca_paper_admission_bundle",
                )


AdaptiveRiskSourceProvider = Callable[
    ...,
    Mapping[str, Any]
    | AdaptiveRiskBuilderSource
    | AdaptiveRiskRuntimeCaptureMaterial
    | None,
]


class _AdaptiveRiskSourceProviderLease:
    """Shared revocation/optional one-shot state across copied contexts."""

    __slots__ = ("_lock", "_active", "_one_shot", "_claimed")

    def __init__(self, *, one_shot: bool) -> None:
        self._lock = threading.Lock()
        self._active = True
        self._one_shot = bool(one_shot)
        self._claimed = False

    def claim(self) -> str | None:
        with self._lock:
            if not self._active:
                return "builder_capture_provider_scope_revoked"
            if self._one_shot and self._claimed:
                return "builder_capture_provider_already_consumed"
            self._claimed = True
            return None

    def revoke(self) -> None:
        with self._lock:
            self._active = False


@dataclass(frozen=True)
class _InstalledAdaptiveRiskSourceProvider:
    provider: AdaptiveRiskSourceProvider
    lease: _AdaptiveRiskSourceProviderLease


_runtime_source_provider: ContextVar[
    _InstalledAdaptiveRiskSourceProvider | None
] = ContextVar(
    "adaptive_risk_runtime_source_provider", default=None
)


@contextmanager
def adaptive_risk_source_provider(
    provider: AdaptiveRiskSourceProvider | None,
    *,
    one_shot: bool = False,
) -> Iterator[None]:
    """Install a revocable capture-source seam without global monkeypatching."""

    installed = None
    if provider is not None:
        if not callable(provider):
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_source_invalid",
                "runtime_source_provider",
            )
        installed = _InstalledAdaptiveRiskSourceProvider(
            provider=provider,
            lease=_AdaptiveRiskSourceProviderLease(one_shot=one_shot),
        )
    token = _runtime_source_provider.set(installed)
    try:
        yield
    finally:
        if installed is not None:
            installed.lease.revoke()
        _runtime_source_provider.reset(token)


def runtime_adaptive_risk_capture_material(
    **boundary: Any,
) -> AdaptiveRiskRuntimeCaptureMaterial:
    """Load one atomic runtime source/proof generation and check its boundary."""

    installed = _runtime_source_provider.get()
    if installed is None:
        raise AdaptiveRiskBuilderError("builder_missing_capture_binding")
    lease_error = installed.lease.claim()
    if lease_error is not None:
        raise AdaptiveRiskBuilderError(lease_error)
    provider = installed.provider
    try:
        value = provider(**boundary)
    except AdaptiveRiskBuilderError:
        raise
    except Exception as exc:
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_source_unavailable", type(exc).__name__
        ) from exc
    if isinstance(value, AdaptiveRiskRuntimeCaptureMaterial):
        material = value
    elif isinstance(value, AdaptiveRiskBuilderSource):
        material = AdaptiveRiskRuntimeCaptureMaterial(source=value)
    elif isinstance(value, Mapping):
        # Mapping providers can supply reproducible diagnostic/replay material,
        # but never reconstruct the process-private attestation capability.
        material = AdaptiveRiskRuntimeCaptureMaterial(
            source=load_adaptive_risk_builder_source(value)
        )
    elif value is None:
        raise AdaptiveRiskBuilderError("builder_missing_capture_binding")
    else:
        raise AdaptiveRiskBuilderError("adaptive_risk_builder_source_invalid")

    source = material.source
    expected = {
        "execution_surface": source.inputs.execution_surface,
        "execution_family": source.inputs.execution_family,
        "venue": source.inputs.venue,
        "broker_environment": source.inputs.broker_environment,
        "symbol": source.inputs.symbol,
        "decision_id": source.inputs.decision_id,
        "setup_family": source.setup_family,
        "correlation_cluster": source.correlation_cluster,
    }
    for name, actual in boundary.items():
        if name not in expected or actual is None:
            continue
        normalized_actual: Any = actual
        if name == "symbol":
            normalized_actual = str(actual).strip().upper()
        elif name in {
            "execution_surface",
            "execution_family",
            "venue",
            "broker_environment",
            "setup_family",
            "correlation_cluster",
        }:
            normalized_actual = str(actual).strip().lower()
        else:
            normalized_actual = str(actual).strip()
        if normalized_actual != expected[name]:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch", name
            )
    return material


def runtime_adaptive_risk_source(**boundary: Any) -> AdaptiveRiskBuilderSource:
    """Compatibility view of the active source without elevating its trust."""

    return runtime_adaptive_risk_capture_material(**boundary).source


DbPaperFinalAdmissionProvider = Callable[
    ..., Mapping[str, Any] | DbPaperFinalAdmissionMaterial | None
]
_db_paper_final_admission_provider: ContextVar[
    DbPaperFinalAdmissionProvider | None
] = ContextVar("db_paper_final_admission_provider", default=None)


@contextmanager
def db_paper_final_admission_provider(
    provider: DbPaperFinalAdmissionProvider | None,
) -> Iterator[None]:
    """Install the single-read producer used by DB-paper final admission."""

    token = _db_paper_final_admission_provider.set(provider)
    try:
        yield
    finally:
        _db_paper_final_admission_provider.reset(token)


def runtime_db_paper_final_admission(
    **boundary: Any,
) -> DbPaperFinalAdmissionMaterial:
    """Load capture-owned material; the runner finalizes it under DB risk lock."""

    provider = _db_paper_final_admission_provider.get()
    if provider is None:
        raise AdaptiveRiskBuilderError(
            "builder_missing_final_admission_provider"
        )
    try:
        value = provider(**boundary)
    except AdaptiveRiskBuilderError:
        raise
    except Exception as exc:
        raise AdaptiveRiskBuilderError(
            "db_paper_final_admission_provider_unavailable", type(exc).__name__
        ) from exc
    if isinstance(value, DbPaperFinalAdmissionMaterial):
        # Reconstruct and rehash every public byte, then carry forward only the
        # exact process-local capability objects from the typed return value.
        # A mapping/JSON return can never acquire them.
        material = load_db_paper_final_admission_material(value.to_payload())
        material = replace(
            material,
            _first_dip_final_admission_envelope=(
                value._first_dip_final_admission_envelope
            ),
            _first_dip_final_admission_expectation=(
                value._first_dip_final_admission_expectation
            ),
        )
    elif isinstance(value, Mapping):
        material = load_db_paper_final_admission_material(value)
    elif value is None:
        raise AdaptiveRiskBuilderError(
            "builder_missing_final_admission_provider"
        )
    else:
        raise AdaptiveRiskBuilderError("db_paper_final_material_invalid")

    source = material.source
    expected = {
        "execution_surface": source.inputs.execution_surface,
        "execution_family": source.inputs.execution_family,
        "venue": source.inputs.venue,
        "broker_environment": source.inputs.broker_environment,
        "symbol": source.inputs.symbol,
        "setup_family": source.setup_family,
        "account_scope": source.account_scope,
        "account_identity_sha256": source.inputs.account_identity_sha256,
    }
    for name, expected_value in expected.items():
        if name not in boundary or boundary[name] is None:
            continue
        actual: Any = boundary[name]
        if name == "symbol":
            actual = str(actual).strip().upper()
        elif name in {"account_scope", "account_identity_sha256"}:
            actual = str(actual).strip().lower()
            expected_value = str(expected_value).strip().lower()
        else:
            actual = str(actual).strip().lower()
        if actual != expected_value:
            raise AdaptiveRiskBuilderError(
                "adaptive_risk_builder_boundary_mismatch", name
            )
    return material
