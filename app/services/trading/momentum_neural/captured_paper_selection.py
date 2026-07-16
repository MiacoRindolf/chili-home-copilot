"""Process-private phase-zero selection handoff for captured Alpaca PAPER.

The normal momentum FSM still owns setup selection.  A capture-owned runtime
may install one immutable expectation around exactly one FSM invocation.  Once
the FSM has selected a setup and has an executable BBO plus structural stop,
``resolve_captured_paper_selection`` proves that those already-observed values
match the expectation and returns its exact ``CapturedPaperPostCommitRequest``.

This module is intentionally pure with respect to persistence and external
systems.  It cannot receive a SQLAlchemy session, fetch market/account data,
reserve risk, claim an opportunity, construct an adapter, or submit an order.
Missing, stale, or mismatched evidence is a decision-local unavailable result;
the caller may return the session to WATCHING without consuming anything.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
import uuid
from typing import Any, Iterator, Mapping

from .captured_paper_dispatcher import (
    CapturedPaperDispatchError,
    CapturedPaperDispatchRequest,
)
from .captured_paper_entry_intent import (
    CapturedPaperConfirmedArmGeneration,
    CapturedPaperEntryIntent,
    CapturedPaperIntentContractError,
    CapturedPaperOpportunityKey,
    CapturedPaperPostCommitRequest,
)


UTC = timezone.utc
CAPTURED_PAPER_SELECTION_CONTEXT_SCHEMA_VERSION = (
    "chili.captured-paper-selection-context.v1"
)
CAPTURED_PAPER_TRIGGER_SNAPSHOT_SCHEMA_VERSION = (
    "chili.captured-paper-trigger-snapshot.v1"
)
CAPTURED_PAPER_OBSERVATION_CONTEXT_SCHEMA_VERSION = (
    "chili.captured-paper-observation-context.v1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SETUP_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_GENERATION_NAMESPACE = uuid.UUID("d9336069-3ef3-4a39-8a66-6ab813086917")
_FIRST_DIP_SETUP_FAMILY = "first_dip_reclaim"
_OBSERVATION_STATES = frozenset({"queued_live", "watching_live"})


class CapturedPaperSelectionContextError(ValueError):
    """The process-private captured selection material is invalid."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_selection_context_invalid")
        super().__init__(self.reason)


def _canonical_json(payload: Any) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_json_invalid"
        ) from exc


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sha256(value: Any, *, field_name: str) -> str:
    digest = str(value or "").strip()
    if digest != value or _SHA256_RE.fullmatch(digest) is None:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    return digest


def _aware_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CapturedPaperSelectionContextError(
            f"{field_name}_invalid"
        ) from exc
    if offset is None:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    return value.astimezone(UTC)


