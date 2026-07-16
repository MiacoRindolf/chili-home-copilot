from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import threading
from typing import Any

import pytest
from sqlalchemy import create_engine

from app.services.trading.momentum_neural import (
    captured_paper_initial_controller as controller,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
    AdaptiveRiskPolicy,
    AdaptiveRiskPolicySettingsReceipt,
)
from app.services.trading.momentum_neural.captured_adaptive_risk_source import (
    CapturedAdaptiveRiskPolicySpec,
)
from app.services.trading.momentum_neural.captured_paper_initial_provider import (
    CapturedPaperInitialProviderCoverageUnavailable,
)
from app.services.trading.momentum_neural.captured_paper_preowner_promotion import (
    CapturedPaperPreownerPromotionError,
)
from app.services.trading.momentum_neural.captured_paper_iqfeed_trigger import (
    IqfeedTriggerStatus,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureStream,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 19, 0, 0, 500_000, tzinfo=UTC)
SYMBOL = "TEST"
ACCOUNT_ID = "7ddc5883-c493-4de4-a4e5-e3f959461bfd"
RUNTIME_GENERATION = "97beeb02-84c7-47a8-859d-44d409674ec0"
READ_ID = "07e20956-f1b8-4890-9bea-da4aa3105cca"
BRIDGE_VERSION = "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _notify(**overrides: Any) -> dict[str, Any]:
    reference_at = NOW - timedelta(milliseconds=300)
    payload: dict[str, Any] = {
        "symbol": SYMBOL,
        "observed_at": reference_at.isoformat(),
        "bid": 4.19,
        "ask": 4.21,
        "received_at": (NOW - timedelta(milliseconds=200)).isoformat(),
        "provider_event_at": None,
        "provider_trade_reference_at": reference_at.isoformat(),
        "timestamp_basis": "iqfeed_q_receive_trade_reference_fenced",
        "source": "iqfeed_l1",
        "bridge_version": BRIDGE_VERSION,
        "message_type": "Q",
        "bridge_run_id": "8da0a1ed-24f3-4545-8a7a-6f582ff1acc2",
        "connection_generation": 3,
        "source_frame_sequence": 41,
        "source_frame_sha256": _digest("raw-iqfeed-frame"),
        "available_at": (NOW - timedelta(milliseconds=100)).isoformat(),
    }
    payload.update(overrides)
    return payload


def _policy_receipt() -> AdaptiveRiskPolicySettingsReceipt:
    policy = AdaptiveRiskPolicy(
        policy_version="shared-replay-paper-v1",
        policy_source="test:captured-paper-initial-controller",
        risk_fraction_of_equity=0.012,
        daily_risk_fraction_of_equity=0.06,
        portfolio_risk_fraction_of_equity=0.08,
        cluster_risk_fraction_of_equity=0.035,
        symbol_risk_fraction_of_equity=0.022,
        daily_gap_reserve_fraction_of_equity=0.002,
        max_notional_fraction_of_equity=0.17,
        max_buying_power_fraction_for_notional=0.45,
        max_portfolio_gross_fraction_of_equity=1.75,
        quality_multiplier_floor=0.60,
        quality_multiplier_ceiling=1.40,
        volatility_reference_fraction=0.04,
        volatility_multiplier_floor=0.35,
        spread_reserve_multiple=1.20,
        per_share_gap_reserve_volatility_multiple=0.12,
        max_adv_participation=0.015,
        max_recent_volume_participation=0.11,
        max_executable_depth_participation=0.55,
        market_data_max_age_seconds=1.75,
        account_data_max_age_seconds=9.0,
        reservation_data_max_age_seconds=0.20,
        context_data_max_age_seconds=45.0,
    )
    return AdaptiveRiskPolicySettingsReceipt(
        policy=policy,
        setting_values=tuple(
            (setting_name, getattr(policy, policy_name))
            for policy_name, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
        ),
    )


def _read_query() -> CaptureMicrostructureReadQuery:
    decision_at = NOW - timedelta(milliseconds=100)
    return CaptureMicrostructureReadQuery(
        operation=CaptureMicrostructureOperation.TRADE_FLOW,
        stream=CaptureStream.IQFEED_PRINT,
        symbol=SYMBOL,
        provider="iqfeed",
        event_start_exclusive=decision_at - timedelta(milliseconds=250),
        event_end_inclusive=decision_at,
        decision_at=decision_at,
        available_at_most=NOW,
        source_frontier_sequence=41,
        source_clock_basis="provider_event_at",
        parameters={"window_seconds": 0.25},
    )


