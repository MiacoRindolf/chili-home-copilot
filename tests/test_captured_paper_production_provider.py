from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import time
import uuid

import pandas as pd
import pytest

from app.services.trading.momentum_neural.captured_paper_production_material import (
    CapturedPaperDurableObservationSnapshot,
    CapturedPaperProductionMaterialUnavailable,
)
from app.services.trading.momentum_neural.captured_paper_production_provider import (
    CapturedPaperCandidateSupplement,
    CapturedPaperCaptureBackedSupplementProviders,
    CapturedPaperFactSource,
    CapturedPaperObservationSupplement,
    CapturedPaperServiceCaptureProviders,
    _CapturedPaperCoordinatorObservedInputs,
    _CapturedPaperExactRuntimeInputScope,
    _CANDIDATE_FACT_READ_KEYS,
    build_capture_backed_paper_service_material_factory,
    build_live_fsm_captured_paper_service_material_factory,
)
from app.services.trading.momentum_neural.captured_paper_selection import (
    captured_paper_observation_generation_sha256,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapePolicy,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    ActiveCaptureReadEvidence,
    CaptureClocks,
    CaptureEventRef,
    CaptureScannerProfile,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    STREAM_POLICIES,
    _issue_active_capture_input_attestation,
    sha256_json,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    LiveMicrostructureCaptureBridge,
    LiveOhlcvCaptureBridge,
    LiveScannerSnapshotCaptureBridge,
    ObservedCaptureInput,
)
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedProduct
from tests.test_captured_alpaca_paper_adapter import (
    _Coordinator,
    _PaperAdapter,
    _Clock,
)
from tests.test_captured_paper_production_material import (
    NOW,
    _candidate,
    _economics,
    _request,
)
from tests.test_live_replay_capture import BASE as CAPTURE_BASE, _coordinator


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _proof_for_results(coordinator, *, profile, decision_id, **_ignored):
    evidence = []
    for result in coordinator.results:
        assert result.receipt is not None
        assert result.receipt_submission is not None
        assert result.receipt_submission.event is not None
        receipt_event = result.receipt_submission.event
        evidence.append(
            ActiveCaptureReadEvidence(
                receipt=result.receipt,
                receipt_sha256=sha256_json(result.receipt.to_dict()),
                receipt_event_sha256=receipt_event.event_sha256,
                receipt_event_sequence=receipt_event.sequence,
                receipt_committed_available_at=(
                    receipt_event.clocks.available_at
                ),
                producer_id=coordinator._coordinator_producer_id,
                producer_generation=coordinator.identity.generation,
                source_event_refs=tuple(
                    CaptureEventRef.from_event(event)
                    for event in result.source_events
                ),
            )
        )
    assert tuple(sorted(row.receipt.read_id for row in evidence)) == (
        profile.required_read_ids
    )
    committed_at = max(row.receipt_committed_available_at for row in evidence)
    return _issue_active_capture_input_attestation(
        run_id=coordinator.identity.run_id,
        generation=coordinator.identity.generation,
        decision_id=decision_id,
        input_prefix_sequence=max(row.receipt_event_sequence for row in evidence),
        input_prefix_root_sha256=sha256_json(
            {
                "receipt_event_sha256s": sorted(
                    row.receipt_event_sha256 for row in evidence
                )
            }
        ),
        attested_available_at=committed_at,
        expires_at=committed_at + timedelta(seconds=60),
        dependency_profile=profile,
        identity_sha256=coordinator.identity.identity_sha256,
        account_identity_sha256=coordinator.identity.account_identity_sha256,
        code_build_sha256=coordinator.identity.code_build_sha256,
        config_sha256=coordinator.identity.config_sha256,
        feature_flags_sha256=coordinator.identity.feature_flags_sha256,
        resource_binding_sha256=_digest("service-provider-test-resource"),
        producer_generations={
            coordinator._coordinator_producer_id: coordinator.identity.generation
        },
        required_read_ids=profile.required_read_ids,
        read_evidence=tuple(evidence),
        continuity_evidence=(),
    )


