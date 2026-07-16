from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
    AdaptiveRiskPolicy,
    AdaptiveRiskPolicySettingsReceipt,
)
from app.services.trading.momentum_neural.captured_adaptive_risk_source import (
    CapturedAdaptiveRiskPolicySpec,
)
from app.services.trading.momentum_neural.captured_paper_initial_provider import (
    CaptureBackedPaperInitialSessionMaterialProvider,
    CapturedPaperInitialCandidateRead,
    CapturedPaperInitialCandidateRow,
    CapturedPaperInitialProviderCoverageUnavailable,
)
from app.services.trading.momentum_neural.captured_paper_iqfeed_trigger import (
    CapturedPaperIqfeedTriggerReceipt,
    IqfeedTriggerResolution,
    IqfeedTriggerStatus,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    CaptureIdentityEvidence,
    CaptureSessionState,
    CaptureSubmission,
    CapturedReadResult,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    ActiveCaptureContinuityEvidence,
    ActiveCaptureReadEvidence,
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    ProviderWatermark,
    StreamCoverage,
    _issue_active_capture_input_attestation,
    captured_read_result_sha256,
    sha256_json,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 18, 0, 0, 500_000, tzinfo=UTC)
SYMBOL = "TEST"
USER_ID = 31
ACCOUNT_ID = "7ddc5883-c493-4de4-a4e5-e3f959461bfd"
RUNTIME_GENERATION = "97beeb02-84c7-47a8-859d-44d409674ec0"
DECISION_ID = "captured-paper-initial:TEST:20260716T180000Z"
READ_ID = "07e20956-f1b8-4890-9bea-da4aa3105cca"
RUN_ID = "3ee3ebf6-1620-4af1-80d7-0418de6a9bd6"
RESOURCE_SHA256 = sha256_json({"fixture": "resource-binding"})
CAPTURE_RECEIPT_SHA256 = sha256_json({"fixture": "capture-host-receipt"})

SOURCE_PROVIDER_AT = NOW - timedelta(seconds=1.2)
SOURCE_RECEIVED_AT = NOW - timedelta(seconds=1.1)
SOURCE_AVAILABLE_AT = NOW - timedelta(seconds=1.0)
READ_RETURNED_AT = NOW - timedelta(seconds=0.8)
RECEIPT_COMMITTED_AT = NOW - timedelta(seconds=0.7)
WATERMARK_COMMITTED_AT = NOW - timedelta(seconds=0.6)
COVERAGE_COMMITTED_AT = NOW - timedelta(seconds=0.5)
ATTESTED_AT = NOW - timedelta(seconds=0.4)

CODE_BUILD = {"git_sha256": sha256_json({"fixture": "git"}), "dirty": False}
CAPTURE_CONFIG = {
    "capture_certification_symbol": SYMBOL,
    "capture_mode": "hot_exact",
    "bounded_queue": True,
}
FEATURE_FLAGS = {
    "first_dip_candidate": True,
    "captured_paper_policy_parity": True,
}
ACCOUNT_IDENTITY = {
    "broker": "alpaca",
    "environment": "paper",
    "account_id": ACCOUNT_ID,
}


def _digest(label: str) -> str:
    return sha256_json({"fixture": label})


def _policy_receipt() -> AdaptiveRiskPolicySettingsReceipt:
    policy = AdaptiveRiskPolicy(
        policy_version="shared-replay-paper-v1",
        policy_source="test:captured-initial-provider",
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


def _identity(
    *,
    code_build: dict[str, Any] = CODE_BUILD,
    capture_config: dict[str, Any] = CAPTURE_CONFIG,
    feature_flags: dict[str, Any] = FEATURE_FLAGS,
    account_identity: dict[str, Any] = ACCOUNT_IDENTITY,
) -> CaptureRunIdentity:
    return CaptureRunIdentity(
        run_id=RUN_ID,
        generation=7,
        code_build_sha256=sha256_json(code_build),
        config_sha256=sha256_json(capture_config),
        feature_flags_sha256=sha256_json(feature_flags),
        account_identity_sha256=sha256_json(account_identity),
        broker="alpaca",
        broker_environment="paper",
    )


def _captured_read(
    identity: CaptureRunIdentity,
    *,
    read_id: str = READ_ID,
    decision_id: str = DECISION_ID,
    source_available_at: datetime = SOURCE_AVAILABLE_AT,
) -> CapturedReadResult:
    query = CaptureMicrostructureReadQuery(
        operation=CaptureMicrostructureOperation.TRADE_FLOW,
        stream=CaptureStream.IQFEED_PRINT,
        symbol=SYMBOL,
        provider="iqfeed",
        event_start_exclusive=SOURCE_PROVIDER_AT - timedelta(milliseconds=1),
        event_end_inclusive=SOURCE_PROVIDER_AT,
        decision_at=SOURCE_PROVIDER_AT,
        available_at_most=READ_RETURNED_AT,
        source_frontier_sequence=1,
        source_clock_basis="provider_event_at",
        parameters={"window_seconds": 0.001},
    )
    query_body = query.to_dict()
    query_sha256 = sha256_json(query_body)
    source = CaptureEvent(
        identity=identity,
        sequence=1,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol=SYMBOL,
        clocks=CaptureClocks(
            provider_event_at=SOURCE_PROVIDER_AT,
            received_at=SOURCE_RECEIVED_AT,
            available_at=source_available_at,
        ),
        payload={
            "symbol": SYMBOL,
            "price": 4.20,
            "size": 100.0,
            "bid": 4.19,
            "ask": 4.21,
            "provider_tick_id": "901234",
        },
    )
    source_ref = CaptureEventRef.from_event(source)
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol=SYMBOL,
        requested_at=source_available_at,
        returned_at=READ_RETURNED_AT,
        query_sha256=query_sha256,
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256((source_ref,)),
        content_verified=True,
        replay_network_fallback_used=False,
        query=query_body,
    )
    receipt_event = CaptureEvent(
        identity=identity,
        sequence=3,
        stream=CaptureStream.READ_RECEIPT,
        provider="iqfeed",
        symbol=SYMBOL,
        clocks=CaptureClocks(
            received_at=RECEIPT_COMMITTED_AT,
            available_at=RECEIPT_COMMITTED_AT,
        ),
        payload=receipt.to_dict(),
    )
    return CapturedReadResult(
        receipt=receipt,
        source_events=(source,),
        receipt_submission=CaptureSubmission(
            accepted=True,
            event=receipt_event,
            coverage_gap_recorded=False,
            disposition="fixture_durable_receipt",
        ),
        coverage_gap_recorded=False,
    )