def _instant_from_marker(value: Any, *, field_name: str) -> datetime:
    """Parse the durable marker's established naive-is-UTC timestamp shape."""

    if not isinstance(value, str) or not value:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperSelectionContextError(
            f"{field_name}_invalid"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_decimal(value: Any, *, field_name: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionContextError(
            f"{field_name}_invalid"
        ) from exc
    if not number.is_finite() or number <= 0:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    canonical = format(number.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if not canonical or len(canonical) > 96:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    return canonical


def _unit_decimal(value: Any, *, field_name: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionContextError(
            f"{field_name}_invalid"
        ) from exc
    if not number.is_finite() or not Decimal("0") <= number <= Decimal("1"):
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    canonical = format(number.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def _optional_nonnegative_decimal(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionContextError(
            f"{field_name}_invalid"
        ) from exc
    if not number.is_finite() or number < 0:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    canonical = format(number.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if not canonical or len(canonical) > 96:
        raise CapturedPaperSelectionContextError(f"{field_name}_invalid")
    return canonical


def captured_paper_trigger_snapshot_sha256(
    *,
    trigger_reason: str,
    trigger_debug: Mapping[str, Any],
) -> str:
    """Hash the exact setup identity the live detector persisted this tick."""

    reason = str(trigger_reason or "")
    if reason != trigger_reason or not reason.strip():
        raise CapturedPaperSelectionContextError(
            "captured_paper_trigger_reason_invalid"
        )
    if type(trigger_debug) is not dict:
        raise CapturedPaperSelectionContextError(
            "captured_paper_trigger_debug_invalid"
        )
    return _sha256_json(
        {
            "schema_version": CAPTURED_PAPER_TRIGGER_SNAPSHOT_SCHEMA_VERSION,
            "trigger_reason": reason,
            "trigger_debug": trigger_debug,
        }
    )


def captured_paper_candidate_generation_sha256(
    *,
    session_id: Any,
    symbol: Any,
    execution_family: Any,
    entry_place_count: Any,
    client_order_id: Any,
    setup_family: Any,
    structural_stop_price: Any,
    trigger_reason: Any,
    trigger_debug: Mapping[str, Any],
    confirmed_arm_marker: Mapping[str, Any],
    viability_updated_at: datetime,
    viability_score: Any,
    viability_payload_sha256: Any,
    execution_readiness_sha256: Any,
) -> str:
    """Hash the durable fields that must survive capture I/O unchanged.

    Volatile session timestamps and unrelated execution telemetry are excluded.
    The final FSM lock recomputes this exact generation immediately before it
    may return a typed post-commit request.
    """

    try:
        if isinstance(session_id, bool) or int(session_id) <= 0:
            raise ValueError("session")
        if isinstance(entry_place_count, bool) or int(entry_place_count) <= 0:
            raise ValueError("place")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperSelectionContextError(
            "captured_paper_candidate_generation_invalid"
        ) from exc
    normalized_symbol = str(symbol or "").strip().upper()
    family = str(execution_family or "").strip().lower()
    cid = str(client_order_id or "").strip()
    setup = str(setup_family or "").strip().lower()
    reason = str(trigger_reason or "")
    if (
        not normalized_symbol
        or family != "alpaca_spot"
        or not cid
        or _SETUP_RE.fullmatch(setup) is None
        or not reason.strip()
        or type(trigger_debug) is not dict
        or type(confirmed_arm_marker) is not dict
    ):
        raise CapturedPaperSelectionContextError(
            "captured_paper_candidate_generation_invalid"
        )
    stop = _positive_decimal(
        structural_stop_price,
        field_name="captured_paper_candidate_structural_stop",
    )
    viability_updated = _aware_utc(
        viability_updated_at,
        field_name="captured_paper_candidate_viability_updated_at",
    )
    viability = _unit_decimal(
        viability_score,
        field_name="captured_paper_candidate_viability_score",
    )
    viability_payload_digest = _sha256(
        viability_payload_sha256,
        field_name="captured_paper_candidate_viability_payload_sha256",
    )
    readiness_digest = _sha256(
        execution_readiness_sha256,
        field_name="captured_paper_candidate_execution_readiness_sha256",
    )
    return _sha256_json(
        {
            "schema_version": "chili.captured-paper-candidate-generation.v4",
            "session_id": int(session_id),
            "symbol": normalized_symbol,
            "execution_family": family,
            "entry_place_count": int(entry_place_count),
            "client_order_id": cid,
            "setup_family": setup,
            "structural_stop_price": stop,
            "trigger_reason": reason,
            "trigger_debug": trigger_debug,
            "confirmed_arm_marker": confirmed_arm_marker,
            "viability_updated_at": viability_updated.isoformat(),
            "viability_score": viability,
            "viability_payload_sha256": viability_payload_digest,
            "execution_readiness_sha256": readiness_digest,
        }
    )


def captured_paper_observation_generation_sha256(
    *,
    session_id: Any,
    symbol: Any,
    execution_family: Any,
    state: Any,
    correlation_id: Any,
    variant_id: Any,
    session_updated_at: datetime,
    risk_snapshot_sha256: Any,
    viability_payload_sha256: Any,
    variant_payload_sha256: Any,
    confirmed_arm_marker_sha256: Any,
) -> str:
    """Hash the exact durable watcher generation captured before provider I/O."""

    try:
        if isinstance(session_id, bool) or int(session_id) <= 0:
            raise ValueError("session")
        if isinstance(variant_id, bool) or int(variant_id) <= 0:
            raise ValueError("variant")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperSelectionContextError(
            "captured_paper_observation_generation_invalid"
        ) from exc
    normalized_symbol = str(symbol or "").strip().upper()
    family = str(execution_family or "").strip().lower()
    normalized_state = str(state or "").strip().lower()
    correlation = str(correlation_id or "").strip()
    if (
        not normalized_symbol
        or family != "alpaca_spot"
        or normalized_state not in _OBSERVATION_STATES
        or not correlation
    ):
        raise CapturedPaperSelectionContextError(
            "captured_paper_observation_generation_invalid"
        )
    updated = _aware_utc(
        session_updated_at,
        field_name="captured_paper_observation_session_updated_at",
    )
    bound_hashes = {
        name: _sha256(value, field_name=name)
        for name, value in {
            "captured_paper_observation_risk_snapshot_sha256": (
                risk_snapshot_sha256
            ),
            "captured_paper_observation_viability_payload_sha256": (
                viability_payload_sha256
            ),
            "captured_paper_observation_variant_payload_sha256": (
                variant_payload_sha256
            ),
            "captured_paper_observation_confirmed_arm_marker_sha256": (
                confirmed_arm_marker_sha256
            ),
        }.items()
    }
    return _sha256_json(
        {
            "schema_version": "chili.captured-paper-observation-generation.v1",
            "session_id": int(session_id),
            "symbol": normalized_symbol,
            "execution_family": family,
            "state": normalized_state,
            "correlation_id": correlation,
            "variant_id": int(variant_id),
            "session_updated_at": updated.isoformat(),
            **bound_hashes,
        }
    )


def _arm_marker_sha256(marker: Mapping[str, Any]) -> str:
    if type(marker) is not dict:
        raise CapturedPaperSelectionContextError(
            "captured_paper_confirmed_arm_marker_invalid"
        )
    return _sha256_json(marker)


def _verify_arm_marker_semantics(
    marker: Mapping[str, Any],
    arm: CapturedPaperConfirmedArmGeneration,
) -> None:
    if type(marker) is not dict:
        raise CapturedPaperSelectionContextError(
            "captured_paper_confirmed_arm_marker_invalid"
        )
    exact = {
        "version": 1,
        "session_id": arm.session_id,
        "arm_token": arm.arm_token,
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "alpaca_account_scope": arm.account_scope,
        "alpaca_account_id": arm.expected_account_id,
    }
    if any(marker.get(name) != value for name, value in exact.items()):
        raise CapturedPaperSelectionContextError(
            "captured_paper_confirmed_arm_marker_mismatch"
        )
    expires_at = _instant_from_marker(
        marker.get("expires_at_utc"), field_name="confirmed_arm_expires_at"
    )
    confirmed_at = _instant_from_marker(
        marker.get("confirmed_at_utc"), field_name="confirmed_arm_confirmed_at"
    )
    if expires_at != arm.expires_at or confirmed_at != arm.confirmed_at:
        raise CapturedPaperSelectionContextError(
            "captured_paper_confirmed_arm_clock_mismatch"
        )


def _selection_seed_payload(
    *,
    dispatch_request: CapturedPaperDispatchRequest,
    entry_place_count: int,
    client_order_id: str,
    setup_family: str,
    decision_at: datetime,
    bid: str,
    ask: str,
    structural_stop_price: str,
    entry_limit_ceiling_price: str,
    trigger_snapshot_sha256: str,
    confirmed_arm_marker_sha256: str,
    account_receipt_sha256: str,
    bbo_receipt_sha256: str,
    setup_evidence_sha256: str,
    policy_sha256: str,
    feature_flags_sha256: str,
    opportunity_key: CapturedPaperOpportunityKey | None,
) -> dict[str, Any]:
    return {
        "route_token_sha256": dispatch_request.route_token.route_token_sha256,
        "entry_place_count": entry_place_count,
        "client_order_id": client_order_id,
        "setup_family": setup_family,
        "decision_at": decision_at.isoformat(),
        "bid": bid,
        "ask": ask,
        "structural_stop_price": structural_stop_price,
        "entry_limit_ceiling_price": entry_limit_ceiling_price,
        "trigger_snapshot_sha256": trigger_snapshot_sha256,
        "confirmed_arm_marker_sha256": confirmed_arm_marker_sha256,
        "account_receipt_sha256": account_receipt_sha256,
        "bbo_receipt_sha256": bbo_receipt_sha256,
        "setup_evidence_sha256": setup_evidence_sha256,
        "policy_sha256": policy_sha256,
        "feature_flags_sha256": feature_flags_sha256,
        "opportunity_key_sha256": (
            opportunity_key.opportunity_key_sha256
            if opportunity_key is not None
            else None
        ),
    }


def _derived_generation(seed_sha256: str, role: str) -> str:
    return str(uuid.uuid5(_GENERATION_NAMESPACE, f"{role}:{seed_sha256}"))


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionContext:
    """One immutable pre-captured expectation for one PAPER FSM decision."""

    dispatch_request: CapturedPaperDispatchRequest
    draft: CapturedPaperPostCommitRequest
    entry_place_count: int
    expected_bid: str
    expected_ask: str
    trigger_reason: str
    trigger_snapshot_sha256: str
    candidate_generation_sha256: str
    confirmed_arm_marker_sha256: str
    evidence_available_at: datetime
    evidence_expires_at: datetime
    context_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.dispatch_request) is not CapturedPaperDispatchRequest:
            raise CapturedPaperSelectionContextError(
                "captured_paper_dispatch_request_invalid"
            )
        if type(self.draft) is not CapturedPaperPostCommitRequest:
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_draft_invalid"
            )
        try:
            self.dispatch_request.verify()
            self.draft.verify()
        except (
            CapturedPaperDispatchError,
            CapturedPaperIntentContractError,
            ValueError,
        ) as exc:
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_contract_invalid"
            ) from exc
        if (
            self.dispatch_request.route_token.route_token_sha256
            != self.draft.route_token.route_token_sha256
        ):
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_route_mismatch"
            )
        if isinstance(self.entry_place_count, bool) or int(
            self.entry_place_count
        ) <= 0:
            raise CapturedPaperSelectionContextError(
                "captured_paper_entry_place_count_invalid"
            )
        object.__setattr__(self, "entry_place_count", int(self.entry_place_count))
        bid = _positive_decimal(self.expected_bid, field_name="captured_paper_bid")
        ask = _positive_decimal(self.expected_ask, field_name="captured_paper_ask")
        if Decimal(bid) > Decimal(ask):
            raise CapturedPaperSelectionContextError(
                "captured_paper_bbo_crossed"
            )
        object.__setattr__(self, "expected_bid", bid)
        object.__setattr__(self, "expected_ask", ask)
        if self.trigger_reason != str(self.trigger_reason or "") or not self.trigger_reason.strip():
            raise CapturedPaperSelectionContextError(
                "captured_paper_trigger_reason_invalid"
            )
        object.__setattr__(
            self,
            "trigger_snapshot_sha256",
            _sha256(
                self.trigger_snapshot_sha256,
                field_name="captured_paper_trigger_snapshot_sha256",
            ),
        )
        object.__setattr__(
            self,
            "candidate_generation_sha256",
            _sha256(
                self.candidate_generation_sha256,
                field_name="captured_paper_candidate_generation_sha256",
            ),
        )
        object.__setattr__(
            self,
            "confirmed_arm_marker_sha256",
            _sha256(
                self.confirmed_arm_marker_sha256,
                field_name="captured_paper_confirmed_arm_marker_sha256",
            ),
        )
        available = _aware_utc(
            self.evidence_available_at,
            field_name="captured_paper_evidence_available_at",
        )
        expires = _aware_utc(
            self.evidence_expires_at,
            field_name="captured_paper_evidence_expires_at",
        )
        decision_at = self.draft.intent.decision_at
        if not (available <= decision_at <= expires):
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_evidence_clock_invalid"
            )
        object.__setattr__(self, "evidence_available_at", available)
        object.__setattr__(self, "evidence_expires_at", expires)
        self._verify_deterministic_generations()
        object.__setattr__(self, "context_sha256", _sha256_json(self._body()))

    @classmethod
    def create(
        cls,
        *,
        dispatch_request: CapturedPaperDispatchRequest,
        confirmed_arm_generation: CapturedPaperConfirmedArmGeneration,
        confirmed_arm_marker: Mapping[str, Any],
        entry_place_count: int,
        client_order_id: str,
        setup_family: str,
        decision_at: datetime,
        evidence_available_at: datetime,
        evidence_expires_at: datetime,
        bid: Any,
        ask: Any,
        structural_stop_price: Any,
        entry_limit_ceiling_price: Any,
        trigger_reason: str,
        trigger_debug: Mapping[str, Any],
        candidate_generation_sha256: str,
        viability_updated_at: datetime,
        viability_score: Any,
        viability_payload_sha256: str,
        execution_readiness_sha256: str,
        account_receipt_sha256: str,
        bbo_receipt_sha256: str,
        setup_evidence_sha256: str,
        policy_sha256: str,
        feature_flags_sha256: str,
        opportunity_key: CapturedPaperOpportunityKey | None,
    ) -> "CapturedPaperSelectionContext":
        if type(dispatch_request) is not CapturedPaperDispatchRequest:
            raise CapturedPaperSelectionContextError(
                "captured_paper_dispatch_request_invalid"
            )
        dispatch_request.verify()
        if type(confirmed_arm_generation) is not CapturedPaperConfirmedArmGeneration:
            raise CapturedPaperSelectionContextError(
                "captured_paper_confirmed_arm_generation_invalid"
            )
        confirmed_arm_generation.verify()
        _verify_arm_marker_semantics(confirmed_arm_marker, confirmed_arm_generation)
        if isinstance(entry_place_count, bool) or int(entry_place_count) <= 0:
            raise CapturedPaperSelectionContextError(
                "captured_paper_entry_place_count_invalid"
            )
        place_count = int(entry_place_count)
        setup = str(setup_family or "").strip().lower()
        if setup != setup_family or _SETUP_RE.fullmatch(setup) is None:
            raise CapturedPaperSelectionContextError(
                "captured_paper_setup_family_invalid"
            )
        decision = _aware_utc(
            decision_at, field_name="captured_paper_decision_at"
        )
        bid_text = _positive_decimal(bid, field_name="captured_paper_bid")
        ask_text = _positive_decimal(ask, field_name="captured_paper_ask")
        stop_text = _positive_decimal(
            structural_stop_price,
            field_name="captured_paper_structural_stop",
        )
        ceiling_text = _positive_decimal(
            entry_limit_ceiling_price,
            field_name="captured_paper_entry_limit_ceiling",
        )
        if not Decimal(bid_text) <= Decimal(ask_text):
            raise CapturedPaperSelectionContextError(
                "captured_paper_bbo_crossed"
            )
        if not Decimal(stop_text) < Decimal(ceiling_text):
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_price_order_invalid"
            )
        trigger_sha256 = captured_paper_trigger_snapshot_sha256(
            trigger_reason=trigger_reason,
            trigger_debug=trigger_debug,
        )
        arm_marker_sha256 = _arm_marker_sha256(confirmed_arm_marker)
        candidate_sha256 = _sha256(
            candidate_generation_sha256,
            field_name="captured_paper_candidate_generation_sha256",
        )
        expected_candidate_sha256 = captured_paper_candidate_generation_sha256(
            session_id=dispatch_request.session_id,
            symbol=dispatch_request.symbol,
            execution_family=dispatch_request.execution_family,
            entry_place_count=place_count,
            client_order_id=client_order_id,
            setup_family=setup,
            structural_stop_price=stop_text,
            trigger_reason=trigger_reason,
            trigger_debug=trigger_debug,
            confirmed_arm_marker=confirmed_arm_marker,
            viability_updated_at=viability_updated_at,
            viability_score=viability_score,
            viability_payload_sha256=viability_payload_sha256,
            execution_readiness_sha256=execution_readiness_sha256,
        )
        if candidate_sha256 != expected_candidate_sha256:
            raise CapturedPaperSelectionContextError(
                "captured_paper_candidate_generation_mismatch"
            )
        hashes = {
            name: _sha256(value, field_name=name)
            for name, value in {
                "account_receipt_sha256": account_receipt_sha256,
                "bbo_receipt_sha256": bbo_receipt_sha256,
                "setup_evidence_sha256": setup_evidence_sha256,
                "policy_sha256": policy_sha256,
                "feature_flags_sha256": feature_flags_sha256,
            }.items()
        }
        if setup == _FIRST_DIP_SETUP_FAMILY:
            if type(opportunity_key) is not CapturedPaperOpportunityKey:
                raise CapturedPaperSelectionContextError(
                    "captured_paper_first_dip_opportunity_missing"
                )
            opportunity_key.verify()
            if (
                trigger_debug.get(
                    "first_dip_tape_decision_receipt_binding_sha256"
                )
                != hashes["setup_evidence_sha256"]
            ):
                raise CapturedPaperSelectionContextError(
                    "captured_paper_first_dip_setup_evidence_mismatch"
                )
        elif opportunity_key is not None:
            raise CapturedPaperSelectionContextError(
                "captured_paper_non_first_dip_opportunity_prohibited"
            )
        seed = _sha256_json(
            _selection_seed_payload(
                dispatch_request=dispatch_request,
                entry_place_count=place_count,
                client_order_id=client_order_id,
                setup_family=setup,
                decision_at=decision,
                bid=bid_text,
                ask=ask_text,
                structural_stop_price=stop_text,
                entry_limit_ceiling_price=ceiling_text,
                trigger_snapshot_sha256=trigger_sha256,
                confirmed_arm_marker_sha256=arm_marker_sha256,
                opportunity_key=opportunity_key,
                **hashes,
            )
        )
        intent = CapturedPaperEntryIntent(
            route_token=dispatch_request.route_token,
            confirmed_arm_generation=confirmed_arm_generation,
            symbol_claim_token=confirmed_arm_generation.symbol_claim_token,
            binder_id=_derived_generation(seed, "binder"),
            opportunity_key=opportunity_key,
            intent_generation=_derived_generation(seed, "intent"),
            decision_id=client_order_id,
            client_order_id=client_order_id,
            setup_family=setup,
            decision_at=decision,
            structural_stop_price=stop_text,
            entry_limit_ceiling_price=ceiling_text,
            **hashes,
        )
        draft = CapturedPaperPostCommitRequest(
            intent=intent,
            completion_generation=_derived_generation(seed, "completion"),
        )
        return cls(
            dispatch_request=dispatch_request,
            draft=draft,
            entry_place_count=place_count,
            expected_bid=bid_text,
            expected_ask=ask_text,
            trigger_reason=trigger_reason,
            trigger_snapshot_sha256=trigger_sha256,
            candidate_generation_sha256=candidate_sha256,
            confirmed_arm_marker_sha256=arm_marker_sha256,
            evidence_available_at=evidence_available_at,
            evidence_expires_at=evidence_expires_at,
        )

    def _seed_sha256(self) -> str:
        intent = self.draft.intent
        return _sha256_json(
            _selection_seed_payload(
                dispatch_request=self.dispatch_request,
                entry_place_count=self.entry_place_count,
                client_order_id=intent.client_order_id,
                setup_family=intent.setup_family,
                decision_at=intent.decision_at,
                bid=self.expected_bid,
                ask=self.expected_ask,
                structural_stop_price=intent.structural_stop_price,
                entry_limit_ceiling_price=intent.entry_limit_ceiling_price,
                trigger_snapshot_sha256=self.trigger_snapshot_sha256,
                confirmed_arm_marker_sha256=self.confirmed_arm_marker_sha256,
                account_receipt_sha256=intent.account_receipt_sha256,
                bbo_receipt_sha256=intent.bbo_receipt_sha256,
                setup_evidence_sha256=intent.setup_evidence_sha256,
                policy_sha256=intent.policy_sha256,
                feature_flags_sha256=intent.feature_flags_sha256,
                opportunity_key=intent.opportunity_key,
            )
        )

    def _verify_deterministic_generations(self) -> None:
        seed = self._seed_sha256()
        intent = self.draft.intent
        expected = {
            "binder": (intent.binder_id, _derived_generation(seed, "binder")),
            "intent": (
                intent.intent_generation,
                _derived_generation(seed, "intent"),
            ),
            "completion": (
                self.draft.completion_generation,
                _derived_generation(seed, "completion"),
            ),
        }
        if any(actual != derived for actual, derived in expected.values()):
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_generation_mismatch"
            )

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURED_PAPER_SELECTION_CONTEXT_SCHEMA_VERSION,
            "dispatch_provenance_sha256": self.dispatch_request.provenance_sha256,
            "completion_sha256": self.draft.completion_sha256,
            "entry_place_count": self.entry_place_count,
            "expected_bid": self.expected_bid,
            "expected_ask": self.expected_ask,
            "trigger_reason": self.trigger_reason,
            "trigger_snapshot_sha256": self.trigger_snapshot_sha256,
            "candidate_generation_sha256": self.candidate_generation_sha256,
            "confirmed_arm_marker_sha256": self.confirmed_arm_marker_sha256,
            "evidence_available_at": self.evidence_available_at.isoformat(),
            "evidence_expires_at": self.evidence_expires_at.isoformat(),
        }

    def verify(self) -> None:
        try:
            self.dispatch_request.verify()
            self.draft.verify()
        except (
            CapturedPaperDispatchError,
            CapturedPaperIntentContractError,
            ValueError,
        ) as exc:
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_contract_invalid"
            ) from exc
        if (
            self.dispatch_request.route_token.route_token_sha256
            != self.draft.route_token.route_token_sha256
        ):
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_route_mismatch"
            )
        self._verify_deterministic_generations()
        if _sha256_json(self._body()) != self.context_sha256:
            raise CapturedPaperSelectionContextError(
                "captured_paper_selection_context_mutated"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionResolution:
    """Inactive, unavailable, or exact typed completion for the hook."""

    active: bool
    request: CapturedPaperPostCommitRequest | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.active:
            if (self.request is None) == (self.reason is None):
                raise ValueError(
                    "active captured selection needs exactly one result shape"
                )
            if self.request is not None:
                self.request.verify()
        elif self.request is not None or self.reason is not None:
            raise ValueError("inactive captured selection cannot carry a result")


@dataclass(frozen=True, slots=True)
class CapturedPaperObservationContext:
    """Exact pre-provider watcher generation with no admission capability."""

    dispatch_request: CapturedPaperDispatchRequest
    initial_state: str
    correlation_id: str
    variant_id: int
    session_updated_at: datetime
    decision_at: datetime
    evidence_available_at: datetime
    evidence_expires_at: datetime
    risk_snapshot_sha256: str
    viability_payload_sha256: str
    variant_payload_sha256: str
    confirmed_arm_marker_sha256: str
    observation_decision_id: str
    observation_generation_sha256: str
    context_sha256: str = field(init=False)
    schema_version: str = CAPTURED_PAPER_OBSERVATION_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version
            != CAPTURED_PAPER_OBSERVATION_CONTEXT_SCHEMA_VERSION
            or type(self.dispatch_request) is not CapturedPaperDispatchRequest
        ):
            raise CapturedPaperSelectionContextError(
                "captured_paper_observation_context_invalid"
            )
        self.dispatch_request.verify()
        state = str(self.initial_state or "").strip().lower()
        correlation = str(self.correlation_id or "").strip()
        if state not in _OBSERVATION_STATES or not correlation:
            raise CapturedPaperSelectionContextError(
                "captured_paper_observation_state_invalid"
            )
        if isinstance(self.variant_id, bool) or int(self.variant_id) <= 0:
            raise CapturedPaperSelectionContextError(
                "captured_paper_observation_variant_invalid"
            )
        updated = _aware_utc(
            self.session_updated_at,
            field_name="captured_paper_observation_session_updated_at",
        )
        decision = _aware_utc(
            self.decision_at,
            field_name="captured_paper_observation_decision_at",
        )
        available = _aware_utc(
            self.evidence_available_at,
            field_name="captured_paper_observation_available_at",
        )
        expires = _aware_utc(
            self.evidence_expires_at,
            field_name="captured_paper_observation_expires_at",
        )
        if not available <= decision <= expires:
            raise CapturedPaperSelectionContextError(
                "captured_paper_observation_clock_invalid"
            )
        generation = _sha256(
            self.observation_generation_sha256,
            field_name="captured_paper_observation_generation_sha256",
        )
        bound_hashes = {
            name: _sha256(getattr(self, name), field_name=name)
            for name in (
                "risk_snapshot_sha256",
                "viability_payload_sha256",
                "variant_payload_sha256",
                "confirmed_arm_marker_sha256",
            )
        }
        expected_generation = captured_paper_observation_generation_sha256(
            session_id=self.dispatch_request.session_id,
            symbol=self.dispatch_request.symbol,
            execution_family=self.dispatch_request.execution_family,
            state=state,
            correlation_id=correlation,
            variant_id=int(self.variant_id),
            session_updated_at=updated,
            **bound_hashes,
        )
        decision_id = str(self.observation_decision_id or "").strip()
        expected_decision_id = (
            f"captured-paper-observe-{self.dispatch_request.session_id}-"
            f"{generation[:24]}"
        )
        if generation != expected_generation or decision_id != expected_decision_id:
            raise CapturedPaperSelectionContextError(
                "captured_paper_observation_generation_mismatch"
            )
        object.__setattr__(self, "initial_state", state)
        object.__setattr__(self, "correlation_id", correlation)
        object.__setattr__(self, "variant_id", int(self.variant_id))
        object.__setattr__(self, "session_updated_at", updated)
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "evidence_available_at", available)
        object.__setattr__(self, "evidence_expires_at", expires)
        for name, value in bound_hashes.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "observation_decision_id", decision_id)
        object.__setattr__(self, "observation_generation_sha256", generation)
        object.__setattr__(self, "context_sha256", _sha256_json(self._body()))

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dispatch_provenance_sha256": self.dispatch_request.provenance_sha256,
            "initial_state": self.initial_state,
            "correlation_id": self.correlation_id,
            "variant_id": self.variant_id,
            "session_updated_at": self.session_updated_at.isoformat(),
            "decision_at": self.decision_at.isoformat(),
            "evidence_available_at": self.evidence_available_at.isoformat(),
            "evidence_expires_at": self.evidence_expires_at.isoformat(),
            "risk_snapshot_sha256": self.risk_snapshot_sha256,
            "viability_payload_sha256": self.viability_payload_sha256,
            "variant_payload_sha256": self.variant_payload_sha256,
            "confirmed_arm_marker_sha256": (
                self.confirmed_arm_marker_sha256
            ),
            "observation_decision_id": self.observation_decision_id,
            "observation_generation_sha256": (
                self.observation_generation_sha256
            ),
        }

    def verify(self) -> None:
        self.dispatch_request.verify()
        if (
            captured_paper_observation_generation_sha256(
                session_id=self.dispatch_request.session_id,
                symbol=self.dispatch_request.symbol,
                execution_family=self.dispatch_request.execution_family,
                state=self.initial_state,
                correlation_id=self.correlation_id,
                variant_id=self.variant_id,
                session_updated_at=self.session_updated_at,
                risk_snapshot_sha256=self.risk_snapshot_sha256,
                viability_payload_sha256=self.viability_payload_sha256,
                variant_payload_sha256=self.variant_payload_sha256,
                confirmed_arm_marker_sha256=(
                    self.confirmed_arm_marker_sha256
                ),
            )
            != self.observation_generation_sha256
            or _sha256_json(self._body()) != self.context_sha256
        ):
            raise CapturedPaperSelectionContextError(
                "captured_paper_observation_context_mutated"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperObservationResolution:
    active: bool
    permitted: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.active and (self.permitted or self.reason is not None):
            raise ValueError("inactive captured observation cannot carry a result")
        if self.active and (self.permitted == (self.reason is not None)):
            raise ValueError(
                "active captured observation needs exactly one result shape"
            )


_ACTIVE_SELECTION_CONTEXT: contextvars.ContextVar[
    CapturedPaperSelectionContext | None
] = contextvars.ContextVar("_chili_captured_paper_selection", default=None)
_ACTIVE_OBSERVATION_CONTEXT: contextvars.ContextVar[
    tuple[CapturedPaperObservationContext, int] | None
] = contextvars.ContextVar("_chili_captured_paper_observation", default=None)
_REQUIRED_DISPATCH_REQUEST: contextvars.ContextVar[
    CapturedPaperDispatchRequest | None
] = contextvars.ContextVar(
    "_chili_captured_paper_selection_required",
    default=None,
)


@contextmanager
def require_captured_paper_selection(
    dispatch_request: CapturedPaperDispatchRequest,
) -> Iterator[CapturedPaperDispatchRequest]:
    """Fence one registered captured-runtime callback from legacy entry code."""

    if type(dispatch_request) is not CapturedPaperDispatchRequest:
        raise CapturedPaperSelectionContextError(
            "captured_paper_required_dispatch_invalid"
        )
    dispatch_request.verify()
    if _REQUIRED_DISPATCH_REQUEST.get() is not None:
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_requirement_already_active"
        )
    token = _REQUIRED_DISPATCH_REQUEST.set(dispatch_request)
    try:
        yield dispatch_request
    finally:
        _REQUIRED_DISPATCH_REQUEST.reset(token)


