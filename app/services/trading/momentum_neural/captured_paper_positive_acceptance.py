"""Atomic positive zero-fill adoption for captured Alpaca PAPER entries.

The broker read happens before this module is called.  Every invocation owns a
fresh short PostgreSQL transaction, walks the canonical account/risk locks via
the outbox authority seam, and either adopts the exact order into all durable
owners or rolls the whole transaction back.  No Session, clock, or network
capability escapes.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
import uuid
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .adaptive_risk_reservation import (
    AdaptiveRiskReservationStore,
    DurableOrderLifecycleEvidence,
)
from .captured_paper_outbox import (
    CapturedPaperBrokerAcceptanceProof,
    CapturedPaperPositiveAdoptionLockReceipt,
    lock_captured_paper_positive_adoption,
)
from .captured_paper_transport_coordinator import (
    CapturedPaperPositiveOrderObservation,
    CapturedPaperTransportContractError,
    CapturedPaperTransportInstruction,
    EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
)


POSITIVE_ACCEPTANCE_SCHEMA_VERSION = (
    "chili.captured-paper-positive-acceptance.v1"
)
_LIVE_EXECUTION_KEY = "momentum_live_execution"
_ACTIVE_SOURCE_STATES = frozenset(
    {"watching_live", "live_entry_candidate", "live_pending_entry"}
)


class CapturedPaperPositiveAcceptanceError(RuntimeError):
    """Stable fail-closed positive-adoption error."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_positive_acceptance_error")
        super().__init__(self.reason)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _acceptance_proof(
    instruction: CapturedPaperTransportInstruction,
    observation: CapturedPaperPositiveOrderObservation,
    *,
    acceptance_kind: str,
) -> CapturedPaperBrokerAcceptanceProof:
    return _acceptance_proof_values(
        instruction,
        acceptance_kind=acceptance_kind,
        broker_order_id=observation.broker_order_id,
        broker_order_evidence_sha256=(
            observation.broker_order_evidence_sha256
        ),
        observed_at=observation.observed_at,
        available_at=observation.available_at,
    )


def _acceptance_proof_values(
    instruction: CapturedPaperTransportInstruction,
    *,
    acceptance_kind: str,
    broker_order_id: str,
    broker_order_evidence_sha256: str,
    observed_at: Any,
    available_at: Any,
) -> CapturedPaperBrokerAcceptanceProof:
    return CapturedPaperBrokerAcceptanceProof(
        acceptance_kind=acceptance_kind,
        completion_sha256=instruction.request.completion_sha256,
        account_scope=instruction.account_scope,
        expected_account_id=instruction.expected_account_id,
        client_order_id=instruction.client_order_id,
        broker_order_id=broker_order_id,
        reservation_id=instruction.authority.reservation_id,
        action_claim_token=instruction.authority.action_claim_token,
        binder_id=instruction.authority.binder_id,
        broker_order_evidence_sha256=broker_order_evidence_sha256,
        observed_at=observed_at,
        available_at=available_at,
    )


def _lifecycle_evidence(
    instruction: CapturedPaperTransportInstruction,
    observation: CapturedPaperPositiveOrderObservation,
) -> DurableOrderLifecycleEvidence:
    return _lifecycle_evidence_values(
        instruction,
        broker_order_id=observation.broker_order_id,
        broker_connection_generation=(
            observation.broker_connection_generation
        ),
        broker_order_evidence_sha256=(
            observation.broker_order_evidence_sha256
        ),
        observed_at=observation.observed_at,
        available_at=observation.available_at,
        broker_order_status=observation.broker_order_status,
    )


