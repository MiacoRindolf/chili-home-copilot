"""Scoped typed-evidence seam for the first-dip detector.

The seam is deliberately narrower than an execution interface.  One exact
active-boundary authority may answer one immutable detector request with a
canonical ``FirstDipTapeEvaluation``.  It grants no reservation, admission,
order, or broker authority.  The production default is uninstalled and fails
the individual detector decision closed without consulting a database,
provider client, network, wall clock, or process-global cache.

The private token/HMAC/lineage are defense-in-depth against accidental copying,
serialization, and cross-context reuse.  Python is not a hostile in-process
security boundary: architectural reachability and static tests restrict the
sealed issuer/installer to ReplayV3, while operational order safeguards remain
independently mandatory.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import math
import secrets
import threading
from typing import Callable, Iterator, Mapping

from .first_dip_tape_policy import (
    FIRST_DIP_TAPE_EVALUATION_SCHEMA_VERSION,
    FirstDipTapeEvaluation,
    FirstDipTapePolicy,
    FirstDipTapePolicyError,
    FirstDipTapeReadQuery,
    evaluate_first_dip_tape,
    first_dip_tape_window_from_capture,
)
from .replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureEvent,
    CaptureStream,
    sha256_json,
    verify_active_capture_input_attestation,
)


FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE = "detector_diagnostic_only"
FIRST_DIP_TAPE_DECISION_RECEIPT_SCHEMA_VERSION = (
    "chili.first-dip-tape-decision-receipt.mechanics-only.v1"
)
FIRST_DIP_TAPE_DECISION_CAPABILITY = "iqfeed_print_v1_mechanics_only"
FIRST_DIP_TAPE_PURPOSE_DETECTOR = "detector"
FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION = "pre_reservation"
_FIRST_DIP_TAPE_DECISION_PURPOSES = frozenset(
    {
        FIRST_DIP_TAPE_PURPOSE_DETECTOR,
        FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    }
)
_FIRST_DIP_TAPE_DECISION_RECEIPT_TOKEN = object()
_FIRST_DIP_TAPE_DECISION_RECEIPT_KEY = secrets.token_bytes(32)
_FIRST_DIP_TAPE_DECISION_LINEAGE_ORIGIN = object()
_FIRST_DIP_TAPE_DECISION_AUTHORITY_TOKEN = object()
_FIRST_DIP_TAPE_DECISION_LINEAGE_SCHEMA_VERSION = (
    "chili.first-dip-tape-decision-lineage.v1"
)
_FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES = frozenset(
    {"sealed_replay", "captured_db_paper"}
)
_FIRST_DIP_TAPE_ALL_AUTHORITY_SOURCES = (
    _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES | {"exact_bound_test"}
)
_FIRST_DIP_PRIOR_DETECTOR_REFERENCE_SCHEMA_VERSION = (
    "chili.first-dip-prior-detector-reference.v1"
)


class FirstDipTapeDecisionProviderError(ValueError):
    """The scoped authority or its typed result violated the decision contract."""


def _utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise FirstDipTapeDecisionProviderError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    resolved = str(value or "").strip().lower()
    if len(resolved) != 64 or any(char not in "0123456789abcdef" for char in resolved):
        raise FirstDipTapeDecisionProviderError(f"{name} must be a lowercase SHA256")
    return resolved


@dataclass(frozen=True)
class FirstDipTapeDecisionRequest:
    """Exact detector question presented to a scoped evidence authority.

    The explicit false authority fields are part of the typed handoff so a
    caller cannot mistake this evidence request for an execution capability.
    """

    symbol: str
    decision_at: datetime
    policy: FirstDipTapePolicy
    purpose: str = FIRST_DIP_TAPE_PURPOSE_DETECTOR
    authority_scope: str = FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE
    reservation_authority: bool = False
    order_authority: bool = False

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision symbol is missing"
            )
        if not isinstance(self.policy, FirstDipTapePolicy):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision policy is not typed"
            )
        purpose = str(self.purpose or "").strip().lower()
        if purpose not in _FIRST_DIP_TAPE_DECISION_PURPOSES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision purpose is invalid"
            )
        if (
            self.authority_scope != FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE
            or self.reservation_authority is not False
            or self.order_authority is not False
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision request cannot carry execution authority"
            )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "decision_at", _utc(self.decision_at, "decision_at"))


@dataclass(frozen=True)
class _FirstDipPriorDetectorReference:
    """Typed link from a final read to its earlier accepted detector receipt."""

    run_id: str
    authority_source: str
    generation: int
    symbol: str
    decision_id: str
    decision_at: datetime
    input_prefix_root_sha256: str
    decision_checkpoint_sha256: str | None
    active_input_attestation_sha256: str | None
    read_receipt_sha256: str
    receipt_event_sha256: str
    source_event_inventory_sha256: str
    policy_sha256: str
    evaluation_sha256: str
    receipt_binding_sha256: str
    opportunity_key_sha256: str
    schema_version: str = _FIRST_DIP_PRIOR_DETECTOR_REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _FIRST_DIP_PRIOR_DETECTOR_REFERENCE_SCHEMA_VERSION:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector reference schema is invalid"
            )
        run_id = str(self.run_id or "").strip()
        source = str(self.authority_source or "").strip()
        symbol = str(self.symbol or "").strip().upper()
        decision_id = str(self.decision_id or "").strip()
        if not run_id or not symbol or not decision_id:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector identity is missing"
            )
        if source not in _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector authority source is invalid"
            )
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector generation is invalid"
            )
        for name in (
            "input_prefix_root_sha256",
            "read_receipt_sha256",
            "receipt_event_sha256",
            "source_event_inventory_sha256",
            "policy_sha256",
            "evaluation_sha256",
            "receipt_binding_sha256",
            "opportunity_key_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if source == "sealed_replay":
            object.__setattr__(
                self,
                "decision_checkpoint_sha256",
                _sha256(
                    self.decision_checkpoint_sha256,
                    "decision_checkpoint_sha256",
                ),
            )
            if self.active_input_attestation_sha256 is not None:
                raise FirstDipTapeDecisionProviderError(
                    "sealed prior detector carries live-only attestation"
                )
        else:
            object.__setattr__(
                self,
                "active_input_attestation_sha256",
                _sha256(
                    self.active_input_attestation_sha256,
                    "active_input_attestation_sha256",
                ),
            )
            if self.decision_checkpoint_sha256 is not None:
                raise FirstDipTapeDecisionProviderError(
                    "captured prior detector carries sealed checkpoint"
                )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "authority_source", source)
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(
            self,
            "decision_at",
            _utc(self.decision_at, "prior_detector.decision_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "authority_source": self.authority_source,
            "generation": self.generation,
            "symbol": self.symbol,
            "decision_id": self.decision_id,
            "decision_at": self.decision_at.isoformat().replace("+00:00", "Z"),
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "decision_checkpoint_sha256": self.decision_checkpoint_sha256,
            "active_input_attestation_sha256": (
                self.active_input_attestation_sha256
            ),
            "read_receipt_sha256": self.read_receipt_sha256,
            "receipt_event_sha256": self.receipt_event_sha256,
            "source_event_inventory_sha256": self.source_event_inventory_sha256,
            "policy_sha256": self.policy_sha256,
            "evaluation_sha256": self.evaluation_sha256,
            "receipt_binding_sha256": self.receipt_binding_sha256,
            "opportunity_key_sha256": self.opportunity_key_sha256,
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, object],
    ) -> "_FirstDipPriorDetectorReference":
        """Strictly load serialized detector lineage without granting authority."""

        expected = {
            "schema_version",
            "run_id",
            "authority_source",
            "generation",
            "symbol",
            "decision_id",
            "decision_at",
            "input_prefix_root_sha256",
            "decision_checkpoint_sha256",
            "active_input_attestation_sha256",
            "read_receipt_sha256",
            "receipt_event_sha256",
            "source_event_inventory_sha256",
            "policy_sha256",
            "evaluation_sha256",
            "receipt_binding_sha256",
            "opportunity_key_sha256",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector reference fields are invalid"
            )
        try:
            decision_at = datetime.fromisoformat(
                str(raw["decision_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector reference clock is invalid"
            ) from exc
        try:
            return cls(
                schema_version=str(raw["schema_version"]),
                run_id=str(raw["run_id"]),
                authority_source=str(raw["authority_source"]),
                generation=raw["generation"],
                symbol=str(raw["symbol"]),
                decision_id=str(raw["decision_id"]),
                decision_at=decision_at,
                input_prefix_root_sha256=str(
                    raw["input_prefix_root_sha256"]
                ),
                decision_checkpoint_sha256=(
                    None
                    if raw["decision_checkpoint_sha256"] is None
                    else str(raw["decision_checkpoint_sha256"])
                ),
                active_input_attestation_sha256=(
                    None
                    if raw["active_input_attestation_sha256"] is None
                    else str(raw["active_input_attestation_sha256"])
                ),
                read_receipt_sha256=str(raw["read_receipt_sha256"]),
                receipt_event_sha256=str(raw["receipt_event_sha256"]),
                source_event_inventory_sha256=str(
                    raw["source_event_inventory_sha256"]
                ),
                policy_sha256=str(raw["policy_sha256"]),
                evaluation_sha256=str(raw["evaluation_sha256"]),
                receipt_binding_sha256=str(raw["receipt_binding_sha256"]),
                opportunity_key_sha256=str(raw["opportunity_key_sha256"]),
            )
        except (TypeError, ValueError) as exc:
            raise FirstDipTapeDecisionProviderError(
                "first-dip prior detector reference is malformed"
            ) from exc


@dataclass(frozen=True)
class _FirstDipTapeDecisionBinding:
    """Exact immutable active-boundary identity expected by one resolver scope."""

    run_id: str
    authority_source: str
    purpose: str
    generation: int
    identity_sha256: str
    symbol: str
    decision_id: str
    decision_at: datetime
    boundary_attested_available_at: datetime
    boundary_expires_at: datetime
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    admission_handoff_sha256: str | None
    adaptive_request_sha256: str | None
    opportunity_key_sha256: str | None
    decision_checkpoint_sha256: str | None
    final_capture_seal_sha256: str | None
    coverage_manifest_sha256: str | None
    coverage_grade_sha256: str | None
    stream_coverage_sha256: str
    active_input_attestation_sha256: str | None
    active_continuity_inventory_sha256: str | None
    active_producer_generations_sha256: str | None
    active_resource_binding_sha256: str | None
    read_receipt_sha256: str
    receipt_event_sha256: str
    receipt_event_sequence: int
    receipt_committed_available_at: datetime
    source_frontier_sequence: int
    source_event_inventory_sha256: str
    watermark_event_at: datetime
    watermark_emitted_available_at: datetime
    evaluation_sha256: str
    prior_detector_reference: _FirstDipPriorDetectorReference | None

    def __post_init__(self) -> None:
        run_id = str(self.run_id or "").strip()
        authority_source = str(self.authority_source or "").strip()
        purpose = str(self.purpose or "").strip().lower()
        symbol = str(self.symbol or "").strip().upper()
        decision_id = str(self.decision_id or "").strip()
        if not run_id or not symbol or not decision_id:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision binding identity is missing"
            )
        if authority_source not in _FIRST_DIP_TAPE_ALL_AUTHORITY_SOURCES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision binding authority source is invalid"
            )
        if purpose not in _FIRST_DIP_TAPE_DECISION_PURPOSES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision binding purpose is invalid"
            )
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision binding generation must be positive"
            )
        for name in (
            "input_prefix_sequence",
            "receipt_event_sequence",
            "source_frontier_sequence",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) <= 0:
                raise FirstDipTapeDecisionProviderError(
                    f"first-dip tape decision binding {name} must be positive"
                )
            object.__setattr__(self, name, int(value))
        if not (
            self.source_frontier_sequence
            < self.receipt_event_sequence
            <= self.input_prefix_sequence
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape binding sequences escape the decision prefix"
            )
        for name in (
            "identity_sha256",
            "input_prefix_root_sha256",
            "stream_coverage_sha256",
            "read_receipt_sha256",
            "receipt_event_sha256",
            "source_event_inventory_sha256",
            "evaluation_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        handoff = self.admission_handoff_sha256
        if handoff is not None:
            handoff = _sha256(handoff, "admission_handoff_sha256")
        object.__setattr__(self, "admission_handoff_sha256", handoff)
        adaptive_request = self.adaptive_request_sha256
        if adaptive_request is not None:
            adaptive_request = _sha256(
                adaptive_request,
                "adaptive_request_sha256",
            )
        object.__setattr__(self, "adaptive_request_sha256", adaptive_request)
        opportunity = self.opportunity_key_sha256
        if opportunity is not None:
            opportunity = _sha256(opportunity, "opportunity_key_sha256")
        object.__setattr__(self, "opportunity_key_sha256", opportunity)
        sealed_fields = (
            "decision_checkpoint_sha256",
            "final_capture_seal_sha256",
            "coverage_manifest_sha256",
            "coverage_grade_sha256",
        )
        active_fields = (
            "active_input_attestation_sha256",
            "active_continuity_inventory_sha256",
            "active_producer_generations_sha256",
            "active_resource_binding_sha256",
        )
        if authority_source in {"sealed_replay", "exact_bound_test"}:
            if any(getattr(self, name) is not None for name in active_fields):
                raise FirstDipTapeDecisionProviderError(
                    "sealed first-dip binding carries live-only proof fields"
                )
            for name in sealed_fields:
                object.__setattr__(
                    self,
                    name,
                    _sha256(getattr(self, name), name),
                )
        else:
            if any(getattr(self, name) is not None for name in sealed_fields):
                raise FirstDipTapeDecisionProviderError(
                    "captured-paper first-dip binding carries future run proof"
                )
            for name in active_fields:
                object.__setattr__(
                    self,
                    name,
                    _sha256(getattr(self, name), name),
                )
        decision_at = _utc(self.decision_at, "binding.decision_at")
        attested_at = _utc(
            self.boundary_attested_available_at,
            "binding.boundary_attested_available_at",
        )
        expires_at = _utc(
            self.boundary_expires_at,
            "binding.boundary_expires_at",
        )
        committed_at = _utc(
            self.receipt_committed_available_at,
            "binding.receipt_committed_available_at",
        )
        watermark_at = _utc(
            self.watermark_event_at,
            "binding.watermark_event_at",
        )
        watermark_available = _utc(
            self.watermark_emitted_available_at,
            "binding.watermark_emitted_available_at",
        )
        if watermark_available < watermark_at or watermark_at < decision_at:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape binding watermark does not cover the decision"
            )
        if committed_at < decision_at or committed_at > watermark_available:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape binding receipt commit escapes its causal interval"
            )
        if (
            attested_at < decision_at
            or attested_at < committed_at
            or expires_at <= attested_at
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape boundary availability/expiry is invalid"
            )
        prior = self.prior_detector_reference
        if purpose == FIRST_DIP_TAPE_PURPOSE_DETECTOR:
            if prior is not None or adaptive_request is not None or opportunity is not None:
                raise FirstDipTapeDecisionProviderError(
                    "detector receipt cannot carry final-admission lineage"
                )
        elif (
            type(prior) is not _FirstDipPriorDetectorReference
            or adaptive_request is None
            or opportunity is None
            or prior.opportunity_key_sha256 != opportunity
            or prior.run_id != run_id
            or prior.authority_source != authority_source
            or prior.generation != int(self.generation)
            or prior.symbol != symbol
            or prior.decision_at > decision_at
        ):
            raise FirstDipTapeDecisionProviderError(
                "pre-reservation receipt lacks exact prior-detector lineage"
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "authority_source", authority_source)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "boundary_attested_available_at", attested_at)
        object.__setattr__(self, "boundary_expires_at", expires_at)
        object.__setattr__(self, "receipt_committed_available_at", committed_at)
        object.__setattr__(self, "watermark_event_at", watermark_at)
        object.__setattr__(
            self,
            "watermark_emitted_available_at",
            watermark_available,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "authority_source": self.authority_source,
            "purpose": self.purpose,
            "generation": self.generation,
            "identity_sha256": self.identity_sha256,
            "symbol": self.symbol,
            "decision_id": self.decision_id,
            "decision_at": self.decision_at.isoformat().replace("+00:00", "Z"),
            "boundary_attested_available_at": (
                self.boundary_attested_available_at.isoformat().replace("+00:00", "Z")
            ),
            "boundary_expires_at": self.boundary_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "input_prefix_sequence": self.input_prefix_sequence,
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "admission_handoff_sha256": self.admission_handoff_sha256,
            "adaptive_request_sha256": self.adaptive_request_sha256,
            "opportunity_key_sha256": self.opportunity_key_sha256,
            "decision_checkpoint_sha256": self.decision_checkpoint_sha256,
            "final_capture_seal_sha256": self.final_capture_seal_sha256,
            "coverage_manifest_sha256": self.coverage_manifest_sha256,
            "coverage_grade_sha256": self.coverage_grade_sha256,
            "stream_coverage_sha256": self.stream_coverage_sha256,
            "active_input_attestation_sha256": (
                self.active_input_attestation_sha256
            ),
            "active_continuity_inventory_sha256": (
                self.active_continuity_inventory_sha256
            ),
            "active_producer_generations_sha256": (
                self.active_producer_generations_sha256
            ),
            "active_resource_binding_sha256": (
                self.active_resource_binding_sha256
            ),
            "read_receipt_sha256": self.read_receipt_sha256,
            "receipt_event_sha256": self.receipt_event_sha256,
            "receipt_event_sequence": self.receipt_event_sequence,
            "receipt_committed_available_at": (
                self.receipt_committed_available_at.isoformat().replace("+00:00", "Z")
            ),
            "source_frontier_sequence": self.source_frontier_sequence,
            "source_event_inventory_sha256": self.source_event_inventory_sha256,
            "watermark_event_at": self.watermark_event_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "watermark_emitted_available_at": (
                self.watermark_emitted_available_at.isoformat().replace(
                    "+00:00", "Z"
                )
            ),
            "evaluation_sha256": self.evaluation_sha256,
            "prior_detector_reference": (
                None
                if self.prior_detector_reference is None
                else self.prior_detector_reference.to_dict()
            ),
        }

    @property
    def lineage_sha256(self) -> str:
        return sha256_json(
            {
                "schema_version": _FIRST_DIP_TAPE_DECISION_LINEAGE_SCHEMA_VERSION,
                "binding": self.to_dict(),
            }
        )

    @classmethod
    def from_receipt(
        cls,
        receipt: "FirstDipTapeDecisionReceipt",
    ) -> "_FirstDipTapeDecisionBinding":
        return cls(
            run_id=receipt.run_id,
            authority_source=receipt.authority_source,
            purpose=receipt.purpose,
            generation=receipt.generation,
            identity_sha256=receipt.identity_sha256,
            symbol=receipt.evaluation.symbol,
            decision_id=receipt.decision_id,
            decision_at=receipt.decision_at,
            boundary_attested_available_at=(
                receipt.boundary_attested_available_at
            ),
            boundary_expires_at=receipt.boundary_expires_at,
            input_prefix_sequence=receipt.input_prefix_sequence,
            input_prefix_root_sha256=receipt.input_prefix_root_sha256,
            admission_handoff_sha256=receipt.admission_handoff_sha256,
            adaptive_request_sha256=receipt.adaptive_request_sha256,
            opportunity_key_sha256=receipt.opportunity_key_sha256,
            decision_checkpoint_sha256=receipt.decision_checkpoint_sha256,
            final_capture_seal_sha256=receipt.final_capture_seal_sha256,
            coverage_manifest_sha256=receipt.coverage_manifest_sha256,
            coverage_grade_sha256=receipt.coverage_grade_sha256,
            stream_coverage_sha256=receipt.stream_coverage_sha256,
            active_input_attestation_sha256=(
                receipt.active_input_attestation_sha256
            ),
            active_continuity_inventory_sha256=(
                receipt.active_continuity_inventory_sha256
            ),
            active_producer_generations_sha256=(
                receipt.active_producer_generations_sha256
            ),
            active_resource_binding_sha256=(
                receipt.active_resource_binding_sha256
            ),
            read_receipt_sha256=receipt.read_receipt_sha256,
            receipt_event_sha256=receipt.receipt_event_sha256,
            receipt_event_sequence=receipt.receipt_event_sequence,
            receipt_committed_available_at=receipt.receipt_committed_available_at,
            source_frontier_sequence=receipt.source_frontier_sequence,
            source_event_inventory_sha256=receipt.source_event_inventory_sha256,
            watermark_event_at=receipt.watermark_event_at,
            watermark_emitted_available_at=receipt.watermark_emitted_available_at,
            evaluation_sha256=receipt.evaluation.evaluation_sha256,
            prior_detector_reference=receipt.prior_detector_reference,
        )


class _FirstDipTapeDecisionLineage:
    """Ephemeral process-local one-shot state shared by every receipt copy."""

    __slots__ = (
        "lineage_sha256",
        "_origin",
        "_lock",
        "_consumed",
        "_final_envelope_issued",
    )

    def __init__(self, lineage_sha256: str, *, _origin: object) -> None:
        if _origin is not _FIRST_DIP_TAPE_DECISION_LINEAGE_ORIGIN:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision lineage origin is invalid"
            )
        self.lineage_sha256 = _sha256(lineage_sha256, "lineage_sha256")
        self._origin = _origin
        self._lock = threading.Lock()
        self._consumed = False
        self._final_envelope_issued = False

    def consume_once(self) -> bool:
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True

    @property
    def consumed(self) -> bool:
        with self._lock:
            return bool(self._consumed)

    def claim_final_envelope_once(self) -> bool:
        """Allow exactly one typed final handoff after detector acceptance."""

        with self._lock:
            if not self._consumed or self._final_envelope_issued:
                return False
            self._final_envelope_issued = True
            return True

    def __copy__(self) -> "_FirstDipTapeDecisionLineage":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> "_FirstDipTapeDecisionLineage":
        memo[id(self)] = self
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip tape decision lineage cannot be pickled")


@dataclass(frozen=True)
class FirstDipTapeDecisionReceipt:
    """Private-token proof that one sealed run produced one tape evaluation.

    This object deliberately has no public deserializer.  Its ordinary
    ``binding_sha256`` is useful audit provenance, while the private keyed tag
    rejects ordinary caller-built dataclasses and JSON round trips.  This is
    defense in depth inside a cooperative process, not a hostile-code boundary.
    The capability is mechanics-only and can never grant reservation or order
    authority.
    """

    run_id: str
    authority_source: str
    purpose: str
    generation: int
    identity_sha256: str
    decision_id: str
    decision_at: datetime
    boundary_attested_available_at: datetime
    boundary_expires_at: datetime
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    admission_handoff_sha256: str | None
    adaptive_request_sha256: str | None
    opportunity_key_sha256: str | None
    decision_checkpoint_sha256: str | None
    final_capture_seal_sha256: str | None
    coverage_manifest_sha256: str | None
    coverage_grade_sha256: str | None
    stream_coverage_sha256: str
    active_input_attestation_sha256: str | None
    active_continuity_inventory_sha256: str | None
    active_producer_generations_sha256: str | None
    active_resource_binding_sha256: str | None
    read_receipt_sha256: str
    receipt_event_sha256: str
    receipt_event_sequence: int
    receipt_committed_available_at: datetime
    source_frontier_sequence: int
    source_event_inventory_sha256: str
    watermark_event_at: datetime
    watermark_emitted_available_at: datetime
    prior_detector_reference: _FirstDipPriorDetectorReference | None
    evaluation: FirstDipTapeEvaluation
    capability: str = FIRST_DIP_TAPE_DECISION_CAPABILITY
    authority_scope: str = FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE
    reservation_authority: bool = False
    order_authority: bool = False
    schema_version: str = FIRST_DIP_TAPE_DECISION_RECEIPT_SCHEMA_VERSION
    _verification_token: object = field(default=None, repr=False, compare=False)
    _verification_tag: str = field(default="", repr=False, compare=False)
    _lineage: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _FIRST_DIP_TAPE_DECISION_RECEIPT_TOKEN:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt was not issued by the runtime"
            )
        if self.schema_version != FIRST_DIP_TAPE_DECISION_RECEIPT_SCHEMA_VERSION:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt schema is unsupported"
            )
        run_id = str(self.run_id or "").strip()
        authority_source = str(self.authority_source or "").strip()
        purpose = str(self.purpose or "").strip().lower()
        decision_id = str(self.decision_id or "").strip()
        if not run_id or not decision_id:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision run identity is missing"
            )
        if authority_source not in _FIRST_DIP_TAPE_ALL_AUTHORITY_SOURCES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt authority source is invalid"
            )
        if purpose not in _FIRST_DIP_TAPE_DECISION_PURPOSES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt purpose is invalid"
            )
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision generation must be positive"
            )
        for name in (
            "input_prefix_sequence",
            "receipt_event_sequence",
            "source_frontier_sequence",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) <= 0:
                raise FirstDipTapeDecisionProviderError(
                    f"first-dip tape decision {name} must be positive"
                )
            object.__setattr__(self, name, int(value))
        if not (
            self.source_frontier_sequence
            < self.receipt_event_sequence
            <= self.input_prefix_sequence
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape receipt/source sequences escape the decision prefix"
            )
        for name in (
            "identity_sha256",
            "input_prefix_root_sha256",
            "stream_coverage_sha256",
            "read_receipt_sha256",
            "receipt_event_sha256",
            "source_event_inventory_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        handoff = self.admission_handoff_sha256
        if handoff is not None:
            handoff = _sha256(handoff, "admission_handoff_sha256")
        object.__setattr__(self, "admission_handoff_sha256", handoff)
        adaptive_request = self.adaptive_request_sha256
        if adaptive_request is not None:
            adaptive_request = _sha256(
                adaptive_request,
                "adaptive_request_sha256",
            )
        object.__setattr__(self, "adaptive_request_sha256", adaptive_request)
        opportunity = self.opportunity_key_sha256
        if opportunity is not None:
            opportunity = _sha256(opportunity, "opportunity_key_sha256")
        object.__setattr__(self, "opportunity_key_sha256", opportunity)
        sealed_fields = (
            "decision_checkpoint_sha256",
            "final_capture_seal_sha256",
            "coverage_manifest_sha256",
            "coverage_grade_sha256",
        )
        active_fields = (
            "active_input_attestation_sha256",
            "active_continuity_inventory_sha256",
            "active_producer_generations_sha256",
            "active_resource_binding_sha256",
        )
        if authority_source in {"sealed_replay", "exact_bound_test"}:
            if any(getattr(self, name) is not None for name in active_fields):
                raise FirstDipTapeDecisionProviderError(
                    "sealed first-dip receipt carries live-only proof fields"
                )
            for name in sealed_fields:
                object.__setattr__(
                    self,
                    name,
                    _sha256(getattr(self, name), name),
                )
        else:
            if any(getattr(self, name) is not None for name in sealed_fields):
                raise FirstDipTapeDecisionProviderError(
                    "captured-paper first-dip receipt carries future run proof"
                )
            for name in active_fields:
                object.__setattr__(
                    self,
                    name,
                    _sha256(getattr(self, name), name),
                )
        decision_at = _utc(self.decision_at, "receipt.decision_at")
        attested_at = _utc(
            self.boundary_attested_available_at,
            "receipt.boundary_attested_available_at",
        )
        expires_at = _utc(
            self.boundary_expires_at,
            "receipt.boundary_expires_at",
        )
        committed_at = _utc(
            self.receipt_committed_available_at,
            "receipt.receipt_committed_available_at",
        )
        watermark_at = _utc(self.watermark_event_at, "receipt.watermark_event_at")
        watermark_available = _utc(
            self.watermark_emitted_available_at,
            "receipt.watermark_emitted_available_at",
        )
        if watermark_available < watermark_at or watermark_at < decision_at:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape watermark does not cover the decision clock"
            )
        if committed_at < decision_at or committed_at > watermark_available:
            # A receipt may publish after its economic decision clock, but the
            # continuity proof that covers it cannot predate that commit.
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape receipt commit escapes its causal interval"
            )
        if (
            attested_at < decision_at
            or attested_at < committed_at
            or expires_at <= attested_at
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape receipt availability/expiry is invalid"
            )
        if not isinstance(self.evaluation, FirstDipTapeEvaluation):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt evaluation is not typed"
            )
        prior = self.prior_detector_reference
        if purpose == FIRST_DIP_TAPE_PURPOSE_DETECTOR:
            if prior is not None or adaptive_request is not None or opportunity is not None:
                raise FirstDipTapeDecisionProviderError(
                    "detector receipt cannot carry final-admission lineage"
                )
        elif (
            type(prior) is not _FirstDipPriorDetectorReference
            or adaptive_request is None
            or opportunity is None
            or prior.opportunity_key_sha256 != opportunity
            or prior.run_id != run_id
            or prior.authority_source != authority_source
            or prior.generation != int(self.generation)
            or prior.symbol != self.evaluation.symbol
            or prior.decision_at > decision_at
        ):
            raise FirstDipTapeDecisionProviderError(
                "pre-reservation receipt lacks exact prior-detector lineage"
            )
        if self.evaluation.decision_at != decision_at:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape receipt/evaluation decision clock mismatch"
            )
        if self.capability != FIRST_DIP_TAPE_DECISION_CAPABILITY or (
            self.authority_scope != FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE
            or self.reservation_authority is not False
            or self.order_authority is not False
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape v1 receipt cannot carry execution authority"
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "authority_source", authority_source)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "boundary_attested_available_at", attested_at)
        object.__setattr__(self, "boundary_expires_at", expires_at)
        object.__setattr__(self, "receipt_committed_available_at", committed_at)
        object.__setattr__(self, "watermark_event_at", watermark_at)
        object.__setattr__(
            self, "watermark_emitted_available_at", watermark_available
        )
        if (
            type(self._lineage) is not _FirstDipTapeDecisionLineage
            or self._lineage._origin
            is not _FIRST_DIP_TAPE_DECISION_LINEAGE_ORIGIN
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt lineage is invalid"
            )
        expected_lineage = _FirstDipTapeDecisionBinding.from_receipt(
            self
        ).lineage_sha256
        if self._lineage.lineage_sha256 != expected_lineage:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision receipt lineage binding mismatch"
            )
        if self._verification_tag:
            expected_tag = _receipt_verification_tag(self)
            if not hmac.compare_digest(self._verification_tag, expected_tag):
                raise FirstDipTapeDecisionProviderError(
                    "first-dip tape decision receipt verification failed"
                )

    def body_without_private_tag(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "authority_source": self.authority_source,
            "purpose": self.purpose,
            "generation": self.generation,
            "identity_sha256": self.identity_sha256,
            "decision_id": self.decision_id,
            "decision_at": self.decision_at.isoformat().replace("+00:00", "Z"),
            "boundary_attested_available_at": (
                self.boundary_attested_available_at.isoformat().replace("+00:00", "Z")
            ),
            "boundary_expires_at": self.boundary_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "input_prefix_sequence": self.input_prefix_sequence,
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "admission_handoff_sha256": self.admission_handoff_sha256,
            "adaptive_request_sha256": self.adaptive_request_sha256,
            "opportunity_key_sha256": self.opportunity_key_sha256,
            "decision_checkpoint_sha256": self.decision_checkpoint_sha256,
            "final_capture_seal_sha256": self.final_capture_seal_sha256,
            "coverage_manifest_sha256": self.coverage_manifest_sha256,
            "coverage_grade_sha256": self.coverage_grade_sha256,
            "stream_coverage_sha256": self.stream_coverage_sha256,
            "active_input_attestation_sha256": (
                self.active_input_attestation_sha256
            ),
            "active_continuity_inventory_sha256": (
                self.active_continuity_inventory_sha256
            ),
            "active_producer_generations_sha256": (
                self.active_producer_generations_sha256
            ),
            "active_resource_binding_sha256": (
                self.active_resource_binding_sha256
            ),
            "read_receipt_sha256": self.read_receipt_sha256,
            "receipt_event_sha256": self.receipt_event_sha256,
            "receipt_event_sequence": self.receipt_event_sequence,
            "receipt_committed_available_at": (
                self.receipt_committed_available_at.isoformat().replace("+00:00", "Z")
            ),
            "source_frontier_sequence": self.source_frontier_sequence,
            "source_event_inventory_sha256": self.source_event_inventory_sha256,
            "watermark_event_at": self.watermark_event_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "watermark_emitted_available_at": (
                self.watermark_emitted_available_at.isoformat().replace(
                    "+00:00", "Z"
                )
            ),
            "evaluation": self.evaluation.to_dict(),
            "evaluation_sha256": self.evaluation.evaluation_sha256,
            "prior_detector_reference": (
                None
                if self.prior_detector_reference is None
                else self.prior_detector_reference.to_dict()
            ),
            "lineage_sha256": self._lineage.lineage_sha256,
            "capability": self.capability,
            "authority_scope": self.authority_scope,
            "reservation_authority": False,
            "order_authority": False,
        }

    @property
    def binding_sha256(self) -> str:
        return sha256_json(self.body_without_private_tag())

    def to_audit_dict(self) -> dict[str, object]:
        payload = self.body_without_private_tag()
        payload.pop("evaluation", None)
        payload["binding_sha256"] = self.binding_sha256
        payload["run_bound"] = (
            self.authority_source in _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES
        )
        return payload

    def __copy__(self) -> "FirstDipTapeDecisionReceipt":
        return self

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> "FirstDipTapeDecisionReceipt":
        memo[id(self)] = self
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip tape decision receipt cannot be pickled")


def _receipt_verification_tag(receipt: FirstDipTapeDecisionReceipt) -> str:
    return hmac.new(
        _FIRST_DIP_TAPE_DECISION_RECEIPT_KEY,
        receipt.binding_sha256.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _verify_first_dip_tape_decision_receipt(
    value: object,
) -> FirstDipTapeDecisionReceipt:
    if type(value) is not FirstDipTapeDecisionReceipt:
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape decision provider result is not a runtime receipt"
        )
    if value._verification_token is not _FIRST_DIP_TAPE_DECISION_RECEIPT_TOKEN:
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape decision receipt token is invalid"
        )
    if (
        type(value._lineage) is not _FirstDipTapeDecisionLineage
        or value._lineage._origin is not _FIRST_DIP_TAPE_DECISION_LINEAGE_ORIGIN
        or value._lineage.lineage_sha256
        != _FirstDipTapeDecisionBinding.from_receipt(value).lineage_sha256
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape decision receipt lineage is invalid"
        )
    try:
        expected = _receipt_verification_tag(value)
    except Exception as exc:
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape decision receipt binding is malformed"
        ) from exc
    if not value._verification_tag or not hmac.compare_digest(
        value._verification_tag, expected
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape decision receipt verification failed"
        )
    return value


@dataclass(frozen=True)
class FirstDipTapeDecisionResolution:
    """Canonical detector result plus optional private run-bound provenance."""

    evaluation: FirstDipTapeEvaluation
    receipt: FirstDipTapeDecisionReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.evaluation) is not FirstDipTapeEvaluation:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision resolution is not typed"
            )
        if self.receipt is not None:
            verified = _verify_first_dip_tape_decision_receipt(self.receipt)
            if verified.evaluation.to_dict() != self.evaluation.to_dict():
                raise FirstDipTapeDecisionProviderError(
                    "first-dip tape decision receipt/evaluation mismatch"
                )

    @property
    def run_bound(self) -> bool:
        return bool(
            self.receipt is not None
            and self.receipt.authority_source
            in _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES
        )

    def to_dict(self) -> dict[str, object]:
        return self.evaluation.to_dict()

    @property
    def evaluation_sha256(self) -> str:
        return self.evaluation.evaluation_sha256

    def __getattr__(self, name: str) -> object:
        # Preserve the small read-only evaluation surface used by the detector.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.evaluation, name)


def _prior_detector_reference_from_resolution(
    resolution: FirstDipTapeDecisionResolution,
    *,
    opportunity_key_sha256: str,
) -> _FirstDipPriorDetectorReference:
    """Derive a final-checkpoint lineage link from one accepted detector receipt."""

    if type(resolution) is not FirstDipTapeDecisionResolution:
        raise FirstDipTapeDecisionProviderError(
            "first-dip prior detector resolution is not typed"
        )
    receipt = resolution.receipt
    if receipt is None:
        raise FirstDipTapeDecisionProviderError(
            "first-dip prior detector receipt is missing"
        )
    receipt = _verify_first_dip_tape_decision_receipt(receipt)
    if (
        receipt.purpose != FIRST_DIP_TAPE_PURPOSE_DETECTOR
        or receipt.authority_source not in _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES
        or resolution.evaluation.status != "valid_positive"
        or resolution.evaluation.confirmed is not True
        or resolution.evaluation.reason != "first_dip_tape_confirmed"
        or not receipt._lineage.consumed
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip prior detector receipt was not positively accepted"
        )
    return _FirstDipPriorDetectorReference(
        run_id=receipt.run_id,
        authority_source=receipt.authority_source,
        generation=receipt.generation,
        symbol=receipt.evaluation.symbol,
        decision_id=receipt.decision_id,
        decision_at=receipt.decision_at,
        input_prefix_root_sha256=receipt.input_prefix_root_sha256,
        decision_checkpoint_sha256=receipt.decision_checkpoint_sha256,
        active_input_attestation_sha256=(
            receipt.active_input_attestation_sha256
        ),
        read_receipt_sha256=receipt.read_receipt_sha256,
        receipt_event_sha256=receipt.receipt_event_sha256,
        source_event_inventory_sha256=receipt.source_event_inventory_sha256,
        policy_sha256=receipt.evaluation.policy_sha256,
        evaluation_sha256=receipt.evaluation.evaluation_sha256,
        receipt_binding_sha256=receipt.binding_sha256,
        opportunity_key_sha256=_sha256(
            opportunity_key_sha256,
            "opportunity_key_sha256",
        ),
    )


@dataclass(frozen=True)
class _VerifiedFirstDipTapeDecisionAuthority:
    """One exact boundary capability prepared by a sealed adapter.

    The authority is deliberately not a callable/provider surface.  It owns one
    pre-minted receipt, its independently retained expected binding, and the
    exact detector question that may consume it.  Production construction is
    limited to the sealed-replay issuer below; focused tests use a separate
    exact-bound source that cannot choose provenance fields.
    """

    request: FirstDipTapeDecisionRequest
    expected_binding: _FirstDipTapeDecisionBinding
    receipt: FirstDipTapeDecisionReceipt
    source: str
    _on_accept: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _authority_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _FIRST_DIP_TAPE_DECISION_AUTHORITY_TOKEN:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority origin is invalid"
            )
        if type(self.request) is not FirstDipTapeDecisionRequest:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority request is not exact"
            )
        if type(self.expected_binding) is not _FirstDipTapeDecisionBinding:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority binding is not exact"
            )
        if self.source not in _FIRST_DIP_TAPE_ALL_AUTHORITY_SOURCES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority source is invalid"
            )
        if (
            self.expected_binding.authority_source != self.source
            or self.receipt.authority_source != self.source
            or self.expected_binding.purpose != self.request.purpose
            or self.receipt.purpose != self.request.purpose
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority source binding mismatch"
            )
        verified = _verify_first_dip_tape_decision_receipt(self.receipt)
        mismatch = _first_dip_tape_receipt_binding_mismatch(
            verified,
            self.expected_binding,
        )
        if mismatch is not None:
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority receipt binding mismatch: "
                + mismatch
            )
        if (
            self.expected_binding.decision_at != self.request.decision_at
            or self.expected_binding.symbol != self.request.symbol
            or verified.evaluation.symbol != self.request.symbol
            or verified.evaluation.decision_at != self.request.decision_at
            or verified.evaluation.policy_sha256
            != self.request.policy.policy_sha256
            or (
                self.expected_binding.prior_detector_reference is not None
                and self.expected_binding.prior_detector_reference.policy_sha256
                != self.request.policy.policy_sha256
            )
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority escaped its exact request"
            )
        if self._on_accept is not None and not callable(self._on_accept):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority accept hook is invalid"
            )

    def receipt_for(
        self,
        request: FirstDipTapeDecisionRequest,
    ) -> FirstDipTapeDecisionReceipt:
        if type(request) is not FirstDipTapeDecisionRequest or (
            request.symbol != self.request.symbol
            or request.decision_at != self.request.decision_at
            or request.policy.to_dict() != self.request.policy.to_dict()
            or request.policy.policy_sha256 != self.request.policy.policy_sha256
            or request.purpose != self.request.purpose
            or request.authority_scope != self.request.authority_scope
            or request.reservation_authority is not False
            or request.order_authority is not False
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision request does not match active authority"
            )
        return self.receipt

    def notify_accepted(self) -> None:
        if self._on_accept is not None:
            self._on_accept()

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip tape decision authority cannot be pickled")


def _first_dip_tape_receipt_binding_mismatch(
    receipt: FirstDipTapeDecisionReceipt,
    expected: _FirstDipTapeDecisionBinding,
) -> str | None:
    """Return the first independently compared active-boundary field mismatch."""

    if type(expected) is not _FirstDipTapeDecisionBinding:
        return "expected_binding_type"
    try:
        actual = _FirstDipTapeDecisionBinding.from_receipt(receipt)
    except (FirstDipTapeDecisionProviderError, TypeError, ValueError):
        return "receipt_binding"
    actual_values = actual.to_dict()
    expected_values = expected.to_dict()
    if set(actual_values) != set(expected_values):
        return "binding_fields"
    for name in expected_values:
        if actual_values[name] != expected_values[name]:
            return name
    return None


class _FirstDipTapeDecisionAuthorityIssuer:
    """Private typed issuer; there is intentionally no arbitrary kwargs mint."""

    __slots__ = ()

    def issue_sealed_replay(
        self,
        request: FirstDipTapeDecisionRequest,
        binding: _FirstDipTapeDecisionBinding,
        evaluation: FirstDipTapeEvaluation,
        on_accept: Callable[[], None],
    ) -> _VerifiedFirstDipTapeDecisionAuthority:
        return self._issue_exact(
            request,
            binding,
            evaluation,
            source="sealed_replay",
            on_accept=on_accept,
        )

    def issue_captured_db_paper_from_active_input(
        self,
        *,
        attestation: ActiveCaptureInputPrefixAttestation,
        source_events: tuple[CaptureEvent, ...],
        policy: FirstDipTapePolicy,
        purpose: str,
        prior_detector_reference: _FirstDipPriorDetectorReference | None = None,
    ) -> _VerifiedFirstDipTapeDecisionAuthority:
        """Derive captured-paper authority only from a private active input proof.

        No caller supplies provenance hashes, an evaluation, or an accept
        callback.  Every binding field is recomputed from the runtime-issued
        attestation and its exact IQFeed receipt/source inventory.
        """

        try:
            proof = verify_active_capture_input_attestation(attestation)
            normalized_purpose = str(purpose or "").strip().lower()
            if normalized_purpose not in _FIRST_DIP_TAPE_DECISION_PURPOSES:
                raise FirstDipTapeDecisionProviderError(
                    "captured first-dip authority purpose is invalid"
                )
            if prior_detector_reference is not None and (
                type(prior_detector_reference) is not _FirstDipPriorDetectorReference
            ):
                raise FirstDipTapeDecisionProviderError(
                    "captured first-dip prior detector reference is not typed"
                )
            prior_reference_sha256 = (
                None
                if prior_detector_reference is None
                else sha256_json(prior_detector_reference.to_dict())
            )
            if normalized_purpose == FIRST_DIP_TAPE_PURPOSE_DETECTOR:
                if (
                    prior_detector_reference is not None
                    or proof.first_dip_prior_detector_reference_sha256 is not None
                    or proof.first_dip_adaptive_request_sha256 is not None
                    or proof.first_dip_opportunity_key_sha256 is not None
                ):
                    raise FirstDipTapeDecisionProviderError(
                        "captured detector proof carries final-admission lineage"
                    )
            elif (
                type(prior_detector_reference) is not _FirstDipPriorDetectorReference
                or proof.first_dip_prior_detector_reference_sha256
                != prior_reference_sha256
                or proof.first_dip_adaptive_request_sha256 is None
                or proof.first_dip_opportunity_key_sha256
                != prior_detector_reference.opportunity_key_sha256
            ):
                raise FirstDipTapeDecisionProviderError(
                    "captured pre-reservation proof lacks typed prior-detector lineage"
                )
            read_id = proof.first_dip_tape_read_id
            rows = tuple(
                row
                for row in proof.read_evidence
                if row.receipt.read_id == read_id
            )
            continuity_rows = tuple(
                row
                for row in proof.continuity_evidence
                if row.coverage.stream is CaptureStream.IQFEED_PRINT
            )
            if read_id is None or len(rows) != 1 or len(continuity_rows) != 1:
                raise FirstDipTapeDecisionProviderError(
                    "captured first-dip active proof is incomplete"
                )
            read = rows[0]
            receipt = read.receipt
            if receipt.query is None:
                raise FirstDipTapeDecisionProviderError(
                    "captured first-dip receipt lacks typed query"
                )
            query = FirstDipTapeReadQuery.from_dict(receipt.query)
            query.validate_for_policy(policy)
            events = tuple(source_events)
            if (
                tuple(event.event_sha256 for event in events)
                != receipt.source_event_sha256s
                or tuple(ref.event_sha256 for ref in read.source_event_refs)
                != receipt.source_event_sha256s
            ):
                raise FirstDipTapeDecisionProviderError(
                    "captured first-dip source inventory mismatch"
                )
            window = first_dip_tape_window_from_capture(receipt, events)
            evaluation = evaluate_first_dip_tape(
                window,
                policy=policy,
                decision_at=query.decision_at,
                symbol=str(receipt.symbol or ""),
            )
            continuity = continuity_rows[0]
            watermark = continuity.coverage.watermark
            if (
                watermark is None
                or query.symbol != str(receipt.symbol or "").strip().upper()
                or query.decision_at != receipt.returned_at
                or query.source_frontier_sequence
                != max(
                    (
                        ref.sequence
                        for ref in read.source_event_refs
                    ),
                    default=query.source_frontier_sequence,
                )
                or watermark.event_watermark_at < query.event_end_inclusive
                or proof.attested_available_at > proof.expires_at
            ):
                raise FirstDipTapeDecisionProviderError(
                    "captured first-dip active proof is not causal"
                )
            request = FirstDipTapeDecisionRequest(
                symbol=query.symbol,
                decision_at=query.decision_at,
                policy=policy,
                purpose=normalized_purpose,
            )
            binding = _FirstDipTapeDecisionBinding(
                run_id=proof.run_id,
                authority_source="captured_db_paper",
                purpose=normalized_purpose,
                generation=proof.generation,
                identity_sha256=proof.identity_sha256,
                symbol=query.symbol,
                decision_id=proof.decision_id,
                decision_at=query.decision_at,
                boundary_attested_available_at=proof.attested_available_at,
                boundary_expires_at=proof.expires_at,
                input_prefix_sequence=proof.input_prefix_sequence,
                input_prefix_root_sha256=proof.input_prefix_root_sha256,
                admission_handoff_sha256=proof.admission_handoff_sha256,
                adaptive_request_sha256=(
                    proof.first_dip_adaptive_request_sha256
                ),
                opportunity_key_sha256=(
                    proof.first_dip_opportunity_key_sha256
                ),
                decision_checkpoint_sha256=None,
                final_capture_seal_sha256=None,
                coverage_manifest_sha256=None,
                coverage_grade_sha256=None,
                stream_coverage_sha256=sha256_json(
                    continuity.coverage.to_dict()
                ),
                active_input_attestation_sha256=proof.attestation_sha256,
                active_continuity_inventory_sha256=(
                    proof.continuity_evidence_inventory_sha256
                ),
                active_producer_generations_sha256=sha256_json(
                    {"producer_generations": dict(proof.producer_generations)}
                ),
                active_resource_binding_sha256=proof.resource_binding_sha256,
                read_receipt_sha256=read.receipt_sha256,
                receipt_event_sha256=read.receipt_event_sha256,
                receipt_event_sequence=read.receipt_event_sequence,
                receipt_committed_available_at=(
                    read.receipt_committed_available_at
                ),
                source_frontier_sequence=query.source_frontier_sequence,
                source_event_inventory_sha256=sha256_json(
                    {
                        "read_id": receipt.read_id,
                        "source_event_sha256s": list(
                            receipt.source_event_sha256s
                        ),
                    }
                ),
                watermark_event_at=watermark.event_watermark_at,
                watermark_emitted_available_at=(
                    watermark.emitted_available_at
                ),
                evaluation_sha256=evaluation.evaluation_sha256,
                prior_detector_reference=prior_detector_reference,
            )
        except FirstDipTapeDecisionProviderError:
            raise
        except (
            CaptureContractError,
            FirstDipTapePolicyError,
            IndexError,
            OverflowError,
            TypeError,
            ValueError,
        ) as exc:
            raise FirstDipTapeDecisionProviderError(
                "captured first-dip active proof is invalid"
            ) from exc
        return self._issue_exact(
            request,
            binding,
            evaluation,
            source="captured_db_paper",
            on_accept=None,
        )

    def issue_exact_bound_test(
        self,
        request: FirstDipTapeDecisionRequest,
        evaluation: FirstDipTapeEvaluation,
    ) -> _VerifiedFirstDipTapeDecisionAuthority:
        if type(request) is not FirstDipTapeDecisionRequest or (
            type(evaluation) is not FirstDipTapeEvaluation
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape exact-bound test inputs are not typed"
            )
        if request.purpose != FIRST_DIP_TAPE_PURPOSE_DETECTOR:
            raise FirstDipTapeDecisionProviderError(
                "exact-bound test issuer is detector-only"
            )
        seed = {
            "schema_version": "chili.first-dip-tape-test-boundary.v1",
            "request": {
                "symbol": request.symbol,
                "decision_at": request.decision_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "policy": request.policy.to_dict(),
                "purpose": request.purpose,
            },
            "evaluation_sha256": evaluation.evaluation_sha256,
        }
        boundary_sha256 = sha256_json(seed)

        def digest(label: str) -> str:
            return sha256_json(
                {
                    "schema_version": "chili.first-dip-tape-test-binding.v1",
                    "boundary_sha256": boundary_sha256,
                    "field": label,
                }
            )

        binding = _FirstDipTapeDecisionBinding(
            run_id=f"exact-bound-test:{boundary_sha256}",
            authority_source="exact_bound_test",
            purpose=request.purpose,
            generation=1,
            identity_sha256=digest("identity"),
            symbol=request.symbol,
            decision_id=f"exact-bound-test-decision:{boundary_sha256}",
            decision_at=request.decision_at,
            boundary_attested_available_at=(
                request.decision_at + timedelta(seconds=0.5)
            ),
            boundary_expires_at=request.decision_at + timedelta(seconds=3),
            input_prefix_sequence=4,
            input_prefix_root_sha256=digest("input_prefix_root"),
            admission_handoff_sha256=None,
            adaptive_request_sha256=None,
            opportunity_key_sha256=None,
            decision_checkpoint_sha256=digest("decision_checkpoint"),
            final_capture_seal_sha256=digest("final_capture_seal"),
            coverage_manifest_sha256=digest("coverage_manifest"),
            coverage_grade_sha256=digest("coverage_grade"),
            stream_coverage_sha256=digest("stream_coverage"),
            active_input_attestation_sha256=None,
            active_continuity_inventory_sha256=None,
            active_producer_generations_sha256=None,
            active_resource_binding_sha256=None,
            read_receipt_sha256=digest("read_receipt"),
            receipt_event_sha256=digest("receipt_event"),
            receipt_event_sequence=3,
            receipt_committed_available_at=(
                request.decision_at + timedelta(seconds=0.5)
            ),
            source_frontier_sequence=2,
            source_event_inventory_sha256=digest("source_event_inventory"),
            watermark_event_at=request.decision_at + timedelta(seconds=1),
            watermark_emitted_available_at=(
                request.decision_at + timedelta(seconds=2)
            ),
            evaluation_sha256=evaluation.evaluation_sha256,
            prior_detector_reference=None,
        )
        return self._issue_exact(
            request,
            binding,
            evaluation,
            source="exact_bound_test",
            on_accept=None,
        )

    @staticmethod
    def _issue_exact(
        request: FirstDipTapeDecisionRequest,
        binding: _FirstDipTapeDecisionBinding,
        evaluation: FirstDipTapeEvaluation,
        *,
        source: str,
        on_accept: Callable[[], None] | None,
    ) -> _VerifiedFirstDipTapeDecisionAuthority:
        if (
            type(request) is not FirstDipTapeDecisionRequest
            or type(binding) is not _FirstDipTapeDecisionBinding
            or type(evaluation) is not FirstDipTapeEvaluation
            or binding.authority_source != source
            or binding.purpose != request.purpose
            or binding.symbol != request.symbol
            or binding.decision_at != request.decision_at
            or binding.evaluation_sha256 != evaluation.evaluation_sha256
            or evaluation.symbol != request.symbol
            or evaluation.decision_at != request.decision_at
            or evaluation.policy_sha256 != request.policy.policy_sha256
            or (
                binding.prior_detector_reference is not None
                and binding.prior_detector_reference.policy_sha256
                != request.policy.policy_sha256
            )
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority inputs are not exactly bound"
            )
        canonical, reason = _canonical_result_or_reason(
            evaluation,
            request=request,
        )
        if canonical is None or canonical.to_dict() != evaluation.to_dict():
            raise FirstDipTapeDecisionProviderError(
                "first-dip tape decision authority evaluation is invalid: "
                + str(reason or "unknown")
            )
        lineage = _FirstDipTapeDecisionLineage(
            binding.lineage_sha256,
            _origin=_FIRST_DIP_TAPE_DECISION_LINEAGE_ORIGIN,
        )
        receipt = FirstDipTapeDecisionReceipt(
            run_id=binding.run_id,
            authority_source=binding.authority_source,
            purpose=binding.purpose,
            generation=binding.generation,
            identity_sha256=binding.identity_sha256,
            decision_id=binding.decision_id,
            decision_at=binding.decision_at,
            boundary_attested_available_at=(
                binding.boundary_attested_available_at
            ),
            boundary_expires_at=binding.boundary_expires_at,
            input_prefix_sequence=binding.input_prefix_sequence,
            input_prefix_root_sha256=binding.input_prefix_root_sha256,
            admission_handoff_sha256=binding.admission_handoff_sha256,
            adaptive_request_sha256=binding.adaptive_request_sha256,
            opportunity_key_sha256=binding.opportunity_key_sha256,
            decision_checkpoint_sha256=binding.decision_checkpoint_sha256,
            final_capture_seal_sha256=binding.final_capture_seal_sha256,
            coverage_manifest_sha256=binding.coverage_manifest_sha256,
            coverage_grade_sha256=binding.coverage_grade_sha256,
            stream_coverage_sha256=binding.stream_coverage_sha256,
            active_input_attestation_sha256=(
                binding.active_input_attestation_sha256
            ),
            active_continuity_inventory_sha256=(
                binding.active_continuity_inventory_sha256
            ),
            active_producer_generations_sha256=(
                binding.active_producer_generations_sha256
            ),
            active_resource_binding_sha256=(
                binding.active_resource_binding_sha256
            ),
            read_receipt_sha256=binding.read_receipt_sha256,
            receipt_event_sha256=binding.receipt_event_sha256,
            receipt_event_sequence=binding.receipt_event_sequence,
            receipt_committed_available_at=(
                binding.receipt_committed_available_at
            ),
            source_frontier_sequence=binding.source_frontier_sequence,
            source_event_inventory_sha256=(
                binding.source_event_inventory_sha256
            ),
            watermark_event_at=binding.watermark_event_at,
            watermark_emitted_available_at=(
                binding.watermark_emitted_available_at
            ),
            prior_detector_reference=binding.prior_detector_reference,
            evaluation=canonical,
            _verification_token=_FIRST_DIP_TAPE_DECISION_RECEIPT_TOKEN,
            _lineage=lineage,
        )
        object.__setattr__(
            receipt,
            "_verification_tag",
            _receipt_verification_tag(receipt),
        )
        _verify_first_dip_tape_decision_receipt(receipt)
        return _VerifiedFirstDipTapeDecisionAuthority(
            request=request,
            expected_binding=binding,
            receipt=receipt,
            source=source,
            _on_accept=on_accept,
            _authority_token=_FIRST_DIP_TAPE_DECISION_AUTHORITY_TOKEN,
        )


_FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER = (
    _FirstDipTapeDecisionAuthorityIssuer()
)


class _FirstDipAuthorityScopeLease:
    """Shared revocable one-shot lease across copied ContextVar contexts."""

    __slots__ = ("_lock", "_active", "_question_spent")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = True
        self._question_spent = False

    def claim_question(self) -> bool:
        with self._lock:
            if not self._active or self._question_spent:
                return False
            self._question_spent = True
            return True

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._active)

    @property
    def question_spent(self) -> bool:
        with self._lock:
            return bool(self._question_spent)

    def revoke(self) -> None:
        with self._lock:
            self._active = False


@dataclass(frozen=True)
class _InstalledFirstDipTapeDecisionAuthority:
    authority: _VerifiedFirstDipTapeDecisionAuthority
    lease: _FirstDipAuthorityScopeLease

    @property
    def active(self) -> bool:
        return self.lease.active

    @property
    def question_asked(self) -> bool:
        return self.lease.question_spent

    def spend_question(self) -> bool:
        return self.lease.claim_question()


_FIRST_DIP_TAPE_DECISION_AUTHORITY: ContextVar[
    _InstalledFirstDipTapeDecisionAuthority | None
] = ContextVar("first_dip_tape_decision_authority", default=None)
_CapturedDetectorRetentionProvider = Callable[
    [FirstDipTapeDecisionResolution, Mapping[str, object]], str
]


class _FirstDipAuxiliaryProviderLease:
    """Revocable one-shot claim shared by every copied ContextVar context."""

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


@dataclass(frozen=True)
class _InstalledFirstDipAuxiliaryProvider:
    provider: Callable[..., object]
    lease: _FirstDipAuxiliaryProviderLease


_CAPTURED_FIRST_DIP_DETECTOR_RETENTION_PROVIDER: ContextVar[
    _InstalledFirstDipAuxiliaryProvider | None
] = ContextVar(
    "captured_first_dip_detector_retention_provider",
    default=None,
)
_CapturedFinalAuthorityProvider = Callable[..., object]
_CAPTURED_FIRST_DIP_FINAL_AUTHORITY_PROVIDER: ContextVar[
    _InstalledFirstDipAuxiliaryProvider | None
] = ContextVar(
    "captured_first_dip_final_authority_provider",
    default=None,
)
_SEALED_REPLAY_FIRST_DIP_FINAL_AUTHORITY_PROVIDER: ContextVar[
    _InstalledFirstDipAuxiliaryProvider | None
] = ContextVar(
    "sealed_replay_first_dip_final_authority_provider",
    default=None,
)


_FIRST_DIP_FINAL_AUTHORITY_HANDOFF_TOKEN = object()


@dataclass(frozen=True)
class _FirstDipFinalAuthorityHandoff:
    """Private authority plus the exact post-capture decision clock.

    The caller's clock is sampled before a final-read provider runs.  It cannot
    prove when the provider finished durably committing its read/continuity
    evidence.  Captured paper therefore returns this post-attestation clock,
    while sealed replay returns the same recorded value from the verified
    frontier.  The wrapper is process-local and grants no reservation/order
    authority by itself.
    """

    authority: _VerifiedFirstDipTapeDecisionAuthority
    final_boundary_available_at: datetime
    source: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _FIRST_DIP_FINAL_AUTHORITY_HANDOFF_TOKEN:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final authority handoff origin is invalid"
            )
        if (
            type(self.authority) is not _VerifiedFirstDipTapeDecisionAuthority
            or self.authority._authority_token
            is not _FIRST_DIP_TAPE_DECISION_AUTHORITY_TOKEN
            or self.source not in _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES
            or self.authority.source != self.source
            or self.authority.request.purpose
            != FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final authority handoff is untyped"
            )
        boundary = _utc(
            self.final_boundary_available_at,
            "first_dip_final_authority_handoff.final_boundary_available_at",
        )
        if not (
            self.authority.expected_binding.boundary_attested_available_at
            <= boundary
            <= self.authority.expected_binding.boundary_expires_at
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final authority handoff clock escaped its binding"
            )
        object.__setattr__(self, "final_boundary_available_at", boundary)

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip final authority handoff cannot be pickled")


def _issue_first_dip_final_authority_handoff(
    *,
    authority: _VerifiedFirstDipTapeDecisionAuthority,
    final_boundary_available_at: datetime,
    source: str,
) -> _FirstDipFinalAuthorityHandoff:
    """Issue the one private wrapper accepted by the final resolver."""

    return _FirstDipFinalAuthorityHandoff(
        authority=authority,
        final_boundary_available_at=final_boundary_available_at,
        source=source,
        _token=_FIRST_DIP_FINAL_AUTHORITY_HANDOFF_TOKEN,
    )


@contextmanager
def _installed_captured_first_dip_detector_retention_provider(
    provider: _CapturedDetectorRetentionProvider | None,
) -> Iterator[None]:
    """Install the exact active-runtime sink for an accepted detector receipt."""

    if provider is not None and not callable(provider):
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector retention provider is invalid"
        )
    installed = None
    if provider is not None:
        installed = _InstalledFirstDipAuxiliaryProvider(
            provider=provider,
            lease=_FirstDipAuxiliaryProviderLease(),
        )
    token = _CAPTURED_FIRST_DIP_DETECTOR_RETENTION_PROVIDER.set(installed)
    try:
        yield
    finally:
        if installed is not None:
            installed.lease.revoke()
        _CAPTURED_FIRST_DIP_DETECTOR_RETENTION_PROVIDER.reset(token)


@contextmanager
def _installed_captured_first_dip_final_authority_provider(
    provider: _CapturedFinalAuthorityProvider | None,
) -> Iterator[None]:
    """Install a local no-fallback producer for the fresh final checkpoint."""

    if provider is not None and not callable(provider):
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip final authority provider is invalid"
        )
    installed = None
    if provider is not None:
        installed = _InstalledFirstDipAuxiliaryProvider(
            provider=provider,
            lease=_FirstDipAuxiliaryProviderLease(),
        )
    token = _CAPTURED_FIRST_DIP_FINAL_AUTHORITY_PROVIDER.set(installed)
    try:
        yield
    finally:
        if installed is not None:
            installed.lease.revoke()
        _CAPTURED_FIRST_DIP_FINAL_AUTHORITY_PROVIDER.reset(token)


@contextmanager
def _installed_sealed_replay_first_dip_final_authority_provider(
    provider: _CapturedFinalAuthorityProvider | None,
) -> Iterator[None]:
    """Install one no-fallback provider backed only by a sealed frontier."""

    if provider is not None and not callable(provider):
        raise FirstDipTapeDecisionProviderError(
            "sealed replay first-dip final authority provider is invalid"
        )
    installed = None
    if provider is not None:
        installed = _InstalledFirstDipAuxiliaryProvider(
            provider=provider,
            lease=_FirstDipAuxiliaryProviderLease(),
        )
    token = _SEALED_REPLAY_FIRST_DIP_FINAL_AUTHORITY_PROVIDER.set(installed)
    try:
        yield
    finally:
        if installed is not None:
            installed.lease.revoke()
        _SEALED_REPLAY_FIRST_DIP_FINAL_AUTHORITY_PROVIDER.reset(token)


def _retain_captured_first_dip_detector_for_opportunity(
    resolution: FirstDipTapeDecisionResolution,
    *,
    opportunity_key: Mapping[str, object],
) -> str:
    """Retain the exact accepted object before a detector may report success.

    Serialized debug cannot call this path.  The canonical opportunity digest
    is reconstructed from the typed adaptive-risk key, then the active capture
    runtime receives the still-private resolution object.  Missing retention
    capability fails only this detector decision closed.
    """

    if type(resolution) is not FirstDipTapeDecisionResolution:
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector resolution is not typed"
        )
    receipt = resolution.receipt
    if (
        receipt is None
        or receipt.authority_source != "captured_db_paper"
        or receipt.purpose != FIRST_DIP_TAPE_PURPOSE_DETECTOR
        or resolution.evaluation.status != "valid_positive"
        or resolution.evaluation.confirmed is not True
        or not resolution.run_bound
    ):
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector is not an accepted positive receipt"
        )
    opportunity = dict(opportunity_key) if isinstance(
        opportunity_key, Mapping
    ) else {}
    required = {"symbol", "trading_date", "setup_family"}
    opportunity_fields = set(opportunity)
    if opportunity_fields != required and opportunity_fields != (
        required | {"account_scope"}
    ):
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector opportunity is invalid"
        )
    symbol = str(opportunity.get("symbol") or "").strip().upper()
    setup = str(opportunity.get("setup_family") or "").strip().lower()
    trading_date = str(opportunity.get("trading_date") or "").strip()
    try:
        datetime.fromisoformat(trading_date)
    except ValueError as exc:
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector opportunity date is invalid"
        ) from exc
    if (
        symbol != resolution.evaluation.symbol
        or setup != "first_dip_reclaim"
        or len(trading_date) != 10
    ):
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector opportunity identity mismatch"
        )
    installed = _CAPTURED_FIRST_DIP_DETECTOR_RETENTION_PROVIDER.get()
    if installed is None:
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector retention provider is missing"
        )
    lease_error = installed.lease.claim()
    if lease_error is not None:
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector retention provider " + lease_error
        )
    try:
        retained = installed.provider(resolution, opportunity)
        return _sha256(retained, "prior_detector_reference_sha256")
    except FirstDipTapeDecisionProviderError:
        raise
    except Exception as exc:
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip detector retention failed"
        ) from exc


def _unavailable_evaluation(
    request: FirstDipTapeDecisionRequest,
    *,
    status: str,
    reason: str,
) -> FirstDipTapeEvaluation:
    """Build a deterministic non-authorizing result without reading any source."""

    result_sha256 = sha256_json(
        {
            "schema_version": "chili.first-dip-tape-decision-unavailable.v1",
            "symbol": request.symbol,
            "decision_at": request.decision_at.isoformat().replace("+00:00", "Z"),
            "policy_sha256": request.policy.policy_sha256,
            "purpose": request.purpose,
            "status": status,
            "reason": reason,
            "authority_scope": request.authority_scope,
            "reservation_authority": False,
            "order_authority": False,
        }
    )
    return FirstDipTapeEvaluation(
        symbol=request.symbol,
        decision_at=request.decision_at,
        read_id=f"first-dip-tape-decision:{reason}",
        result_sha256=result_sha256,
        source_event_sha256s=(),
        policy_sha256=request.policy.policy_sha256,
        status=status,
        reason=reason,
        confirmed=False,
        features=None,
        newest_source_age_seconds=None,
    )


def _canonical_result_or_reason(
    evaluation: object,
    *,
    request: FirstDipTapeDecisionRequest,
) -> tuple[FirstDipTapeEvaluation | None, str | None]:
    """Validate exact identity and the canonical evaluation value domain."""

    if type(evaluation) is not FirstDipTapeEvaluation:
        return None, "first_dip_tape_decision_provider_untyped_result"
    try:
        if evaluation.schema_version != FIRST_DIP_TAPE_EVALUATION_SCHEMA_VERSION:
            return None, "first_dip_tape_evaluation_schema_mismatch"
        if evaluation.status not in {
            "valid_positive",
            "valid_negative",
            "coverage_unavailable",
            "invalid",
        } or (
            type(evaluation.confirmed) is not bool
            or evaluation.confirmed != (evaluation.status == "valid_positive")
        ):
            return None, "first_dip_tape_verdict_status_mismatch"
        if evaluation.symbol != request.symbol:
            return None, "first_dip_tape_symbol_mismatch"
        if (
            _utc(evaluation.decision_at, "evaluation.decision_at")
            != request.decision_at
            or evaluation.decision_at.utcoffset() != timezone.utc.utcoffset(None)
        ):
            return None, "first_dip_tape_decision_clock_mismatch"
        if (
            evaluation.policy_sha256 != request.policy.policy_sha256
            or _sha256(evaluation.policy_sha256, "evaluation.policy_sha256")
            != request.policy.policy_sha256
        ):
            return None, "first_dip_tape_policy_mismatch"
        if not str(evaluation.read_id or "").strip() or (
            evaluation.read_id != str(evaluation.read_id).strip()
        ):
            return None, "first_dip_tape_read_id_missing"
        if _sha256(evaluation.result_sha256, "evaluation.result_sha256") != (
            evaluation.result_sha256
        ):
            return None, "first_dip_tape_result_sha256_noncanonical"
        source_hashes = evaluation.source_event_sha256s
        if (
            type(source_hashes) is not tuple
            or len(source_hashes) != len(set(source_hashes))
        ):
            return None, "first_dip_tape_source_inventory_invalid"
        for source_sha256 in source_hashes:
            if _sha256(source_sha256, "evaluation.source_event_sha256") != source_sha256:
                return None, "first_dip_tape_source_inventory_invalid"
        if not str(evaluation.reason or "").strip() or (
            evaluation.reason != str(evaluation.reason).strip()
        ):
            return None, "first_dip_tape_reason_missing"

        features = evaluation.features
        if features is not None:
            expected_feature_keys = {
                "signed_tape_accel",
                "tick_rate",
                "tick_rate_floor",
                "n_ticks",
            }
            if not isinstance(features, Mapping) or set(features) != expected_feature_keys:
                return None, "first_dip_tape_features_invalid"
            if type(features["n_ticks"]) is not int or features["n_ticks"] < 0:
                return None, "first_dip_tape_features_invalid"
            for name in ("signed_tape_accel", "tick_rate", "tick_rate_floor"):
                value = features[name]
                if type(value) not in {int, float} or not math.isfinite(
                    float(value)
                ):
                    return None, "first_dip_tape_features_invalid"
        newest_age = evaluation.newest_source_age_seconds
        if newest_age is not None:
            if type(newest_age) not in {int, float}:
                return None, "first_dip_tape_source_age_invalid"
            newest_age = float(newest_age)
            if not math.isfinite(newest_age) or newest_age < 0.0:
                return None, "first_dip_tape_source_age_invalid"

        source_count = len(source_hashes)
        minimum_prints = request.policy.minimum_prints
        max_age = request.policy.max_source_age_seconds
        if evaluation.status == "valid_positive":
            if (
                features is None
                or evaluation.reason != "first_dip_tape_confirmed"
                or features["signed_tape_accel"] <= 0.0
                or features["tick_rate"] < features["tick_rate_floor"]
                or features["n_ticks"] != source_count
                or source_count < minimum_prints
                or newest_age is None
                or newest_age > max_age
            ):
                return None, "first_dip_tape_positive_verdict_invalid"
        elif evaluation.status == "valid_negative":
            if source_count == 0:
                if not (
                    evaluation.reason == "first_dip_tape_no_prints"
                    and features is None
                    and newest_age is None
                ):
                    return None, "first_dip_tape_empty_negative_invalid"
            elif source_count < minimum_prints:
                if not (
                    evaluation.reason == "first_dip_tape_insufficient_prints"
                    and features is None
                    and newest_age is not None
                    and newest_age <= max_age
                ):
                    return None, "first_dip_tape_thin_negative_invalid"
            elif features is None:
                if not (
                    evaluation.reason == "first_dip_tape_features_unavailable"
                    and newest_age is not None
                    and newest_age <= max_age
                ):
                    return None, "first_dip_tape_full_negative_invalid"
            elif (
                evaluation.reason != "first_dip_tape_not_confirmed"
                or features["n_ticks"] != source_count
                or newest_age is None
                or newest_age > max_age
                or (
                    features["signed_tape_accel"] > 0.0
                    and features["tick_rate"] >= features["tick_rate_floor"]
                )
            ):
                return None, "first_dip_tape_negative_verdict_invalid"
        elif evaluation.status == "coverage_unavailable":
            if not (
                source_count > 0
                and features is None
                and evaluation.reason == "first_dip_tape_source_stale"
                and newest_age is not None
                and newest_age > max_age
            ):
                return None, "first_dip_tape_coverage_unavailable_invalid"
        elif evaluation.status == "invalid":
            if (
                features is not None
                or newest_age is not None
                or evaluation.reason
                not in {
                    "first_dip_tape_symbol_mismatch",
                    "first_dip_tape_receipt_from_future",
                    "first_dip_tape_source_from_future",
                    "first_dip_tape_source_outside_window",
                }
            ):
                return None, "first_dip_tape_invalid_verdict_invalid"

        # Force serialization/digest computation here.  This rejects mutable or
        # otherwise non-canonical feature values before they enter detector debug.
        original_sha256 = evaluation.evaluation_sha256
        canonical_features = (
            None
            if features is None
            else {
                "signed_tape_accel": float(features["signed_tape_accel"]),
                "tick_rate": float(features["tick_rate"]),
                "tick_rate_floor": float(features["tick_rate_floor"]),
                "n_ticks": int(features["n_ticks"]),
            }
        )
        canonical = FirstDipTapeEvaluation(
            schema_version=evaluation.schema_version,
            symbol=request.symbol,
            decision_at=request.decision_at,
            read_id=evaluation.read_id,
            result_sha256=evaluation.result_sha256,
            source_event_sha256s=tuple(source_hashes),
            policy_sha256=request.policy.policy_sha256,
            status=evaluation.status,
            reason=evaluation.reason,
            confirmed=evaluation.confirmed,
            features=canonical_features,
            newest_source_age_seconds=(
                None if newest_age is None else float(newest_age)
            ),
        )
        if (
            canonical.to_dict() != evaluation.to_dict()
            or canonical.evaluation_sha256 != original_sha256
        ):
            return None, "first_dip_tape_evaluation_noncanonical"
    except (TypeError, ValueError, OverflowError):
        return None, "first_dip_tape_evaluation_malformed"
    return canonical, None


@contextmanager
def _installed_first_dip_tape_decision_authority(
    authority: _VerifiedFirstDipTapeDecisionAuthority,
    *,
    expected_source: str,
) -> Iterator[_VerifiedFirstDipTapeDecisionAuthority]:
    if type(authority) is not _VerifiedFirstDipTapeDecisionAuthority or (
        authority._authority_token is not _FIRST_DIP_TAPE_DECISION_AUTHORITY_TOKEN
        or authority.source != expected_source
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape decision authority is not an exact trusted source"
        )
    lease = _FirstDipAuthorityScopeLease()
    installed = _InstalledFirstDipTapeDecisionAuthority(
        authority=authority,
        lease=lease,
    )
    token = _FIRST_DIP_TAPE_DECISION_AUTHORITY.set(installed)
    try:
        yield authority
    finally:
        lease.revoke()
        _FIRST_DIP_TAPE_DECISION_AUTHORITY.reset(token)


@contextmanager
def _installed_sealed_replay_first_dip_tape_decision_authority(
    authority: _VerifiedFirstDipTapeDecisionAuthority,
) -> Iterator[_VerifiedFirstDipTapeDecisionAuthority]:
    """Scope one adapter-verified sealed receipt around one real FSM call."""

    with _installed_first_dip_tape_decision_authority(
        authority,
        expected_source="sealed_replay",
    ) as installed:
        yield installed


@contextmanager
def _installed_captured_db_paper_first_dip_tape_decision_authority(
    authority: _VerifiedFirstDipTapeDecisionAuthority,
) -> Iterator[_VerifiedFirstDipTapeDecisionAuthority]:
    """Scope one capture-verified receipt around one DB-paper FSM call."""

    with _installed_first_dip_tape_decision_authority(
        authority,
        expected_source="captured_db_paper",
    ) as installed:
        yield installed


@contextmanager
def _installed_exact_bound_test_first_dip_tape_decision_authority(
    authority: _VerifiedFirstDipTapeDecisionAuthority,
) -> Iterator[_VerifiedFirstDipTapeDecisionAuthority]:
    """Focused-test seam whose issuer derives every provenance field itself."""

    with _installed_first_dip_tape_decision_authority(
        authority,
        expected_source="exact_bound_test",
    ) as installed:
        yield installed


def _make_exact_bound_test_first_dip_tape_decision_authority(
    *,
    request: FirstDipTapeDecisionRequest,
    evaluation: FirstDipTapeEvaluation,
) -> _VerifiedFirstDipTapeDecisionAuthority:
    """Create a narrow mechanics-test authority without caller-set provenance."""

    return _FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER.issue_exact_bound_test(
        request,
        evaluation,
    )


def resolve_first_dip_tape_decision(
    *,
    symbol: str,
    decision_at: datetime,
    policy: FirstDipTapePolicy,
    purpose: str = FIRST_DIP_TAPE_PURPOSE_DETECTOR,
) -> FirstDipTapeDecisionResolution:
    """Resolve typed detector evidence or fail this decision closed.

    No fallback exists.  In particular, this function cannot read a DB, call a
    provider/network client, reserve risk, or authorize an order.
    """

    request = FirstDipTapeDecisionRequest(
        symbol=symbol,
        decision_at=decision_at,
        policy=policy,
        purpose=purpose,
    )
    installed = _FIRST_DIP_TAPE_DECISION_AUTHORITY.get()
    if installed is None or not installed.active:
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="coverage_unavailable",
                reason=(
                    "first_dip_tape_decision_provider_missing"
                    if installed is None
                    else "first_dip_tape_decision_provider_scope_revoked"
                ),
            )
        )
    if not installed.spend_question():
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="coverage_unavailable",
                reason="first_dip_tape_decision_provider_already_consumed",
            )
        )

    # The shared lease is one-shot across copied contexts and is revoked on
    # scope exit.  Receipt lineage remains unconsumed until all checks pass.
    try:
        candidate = installed.authority.receipt_for(request)
    except Exception:
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="coverage_unavailable",
                reason="first_dip_tape_decision_provider_error",
            )
        )

    try:
        receipt = _verify_first_dip_tape_decision_receipt(candidate)
    except FirstDipTapeDecisionProviderError:
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="invalid",
                reason="first_dip_tape_decision_provider_unbound_result",
            )
        )
    mismatch = _first_dip_tape_receipt_binding_mismatch(
        receipt,
        installed.authority.expected_binding,
    )
    if mismatch is not None:
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="invalid",
                reason=(
                    "first_dip_tape_decision_receipt_binding_mismatch:"
                    + mismatch
                ),
            )
        )
    evaluation, invalid_reason = _canonical_result_or_reason(
        receipt.evaluation,
        request=request,
    )
    if evaluation is None:
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="invalid",
                reason=str(invalid_reason or "first_dip_tape_evaluation_invalid"),
            )
        )
    if not receipt._lineage.consume_once():
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="invalid",
                reason="first_dip_tape_decision_receipt_already_consumed",
            )
        )
    try:
        installed.authority.notify_accepted()
    except Exception:
        return FirstDipTapeDecisionResolution(
            evaluation=_unavailable_evaluation(
                request,
                status="coverage_unavailable",
                reason="first_dip_tape_decision_authority_accept_error",
            )
        )
    return FirstDipTapeDecisionResolution(
        evaluation=evaluation,
        receipt=receipt,
    )


def first_dip_tape_decision_debug(
    resolution: FirstDipTapeDecisionResolution,
) -> dict[str, object]:
    """Canonical audit payload with explicit non-execution authority."""

    if type(resolution) is not FirstDipTapeDecisionResolution:
        raise FirstDipTapeDecisionProviderError(
            "first-dip tape debug requires a typed decision resolution"
        )
    evaluation = resolution.evaluation
    payload: dict[str, object] = dict(evaluation.to_dict())
    payload.update(
        {
            "evaluation_sha256": evaluation.evaluation_sha256,
            "authority_scope": FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE,
            "reservation_authority": False,
            "order_authority": False,
            "run_bound": resolution.run_bound,
        }
    )
    if resolution.receipt is not None:
        payload["decision_receipt"] = resolution.receipt.to_audit_dict()
        payload["decision_receipt_binding_sha256"] = (
            resolution.receipt.binding_sha256
        )
    return payload


# The detector debug payload above is intentionally serialization-safe and is
# therefore never an admission credential.  The following private handoff is a
# separate, process-local, one-shot capability.  It lets the final reservation
# boundary re-verify the exact runtime receipt without trusting a copied
# ``first_dip_tape_confirmed`` boolean or reconstructing authority from JSON.
_FIRST_DIP_FINAL_ADMISSION_ENVELOPE_SCHEMA_VERSION = (
    "chili.first-dip-final-admission-envelope.evidence-only.v1"
)
_FIRST_DIP_FINAL_ADMISSION_CAPABILITY = (
    "first_dip_positive_tape_evidence_only"
)
_FIRST_DIP_FINAL_ADMISSION_LINEAGE_SCHEMA_VERSION = (
    "chili.first-dip-final-admission-lineage.v1"
)
_FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES = frozenset(
    {"sealed_replay", "captured_db_paper"}
)
_FIRST_DIP_FINAL_ADMISSION_TOKEN = object()
_FIRST_DIP_FINAL_ADMISSION_KEY = secrets.token_bytes(32)
_FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN = object()
_FIRST_DIP_FINAL_ADMISSION_RESOLUTION_TOKEN = object()
_FIRST_DIP_FINAL_ADMISSION_RESOLUTION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class _FirstDipFinalAdmissionExpectation:
    """Independently supplied active execution context for final verification.

    ``binding`` must come from the active sealed replay or captured DB-paper
    run, not from detector debug.  It contains the complete capture prefix,
    receipt commit, continuity grades, and watermark frontier retained by the
    execution surface.  Keeping it typed makes every causal field an exact
    comparison rather than a caller-authored truthy flag.
    """

    execution_surface: str
    symbol: str
    decision_at: datetime
    policy_sha256: str
    evaluation_sha256: str
    binding: _FirstDipTapeDecisionBinding

    def __post_init__(self) -> None:
        surface = str(self.execution_surface or "").strip()
        symbol = str(self.symbol or "").strip().upper()
        if surface not in _FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission execution surface is invalid"
            )
        if not symbol:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission symbol is missing"
            )
        if type(self.binding) is not _FirstDipTapeDecisionBinding:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission active binding is not typed"
            )
        decision_at = _utc(
            self.decision_at,
            "final_admission.decision_at",
        )
        policy_sha256 = _sha256(self.policy_sha256, "policy_sha256")
        evaluation_sha256 = _sha256(
            self.evaluation_sha256,
            "evaluation_sha256",
        )
        if self.binding.authority_source != surface:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission surface/authority mismatch"
            )
        if self.binding.purpose != FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission binding purpose mismatch"
            )
        if self.binding.decision_at != decision_at:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission decision clock/binding mismatch"
            )
        if self.binding.evaluation_sha256 != evaluation_sha256:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission evaluation/binding mismatch"
            )
        object.__setattr__(self, "execution_surface", surface)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "policy_sha256", policy_sha256)
        object.__setattr__(self, "evaluation_sha256", evaluation_sha256)

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_surface": self.execution_surface,
            "symbol": self.symbol,
            "decision_at": self.decision_at.isoformat().replace("+00:00", "Z"),
            "policy_sha256": self.policy_sha256,
            "evaluation_sha256": self.evaluation_sha256,
            "capture_binding": self.binding.to_dict(),
        }


class _FirstDipFinalAdmissionLineage:
    """Process-local one-shot state shared by every envelope copy."""

    __slots__ = ("lineage_sha256", "_origin", "_lock", "_consumed")

    def __init__(self, lineage_sha256: str, *, _origin: object) -> None:
        if _origin is not _FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission lineage origin is invalid"
            )
        self.lineage_sha256 = _sha256(lineage_sha256, "lineage_sha256")
        self._origin = _origin
        self._lock = threading.Lock()
        self._consumed = False

    def consume_once(self) -> bool:
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True

    @property
    def consumed(self) -> bool:
        with self._lock:
            return bool(self._consumed)

    def __copy__(self) -> "_FirstDipFinalAdmissionLineage":
        return self

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> "_FirstDipFinalAdmissionLineage":
        memo[id(self)] = self
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip final admission lineage cannot be pickled")


@dataclass(frozen=True)
class _FirstDipFinalAdmissionEnvelope:
    """Private positive-evidence handoff; never reservation/order authority."""

    expectation: _FirstDipFinalAdmissionExpectation
    decision_receipt: FirstDipTapeDecisionReceipt
    capability: str = _FIRST_DIP_FINAL_ADMISSION_CAPABILITY
    reservation_authority: bool = False
    order_authority: bool = False
    schema_version: str = _FIRST_DIP_FINAL_ADMISSION_ENVELOPE_SCHEMA_VERSION
    _verification_token: object = field(default=None, repr=False, compare=False)
    _verification_tag: str = field(default="", repr=False, compare=False)
    _lineage: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _FIRST_DIP_FINAL_ADMISSION_TOKEN:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission envelope was not issued by runtime"
            )
        if (
            self.schema_version
            != _FIRST_DIP_FINAL_ADMISSION_ENVELOPE_SCHEMA_VERSION
            or self.capability != _FIRST_DIP_FINAL_ADMISSION_CAPABILITY
            or self.reservation_authority is not False
            or self.order_authority is not False
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission envelope contract is invalid"
            )
        if type(self.expectation) is not _FirstDipFinalAdmissionExpectation:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission envelope expectation is not typed"
            )
        receipt = _verify_first_dip_tape_decision_receipt(
            self.decision_receipt
        )
        if receipt.authority_source not in (
            _FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission test authority is not runtime evidence"
            )
        receipt_binding = _FirstDipTapeDecisionBinding.from_receipt(receipt)
        if (
            receipt_binding.to_dict()
            != self.expectation.binding.to_dict()
            or receipt.authority_source
            != self.expectation.execution_surface
            or receipt.evaluation.symbol != self.expectation.symbol
            or receipt.evaluation.decision_at != self.expectation.decision_at
            or receipt.evaluation.policy_sha256
            != self.expectation.policy_sha256
            or receipt.purpose != FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
            or receipt.evaluation.evaluation_sha256
            != self.expectation.evaluation_sha256
            or receipt.evaluation.status != "valid_positive"
            or receipt.evaluation.confirmed is not True
            or receipt.evaluation.reason != "first_dip_tape_confirmed"
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission envelope receipt binding mismatch"
            )
        if not receipt._lineage.consumed:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission receipt was not detector-accepted"
            )
        if (
            type(self._lineage) is not _FirstDipFinalAdmissionLineage
            or self._lineage._origin
            is not _FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN
            or self._lineage.lineage_sha256 != self.lineage_sha256
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission envelope lineage is invalid"
            )
        if self._verification_tag:
            expected = _first_dip_final_admission_verification_tag(self)
            if not hmac.compare_digest(self._verification_tag, expected):
                raise FirstDipTapeDecisionProviderError(
                    "first-dip final admission envelope verification failed"
                )

    def body_without_private_tag(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "capability": self.capability,
            "expectation": self.expectation.to_dict(),
            "decision_receipt_binding_sha256": (
                self.decision_receipt.binding_sha256
            ),
            "decision_receipt_lineage_sha256": (
                self.decision_receipt._lineage.lineage_sha256
            ),
            "reservation_authority": False,
            "order_authority": False,
        }

    @property
    def binding_sha256(self) -> str:
        return sha256_json(self.body_without_private_tag())

    @property
    def lineage_sha256(self) -> str:
        return sha256_json(
            {
                "schema_version": (
                    _FIRST_DIP_FINAL_ADMISSION_LINEAGE_SCHEMA_VERSION
                ),
                "body": self.body_without_private_tag(),
            }
        )

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "capability": self.capability,
            "execution_surface": self.expectation.execution_surface,
            "run_id": self.expectation.binding.run_id,
            "generation": self.expectation.binding.generation,
            "symbol": self.expectation.symbol,
            "decision_at": self.expectation.decision_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "policy_sha256": self.expectation.policy_sha256,
            "evaluation_sha256": self.expectation.evaluation_sha256,
            "input_prefix_sequence": (
                self.expectation.binding.input_prefix_sequence
            ),
            "input_prefix_root_sha256": (
                self.expectation.binding.input_prefix_root_sha256
            ),
            "watermark_event_at": (
                self.expectation.binding.watermark_event_at.isoformat().replace(
                    "+00:00", "Z"
                )
            ),
            "watermark_emitted_available_at": (
                self.expectation.binding.watermark_emitted_available_at
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "decision_receipt_binding_sha256": (
                self.decision_receipt.binding_sha256
            ),
            "binding_sha256": self.binding_sha256,
            "reservation_authority": False,
            "order_authority": False,
        }

    def __copy__(self) -> "_FirstDipFinalAdmissionEnvelope":
        return self

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> "_FirstDipFinalAdmissionEnvelope":
        memo[id(self)] = self
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip final admission envelope cannot be pickled")


def _first_dip_final_admission_verification_tag(
    envelope: _FirstDipFinalAdmissionEnvelope,
) -> str:
    return hmac.new(
        _FIRST_DIP_FINAL_ADMISSION_KEY,
        envelope.binding_sha256.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _verify_first_dip_final_admission_envelope(
    value: object,
) -> _FirstDipFinalAdmissionEnvelope:
    if type(value) is not _FirstDipFinalAdmissionEnvelope:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission provider is not a typed runtime envelope"
        )
    if value._verification_token is not _FIRST_DIP_FINAL_ADMISSION_TOKEN:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission envelope token is invalid"
        )
    # Re-run the receipt and typed binding checks.  Frozen dataclasses protect
    # ordinary mutation, while this catches object.__setattr__ and copied tags.
    receipt = _verify_first_dip_tape_decision_receipt(value.decision_receipt)
    if (
        receipt.authority_source
        != value.expectation.execution_surface
        or _FirstDipTapeDecisionBinding.from_receipt(receipt).to_dict()
        != value.expectation.binding.to_dict()
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission receipt escaped its capture binding"
        )
    if (
        type(value._lineage) is not _FirstDipFinalAdmissionLineage
        or value._lineage._origin
        is not _FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN
        or value._lineage.lineage_sha256 != value.lineage_sha256
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission lineage verification failed"
        )
    expected = _first_dip_final_admission_verification_tag(value)
    if not value._verification_tag or not hmac.compare_digest(
        value._verification_tag,
        expected,
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission envelope verification failed"
        )
    return value


def _prepare_first_dip_final_admission_envelope(
    *,
    resolution: FirstDipTapeDecisionResolution,
    execution_surface: str,
) -> _FirstDipFinalAdmissionEnvelope:
    """Mint one runtime-only final handoff from an accepted positive receipt.

    The execution surface is caller-stated only to detect a wiring mistake; the
    receipt authority decides the actual surface.  In particular, a sealed
    replay receipt cannot be relabelled as captured DB-paper evidence, and the
    exact-bound test issuer can never mint a runtime final envelope.
    """

    if type(resolution) is not FirstDipTapeDecisionResolution:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission resolution is not typed"
        )
    receipt = resolution.receipt
    if receipt is None:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission provider receipt is missing"
        )
    receipt = _verify_first_dip_tape_decision_receipt(receipt)
    surface = str(execution_surface or "").strip()
    if (
        surface not in _FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES
        or receipt.authority_source != surface
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission receipt execution surface mismatch"
        )
    evaluation = resolution.evaluation
    if (
        evaluation.status != "valid_positive"
        or evaluation.confirmed is not True
        or evaluation.reason != "first_dip_tape_confirmed"
        or evaluation.to_dict() != receipt.evaluation.to_dict()
        or receipt.purpose != FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
        or not receipt._lineage.consumed
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission requires pre-reservation positive evidence"
        )
    binding = _FirstDipTapeDecisionBinding.from_receipt(receipt)
    expectation = _FirstDipFinalAdmissionExpectation(
        execution_surface=surface,
        symbol=evaluation.symbol,
        decision_at=evaluation.decision_at,
        policy_sha256=evaluation.policy_sha256,
        evaluation_sha256=evaluation.evaluation_sha256,
        binding=binding,
    )
    body = {
        "schema_version": _FIRST_DIP_FINAL_ADMISSION_ENVELOPE_SCHEMA_VERSION,
        "capability": _FIRST_DIP_FINAL_ADMISSION_CAPABILITY,
        "expectation": expectation.to_dict(),
        "decision_receipt_binding_sha256": receipt.binding_sha256,
        "decision_receipt_lineage_sha256": receipt._lineage.lineage_sha256,
        "reservation_authority": False,
        "order_authority": False,
    }
    lineage = _FirstDipFinalAdmissionLineage(
        sha256_json(
            {
                "schema_version": (
                    _FIRST_DIP_FINAL_ADMISSION_LINEAGE_SCHEMA_VERSION
                ),
                "body": body,
            }
        ),
        _origin=_FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN,
    )
    envelope = _FirstDipFinalAdmissionEnvelope(
        expectation=expectation,
        decision_receipt=receipt,
        _verification_token=_FIRST_DIP_FINAL_ADMISSION_TOKEN,
        _lineage=lineage,
    )
    object.__setattr__(
        envelope,
        "_verification_tag",
        _first_dip_final_admission_verification_tag(envelope),
    )
    _verify_first_dip_final_admission_envelope(envelope)
    if not receipt._lineage.claim_final_envelope_once():
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission envelope was already issued"
        )
    return envelope


@dataclass(frozen=True)
class _FirstDipFinalAdmissionResolution:
    """Evidence-only outcome; it never reserves risk or authorizes an order."""

    admitted: bool
    reason: str
    execution_surface: str
    envelope_binding_sha256: str | None = None
    reservation_authority: bool = False
    order_authority: bool = False
    _verification_token: object = field(default=None, repr=False, compare=False)
    _verification_tag: str = field(default="", repr=False, compare=False)
    _lineage: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._verification_token
            is not _FIRST_DIP_FINAL_ADMISSION_RESOLUTION_TOKEN
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission resolution was not issued by runtime"
            )
        if type(self.admitted) is not bool or not str(self.reason or "").strip():
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission result is invalid"
            )
        if (
            self.reservation_authority is not False
            or self.order_authority is not False
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission result cannot carry execution authority"
            )
        surface = str(self.execution_surface or "").strip()
        if surface not in _FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES:
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission result surface is invalid"
            )
        if self.envelope_binding_sha256 is not None:
            object.__setattr__(
                self,
                "envelope_binding_sha256",
                _sha256(
                    self.envelope_binding_sha256,
                    "envelope_binding_sha256",
                ),
            )
        if self.admitted:
            if (
                self.reason
                != "first_dip_final_admission_typed_receipt_verified"
                or self.envelope_binding_sha256 is None
            ):
                raise FirstDipTapeDecisionProviderError(
                    "first-dip admitted resolution is missing verified evidence"
                )
        elif self.envelope_binding_sha256 is not None:
            raise FirstDipTapeDecisionProviderError(
                "first-dip rejected resolution cannot retain evidence authority"
            )
        object.__setattr__(self, "execution_surface", surface)
        if (
            type(self._lineage) is not _FirstDipFinalAdmissionLineage
            or self._lineage._origin
            is not _FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN
            or self._lineage.lineage_sha256 != self.binding_sha256
        ):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission resolution lineage is invalid"
            )
        if self._verification_tag:
            expected = _first_dip_final_admission_resolution_tag(self)
            if not hmac.compare_digest(self._verification_tag, expected):
                raise FirstDipTapeDecisionProviderError(
                    "first-dip final admission resolution verification failed"
                )

    def body_without_private_tag(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "execution_surface": self.execution_surface,
            "envelope_binding_sha256": self.envelope_binding_sha256,
            "reservation_authority": False,
            "order_authority": False,
        }

    @property
    def binding_sha256(self) -> str:
        return sha256_json(
            {
                "schema_version": (
                    "chili.first-dip-final-admission-resolution.v1"
                ),
                "resolution": self.body_without_private_tag(),
            }
        )

    def to_audit_dict(self) -> dict[str, object]:
        payload = self.body_without_private_tag()
        payload["binding_sha256"] = self.binding_sha256
        return payload

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("first-dip final admission resolution cannot be pickled")

    def __copy__(self) -> "_FirstDipFinalAdmissionResolution":
        return self

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> "_FirstDipFinalAdmissionResolution":
        memo[id(self)] = self
        return self


def _first_dip_final_admission_resolution_tag(
    resolution: _FirstDipFinalAdmissionResolution,
) -> str:
    return hmac.new(
        _FIRST_DIP_FINAL_ADMISSION_RESOLUTION_KEY,
        resolution.binding_sha256.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _issue_first_dip_final_admission_resolution(
    *,
    admitted: bool,
    reason: str,
    execution_surface: str,
    envelope_binding_sha256: str | None = None,
) -> _FirstDipFinalAdmissionResolution:
    body = {
        "admitted": admitted,
        "reason": reason,
        "execution_surface": str(execution_surface or "").strip(),
        "envelope_binding_sha256": envelope_binding_sha256,
        "reservation_authority": False,
        "order_authority": False,
    }
    lineage = _FirstDipFinalAdmissionLineage(
        sha256_json(
            {
                "schema_version": (
                    "chili.first-dip-final-admission-resolution.v1"
                ),
                "resolution": body,
            }
        ),
        _origin=_FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN,
    )
    resolution = _FirstDipFinalAdmissionResolution(
        admitted=admitted,
        reason=reason,
        execution_surface=execution_surface,
        envelope_binding_sha256=envelope_binding_sha256,
        _verification_token=_FIRST_DIP_FINAL_ADMISSION_RESOLUTION_TOKEN,
        _lineage=lineage,
    )
    object.__setattr__(
        resolution,
        "_verification_tag",
        _first_dip_final_admission_resolution_tag(resolution),
    )
    return _verify_first_dip_final_admission_resolution(resolution)


def _verify_first_dip_final_admission_resolution(
    value: object,
    *,
    require_admitted: bool = False,
) -> _FirstDipFinalAdmissionResolution:
    if type(value) is not _FirstDipFinalAdmissionResolution or (
        value._verification_token
        is not _FIRST_DIP_FINAL_ADMISSION_RESOLUTION_TOKEN
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission result is not a runtime resolution"
        )
    if (
        type(value._lineage) is not _FirstDipFinalAdmissionLineage
        or value._lineage._origin
        is not _FIRST_DIP_FINAL_ADMISSION_LINEAGE_ORIGIN
        or value._lineage.lineage_sha256 != value.binding_sha256
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission resolution lineage is invalid"
        )
    expected = _first_dip_final_admission_resolution_tag(value)
    if not value._verification_tag or not hmac.compare_digest(
        value._verification_tag,
        expected,
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission resolution verification failed"
        )
    if require_admitted and value.admitted is not True:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission resolution did not admit evidence"
        )
    if require_admitted and not value._lineage.consume_once():
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission resolution was already consumed"
        )
    return value


def _resolve_first_dip_final_admission(
    *,
    execution_surface: str,
    envelope: object | None,
    expected: _FirstDipFinalAdmissionExpectation,
) -> _FirstDipFinalAdmissionResolution:
    """Verify one typed final handoff against an independent active context."""

    surface = str(execution_surface or "").strip()
    if surface not in _FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission active execution surface is invalid"
        )
    if type(expected) is not _FirstDipFinalAdmissionExpectation:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission expected context is not typed"
        )

    def rejected(reason: str) -> _FirstDipFinalAdmissionResolution:
        return _issue_first_dip_final_admission_resolution(
            admitted=False,
            reason=reason,
            execution_surface=surface,
        )

    if expected.execution_surface != surface:
        return rejected("first_dip_final_admission_active_surface_mismatch")
    if envelope is None:
        return rejected("first_dip_final_admission_provider_missing")
    try:
        verified = _verify_first_dip_final_admission_envelope(envelope)
    except (FirstDipTapeDecisionProviderError, TypeError, ValueError):
        return rejected("first_dip_final_admission_provider_unbound")
    if (
        verified.expectation.execution_surface
        != expected.execution_surface
    ):
        return rejected("first_dip_final_admission_execution_surface_mismatch")
    actual = verified.expectation.to_dict()
    wanted = expected.to_dict()
    if set(actual) != set(wanted):
        return rejected("first_dip_final_admission_binding_fields_mismatch")
    for name in wanted:
        if actual[name] != wanted[name]:
            reason = (
                "first_dip_final_admission_capture_binding_mismatch"
                if name == "capture_binding"
                else f"first_dip_final_admission_{name}_mismatch"
            )
            return rejected(reason)
    if not verified._lineage.consume_once():
        return rejected("first_dip_final_admission_envelope_already_consumed")
    return _issue_first_dip_final_admission_resolution(
        admitted=True,
        reason="first_dip_final_admission_typed_receipt_verified",
        execution_surface=surface,
        envelope_binding_sha256=verified.binding_sha256,
    )


def current_first_dip_tape_authority_surface() -> str | None:
    """Return the exact installed runtime evidence surface, never a config guess.

    The value is intentionally tiny: callers may use it only to demand that the
    detector's expected surface matches the already-installed private authority.
    It cannot mint a receipt, reconstruct one from JSON, or authorize execution.
    """

    installed = _FIRST_DIP_TAPE_DECISION_AUTHORITY.get()
    if (
        type(installed) is not _InstalledFirstDipTapeDecisionAuthority
        or not installed.active
    ):
        return None
    authority = installed.authority
    if type(authority) is not _VerifiedFirstDipTapeDecisionAuthority or (
        authority._authority_token is not _FIRST_DIP_TAPE_DECISION_AUTHORITY_TOKEN
        or authority.source not in _FIRST_DIP_TAPE_RUNTIME_AUTHORITY_SOURCES
    ):
        return None
    try:
        receipt = _verify_first_dip_tape_decision_receipt(authority.receipt)
    except FirstDipTapeDecisionProviderError:
        return None
    if (
        receipt.authority_source != authority.source
        or _first_dip_tape_receipt_binding_mismatch(
            receipt,
            authority.expected_binding,
        )
        is not None
    ):
        return None
    return authority.source


def _resolve_installed_first_dip_final_admission(
    *,
    symbol: str,
    adaptive_decision_at: datetime,
    run_id: str,
    generation: int,
    adaptive_decision_id: str,
    adaptive_input_prefix_root_sha256: str,
    adaptive_request_sha256: str,
    opportunity_key_sha256: str,
    final_boundary_available_at: datetime,
    expected_execution_surface: str,
    detector_policy_sha256: str,
    detector_authority_source: str,
    detector_receipt_binding_sha256: str,
    detector_opportunity_key_sha256: str,
) -> _FirstDipFinalAdmissionResolution:
    """Resolve and consume one fresh receipt at the final pending boundary.

    Detector and final placement are different committed FSM invocations.  No
    detector capability crosses that boundary: ReplayV3/captured paper must
    install a new authority for this exact PENDING pre-reservation checkpoint.
    The serialized adaptive request is only an independent consistency veto;
    matching it never creates authority.
    """

    surface = str(expected_execution_surface or "").strip()
    if surface not in _FIRST_DIP_FINAL_ADMISSION_RUNTIME_SURFACES:
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission expected surface is invalid"
        )
    installed = _FIRST_DIP_TAPE_DECISION_AUTHORITY.get()
    if (
        type(installed) is not _InstalledFirstDipTapeDecisionAuthority
        or not installed.active
        or installed.authority.source != surface
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final admission has no installed typed authority"
        )

    def rejected(reason: str) -> _FirstDipFinalAdmissionResolution:
        return _issue_first_dip_final_admission_resolution(
            admitted=False,
            reason=reason,
            execution_surface=surface,
        )

    def spent_rejection(reason: str) -> _FirstDipFinalAdmissionResolution:
        installed.spend_question()
        return rejected(reason)

    if installed.question_asked:
        return rejected("first_dip_final_admission_already_asked")

    authority = installed.authority
    binding = authority.expected_binding
    if (
        authority.request.purpose
        != FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
        or binding.purpose != FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
    ):
        return spent_rejection("first_dip_final_admission_purpose_mismatch")
    try:
        normalized_detector_policy = _sha256(
            detector_policy_sha256,
            "detector_policy_sha256",
        )
        normalized_detector_surface = str(
            detector_authority_source or ""
        ).strip()
        normalized_detector_receipt = _sha256(
            detector_receipt_binding_sha256,
            "detector_receipt_binding_sha256",
        )
        normalized_detector_opportunity = _sha256(
            detector_opportunity_key_sha256,
            "detector_opportunity_key_sha256",
        )
        normalized_opportunity = _sha256(
            opportunity_key_sha256,
            "opportunity_key_sha256",
        )
    except FirstDipTapeDecisionProviderError:
        return spent_rejection(
            "first_dip_final_admission_detector_audit_invalid"
        )
    prior = binding.prior_detector_reference
    if (
        type(prior) is not _FirstDipPriorDetectorReference
        or normalized_detector_policy != authority.request.policy.policy_sha256
        or normalized_detector_surface != surface
        or prior.policy_sha256 != normalized_detector_policy
        or prior.authority_source != normalized_detector_surface
        or prior.receipt_binding_sha256 != normalized_detector_receipt
        or prior.opportunity_key_sha256 != normalized_detector_opportunity
        or normalized_detector_opportunity != normalized_opportunity
        or binding.opportunity_key_sha256 != normalized_opportunity
    ):
        return spent_rejection(
            "first_dip_final_admission_detector_audit_mismatch"
        )
    try:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_adaptive_at = _utc(
            adaptive_decision_at,
            "final_admission.adaptive_decision_at",
        )
        normalized_final_at = _utc(
            final_boundary_available_at,
            "final_admission.final_boundary_available_at",
        )
        normalized_run_id = str(run_id or "").strip()
        normalized_decision_id = str(adaptive_decision_id or "").strip()
        if isinstance(generation, bool):
            raise FirstDipTapeDecisionProviderError(
                "first-dip final admission generation is invalid"
            )
        normalized_generation = int(generation)
        normalized_prefix_root = _sha256(
            adaptive_input_prefix_root_sha256,
            "adaptive_input_prefix_root_sha256",
        )
        normalized_request_sha256 = _sha256(
            adaptive_request_sha256,
            "adaptive_request_sha256",
        )
    except (TypeError, ValueError, FirstDipTapeDecisionProviderError):
        return spent_rejection(
            "first_dip_final_admission_request_context_invalid"
        )
    del normalized_decision_id, normalized_prefix_root
    if normalized_symbol != authority.request.symbol:
        return spent_rejection(
            "first_dip_final_admission_request_context_mismatch:symbol"
        )
    if normalized_run_id != binding.run_id:
        return spent_rejection(
            "first_dip_final_admission_request_context_mismatch:run_id"
        )
    if normalized_generation != binding.generation:
        return spent_rejection(
            "first_dip_final_admission_request_context_mismatch:generation"
        )
    if prior.symbol != normalized_symbol:
        return spent_rejection(
            "first_dip_final_admission_detector_audit_mismatch"
        )
    if (
        normalized_adaptive_at > authority.request.decision_at
        or normalized_request_sha256 != binding.adaptive_request_sha256
    ):
        return spent_rejection(
            "first_dip_final_admission_adaptive_handoff_mismatch"
        )
    evaluation = authority.receipt.evaluation
    elapsed = (normalized_final_at - authority.request.decision_at).total_seconds()
    newest_age = evaluation.newest_source_age_seconds
    if (
        normalized_final_at < binding.boundary_attested_available_at
        or normalized_final_at > binding.boundary_expires_at
        or elapsed < 0.0
        or newest_age is None
        or float(newest_age) + elapsed
        > authority.request.policy.max_source_age_seconds
    ):
        return spent_rejection(
            "first_dip_final_admission_boundary_stale"
        )

    # Ask the freshly installed final-checkpoint authority only after the
    # adaptive request has matched its immutable capture prefix.  The resolver
    # owns one-shot receipt consumption and has no DB/provider fallback.
    resolution = resolve_first_dip_tape_decision(
        symbol=authority.request.symbol,
        decision_at=authority.request.decision_at,
        policy=authority.request.policy,
        purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    )
    if (
        type(resolution) is not FirstDipTapeDecisionResolution
        or resolution.receipt is None
        or resolution.evaluation.status != "valid_positive"
        or resolution.evaluation.confirmed is not True
        or not resolution.run_bound
    ):
        return rejected(
            "first_dip_final_admission_fresh_receipt_rejected:"
            + str(resolution.evaluation.reason or "invalid")
        )

    expected = _FirstDipFinalAdmissionExpectation(
        execution_surface=surface,
        symbol=authority.request.symbol,
        decision_at=authority.request.decision_at,
        policy_sha256=authority.request.policy.policy_sha256,
        evaluation_sha256=resolution.evaluation.evaluation_sha256,
        binding=binding,
    )
    try:
        envelope = _prepare_first_dip_final_admission_envelope(
            resolution=resolution,
            execution_surface=surface,
        )
        final = _resolve_first_dip_final_admission(
            execution_surface=surface,
            envelope=envelope,
            expected=expected,
        )
        if final.admitted:
            return _verify_first_dip_final_admission_resolution(
                final,
                require_admitted=True,
            )
        return _verify_first_dip_final_admission_resolution(final)
    except (FirstDipTapeDecisionProviderError, TypeError, ValueError):
        return rejected("first_dip_final_admission_typed_receipt_invalid")


def _resolve_first_dip_final_admission_with_active_provider(
    *,
    adaptive_request: object,
    detector_policy: FirstDipTapePolicy,
    symbol: str,
    adaptive_decision_at: datetime,
    run_id: str,
    generation: int,
    adaptive_decision_id: str,
    adaptive_input_prefix_root_sha256: str,
    adaptive_request_sha256: str,
    opportunity_key_sha256: str,
    final_boundary_available_at: datetime,
    expected_execution_surface: str,
    detector_policy_sha256: str,
    detector_authority_source: str,
    detector_receipt_binding_sha256: str,
    detector_opportunity_key_sha256: str,
) -> _FirstDipFinalAdmissionResolution:
    """Resolve using an installed authority or mint one from active capture.

    The provider receives the exact typed adaptive request only at the literal
    pre-reservation boundary.  It may return only a private authority minted by
    the capture runtime from a fresh durable read.  No mapping/deserialization,
    current DB, provider client, or network fallback exists in this seam.
    """

    if not isinstance(detector_policy, FirstDipTapePolicy):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final detector policy is not typed"
        )
    if detector_policy.policy_sha256 != _sha256(
        detector_policy_sha256,
        "detector_policy_sha256",
    ):
        raise FirstDipTapeDecisionProviderError(
            "first-dip final detector policy hash mismatch"
        )
    kwargs = {
        "symbol": symbol,
        "adaptive_decision_at": adaptive_decision_at,
        "run_id": run_id,
        "generation": generation,
        "adaptive_decision_id": adaptive_decision_id,
        "adaptive_input_prefix_root_sha256": (
            adaptive_input_prefix_root_sha256
        ),
        "adaptive_request_sha256": adaptive_request_sha256,
        "opportunity_key_sha256": opportunity_key_sha256,
        "final_boundary_available_at": final_boundary_available_at,
        "expected_execution_surface": expected_execution_surface,
        "detector_policy_sha256": detector_policy_sha256,
        "detector_authority_source": detector_authority_source,
        "detector_receipt_binding_sha256": (
            detector_receipt_binding_sha256
        ),
        "detector_opportunity_key_sha256": (
            detector_opportunity_key_sha256
        ),
    }
    installed = _FIRST_DIP_TAPE_DECISION_AUTHORITY.get()
    if (
        type(installed) is _InstalledFirstDipTapeDecisionAuthority
        and installed.active
        and installed.authority.source == expected_execution_surface
        and (
            installed.authority.request.purpose
            == FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
            or not installed.question_asked
        )
    ):
        # An already-installed authority owns this exact boundary even when it
        # carries the wrong, still-unconsumed purpose.  Let the typed resolver
        # return the precise purpose/binding rejection instead of disguising it
        # as generic unavailability.  A detector receipt already consumed by
        # the detector is intentionally skipped so the fresh final provider can
        # install the distinct pre-reservation authority.
        return _resolve_installed_first_dip_final_admission(**kwargs)

    if expected_execution_surface == "captured_db_paper":
        provider_scope = _CAPTURED_FIRST_DIP_FINAL_AUTHORITY_PROVIDER.get()
    elif expected_execution_surface == "sealed_replay":
        provider_scope = _SEALED_REPLAY_FIRST_DIP_FINAL_AUTHORITY_PROVIDER.get()
    else:  # pragma: no cover - guarded by the typed resolver above
        provider_scope = None
    if provider_scope is None:
        raise FirstDipTapeDecisionProviderError(
            f"{expected_execution_surface} first-dip final authority provider is missing"
        )
    lease_error = provider_scope.lease.claim()
    if lease_error is not None:
        raise FirstDipTapeDecisionProviderError(
            "captured first-dip final authority provider " + lease_error
        )
    try:
        handoff = provider_scope.provider(
            adaptive_request=adaptive_request,
            detector_policy=detector_policy,
            final_boundary_available_at=final_boundary_available_at,
        )
    except FirstDipTapeDecisionProviderError:
        raise
    except Exception as exc:
        raise FirstDipTapeDecisionProviderError(
            f"{expected_execution_surface} first-dip final authority provider failed"
        ) from exc
    if (
        type(handoff) is not _FirstDipFinalAuthorityHandoff
        or handoff._token is not _FIRST_DIP_FINAL_AUTHORITY_HANDOFF_TOKEN
        or handoff.source != expected_execution_surface
    ):
        raise FirstDipTapeDecisionProviderError(
            f"{expected_execution_surface} first-dip final authority provider is untyped"
        )
    authority = handoff.authority
    kwargs["final_boundary_available_at"] = handoff.final_boundary_available_at
    installer = (
        _installed_captured_db_paper_first_dip_tape_decision_authority
        if expected_execution_surface == "captured_db_paper"
        else _installed_sealed_replay_first_dip_tape_decision_authority
    )
    with installer(authority):
        return _resolve_installed_first_dip_final_admission(**kwargs)