def _trigger_resolution(
    identity: CaptureRunIdentity,
    captured: CapturedReadResult,
    *,
    trigger_overrides: dict[str, Any] | None = None,
) -> IqfeedTriggerResolution:
    assert captured.receipt is not None
    assert captured.receipt_submission is not None
    assert captured.receipt_submission.event is not None
    source = captured.source_events[0]
    receipt_event = captured.receipt_submission.event
    values: dict[str, Any] = {
        "decision_id": captured.receipt.decision_id,
        "notify_sha256": _digest("notify"),
        "symbol": SYMBOL,
        "bridge_version": (
            "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"
        ),
        "bridge_run_id": "8da0a1ed-24f3-4545-8a7a-6f582ff1acc2",
        "connection_generation": 3,
        "source_frame_sequence": 41,
        "source_frame_sha256": _digest("frame"),
        "provider_trade_reference_at": SOURCE_PROVIDER_AT,
        "notify_received_at": SOURCE_RECEIVED_AT,
        "notify_available_at": SOURCE_AVAILABLE_AT,
        "capture_identity_sha256": identity.identity_sha256,
        "captured_read_id": captured.receipt.read_id,
        "captured_read_receipt_sha256": sha256_json(
            captured.receipt.to_dict()
        ),
        "captured_read_receipt_event_sha256": receipt_event.event_sha256,
        "captured_read_receipt_event_sequence": receipt_event.sequence,
        "captured_read_result_sha256": captured.receipt.result_sha256,
        "captured_read_query_sha256": captured.receipt.query_sha256,
        "source_event_sha256": source.event_sha256,
        "source_event_sequence": source.sequence,
        "source_payload_sha256": source.payload_sha256,
        "source_provenance_sha256": _digest("source-provenance"),
        "source_provider_event_at": source.clocks.provider_event_at,
        "source_received_at": source.clocks.received_at,
        "source_available_at": source.clocks.available_at,
    }
    values.update(trigger_overrides or {})
    trigger = CapturedPaperIqfeedTriggerReceipt(**values)
    return IqfeedTriggerResolution(
        status=IqfeedTriggerStatus.READY,
        reason="iqfeed_exact_print_trigger_ready",
        attempts=1,
        notify_sha256=trigger.notify_sha256,
        receipt=trigger,
        captured_read=captured,
    )