def _lifecycle_evidence_values(
    instruction: CapturedPaperTransportInstruction,
    *,
    broker_order_id: str,
    broker_connection_generation: str,
    broker_order_evidence_sha256: str,
    observed_at: Any,
    available_at: Any,
    broker_order_status: str,
) -> DurableOrderLifecycleEvidence:
    authority = instruction.authority
    evidence_sha256 = broker_order_evidence_sha256
    return DurableOrderLifecycleEvidence(
        event_kind="order_accepted",
        durability_kind="authoritative_broker_event",
        provider_event_id=(
            f"captured-paper:order-accepted:{evidence_sha256}"
        ),
        broker_source="alpaca",
        connection_generation=broker_connection_generation,
        account_scope=instruction.account_scope,
        execution_family="alpaca_spot",
        broker_environment="paper",
        account_identity_sha256=authority.account_identity_sha256,
        client_order_id=instruction.client_order_id,
        broker_order_id=broker_order_id,
        observed_at=observed_at,
        available_at=available_at,
        event_content_sha256=evidence_sha256,
        cumulative_filled_quantity=0,
        source_record_table="alpaca_rest_order_observations",
        source_record_id=(
            f"{broker_order_id}:{evidence_sha256}"
        ),
        order_status=broker_order_status,
    )


def _acceptance_marker(
    instruction: CapturedPaperTransportInstruction,
    observation: CapturedPaperPositiveOrderObservation,
    proof: CapturedPaperBrokerAcceptanceProof,
) -> dict[str, Any]:
    return _acceptance_marker_values(
        instruction,
        proof,
        broker_connection_generation=(
            observation.broker_connection_generation
        ),
        broker_order_status=observation.broker_order_status,
        cumulative_filled_quantity_shares=(
            observation.cumulative_filled_quantity_shares
        ),
        broker_order_echo=observation.order.broker_echo_payload(),
    )


def _acceptance_marker_values(
    instruction: CapturedPaperTransportInstruction,
    proof: CapturedPaperBrokerAcceptanceProof,
    *,
    broker_connection_generation: str,
    broker_order_status: str,
    cumulative_filled_quantity_shares: int,
    broker_order_echo: Mapping[str, Any],
) -> dict[str, Any]:
    exact_echo = dict(broker_order_echo)
    return {
        "schema_version": POSITIVE_ACCEPTANCE_SCHEMA_VERSION,
        "acceptance_kind": proof.acceptance_kind,
        "acceptance_sha256": proof.acceptance_sha256,
        "completion_sha256": instruction.request.completion_sha256,
        "transport_authority_sha256": instruction.authority.authority_sha256,
        "account_scope": instruction.account_scope,
        "expected_account_id": instruction.expected_account_id,
        "verified_adapter_account_id": instruction.expected_account_id,
        "account_binding_source": EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
        "reservation_id": instruction.authority.reservation_id,
        "decision_packet_sha256": instruction.authority.decision_packet_sha256,
        "reservation_request_sha256": (
            instruction.authority.reservation_request_sha256
        ),
        "client_order_id": instruction.client_order_id,
        "broker_order_id": proof.broker_order_id,
        "action_claim_token": instruction.authority.action_claim_token,
        "binder_id": instruction.authority.binder_id,
        "broker_connection_generation": (
            broker_connection_generation
        ),
        "broker_order_status": broker_order_status,
        "cumulative_filled_quantity_shares": (
            cumulative_filled_quantity_shares
        ),
        "broker_order_echo": exact_echo,
        "broker_order_echo_sha256": _sha256_json(exact_echo),
        "broker_order_evidence_sha256": (
            proof.broker_order_evidence_sha256
        ),
        "observed_at": proof.observed_at.isoformat(),
        "available_at": proof.available_at.isoformat(),
    }