@dataclass
class _Rig:
    events: list[str] = field(default_factory=list)
    resolver_kwargs: list[dict[str, Any]] = field(default_factory=list)
    resolver_calls: list[dict[str, Any]] = field(default_factory=list)
    provider_kwargs: list[dict[str, Any]] = field(default_factory=list)
    capability_kwargs: list[dict[str, Any]] = field(default_factory=list)
    capability_prepare_calls: list[dict[str, Any]] = field(default_factory=list)
    commit_calls: list[dict[str, Any]] = field(default_factory=list)
    promotion_calls: list[dict[str, Any]] = field(default_factory=list)
    hot_calls: list[dict[str, Any]] = field(default_factory=list)
    abort_calls: list[dict[str, Any]] = field(default_factory=list)
    attest_calls: list[dict[str, Any]] = field(default_factory=list)
    recovery_calls: list[dict[str, Any]] = field(default_factory=list)
    fence_calls: int = 0
    proof_error: Exception | None = None
    provider_error: Exception | None = None
    promotion_error: Exception | None = None
    fence_error: Exception | None = None
    resolver_entered: threading.Event | None = None
    resolver_release: threading.Event | None = None
    recovery_result: object | None = None


class _Coordinator:
    def __init__(self, rig: _Rig) -> None:
        self._rig = rig
        self.identity = SimpleNamespace(
            feature_flags_sha256=_digest("capture-feature-flags")
        )

    def checkpoint_live_continuity(self, stream: CaptureStream) -> None:
        self._rig.events.append("checkpoint")
        assert stream is CaptureStream.IQFEED_PRINT

    def attest_predecision_inputs(self, **kwargs: Any) -> object:
        self._rig.events.append("attest")
        self._rig.attest_calls.append(dict(kwargs))
        if self._rig.proof_error is not None:
            raise self._rig.proof_error
        return SimpleNamespace(proof="active-input-prefix")


class _CaptureService:
    def __init__(self, rig: _Rig, coordinator: _Coordinator) -> None:
        self._rig = rig
        self._coordinator = coordinator

    def capture_complete_microstructure_window(self, **_kwargs: Any) -> None:
        raise AssertionError("the monkeypatched strict resolver owns this read")

    def coordinator_for(self, symbol: str) -> _Coordinator:
        self._rig.events.append("coordinator")
        assert symbol == SYMBOL
        return self._coordinator

    def identity_evidence_for(self, symbol: str) -> object:
        self._rig.events.append("identity_evidence")
        assert symbol == SYMBOL
        return SimpleNamespace(account_id=ACCOUNT_ID)


