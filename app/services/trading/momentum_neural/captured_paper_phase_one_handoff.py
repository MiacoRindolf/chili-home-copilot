"""Durable phase-one evidence for captured Alpaca PAPER decisions.

The live runner commits its ordinary session transaction before adaptive risk,
the action claim, and the transport outbox are created.  The exact admission
material remains process-local across that boundary.  This ledger makes the
otherwise invisible crash window explicit without pretending that an opaque
one-shot capture scope can be reconstructed after process loss.

Recording a row does not consume an opportunity, reserve risk, or authorize a
broker call.  A matching durable outbox can acknowledge it.  On a proven
process restart, a pending row without an outbox becomes
``decision_handoff_unavailable`` and is retained as a replay coverage gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .captured_paper_entry_intent import CapturedPaperPostCommitRequest
from .live_replay_capture import (
    CapturedReadResult,
    ExecutedCaptureReadEvidence,
    ExecutedCaptureReadInventory,
    ExecutedCaptureSourceEventEvidence,
    executed_capture_read_evidence,
)
from .replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureEventRef,
    CaptureReadReceipt,
    canonical_json_bytes,
    captured_read_result_sha256,
    verify_active_capture_input_attestation,
)


UTC = timezone.utc
PHASE_ONE_HANDOFF_SCHEMA_VERSION = "chili.captured-paper-phase-one-handoff.v2"
PHASE_ONE_EVENT_SCHEMA_VERSION = "chili.captured-paper-phase-one-event.v1"
PHASE_ONE_RESTART_SCHEMA_VERSION = (
    "chili.captured-paper-phase-one-restart-reconciliation.v1"
)
EXECUTED_MATERIAL_SCHEMA_VERSION = (
    "chili.captured-paper-executed-material.v1"
)
STATE_PENDING = "pending"
STATE_OUTBOX_COMMITTED = "outbox_committed"
STATE_DECISION_HANDOFF_UNAVAILABLE = "decision_handoff_unavailable"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CapturedPaperPhaseOneHandoffError(RuntimeError):
    """Phase-one evidence could not be durably or exactly reconciled."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_json_invalid"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha(value: Any, field: str, *, optional: bool = False) -> str | None:
    raw = str(value or "").strip().lower()
    if optional and not raw:
        return None
    if _SHA256_RE.fullmatch(raw) is None:
        raise CapturedPaperPhaseOneHandoffError(
            f"captured_paper_phase_one_{field}_invalid"
        )
    return raw