def _verify_persisted_broker_echo(
    instruction: CapturedPaperTransportInstruction,
    receipt: CapturedPaperPositiveAdoptionLockReceipt,
    *,
    prior_status: str,
    broker_order_echo: Mapping[str, Any],
) -> None:
    echo = dict(broker_order_echo)
    expected = {
        "broker_order_id_echo": receipt.broker_order_id,
        "broker_client_order_id_echo": instruction.client_order_id,
        "broker_symbol_echo": instruction.symbol,
        "broker_side_echo": "buy",
        "broker_order_type_echo": "limit",
        "broker_asset_class_echo": "us_equity",
        "broker_time_in_force_echo": instruction.time_in_force,
        "broker_extended_hours_echo": instruction.extended_hours,
        "broker_cumulative_filled_quantity_projection": 0,
    }
    if any(echo.get(name) != value for name, value in expected.items()):
        raise CapturedPaperPositiveAcceptanceError(
            "positive_acceptance_broker_echo_retry_mismatch"
        )
    if echo.get("broker_account_id_echo") not in {
        None,
        instruction.expected_account_id,
    }:
        raise CapturedPaperPositiveAcceptanceError(
            "positive_acceptance_broker_echo_retry_mismatch"
        )
    if echo.get("broker_position_intent_echo") not in {
        None,
        "buy_to_open",
    }:
        raise CapturedPaperPositiveAcceptanceError(
            "positive_acceptance_broker_echo_retry_mismatch"
        )
    try:
        raw_quantity = Decimal(str(echo["broker_quantity_echo"]))
        raw_limit = Decimal(str(echo["broker_limit_price_echo"]))
        raw_fill = Decimal(str(echo["broker_filled_quantity_echo"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise CapturedPaperPositiveAcceptanceError(
            "positive_acceptance_broker_echo_retry_mismatch"
        ) from exc
    if not (
        raw_quantity.is_finite()
        and raw_quantity == raw_quantity.to_integral_value()
        and int(raw_quantity) == instruction.quantity_shares
        and raw_limit.is_finite()
        and raw_limit == Decimal(instruction.limit_price)
        and raw_fill.is_finite()
        and raw_fill == 0
        and str(echo.get("broker_order_status_echo") or "").strip().lower()
        == prior_status
    ):
        raise CapturedPaperPositiveAcceptanceError(
            "positive_acceptance_broker_echo_retry_mismatch"
        )


def _verify_receipt_retry(
    receipt: CapturedPaperPositiveAdoptionLockReceipt,
    observation: CapturedPaperPositiveOrderObservation,
) -> None:
    exact_expected = {
        "broker_order_id": observation.broker_order_id,
        "broker_connection_generation": (
            observation.broker_connection_generation
        ),
    }
    mismatches = [
        name
        for name, value in exact_expected.items()
        if getattr(receipt, name) != value
    ]
    if (
        receipt.broker_observed_at is None
        or receipt.broker_available_at is None
        or receipt.broker_order_evidence_sha256 is None
    ):
        mismatches.append("persisted_broker_evidence")
    else:
        if observation.observed_at < receipt.broker_observed_at:
            mismatches.append("broker_observed_at")
        if observation.available_at < receipt.broker_available_at:
            mismatches.append("broker_available_at")
    if mismatches:
        raise CapturedPaperPositiveAcceptanceError(
            "positive_acceptance_idempotency_mismatch:"
            + ",".join(sorted(mismatches))
        )


class SqlAlchemyCapturedPaperPositiveAcceptanceRecorder:
    """Production zero-fill acceptance recorder with one atomic commit."""

    def __init__(self, bind: Engine) -> None:
        if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_postgresql_engine_required"
            )
        self._bind = bind
        self._factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        self._reservation_store = AdaptiveRiskReservationStore(bind)

    def persist_positive_acceptance(
        self,
        instruction: CapturedPaperTransportInstruction,
        observation: CapturedPaperPositiveOrderObservation,
        *,
        acceptance_kind: str,
    ) -> CapturedPaperBrokerAcceptanceProof:
        if type(instruction) is not CapturedPaperTransportInstruction:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_instruction_type_invalid"
            )
        if type(observation) is not CapturedPaperPositiveOrderObservation:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_observation_type_invalid"
            )
        if acceptance_kind not in {
            "post_response",
            "same_cid_reconciliation",
        }:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_kind_invalid"
            )
        try:
            observation.verify_for_instruction(instruction)
        except CapturedPaperTransportContractError as exc:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_observation_mismatch"
            ) from exc
        if not (
            observation.broker_order_status in {"accepted", "new"}
            and observation.cumulative_filled_quantity_shares == 0
        ):
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_zero_fill_status_required"
            )

        db = self._factory()
        try:
            with db.begin():
                proof = self._persist_in_transaction(
                    db,
                    instruction=instruction,
                    observation=observation,
                    acceptance_kind=acceptance_kind,
                )
            # The proof is intentionally returned only after ``db.begin`` has
            # committed successfully.
            return proof
        finally:
            db.close()

    def _persist_in_transaction(
        self,
        db: Any,
        *,
        instruction: CapturedPaperTransportInstruction,
        observation: CapturedPaperPositiveOrderObservation,
        acceptance_kind: str,
    ) -> CapturedPaperBrokerAcceptanceProof:
        receipt = lock_captured_paper_positive_adoption(
            db,
            completion_sha256=instruction.request.completion_sha256,
            authority=instruction.authority,
            acceptance_kind=acceptance_kind,
        )
        if not (
            receipt.request.to_canonical_json()
            == instruction.request.to_canonical_json()
            and receipt.authority_sha256
            == instruction.authority.authority_sha256
        ):
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_locked_request_mismatch"
            )
        if observation.observed_at < receipt.transport_started_at:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_predates_transport_start"
            )

        lifecycle = _lifecycle_evidence(instruction, observation)
        proof = _acceptance_proof(
            instruction, observation, acceptance_kind=acceptance_kind
        )
        marker = _acceptance_marker(instruction, observation, proof)
        if receipt.binding_state == "already_bound":
            _verify_receipt_retry(receipt, observation)
            prior_status = self._verify_idempotent_rows(
                db,
                instruction=instruction,
                receipt=receipt,
            )
            retry_lifecycle = _lifecycle_evidence_values(
                instruction,
                broker_order_id=str(receipt.broker_order_id),
                broker_connection_generation=str(
                    receipt.broker_connection_generation
                ),
                broker_order_evidence_sha256=str(
                    receipt.broker_order_evidence_sha256
                ),
                observed_at=receipt.broker_observed_at,
                available_at=receipt.broker_available_at,
                broker_order_status=prior_status,
            )
            state = self._reservation_store.mark_submitted(
                uuid.UUID(instruction.authority.reservation_id),
                evidence=retry_lifecycle,
                session=db,
            )
            self._verify_submitted_values(
                state,
                instruction,
                broker_order_id=str(receipt.broker_order_id),
                broker_connection_generation=str(
                    receipt.broker_connection_generation
                ),
                broker_order_evidence_sha256=str(
                    receipt.broker_order_evidence_sha256
                ),
                observed_at=receipt.broker_observed_at,
                available_at=receipt.broker_available_at,
            )
            return _acceptance_proof_values(
                instruction,
                acceptance_kind=acceptance_kind,
                broker_order_id=str(receipt.broker_order_id),
                broker_order_evidence_sha256=str(
                    receipt.broker_order_evidence_sha256
                ),
                observed_at=receipt.broker_observed_at,
                available_at=receipt.broker_available_at,
            )
        if receipt.binding_state != "pending_unbound":
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_locked_state_invalid"
            )

        state = self._reservation_store.mark_submitted(
            uuid.UUID(instruction.authority.reservation_id),
            evidence=lifecycle,
            session=db,
        )
        self._verify_submitted_state(state, instruction, observation)
        self._advance_action_claim(
            db,
            instruction=instruction,
            observation=observation,
            marker=marker,
        )
        self._persist_pending_session(
            db,
            instruction=instruction,
            observation=observation,
            proof=proof,
            receipt=receipt,
        )
        return proof

    @staticmethod
    def _verify_submitted_state(
        state: Any,
        instruction: CapturedPaperTransportInstruction,
        observation: CapturedPaperPositiveOrderObservation,
    ) -> None:
        SqlAlchemyCapturedPaperPositiveAcceptanceRecorder._verify_submitted_values(
            state,
            instruction,
            broker_order_id=observation.broker_order_id,
            broker_connection_generation=(
                observation.broker_connection_generation
            ),
            broker_order_evidence_sha256=(
                observation.broker_order_evidence_sha256
            ),
            observed_at=observation.observed_at,
            available_at=observation.available_at,
        )

    @staticmethod
    def _verify_submitted_values(
        state: Any,
        instruction: CapturedPaperTransportInstruction,
        *,
        broker_order_id: str,
        broker_connection_generation: str,
        broker_order_evidence_sha256: str,
        observed_at: Any,
        available_at: Any,
    ) -> None:
        expected = {
            "state": "submitted",
            "account_scope": instruction.account_scope,
            "broker_source": "alpaca",
            "broker_connection_generation": (
                broker_connection_generation
            ),
            "broker_order_id": broker_order_id,
            "last_broker_observed_at": observed_at,
            "last_broker_available_at": available_at,
            "last_source_event_content_sha256": (
                broker_order_evidence_sha256
            ),
            "cumulative_filled_quantity_shares": 0,
            "open_quantity_shares": 0,
        }
        mismatches = sorted(
            name
            for name, value in expected.items()
            if getattr(state, name) != value
        )
        if mismatches:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_reservation_mismatch:"
                + ",".join(mismatches)
            )

    @staticmethod
    def _advance_action_claim(
        db: Any,
        *,
        instruction: CapturedPaperTransportInstruction,
        observation: CapturedPaperPositiveOrderObservation,
        marker: Mapping[str, Any],
    ) -> None:
        authority = instruction.authority
        route = instruction.request.intent.route_token
        result = db.execute(
            text(
                """
                UPDATE broker_symbol_action_claims
                   SET phase = 'submitted', broker_order_id = :broker_order_id,
                       metadata_json = metadata_json || CAST(:marker AS JSONB),
                       lease_expires_at = NULL,
                       updated_at = clock_timestamp()
                 WHERE account_scope = :account_scope AND symbol = :symbol
                   AND claim_token = :claim_token AND action = 'entry'
                   AND phase = 'submit_indeterminate'
                   AND owner_session_id = :session_id
                   AND client_order_id = :client_order_id
                   AND broker_order_id IS NULL
                   AND COALESCE(metadata_json->>'alpaca_account_id', '')
                       = :expected_account_id
                   AND COALESCE(metadata_json->>'entry_post_bind_token', '')
                       = :binder_id
                   AND COALESCE(
                         metadata_json->'entry_transport_started'
                           ->>'completion_sha256', ''
                       ) = :completion_sha256
                   AND COALESCE(
                         metadata_json->'entry_transport_started'
                           ->>'transport_authority_sha256', ''
                       ) = :transport_authority_sha256
                   AND COALESCE(
                         metadata_json->'entry_transport_started'
                           ->>'client_order_id', ''
                       ) = :client_order_id
                   AND COALESCE(
                         metadata_json->'entry_transport_started'
                           ->>'post_bind_token', ''
                       ) = :binder_id
                   AND metadata_json->'captured_paper_positive_acceptance'
                       IS NULL
                """
            ),
            {
                "account_scope": instruction.account_scope,
                "symbol": instruction.symbol,
                "claim_token": authority.action_claim_token,
                "session_id": route.session_id,
                "client_order_id": instruction.client_order_id,
                "broker_order_id": observation.broker_order_id,
                "expected_account_id": instruction.expected_account_id,
                "binder_id": authority.binder_id,
                "completion_sha256": instruction.request.completion_sha256,
                "transport_authority_sha256": authority.authority_sha256,
                "marker": _canonical_json(
                    {"captured_paper_positive_acceptance": dict(marker)}
                ),
            },
        )
        if int(result.rowcount or 0) != 1:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_action_claim_cas_failed"
            )

    @staticmethod
    def _persist_pending_session(
        db: Any,
        *,
        instruction: CapturedPaperTransportInstruction,
        observation: CapturedPaperPositiveOrderObservation,
        proof: CapturedPaperBrokerAcceptanceProof,
        receipt: CapturedPaperPositiveAdoptionLockReceipt,
    ) -> None:
        route = instruction.request.intent.route_token
        row = db.execute(
            text(
                """
                SELECT id, state, risk_snapshot_json, ended_at,
                       correlation_id, source_node_id
                  FROM trading_automation_sessions
                 WHERE id = :session_id
                 FOR UPDATE
                """
            ),
            {"session_id": route.session_id},
        ).mappings().one_or_none()
        if row is None:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_session_missing"
            )
        source_state = str(row["state"] or "")
        if row["ended_at"] is None and source_state not in _ACTIVE_SOURCE_STATES:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_active_session_state_invalid"
            )
        snapshot = (
            dict(row["risk_snapshot_json"])
            if type(row["risk_snapshot_json"]) is dict
            else {}
        )
        live = snapshot.get(_LIVE_EXECUTION_KEY)
        live = dict(live) if type(live) is dict else {}
        history = [str(value) for value in (live.get("entry_order_ids_all") or [])]
        resolved = live.get("entry_orders_resolved")
        resolved = dict(resolved) if type(resolved) is dict else {}
        existing_cid = str(live.get("entry_client_order_id") or "").strip()
        existing_oid = str(live.get("entry_order_id") or "").strip()
        broker_order_echo = observation.order.broker_echo_payload()
        broker_order_echo_sha256 = _sha256_json(broker_order_echo)
        if not (
            live.get("position") is None
            and all(value == observation.broker_order_id for value in history)
            and not resolved
            and existing_cid in {"", instruction.client_order_id}
            and existing_oid in {"", observation.broker_order_id}
            and (
                live.get("entry_submitted") is not True
                or existing_cid == instruction.client_order_id
            )
        ):
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_session_entry_conflict"
            )

        live.update(
            {
                "entry_submitted": True,
                "entry_submit_utc": receipt.transport_started_at.isoformat(),
                "entry_order_type": "limit",
                "entry_limit_price": instruction.limit_price,
                "entry_quantity_shares": instruction.quantity_shares,
                "entry_client_order_id": instruction.client_order_id,
                "entry_order_id": observation.broker_order_id,
                "entry_order_ids_all": [observation.broker_order_id],
                "entry_orders_resolved": {},
                "entry_place_result": {
                    "ok": True,
                    "acceptance_kind": proof.acceptance_kind,
                    "acceptance_sha256": proof.acceptance_sha256,
                    "broker_order_echo": broker_order_echo,
                    "broker_order_echo_sha256": broker_order_echo_sha256,
                },
                "adaptive_risk_reservation_id": (
                    instruction.authority.reservation_id
                ),
                "adaptive_risk_decision_packet_sha256": (
                    instruction.authority.decision_packet_sha256
                ),
                "adaptive_risk_reservation_request_sha256": (
                    instruction.authority.reservation_request_sha256
                ),
                "alpaca_account_scope": instruction.account_scope,
                "alpaca_account_id": instruction.expected_account_id,
                "entry_post_bind_token": instruction.authority.binder_id,
                "entry_symbol_claim_token": (
                    instruction.authority.action_claim_token
                ),
                "entry_broker_connection_generation": (
                    observation.broker_connection_generation
                ),
                "entry_broker_order_status": observation.broker_order_status,
                "entry_broker_order_evidence_sha256": (
                    observation.broker_order_evidence_sha256
                ),
                "entry_broker_observed_at": observation.observed_at.isoformat(),
                "entry_broker_available_at": (
                    observation.available_at.isoformat()
                ),
            }
        )
        live.pop("entry_reconcile_pending_client_order_id", None)
        live.pop("entry_reconcile_pending_since_utc", None)
        snapshot[_LIVE_EXECUTION_KEY] = live
        result = db.execute(
            text(
                """
                UPDATE trading_automation_sessions
                   SET state = 'live_pending_entry',
                       risk_snapshot_json = CAST(:snapshot AS JSONB),
                       updated_at = clock_timestamp()
                 WHERE id = :session_id AND state = :source_state
                """
            ),
            {
                "session_id": route.session_id,
                "source_state": source_state,
                "snapshot": _canonical_json(snapshot),
            },
        )
        if int(result.rowcount or 0) != 1:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_session_cas_failed"
            )
        event_payload = {
            "schema_version": POSITIVE_ACCEPTANCE_SCHEMA_VERSION,
            "client_order_id": instruction.client_order_id,
            "broker_order_id": observation.broker_order_id,
            "order_type": "limit",
            "limit_price": instruction.limit_price,
            "quantity_shares": instruction.quantity_shares,
            "acceptance_kind": proof.acceptance_kind,
            "acceptance_sha256": proof.acceptance_sha256,
            "reservation_id": instruction.authority.reservation_id,
            "broker_order_evidence_sha256": (
                observation.broker_order_evidence_sha256
            ),
            "broker_order_echo": broker_order_echo,
            "broker_order_echo_sha256": broker_order_echo_sha256,
        }
        db.execute(
            text(
                """
                INSERT INTO trading_automation_events (
                    session_id, ts, event_type, payload_json,
                    correlation_id, source_node_id
                ) VALUES (
                    :session_id, clock_timestamp(), 'live_entry_submitted',
                    CAST(:payload AS JSONB), :correlation_id, :source_node_id
                )
                """
            ),
            {
                "session_id": route.session_id,
                "payload": _canonical_json(event_payload),
                "correlation_id": row["correlation_id"],
                "source_node_id": row["source_node_id"],
            },
        )

    @staticmethod
    def _verify_idempotent_rows(
        db: Any,
        *,
        instruction: CapturedPaperTransportInstruction,
        receipt: CapturedPaperPositiveAdoptionLockReceipt,
    ) -> str:
        action = db.execute(
            text(
                """
                SELECT phase, broker_order_id, metadata_json, lease_expires_at
                  FROM broker_symbol_action_claims
                 WHERE account_scope = :account_scope AND symbol = :symbol
                 FOR UPDATE
                """
            ),
            {
                "account_scope": instruction.account_scope,
                "symbol": instruction.symbol,
            },
        ).mappings().one_or_none()
        action_metadata = (
            action["metadata_json"]
            if action is not None and type(action["metadata_json"]) is dict
            else {}
        )
        prior_marker = action_metadata.get(
            "captured_paper_positive_acceptance"
        )
        prior_marker = (
            prior_marker if type(prior_marker) is dict else {}
        )
        prior_kind = prior_marker.get("acceptance_kind")
        if prior_kind not in {
            "post_response",
            "same_cid_reconciliation",
        }:
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_action_retry_mismatch"
            )
        prior_status = str(prior_marker.get("broker_order_status") or "")
        prior_echo = prior_marker.get("broker_order_echo")
        prior_echo = prior_echo if type(prior_echo) is dict else {}
        if not (
            prior_status in {"accepted", "new"}
            and receipt.broker_order_id is not None
            and receipt.broker_connection_generation is not None
            and receipt.broker_order_evidence_sha256 is not None
            and receipt.broker_observed_at is not None
            and receipt.broker_available_at is not None
        ):
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_action_retry_mismatch"
            )
        _verify_persisted_broker_echo(
            instruction,
            receipt,
            prior_status=prior_status,
            broker_order_echo=prior_echo,
        )
        prior_proof = CapturedPaperBrokerAcceptanceProof(
            acceptance_kind=str(prior_kind),
            completion_sha256=instruction.request.completion_sha256,
            account_scope=instruction.account_scope,
            expected_account_id=instruction.expected_account_id,
            client_order_id=instruction.client_order_id,
            broker_order_id=receipt.broker_order_id,
            reservation_id=instruction.authority.reservation_id,
            action_claim_token=instruction.authority.action_claim_token,
            binder_id=instruction.authority.binder_id,
            broker_order_evidence_sha256=(
                receipt.broker_order_evidence_sha256
            ),
            observed_at=receipt.broker_observed_at,
            available_at=receipt.broker_available_at,
        )
        expected_prior_marker = _acceptance_marker_values(
            instruction,
            prior_proof,
            broker_connection_generation=(
                receipt.broker_connection_generation
            ),
            broker_order_status=prior_status,
            cumulative_filled_quantity_shares=0,
            broker_order_echo=prior_echo,
        )
        if not (
            action is not None
            and action["phase"] == "submitted"
            and action["broker_order_id"] == receipt.broker_order_id
            and action["lease_expires_at"] is None
            and prior_marker == expected_prior_marker
        ):
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_action_retry_mismatch"
            )
        session = db.execute(
            text(
                """
                SELECT state, risk_snapshot_json
                  FROM trading_automation_sessions
                 WHERE id = :session_id
                 FOR UPDATE
                """
            ),
            {"session_id": instruction.request.intent.route_token.session_id},
        ).mappings().one_or_none()
        snapshot = (
            session["risk_snapshot_json"]
            if session is not None and type(session["risk_snapshot_json"]) is dict
            else {}
        )
        live = snapshot.get(_LIVE_EXECUTION_KEY)
        live = live if type(live) is dict else {}
        place_result = live.get("entry_place_result")
        place_result = place_result if type(place_result) is dict else {}
        if not (
            session is not None
            and session["state"] == "live_pending_entry"
            and live.get("entry_submitted") is True
            and live.get("entry_client_order_id")
            == instruction.client_order_id
            and live.get("entry_order_id") == receipt.broker_order_id
            and live.get("entry_order_ids_all")
            == [receipt.broker_order_id]
            and live.get("entry_orders_resolved") == {}
            and place_result
            == {
                "ok": True,
                "acceptance_kind": prior_proof.acceptance_kind,
                "acceptance_sha256": prior_proof.acceptance_sha256,
                "broker_order_echo": prior_echo,
                "broker_order_echo_sha256": _sha256_json(prior_echo),
            }
            and live.get("adaptive_risk_reservation_id")
            == instruction.authority.reservation_id
            and live.get("adaptive_risk_decision_packet_sha256")
            == instruction.authority.decision_packet_sha256
            and live.get("adaptive_risk_reservation_request_sha256")
            == instruction.authority.reservation_request_sha256
            and live.get("alpaca_account_scope")
            == instruction.account_scope
            and live.get("alpaca_account_id")
            == instruction.expected_account_id
            and live.get("entry_post_bind_token")
            == instruction.authority.binder_id
            and live.get("entry_symbol_claim_token")
            == instruction.authority.action_claim_token
            and live.get("entry_broker_connection_generation")
            == receipt.broker_connection_generation
            and live.get("entry_broker_order_status")
            == prior_status
            and live.get("entry_broker_order_evidence_sha256")
            == receipt.broker_order_evidence_sha256
            and live.get("entry_broker_observed_at")
            == receipt.broker_observed_at.isoformat()
            and live.get("entry_broker_available_at")
            == receipt.broker_available_at.isoformat()
        ):
            raise CapturedPaperPositiveAcceptanceError(
                "positive_acceptance_session_retry_mismatch"
            )
        return prior_status


__all__ = (
    "CapturedPaperPositiveAcceptanceError",
    "POSITIVE_ACCEPTANCE_SCHEMA_VERSION",
    "SqlAlchemyCapturedPaperPositiveAcceptanceRecorder",
)