class _Host:
    def __init__(self, rig: _Rig, service: _CaptureService) -> None:
        self._rig = rig
        self.composition = SimpleNamespace(service=service)
        self.hot_result = SimpleNamespace(
            capture_ready=True,
            l2_checkpoint_queued=False,
            rejected_reason="l2_checkpoint_unavailable",
        )

    def admit_hot_symbol(self, symbol: str, **kwargs: Any) -> object:
        self._rig.events.append("hot")
        self._rig.hot_calls.append({"symbol": symbol, **kwargs})
        return self.hot_result

    def abort_hot_symbol(self, symbol: str, **kwargs: Any) -> None:
        self._rig.events.append("abort")
        self._rig.abort_calls.append({"symbol": symbol, **kwargs})

    def captured_paper_config_sha256_for(self, symbol: str) -> str:
        self._rig.events.append("config")
        assert symbol == SYMBOL
        return _digest("per-symbol-capture-config")


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    rig: _Rig,
) -> tuple[controller.CapturedPaperInitialAdmissionController, _Host, Any]:
    query = _read_query()
    captured_receipt = SimpleNamespace(read_id=READ_ID, query=query.to_dict())
    captured_read = SimpleNamespace(receipt=captured_receipt)
    trigger_receipt = SimpleNamespace(content_sha256=_digest("trigger-receipt"))
    resolution = SimpleNamespace(
        status=IqfeedTriggerStatus.READY,
        ready=True,
        receipt=trigger_receipt,
        captured_read=captured_read,
        reason="iqfeed_exact_print_trigger_ready",
    )

    class _Resolver:
        def __init__(self, **kwargs: Any) -> None:
            rig.events.append("resolver_init")
            rig.resolver_kwargs.append(dict(kwargs))

        def resolve(self, payload: Any, **kwargs: Any) -> object:
            rig.events.append("resolve")
            rig.resolver_calls.append({"payload": payload, **kwargs})
            if rig.resolver_entered is not None:
                rig.resolver_entered.set()
            if rig.resolver_release is not None:
                assert rig.resolver_release.wait(timeout=5.0)
            return resolution

    class _Provider:
        def __init__(self, **kwargs: Any) -> None:
            rig.events.append("provider")
            rig.provider_kwargs.append(dict(kwargs))

    material = SimpleNamespace(
        symbol=SYMBOL,
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=_digest("code-build"),
        config_sha256=_digest("per-symbol-capture-config"),
        capture_receipt_sha256=_digest("capture-receipt"),
        runtime_generation=RUNTIME_GENERATION,
    )

    class _Capability:
        def __init__(self, **kwargs: Any) -> None:
            rig.events.append("capability")
            rig.capability_kwargs.append(dict(kwargs))

        def prepare(self, **kwargs: Any) -> object:
            rig.events.append("prepare")
            rig.capability_prepare_calls.append(dict(kwargs))
            if rig.provider_error is not None:
                raise rig.provider_error
            return material

    preowner = SimpleNamespace(session_id=91)

    def _commit(_bind: Any, **kwargs: Any) -> object:
        rig.events.append("commit")
        rig.commit_calls.append(dict(kwargs))
        return preowner

    def _promote(_bind: Any, **kwargs: Any) -> object:
        rig.events.append("promote")
        rig.promotion_calls.append(dict(kwargs))
        if rig.promotion_error is not None:
            raise rig.promotion_error
        return SimpleNamespace(session_id=preowner.session_id)

    monkeypatch.setattr(controller, "CapturedPaperIqfeedTriggerResolver", _Resolver)
    monkeypatch.setattr(
        controller,
        "CaptureBackedPaperInitialSessionMaterialProvider",
        _Provider,
    )
    monkeypatch.setattr(controller, "CapturedPaperInitialAdmissionCapability", _Capability)
    monkeypatch.setattr(controller, "commit_captured_paper_initial_preowner", _commit)
    monkeypatch.setattr(controller, "promote_captured_paper_preowner", _promote)

    def _recover(_bind: Any, **kwargs: Any) -> object | None:
        rig.events.append("recover")
        rig.recovery_calls.append(dict(kwargs))
        return rig.recovery_result

    monkeypatch.setattr(
        controller,
        "recover_captured_paper_initial_symbol",
        _recover,
    )

    coordinator = _Coordinator(rig)
    service = _CaptureService(rig, coordinator)
    host = _Host(rig, service)
    receipt = _policy_receipt()
    spec = CapturedAdaptiveRiskPolicySpec(
        policy=receipt.policy,
        code_build_sha256=_digest("code-build"),
        effective_config_sha256=receipt.settings_projection_sha256,
        feature_flags_sha256=coordinator.identity.feature_flags_sha256,
    )

    def _fence() -> None:
        rig.fence_calls += 1
        if rig.fence_error is not None:
            raise rig.fence_error

    # Constructing the controller must not connect to this engine.  Every
    # persistence function is replaced above with a pure recorder.
    bind = create_engine("sqlite+pysqlite:///:memory:")
    instance = controller.CapturedPaperInitialAdmissionController(
        host=host,
        bind=bind,
        candidate_reader=SimpleNamespace(read_only=True),
        user_id=31,
        expected_account_id=ACCOUNT_ID,
        runtime_generation=RUNTIME_GENERATION,
        code_build_sha256=_digest("code-build"),
        capture_receipt_sha256=_digest("capture-receipt"),
        expected_bridge_version=BRIDGE_VERSION,
        adaptive_policy_settings_receipt=receipt,
        adaptive_policy_spec=spec,
        controller_policy=controller.CapturedPaperInitialControllerPolicy(
            max_attempts=3,
            retry_delay_seconds=0.0,
            future_tolerance_seconds=0.25,
            exact_print_window_seconds=0.25,
        ),
        assert_service_fence_held=_fence,
        wall_clock=lambda: NOW,
        wait=lambda _seconds: None,
    )
    return instance, host, resolution