def _ohlcv_supplement(coordinator, *, candidate, decision_id):
    clocks = CaptureClocks(
        received_at=NOW - timedelta(milliseconds=2),
        available_at=NOW,
        market_reference_at=NOW - timedelta(seconds=1),
    )
    query = {
        "operation": "aggregate_bars",
        "symbol": candidate.symbol,
        "interval": "1m",
        "adjusted": True,
    }
    read = coordinator.capture_query_result(
        decision_id=decision_id,
        stream=CaptureStream.PROVIDER_OHLCV,
        provider="massive",
        query=query,
        requested_at=NOW - timedelta(milliseconds=3),
        returned_at=NOW,
        results=(
            ObservedCaptureInput(
                payload={
                    "schema_version": "fixture.ohlcv.v1",
                    "symbol": candidate.symbol,
                    "bars": [{"open": 2.9, "high": 3.1, "low": 2.8, "close": 3.0}],
                },
                clocks=clocks,
            ),
        ),
        symbol=candidate.symbol,
        read_id=str(uuid.uuid4()),
    )
    assert read.receipt is not None
    profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.PROVIDER_OHLCV}),
        required_read_ids=(read.receipt.read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=CaptureStream.PROVIDER_OHLCV,
                exact_provider_event_at_required=False,
                market_reference_at_required=True,
                max_source_age_seconds=60,
                coverage_start_at=NOW - timedelta(seconds=1),
            ),
        ),
    )
    source = CapturedPaperFactSource(
        source="captured-derived:ohlcv",
        observed_at=NOW - timedelta(seconds=1),
        available_at=NOW,
        provider_generation="massive:test:1",
        source_read_ids=(read.receipt.read_id,),
    )
    return CapturedPaperCandidateSupplement(
        captured_reads=(read,),
        dependency_profile=profile,
        input_scope_installer=lambda: nullcontext(),
        input_scope_sha256=_digest("candidate-input-scope"),
        economics=_economics(),
        fact_sources={name: source for name in (
            "structural_stop",
            "setup_quality",
            "volatility",
            "liquidity",
            "correlation",
            "candidate_buying_power_estimate",
        )},
        correlation_cluster="equity:momentum-a",
        setup_read_id=read.receipt.read_id,
    )


def test_candidate_provider_owns_exact_adapter_scope_and_pre_attests_before_yield(
    monkeypatch,
):
    clock = _Clock()
    clock.now = NOW
    coordinator = _Coordinator()
    request = _request(coordinator)
    candidate = _candidate(request)
    raw_adapter = _PaperAdapter(clock)
    coordinator.attest_predecision_inputs = lambda **kwargs: _proof_for_results(
        coordinator,
        profile=kwargs["dependency_profile"],
        decision_id=kwargs["decision_id"],
    )

    @contextmanager
    def candidate_source(
        *, request, candidate, coordinator, adapter, account_read, bbo_read
    ):
        assert adapter.bound_account_id == request.expected_account_id
        assert account_read is coordinator.results[0]
        assert bbo_read is coordinator.results[1]
        yield _ohlcv_supplement(
            coordinator,
            candidate=candidate,
            decision_id=candidate.client_order_id,
        )

    @contextmanager
    def unused_observation_source(**_kwargs):
        raise AssertionError("observation source was called")
        yield

    providers = CapturedPaperServiceCaptureProviders(
        raw_adapter_factory=lambda observed_request, observed_coordinator: raw_adapter,
        candidate_supplement_provider=candidate_source,
        observation_supplement_provider=unused_observation_source,
        wall_clock=clock,
        quote_max_age_seconds=30,
        account_max_age_seconds=60,
    )
    # The production boundary requires a real LiveReplayCaptureCoordinator.
    # This synthetic test replaces only that nominal type guard; the private
    # typed proof, exact original read objects, and wrapper decision scope are
    # still exercised end to end.
    monkeypatch.setattr(providers, "_coordinator", lambda value, symbol: value)

    with providers.capture_candidate(
        request=request,
        candidate=candidate,
        coordinator=coordinator,
    ) as captured:
        assert captured.active_input_attestation.decision_id == candidate.client_order_id
        assert captured.active_input_attestation.attested_available_at <= captured.decision_at
        assert captured.captured_reads[0] is coordinator.results[0]
        assert captured.captured_reads[1] is coordinator.results[1]
        assert captured.captured_reads[2] is coordinator.results[2]
        assert captured.fact_evidence.setup_quality.source_read_ids == (
            coordinator.results[2].receipt.read_id,
        )
        assert captured.bound_input_scope.required_read_ids == (
            captured.dependency_profile.required_read_ids
        )
        assert raw_adapter.lifecycle_calls == 0