def _attestation(
    identity: CaptureRunIdentity,
    captured: CapturedReadResult,
    *,
    decision_id: str = DECISION_ID,
    expires_at: datetime = NOW + timedelta(seconds=30),
    resource_binding_sha256: str = RESOURCE_SHA256,
    dependency_max_age_seconds: float = 1.75,
    dependency_coverage_start_at: datetime | None = None,
    continuity_provider: str = "iqfeed",
    continuity_evidence: tuple[ActiveCaptureContinuityEvidence, ...] | None = None,
) -> Any:
    assert captured.receipt is not None
    assert captured.receipt_submission is not None
    assert captured.receipt_submission.event is not None
    receipt_event = captured.receipt_submission.event
    active_read = ActiveCaptureReadEvidence(
        receipt=captured.receipt,
        receipt_sha256=sha256_json(captured.receipt.to_dict()),
        receipt_event_sha256=receipt_event.event_sha256,
        receipt_event_sequence=receipt_event.sequence,
        receipt_committed_available_at=receipt_event.clocks.available_at,
        producer_id="iqfeed_l1",
        producer_generation=identity.generation,
        source_event_refs=tuple(
            CaptureEventRef.from_event(event) for event in captured.source_events
        ),
    )
    watermark = ProviderWatermark(
        stream=CaptureStream.IQFEED_PRINT,
        provider=continuity_provider,
        identity_sha256=identity.identity_sha256,
        event_watermark_at=SOURCE_PROVIDER_AT,
        emitted_available_at=WATERMARK_COMMITTED_AT,
        bounded_lateness_seconds=1.0,
        max_observed_lateness_seconds=0.1,
        generation=identity.generation,
        symbol=SYMBOL,
    )
    coverage = StreamCoverage(
        stream=CaptureStream.IQFEED_PRINT,
        identity_sha256=identity.identity_sha256,
        provider=continuity_provider,
        symbol=SYMBOL,
        first_available_at=SOURCE_AVAILABLE_AT,
        last_available_at=SOURCE_AVAILABLE_AT,
        event_count=1,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=True,
        watermark=watermark,
    )
    continuity = ActiveCaptureContinuityEvidence(
        coverage=coverage,
        producer_id="iqfeed_l1",
        producer_generation=identity.generation,
        source_frontier_sequence=1,
        watermark_event_sha256=_digest("watermark-event"),
        watermark_event_sequence=4,
        watermark_committed_available_at=WATERMARK_COMMITTED_AT,
        coverage_event_sha256=_digest("coverage-event"),
        coverage_event_sequence=5,
        coverage_committed_available_at=COVERAGE_COMMITTED_AT,
    )
    dependency = FSMStreamDependency(
        stream=CaptureStream.IQFEED_PRINT,
        exact_provider_event_at_required=True,
        market_reference_at_required=False,
        max_source_age_seconds=dependency_max_age_seconds,
        coverage_start_at=(
            dependency_coverage_start_at
            if dependency_coverage_start_at is not None
            else SOURCE_PROVIDER_AT - timedelta(milliseconds=1)
        ),
    )
    profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
        required_read_ids=(captured.receipt.read_id,),
        stream_dependencies=(dependency,),
    )
    return _issue_active_capture_input_attestation(
        run_id=identity.run_id,
        generation=identity.generation,
        decision_id=decision_id,
        input_prefix_sequence=5,
        input_prefix_root_sha256=_digest("input-prefix"),
        attested_available_at=ATTESTED_AT,
        expires_at=expires_at,
        dependency_profile=profile,
        identity_sha256=identity.identity_sha256,
        account_identity_sha256=identity.account_identity_sha256,
        code_build_sha256=identity.code_build_sha256,
        config_sha256=identity.config_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
        resource_binding_sha256=resource_binding_sha256,
        producer_generations={"iqfeed_l1": identity.generation},
        required_read_ids=(captured.receipt.read_id,),
        read_evidence=(active_read,),
        continuity_evidence=(
            (continuity,)
            if continuity_evidence is None
            else continuity_evidence
        ),
    )


def _candidate(
    variant_id: int,
    *,
    score: float,
    freshness_at: datetime,
    viability_id: int | None = None,
    active: bool = True,
    paper_eligible: bool = True,
    live_eligible: bool = True,
    execution_family: str = "alpaca_spot",
    readiness: dict[str, Any] | None = None,
) -> CapturedPaperInitialCandidateRow:
    created = NOW - timedelta(days=1)
    variant = MomentumStrategyVariant(
        id=variant_id,
        family="captured_paper",
        variant_key=f"candidate-{variant_id}",
        version=1,
        label=f"Captured candidate {variant_id}",
        params_json={"setup_family": "first_dip_reclaim", "parity": True},
        is_active=active,
        execution_family=execution_family,
        parent_variant_id=None,
        refinement_meta_json={"source": "sealed-provider-test"},
        scan_pattern_id=None,
        created_at=created.replace(tzinfo=None),
        updated_at=created.replace(tzinfo=None),
    )
    viability = MomentumSymbolViability(
        id=viability_id or 100 + variant_id,
        symbol=SYMBOL,
        scope="symbol",
        variant_id=variant_id,
        viability_score=score,
        paper_eligible=paper_eligible,
        live_eligible=live_eligible,
        freshness_ts=freshness_at.replace(tzinfo=None),
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json=(
            readiness
            if readiness is not None
            else {"coverage": "complete", "spread_bps": 7.25}
        ),
        explain_json={"reason": "captured-test"},
        evidence_window_json={"coverage": "complete"},
        source_node_id="captured_initial_provider_test",
        correlation_id=_digest(f"candidate:{variant_id}"),
        created_at=created.replace(tzinfo=None),
        updated_at=freshness_at.replace(tzinfo=None),
    )
    return CapturedPaperInitialCandidateRow(
        variant=variant,
        viability=viability,
    )


