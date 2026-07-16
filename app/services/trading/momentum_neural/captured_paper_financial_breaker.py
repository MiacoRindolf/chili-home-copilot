"""Typed financial-breaker authority for captured Alpaca PAPER entries.

The captured PAPER entry lane deliberately commits selection and transport in
separate transactions.  This module carries the existing final Alpaca
financial-breaker decision across that boundary without turning an earlier
healthy observation into permanent POST authority.

The concrete issuer performs its broker/DB reads outside every admission or
outbox lock walk.  Callers must verify the short-lived, content-addressed
receipt again at the exact boundary where it is consumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ....models.trading import TradingAutomationSession
from .captured_paper_entry_intent import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    CapturedPaperPostCommitRequest,
)


UTC = timezone.utc
CAPTURED_PAPER_FINANCIAL_BREAKER_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-financial-breaker-receipt.v1"
)
CAPTURED_PAPER_FINANCIAL_BREAKER_EVALUATOR_ID = (
    "app.services.trading.momentum_neural.live_runner:"
    "_final_alpaca_financial_breaker_admission"
)
_PHASES = frozenset({"pre_reservation", "pre_post"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CapturedPaperFinancialBreakerError(RuntimeError):
    """The final financial-breaker truth is unavailable or mismatched."""

    def __init__(self, reason: str):
        self.reason = str(
            reason or "captured_paper_financial_breaker_unavailable"
        )
        super().__init__(self.reason)


def _utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise CapturedPaperFinancialBreakerError(f"{field_name}_invalid")
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if aware.utcoffset() != timedelta(0):
        raise CapturedPaperFinancialBreakerError(f"{field_name}_invalid")
    return aware


def _sha(value: Any, *, field_name: str) -> str:
    digest = str(value or "").strip()
    if _SHA256_RE.fullmatch(digest) is None:
        raise CapturedPaperFinancialBreakerError(f"{field_name}_invalid")
    return digest


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, datetime):
        return _utc(value, field_name="breaker_evidence_clock").isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_evidence_nonfinite"
            )
        return value
    raise CapturedPaperFinancialBreakerError(
        "financial_breaker_evidence_not_json_safe"
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            _canonical_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CapturedPaperFinancialBreakerError(
            "financial_breaker_evidence_not_canonical"
        ) from exc


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CapturedPaperFinancialBreakerReceipt:
    """Short-lived evidence for one exact entry financial-breaker phase."""

    phase: str
    completion_sha256: str
    route_token_sha256: str
    intent_sha256: str
    session_id: int
    symbol: str
    execution_family: str
    account_scope: str
    expected_account_id: str
    code_build_sha256: str
    config_sha256: str
    feature_flags_sha256: str
    policy_sha256: str
    runtime_generation: str
    intent_generation: str
    completion_generation: str
    decision_id: str
    capture_receipt_sha256: str
    checked_at: datetime
    issued_at: datetime
    valid_until: datetime
    allowed: bool
    blocker: str | None
    reason: str | None
    breaker_evidence: Mapping[str, Any]
    transport_instruction_sha256: str | None = None
    transport_invocation_authority_sha256: str | None = None
    evaluator_id: str = CAPTURED_PAPER_FINANCIAL_BREAKER_EVALUATOR_ID
    breaker_evidence_sha256: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        phase = str(self.phase or "").strip().lower()
        if phase not in _PHASES or phase != self.phase:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_phase_invalid"
            )
        object.__setattr__(self, "phase", phase)
        if isinstance(self.session_id, bool) or int(self.session_id) <= 0:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_session_id_invalid"
            )
        object.__setattr__(self, "session_id", int(self.session_id))
        symbol = str(self.symbol or "").strip().upper()
        if not symbol or symbol != self.symbol:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_symbol_invalid"
            )
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_account_scope_invalid"
            )
        if not str(self.expected_account_id or "").strip():
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_account_id_invalid"
            )
        for name in (
            "completion_sha256",
            "route_token_sha256",
            "intent_sha256",
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "policy_sha256",
            "capture_receipt_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _sha(getattr(self, name), field_name=name),
            )
        for name in (
            "transport_instruction_sha256",
            "transport_invocation_authority_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _sha(value, field_name=name),
                )
        has_transport_binding = self.transport_instruction_sha256 is not None
        if has_transport_binding != (
            self.transport_invocation_authority_sha256 is not None
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_transport_binding_partial"
            )
        if (phase == "pre_post") != has_transport_binding:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_transport_binding_phase_mismatch"
            )
        for name in (
            "runtime_generation",
            "intent_generation",
            "completion_generation",
            "decision_id",
            "execution_family",
        ):
            if not str(getattr(self, name) or "").strip():
                raise CapturedPaperFinancialBreakerError(
                    f"financial_breaker_{name}_invalid"
                )
        if self.evaluator_id != CAPTURED_PAPER_FINANCIAL_BREAKER_EVALUATOR_ID:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_evaluator_identity_invalid"
            )
        checked = _utc(self.checked_at, field_name="financial_breaker_checked_at")
        issued = _utc(self.issued_at, field_name="financial_breaker_issued_at")
        valid_until = _utc(
            self.valid_until,
            field_name="financial_breaker_valid_until",
        )
        if (
            checked > issued
            or issued - checked > timedelta(seconds=10)
            or not issued < valid_until
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_clock_order_invalid"
            )
        if valid_until - issued > timedelta(seconds=10):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_validity_window_invalid"
            )
        object.__setattr__(self, "checked_at", checked)
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "valid_until", valid_until)
        if type(self.allowed) is not bool:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_allowed_invalid"
            )
        blocker = str(self.blocker or "").strip() or None
        reason = str(self.reason or "").strip() or None
        if self.allowed and (blocker is not None or reason is not None):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_allowed_payload_invalid"
            )
        if not self.allowed and (blocker is None or reason is None):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_denial_payload_invalid"
            )
        object.__setattr__(self, "blocker", blocker)
        object.__setattr__(self, "reason", reason)
        evidence = _canonical_json_value(self.breaker_evidence)
        if not isinstance(evidence, dict):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_evidence_invalid"
            )
        if not (
            evidence.get("schema_version")
            == "chili.alpaca-final-breaker-admission.v1"
            and evidence.get("phase") == phase
            and evidence.get("execution_family") == self.execution_family
            and evidence.get("allowed") is self.allowed
            and (str(evidence.get("breaker") or "").strip() or None) == blocker
            and (str(evidence.get("reason") or "").strip() or None) == reason
            and evidence.get("checked_at_utc") == checked.isoformat()
            and isinstance(evidence.get("checks"), list)
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_evidence_binding_mismatch"
            )
        object.__setattr__(self, "breaker_evidence", evidence)
        object.__setattr__(
            self,
            "breaker_evidence_sha256",
            _sha256_json(evidence),
        )
        object.__setattr__(self, "receipt_sha256", _sha256_json(self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": (
                CAPTURED_PAPER_FINANCIAL_BREAKER_RECEIPT_SCHEMA_VERSION
            ),
            "phase": self.phase,
            "completion_sha256": self.completion_sha256,
            "route_token_sha256": self.route_token_sha256,
            "intent_sha256": self.intent_sha256,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "execution_family": self.execution_family,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "policy_sha256": self.policy_sha256,
            "runtime_generation": self.runtime_generation,
            "intent_generation": self.intent_generation,
            "completion_generation": self.completion_generation,
            "decision_id": self.decision_id,
            "capture_receipt_sha256": self.capture_receipt_sha256,
            "checked_at": self.checked_at.isoformat(),
            "issued_at": self.issued_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "allowed": self.allowed,
            "blocker": self.blocker,
            "reason": self.reason,
            "breaker_evidence": dict(self.breaker_evidence),
            "breaker_evidence_sha256": self.breaker_evidence_sha256,
            "transport_instruction_sha256": self.transport_instruction_sha256,
            "transport_invocation_authority_sha256": (
                self.transport_invocation_authority_sha256
            ),
            "evaluator_id": self.evaluator_id,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "CapturedPaperFinancialBreakerReceipt":
        if not isinstance(payload, Mapping):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_receipt_payload_invalid"
            )
        data = dict(payload)
        expected_keys = {
            "schema_version",
            "phase",
            "completion_sha256",
            "route_token_sha256",
            "intent_sha256",
            "session_id",
            "symbol",
            "execution_family",
            "account_scope",
            "expected_account_id",
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "policy_sha256",
            "runtime_generation",
            "intent_generation",
            "completion_generation",
            "decision_id",
            "capture_receipt_sha256",
            "checked_at",
            "issued_at",
            "valid_until",
            "allowed",
            "blocker",
            "reason",
            "breaker_evidence",
            "breaker_evidence_sha256",
            "transport_instruction_sha256",
            "transport_invocation_authority_sha256",
            "evaluator_id",
            "receipt_sha256",
        }
        if set(data) != expected_keys or data.get("schema_version") != (
            CAPTURED_PAPER_FINANCIAL_BREAKER_RECEIPT_SCHEMA_VERSION
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_receipt_payload_shape_invalid"
            )
        try:
            value = cls(
                phase=data["phase"],
                completion_sha256=data["completion_sha256"],
                route_token_sha256=data["route_token_sha256"],
                intent_sha256=data["intent_sha256"],
                session_id=data["session_id"],
                symbol=data["symbol"],
                execution_family=data["execution_family"],
                account_scope=data["account_scope"],
                expected_account_id=data["expected_account_id"],
                code_build_sha256=data["code_build_sha256"],
                config_sha256=data["config_sha256"],
                feature_flags_sha256=data["feature_flags_sha256"],
                policy_sha256=data["policy_sha256"],
                runtime_generation=data["runtime_generation"],
                intent_generation=data["intent_generation"],
                completion_generation=data["completion_generation"],
                decision_id=data["decision_id"],
                capture_receipt_sha256=data["capture_receipt_sha256"],
                checked_at=datetime.fromisoformat(data["checked_at"]),
                issued_at=datetime.fromisoformat(data["issued_at"]),
                valid_until=datetime.fromisoformat(data["valid_until"]),
                allowed=data["allowed"],
                blocker=data["blocker"],
                reason=data["reason"],
                breaker_evidence=data["breaker_evidence"],
                transport_instruction_sha256=(
                    data["transport_instruction_sha256"]
                ),
                transport_invocation_authority_sha256=(
                    data["transport_invocation_authority_sha256"]
                ),
                evaluator_id=data["evaluator_id"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CapturedPaperFinancialBreakerError):
                raise
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_receipt_payload_invalid"
            ) from exc
        if not (
            _sha(
                data["breaker_evidence_sha256"],
                field_name="breaker_evidence_sha256",
            )
            == value.breaker_evidence_sha256
            and _sha(data["receipt_sha256"], field_name="receipt_sha256")
            == value.receipt_sha256
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_receipt_payload_hash_mismatch"
            )
        return value

    def verify_for_request(
        self,
        request: CapturedPaperPostCommitRequest,
        *,
        phase: str,
        now: datetime,
        require_allowed: bool = True,
        transport_instruction_sha256: str | None = None,
        transport_invocation_authority_sha256: str | None = None,
    ) -> None:
        if type(request) is not CapturedPaperPostCommitRequest:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_request_invalid"
            )
        request.verify()
        route = request.route_token
        intent = request.intent
        expected = {
            "phase": str(phase or "").strip().lower(),
            "completion_sha256": request.completion_sha256,
            "route_token_sha256": route.route_token_sha256,
            "intent_sha256": intent.intent_sha256,
            "session_id": route.session_id,
            "symbol": route.symbol,
            "execution_family": route.execution_family,
            "account_scope": route.account_scope,
            "expected_account_id": route.expected_account_id,
            "code_build_sha256": route.code_build_sha256,
            "config_sha256": route.config_sha256,
            "feature_flags_sha256": intent.feature_flags_sha256,
            "policy_sha256": intent.policy_sha256,
            "runtime_generation": route.runtime_generation,
            "intent_generation": intent.intent_generation,
            "completion_generation": request.completion_generation,
            "decision_id": intent.decision_id,
            "capture_receipt_sha256": route.capture_receipt_sha256,
            "transport_instruction_sha256": transport_instruction_sha256,
            "transport_invocation_authority_sha256": (
                transport_invocation_authority_sha256
            ),
        }
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_request_binding_mismatch"
            )
        verified_now = _utc(now, field_name="financial_breaker_verification_at")
        if not (
            intent.decision_at <= self.checked_at <= self.issued_at
            and self.issued_at <= verified_now < self.valid_until
            and verified_now <= intent.confirmed_arm_generation.expires_at
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_receipt_stale_or_future"
            )
        canonical = CapturedPaperFinancialBreakerReceipt(
            phase=self.phase,
            completion_sha256=self.completion_sha256,
            route_token_sha256=self.route_token_sha256,
            intent_sha256=self.intent_sha256,
            session_id=self.session_id,
            symbol=self.symbol,
            execution_family=self.execution_family,
            account_scope=self.account_scope,
            expected_account_id=self.expected_account_id,
            code_build_sha256=self.code_build_sha256,
            config_sha256=self.config_sha256,
            feature_flags_sha256=self.feature_flags_sha256,
            policy_sha256=self.policy_sha256,
            runtime_generation=self.runtime_generation,
            intent_generation=self.intent_generation,
            completion_generation=self.completion_generation,
            decision_id=self.decision_id,
            capture_receipt_sha256=self.capture_receipt_sha256,
            checked_at=self.checked_at,
            issued_at=self.issued_at,
            valid_until=self.valid_until,
            allowed=self.allowed,
            blocker=self.blocker,
            reason=self.reason,
            breaker_evidence=self.breaker_evidence,
            transport_instruction_sha256=self.transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                self.transport_invocation_authority_sha256
            ),
            evaluator_id=self.evaluator_id,
        )
        if canonical.receipt_sha256 != self.receipt_sha256:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_receipt_hash_mismatch"
            )
        if require_allowed and not self.allowed:
            raise CapturedPaperFinancialBreakerError(
                self.reason or "financial_breaker_denied"
            )


class SqlAlchemyCapturedPaperFinancialBreakerIssuer:
    """Issue exact breaker receipts without holding an admission/outbox lock."""

    def __init__(
        self,
        bind: Engine,
        *,
        observation_clock: Callable[[], datetime],
        validity_seconds: float = 5.0,
        evaluator: Callable[..., tuple[bool, dict[str, Any]]] | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not callable(observation_clock):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_clock_invalid"
            )
        try:
            validity = float(validity_seconds)
        except (TypeError, ValueError) as exc:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_validity_invalid"
            ) from exc
        if not math.isfinite(validity) or validity <= 0.0 or validity > 10.0:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_validity_invalid"
            )
        if evaluator is not None and not callable(evaluator):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_evaluator_invalid"
            )
        self._clock = observation_clock
        self._validity_seconds = validity
        self._evaluator = evaluator
        self._session_factory = session_factory or sessionmaker(
            bind=bind,
            autoflush=False,
            expire_on_commit=False,
        )

    def _now(self) -> datetime:
        return _utc(self._clock(), field_name="financial_breaker_issuer_clock")

    def _evaluate(
        self,
        request: CapturedPaperPostCommitRequest,
        *,
        phase: str,
    ) -> tuple[bool, dict[str, Any]]:
        db = self._session_factory()
        try:
            row = (
                db.query(TradingAutomationSession)
                .populate_existing()
                .filter(
                    TradingAutomationSession.id == request.route_token.session_id,
                    TradingAutomationSession.mode == "live",
                )
                .one_or_none()
            )
            if row is None:
                raise CapturedPaperFinancialBreakerError(
                    "financial_breaker_session_unavailable"
                )
            snapshot = (
                row.risk_snapshot_json
                if isinstance(row.risk_snapshot_json, dict)
                else {}
            )
            route = request.route_token
            if not (
                str(row.symbol or "").strip().upper() == route.symbol
                and str(row.execution_family or "").strip().lower()
                == route.execution_family
                and snapshot.get("alpaca_account_scope") == route.account_scope
                and str(snapshot.get("alpaca_account_id") or "").strip()
                == route.expected_account_id
            ):
                raise CapturedPaperFinancialBreakerError(
                    "financial_breaker_session_route_mismatch"
                )
            evaluator = self._evaluator
            if evaluator is None:
                from .live_runner import (
                    _final_alpaca_financial_breaker_admission,
                )

                evaluator = _final_alpaca_financial_breaker_admission
            raw_allowed, raw_evidence = evaluator(row, phase=phase)
            if type(raw_allowed) is not bool or not isinstance(
                raw_evidence, Mapping
            ):
                raise CapturedPaperFinancialBreakerError(
                    "financial_breaker_evaluator_result_invalid"
                )
            return raw_allowed, dict(raw_evidence)
        finally:
            try:
                db.rollback()
            finally:
                db.close()

    def issue_for_request(
        self,
        request: CapturedPaperPostCommitRequest,
        *,
        phase: str,
        transport_instruction_sha256: str | None = None,
        transport_invocation_authority_sha256: str | None = None,
        authority_valid_until: datetime | None = None,
    ) -> CapturedPaperFinancialBreakerReceipt:
        if type(request) is not CapturedPaperPostCommitRequest:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_request_invalid"
            )
        request.verify()
        normalized_phase = str(phase or "").strip().lower()
        if normalized_phase not in _PHASES:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_phase_invalid"
            )
        has_transport = transport_instruction_sha256 is not None
        if (normalized_phase == "pre_post") != has_transport or has_transport != (
            transport_invocation_authority_sha256 is not None
            and authority_valid_until is not None
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_transport_binding_phase_mismatch"
            )
        allowed, evidence = self._evaluate(request, phase=normalized_phase)
        checked_raw = evidence.get("checked_at_utc")
        if not isinstance(checked_raw, str):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_checked_at_unavailable"
            )
        try:
            parsed_checked = datetime.fromisoformat(checked_raw)
        except ValueError as exc:
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_checked_at_invalid"
            ) from exc
        checked_at = _utc(
            parsed_checked,
            field_name="financial_breaker_checked_at",
        )
        if parsed_checked.utcoffset() != timedelta(0) or (
            checked_raw != parsed_checked.isoformat()
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_checked_at_noncanonical"
            )
        issued_at = self._now()
        if (
            checked_at > issued_at
            or issued_at - checked_at
            > timedelta(seconds=self._validity_seconds)
        ):
            raise CapturedPaperFinancialBreakerError(
                "financial_breaker_observation_stale_or_future"
            )
        route = request.route_token
        intent = request.intent
        valid_until = min(
            checked_at + timedelta(seconds=self._validity_seconds),
            intent.confirmed_arm_generation.expires_at,
            (
                _utc(
                    authority_valid_until,
                    field_name="financial_breaker_authority_valid_until",
                )
                if authority_valid_until is not None
                else intent.confirmed_arm_generation.expires_at
            ),
        )
        receipt = CapturedPaperFinancialBreakerReceipt(
            phase=normalized_phase,
            completion_sha256=request.completion_sha256,
            route_token_sha256=route.route_token_sha256,
            intent_sha256=intent.intent_sha256,
            session_id=route.session_id,
            symbol=route.symbol,
            execution_family=route.execution_family,
            account_scope=route.account_scope,
            expected_account_id=route.expected_account_id,
            code_build_sha256=route.code_build_sha256,
            config_sha256=route.config_sha256,
            feature_flags_sha256=intent.feature_flags_sha256,
            policy_sha256=intent.policy_sha256,
            runtime_generation=route.runtime_generation,
            intent_generation=intent.intent_generation,
            completion_generation=request.completion_generation,
            decision_id=intent.decision_id,
            capture_receipt_sha256=route.capture_receipt_sha256,
            checked_at=checked_at,
            issued_at=issued_at,
            valid_until=valid_until,
            allowed=allowed,
            blocker=str(evidence.get("breaker") or "").strip() or None,
            reason=str(evidence.get("reason") or "").strip() or None,
            breaker_evidence=evidence,
            transport_instruction_sha256=transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                transport_invocation_authority_sha256
            ),
        )
        receipt.verify_for_request(
            request,
            phase=normalized_phase,
            now=issued_at,
            require_allowed=False,
            transport_instruction_sha256=transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                transport_invocation_authority_sha256
            ),
        )
        return receipt


__all__ = [
    "CAPTURED_PAPER_FINANCIAL_BREAKER_EVALUATOR_ID",
    "CAPTURED_PAPER_FINANCIAL_BREAKER_RECEIPT_SCHEMA_VERSION",
    "CapturedPaperFinancialBreakerError",
    "CapturedPaperFinancialBreakerReceipt",
    "SqlAlchemyCapturedPaperFinancialBreakerIssuer",
]