def test_production_provider_rejects_non_running_non_live_coordinator_before_adapter():
    calls = []
    providers = CapturedPaperServiceCaptureProviders(
        raw_adapter_factory=lambda *_args: calls.append("adapter"),
        candidate_supplement_provider=lambda **_kwargs: nullcontext(),
        observation_supplement_provider=lambda **_kwargs: nullcontext(),
        wall_clock=lambda: NOW,
        quote_max_age_seconds=2,
        account_max_age_seconds=5,
    )
    coordinator = _Coordinator()
    request = _request(coordinator)
    with pytest.raises(
        CapturedPaperProductionMaterialUnavailable,
        match="production_live_capture_coordinator_unavailable",
    ):
        with providers.capture_candidate(
            request=request,
            candidate=_candidate(request),
            coordinator=coordinator,
        ):
            pass
    assert calls == []


def test_optional_first_dip_material_is_strictly_paired_and_provider_has_no_order_calls():
    policy = FirstDipTapePolicy(
        window_seconds=10,
        max_source_age_seconds=2,
        tick_rate_floor_pctile=0.5,
        minimum_prints=3,
    )
    with pytest.raises(
        CapturedPaperProductionMaterialUnavailable,
        match="observation_supplemental_first_dip_pair_unavailable",
    ):
        CapturedPaperObservationSupplement(
            captured_reads=(),
            dependency_profile=object(),
            input_scope_installer=lambda: nullcontext(),
            input_scope_sha256=_digest("observation-scope"),
            observation_snapshot_read_id=str(uuid.uuid4()),
            admission_eligibility_read_id=str(uuid.uuid4()),
            first_dip_tape_read_id=str(uuid.uuid4()),
            first_dip_detector_policy=None,
        )
    source = inspect.getsource(CapturedPaperServiceCaptureProviders)
    assert "place_" not in source
    assert "cancel_order" not in source
    assert "attest_predecision_inputs" in source
    assert source.index("attest_predecision_inputs") < source.index(
        'decision_at = _utc(self._wall_clock(), "production_decision")'
    )
    assert policy.policy_sha256


def test_public_builder_has_no_fact_callback_or_variadic_injection_surface() -> None:
    signature = inspect.signature(build_capture_backed_paper_service_material_factory)
    assert tuple(signature.parameters) == (
        "coordinator_for",
        "capture_config_for",
        "settings_projection_sha256",
        "raw_adapter_factory",
        "policy_spec",
        "operational_policy",
        "first_dip_detector_policy",
        "wall_clock",
        "quote_max_age_seconds",
        "account_max_age_seconds",
        "context_max_age_seconds",
    )
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    source = inspect.getsource(build_capture_backed_paper_service_material_factory)
    assert "_CapturedPaperCoordinatorObservedInputs(" in source
    assert "candidate_inputs=observed.candidate" in source
    assert "observation_inputs=observed.observation" in source
    compatibility = inspect.signature(
        build_live_fsm_captured_paper_service_material_factory
    )
    assert tuple(compatibility.parameters) == (
        "host",
        "settings",
        "settings_projection_sha256",
        "raw_adapter_factory",
        "policy_spec",
        "operational_policy",
        "wall_clock",
        "quote_max_age_seconds",
        "account_max_age_seconds",
    )
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in compatibility.parameters.values()
    )


def test_market_session_capture_does_not_consult_process_global_tradeability_cache() -> None:
    source = inspect.getsource(
        _CapturedPaperCoordinatorObservedInputs._market_session_read
    )
    assert "is_tradeable_now(" not in source
    assert "_is_24h_eligible(" not in source
    assert "overnight_tradeability_claim" in source


def test_admission_eligibility_is_exact_symbol_fresh_and_tradeable() -> None:
    policy = FirstDipTapePolicy(
        window_seconds=10,
        max_source_age_seconds=2,
        tick_rate_floor_pctile=0.5,
        minimum_prints=3,
    )
    observed = _CapturedPaperCoordinatorObservedInputs(
        wall_clock=lambda: NOW,
        context_max_age_seconds=30,
        first_dip_detector_policy=policy,
    )
    coordinator = _Coordinator()
    request = _request(coordinator)

    class Adapter:
        def __init__(self, product: NormalizedProduct) -> None:
            self.product = product

        def capture_product_eligibility(self, _symbol):
            return self.product, FreshnessMeta(
                retrieved_at_utc=NOW - timedelta(milliseconds=1),
                provider_time_utc=NOW - timedelta(milliseconds=2),
                max_age_seconds=5,
            )

    good = NormalizedProduct(
        product_id=request.symbol,
        base_currency=request.symbol,
        quote_currency="USD",
        status="active",
        trading_disabled=False,
        cancel_only=False,
        limit_only=True,
        post_only=False,
        auction_mode=False,
        product_type="equity",
    )
    read = observed._eligibility_read(
        request=request,
        adapter=Adapter(good),
    )
    assert read.stream is CaptureStream.ADMISSION_ELIGIBILITY
    assert read.results[0].payload["symbol"] == request.symbol

    for invalid in (
        NormalizedProduct(
            **{**good.__dict__, "product_id": "WRONG"}
        ),
        NormalizedProduct(
            **{**good.__dict__, "trading_disabled": True}
        ),
        NormalizedProduct(
            **{**good.__dict__, "quote_currency": "EUR"}
        ),
        NormalizedProduct(
            **{**good.__dict__, "product_type": "crypto"}
        ),
    ):
        with pytest.raises(
            CapturedPaperProductionMaterialUnavailable,
            match="production_admission_eligibility_rejected",
        ):
            observed._eligibility_read(
                request=request,
                adapter=Adapter(invalid),
            )