class _Reader:
    network_fallback_allowed = False
    mutation_allowed = False

    def __init__(
        self,
        rows: tuple[CapturedPaperInitialCandidateRow, ...],
        *,
        read_at: datetime = NOW - timedelta(milliseconds=50),
    ) -> None:
        self.rows = rows
        self.read_at = read_at
        self.calls: list[dict[str, Any]] = []
        self.network_calls = 0
        self.mutation_calls = 0
        self.order_calls = 0

    def read_candidates(self, **kwargs: Any) -> CapturedPaperInitialCandidateRead:
        self.calls.append(dict(kwargs))
        return CapturedPaperInitialCandidateRead(
            user_id=kwargs["user_id"],
            symbol=kwargs["symbol"],
            read_at=self.read_at,
            rows=self.rows,
        )

    def fetch_provider(self) -> None:  # pragma: no cover - forbidden capability
        self.network_calls += 1
        raise AssertionError("provider/network call escaped pure reader")

    def mutate_database(self) -> None:  # pragma: no cover - forbidden capability
        self.mutation_calls += 1
        raise AssertionError("database mutation escaped pure reader")

    def post_order(self) -> None:  # pragma: no cover - forbidden capability
        self.order_calls += 1
        raise AssertionError("order call escaped pure reader")


class _ConfigResolver:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls: list[str] = []

    def __call__(self, symbol: str) -> str:
        self.calls.append(symbol)
        return self.digest


def _provider_fixture(
    *,
    rows: tuple[CapturedPaperInitialCandidateRow, ...] | None = None,
    identity: CaptureRunIdentity | None = None,
    captured: CapturedReadResult | None = None,
    trigger_resolution: IqfeedTriggerResolution | None = None,
    attestation: Any | None = None,
    reader: _Reader | None = None,
    config_resolver: _ConfigResolver | None = None,
    expected_account_id: str = ACCOUNT_ID,
    capture_identity_evidence: CaptureIdentityEvidence | None = None,
    policy_receipt: AdaptiveRiskPolicySettingsReceipt | None = None,
    policy_spec: CapturedAdaptiveRiskPolicySpec | None = None,
) -> tuple[
    CaptureBackedPaperInitialSessionMaterialProvider,
    IqfeedTriggerResolution,
    Any,
    _Reader,
    _ConfigResolver,
    Any,
]:
    identity = identity or _identity()
    captured = captured or _captured_read(identity)
    trigger_resolution = trigger_resolution or _trigger_resolution(identity, captured)
    attestation = attestation or _attestation(identity, captured)
    rows = rows if rows is not None else (
        _candidate(1, score=0.82, freshness_at=NOW - timedelta(seconds=1)),
        _candidate(2, score=0.91, freshness_at=NOW - timedelta(seconds=2)),
    )
    reader = reader or _Reader(rows)
    config_resolver = config_resolver or _ConfigResolver(identity.config_sha256)
    evidence = capture_identity_evidence or CaptureIdentityEvidence(
        code_build=CODE_BUILD,
        config=CAPTURE_CONFIG,
        feature_flags=FEATURE_FLAGS,
        account_identity=ACCOUNT_IDENTITY,
        account_risk_snapshot={"status": "ACTIVE"},
        account_query={"operation": "captured_startup_snapshot"},
        account_provider="alpaca",
    )
    policy_receipt = policy_receipt or _policy_receipt()
    policy_spec = policy_spec or CapturedAdaptiveRiskPolicySpec(
        policy=policy_receipt.policy,
        code_build_sha256=identity.code_build_sha256,
        effective_config_sha256=policy_receipt.settings_projection_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
    )
    coordinator = SimpleNamespace(
        identity=identity,
        certification_symbol=SYMBOL,
        state=CaptureSessionState.RUNNING,
        resource_binding=SimpleNamespace(binding_sha256=RESOURCE_SHA256),
        provider_calls=0,
        broker_calls=0,
        order_calls=0,
    )
    provider = CaptureBackedPaperInitialSessionMaterialProvider(
        user_id=USER_ID,
        account_scope="alpaca:paper",
        expected_account_id=expected_account_id,
        runtime_generation=RUNTIME_GENERATION,
        code_build_sha256=identity.code_build_sha256,
        capture_receipt_sha256=CAPTURE_RECEIPT_SHA256,
        trigger_resolution=trigger_resolution,
        active_input_attestation=attestation,
        capture_coordinator=coordinator,
        capture_identity_evidence=evidence,
        capture_config_sha256_resolver=config_resolver,
        candidate_reader=reader,
        adaptive_policy_settings_receipt=policy_receipt,
        adaptive_policy_spec=policy_spec,
        material_ttl_seconds=30.0,
        wall_clock=lambda: NOW,
    )
    return (
        provider,
        trigger_resolution,
        attestation,
        reader,
        config_resolver,
        coordinator,
    )


def _prepare(provider, resolution):
    assert resolution.receipt is not None
    return provider.prepare_initial_session(
        symbol=SYMBOL,
        trigger_read_receipt_sha256=resolution.receipt.content_sha256,
    )