def _assert_no_execution_authority(result: dict[str, Any]) -> None:
    assert result["opportunity_consumed"] is False
    assert result["risk_reserved"] is False
    assert result["outbox_created"] is False
    assert result["order_posted"] is False
    assert result["broker_order_post_calls"] == 0


def test_constructor_is_inert_and_lost_service_fence_precedes_all_host_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(fence_error=RuntimeError("service fence not held"))
    instance, host, _resolution = _install_fakes(monkeypatch, rig)

    assert rig.fence_calls == 0
    assert rig.events == []

    result = instance.admit(symbol=SYMBOL, payload=_notify())

    assert result["admitted"] is False
    assert result["reason"] == "initial_controller_service_fence_lost"
    assert rig.fence_calls == 1
    assert host._rig.hot_calls == []
    assert host._rig.abort_calls == []
    assert rig.resolver_kwargs == []
    assert rig.commit_calls == []
    assert rig.promotion_calls == []
    _assert_no_execution_authority(result)


def test_strict_notify_preparse_rejects_before_hot_allocation_or_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig()
    instance, _host, _resolution = _install_fakes(monkeypatch, rig)

    result = instance.admit(symbol=SYMBOL, payload={"symbol": SYMBOL})

    assert result["admitted"] is False
    assert "iqfeed_notify_fields_mismatch" in result["reason"]
    assert rig.fence_calls == 1
    assert rig.hot_calls == []
    assert rig.abort_calls == []
    assert rig.resolver_kwargs == []
    assert rig.commit_calls == []
    _assert_no_execution_authority(result)


def test_next_exact_q_recovers_preowner_before_new_provider_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(
        recovery_result=SimpleNamespace(
            disposition="pending_owner_recovered",
            session_id=77,
        )
    )
    instance, host, _resolution = _install_fakes(monkeypatch, rig)

    result = instance.admit(symbol=SYMBOL, payload=_notify())

    assert result["admitted"] is True
    assert result["reason"] == "captured_paper_pending_owner_recovered"
    assert result["session_id"] == 77
    assert result["durable_preowner"] is True
    assert rig.recovery_calls == [
        {
            "symbol": SYMBOL,
            "expected_account_id": ACCOUNT_ID,
            "expected_runtime_generation": RUNTIME_GENERATION,
            "expected_code_build_sha256": _digest("code-build"),
            "expected_config_sha256": _digest("per-symbol-capture-config"),
            "expected_capture_receipt_sha256": _digest("capture-receipt"),
            "assert_service_fence_held": instance._assert_service_fence_held,
        }
    ]
    assert rig.resolver_kwargs == []
    assert rig.provider_kwargs == []
    assert rig.commit_calls == []
    assert rig.promotion_calls == []
    assert host._rig.abort_calls == []
    _assert_no_execution_authority(result)


def test_expired_initial_generation_is_released_then_same_q_reads_fresh_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(
        recovery_result=SimpleNamespace(
            disposition="expired_released",
            session_id=77,
        )
    )
    instance, host, _resolution = _install_fakes(monkeypatch, rig)

    result = instance.admit(symbol=SYMBOL, payload=_notify())

    assert result["admitted"] is True
    assert result["session_id"] == 91
    assert rig.events.index("recover") < rig.events.index("resolver_init")
    assert len(rig.commit_calls) == 1
    assert len(rig.promotion_calls) == 1
    assert host._rig.abort_calls == []
    _assert_no_execution_authority(result)