def _runtime_scope_fixture(tmp_path, *, decision_id: str = "paper-tick-1"):
    coordinator, _startup, wall = _coordinator(
        tmp_path,
        certification_symbol="ACTU",
        extra_streams=(
            CaptureStream.IQFEED_PRINT,
            CaptureStream.PROVIDER_OHLCV,
            CaptureStream.SCANNER_SNAPSHOT,
        ),
    )
    wall.set(CAPTURE_BASE + timedelta(seconds=3))
    predecision = coordinator.capture_latest_durable_state_read(
        decision_id=decision_id,
        stream=CaptureStream.CONFIG_SNAPSHOT,
        returned_at=wall.now,
        max_source_age_seconds=60,
    )
    from app.services.trading.momentum_neural.universe import EQUITY_ROSS_SMALLCAP

    live_profile = EQUITY_ROSS_SMALLCAP
    profile = CaptureScannerProfile(
        profile_id=live_profile.profile_id,
        asset_class=live_profile.asset_class,
        price_min=live_profile.price_min,
        price_max=live_profile.price_max,
        min_dollar_volume=live_profile.min_dollar_volume,
        min_change_pct=live_profile.min_change_pct,
        snapshot_max_age_seconds=live_profile.snapshot_max_age_seconds,
    )
    scanner = LiveScannerSnapshotCaptureBridge(
        coordinator=coordinator,
        decision_id=decision_id,
        profile=profile,
        include_otc=False,
    )
    ohlcv = LiveOhlcvCaptureBridge(
        coordinator=coordinator,
        decision_id=decision_id,
        macro_cache={},
    )
    micro = LiveMicrostructureCaptureBridge(
        coordinator=coordinator,
        decision_id=decision_id,
    )
    return (
        coordinator,
        wall,
        _CapturedPaperExactRuntimeInputScope((predecision,)),
        scanner,
        ohlcv,
        micro,
    )


def test_exact_runtime_scope_requires_same_decision_bridges_and_resets_context(
    tmp_path,
) -> None:
    from app.services.trading.momentum_neural import risk_evaluator

    coordinator, _wall, scope, scanner, ohlcv, micro = _runtime_scope_fixture(
        tmp_path / "same-decision"
    )
    assert risk_evaluator._captured_live_scanner_snapshot_required() is False
    with scanner.install(), ohlcv.install(), micro.install(), scope.install():
        assert risk_evaluator._captured_live_scanner_snapshot_required() is True
    assert risk_evaluator._captured_live_scanner_snapshot_required() is False

    wrong_scanner = LiveScannerSnapshotCaptureBridge(
        coordinator=coordinator,
        decision_id="wrong-decision",
        profile=scanner.profile,
        include_otc=False,
    )
    fresh_ohlcv = LiveOhlcvCaptureBridge(
        coordinator=coordinator,
        decision_id="paper-tick-1",
        macro_cache={},
    )
    fresh_micro = LiveMicrostructureCaptureBridge(
        coordinator=coordinator,
        decision_id="paper-tick-1",
    )
    with wrong_scanner.install(), fresh_ohlcv.install(), fresh_micro.install():
        with pytest.raises(
            CapturedPaperProductionMaterialUnavailable,
            match="production_live_capture_bridge_scope_unavailable",
        ):
            with scope.install():
                pass
    assert risk_evaluator._captured_live_scanner_snapshot_required() is False