def test_provider_is_order_independent_and_uses_score_readiness_then_ids() -> None:
    older = NOW - timedelta(seconds=2)
    newer = NOW - timedelta(seconds=1)
    rows = (
        _candidate(9, score=0.91, freshness_at=older),
        _candidate(4, score=0.91, freshness_at=newer),
        _candidate(3, score=0.91, freshness_at=newer),
        _candidate(1, score=0.89, freshness_at=NOW),
    )
    first, resolution, *_ = _provider_fixture(rows=rows)
    second, second_resolution, *_ = _provider_fixture(rows=tuple(reversed(rows)))

    first_material = _prepare(first, resolution)
    second_material = _prepare(second, second_resolution)

    assert first_material.variant_id == 3
    assert second_material.variant_id == 3
    assert first_material.to_dict() == second_material.to_dict()


def test_material_embeds_recomputable_complete_set_selected_row_and_capture_checkpoint() -> None:
    rows = (
        _candidate(1, score=0.82, freshness_at=NOW - timedelta(seconds=1)),
        _candidate(2, score=0.91, freshness_at=NOW - timedelta(seconds=2)),
        _candidate(
            3,
            score=0.99,
            freshness_at=NOW - timedelta(seconds=1),
            active=False,
        ),
    )
    provider, resolution, attestation, *_ = _provider_fixture(rows=rows)

    material = _prepare(provider, resolution)
    brief = material.runner_risk_template.payload["viability_brief"]
    considered = json.loads(brief["considered_set_canonical_json"])
    selected = json.loads(brief["selected_candidate_canonical_json"])
    selection = json.loads(brief["selection_receipt_canonical_json"])
    checkpoint = json.loads(
        material.runner_risk_template.payload["execution_readiness_subset"][
            "capture_checkpoint_canonical_json"
        ]
    )

    assert len(considered["candidates"]) == 3
    assert sha256_json(considered) == brief["considered_set_sha256"]
    assert sha256_json(selected) == brief["selected_candidate_sha256"]
    assert sha256_json(selection) == material.selection_receipt_sha256
    assert selection["considered_set"] == considered
    assert selection["selected_candidate"] == selected
    assert selected["variant"]["id"] == 2
    assert checkpoint["attestation_sha256"] == attestation.attestation_sha256
    assert checkpoint["required_read_ids"] == [READ_ID]
    assert checkpoint["read_evidence"][0]["receipt"]["read_id"] == READ_ID
    assert material.captured_input_attestation_sha256 == (
        attestation.attestation_sha256
    )
    assert material.config_sha256 != material.settings_projection_sha256


def test_nonselected_candidate_change_changes_full_set_selection_and_material_hashes() -> None:
    base_rows = (
        _candidate(1, score=0.95, freshness_at=NOW - timedelta(seconds=1)),
        _candidate(2, score=0.70, freshness_at=NOW - timedelta(seconds=2)),
    )
    changed_rows = (
        base_rows[0],
        _candidate(
            2,
            score=0.70,
            freshness_at=NOW - timedelta(seconds=2),
            readiness={"coverage": "complete", "spread_bps": 9.75},
        ),
    )
    first, first_resolution, *_ = _provider_fixture(rows=base_rows)
    second, second_resolution, *_ = _provider_fixture(rows=changed_rows)

    first_material = _prepare(first, first_resolution)
    second_material = _prepare(second, second_resolution)

    assert first_material.variant_id == second_material.variant_id == 1
    assert first_material.selection_receipt_sha256 != (
        second_material.selection_receipt_sha256
    )
    assert first_material.runner_risk_template.template_sha256 != (
        second_material.runner_risk_template.template_sha256
    )
    assert first_material.material_sha256 != second_material.material_sha256


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("trigger_hash", "initial_provider_trigger_route_mismatch"),
        ("trigger_receipt_hash", "initial_provider_trigger_read_attestation_mismatch"),
        ("trigger_source_clock", "initial_provider_trigger_source_attestation_mismatch"),
        ("attestation_decision", "initial_provider_capture_attestation_identity_mismatch"),
        ("attestation_resource", "initial_provider_capture_attestation_identity_mismatch"),
        ("coordinator_symbol", "initial_provider_capture_coordinator_unavailable"),
        ("config", "initial_provider_capture_config_identity_mismatch"),
        ("account", "initial_provider_expected_account_mismatch"),
    ),
)
def test_identity_hash_and_clock_mismatches_fail_closed(case: str, reason: str) -> None:
    identity = _identity()
    captured = _captured_read(identity)
    resolution = _trigger_resolution(identity, captured)
    attestation = _attestation(identity, captured)
    config_resolver = _ConfigResolver(identity.config_sha256)
    evidence = None
    expected_account_id = ACCOUNT_ID

    if case == "trigger_receipt_hash":
        assert resolution.receipt is not None
        resolution = _trigger_resolution(
            identity,
            captured,
            trigger_overrides={"captured_read_receipt_sha256": _digest("wrong")},
        )
    elif case == "trigger_source_clock":
        resolution = _trigger_resolution(
            identity,
            captured,
            trigger_overrides={
                "source_available_at": SOURCE_AVAILABLE_AT + timedelta(microseconds=1)
            },
        )
    elif case == "attestation_decision":
        resolution = _trigger_resolution(
            identity,
            captured,
            trigger_overrides={"decision_id": f"{DECISION_ID}:other"},
        )
    elif case == "attestation_resource":
        attestation = _attestation(
            identity,
            captured,
            resource_binding_sha256=_digest("other-resource"),
        )
    elif case == "config":
        config_resolver = _ConfigResolver(_digest("wrong-config"))
    elif case == "account":
        expected_account_id = "e5b68d1f-7af1-4d8d-a221-c46001484358"

    provider, resolution, *_rest = _provider_fixture(
        identity=identity,
        captured=captured,
        trigger_resolution=resolution,
        attestation=attestation,
        config_resolver=config_resolver,
        expected_account_id=expected_account_id,
        capture_identity_evidence=evidence,
    )
    if case == "coordinator_symbol":
        provider.capture_coordinator.certification_symbol = "OTHER"

    trigger_sha256 = (
        _digest("wrong-trigger")
        if case == "trigger_hash"
        else resolution.receipt.content_sha256
    )
    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match=reason,
    ) as caught:
        provider.prepare_initial_session(
            symbol=SYMBOL,
            trigger_read_receipt_sha256=trigger_sha256,
        )
    assert caught.value.status == "COVERAGE_UNAVAILABLE"