def _uuid(value: Any, field: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperPhaseOneHandoffError(
            f"captured_paper_phase_one_{field}_invalid"
        ) from exc
    if str(parsed) != raw:
        raise CapturedPaperPhaseOneHandoffError(
            f"captured_paper_phase_one_{field}_invalid"
        )
    return raw


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperPhaseOneHandoffError(
            f"captured_paper_phase_one_{field}_invalid"
        )
    return value.astimezone(UTC)


def _db_now(db: Session) -> datetime:
    return _utc(db.execute(text("SELECT clock_timestamp()" )).scalar_one(), "db_clock")


@dataclass(frozen=True, slots=True)
class CapturedPaperExecutedReadBinding:
    """Canonical execution-time read authority bound to one phase-one row."""

    canonical_json: str
    inventory_sha256: str
    executed_material_sha256: str
    run_id: str
    generation: int
    identity_sha256: str
    decision_id: str
    read_ids: tuple[str, ...]


def _verify_executed_source(
    source: ExecutedCaptureSourceEventEvidence,
    *,
    source_index: int,
    reference: Any,
) -> None:
    if (
        type(source) is not ExecutedCaptureSourceEventEvidence
        or source.schema_version
        != "chili.captured-paper-executed-source-event.v1"
        or source.source_index != source_index
        or source.sequence != reference.sequence
        or source.event_sha256 != reference.event_sha256
        or source.payload_sha256 != reference.payload_sha256
        or source.query_sha256 != reference.query_sha256
        or source.stream != reference.stream.value
        or source.provider != reference.provider
        or source.symbol != reference.symbol
        or source.provider_event_at != reference.provider_event_at
        or source.market_reference_at != reference.market_reference_at
        or source.received_at != reference.received_at
        or source.available_at != reference.available_at
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_source_mismatch"
        )


def verify_captured_paper_executed_read_inventory(
    *,
    inventory: ExecutedCaptureReadInventory,
    captured_reads: Sequence[CapturedReadResult],
    active_input_attestation: ActiveCaptureInputPrefixAttestation,
    request: CapturedPaperPostCommitRequest,
    material_sha256: str,
    require_exact_attestation: bool = True,
) -> CapturedPaperExecutedReadBinding:
    """Bind the full durable read objects to the runtime-issued attestation.

    No read identifier is accepted on its own.  Receipt bytes, receipt-event
    identity, result hash, every ordered source-event hash and all four causal
    clocks must equal the process-private attestation evidence.
    """

    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_invalid"
        )
    request.verify()
    material = str(_sha(material_sha256, "material"))
    if type(inventory) is not ExecutedCaptureReadInventory:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_unavailable"
        )
    raw_reads = tuple(captured_reads)
    if not raw_reads or any(type(row) is not CapturedReadResult for row in raw_reads):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_results_unavailable"
        )
    try:
        raw_evidence = tuple(
            sorted(
                (executed_capture_read_evidence(row) for row in raw_reads),
                key=lambda row: (row.receipt_event_sequence, row.read_id),
            )
        )
    except CaptureContractError as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_results_invalid"
        ) from exc
    if tuple(inventory.reads) != raw_evidence:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_results_mismatch"
        )
    try:
        proof = verify_active_capture_input_attestation(
            active_input_attestation
        )
    except CaptureContractError as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_attestation_invalid"
        ) from exc
    if (
        inventory.schema_version
        != "chili.captured-paper-executed-read-inventory.v1"
        or inventory.run_id != proof.run_id
        or inventory.generation != proof.generation
        or inventory.identity_sha256 != proof.identity_sha256
        or inventory.decision_id != proof.decision_id
        or inventory.decision_id != request.intent.decision_id
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_identity_mismatch"
        )
    reads = tuple(inventory.reads)
    if not reads or any(type(row) is not ExecutedCaptureReadEvidence for row in reads):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_unavailable"
        )
    causal_order = tuple(
        sorted(reads, key=lambda row: (row.receipt_event_sequence, row.read_id))
    )
    if reads != causal_order:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_reordered"
        )
    if (
        len({row.read_id for row in reads}) != len(reads)
        or len({row.receipt_event_sequence for row in reads}) != len(reads)
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_duplicated"
        )
    if type(require_exact_attestation) is not bool:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_attestation_mode_invalid"
        )
    proof_by_id = {row.receipt.read_id: row for row in proof.read_evidence}
    inventory_ids = {row.read_id for row in reads}
    if (
        not set(proof_by_id).issubset(inventory_ids)
        or (require_exact_attestation and set(proof_by_id) != inventory_ids)
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_incomplete"
        )
    for row in reads:
        expected = proof_by_id.get(row.read_id)
        if (
            row.schema_version != "chili.captured-paper-executed-read.v1"
            or row.run_id != proof.run_id
            or row.generation != proof.generation
            or row.identity_sha256 != proof.identity_sha256
            or row.decision_id != proof.decision_id
            or row.replay_network_fallback_used is not False
        ):
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_executed_read_receipt_mismatch"
            )
        try:
            raw_receipt = json.loads(row.receipt_canonical_json)
            if not isinstance(raw_receipt, Mapping):
                raise ValueError("receipt is not an object")
            receipt = CaptureReadReceipt.from_dict(raw_receipt)
            canonical_receipt = canonical_json_bytes(
                receipt.to_dict()
            ).decode("utf-8")
        except (CaptureContractError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_executed_read_receipt_invalid"
            ) from exc
        if (
            canonical_receipt != row.receipt_canonical_json
            or hashlib.sha256(canonical_receipt.encode("utf-8")).hexdigest()
            != row.receipt_sha256
            or row.stream != receipt.stream.value
            or row.provider != receipt.provider
            or row.symbol != receipt.symbol
            or row.result_sha256 != receipt.result_sha256
            or receipt.content_verified is not True
            or receipt.replay_network_fallback_used is not False
        ):
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_executed_read_receipt_mismatch"
            )
        sources = tuple(row.source_events)
        if expected is not None:
            if (
                row.receipt_event_sha256 != expected.receipt_event_sha256
                or row.receipt_event_sequence != expected.receipt_event_sequence
                or row.receipt_committed_available_at
                != expected.receipt_committed_available_at
                or row.result_sha256 != expected.receipt.result_sha256
                or row.receipt_sha256 != expected.receipt_sha256
                or receipt != expected.receipt
            ):
                raise CapturedPaperPhaseOneHandoffError(
                    "captured_paper_executed_read_receipt_mismatch"
                )
            references = tuple(expected.source_event_refs)
        else:
            if (
                row.receipt_event_sequence <= proof.input_prefix_sequence
                or row.receipt_committed_available_at < receipt.returned_at
                or receipt.decision_id != proof.decision_id
                or receipt.identity_sha256 != proof.identity_sha256
                or receipt.read_id != row.read_id
            ):
                raise CapturedPaperPhaseOneHandoffError(
                    "captured_paper_executed_read_dynamic_receipt_mismatch"
                )
            try:
                references = tuple(
                    CaptureEventRef(
                        identity_sha256=proof.identity_sha256,
                        event_sha256=source.event_sha256,
                        sequence=source.sequence,
                        stream=source.stream,
                        received_at=source.received_at,
                        available_at=source.available_at,
                        payload_sha256=source.payload_sha256,
                        provider=source.provider,
                        symbol=source.symbol,
                        query_sha256=source.query_sha256,
                        provider_event_at=source.provider_event_at,
                        market_reference_at=source.market_reference_at,
                    )
                    for source in sources
                )
            except CaptureContractError as exc:
                raise CapturedPaperPhaseOneHandoffError(
                    "captured_paper_executed_read_dynamic_source_invalid"
                ) from exc
            if (
                tuple(ref.event_sha256 for ref in references)
                != receipt.source_event_sha256s
                or captured_read_result_sha256(references)
                != receipt.result_sha256
            ):
                raise CapturedPaperPhaseOneHandoffError(
                    "captured_paper_executed_read_dynamic_result_mismatch"
                )
        if len(sources) != len(references):
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_executed_read_source_mismatch"
            )
        for source_index, (source, reference) in enumerate(
            zip(sources, references, strict=True)
        ):
            _verify_executed_source(
                source,
                source_index=source_index,
                reference=reference,
            )
    try:
        canonical = canonical_json_bytes(inventory.to_dict()).decode("utf-8")
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_invalid"
        ) from exc
    inventory_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if inventory_sha != inventory.inventory_sha256:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_hash_mismatch"
        )
    executed_material_sha = _sha256_json(
        {
            "schema_version": EXECUTED_MATERIAL_SCHEMA_VERSION,
            "completion_sha256": request.completion_sha256,
            "request_payload_sha256": hashlib.sha256(
                request.to_canonical_json().encode("utf-8")
            ).hexdigest(),
            "material_sha256": material,
            "executed_read_inventory_sha256": inventory_sha,
        }
    )
    return CapturedPaperExecutedReadBinding(
        canonical_json=canonical,
        inventory_sha256=inventory_sha,
        executed_material_sha256=executed_material_sha,
        run_id=inventory.run_id,
        generation=inventory.generation,
        identity_sha256=inventory.identity_sha256,
        decision_id=inventory.decision_id,
        read_ids=tuple(row.read_id for row in reads),
    )