def test_captured_live_scanner_consumes_and_receipts_actual_massive_cache_result(
    tmp_path,
    monkeypatch,
) -> None:
    from app.services import massive_client
    from app.services.trading.momentum_neural import risk_evaluator
    from app.services.trading.momentum_neural.live_runner import replay_clock

    coordinator, wall, scope, scanner, ohlcv, micro = _runtime_scope_fixture(
        tmp_path / "massive-integration"
    )
    decision_at = CAPTURE_BASE + timedelta(seconds=4)
    provider_now = decision_at - timedelta(milliseconds=250)
    wall.set(decision_at)
    market_at = decision_at - timedelta(seconds=1)
    epoch_ns = int(market_at.timestamp() * 1_000_000_000)

    class _DeterministicMassiveDatetime:
        @classmethod
        def now(cls, tz=None):
            return provider_now if tz is not None else provider_now.replace(tzinfo=None)

    monkeypatch.setattr(massive_client, "datetime", _DeterministicMassiveDatetime)
    monkeypatch.setattr(
        massive_client,
        "_snapshot_cache",
        (
            time.time(),
            [
                {
                    "ticker": "ACTU",
                    "todaysChangePerc": 31.0,
                    "updated": epoch_ns,
                    "lastTrade": {"p": 3.00, "t": epoch_ns},
                    "day": {"c": 3.00, "vw": 2.75, "v": 500_000.0},
                    "min": {"c": 3.00, "av": 600_000.0},
                }
            ],
        ),
    )
    with (
        replay_clock(decision_at),
        scanner.install(),
        ohlcv.install(),
        micro.install(),
        scope.install(),
    ):
        rows = risk_evaluator._ross_risk_snapshot_rows("ACTU")
        assert rows["ACTU"]["lastTrade"]["p"] == 3.00
        assert risk_evaluator._captured_live_scanner_snapshot_required() is True
    assert risk_evaluator._captured_live_scanner_snapshot_required() is False
    assert len(scanner.captured_reads) == 1
    assert scanner.captured_reads[0].receipt is not None
    assert scanner.captured_reads[0].receipt.decision_id == "paper-tick-1"
    assert scanner.captured_reads[0].receipt.stream is CaptureStream.SCANNER_SNAPSHOT
    assert coordinator.health()["network_fallback_allowed"] is False


def test_captured_live_scanner_rejects_simultaneous_replay_authority(
    tmp_path,
) -> None:
    from app.services.trading.momentum_neural import risk_evaluator
    from app.services.trading.momentum_neural.live_runner import replay_clock

    _coordinator_row, wall, scope, scanner, ohlcv, micro = _runtime_scope_fixture(
        tmp_path / "ambiguous-scanner-authority"
    )
    decision_at = CAPTURE_BASE + timedelta(seconds=4)
    wall.set(decision_at)
    with (
        replay_clock(decision_at),
        scanner.install(),
        ohlcv.install(),
        micro.install(),
        scope.install(),
        risk_evaluator.replay_scanner_snapshot_provider(
            lambda *_args, **_kwargs: {"ticker": "ACTU"}
        ),
    ):
        with pytest.raises(
            risk_evaluator.ReplayScannerSnapshotUnavailableError,
            match="authority is ambiguous",
        ):
            risk_evaluator._ross_risk_snapshot_rows("ACTU")


def test_exact_runtime_scope_latches_a_swallowed_ohlcv_capture_failure(
    tmp_path,
) -> None:
    _coordinator_row, wall, scope, scanner, ohlcv, micro = _runtime_scope_fixture(
        tmp_path / "ohlcv-latch"
    )
    with scanner.install(), ohlcv.install(), micro.install():
        with pytest.raises(
            CapturedPaperProductionMaterialUnavailable,
            match="production_live_capture_read_failed:provider_ohlcv_fetch_failed",
        ):
            with scope.install():
                # This simulates a legacy feature helper swallowing the fetch
                # exception.  The sticky bridge failure must still veto the
                # staged post-commit request when the capability scope exits.
                assert ohlcv.on_ohlcv_failure(
                    ticker="ACTU",
                    interval="15m",
                    period="5d",
                    requested_at=wall.now,
                    failed_at=wall.now,
                    allow_provider_fallback=True,
                    error=RuntimeError("provider unavailable"),
                ) in {True, False}