def test_attestation_with_different_exact_read_cannot_substitute_for_trigger_read() -> None:
    identity = _identity()
    trigger_read = _captured_read(identity)
    resolution = _trigger_resolution(identity, trigger_read)
    other_read = _captured_read(
        identity,
        read_id="5fa89cd5-4962-43d7-8735-5e5bedcc5ad2",
    )
    other_attestation = _attestation(identity, other_read)
    provider, resolution, *_ = _provider_fixture(
        identity=identity,
        captured=trigger_read,
        trigger_resolution=resolution,
        attestation=other_attestation,
    )

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_trigger_read_attestation_missing",
    ):
        _prepare(provider, resolution)


def test_lax_iqfeed_dependency_max_age_is_rejected_by_provider() -> None:
    identity = _identity()
    captured = _captured_read(identity)
    resolution = _trigger_resolution(identity, captured)
    lax_attestation = _attestation(
        identity,
        captured,
        dependency_max_age_seconds=1.751,
    )
    provider, resolution, *_ = _provider_fixture(
        identity=identity,
        captured=captured,
        trigger_resolution=resolution,
        attestation=lax_attestation,
    )

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_trigger_dependency_profile_mismatch",
    ):
        _prepare(provider, resolution)


def test_exact_iqfeed_print_older_than_market_policy_is_rejected() -> None:
    provider, resolution, *_ = _provider_fixture()
    provider.wall_clock = lambda: NOW + timedelta(seconds=0.8)

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_capture_authority_stale",
    ):
        _prepare(provider, resolution)


def test_iqfeed_dependency_cannot_downgrade_exact_event_clock() -> None:
    with pytest.raises(CaptureContractError, match="cannot downgrade"):
        FSMStreamDependency(
            stream=CaptureStream.IQFEED_PRINT,
            exact_provider_event_at_required=False,
            market_reference_at_required=False,
            max_source_age_seconds=45.0,
            coverage_start_at=SOURCE_PROVIDER_AT - timedelta(milliseconds=1),
        )


def _foreign_nbbo_continuity(
    identity: CaptureRunIdentity,
) -> ActiveCaptureContinuityEvidence:
    watermark = ProviderWatermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        identity_sha256=identity.identity_sha256,
        event_watermark_at=SOURCE_PROVIDER_AT,
        emitted_available_at=WATERMARK_COMMITTED_AT,
        bounded_lateness_seconds=1.0,
        max_observed_lateness_seconds=0.1,
        generation=identity.generation,
        symbol=SYMBOL,
    )
    coverage = StreamCoverage(
        stream=CaptureStream.NBBO_QUOTE,
        identity_sha256=identity.identity_sha256,
        provider="iqfeed",
        symbol=SYMBOL,
        first_available_at=SOURCE_AVAILABLE_AT,
        last_available_at=SOURCE_AVAILABLE_AT,
        event_count=1,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=True,
        watermark=watermark,
    )
    return ActiveCaptureContinuityEvidence(
        coverage=coverage,
        producer_id="iqfeed_l1",
        producer_generation=identity.generation,
        source_frontier_sequence=1,
        watermark_event_sha256=_digest("foreign-watermark-event"),
        watermark_event_sequence=4,
        watermark_committed_available_at=WATERMARK_COMMITTED_AT,
        coverage_event_sha256=_digest("foreign-coverage-event"),
        coverage_event_sequence=5,
        coverage_committed_available_at=COVERAGE_COMMITTED_AT,
    )