@dataclass(frozen=True, slots=True)
class CapturedPaperPhaseOneHandoffReceipt:
    completion_sha256: str
    request_payload_sha256: str
    material_sha256: str
    executed_read_inventory_sha256: str
    executed_material_sha256: str
    state: str
    event_sequence: int
    last_event_sha256: str
    recorded_at: datetime
    state_changed_at: datetime

    def to_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": PHASE_ONE_HANDOFF_SCHEMA_VERSION,
                "completion_sha256": self.completion_sha256,
                "request_payload_sha256": self.request_payload_sha256,
                "material_sha256": self.material_sha256,
                "executed_read_inventory_sha256": (
                    self.executed_read_inventory_sha256
                ),
                "executed_material_sha256": self.executed_material_sha256,
                "state": self.state,
                "event_sequence": self.event_sequence,
                "last_event_sha256": self.last_event_sha256,
                "recorded_at": self.recorded_at.isoformat(),
                "state_changed_at": self.state_changed_at.isoformat(),
            }
        )


def _receipt(row: Mapping[str, Any]) -> CapturedPaperPhaseOneHandoffReceipt:
    state = str(row.get("state") or "")
    if state not in {
        STATE_PENDING,
        STATE_OUTBOX_COMMITTED,
        STATE_DECISION_HANDOFF_UNAVAILABLE,
    }:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_state_invalid"
        )
    sequence = row.get("event_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_event_sequence_invalid"
        )
    canonical_inventory = row.get("executed_read_inventory_canonical_json")
    if not isinstance(canonical_inventory, str) or not canonical_inventory:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_unavailable"
        )
    try:
        decoded_inventory = json.loads(canonical_inventory)
    except json.JSONDecodeError as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_invalid"
        ) from exc
    if _canonical_json_bytes(decoded_inventory).decode("utf-8") != canonical_inventory:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_not_canonical"
        )
    inventory_sha = str(
        _sha(row.get("executed_read_inventory_sha256"), "executed_inventory")
    )
    if hashlib.sha256(canonical_inventory.encode("utf-8")).hexdigest() != inventory_sha:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_read_inventory_hash_mismatch"
        )
    completion = str(_sha(row.get("completion_sha256"), "completion"))
    request_payload = str(
        _sha(row.get("request_payload_sha256"), "request_payload")
    )
    material = str(_sha(row.get("material_sha256"), "material"))
    executed_material = str(
        _sha(row.get("executed_material_sha256"), "executed_material")
    )
    if executed_material != _sha256_json(
        {
            "schema_version": EXECUTED_MATERIAL_SCHEMA_VERSION,
            "completion_sha256": completion,
            "request_payload_sha256": request_payload,
            "material_sha256": material,
            "executed_read_inventory_sha256": inventory_sha,
        }
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_executed_material_hash_mismatch"
        )
    return CapturedPaperPhaseOneHandoffReceipt(
        completion_sha256=completion,
        request_payload_sha256=request_payload,
        material_sha256=material,
        executed_read_inventory_sha256=inventory_sha,
        executed_material_sha256=executed_material,
        state=state,
        event_sequence=sequence,
        last_event_sha256=str(_sha(row.get("last_event_sha256"), "last_event")),
        recorded_at=_utc(row.get("recorded_at"), "recorded_at"),
        state_changed_at=_utc(row.get("state_changed_at"), "state_changed_at"),
    )