def test_cold_observation_does_not_require_prior_scanner_or_ohlcv_and_first_dip_is_local(
    tmp_path,
) -> None:
    coordinator, _startup, wall = _coordinator(
        tmp_path / "cold-observation",
        certification_symbol="ACTU",
        extra_streams=(
            CaptureStream.HALT_LULD_STATE,
            CaptureStream.IQFEED_PRINT,
        ),
    )
    wall.set(CAPTURE_BASE + timedelta(seconds=3))
    request = _request(coordinator)
    risk_snapshot = {"momentum_live_execution": {}}
    viability_payload = {"symbol": request.symbol, "live_eligible": True}
    variant_payload = {"id": 7, "name": "paper"}
    arm_marker = {"generation": "test-arm"}
    hashes = {
        "risk_snapshot_sha256": sha256_json(risk_snapshot),
        "viability_payload_sha256": sha256_json(viability_payload),
        "variant_payload_sha256": sha256_json(variant_payload),
        "confirmed_arm_marker_sha256": sha256_json(arm_marker),
    }
    updated_at = wall.now - timedelta(milliseconds=10)
    generation = captured_paper_observation_generation_sha256(
        session_id=request.session_id,
        symbol=request.symbol,
        execution_family=request.execution_family,
        state="watching_live",
        correlation_id="cold-observation",
        variant_id=7,
        session_updated_at=updated_at,
        **hashes,
    )
    snapshot = CapturedPaperDurableObservationSnapshot(
        dispatch_provenance_sha256=request.provenance_sha256,
        session_id=request.session_id,
        symbol=request.symbol,
        execution_family=request.execution_family,
        state="watching_live",
        correlation_id="cold-observation",
        variant_id=7,
        session_updated_at=updated_at,
        risk_snapshot=risk_snapshot,
        viability_payload=viability_payload,
        variant_payload=variant_payload,
        confirmed_arm_marker=arm_marker,
        observation_generation_sha256=generation,
        **hashes,
    )
    policy = FirstDipTapePolicy(
        window_seconds=10,
        max_source_age_seconds=2,
        tick_rate_floor_pctile=0.5,
        minimum_prints=3,
    )
    observed = _CapturedPaperCoordinatorObservedInputs(
        wall_clock=wall,
        context_max_age_seconds=30,
        first_dip_detector_policy=policy,
    )

    class Adapter:
        def capture_product_eligibility(self, symbol):
            return NormalizedProduct(
                product_id=symbol,
                base_currency=symbol,
                quote_currency="USD",
                status="active",
                trading_disabled=False,
                cancel_only=False,
                limit_only=True,
                post_only=False,
                auction_mode=False,
                product_type="equity",
            ), FreshnessMeta(
                retrieved_at_utc=wall.now,
                provider_time_utc=wall.now,
                max_age_seconds=5,
            )

    material = observed.observation(
        request=request,
        observation=snapshot,
        coordinator=coordinator,
        adapter=Adapter(),
        account_read=None,  # unused: adapter-owned read is added by the service
        bbo_read=None,  # unused: adapter-owned read is added by the service
    )
    assert CaptureStream.SCANNER_SNAPSHOT.value not in material.existing_reads
    assert CaptureStream.PROVIDER_OHLCV.value not in material.existing_reads
    assert material.first_dip_tape_key is None
    assert material.first_dip_detector_policy is None
    halt_rows = tuple(
        row for row in material.reads if row.stream is CaptureStream.HALT_LULD_STATE
    )
    assert len(halt_rows) == 1
    assert halt_rows[0].results[0].payload["external_exchange_halt_status"] == (
        "not_inspected"
    )


def test_candidate_snapshot_reader_does_not_duplicate_live_eligibility_policy() -> None:
    from app.services.trading.momentum_neural.live_runner import (
        read_captured_paper_durable_candidate,
    )

    source = inspect.getsource(read_captured_paper_durable_candidate)
    assert "not bool(via.live_eligible)" not in source
    assert "if via is None:" in source