def test_exact_print_success_checkpoints_before_attestation_and_ignores_local_l2_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig()
    instance, _host, resolution = _install_fakes(monkeypatch, rig)

    result = instance.admit(symbol="test", payload=_notify())

    assert result == {
        "ok": True,
        "admitted": True,
        "skipped": None,
        "reason": "captured_paper_pending_owner_ready",
        "symbol": SYMBOL,
        "session_id": 91,
        "coverage_status": "READY",
        "l2_checkpoint_queued": False,
        "durable_preowner": True,
        "opportunity_consumed": False,
        "risk_reserved": False,
        "outbox_created": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }
    assert rig.hot_calls == [
        {"symbol": SYMBOL, "required_l1_stream": CaptureStream.IQFEED_PRINT}
    ]
    assert rig.abort_calls == []
    assert rig.events.index("checkpoint") < rig.events.index("attest")
    assert len(rig.attest_calls) == 1
    attest = rig.attest_calls[0]
    assert attest["captured_reads"] == (resolution.captured_read,)
    profile = attest["dependency_profile"]
    assert profile.required_streams == frozenset({CaptureStream.IQFEED_PRINT})
    assert profile.required_read_ids == (READ_ID,)
    dependency = profile.dependency_for(CaptureStream.IQFEED_PRINT)
    assert dependency.exact_provider_event_at_required is True
    assert dependency.market_reference_at_required is False
    receipt = _policy_receipt()
    assert dependency.max_source_age_seconds == pytest.approx(
        receipt.policy.market_data_max_age_seconds
    )
    query = CaptureMicrostructureReadQuery.from_dict(
        resolution.captured_read.receipt.query
    )
    assert dependency.coverage_start_at == query.event_start_exclusive
    assert rig.resolver_kwargs[0]["max_notify_age_seconds"] == pytest.approx(
        receipt.policy.market_data_max_age_seconds
    )
    assert rig.provider_kwargs[0]["material_ttl_seconds"] == pytest.approx(
        receipt.policy.market_data_max_age_seconds
    )
    assert rig.commit_calls[0]["assert_service_fence_held"] is instance._assert_service_fence_held
    dispatch = rig.promotion_calls[0]["dispatch_request"]
    assert dispatch.first_dip_policy_mode == "candidate"
    assert dispatch.account_scope == "alpaca:paper"
    assert dispatch.execution_family == "alpaca_spot"


@pytest.mark.parametrize("failure_stage", ["proof", "provider"])
def test_failure_before_preowner_aborts_hot_run_without_durable_or_execution_state(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    rig = _Rig()
    if failure_stage == "proof":
        rig.proof_error = CaptureContractError("exact_print_continuity_unavailable")
    else:
        rig.provider_error = CapturedPaperInitialProviderCoverageUnavailable(
            "candidate_capture_inputs_unavailable"
        )
    instance, _host, _resolution = _install_fakes(monkeypatch, rig)

    result = instance.admit(symbol=SYMBOL, payload=_notify())

    assert result["admitted"] is False
    assert result["durable_preowner"] is False
    assert result["session_id"] is None
    assert len(rig.hot_calls) == 1
    assert len(rig.abort_calls) == 1
    assert rig.abort_calls[0]["symbol"] == SYMBOL
    assert rig.commit_calls == []
    assert rig.promotion_calls == []
    _assert_no_execution_authority(result)


def test_promotion_failure_retains_hot_capture_and_durable_preowner_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(
        promotion_error=CapturedPaperPreownerPromotionError(
            "pending_owner_promotion_fence_lost"
        )
    )
    instance, _host, _resolution = _install_fakes(monkeypatch, rig)

    result = instance.admit(symbol=SYMBOL, payload=_notify())

    assert result["admitted"] is False
    assert result["reason"] == "pending_owner_promotion_fence_lost"
    assert result["durable_preowner"] is True
    assert result["session_id"] == 91
    assert len(rig.commit_calls) == 1
    assert len(rig.promotion_calls) == 1
    assert rig.abort_calls == []
    _assert_no_execution_authority(result)


def test_duplicate_symbol_has_exactly_one_inflight_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    rig = _Rig(resolver_entered=entered, resolver_release=release)
    instance, _host, _resolution = _install_fakes(monkeypatch, rig)
    first_results: list[dict[str, Any]] = []
    thread = threading.Thread(
        target=lambda: first_results.append(
            instance.admit(symbol=SYMBOL, payload=_notify())
        ),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=5.0)

    duplicate = instance.admit(symbol="test", payload=_notify())

    assert duplicate["admitted"] is False
    assert duplicate["reason"] == "initial_controller_symbol_inflight"
    assert len(rig.hot_calls) == 1
    assert rig.abort_calls == []
    assert rig.commit_calls == []
    _assert_no_execution_authority(duplicate)

    release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(first_results) == 1
    assert first_results[0]["admitted"] is True
    assert len(rig.hot_calls) == 1
    assert len(rig.commit_calls) == 1
    assert len(rig.promotion_calls) == 1