_ROW_COLUMNS = (
    "completion_sha256, request_payload_sha256, material_sha256, "
    "executed_read_inventory_canonical_json, "
    "executed_read_inventory_sha256, executed_material_sha256, state, "
    "event_sequence, last_event_sha256, recorded_at, state_changed_at"
)


def _locked_row(db: Session, completion_sha256: str) -> Mapping[str, Any] | None:
    return db.execute(
        text(
            "SELECT * FROM captured_paper_phase_one_handoffs "
            "WHERE completion_sha256 = :completion_sha256 FOR UPDATE"
        ),
        {"completion_sha256": completion_sha256},
    ).mappings().one_or_none()


def _verify_locked_request_binding(
    row: Mapping[str, Any],
    *,
    request: CapturedPaperPostCommitRequest,
    material_sha256: str,
    executed_binding: CapturedPaperExecutedReadBinding | None = None,
) -> CapturedPaperPhaseOneHandoffReceipt:
    """Verify the immutable RAM request against its locked phase-one row."""

    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_invalid"
        )
    request.verify()
    material = str(_sha(material_sha256, "material"))
    payload = request.to_canonical_json()
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_json_invalid"
        ) from exc
    if _canonical_json_bytes(decoded).decode("utf-8") != payload:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_not_canonical"
        )
    route = request.route_token
    expected = {
        "completion_sha256": request.completion_sha256,
        "request_payload_sha256": hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest(),
        "request_canonical_json": payload,
        "material_sha256": material,
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": request.intent.intent_sha256,
        "account_scope": route.account_scope,
        "expected_account_id": route.expected_account_id,
        "session_id": route.session_id,
        "symbol": route.symbol,
        "client_order_id": request.intent.client_order_id,
        "binder_id": request.intent.binder_id,
        "runtime_generation": route.runtime_generation,
        "code_build_sha256": route.code_build_sha256,
        "config_sha256": route.config_sha256,
        "capture_receipt_sha256": route.capture_receipt_sha256,
    }
    if executed_binding is not None:
        expected.update(
            {
                "executed_read_inventory_canonical_json": (
                    executed_binding.canonical_json
                ),
                "executed_read_inventory_sha256": (
                    executed_binding.inventory_sha256
                ),
                "executed_material_sha256": (
                    executed_binding.executed_material_sha256
                ),
            }
        )
    if any(str(row.get(name)) != str(value) for name, value in expected.items()):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_locked_binding_mismatch"
        )
    return _receipt(row)