@pytest.mark.parametrize("continuity_kind", ("missing", "foreign"))
def test_typed_attestation_rejects_missing_or_foreign_iqfeed_continuity(
    continuity_kind: str,
) -> None:
    identity = _identity()
    captured = _captured_read(identity)
    continuity = (
        ()
        if continuity_kind == "missing"
        else (_foreign_nbbo_continuity(identity),)
    )

    with pytest.raises(
        CaptureContractError,
        match="continuity inventory is incomplete",
    ):
        _attestation(
            identity,
            captured,
            continuity_evidence=continuity,
        )


def test_provider_rejects_foreign_provider_continuity_for_exact_iqfeed_read() -> None:
    identity = _identity()
    captured = _captured_read(identity)
    resolution = _trigger_resolution(identity, captured)
    attestation = _attestation(
        identity,
        captured,
        continuity_provider="foreign-feed",
    )
    provider, resolution, *_ = _provider_fixture(
        identity=identity,
        captured=captured,
        trigger_resolution=resolution,
        attestation=attestation,
    )

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_trigger_continuity_mismatch",
    ):
        _prepare(provider, resolution)


@pytest.mark.parametrize(
    ("reader", "reason"),
    (
        (_Reader(()), "initial_candidate_selection_coverage_unavailable"),
        (
            _Reader(
                (_candidate(1, score=0.8, freshness_at=NOW - timedelta(seconds=46)),)
            ),
            "initial_candidate_selection_coverage_unavailable",
        ),
        (
            _Reader(
                (_candidate(1, score=0.8, freshness_at=NOW - timedelta(seconds=1)),),
                read_at=NOW - timedelta(seconds=46),
            ),
            "initial_candidate_read_stale",
        ),
    ),
)
def test_missing_or_stale_candidate_evidence_is_local_coverage_unavailable(
    reader: _Reader,
    reason: str,
) -> None:
    provider, resolution, *_ = _provider_fixture(reader=reader)

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match=reason,
    ):
        _prepare(provider, resolution)
    assert reader.network_calls == 0
    assert reader.mutation_calls == 0
    assert reader.order_calls == 0


@pytest.mark.parametrize(
    ("field", "reason"),
    (
        ("variant", "initial_candidate_variant_unavailable"),
        ("viability", "initial_candidate_viability_unavailable"),
    ),
)
def test_malformed_candidate_scalars_remain_typed_coverage_unavailable(
    field: str,
    reason: str,
) -> None:
    row = _candidate(1, score=0.8, freshness_at=NOW - timedelta(seconds=1))
    setattr(getattr(row, field), "id", "not-an-integer")
    reader = _Reader((row,))
    provider, resolution, *_ = _provider_fixture(reader=reader)

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match=reason,
    ):
        _prepare(provider, resolution)


def test_capture_config_and_adaptive_settings_digest_swap_is_rejected() -> None:
    receipt = _policy_receipt()
    swapped_config = {
        "capture_certification_symbol": SYMBOL,
        "forced_digest": receipt.settings_projection_sha256,
    }
    # The test uses a mapping whose natural digest differs, then explicitly
    # demonstrates that returning the policy digest cannot masquerade as the
    # per-symbol capture digest.
    identity = _identity(capture_config=swapped_config)
    captured = _captured_read(identity)
    resolution = _trigger_resolution(identity, captured)
    attestation = _attestation(identity, captured)
    evidence = CaptureIdentityEvidence(
        code_build=CODE_BUILD,
        config=swapped_config,
        feature_flags=FEATURE_FLAGS,
        account_identity=ACCOUNT_IDENTITY,
        account_risk_snapshot={"status": "ACTIVE"},
        account_query={"operation": "captured_startup_snapshot"},
        account_provider="alpaca",
    )
    provider, resolution, *_ = _provider_fixture(
        identity=identity,
        captured=captured,
        trigger_resolution=resolution,
        attestation=attestation,
        capture_identity_evidence=evidence,
        config_resolver=_ConfigResolver(receipt.settings_projection_sha256),
        policy_receipt=receipt,
    )

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_capture_config_identity_mismatch",
    ):
        _prepare(provider, resolution)


def test_policy_spec_mismatch_rejects_at_constructor_before_any_read() -> None:
    identity = _identity()
    receipt = _policy_receipt()
    wrong_spec = CapturedAdaptiveRiskPolicySpec(
        policy=receipt.policy,
        code_build_sha256=_digest("wrong-code"),
        effective_config_sha256=receipt.settings_projection_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
    )
    reader = _Reader(
        (_candidate(1, score=0.8, freshness_at=NOW - timedelta(seconds=1)),)
    )

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_adaptive_policy_binding_mismatch",
    ):
        _provider_fixture(
            identity=identity,
            reader=reader,
            policy_receipt=receipt,
            policy_spec=wrong_spec,
        )
    assert reader.calls == []