@contextmanager
def install_captured_paper_selection_context(
    context: CapturedPaperSelectionContext,
) -> Iterator[CapturedPaperSelectionContext]:
    """Bind one capture-owned expectation around one same-process FSM call."""

    if type(context) is not CapturedPaperSelectionContext:
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_context_invalid"
        )
    context.verify()
    required = _REQUIRED_DISPATCH_REQUEST.get()
    if required is None:
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_requirement_missing"
        )
    required.verify()
    if (
        required.provenance_sha256
        != context.dispatch_request.provenance_sha256
        or required.route_token.route_token_sha256
        != context.dispatch_request.route_token.route_token_sha256
    ):
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_required_route_mismatch"
        )
    if _ACTIVE_SELECTION_CONTEXT.get() is not None:
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_context_already_active"
        )
    if _ACTIVE_OBSERVATION_CONTEXT.get() is not None:
        raise CapturedPaperSelectionContextError(
            "captured_paper_observation_context_already_active"
        )
    token = _ACTIVE_SELECTION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _ACTIVE_SELECTION_CONTEXT.reset(token)


@contextmanager
def install_captured_paper_observation_context(
    context: CapturedPaperObservationContext,
) -> Iterator[CapturedPaperObservationContext]:
    """Permit one exact WATCHING/QUEUED detector tick, never admission."""

    if type(context) is not CapturedPaperObservationContext:
        raise CapturedPaperSelectionContextError(
            "captured_paper_observation_context_invalid"
        )
    context.verify()
    required = _REQUIRED_DISPATCH_REQUEST.get()
    if required is None:
        raise CapturedPaperSelectionContextError(
            "captured_paper_selection_requirement_missing"
        )
    required.verify()
    if (
        required.provenance_sha256
        != context.dispatch_request.provenance_sha256
        or required.route_token.route_token_sha256
        != context.dispatch_request.route_token.route_token_sha256
    ):
        raise CapturedPaperSelectionContextError(
            "captured_paper_observation_required_route_mismatch"
        )
    if (
        _ACTIVE_SELECTION_CONTEXT.get() is not None
        or _ACTIVE_OBSERVATION_CONTEXT.get() is not None
    ):
        raise CapturedPaperSelectionContextError(
            "captured_paper_observation_context_already_active"
        )
    token = _ACTIVE_OBSERVATION_CONTEXT.set(
        (context, threading.get_ident())
    )
    try:
        yield context
    finally:
        _ACTIVE_OBSERVATION_CONTEXT.reset(token)