def lock_captured_paper_phase_one_for_admission(
    db: Session,
    *,
    request: CapturedPaperPostCommitRequest,
    material_sha256: str,
    executed_read_inventory: ExecutedCaptureReadInventory,
    captured_reads: Sequence[CapturedReadResult],
    active_input_attestation: ActiveCaptureInputPrefixAttestation,
) -> CapturedPaperPhaseOneHandoffReceipt:
    """Acquire the admission gate before risk, claims, or outbox mutation.

    The lock is caller-transaction owned.  A missing, terminal, mismatched, or
    already-outboxed row is a permanent fail-closed result for this invocation.
    """

    if not isinstance(db, Session):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_session_invalid"
        )
    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_invalid"
        )
    request.verify()
    executed_binding = verify_captured_paper_executed_read_inventory(
        inventory=executed_read_inventory,
        captured_reads=captured_reads,
        active_input_attestation=active_input_attestation,
        request=request,
        material_sha256=material_sha256,
        require_exact_attestation=False,
    )
    row = _locked_row(db, request.completion_sha256)
    if row is None:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_handoff_missing"
        )
    receipt = _verify_locked_request_binding(
        row,
        request=request,
        material_sha256=material_sha256,
        executed_binding=executed_binding,
    )
    if receipt.state == STATE_DECISION_HANDOFF_UNAVAILABLE:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_decision_handoff_unavailable"
        )
    if receipt.state != STATE_PENDING:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_not_pending"
        )
    existing_outbox = db.execute(
        text(
            "SELECT 1 FROM captured_paper_post_commit_outbox "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": request.completion_sha256},
    ).scalar_one_or_none()
    if existing_outbox is not None:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_pending_outbox_contradiction"
        )
    return receipt


def _append_event(
    db: Session,
    *,
    row: Mapping[str, Any],
    event_type: str,
    new_state: str,
    effective_at: datetime,
    restart_generation: str | None = None,
) -> CapturedPaperPhaseOneHandoffReceipt:
    completion = str(_sha(row.get("completion_sha256"), "completion"))
    previous_sequence = int(row.get("event_sequence") or 0)
    previous_hash = _sha(
        row.get("last_event_sha256"), "previous_event", optional=True
    )
    sequence = previous_sequence + 1
    if (previous_sequence == 0) != (previous_hash is None):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_event_head_invalid"
        )
    payload = {
        "schema_version": PHASE_ONE_EVENT_SCHEMA_VERSION,
        "completion_sha256": completion,
        "request_payload_sha256": str(
            _sha(row.get("request_payload_sha256"), "request_payload")
        ),
        "material_sha256": str(_sha(row.get("material_sha256"), "material")),
        "executed_read_inventory_sha256": str(
            _sha(
                row.get("executed_read_inventory_sha256"),
                "executed_inventory",
            )
        ),
        "executed_material_sha256": str(
            _sha(row.get("executed_material_sha256"), "executed_material")
        ),
        "event_type": event_type,
        "prior_state": None if previous_sequence == 0 else str(row.get("state")),
        "new_state": new_state,
        "restart_generation": restart_generation,
        "effective_at": effective_at.isoformat(),
    }
    payload_raw = _canonical_json_bytes(payload).decode("utf-8")
    payload_sha256 = hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()
    event_sha256 = _sha256_json(
        {
            "completion_sha256": completion,
            "sequence": sequence,
            "event_type": event_type,
            "previous_event_sha256": previous_hash,
            "event_payload_sha256": payload_sha256,
            "effective_at": effective_at.isoformat(),
        }
    )
    db.execute(
        text(
            "INSERT INTO captured_paper_phase_one_handoff_events ("
            "completion_sha256, sequence, event_type, previous_event_sha256, "
            "event_sha256, event_payload_sha256, event_payload_canonical_json, "
            "effective_at) VALUES (:completion, :sequence, :event_type, "
            ":previous, :event_sha, :payload_sha, :payload, :effective_at)"
        ),
        {
            "completion": completion,
            "sequence": sequence,
            "event_type": event_type,
            "previous": previous_hash,
            "event_sha": event_sha256,
            "payload_sha": payload_sha256,
            "payload": payload_raw,
            "effective_at": effective_at,
        },
    )
    result = db.execute(
        text(
            "UPDATE captured_paper_phase_one_handoffs SET state = :new_state, "
            "state_changed_at = :effective_at, event_sequence = :sequence, "
            "last_event_sha256 = :event_sha, version = version + 1 "
            "WHERE completion_sha256 = :completion "
            "AND state = :prior_state AND event_sequence = :prior_sequence "
            "AND last_event_sha256 IS NOT DISTINCT FROM :previous "
            f"RETURNING {_ROW_COLUMNS}"
        ),
        {
            "new_state": new_state,
            "effective_at": effective_at,
            "sequence": sequence,
            "event_sha": event_sha256,
            "completion": completion,
            "prior_state": str(row.get("state")),
            "prior_sequence": previous_sequence,
            "previous": previous_hash,
        },
    ).mappings().one_or_none()
    if result is None:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_event_cas_failed"
        )
    return _receipt(result)


