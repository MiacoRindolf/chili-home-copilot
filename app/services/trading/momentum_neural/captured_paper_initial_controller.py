"""Strict IQFeed-notify to initial captured Alpaca PAPER session controller.

This is the production composition seam for a symbol which does not yet have
an automation session.  It deliberately performs only the bounded sequence
needed to create a recoverable, non-executable PENDING_OWNER:

``strict Q -> hot IQFeed print capture -> continuity proof -> typed material
-> PREOWNER -> PENDING_OWNER``.

Missing L2 is local coverage information and never suppresses an otherwise
complete exact-print setup.  Every failure before PREOWNER aborts the newly
hot capture run; every failure after PREOWNER retains the durable generation
for the same deterministic retry.  No opportunity, adaptive reservation,
outbox, broker transport, or order can be created here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any, Callable, Mapping
import uuid

from sqlalchemy.engine import Engine

from .captured_adaptive_risk_source import CapturedAdaptiveRiskPolicySpec
from .captured_paper_dispatcher import CapturedPaperDispatchRequest
from .captured_paper_initial_admission import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    ALPACA_SPOT_EXECUTION_FAMILY,
    CapturedPaperInitialAdmissionCapability,
    CapturedPaperInitialAdmissionError,
    commit_captured_paper_initial_preowner,
)
from .captured_paper_initial_provider import (
    CaptureBackedPaperInitialSessionMaterialProvider,
    CapturedPaperInitialCandidateReadPort,
    CapturedPaperInitialProviderCoverageUnavailable,
)
from .captured_paper_initial_recovery import (
    CapturedPaperInitialRecoveryError,
    recover_captured_paper_initial_symbol,
)
from .captured_paper_iqfeed_trigger import (
    CapturedPaperIqfeedTriggerResolver,
    IqfeedTriggerStatus,
    parse_captured_paper_iqfeed_q_notify,
)
from .captured_paper_preowner_promotion import (
    CapturedPaperPreownerPromotionError,
    promote_captured_paper_preowner,
)
from .adaptive_risk_policy import AdaptiveRiskPolicySettingsReceipt
from .replay_capture_contract import (
    CaptureContractError,
    CaptureMicrostructureReadQuery,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
)


UTC = timezone.utc
_DECISION_NAMESPACE = uuid.UUID("d4044fa8-494d-49fd-8b7d-56f658a96e45")


class CapturedPaperInitialControllerError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_initial_controller_unavailable")
        super().__init__(self.reason)


def _utc(value: Any, reason: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperInitialControllerError(reason)
    try:
        if value.utcoffset() is None:
            raise CapturedPaperInitialControllerError(reason)
    except CapturedPaperInitialControllerError:
        raise
    except Exception as exc:  # pragma: no cover - defensive tzinfo boundary
        raise CapturedPaperInitialControllerError(reason) from exc
    return value.astimezone(UTC)


def _assert_service_fence(assertion: Callable[[], None] | None) -> None:
    if not callable(assertion):
        raise CapturedPaperInitialControllerError(
            "initial_controller_service_fence_unavailable"
        )
    try:
        result = assertion()
    except Exception as exc:
        raise CapturedPaperInitialControllerError(
            "initial_controller_service_fence_lost"
        ) from exc
    if result is not None:
        raise CapturedPaperInitialControllerError(
            "initial_controller_service_fence_invalid"
        )


def _decision_id(runtime_generation: str, notify_sha256: str) -> str:
    return str(
        uuid.uuid5(
            _DECISION_NAMESPACE,
            f"{runtime_generation}:{notify_sha256}",
        )
    )


def _result(
    *,
    symbol: str,
    admitted: bool,
    reason: str,
    session_id: int | None = None,
    l2_checkpoint_queued: bool = False,
    durable_preowner: bool = False,
) -> dict[str, Any]:
    return {
        "ok": bool(admitted),
        "admitted": bool(admitted),
        "skipped": None if admitted else str(reason),
        "reason": str(reason),
        "symbol": str(symbol or "").strip().upper(),
        "session_id": session_id,
        "coverage_status": (
            "READY" if admitted else "COVERAGE_UNAVAILABLE"
        ),
        "l2_checkpoint_queued": bool(l2_checkpoint_queued),
        "durable_preowner": bool(durable_preowner),
        "opportunity_consumed": False,
        "risk_reserved": False,
        "outbox_created": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialControllerPolicy:
    max_attempts: int
    retry_delay_seconds: float
    future_tolerance_seconds: float
    exact_print_window_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 64
        ):
            raise CapturedPaperInitialControllerError(
                "initial_controller_attempt_policy_invalid"
            )
        numeric = (
            self.retry_delay_seconds,
            self.future_tolerance_seconds,
            self.exact_print_window_seconds,
        )
        if any(type(value) not in {int, float} for value in numeric):
            raise CapturedPaperInitialControllerError(
                "initial_controller_timing_policy_invalid"
            )
        retry, future, window = map(float, numeric)
        if (
            not 0.0 <= retry <= 1.0
            or not 0.0 <= future <= 5.0
            or not 0.0 < window <= 1.0
        ):
            raise CapturedPaperInitialControllerError(
                "initial_controller_timing_policy_invalid"
            )
        object.__setattr__(self, "retry_delay_seconds", retry)
        object.__setattr__(self, "future_tolerance_seconds", future)
        object.__setattr__(self, "exact_print_window_seconds", window)


class CapturedPaperInitialAdmissionController:
    """One process-private, service-fenced initial symbol admission owner."""

    def __init__(
        self,
        *,
        host: Any,
        bind: Engine,
        candidate_reader: CapturedPaperInitialCandidateReadPort,
        user_id: int,
        expected_account_id: str,
        runtime_generation: str,
        code_build_sha256: str,
        capture_receipt_sha256: str,
        expected_bridge_version: str,
        adaptive_policy_settings_receipt: AdaptiveRiskPolicySettingsReceipt,
        adaptive_policy_spec: CapturedAdaptiveRiskPolicySpec,
        controller_policy: CapturedPaperInitialControllerPolicy,
        assert_service_fence_held: Callable[[], None],
        wall_clock: Callable[[], datetime],
        wait: Callable[[float], None],
    ) -> None:
        if not isinstance(bind, Engine):
            raise CapturedPaperInitialControllerError(
                "initial_controller_engine_invalid"
            )
        required_host = (
            "admit_hot_symbol",
            "abort_hot_symbol",
            "captured_paper_config_sha256_for",
        )
        if any(not callable(getattr(host, name, None)) for name in required_host):
            raise CapturedPaperInitialControllerError(
                "initial_controller_host_invalid"
            )
        service = getattr(getattr(host, "composition", None), "service", None)
        if any(
            not callable(getattr(service, name, None))
            for name in ("coordinator_for", "identity_evidence_for")
        ) or not callable(
            getattr(service, "capture_complete_microstructure_window", None)
        ):
            raise CapturedPaperInitialControllerError(
                "initial_controller_capture_service_invalid"
            )
        if not callable(wall_clock) or not callable(wait):
            raise CapturedPaperInitialControllerError(
                "initial_controller_clock_invalid"
            )
        if type(controller_policy) is not CapturedPaperInitialControllerPolicy:
            raise CapturedPaperInitialControllerError(
                "initial_controller_policy_invalid"
            )
        if type(adaptive_policy_settings_receipt) is not AdaptiveRiskPolicySettingsReceipt:
            raise CapturedPaperInitialControllerError(
                "initial_controller_adaptive_policy_invalid"
            )
        if type(adaptive_policy_spec) is not CapturedAdaptiveRiskPolicySpec:
            raise CapturedPaperInitialControllerError(
                "initial_controller_adaptive_policy_invalid"
            )
        if type(user_id) is not int or user_id <= 0:
            raise CapturedPaperInitialControllerError(
                "initial_controller_user_invalid"
            )
        if not callable(assert_service_fence_held):
            raise CapturedPaperInitialControllerError(
                "initial_controller_service_fence_unavailable"
            )
        self._host = host
        self._capture_service = service
        self._bind = bind
        self._candidate_reader = candidate_reader
        self._user_id = user_id
        self._expected_account_id = expected_account_id
        self._runtime_generation = runtime_generation
        self._code_build_sha256 = code_build_sha256
        self._capture_receipt_sha256 = capture_receipt_sha256
        self._expected_bridge_version = expected_bridge_version
        self._policy_receipt = adaptive_policy_settings_receipt
        self._policy_spec = adaptive_policy_spec
        self._controller_policy = controller_policy
        self._assert_service_fence_held = assert_service_fence_held
        self._wall_clock = wall_clock
        self._wait = wait
        self._guard = threading.Lock()
        self._inflight_symbols: set[str] = set()

    def _enter_symbol(self, symbol: str) -> bool:
        with self._guard:
            if symbol in self._inflight_symbols:
                return False
            self._inflight_symbols.add(symbol)
            return True

    def _leave_symbol(self, symbol: str) -> None:
        with self._guard:
            self._inflight_symbols.discard(symbol)

    def _abort_before_durable(self, symbol: str, reason: str) -> None:
        try:
            self._host.abort_hot_symbol(symbol, reason=reason)
        except Exception:
            # A failed resource cleanup grants no session/order authority.  The
            # host health/supervisor owns recovery of the bounded hot run.
            pass

    def admit(
        self,
        *,
        symbol: str,
        payload: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        durable_preowner = False
        hot_capture_admitted = False
        l2_checkpoint_queued = False
        session_id: int | None = None
        normalized = str(symbol or "").strip().upper()
        if not self._enter_symbol(normalized):
            return _result(
                symbol=normalized,
                admitted=False,
                reason="initial_controller_symbol_inflight",
            )
        try:
            _assert_service_fence(self._assert_service_fence_held)
            notify = parse_captured_paper_iqfeed_q_notify(
                payload,
                expected_bridge_version=self._expected_bridge_version,
            )
            if notify.symbol != normalized:
                raise CapturedPaperInitialControllerError(
                    "initial_controller_notify_symbol_mismatch"
                )
            decision_id = _decision_id(
                self._runtime_generation,
                notify.content_sha256,
            )
            hot = self._host.admit_hot_symbol(
                normalized,
                required_l1_stream=CaptureStream.IQFEED_PRINT,
            )
            if not bool(getattr(hot, "capture_ready", False)):
                raise CapturedPaperInitialControllerError(
                    str(
                        getattr(hot, "rejected_reason", None)
                        or "initial_controller_hot_capture_unavailable"
                    )
                )
            hot_capture_admitted = True
            l2_checkpoint_queued = bool(
                getattr(hot, "l2_checkpoint_queued", False)
            )
            config_sha256 = self._host.captured_paper_config_sha256_for(
                normalized
            )
            recovered = recover_captured_paper_initial_symbol(
                self._bind,
                symbol=normalized,
                expected_account_id=self._expected_account_id,
                expected_runtime_generation=self._runtime_generation,
                expected_code_build_sha256=self._code_build_sha256,
                expected_config_sha256=config_sha256,
                expected_capture_receipt_sha256=(
                    self._capture_receipt_sha256
                ),
                assert_service_fence_held=self._assert_service_fence_held,
            )
            if recovered is not None:
                if recovered.disposition == "pending_owner_recovered":
                    durable_preowner = True
                    session_id = recovered.session_id
                    return _result(
                        symbol=normalized,
                        admitted=True,
                        reason="captured_paper_pending_owner_recovered",
                        session_id=session_id,
                        l2_checkpoint_queued=l2_checkpoint_queued,
                        durable_preowner=True,
                    )
                if recovered.disposition != "expired_released":
                    raise CapturedPaperInitialControllerError(
                        "initial_controller_recovery_disposition_invalid"
                    )
                # The exact stale generation is terminal and its claim is
                # resolved.  Reuse this same fresh Q/hot capture to build a new
                # material generation; no opportunity or risk was consumed.
                session_id = None
            policy = self._controller_policy
            market_max_age = float(
                self._policy_receipt.policy.market_data_max_age_seconds
            )
            resolver = CapturedPaperIqfeedTriggerResolver(
                capture=self._capture_service,
                expected_bridge_version=self._expected_bridge_version,
                wall_clock=self._wall_clock,
                wait=self._wait,
                max_attempts=policy.max_attempts,
                retry_delay_seconds=policy.retry_delay_seconds,
                max_notify_age_seconds=market_max_age,
                future_tolerance_seconds=policy.future_tolerance_seconds,
                exact_print_window_seconds=policy.exact_print_window_seconds,
            )
            resolution = resolver.resolve(payload, decision_id=decision_id)
            if (
                resolution.status is not IqfeedTriggerStatus.READY
                or not resolution.ready
                or resolution.receipt is None
                or resolution.captured_read is None
                or resolution.captured_read.receipt is None
            ):
                raise CapturedPaperInitialControllerError(resolution.reason)
            captured = resolution.captured_read
            receipt = captured.receipt
            try:
                query = CaptureMicrostructureReadQuery.from_dict(
                    dict(receipt.query or {})
                )
            except Exception as exc:
                raise CapturedPaperInitialControllerError(
                    "initial_controller_trigger_query_invalid"
                ) from exc
            dependency_profile = FSMDependencyProfile(
                required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
                required_read_ids=(receipt.read_id,),
                stream_dependencies=(
                    FSMStreamDependency(
                        stream=CaptureStream.IQFEED_PRINT,
                        exact_provider_event_at_required=True,
                        market_reference_at_required=False,
                        max_source_age_seconds=market_max_age,
                        coverage_start_at=query.event_start_exclusive,
                    ),
                ),
            )
            coordinator = self._capture_service.coordinator_for(normalized)
            coordinator.checkpoint_live_continuity(CaptureStream.IQFEED_PRINT)
            proof = coordinator.attest_predecision_inputs(
                decision_id=decision_id,
                dependency_profile=dependency_profile,
                captured_reads=(captured,),
            )
            provider = CaptureBackedPaperInitialSessionMaterialProvider(
                user_id=self._user_id,
                account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
                expected_account_id=self._expected_account_id,
                runtime_generation=self._runtime_generation,
                code_build_sha256=self._code_build_sha256,
                capture_receipt_sha256=self._capture_receipt_sha256,
                trigger_resolution=resolution,
                active_input_attestation=proof,
                capture_coordinator=coordinator,
                capture_identity_evidence=(
                    self._capture_service.identity_evidence_for(normalized)
                ),
                capture_config_sha256_resolver=(
                    self._host.captured_paper_config_sha256_for
                ),
                candidate_reader=self._candidate_reader,
                adaptive_policy_settings_receipt=self._policy_receipt,
                adaptive_policy_spec=self._policy_spec,
                material_ttl_seconds=market_max_age,
                wall_clock=self._wall_clock,
            )
            identity = coordinator.identity
            capability = CapturedPaperInitialAdmissionCapability(
                provider=provider,
                expected_account_id=self._expected_account_id,
                runtime_generation=self._runtime_generation,
                code_build_sha256=self._code_build_sha256,
                config_sha256=config_sha256,
                capture_receipt_sha256=self._capture_receipt_sha256,
                policy_sha256=self._policy_receipt.policy.policy_sha256,
                settings_projection_sha256=(
                    self._policy_receipt.settings_projection_sha256
                ),
                feature_flags_sha256=identity.feature_flags_sha256,
                adaptive_policy_provenance_sha256=(
                    self._policy_spec.provenance_sha256
                ),
            )
            material = capability.prepare(
                symbol=normalized,
                trigger_read_receipt_sha256=(
                    resolution.receipt.content_sha256
                ),
            )
            preowner = commit_captured_paper_initial_preowner(
                self._bind,
                material=material,
                verification_at=_utc(
                    self._wall_clock(),
                    "initial_controller_preowner_clock_invalid",
                ),
                assert_service_fence_held=(
                    self._assert_service_fence_held
                ),
            )
            durable_preowner = True
            session_id = preowner.session_id
            dispatch_request = CapturedPaperDispatchRequest(
                session_id=preowner.session_id,
                symbol=material.symbol,
                execution_family=ALPACA_SPOT_EXECUTION_FAMILY,
                account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
                expected_account_id=material.expected_account_id,
                code_build_sha256=material.code_build_sha256,
                config_sha256=material.config_sha256,
                capture_receipt_sha256=material.capture_receipt_sha256,
                runtime_generation=material.runtime_generation,
                first_dip_policy_mode="candidate",
            )
            promoted = promote_captured_paper_preowner(
                self._bind,
                material=material,
                preowner_receipt=preowner,
                dispatch_request=dispatch_request,
                verification_at=_utc(
                    self._wall_clock(),
                    "initial_controller_promotion_clock_invalid",
                ),
                assert_service_fence_held=(
                    self._assert_service_fence_held
                ),
            )
            if promoted.session_id != preowner.session_id:
                raise CapturedPaperInitialControllerError(
                    "initial_controller_promotion_session_mismatch"
                )
            return _result(
                symbol=normalized,
                admitted=True,
                reason="captured_paper_pending_owner_ready",
                session_id=promoted.session_id,
                l2_checkpoint_queued=l2_checkpoint_queued,
                durable_preowner=True,
            )
        except (
            CapturedPaperInitialControllerError,
            CapturedPaperInitialProviderCoverageUnavailable,
            CapturedPaperInitialAdmissionError,
            CapturedPaperInitialRecoveryError,
            CapturedPaperPreownerPromotionError,
            CaptureContractError,
        ) as exc:
            reason = str(getattr(exc, "reason", None) or exc)
            if hot_capture_admitted and not durable_preowner:
                self._abort_before_durable(normalized, reason)
            return _result(
                symbol=normalized,
                admitted=False,
                reason=reason,
                session_id=session_id,
                l2_checkpoint_queued=l2_checkpoint_queued,
                durable_preowner=durable_preowner,
            )
        except Exception:
            reason = "initial_controller_internal_unavailable"
            if hot_capture_admitted and not durable_preowner:
                self._abort_before_durable(normalized, reason)
            return _result(
                symbol=normalized,
                admitted=False,
                reason=reason,
                session_id=session_id,
                l2_checkpoint_queued=l2_checkpoint_queued,
                durable_preowner=durable_preowner,
            )
        finally:
            self._leave_symbol(normalized)


__all__ = [
    "CapturedPaperInitialAdmissionController",
    "CapturedPaperInitialControllerError",
    "CapturedPaperInitialControllerPolicy",
]