def test_adaptive_economics_are_derived_from_exact_bbo_ohlcv_and_scanner(
    tmp_path,
) -> None:
    coordinator, _startup, wall = _coordinator(
        tmp_path / "economic-derivation",
        certification_symbol="ACTU",
        extra_streams=(
            CaptureStream.ALPACA_NBBO_QUOTE,
            CaptureStream.PROVIDER_OHLCV,
            CaptureStream.SCANNER_SNAPSHOT,
        ),
    )
    request = _request(coordinator)
    candidate = _candidate(request)
    decision_id = candidate.client_order_id
    decision_at = CAPTURE_BASE + timedelta(seconds=10)
    wall.set(decision_at)
    bbo = coordinator.capture_query_result(
        decision_id=decision_id,
        stream=CaptureStream.ALPACA_NBBO_QUOTE,
        provider="alpaca",
        query={"operation": "latest_quote", "symbol": request.symbol},
        requested_at=decision_at - timedelta(milliseconds=3),
        returned_at=decision_at,
        results=(
            ObservedCaptureInput(
                payload={
                    "symbol": request.symbol,
                    "bid": 2.98,
                    "ask": 3.00,
                    "bid_size": 1_200.0,
                    "ask_size": 900.0,
                    "size_unit": "shares",
                    "feed": "iex",
                },
                clocks=CaptureClocks(
                    received_at=decision_at - timedelta(milliseconds=1),
                    available_at=decision_at,
                    provider_event_at=decision_at - timedelta(milliseconds=2),
                    market_reference_at=decision_at - timedelta(milliseconds=2),
                ),
            ),
        ),
        symbol=request.symbol,
    )
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    frame_rows: list[dict[str, float]] = []
    frame_times: list[datetime] = []
    for trading_date, bar_volume in (
        (date(2026, 7, 9), 4_000.0),  # oldest 5d boundary: dropped
        (date(2026, 7, 10), 5_000.0),
        (date(2026, 7, 13), 6_000.0),
    ):
        session_open = datetime(
            trading_date.year,
            trading_date.month,
            trading_date.day,
            9,
            30,
            tzinfo=eastern,
        )
        for index in range(26):
            frame_times.append(
                (session_open + timedelta(minutes=15 * index)).astimezone(
                    timezone.utc
                )
            )
            frame_rows.append(
                {
                    "Open": 2.95,
                    "High": 3.05,
                    "Low": 2.90,
                    "Close": 3.00,
                    "Volume": bar_volume,
                }
            )
    current_open = datetime(2026, 7, 14, 14, 45, tzinfo=eastern)
    for index in range(6):
        frame_times.append(
            (current_open + timedelta(minutes=15 * index)).astimezone(
                timezone.utc
            )
        )
        # 16:00 ET has not closed at the 16:00:09 ET receipt frontier.  Its
        # huge range proves the economics path excludes partial current bars.
        partial = index == 5
        frame_rows.append(
            {
                "Open": 3.00,
                "High": 9.00 if partial else 3.10,
                "Low": 0.50 if partial else 2.90,
                "Close": 3.00,
                "Volume": 15_000.0,
            }
        )
    frame = pd.DataFrame(frame_rows, index=pd.DatetimeIndex(frame_times))
    frame.attrs.update(
        {
            "provider": "massive",
            "fetched_at_utc": (decision_at - timedelta(seconds=2)).isoformat(),
            "ticker": request.symbol,
            "interval": "15m",
            "integrity_ok": True,
            "cache_hit": False,
            "cache_age_seconds": 0.0,
        }
    )
    ohlcv_bridge = LiveOhlcvCaptureBridge(
        coordinator=coordinator,
        decision_id=decision_id,
        macro_cache={},
    )
    with ohlcv_bridge.install():
        accepted = ohlcv_bridge.on_ohlcv_result(
            ticker=request.symbol,
            interval="15m",
            period="5d",
            requested_at=decision_at - timedelta(seconds=3),
            returned_at=decision_at - timedelta(seconds=1),
            allow_provider_fallback=True,
            frame=frame,
        )
        assert accepted, ohlcv_bridge.capture_failure_reason
    profile = CaptureScannerProfile(
        profile_id="equity_ross_smallcap",
        asset_class="equity",
        price_min=1.0,
        price_max=20.0,
        min_dollar_volume=1_000_000.0,
        min_change_pct=5.0,
        snapshot_max_age_seconds=300.0,
    )
    scanner_bridge = LiveScannerSnapshotCaptureBridge(
        coordinator=coordinator,
        decision_id=decision_id,
        profile=profile,
    )
    market_at = decision_at - timedelta(seconds=2)
    epoch_ns = int(market_at.timestamp() * 1_000_000_000)
    with scanner_bridge.install():
        assert scanner_bridge.on_massive_full_snapshot(
            include_otc=False,
            max_age_seconds=300.0,
            provider_cache_ttl_seconds=300.0,
            requested_at=decision_at - timedelta(seconds=3),
            returned_at=decision_at - timedelta(seconds=1),
            cache_hit=False,
            cache_age_seconds=None,
            rows=[
                {
                    "ticker": request.symbol,
                    "todaysChangePerc": 31.0,
                    "updated": epoch_ns,
                    "lastTrade": {"p": 3.00, "t": epoch_ns},
                    "day": {"c": 3.00, "vw": 2.75, "v": 500_000.0},
                    "min": {"c": 3.00, "av": 600_000.0},
                }
            ],
        )

    economics, cluster = _CapturedPaperCoordinatorObservedInputs._economics(
        request,
        candidate,
        bbo,
        ohlcv_bridge.captured_reads[0],
        scanner_bridge.captured_reads[0],
    )
    half_spread_bps = (3.00 - 2.98) / ((3.00 + 2.98) / 2.0) * 5_000.0
    assert economics.entry_slippage_bps == pytest.approx(half_spread_bps)
    assert economics.exit_slippage_bps == pytest.approx(half_spread_bps)
    assert economics.fees_per_share_usd == 0.0
    assert economics.executable_depth_shares == 900.0
    assert economics.recent_volume_shares == 600_000.0
    assert economics.average_daily_volume_shares == pytest.approx(143_000.0)
    assert economics.candidate_buying_power_impact_per_share_usd == 3.00
    assert 0.0 < economics.realized_volatility_fraction < 0.20
    assert cluster == "equity:smallcap-momentum"
    scanner_receipt = scanner_bridge.captured_reads[0].receipt
    assert scanner_receipt is not None
    assert CaptureStream.SCANNER_SNAPSHOT.value in (
        _CANDIDATE_FACT_READ_KEYS["candidate_buying_power_estimate"]
    )
    buying_power_source = (
        CapturedPaperCaptureBackedSupplementProviders._fact_source(
            name="candidate_buying_power_estimate",
            keys=_CANDIDATE_FACT_READ_KEYS[
                "candidate_buying_power_estimate"
            ],
            captured_by_key={
                "candidate_snapshot": bbo,
                CaptureStream.SCANNER_SNAPSHOT.value: (
                    scanner_bridge.captured_reads[0]
                ),
            },
            coordinator=coordinator,
        )
    )
    assert scanner_receipt.read_id in buying_power_source.source_read_ids

    valid_ohlcv_event = ohlcv_bridge.captured_reads[0].source_events[0]
    forged_query = dict(valid_ohlcv_event.query or {})
    forged_query["schema_version"] = "forged.ohlcv-query.v999"
    forged_payload = dict(valid_ohlcv_event.payload)
    forged_payload["query_sha256"] = sha256_json(forged_query)
    forged_schema_read = coordinator.capture_query_result(
        decision_id=decision_id,
        stream=CaptureStream.PROVIDER_OHLCV,
        provider=valid_ohlcv_event.provider,
        query=forged_query,
        requested_at=decision_at - timedelta(seconds=3),
        returned_at=decision_at,
        results=(
            ObservedCaptureInput(
                payload=forged_payload,
                clocks=CaptureClocks(
                    received_at=decision_at - timedelta(seconds=2),
                    available_at=decision_at,
                    market_reference_at=(
                        valid_ohlcv_event.clocks.market_reference_at
                    ),
                ),
            ),
        ),
        symbol=request.symbol,
    )
    with pytest.raises(
        CapturedPaperProductionMaterialUnavailable,
        match="production_adaptive_risk_economics_coverage_unavailable",
    ):
        _CapturedPaperCoordinatorObservedInputs._economics(
            request,
            candidate,
            bbo,
            forged_schema_read,
            scanner_bridge.captured_reads[0],
        )

    partial_query = dict(valid_ohlcv_event.query or {})
    partial_rows = []
    for index in range(5):
        bar_start = decision_at + timedelta(minutes=15 * index)
        partial_rows.append(
            {
                "market_reference_at": bar_start.isoformat(),
                "open": 3.0,
                "high": 3.1,
                "low": 2.9,
                "close": 3.0,
                "volume": 1_000.0,
            }
        )
    all_partial_read = coordinator.capture_query_result(
        decision_id=decision_id,
        stream=CaptureStream.PROVIDER_OHLCV,
        provider=valid_ohlcv_event.provider,
        query=partial_query,
        requested_at=decision_at - timedelta(seconds=3),
        returned_at=decision_at,
        results=(
            ObservedCaptureInput(
                payload={
                    "schema_version": valid_ohlcv_event.payload[
                        "schema_version"
                    ],
                    "query_sha256": sha256_json(partial_query),
                    "rows": partial_rows,
                },
                clocks=CaptureClocks(
                    received_at=decision_at - timedelta(seconds=2),
                    available_at=decision_at,
                    market_reference_at=decision_at + timedelta(hours=1),
                ),
            ),
        ),
        symbol=request.symbol,
    )
    with pytest.raises(
        CapturedPaperProductionMaterialUnavailable,
        match="production_adaptive_risk_economics_coverage_unavailable",
    ):
        _CapturedPaperCoordinatorObservedInputs._economics(
            request,
            candidate,
            bbo,
            all_partial_read,
            scanner_bridge.captured_reads[0],
        )