def record_captured_paper_phase_one_handoff(
    db: Session,
    *,
    request: CapturedPaperPostCommitRequest,
    material_sha256: str,
    executed_read_inventory: ExecutedCaptureReadInventory,
    captured_reads: Sequence[CapturedReadResult],
    active_input_attestation: ActiveCaptureInputPrefixAttestation,
    candidate_sha256: str | None,
    bound_input_scope_sha256: str | None,
) -> CapturedPaperPhaseOneHandoffReceipt:
    """Record exact phase-one evidence in the caller-owned transaction."""

    if not isinstance(db, Session):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_session_invalid"
        )
    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_invalid"
        )
    request.verify()
    material = str(_sha(material_sha256, "material"))
    executed_binding = verify_captured_paper_executed_read_inventory(
        inventory=executed_read_inventory,
        captured_reads=captured_reads,
        active_input_attestation=active_input_attestation,
        request=request,
        material_sha256=material,
        require_exact_attestation=False,
    )
    candidate = _sha(candidate_sha256, "candidate", optional=True)
    scope = _sha(bound_input_scope_sha256, "bound_scope", optional=True)
    route = request.route_token
    payload = request.to_canonical_json()
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_json_invalid"
        ) from exc
    if _canonical_json_bytes(decoded).decode("utf-8") != payload:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_not_canonical"
        )
    payload_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    now = _db_now(db)
    inserted = db.execute(
        text(
            "INSERT INTO captured_paper_phase_one_handoffs ("
            "completion_sha256, request_payload_sha256, request_canonical_json, "
            "material_sha256, executed_read_inventory_canonical_json, "
            "executed_read_inventory_sha256, executed_material_sha256, "
            "candidate_sha256, bound_input_scope_sha256, "
            "route_token_sha256, intent_sha256, account_scope, expected_account_id, "
            "session_id, symbol, client_order_id, binder_id, runtime_generation, "
            "code_build_sha256, config_sha256, capture_receipt_sha256, "
            "recorded_at, state_changed_at) VALUES ("
            ":completion, :payload_sha, :payload, :material, "
            ":executed_inventory, :executed_inventory_sha, "
            ":executed_material_sha, :candidate, :scope, "
            ":route, :intent, :account_scope, CAST(:account_id AS UUID), "
            ":session_id, :symbol, :client_order_id, CAST(:binder_id AS UUID), "
            "CAST(:runtime_generation AS UUID), :code_build, :config, :capture, "
            ":now, :now) ON CONFLICT (completion_sha256) DO NOTHING "
            "RETURNING completion_sha256"
        ),
        {
            "completion": request.completion_sha256,
            "payload_sha": payload_sha,
            "payload": payload,
            "material": material,
            "executed_inventory": executed_binding.canonical_json,
            "executed_inventory_sha": executed_binding.inventory_sha256,
            "executed_material_sha": (
                executed_binding.executed_material_sha256
            ),
            "candidate": candidate,
            "scope": scope,
            "route": route.route_token_sha256,
            "intent": request.intent.intent_sha256,
            "account_scope": route.account_scope,
            "account_id": route.expected_account_id,
            "session_id": route.session_id,
            "symbol": route.symbol,
            "client_order_id": request.intent.client_order_id,
            "binder_id": request.intent.binder_id,
            "runtime_generation": route.runtime_generation,
            "code_build": route.code_build_sha256,
            "config": route.config_sha256,
            "capture": route.capture_receipt_sha256,
            "now": now,
        },
    ).scalar_one_or_none()
    row = _locked_row(db, request.completion_sha256)
    if row is None:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_insert_unreadable"
        )
    exact = {
        "request_payload_sha256": payload_sha,
        "request_canonical_json": payload,
        "material_sha256": material,
        "executed_read_inventory_canonical_json": (
            executed_binding.canonical_json
        ),
        "executed_read_inventory_sha256": executed_binding.inventory_sha256,
        "executed_material_sha256": (
            executed_binding.executed_material_sha256
        ),
        "candidate_sha256": candidate,
        "bound_input_scope_sha256": scope,
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": request.intent.intent_sha256,
        "account_scope": route.account_scope,
        "expected_account_id": route.expected_account_id,
        "session_id": route.session_id,
        "symbol": route.symbol,
        "client_order_id": request.intent.client_order_id,
        "binder_id": request.intent.binder_id,
        "runtime_generation": route.runtime_generation,
        "code_build_sha256": route.code_build_sha256,
        "config_sha256": route.config_sha256,
        "capture_receipt_sha256": route.capture_receipt_sha256,
    }
    if any(str(row.get(name)) != str(value) for name, value in exact.items()):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_duplicate_mismatch"
        )
    if inserted is not None:
        return _append_event(
            db,
            row=row,
            event_type="phase_one_committed_pending_handoff",
            new_state=STATE_PENDING,
            effective_at=now,
        )
    existing = _receipt(row)
    if existing.state == STATE_DECISION_HANDOFF_UNAVAILABLE:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_decision_handoff_unavailable"
        )
    return existing