def test_provider_has_only_injected_reads_and_no_forbidden_side_effects() -> None:
    provider, resolution, _proof, reader, resolver, coordinator = (
        _provider_fixture()
    )

    material = _prepare(provider, resolution)

    assert material.account_scope == "alpaca:paper"
    assert reader.calls == [
        {"user_id": USER_ID, "symbol": SYMBOL, "decision_at": NOW}
    ]
    assert resolver.calls == [SYMBOL, SYMBOL]
    assert reader.network_calls == 0
    assert reader.mutation_calls == 0
    assert reader.order_calls == 0
    assert coordinator.provider_calls == 0
    assert coordinator.broker_calls == 0
    assert coordinator.order_calls == 0
    source = Path(
        "app/services/trading/momentum_neural/captured_paper_initial_provider.py"
    ).read_text(encoding="utf-8")
    assert "from sqlalchemy" not in source
    assert "import sqlalchemy" not in source
    assert "requests." not in source
    assert "post_limit_buy" not in source


def test_runner_template_uses_only_shared_equity_relative_policy_caps() -> None:
    provider, resolution, *_ = _provider_fixture()
    material = _prepare(provider, resolution)
    caps = material.runner_risk_template.payload["momentum_policy_caps"]
    policy = _policy_receipt().policy

    assert caps == {
        "source": "shared_adaptive_policy",
        "risk_fraction_of_equity": policy.risk_fraction_of_equity,
        "daily_risk_fraction_of_equity": policy.daily_risk_fraction_of_equity,
        "portfolio_risk_fraction_of_equity": (
            policy.portfolio_risk_fraction_of_equity
        ),
        "cluster_risk_fraction_of_equity": policy.cluster_risk_fraction_of_equity,
        "symbol_risk_fraction_of_equity": policy.symbol_risk_fraction_of_equity,
        "max_notional_fraction_of_equity": policy.max_notional_fraction_of_equity,
        "max_buying_power_fraction_for_notional": (
            policy.max_buying_power_fraction_for_notional
        ),
        "max_portfolio_gross_fraction_of_equity": (
            policy.max_portfolio_gross_fraction_of_equity
        ),
        "max_adv_participation": policy.max_adv_participation,
        "max_recent_volume_participation": policy.max_recent_volume_participation,
        "max_executable_depth_participation": (
            policy.max_executable_depth_participation
        ),
    }
    serialized = json.dumps(dict(caps), sort_keys=True)
    assert "fixed_dollar" not in serialized
    assert "one_symbol" not in serialized
    assert "max_concurrent" not in serialized


def test_identity_evidence_mutated_during_candidate_read_fails_closed() -> None:
    mutable_config = dict(CAPTURE_CONFIG)
    identity = _identity(capture_config=mutable_config)
    evidence = CaptureIdentityEvidence(
        code_build=dict(CODE_BUILD),
        config=mutable_config,
        feature_flags=dict(FEATURE_FLAGS),
        account_identity=dict(ACCOUNT_IDENTITY),
        account_risk_snapshot={"status": "ACTIVE"},
        account_query={"operation": "captured_startup_snapshot"},
        account_provider="alpaca",
    )

    class MutatingReader(_Reader):
        def read_candidates(self, **kwargs: Any) -> CapturedPaperInitialCandidateRead:
            result = super().read_candidates(**kwargs)
            mutable_config["capture_mode"] = "mutated_during_read"
            return result

    reader = MutatingReader(
        (_candidate(1, score=0.8, freshness_at=NOW - timedelta(seconds=1)),)
    )
    provider, resolution, *_ = _provider_fixture(
        identity=identity,
        reader=reader,
        capture_identity_evidence=evidence,
        config_resolver=_ConfigResolver(identity.config_sha256),
    )

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_capture_identity_drifted",
    ):
        _prepare(provider, resolution)


def test_material_that_expires_during_candidate_read_is_not_returned() -> None:
    provider, resolution, *_ = _provider_fixture()
    clock_values = iter((NOW, NOW + timedelta(seconds=31)))
    provider.wall_clock = lambda: next(clock_values)

    with pytest.raises(
        CapturedPaperInitialProviderCoverageUnavailable,
        match="initial_provider_material_expired",
    ):
        _prepare(provider, resolution)


def test_forbidden_reader_postures_fail_before_candidate_callback() -> None:
    for field_name, reason in (
        ("network_fallback_allowed", "initial_candidate_network_fallback_forbidden"),
        ("mutation_allowed", "initial_candidate_mutation_forbidden"),
    ):
        reader = _Reader(
            (_candidate(1, score=0.8, freshness_at=NOW - timedelta(seconds=1)),)
        )
        setattr(reader, field_name, True)
        provider, resolution, *_ = _provider_fixture(reader=reader)

        with pytest.raises(
            CapturedPaperInitialProviderCoverageUnavailable,
            match=reason,
        ):
            _prepare(provider, resolution)
        assert reader.calls == []