def _unavailable(reason: str) -> CapturedPaperSelectionResolution:
    return CapturedPaperSelectionResolution(active=True, reason=reason)


def captured_paper_selection_context_active(*, execution_family: Any) -> bool:
    """Cheap guard so an ordinary tick does not compute any captured inputs."""

    return (
        str(execution_family or "").strip().lower() == "alpaca_spot"
        and _ACTIVE_SELECTION_CONTEXT.get() is not None
    )


def captured_paper_observation_context_active(*, execution_family: Any) -> bool:
    """Whether this exact thread owns a capture-only watcher capability."""

    active = _ACTIVE_OBSERVATION_CONTEXT.get()
    return (
        str(execution_family or "").strip().lower() == "alpaca_spot"
        and active is not None
        and active[1] == threading.get_ident()
    )


def captured_paper_selection_required(*, execution_family: Any) -> bool:
    """Whether the dispatcher fenced this Alpaca tick from legacy entry code."""

    return (
        str(execution_family or "").strip().lower() == "alpaca_spot"
        and _REQUIRED_DISPATCH_REQUEST.get() is not None
    )


def resolve_captured_paper_observation(
    *,
    session_id: Any,
    symbol: Any,
    execution_family: Any,
    state: Any,
    correlation_id: Any,
    variant_id: Any,
    session_updated_at: datetime,
    decision_at: datetime,
    risk_snapshot_sha256: Any,
    viability_payload_sha256: Any,
    variant_payload_sha256: Any,
    confirmed_arm_marker_sha256: Any,
) -> CapturedPaperObservationResolution:
    """Revalidate the exact watcher generation under the FSM's final row lock."""

    if str(execution_family or "").strip().lower() != "alpaca_spot":
        return CapturedPaperObservationResolution(active=False)
    active = _ACTIVE_OBSERVATION_CONTEXT.get()
    if active is None or active[1] != threading.get_ident():
        return CapturedPaperObservationResolution(active=False)
    context = active[0]
    try:
        required = _REQUIRED_DISPATCH_REQUEST.get()
        if required is None:
            return CapturedPaperObservationResolution(
                active=True,
                reason="captured_paper_selection_requirement_missing",
            )
        required.verify()
        context.verify()
        if (
            required.provenance_sha256
            != context.dispatch_request.provenance_sha256
            or required.route_token.route_token_sha256
            != context.dispatch_request.route_token.route_token_sha256
        ):
            return CapturedPaperObservationResolution(
                active=True,
                reason="captured_paper_observation_required_route_mismatch",
            )
        try:
            sid = int(session_id)
            current_variant = int(variant_id)
        except (TypeError, ValueError, OverflowError):
            return CapturedPaperObservationResolution(
                active=True,
                reason="captured_paper_observation_route_mismatch",
            )
        exact = {
            "session_id": (sid, context.dispatch_request.session_id),
            "symbol": (
                str(symbol or "").strip().upper(),
                context.dispatch_request.symbol,
            ),
            "execution_family": (
                str(execution_family or "").strip().lower(),
                context.dispatch_request.execution_family,
            ),
            "state": (str(state or "").strip().lower(), context.initial_state),
            "correlation_id": (
                str(correlation_id or "").strip(),
                context.correlation_id,
            ),
            "variant_id": (current_variant, context.variant_id),
        }
        if any(actual != expected for actual, expected in exact.values()):
            return CapturedPaperObservationResolution(
                active=True,
                reason="captured_paper_observation_route_or_state_drift",
            )
        generation = captured_paper_observation_generation_sha256(
            session_id=sid,
            symbol=symbol,
            execution_family=execution_family,
            state=state,
            correlation_id=correlation_id,
            variant_id=current_variant,
            session_updated_at=session_updated_at,
            risk_snapshot_sha256=risk_snapshot_sha256,
            viability_payload_sha256=viability_payload_sha256,
            variant_payload_sha256=variant_payload_sha256,
            confirmed_arm_marker_sha256=confirmed_arm_marker_sha256,
        )
        if generation != context.observation_generation_sha256:
            return CapturedPaperObservationResolution(
                active=True,
                reason="captured_paper_observation_generation_mismatch",
            )
        current_decision = _aware_utc(
            decision_at,
            field_name="captured_paper_observation_decision_at",
        )
        if (
            current_decision != context.decision_at
            or not context.evidence_available_at
            <= current_decision
            <= context.evidence_expires_at
        ):
            return CapturedPaperObservationResolution(
                active=True,
                reason="captured_paper_observation_decision_clock_mismatch",
            )
        return CapturedPaperObservationResolution(active=True, permitted=True)
    except (CapturedPaperDispatchError, CapturedPaperSelectionContextError):
        return CapturedPaperObservationResolution(
            active=True,
            reason="captured_paper_observation_evidence_invalid",
        )