def _transition_with_outbox_proof(
    db: Session,
    *,
    row: Mapping[str, Any],
    effective_at: datetime,
) -> CapturedPaperPhaseOneHandoffReceipt:
    outbox = db.execute(
        text(
            "SELECT payload_sha256, payload_canonical_json "
            "FROM captured_paper_post_commit_outbox "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": row["completion_sha256"]},
    ).mappings().one_or_none()
    if outbox is None:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_outbox_proof_unavailable"
        )
    if (
        str(outbox.get("payload_sha256"))
        != str(row.get("request_payload_sha256"))
        or str(outbox.get("payload_canonical_json"))
        != str(row.get("request_canonical_json"))
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_outbox_proof_mismatch"
        )
    state = str(row.get("state"))
    if state == STATE_OUTBOX_COMMITTED:
        return _receipt(row)
    if state != STATE_PENDING:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_late_outbox_contradiction"
        )
    return _append_event(
        db,
        row=row,
        event_type="durable_outbox_committed",
        new_state=STATE_OUTBOX_COMMITTED,
        effective_at=effective_at,
    )


def commit_captured_paper_phase_one_outbox_in_transaction(
    db: Session,
    *,
    request: CapturedPaperPostCommitRequest,
    material_sha256: str,
    locked_receipt: CapturedPaperPhaseOneHandoffReceipt,
) -> CapturedPaperPhaseOneHandoffReceipt:
    """Bind the newly persisted outbox to phase one before transaction commit."""

    if not isinstance(db, Session):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_session_invalid"
        )
    if type(locked_receipt) is not CapturedPaperPhaseOneHandoffReceipt:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_locked_receipt_invalid"
        )
    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_invalid"
        )
    request.verify()
    row = _locked_row(db, request.completion_sha256)
    if row is None:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_handoff_missing"
        )
    current = _verify_locked_request_binding(
        row,
        request=request,
        material_sha256=material_sha256,
    )
    if current.state == STATE_DECISION_HANDOFF_UNAVAILABLE:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_decision_handoff_unavailable"
        )
    if not (
        current.state == STATE_PENDING
        and locked_receipt.state == STATE_PENDING
        and current.completion_sha256 == locked_receipt.completion_sha256
        and current.request_payload_sha256
        == locked_receipt.request_payload_sha256
        and current.material_sha256 == locked_receipt.material_sha256
        and current.executed_read_inventory_sha256
        == locked_receipt.executed_read_inventory_sha256
        and current.executed_material_sha256
        == locked_receipt.executed_material_sha256
        and current.event_sequence == locked_receipt.event_sequence
        and current.last_event_sha256 == locked_receipt.last_event_sha256
    ):
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_locked_receipt_drift"
        )
    return _transition_with_outbox_proof(
        db,
        row=row,
        effective_at=_db_now(db),
    )


