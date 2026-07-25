from __future__ import annotations

import hashlib
from types import MappingProxyType, SimpleNamespace
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.momentum_neural import viability as viability_module
from app.services.trading.momentum_neural.captured_viability_adapter import (
    COVERAGE_UNAVAILABLE,
    REQUIRED_COMPONENTS,
    SCORED,
    CapturedViabilityDependencyBinding,
    CapturedViabilityDependencyInventory,
    CapturedViabilityContractError,
    CapturedViabilityInputBundle,
    CapturedViabilityPostScoreAdjustment,
    CapturedViabilityScoreResult,
    CapturedViabilityScoringAuthority,
    captured_viability_component_sha256s,
    captured_viability_read_receipt_sha256,
    score_captured_viability,
)
from app.services.trading.momentum_neural.context import (
    build_momentum_regime_context,
)
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureStream,
    CoverageGap,
    FSMDependencyProfile,
    FSMStreamDependency,
    ProviderWatermark,
    StreamCoverage,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import (
    ViabilityExternalInputs,
    ViabilitySettingsProjection,
    score_viability_explicit,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
READ_ID = "11111111-1111-4111-8111-111111111111"


def h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _external(**changes) -> ViabilityExternalInputs:
    row = ViabilityExternalInputs(
        leveraged_etf=False,
        excluded_fund=False,
        symbol_family_memory_adjust=0.0,
        dilution_history_derate=0.0,
        ross_rvol=None,
        ross_change_pct=None,
        ross_float_shares=None,
        squeeze_fuel_rank_pct=None,
        below_explosive_floor=False,
        catalyst_delta=0.0,
        catalyst_grade_delta=0.0,
        fake_catalyst_delta=0.0,
        sympathy_delta=0.0,
        theme_sympathy_delta=0.0,
        close_strength_delta=0.0,
        thick_tape_delta=0.0,
        nonmonotonic_volume_delta=0.0,
        ross_quality_viability_tilt=0.20,
    )
    return replace(row, **changes)


def _fixture() -> tuple[
    CapturedViabilityInputBundle,
    CapturedViabilityScoringAuthority,
    datetime,
]:
    identity = h("capture-identity")
    policy = h("intended-policy")
    config = h("config")
    code = h("code")
    query_payload = {
        "provider": "massive_rest",
        "symbol": "VEEE",
        "interval": "1m",
        "start": (BASE - timedelta(minutes=30)).isoformat(),
        "end": BASE.isoformat(),
        "adjusted": True,
    }
    query = sha256_json(query_payload)
    family = get_family("impulse_breakout")
    assert family is not None
    context = build_momentum_regime_context(
        now=BASE,
        atr_pct=0.015,
        meta={
            "ross_scores": {"VEEE": 0.72},
            "catalyst_symbols": {"VEEE"},
            "theme_symbols": frozenset({"VEEE"}),
        },
    )
    features = ExecutionReadinessFeatures.from_meta(
        {
            "spread_bps": 40.0,
            "ofi": 0.6,
            "micro_price_edge": 5.0,
            "trade_flow": 0.7,
            "product_tradable": True,
            "ross_signals": {},
        }
    )
    settings = replace(
        ViabilitySettingsProjection.from_runtime(SimpleNamespace()),
        chili_momentum_live_eligible_max_spread_bps=25.0,
        chili_momentum_live_eligible_allow_extreme_explosive=False,
    )
    external = _external(catalyst_delta=0.04)
    post_score_adjustment = CapturedViabilityPostScoreAdjustment(
        tenbeat_entry_tilt_weight=0.03,
        tenbeat_breakout_score=None,
        lookup_status="inapplicable_non_crypto",
        source_read_id=None,
    )

    config_ref = CaptureEventRef(
        identity_sha256=identity,
        event_sha256=h("config-event"),
        sequence=1,
        stream=CaptureStream.CONFIG_SNAPSHOT,
        received_at=BASE - timedelta(seconds=10),
        available_at=BASE - timedelta(seconds=10),
        payload_sha256=config,
        provider="runtime_config",
    )
    policy_ref = CaptureEventRef(
        identity_sha256=identity,
        event_sha256=h("policy-event"),
        sequence=2,
        stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
        received_at=BASE - timedelta(seconds=10),
        available_at=BASE - timedelta(seconds=10),
        payload_sha256=policy,
        provider="feature_flags",
    )
    code_ref = CaptureEventRef(
        identity_sha256=identity,
        event_sha256=h("code-event"),
        sequence=3,
        stream=CaptureStream.CODE_BUILD,
        received_at=BASE - timedelta(seconds=10),
        available_at=BASE - timedelta(seconds=10),
        payload_sha256=code,
        provider="code_build",
    )
    ohlcv_ref = CaptureEventRef(
        identity_sha256=identity,
        event_sha256=h("ohlcv-event"),
        sequence=4,
        stream=CaptureStream.PROVIDER_OHLCV,
        received_at=BASE - timedelta(milliseconds=900),
        available_at=BASE - timedelta(milliseconds=700),
        payload_sha256=h("ohlcv-payload"),
        query_sha256=query,
        provider="massive_rest",
        symbol="VEEE",
        provider_event_at=BASE - timedelta(seconds=1),
        market_reference_at=BASE - timedelta(seconds=1),
    )
    print_ref = CaptureEventRef(
        identity_sha256=identity,
        event_sha256=h("iqfeed-print-event"),
        sequence=5,
        stream=CaptureStream.IQFEED_PRINT,
        received_at=BASE - timedelta(milliseconds=850),
        available_at=BASE - timedelta(milliseconds=650),
        payload_sha256=h("iqfeed-print-payload"),
        provider="iqfeed",
        symbol="VEEE",
        provider_event_at=BASE - timedelta(milliseconds=950),
    )
    receipt = CaptureReadReceipt(
        read_id=READ_ID,
        decision_id="selection:VEEE:101",
        identity_sha256=identity,
        stream=CaptureStream.PROVIDER_OHLCV,
        provider="massive_rest",
        symbol="VEEE",
        requested_at=BASE - timedelta(milliseconds=650),
        returned_at=BASE - timedelta(milliseconds=500),
        query_sha256=query,
        source_event_sha256s=(ohlcv_ref.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256((ohlcv_ref,)),
        content_verified=True,
        replay_network_fallback_used=False,
        query=query_payload,
    )
    coverage_start = BASE - timedelta(seconds=30)
    profile = FSMDependencyProfile(
        required_streams=frozenset(
            {
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.CODE_BUILD,
                CaptureStream.PROVIDER_OHLCV,
                CaptureStream.IQFEED_PRINT,
            }
        ),
        required_read_ids=(READ_ID,),
        stream_dependencies=tuple(
            FSMStreamDependency(
                stream=stream,
                exact_provider_event_at_required=(
                    stream is CaptureStream.IQFEED_PRINT
                ),
                market_reference_at_required=(
                    stream is CaptureStream.PROVIDER_OHLCV
                ),
                max_source_age_seconds=300.0,
                coverage_start_at=coverage_start,
            )
            for stream in (
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.CODE_BUILD,
                CaptureStream.PROVIDER_OHLCV,
                CaptureStream.IQFEED_PRINT,
            )
        ),
    )
    watermark = ProviderWatermark(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        identity_sha256=identity,
        symbol="VEEE",
        event_watermark_at=BASE,
        emitted_available_at=BASE + timedelta(milliseconds=100),
        bounded_lateness_seconds=1.0,
        max_observed_lateness_seconds=0.2,
        generation=1,
    )
    coverages = (
        StreamCoverage(
            stream=CaptureStream.CONFIG_SNAPSHOT,
            identity_sha256=identity,
            provider="runtime_config",
            first_available_at=coverage_start,
            last_available_at=config_ref.available_at,
            event_count=1,
            exact_event_clock_complete=False,
            content_verified=True,
            continuity_complete=True,
        ),
        StreamCoverage(
            stream=CaptureStream.CODE_BUILD,
            identity_sha256=identity,
            provider="code_build",
            first_available_at=coverage_start,
            last_available_at=code_ref.available_at,
            event_count=1,
            exact_event_clock_complete=False,
            content_verified=True,
            continuity_complete=True,
        ),
        StreamCoverage(
            stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
            identity_sha256=identity,
            provider="feature_flags",
            first_available_at=coverage_start,
            last_available_at=policy_ref.available_at,
            event_count=1,
            exact_event_clock_complete=False,
            content_verified=True,
            continuity_complete=True,
        ),
        StreamCoverage(
            stream=CaptureStream.PROVIDER_OHLCV,
            identity_sha256=identity,
            provider="massive_rest",
            symbol="VEEE",
            first_available_at=coverage_start,
            last_available_at=receipt.returned_at,
            event_count=1,
            exact_event_clock_complete=False,
            content_verified=True,
            continuity_complete=True,
            query_receipt_count=1,
        ),
        StreamCoverage(
            stream=CaptureStream.IQFEED_PRINT,
            identity_sha256=identity,
            provider="iqfeed",
            symbol="VEEE",
            first_available_at=coverage_start,
            last_available_at=watermark.emitted_available_at,
            event_count=1,
            exact_event_clock_complete=True,
            content_verified=True,
            continuity_complete=True,
            watermark=watermark,
        ),
    )
    roots = captured_viability_component_sha256s(
        symbol="VEEE",
        variant_id=101,
        family=family,
        context=context,
        features=features,
        settings=settings,
        external=external,
        post_score_adjustment=post_score_adjustment,
        event_at=BASE,
        available_at=BASE + timedelta(milliseconds=200),
        read_at=BASE + timedelta(milliseconds=300),
        capture_identity_sha256=identity,
        policy_sha256=policy,
        config_sha256=config,
        code_sha256=code,
    )
    receipt_sha = captured_viability_read_receipt_sha256(receipt)
    bindings = []
    for component in REQUIRED_COMPONENTS:
        if component in {"settings_projection", "post_score_adjustment", "config"}:
            event_hashes = (config_ref.event_sha256,)
            receipt_hashes = ()
        elif component == "policy":
            event_hashes = (policy_ref.event_sha256,)
            receipt_hashes = ()
        elif component in {"variant", "code_build"}:
            event_hashes = (code_ref.event_sha256,)
            receipt_hashes = ()
        elif component in {
            "execution_readiness",
            "external_inputs",
            "regime_context",
        }:
            event_hashes = (ohlcv_ref.event_sha256, print_ref.event_sha256)
            receipt_hashes = (receipt_sha,)
        else:
            event_hashes = (
                config_ref.event_sha256,
                policy_ref.event_sha256,
                code_ref.event_sha256,
                ohlcv_ref.event_sha256,
                print_ref.event_sha256,
            )
            receipt_hashes = (receipt_sha,)
        bindings.append(
            CapturedViabilityDependencyBinding(
                component=component,
                component_sha256=roots[component],
                source_event_sha256s=event_hashes,
                read_receipt_sha256s=receipt_hashes,
            )
        )
    inventory = CapturedViabilityDependencyInventory(
        dependency_profile=profile,
        bindings=tuple(bindings),
    )
    bundle = CapturedViabilityInputBundle(
        source_sequence=101,
        event_at=BASE,
        available_at=BASE + timedelta(milliseconds=200),
        read_at=BASE + timedelta(milliseconds=300),
        symbol="VEEE",
        variant_id=101,
        family=family,
        context=context,
        features=features,
        settings=settings,
        external=external,
        post_score_adjustment=post_score_adjustment,
        capture_identity_sha256=identity,
        policy_sha256=policy,
        config_sha256=config,
        code_sha256=code,
        dependency_inventory=inventory,
        source_refs=(config_ref, policy_ref, code_ref, ohlcv_ref, print_ref),
        read_receipts=(receipt,),
        stream_coverages=coverages,
        coverage_gaps=(),
        correlation_id="captured-selection-VEEE-101",
    )
    authority = CapturedViabilityScoringAuthority(
        capture_identity_sha256=identity,
        policy_sha256=policy,
        config_sha256=config,
        code_sha256=code,
        settings_projection_sha256=bundle.settings_projection_sha256,
        family_sha256=bundle.component_roots["family"],
        dependency_profile_sha256=profile.profile_sha256,
        activation_policy_sha256=h("activation-policy"),
        activation_settings_projection_sha256=h("activation-settings"),
        activation_code_build_sha256=h("activation-code-build"),
        selection_authority_sha256=h("selection-authority"),
        variant_id=101,
        family_id=family.family_id,
        family_version=family.version,
    )
    return bundle, authority, bundle.read_at


def test_typed_and_serialized_paths_are_identical_and_lossless() -> None:
    bundle, authority, evaluation_at = _fixture()
    typed = score_captured_viability(
        bundle, authority=authority, evaluation_at=evaluation_at
    )
    replay = score_captured_viability(
        deepcopy(bundle.to_dict()),
        authority=deepcopy(authority.to_dict()),
        evaluation_at=evaluation_at,
    )
    expected = score_viability_explicit(
        bundle.symbol,
        bundle.family,
        bundle.context,
        bundle.features,
        settings=bundle.settings,
        external=bundle.external,
    )

    assert typed.status == SCORED
    assert replay.status == SCORED
    assert typed.viability == expected == replay.viability
    assert typed.observation is not None and replay.observation is not None
    assert typed.observation.observation_sha256 == replay.observation.observation_sha256
    assert typed.bundle_sha256 == replay.bundle_sha256 == bundle.bundle_sha256
    assert typed.observation.explain_json["scorer_output"]["paper_eligible"] is True
    assert typed.observation.explain_json["scorer_output"]["live_eligible"] is False
    assert typed.observation.viability_score == round(expected.viability, 4)
    assert typed.observation.paper_eligible is False
    assert typed.observation.live_eligible is False
    assert typed.observation.explain_json["policy_parity"]["policy_parity"] is True
    # Frozen policy/canonicalization oracle.  Any intentional schema or economic
    # change must update all four roots together after deep review.
    assert bundle.bundle_sha256 == (
        "82dc13885232f4a73d1474ef1be0e3e117ef5445f4059d628733eb596dd914ec"
    )
    assert authority.authority_sha256 == (
        "6eb7dd438d755a9e6a534bc41fc4f6447b0679706f3e8ab5039a702edc03cac5"
    )
    assert bundle.dependency_inventory.inventory_sha256 == (
        "767dc53b96cf8223ef3c69f29d1e5f019dc3342ea5a33a56df21daac0c4aa59f"
    )
    assert typed.observation.observation_sha256 == (
        "d5da8713987f1adccc52357980b86658efb9898e6a5ecb473ef38b7ea41de7de"
    )


def test_post_score_adjustment_matches_pipeline_round_then_tilt_order() -> None:
    bundle, _authority, _evaluation_at = _fixture()
    raw = score_viability_explicit(
        bundle.symbol,
        bundle.family,
        bundle.context,
        bundle.features,
        settings=bundle.settings,
        external=bundle.external,
    )
    adjustment = CapturedViabilityPostScoreAdjustment(
        tenbeat_entry_tilt_weight=0.03,
        tenbeat_breakout_score=0.75,
        lookup_status="captured_value",
        source_read_id=READ_ID,
    )

    assert adjustment.persisted_viability(raw) == min(
        1.0, round(raw.viability, 4) + 0.03 * 0.75
    )
    assert adjustment.to_dict()["public_delta"] == round(0.03 * 0.75, 4)


def test_adapter_has_no_runtime_settings_classifier_or_db_fallback(monkeypatch) -> None:
    bundle, authority, evaluation_at = _fixture()

    def forbidden(*args, **kwargs):
        raise AssertionError("external fallback was consulted")

    monkeypatch.setattr(viability_module, "settings", object())
    monkeypatch.setattr(viability_module, "symbol_is_leveraged_etf", forbidden)
    monkeypatch.setattr(viability_module, "symbol_is_excluded_fund", forbidden)
    monkeypatch.setattr(viability_module, "_symbol_family_memory_adjust", forbidden)

    result = score_captured_viability(
        bundle, authority=authority, evaluation_at=evaluation_at
    )

    assert result.status == SCORED


@pytest.mark.parametrize("mutation", ["missing", "extra", "component_hash"])
def test_missing_extra_or_mismatched_dependency_fails_closed(mutation: str) -> None:
    bundle, authority, evaluation_at = _fixture()
    raw = deepcopy(bundle.to_dict())
    bindings = raw["dependency_inventory"]["bindings"]
    if mutation == "missing":
        bindings.pop()
    elif mutation == "extra":
        bindings.append(deepcopy(bindings[0]))
        bindings[-1]["component"] = "unknown_component"
    else:
        bindings[0]["component_sha256"] = h("forged-component")

    result = score_captured_viability(
        raw, authority=authority, evaluation_at=evaluation_at
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert result.observation is None
    assert result.opportunity_consumed is False
    assert result.risk_reserved is False
    assert result.order_posted is False


def test_nested_input_mutation_after_hashing_fails_closed() -> None:
    bundle, authority, evaluation_at = _fixture()
    bundle.context.meta["ross_scores"]["VEEE"] = 0.99

    result = score_captured_viability(
        bundle, authority=authority, evaluation_at=evaluation_at
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert "bundle_hash_mismatch" in result.reasons
    assert result.observation is None


def test_stale_stream_and_intersecting_gap_fail_closed() -> None:
    bundle, authority, evaluation_at = _fixture()
    stale_bundle = replace(bundle, read_at=BASE + timedelta(seconds=400))
    stale = score_captured_viability(
        stale_bundle,
        authority=authority,
        evaluation_at=stale_bundle.read_at,
    )
    gap_bundle = replace(
        bundle,
        coverage_gaps=(
            CoverageGap(
                stream=CaptureStream.IQFEED_PRINT,
                reason="bounded_queue_overflow",
                first_available_at=BASE - timedelta(seconds=1),
                last_available_at=BASE + timedelta(milliseconds=50),
                lost_count=1,
                symbol="VEEE",
            ),
        ),
    )
    gapped = score_captured_viability(
        gap_bundle,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert stale.status == COVERAGE_UNAVAILABLE
    assert any("stream_evidence_stale" in reason for reason in stale.reasons)
    assert gapped.status == COVERAGE_UNAVAILABLE
    assert any(reason.startswith("coverage_gap:iqfeed_print") for reason in gapped.reasons)


def test_evaluation_clock_must_be_the_content_bound_read_clock() -> None:
    bundle, authority, evaluation_at = _fixture()

    result = score_captured_viability(
        bundle,
        authority=authority,
        evaluation_at=evaluation_at + timedelta(microseconds=1),
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert result.reasons == ("evaluation_clock_not_bound_to_bundle_read",)


def test_missing_watermark_and_network_fallback_receipt_fail_closed() -> None:
    bundle, authority, evaluation_at = _fixture()
    coverages = tuple(
        replace(item, watermark=None)
        if item.stream is CaptureStream.IQFEED_PRINT
        else item
        for item in bundle.stream_coverages
    )
    no_watermark = replace(bundle, stream_coverages=coverages)
    unsafe_receipt = replace(
        bundle.read_receipts[0], replay_network_fallback_used=True
    )
    network_bundle = replace(bundle, read_receipts=(unsafe_receipt,))

    watermark_result = score_captured_viability(
        no_watermark, authority=authority, evaluation_at=evaluation_at
    )
    network_result = score_captured_viability(
        network_bundle, authority=authority, evaluation_at=evaluation_at
    )

    assert watermark_result.status == COVERAGE_UNAVAILABLE
    assert "provider_watermark_missing:iqfeed_print" in watermark_result.reasons
    assert network_result.status == COVERAGE_UNAVAILABLE
    assert any("receipt_network_fallback" in reason for reason in network_result.reasons)


def test_query_payload_and_source_query_binding_are_mandatory() -> None:
    bundle, authority, evaluation_at = _fixture()
    missing_payload = replace(bundle.read_receipts[0], query=None)
    payload_bundle = replace(bundle, read_receipts=(missing_payload,))
    ohlcv_ref = next(
        item
        for item in bundle.source_refs
        if item.stream is CaptureStream.PROVIDER_OHLCV
    )
    unbound_ref = replace(ohlcv_ref, query_sha256=None)
    ref_bundle = replace(
        bundle,
        source_refs=tuple(
            unbound_ref if item is ohlcv_ref else item
            for item in bundle.source_refs
        ),
    )

    payload_result = score_captured_viability(
        payload_bundle,
        authority=authority,
        evaluation_at=evaluation_at,
    )
    ref_result = score_captured_viability(
        ref_bundle,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert payload_result.status == COVERAGE_UNAVAILABLE
    assert any(
        reason.startswith("receipt_query_payload_missing:")
        for reason in payload_result.reasons
    )
    assert ref_result.status == COVERAGE_UNAVAILABLE
    assert any(
        reason.startswith("receipt_query_mismatch:")
        for reason in ref_result.reasons
    )


def test_coverage_proof_cannot_arrive_after_declared_bundle_availability() -> None:
    bundle, authority, evaluation_at = _fixture()
    print_coverage = next(
        item
        for item in bundle.stream_coverages
        if item.stream is CaptureStream.IQFEED_PRINT
    )
    assert print_coverage.watermark is not None
    late_watermark = replace(
        print_coverage.watermark,
        emitted_available_at=BASE + timedelta(milliseconds=250),
    )
    late_coverage = replace(
        print_coverage,
        last_available_at=late_watermark.emitted_available_at,
        watermark=late_watermark,
    )
    late_bundle = replace(
        bundle,
        stream_coverages=tuple(
            late_coverage if item is print_coverage else item
            for item in bundle.stream_coverages
        ),
    )

    result = score_captured_viability(
        late_bundle,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert "coverage_after_bundle_available:iqfeed_print" in result.reasons
    assert (
        "provider_watermark_after_bundle_available:iqfeed_print"
        in result.reasons
    )


def test_coverage_count_must_match_exact_bound_event_inventory() -> None:
    bundle, authority, evaluation_at = _fixture()
    coverages = tuple(
        replace(item, event_count=item.event_count + 1)
        if item.stream is CaptureStream.PROVIDER_OHLCV
        else item
        for item in bundle.stream_coverages
    )
    mismatched = replace(bundle, stream_coverages=coverages)

    result = score_captured_viability(
        mismatched,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert "coverage_event_count_mismatch:provider_ohlcv" in result.reasons


def test_verified_complete_quiet_continuous_stream_is_not_a_coverage_gap() -> None:
    bundle, authority, evaluation_at = _fixture()
    print_ref = next(
        item
        for item in bundle.source_refs
        if item.stream is CaptureStream.IQFEED_PRINT
    )
    bindings = tuple(
        CapturedViabilityDependencyBinding(
            component=row.component,
            component_sha256=row.component_sha256,
            source_event_sha256s=tuple(
                value
                for value in row.source_event_sha256s
                if value != print_ref.event_sha256
            ),
            read_receipt_sha256s=row.read_receipt_sha256s,
        )
        for row in bundle.dependency_inventory.bindings
    )
    inventory = CapturedViabilityDependencyInventory(
        dependency_profile=bundle.dependency_inventory.dependency_profile,
        bindings=bindings,
    )
    quiet_coverages = tuple(
        replace(item, event_count=0)
        if item.stream is CaptureStream.IQFEED_PRINT
        else item
        for item in bundle.stream_coverages
    )
    quiet = replace(
        bundle,
        dependency_inventory=inventory,
        source_refs=tuple(
            item for item in bundle.source_refs if item is not print_ref
        ),
        stream_coverages=quiet_coverages,
    )

    result = score_captured_viability(
        quiet,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert result.status == SCORED
    assert result.reasons == ()


def test_authority_mismatch_and_nonfinite_input_fail_closed() -> None:
    bundle, authority, evaluation_at = _fixture()
    wrong = replace(authority, policy_sha256=h("other-policy"))
    mismatched = score_captured_viability(
        bundle, authority=wrong, evaluation_at=evaluation_at
    )
    raw = deepcopy(bundle.to_dict())
    raw["external"]["catalyst_delta"] = float("inf")
    nonfinite = score_captured_viability(
        raw, authority=authority, evaluation_at=evaluation_at
    )

    assert mismatched.status == COVERAGE_UNAVAILABLE
    assert "authority_mismatch:policy_sha256" in mismatched.reasons
    assert nonfinite.status == COVERAGE_UNAVAILABLE
    assert nonfinite.reasons == ("bundle_contract_invalid",)


@pytest.mark.parametrize(
    ("scorer_field", "activation_field"),
    (
        ("policy_sha256", "activation_policy_sha256"),
        (
            "settings_projection_sha256",
            "activation_settings_projection_sha256",
        ),
        ("code_sha256", "activation_code_build_sha256"),
    ),
)
def test_scorer_and_activation_hash_domains_are_not_interchangeable(
    scorer_field: str,
    activation_field: str,
) -> None:
    bundle, authority, evaluation_at = _fixture()

    scorer_hash_replaced_by_activation_hash = replace(
        authority,
        **{scorer_field: getattr(authority, activation_field)},
    )
    scorer_result = score_captured_viability(
        bundle,
        authority=scorer_hash_replaced_by_activation_hash,
        evaluation_at=evaluation_at,
    )

    activation_hash_replaced_by_scorer_hash = replace(
        authority,
        **{activation_field: getattr(authority, scorer_field)},
    )
    activation_result = score_captured_viability(
        bundle,
        authority=activation_hash_replaced_by_scorer_hash,
        evaluation_at=evaluation_at,
    )

    assert scorer_result.status == COVERAGE_UNAVAILABLE
    assert f"authority_mismatch:{scorer_field}" in scorer_result.reasons
    # The scorer validates bundle-local authority.  The queue performs the
    # separate activation/selection cross-check where that context exists.
    assert activation_result.status == SCORED


def test_serialized_authority_requires_every_activation_binding() -> None:
    _bundle, authority, _evaluation_at = _fixture()
    raw = authority.to_dict()
    raw.pop("selection_authority_sha256")

    with pytest.raises(
        CapturedViabilityContractError,
        match="scoring_authority fields do not match schema",
    ):
        CapturedViabilityScoringAuthority.from_dict(raw)


def test_authority_pins_exact_family_semantics() -> None:
    bundle, authority, evaluation_at = _fixture()
    wrong = replace(authority, family_sha256=h("different-family-semantics"))

    result = score_captured_viability(
        bundle, authority=wrong, evaluation_at=evaluation_at
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert "authority_mismatch:family_sha256" in result.reasons


def test_config_policy_and_code_identity_streams_are_mandatory() -> None:
    bundle, authority, evaluation_at = _fixture()
    original = bundle.dependency_inventory.dependency_profile
    profile = FSMDependencyProfile(
        required_streams=frozenset(
            stream
            for stream in original.required_streams
            if stream
            not in {
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.CODE_BUILD,
            }
        ),
        required_read_ids=original.required_read_ids,
        stream_dependencies=tuple(
            row
            for row in original.stream_dependencies
            if row.stream
            not in {
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.CODE_BUILD,
            }
        ),
    )
    inventory = CapturedViabilityDependencyInventory(
        dependency_profile=profile,
        bindings=bundle.dependency_inventory.bindings,
    )
    unpinned = replace(bundle, dependency_inventory=inventory)
    matching_authority = replace(
        authority,
        dependency_profile_sha256=profile.profile_sha256,
    )

    result = score_captured_viability(
        unpinned,
        authority=matching_authority,
        evaluation_at=evaluation_at,
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert "required_identity_stream_missing:config_snapshot" in result.reasons
    assert (
        "required_identity_stream_missing:feature_flag_snapshot"
        in result.reasons
    )
    assert "required_identity_stream_missing:code_build" in result.reasons


def test_non_crypto_post_score_capture_cannot_masquerade_as_applicable() -> None:
    bundle, authority, evaluation_at = _fixture()
    adjustment = CapturedViabilityPostScoreAdjustment(
        tenbeat_entry_tilt_weight=0.03,
        tenbeat_breakout_score=0.75,
        lookup_status="captured_value",
        source_read_id=READ_ID,
    )
    mismatched = replace(bundle, post_score_adjustment=adjustment)

    result = score_captured_viability(
        mismatched,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert "post_score_non_crypto_lookup_mismatch" in result.reasons


def test_coverage_unavailable_result_requires_a_reason() -> None:
    with pytest.raises(
        CapturedViabilityContractError,
        match="must explain why coverage failed",
    ):
        CapturedViabilityScoreResult(
            status=COVERAGE_UNAVAILABLE,
            reasons=(),
            bundle_sha256=None,
            authority_sha256=None,
        )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("settings", "chili_momentum_exclude_leveraged_etfs", 1),
        ("settings", "chili_momentum_ofi_threshold", "0.25"),
        ("external", "below_explosive_floor", 0),
        ("external", "ross_rvol", float("nan")),
        ("features", "spread_bps", "40.0"),
    ],
)
def test_arbitrary_projection_feature_and_external_types_fail_closed(
    section: str,
    field: str,
    value,
) -> None:
    bundle, authority, evaluation_at = _fixture()
    raw = deepcopy(bundle.to_dict())
    raw[section][field] = value

    result = score_captured_viability(
        raw, authority=authority, evaluation_at=evaluation_at
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert result.reasons == ("bundle_contract_invalid",)


def test_context_wall_clock_proxy_cannot_masquerade_as_event_clock() -> None:
    bundle, _authority, _evaluation_at = _fixture()
    wrong_context = replace(
        bundle.context,
        utc_iso=(BASE + timedelta(seconds=1)).isoformat(),
        utc_hour=(BASE + timedelta(seconds=1)).hour,
    )

    with pytest.raises(ValueError, match="context clock"):
        replace(bundle, context=wrong_context)


def test_nested_mapping_subclass_cannot_change_typed_vs_serialized_semantics() -> None:
    bundle, _authority, _evaluation_at = _fixture()
    context = replace(
        bundle.context,
        meta={"ross_scores": MappingProxyType({"VEEE": 0.72})},
    )

    with pytest.raises(
        CapturedViabilityContractError,
        match="unsupported scorer input type mappingproxy",
    ):
        replace(bundle, context=context)


def test_legacy_viability_row_snapshot_is_discovery_only_not_scorer_authority() -> None:
    _bundle, authority, evaluation_at = _fixture()
    legacy_row_snapshot = {
        "symbol": "VEEE",
        "variant_id": 101,
        "viability_score": 0.91,
        "paper_eligible": True,
        "live_eligible": True,
        "freshness_ts": evaluation_at.isoformat(),
    }

    result = score_captured_viability(
        legacy_row_snapshot,
        authority=authority,
        evaluation_at=evaluation_at,
    )

    assert result.status == COVERAGE_UNAVAILABLE
    assert result.reasons == ("bundle_contract_invalid",)
    assert result.observation is None