def _opportunity_matches_trigger(
    opportunity: CapturedPaperOpportunityKey,
    trigger_debug: Mapping[str, Any],
) -> bool:
    raw = trigger_debug.get("opportunity_key")
    if type(raw) is not dict:
        return False
    return raw == {
        "symbol": opportunity.symbol,
        "trading_date": opportunity.trading_date.isoformat(),
        "setup_family": opportunity.setup_family,
    }


def resolve_captured_paper_selection(
    *,
    session_id: Any,
    symbol: Any,
    execution_family: Any,
    decision_at: datetime,
    bid: Any,
    ask: Any,
    structural_stop_price: Any,
    entry_limit_ceiling_price: Any,
    entry_place_count: Any,
    client_order_id: Any,
    setup_family: Any,
    trigger_reason: Any,
    trigger_debug: Mapping[str, Any],
    confirmed_arm_marker: Mapping[str, Any],
    candidate_generation_sha256: Any,
    viability_updated_at: datetime,
    viability_score: Any,
    viability_payload_sha256: Any,
    execution_readiness_sha256: Any,
) -> CapturedPaperSelectionResolution:
    """Match already-observed FSM values to the installed captured draft."""

    # A capture context must never perturb another execution family.  Inactive
    # operation is byte-for-byte ordinary runner behavior.
    if str(execution_family or "").strip().lower() != "alpaca_spot":
        return CapturedPaperSelectionResolution(active=False)
    context = _ACTIVE_SELECTION_CONTEXT.get()
    if context is None:
        return CapturedPaperSelectionResolution(active=False)
    try:
        required = _REQUIRED_DISPATCH_REQUEST.get()
        if required is None:
            return _unavailable("captured_paper_selection_requirement_missing")
        required.verify()
        if (
            required.provenance_sha256
            != context.dispatch_request.provenance_sha256
            or required.route_token.route_token_sha256
            != context.dispatch_request.route_token.route_token_sha256
        ):
            return _unavailable(
                "captured_paper_selection_required_route_mismatch"
            )
        context.verify()
        intent = context.draft.intent
        route = context.dispatch_request.route_token
        now = _aware_utc(decision_at, field_name="captured_paper_decision_at")
        if not (context.evidence_available_at <= now <= context.evidence_expires_at):
            return _unavailable("captured_paper_selection_evidence_stale")
        if now != intent.decision_at:
            return _unavailable("captured_paper_selection_decision_clock_mismatch")
        try:
            if isinstance(session_id, bool):
                raise ValueError("boolean session id")
            sid = int(session_id)
        except (TypeError, ValueError):
            return _unavailable("captured_paper_selection_session_id_invalid")
        exact_route = {
            "session_id": (sid, route.session_id),
            "symbol": (str(symbol or "").strip().upper(), route.symbol),
            "execution_family": (
                str(execution_family or "").strip().lower(),
                route.execution_family,
            ),
        }
        if any(actual != expected for actual, expected in exact_route.values()):
            return _unavailable("captured_paper_selection_route_mismatch")
        try:
            if isinstance(entry_place_count, bool):
                raise ValueError("boolean place count")
            current_place_count = int(entry_place_count)
        except (TypeError, ValueError):
            return _unavailable("captured_paper_entry_place_generation_mismatch")
        if current_place_count != context.entry_place_count:
            return _unavailable("captured_paper_entry_place_generation_mismatch")
        if str(client_order_id or "") != intent.client_order_id:
            return _unavailable("captured_paper_client_order_id_mismatch")
        if str(setup_family or "").strip().lower() != intent.setup_family:
            return _unavailable("captured_paper_setup_family_mismatch")
        current_bid = _positive_decimal(bid, field_name="captured_paper_bid")
        current_ask = _positive_decimal(ask, field_name="captured_paper_ask")
        current_stop = _positive_decimal(
            structural_stop_price,
            field_name="captured_paper_structural_stop",
        )
        current_ceiling = _positive_decimal(
            entry_limit_ceiling_price,
            field_name="captured_paper_entry_limit_ceiling",
        )
        if current_bid != context.expected_bid or current_ask != context.expected_ask:
            return _unavailable("captured_paper_selection_bbo_mismatch")
        if (
            current_stop != intent.structural_stop_price
            or current_ceiling != intent.entry_limit_ceiling_price
        ):
            return _unavailable("captured_paper_selection_price_mismatch")
        if str(trigger_reason or "") != context.trigger_reason:
            return _unavailable("captured_paper_trigger_reason_mismatch")
        trigger_sha256 = captured_paper_trigger_snapshot_sha256(
            trigger_reason=str(trigger_reason),
            trigger_debug=trigger_debug,
        )
        if trigger_sha256 != context.trigger_snapshot_sha256:
            return _unavailable("captured_paper_trigger_snapshot_mismatch")
        _verify_arm_marker_semantics(
            confirmed_arm_marker,
            intent.confirmed_arm_generation,
        )
        if _arm_marker_sha256(confirmed_arm_marker) != context.confirmed_arm_marker_sha256:
            return _unavailable("captured_paper_confirmed_arm_marker_mismatch")
        current_candidate_sha256 = captured_paper_candidate_generation_sha256(
            session_id=sid,
            symbol=symbol,
            execution_family=execution_family,
            entry_place_count=current_place_count,
            client_order_id=client_order_id,
            setup_family=setup_family,
            structural_stop_price=structural_stop_price,
            trigger_reason=trigger_reason,
            trigger_debug=trigger_debug,
            confirmed_arm_marker=confirmed_arm_marker,
            viability_updated_at=viability_updated_at,
            viability_score=viability_score,
            viability_payload_sha256=viability_payload_sha256,
            execution_readiness_sha256=execution_readiness_sha256,
        )
        if (
            current_candidate_sha256 != context.candidate_generation_sha256
            or str(candidate_generation_sha256 or "")
            != context.candidate_generation_sha256
        ):
            return _unavailable(
                "captured_paper_candidate_generation_mismatch"
            )
        opportunity = intent.opportunity_key
        if intent.setup_family == _FIRST_DIP_SETUP_FAMILY:
            if (
                type(opportunity) is not CapturedPaperOpportunityKey
                or trigger_debug.get("first_dip_tape_confirmed") is not True
                or trigger_debug.get("first_dip_tape_run_bound") is not True
                or trigger_debug.get(
                    "first_dip_tape_decision_receipt_binding_sha256"
                )
                != intent.setup_evidence_sha256
                or not _opportunity_matches_trigger(opportunity, trigger_debug)
            ):
                return _unavailable(
                    "captured_paper_first_dip_selection_evidence_mismatch"
                )
        elif opportunity is not None:
            return _unavailable(
                "captured_paper_non_first_dip_opportunity_prohibited"
            )
        # A canonical round-trip detects hidden object mutation before the same
        # draft object crosses the post-commit dispatcher boundary.
        exact = CapturedPaperPostCommitRequest.from_canonical_json(
            context.draft.to_canonical_json()
        )
        if (
            exact.to_canonical_json() != context.draft.to_canonical_json()
            or exact.completion_sha256 != context.draft.completion_sha256
        ):
            return _unavailable("captured_paper_selection_draft_mismatch")
        context.draft.verify()
        return CapturedPaperSelectionResolution(
            active=True,
            request=context.draft,
        )
    except (
        CapturedPaperDispatchError,
        CapturedPaperIntentContractError,
        CapturedPaperSelectionContextError,
    ):
        return _unavailable("captured_paper_selection_evidence_invalid")


__all__ = (
    "CAPTURED_PAPER_OBSERVATION_CONTEXT_SCHEMA_VERSION",
    "CAPTURED_PAPER_SELECTION_CONTEXT_SCHEMA_VERSION",
    "CAPTURED_PAPER_TRIGGER_SNAPSHOT_SCHEMA_VERSION",
    "CapturedPaperObservationContext",
    "CapturedPaperObservationResolution",
    "CapturedPaperSelectionContext",
    "CapturedPaperSelectionContextError",
    "CapturedPaperSelectionResolution",
    "captured_paper_candidate_generation_sha256",
    "captured_paper_observation_context_active",
    "captured_paper_observation_generation_sha256",
    "captured_paper_selection_context_active",
    "captured_paper_selection_required",
    "captured_paper_trigger_snapshot_sha256",
    "install_captured_paper_observation_context",
    "install_captured_paper_selection_context",
    "require_captured_paper_selection",
    "resolve_captured_paper_observation",
    "resolve_captured_paper_selection",
)