def acknowledge_captured_paper_phase_one_handoff(
    bind: Engine,
    *,
    request: CapturedPaperPostCommitRequest,
    material_sha256: str,
) -> CapturedPaperPhaseOneHandoffReceipt:
    """Acknowledge RAM material only after exact outbox evidence exists."""

    if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_postgresql_engine_required"
        )
    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_request_invalid"
        )
    request.verify()
    material = str(_sha(material_sha256, "material"))
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    with factory() as db, db.begin():
        row = _locked_row(db, request.completion_sha256)
        if row is None:
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_phase_one_handoff_missing"
            )
        if (
            str(row.get("request_canonical_json")) != request.to_canonical_json()
            or str(row.get("material_sha256")) != material
        ):
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_phase_one_ack_binding_mismatch"
            )
        return _transition_with_outbox_proof(
            db,
            row=row,
            effective_at=_db_now(db),
        )


def reconcile_captured_paper_phase_one_after_restart(
    bind: Engine,
    *,
    activation_generation: str,
    limit: int,
) -> Mapping[str, Any]:
    """Classify pre-outbox decisions after a proven singleton restart."""

    if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_postgresql_engine_required"
        )
    generation = _uuid(activation_generation, "restart_generation")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise CapturedPaperPhaseOneHandoffError(
            "captured_paper_phase_one_reconcile_limit_invalid"
        )
    committed: list[str] = []
    unavailable: list[str] = []
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    with factory() as db, db.begin():
        initial_pending_count = int(
            db.execute(
                text(
                    "SELECT count(*) FROM captured_paper_phase_one_handoffs "
                    "WHERE state = 'pending'"
                )
            ).scalar_one()
        )
        if initial_pending_count > limit:
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_phase_one_reconcile_limit_insufficient"
            )
        rows = db.execute(
            text(
                "SELECT * FROM captured_paper_phase_one_handoffs "
                "WHERE state = 'pending' ORDER BY recorded_at, completion_sha256 "
                "FOR UPDATE NOWAIT"
            )
        ).mappings().all()
        for row in rows:
            now = _db_now(db)
            outbox = db.execute(
                text(
                    "SELECT payload_sha256, payload_canonical_json "
                    "FROM captured_paper_post_commit_outbox "
                    "WHERE completion_sha256 = :completion_sha256"
                ),
                {"completion_sha256": row["completion_sha256"]},
            ).mappings().one_or_none()
            if outbox is not None:
                _transition_with_outbox_proof(db, row=row, effective_at=now)
                committed.append(str(row["completion_sha256"]))
                continue
            _append_event(
                db,
                row=row,
                event_type="process_restart_before_durable_outbox",
                new_state=STATE_DECISION_HANDOFF_UNAVAILABLE,
                effective_at=now,
                restart_generation=generation,
            )
            unavailable.append(str(row["completion_sha256"]))
        remaining_pending_count = int(
            db.execute(
                text(
                    "SELECT count(*) FROM captured_paper_phase_one_handoffs "
                    "WHERE state = 'pending'"
                )
            ).scalar_one()
        )
        if remaining_pending_count != 0:
            raise CapturedPaperPhaseOneHandoffError(
                "captured_paper_phase_one_reconcile_incomplete"
            )
    body = {
        "schema_version": PHASE_ONE_RESTART_SCHEMA_VERSION,
        "activation_generation": generation,
        "initial_pending_count": initial_pending_count,
        "remaining_pending_count": remaining_pending_count,
        "reconciliation_complete": True,
        "outbox_committed_count": len(committed),
        "decision_handoff_unavailable_count": len(unavailable),
        "outbox_committed_completion_sha256s": sorted(committed),
        "decision_handoff_unavailable_completion_sha256s": sorted(unavailable),
        "phase_two_side_effects_inferred": False,
    }
    body["receipt_sha256"] = _sha256_json(body)
    return MappingProxyType(body)


__all__ = (
    "CapturedPaperPhaseOneHandoffError",
    "CapturedPaperPhaseOneHandoffReceipt",
    "CapturedPaperExecutedReadBinding",
    "EXECUTED_MATERIAL_SCHEMA_VERSION",
    "PHASE_ONE_HANDOFF_SCHEMA_VERSION",
    "PHASE_ONE_RESTART_SCHEMA_VERSION",
    "acknowledge_captured_paper_phase_one_handoff",
    "commit_captured_paper_phase_one_outbox_in_transaction",
    "lock_captured_paper_phase_one_for_admission",
    "record_captured_paper_phase_one_handoff",
    "reconcile_captured_paper_phase_one_after_restart",
    "verify_captured_paper_executed_read_inventory",
)
