from __future__ import annotations

import ast
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import textwrap
import uuid

import pytest

from app.models.core import User
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    RiskInputEvidence,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskReservationRequest,
    ImmutableAccountRiskSnapshot,
)
from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
    build_adaptive_risk_reservation_claim,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureAdaptiveOrderArtifacts,
    CaptureClocks,
    CaptureCoverageManifest,
    CaptureDecisionAction,
    CaptureDecisionCheckpoint,
    CaptureDecisionOutput,
    CaptureEvent,
    CaptureEventRef,
    CaptureOrderIntent,
    CaptureOrderIntentRole,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    ProviderWatermark,
    ReplayCoverageRequest,
    STREAM_POLICIES,
    StreamCoverage,
    VerifiedReplayCapture,
    capture_prefix_root_sha256,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureWriterWorker,
    ContentAddressedCaptureStore,
)
from app.services.trading.momentum_neural import replay_v3 as rv3


UTC = timezone.utc
NOW = datetime(2026, 7, 14, 13, 5, tzinfo=UTC)
RUN_ID = str(uuid.UUID(int=19))
DECISION_ID = "veee-entry-recorded"


def _policy() -> AdaptiveRiskPolicy:
    return AdaptiveRiskPolicy(
        policy_version="replay-seed-fixture-v1",
        policy_source="recorded_fixture",
        risk_fraction_of_equity=0.01,
        daily_risk_fraction_of_equity=0.10,
        portfolio_risk_fraction_of_equity=0.05,
        cluster_risk_fraction_of_equity=0.04,
        symbol_risk_fraction_of_equity=0.03,
        daily_gap_reserve_fraction_of_equity=0.001,
        max_notional_fraction_of_equity=0.80,
        max_buying_power_fraction_for_notional=0.50,
        max_portfolio_gross_fraction_of_equity=2.0,
        quality_multiplier_floor=0.50,
        quality_multiplier_ceiling=1.50,
        volatility_reference_fraction=0.05,
        volatility_multiplier_floor=0.40,
        spread_reserve_multiple=1.0,
        per_share_gap_reserve_volatility_multiple=0.10,
        max_adv_participation=0.02,
        max_recent_volume_participation=0.10,
        max_executable_depth_participation=0.50,
        market_data_max_age_seconds=2.0,
        account_data_max_age_seconds=10.0,
        reservation_data_max_age_seconds=0.25,
        context_data_max_age_seconds=60.0,
    )


def _evidence(
    *,
    content_sha256: str,
    available: datetime,
    source: str = "recorded_fixture",
) -> RiskInputEvidence:
    return RiskInputEvidence(
        source=source,
        observed_at=available - timedelta(milliseconds=2),
        available_at=available,
        content_sha256=content_sha256,
        provider_generation="fixture-generation-3",
    )


def _fixture_account_snapshot(
    account_identity_sha256: str,
) -> ImmutableAccountRiskSnapshot:
    return ImmutableAccountRiskSnapshot(
        snapshot_id="replay-account-fixture-3",
        source="recorded_fixture",
        provider_generation="fixture-generation-3",
        account_scope="alpaca:paper:paper-fixture",
        execution_family="alpaca_spot",
        broker_environment="paper",
        venue="alpaca",
        account_identity_sha256=account_identity_sha256,
        observed_at=NOW - timedelta(milliseconds=62),
        available_at=NOW - timedelta(milliseconds=60),
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=0.0,
        pending_policy_buying_power_reflected_usd=0.0,
    )


def _coverage_context(
    *, include_events: bool = False
) -> (
    tuple[
        ReplayCoverageRequest,
        CaptureCoverageManifest,
        dict[str, RiskInputEvidence],
    ]
    | tuple[
        ReplayCoverageRequest,
        CaptureCoverageManifest,
        dict[str, RiskInputEvidence],
        tuple[CaptureEvent, ...],
    ]
):
    config_payload = {"effective_config": "alpaca-paper-fixture"}
    flags_payload = {"feature_flags": {"adaptive_risk": True}}
    code_payload = {"git_tree": "replay-seed-fixture"}
    account_identity_payload = {
        "account_id": "paper-fixture",
        "equity_usd": 100_000.0,
        "buying_power_usd": 400_000.0,
        "broker_day_change_usd": 0.0,
        "local_realized_pnl_usd": 0.0,
    }
    identity = CaptureRunIdentity(
        run_id=RUN_ID,
        generation=3,
        code_build_sha256=sha256_json(code_payload),
        config_sha256=sha256_json(config_payload),
        feature_flags_sha256=sha256_json(flags_payload),
        account_identity_sha256=sha256_json(account_identity_payload),
        broker="alpaca",
        broker_environment="paper",
    )
    account_snapshot = _fixture_account_snapshot(
        identity.account_identity_sha256
    )
    account_payload = account_snapshot.to_payload()
    supplied_snapshot_sha256 = account_payload.pop("snapshot_sha256")
    assert supplied_snapshot_sha256 == account_snapshot.snapshot_sha256
    assert sha256_json(account_payload) == account_snapshot.snapshot_sha256
    warmup = NOW - timedelta(minutes=10)
    exit_at = NOW + timedelta(minutes=10)
    read_ids = {
        "nbbo": str(uuid.UUID(int=20)),
        "account": str(uuid.UUID(int=21)),
        "admission": str(uuid.UUID(int=22)),
        "reservation": str(uuid.UUID(int=23)),
        "candidate_bp": str(uuid.UUID(int=24)),
    }

    def event(
        *,
        sequence: int,
        stream: CaptureStream,
        available_at: datetime,
        payload: dict,
        symbol: str | None = None,
        query: dict | None = None,
        exact_clock: bool = False,
    ) -> CaptureEvent:
        return CaptureEvent(
            identity=identity,
            sequence=sequence,
            stream=stream,
            symbol=symbol,
            provider="fixture",
            clocks=CaptureClocks(
                provider_event_at=(
                    available_at - timedelta(milliseconds=2)
                    if exact_clock
                    else None
                ),
                market_reference_at=(
                    None
                    if stream
                    in {
                        CaptureStream.CONFIG_SNAPSHOT,
                        CaptureStream.FEATURE_FLAG_SNAPSHOT,
                        CaptureStream.CODE_BUILD,
                    }
                    else available_at - timedelta(milliseconds=2)
                ),
                received_at=available_at - timedelta(milliseconds=1),
                available_at=available_at,
            ),
            query=query,
            payload=payload,
        )

    admission_payload = {
        "symbol": "VEEE",
        "structural_stop": 9.50,
        "setup_quality": 0.80,
        "realized_volatility_fraction": 0.05,
        "average_daily_volume_shares": 5_000_000.0,
        "recent_volume_shares": 500_000.0,
        "executable_depth_shares": 100_000.0,
        "portfolio_heat": {"open_risk": 0.0, "pending_risk": 0.0},
        "correlation_cluster": {"open_risk": 0.0, "pending_risk": 0.0},
    }
    reservation_payload = {
        "ledger_version": 1,
        "open": [],
        "pending": [],
        "policy_buying_power_capacity_usd": 400_000.0,
    }
    candidate_bp_payload = {
        "symbol": "VEEE",
        "side": "long",
        "buying_power_impact_per_share_usd": 10.0,
    }
    events = [
        event(
            sequence=1,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            available_at=NOW - timedelta(milliseconds=100),
            payload=config_payload,
        ),
        event(
            sequence=2,
            stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
            available_at=NOW - timedelta(milliseconds=90),
            payload=flags_payload,
        ),
        event(
            sequence=3,
            stream=CaptureStream.CODE_BUILD,
            available_at=NOW - timedelta(milliseconds=80),
            payload=code_payload,
        ),
        event(
            sequence=4,
            stream=CaptureStream.NBBO_QUOTE,
            available_at=NOW - timedelta(milliseconds=70),
            payload={"bid": 9.99, "ask": 10.00},
            symbol="VEEE",
            exact_clock=True,
        ),
        event(
            sequence=5,
            stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            available_at=NOW - timedelta(milliseconds=60),
            payload=account_payload,
            query={"account": "paper-fixture", "fields": "risk"},
        ),
        event(
            sequence=6,
            stream=CaptureStream.ADMISSION_ELIGIBILITY,
            available_at=NOW - timedelta(milliseconds=50),
            payload=admission_payload,
            symbol="VEEE",
        ),
        event(
            sequence=7,
            stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            available_at=NOW - timedelta(milliseconds=40),
            payload=reservation_payload,
            query={"ledger": "adaptive-risk", "generation": 3},
        ),
        event(
            sequence=8,
            stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            available_at=NOW - timedelta(milliseconds=30),
            payload=candidate_bp_payload,
            query={"candidate": "VEEE", "side": "long"},
        ),
    ]
    source_refs_by_sequence = {
        ref.sequence: ref
        for ref in (CaptureEventRef.from_event(row) for row in events)
    }

    def receipt(
        name: str,
        stream: CaptureStream,
        source: CaptureEventRef,
        *,
        symbol: str | None,
    ) -> CaptureReadReceipt:
        query_sha256 = source.query_sha256 or sha256_json(
            {"read": name, "symbol": symbol}
        )
        return CaptureReadReceipt(
            read_id=read_ids[name],
            decision_id=DECISION_ID,
            identity_sha256=identity.identity_sha256,
            stream=stream,
            provider="fixture",
            symbol=symbol,
            requested_at=source.available_at - timedelta(milliseconds=1),
            returned_at=source.available_at + timedelta(milliseconds=1),
            query_sha256=query_sha256,
            source_event_sha256s=(source.event_sha256,),
            empty_result=False,
            result_sha256=captured_read_result_sha256((source,)),
        )

    receipts = (
        receipt(
            "nbbo",
            CaptureStream.NBBO_QUOTE,
            source_refs_by_sequence[4],
            symbol="VEEE",
        ),
        receipt(
            "account",
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            source_refs_by_sequence[5],
            symbol=None,
        ),
        receipt(
            "admission",
            CaptureStream.ADMISSION_ELIGIBILITY,
            source_refs_by_sequence[6],
            symbol="VEEE",
        ),
        receipt(
            "reservation",
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            source_refs_by_sequence[7],
            symbol=None,
        ),
        receipt(
            "candidate_bp",
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            source_refs_by_sequence[8],
            symbol=None,
        ),
    )
    receipt_available_at = events[-1].clocks.available_at
    for receipt_row in receipts:
        receipt_available_at = max(
            receipt_available_at, receipt_row.returned_at
        ) + timedelta(microseconds=1)
        events.append(
            CaptureEvent(
                identity=identity,
                sequence=len(events) + 1,
                stream=CaptureStream.READ_RECEIPT,
                provider="chili_capture",
                symbol=None,
                clocks=CaptureClocks(
                    received_at=receipt_available_at - timedelta(microseconds=1),
                    available_at=receipt_available_at,
                ),
                payload=receipt_row.to_dict(),
            )
        )
    prefix_refs = tuple(CaptureEventRef.from_event(row) for row in events)
    prefix_sequence = len(events)
    prefix_root = capture_prefix_root_sha256(
        prefix_refs,
        identity_sha256=identity.identity_sha256,
        through_sequence=prefix_sequence,
    )
    required_streams = frozenset(
        {
            CaptureStream.NBBO_QUOTE,
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            CaptureStream.ADMISSION_ELIGIBILITY,
        }
    )
    dependency_profile = FSMDependencyProfile(
        required_streams=required_streams,
        required_read_ids=tuple(read_ids.values()),
        stream_dependencies=tuple(
            FSMStreamDependency(
                stream=stream,
                exact_provider_event_at_required=(
                    STREAM_POLICIES[stream].exact_provider_event_clock_required
                ),
                market_reference_at_required=(
                    STREAM_POLICIES[stream].market_reference_clock_required
                ),
                max_source_age_seconds=600.0,
                coverage_start_at=warmup,
            )
            for stream in required_streams
        ),
    )
    decision_output = CaptureDecisionOutput(
        decision_id=DECISION_ID,
        symbol="VEEE",
        action=CaptureDecisionAction.REJECT,
        fsm_state="fixture_not_committed",
        setup_role="adaptive_entry",
        order_intents=(),
        reason_code="adaptive_economics_not_yet_committed",
    )
    decision_payload = {
        "decision_id": DECISION_ID,
        "symbol": "VEEE",
        "decision_at": NOW.isoformat().replace("+00:00", "Z"),
        "input_prefix_sequence": prefix_sequence,
        "input_prefix_root_sha256": prefix_root,
        "required_read_ids": sorted(read_ids.values()),
        "fsm_dependency_profile": dependency_profile.to_dict(),
        "decision_output": decision_output.to_dict(),
        "decision_output_sha256": decision_output.decision_output_sha256,
        "adaptive_order_artifacts": [],
    }
    decision_event = event(
        sequence=prefix_sequence + 1,
        stream=CaptureStream.FSM_DECISION,
        available_at=NOW + timedelta(milliseconds=1),
        payload=decision_payload,
        symbol="VEEE",
    )
    exit_quote = event(
        sequence=prefix_sequence + 2,
        stream=CaptureStream.NBBO_QUOTE,
        available_at=exit_at + timedelta(seconds=1),
        payload={"bid": 11.00, "ask": 11.01},
        symbol="VEEE",
        exact_clock=True,
    )
    events.extend((decision_event, exit_quote))
    refs = tuple(CaptureEventRef.from_event(row) for row in events)
    event_index = {ref.event_sha256: ref for ref in refs}
    refs_by_sequence = {ref.sequence: ref for ref in refs}
    checkpoint = CaptureDecisionCheckpoint(
        identity_sha256=identity.identity_sha256,
        decision_id=DECISION_ID,
        symbol="VEEE",
        decision_at=NOW,
        available_at=decision_event.clocks.available_at,
        decision_event_sha256=decision_event.event_sha256,
        input_prefix_sequence=prefix_sequence,
        input_prefix_root_sha256=prefix_root,
        required_read_ids=tuple(read_ids.values()),
        decision_payload=decision_payload,
    )

    def coverage(
        stream: CaptureStream,
        *,
        continuous: bool = False,
        query_receipts: int = 0,
        event_count: int = 1,
        symbol: str | None = None,
    ) -> StreamCoverage:
        watermark = None
        if continuous:
            watermark = ProviderWatermark(
                stream=stream,
                provider="fixture",
                identity_sha256=identity.identity_sha256,
                event_watermark_at=exit_at + timedelta(seconds=1),
                emitted_available_at=exit_at + timedelta(seconds=2),
                bounded_lateness_seconds=1.0,
                max_observed_lateness_seconds=0.1,
                generation=identity.generation,
                symbol=symbol,
            )
        return StreamCoverage(
            stream=stream,
            identity_sha256=identity.identity_sha256,
            provider="fixture",
            first_available_at=warmup - timedelta(seconds=1),
            last_available_at=exit_at + timedelta(seconds=1),
            event_count=event_count,
            exact_event_clock_complete=continuous,
            content_verified=True,
            continuity_complete=True,
            watermark=watermark,
            query_receipt_count=query_receipts,
            symbol=symbol,
        )

    request = ReplayCoverageRequest(
        warmup_start_at=warmup,
        decision_at=NOW,
        exit_end_at=exit_at,
        decision_id=DECISION_ID,
        decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
        required_streams=frozenset(
            {
                CaptureStream.NBBO_QUOTE,
                CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                CaptureStream.ADMISSION_ELIGIBILITY,
            }
        ),
        required_read_ids=frozenset(read_ids.values()),
        symbol="VEEE",
        expected_identity_sha256=identity.identity_sha256,
    )
    manifest = CaptureCoverageManifest(
        identity=identity,
        event_index=event_index,
        decision_checkpoints=(checkpoint,),
        stream_coverage={
            CaptureStream.NBBO_QUOTE: coverage(
                CaptureStream.NBBO_QUOTE,
                continuous=True,
                event_count=2,
                symbol="VEEE",
            ),
            CaptureStream.ACCOUNT_RISK_SNAPSHOT: coverage(
                CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                query_receipts=3,
                event_count=3,
            ),
            CaptureStream.ADMISSION_ELIGIBILITY: coverage(
                CaptureStream.ADMISSION_ELIGIBILITY,
                symbol="VEEE",
            ),
            CaptureStream.CONFIG_SNAPSHOT: coverage(CaptureStream.CONFIG_SNAPSHOT),
            CaptureStream.FEATURE_FLAG_SNAPSHOT: coverage(
                CaptureStream.FEATURE_FLAG_SNAPSHOT
            ),
            CaptureStream.CODE_BUILD: coverage(CaptureStream.CODE_BUILD),
        },
        read_receipts=receipts,
        gaps=(),
        closed_cleanly=True,
        content_root_verified=True,
        replay_network_fallback_count=0,
        required_streams_full_fidelity=True,
        created_at=exit_at + timedelta(minutes=1),
    )
    evidence = {
        "account": _evidence(
            content_sha256=refs_by_sequence[5].payload_sha256,
            available=refs_by_sequence[5].available_at,
        ),
        "daily_pnl": _evidence(
            content_sha256=refs_by_sequence[5].payload_sha256,
            available=refs_by_sequence[5].available_at,
        ),
        "bbo": _evidence(
            content_sha256=refs_by_sequence[4].payload_sha256,
            available=refs_by_sequence[4].available_at,
        ),
        "structural_stop": _evidence(
            content_sha256=refs_by_sequence[6].payload_sha256,
            available=refs_by_sequence[6].available_at,
        ),
        "setup_quality": _evidence(
            content_sha256=refs_by_sequence[6].payload_sha256,
            available=refs_by_sequence[6].available_at,
        ),
        "volatility": _evidence(
            content_sha256=refs_by_sequence[6].payload_sha256,
            available=refs_by_sequence[6].available_at,
        ),
        "liquidity": _evidence(
            content_sha256=refs_by_sequence[6].payload_sha256,
            available=refs_by_sequence[6].available_at,
        ),
        "portfolio_heat": _evidence(
            content_sha256=refs_by_sequence[7].payload_sha256,
            available=refs_by_sequence[7].available_at,
        ),
        "correlation": _evidence(
            content_sha256=refs_by_sequence[6].payload_sha256,
            available=refs_by_sequence[6].available_at,
        ),
        "candidate_buying_power_estimate": _evidence(
            content_sha256=refs_by_sequence[8].payload_sha256,
            available=refs_by_sequence[8].available_at,
        ),
        "reservation_ledger": _evidence(
            content_sha256=refs_by_sequence[7].payload_sha256,
            available=refs_by_sequence[7].available_at,
        ),
        "code_build": _evidence(
            content_sha256=identity.code_build_sha256,
            available=refs_by_sequence[3].available_at,
        ),
        "effective_config": _evidence(
            content_sha256=identity.config_sha256,
            available=refs_by_sequence[1].available_at,
        ),
        "feature_flags": _evidence(
            content_sha256=identity.feature_flags_sha256,
            available=refs_by_sequence[2].available_at,
        ),
        "capture_prefix": _evidence(
            content_sha256=prefix_root,
            available=refs_by_sequence[8].available_at,
            source="capture_hash_chain",
        ),
    }
    if include_events:
        return request, manifest, evidence, tuple(events)
    return request, manifest, evidence


def _risk_inputs(*, symbol: str = "VEEE") -> AdaptiveRiskInputs:
    request, manifest, evidence = _coverage_context()
    checkpoint = manifest.decision_checkpoints[0]
    return AdaptiveRiskInputs(
        decision_id=DECISION_ID,
        replay_or_paper_run_id=RUN_ID,
        generation=3,
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        venue="alpaca",
        broker_environment="paper",
        symbol=symbol,
        side="long",
        as_of=NOW,
        account_identity_sha256=manifest.identity.account_identity_sha256,
        code_build_sha256=manifest.identity.code_build_sha256,
        effective_config_sha256=manifest.identity.config_sha256,
        feature_flags_sha256=manifest.identity.feature_flags_sha256,
        capture_prefix_root_sha256=checkpoint.input_prefix_root_sha256,
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=0.0,
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        policy_buying_power_capacity_usd=400_000.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
        candidate_buying_power_impact_per_share_usd=10.0,
        bid=9.99,
        ask=10.00,
        structural_stop=9.50,
        entry_slippage_bps=10.0,
        exit_slippage_bps=20.0,
        fees_per_share_usd=0.005,
        setup_quality=0.80,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000.0,
        recent_volume_shares=500_000.0,
        executable_depth_shares=100_000.0,
        correlation_cluster_id=f"equity:{symbol[:1].lower()}",
        evidence=evidence,
    )


def _resolution(*, symbol: str = "VEEE"):
    inputs = _risk_inputs(symbol=symbol)
    resolved = resolve_adaptive_risk(_policy(), inputs)
    assert resolved.valid
    return resolved


def _sealed_coverage_context(
    tmp_path,
    resolution,
    *,
    committed_packet_sha256: str | None = None,
    canonical_action: CaptureDecisionAction = CaptureDecisionAction.ORDER_INTENT,
):
    request, logical, evidence, source_events = _coverage_context(
        include_events=True
    )
    events = list(source_events)
    prior_checkpoint = logical.decision_checkpoints[0]
    decision_payload = dict(prior_checkpoint.decision_payload)
    inputs = _risk_inputs(symbol="VEEE")
    assert inputs.input_sha256 == resolution.input_sha256
    account_evidence = inputs.evidence["account"]
    client_order_id = "chili-replay-veee-adaptive-entry-1"
    account_snapshot = _fixture_account_snapshot(
        inputs.account_identity_sha256
    )
    assert account_evidence.content_sha256 == account_snapshot.snapshot_sha256
    reservation_request = AdaptiveRiskReservationRequest(
        policy=_policy(),
        inputs=inputs,
        account_snapshot=account_snapshot,
        account_scope=account_snapshot.account_scope,
        setup_family="adaptive_entry",
        correlation_cluster=inputs.correlation_cluster_id,
        client_order_id=client_order_id,
        entry_limit_price=float(resolution.effective_entry_price),
    )
    reservation_claim = build_adaptive_risk_reservation_claim(
        resolution.to_decision_packet(),
        claim_id=client_order_id,
    )
    canonical_intent = CaptureOrderIntent(
        intent_id=str(uuid.UUID(int=25)),
        client_order_id=client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": client_order_id}
        ),
        symbol="VEEE",
        side="buy",
        order_type="limit",
        quantity=int(resolution.quantity_shares),
        time_in_force="day",
        extended_hours=False,
        intent_role=CaptureOrderIntentRole.ENTRY,
        risk_increasing=True,
        decision_provenance_sha256=sha256_json(
            {
                "fixture": "unverified_predecision_attestation",
                "decision_id": DECISION_ID,
                "input_prefix_root_sha256": (
                    prior_checkpoint.input_prefix_root_sha256
                ),
            }
        ),
        adaptive_request_sha256=reservation_request.request_sha256,
        adaptive_decision_sha256=resolution.decision_packet_sha256,
        adaptive_resolution_sha256=resolution.economic_resolution_sha256,
        reservation_claim_sha256=reservation_claim.claim_sha256,
        limit_price=float(resolution.effective_entry_price),
    )
    adaptive_artifact = CaptureAdaptiveOrderArtifacts(
        order_intent_sha256=canonical_intent.order_intent_sha256,
        reservation_request=reservation_request.to_payload(),
        decision_packet=resolution.to_decision_packet(),
        reservation_claim=reservation_claim.to_payload(),
    )
    if canonical_action is CaptureDecisionAction.ORDER_INTENT:
        canonical_output = CaptureDecisionOutput(
            decision_id=DECISION_ID,
            symbol="VEEE",
            action=canonical_action,
            fsm_state="entry_ready",
            setup_role="adaptive_entry",
            order_intents=(canonical_intent,),
        )
        canonical_artifacts = [adaptive_artifact.to_dict()]
    else:
        canonical_output = CaptureDecisionOutput(
            decision_id=DECISION_ID,
            symbol="VEEE",
            action=canonical_action,
            fsm_state="entry_rejected",
            setup_role="adaptive_entry",
            order_intents=(),
            reason_code="fixture_canonical_entry_rejected",
        )
        canonical_artifacts = []
    decision_payload.update(
        {
            "adaptive_risk_decision_packet_sha256": (
                committed_packet_sha256 or resolution.decision_packet_sha256
            ),
            "resolved_economics": {
                "economic_resolution_sha256": (
                    resolution.economic_resolution_sha256
                ),
                "quantity_shares": int(resolution.quantity_shares),
                "effective_entry_price": float(
                    resolution.effective_entry_price
                ),
                "effective_stop_exit_price": float(
                    resolution.effective_stop_exit_price
                ),
                "risk_per_share_usd": float(resolution.risk_per_share_usd),
                "planned_structural_risk_usd": float(
                    resolution.planned_structural_risk_usd
                ),
                "planned_notional_usd": float(
                    resolution.planned_notional_usd
                ),
                "planned_buying_power_impact_usd": float(
                    resolution.planned_buying_power_impact_usd
                ),
            },
            "order_intent": {
                "symbol": "VEEE",
                "side": "long",
                "execution_family": "alpaca_spot",
                "venue": "alpaca",
                "quantity_shares": int(resolution.quantity_shares),
                "reference_entry_price": float(
                    resolution.effective_entry_price
                ),
                "structural_stop_exit_price": float(
                    resolution.effective_stop_exit_price
                ),
            },
            "decision_output": canonical_output.to_dict(),
            "decision_output_sha256": canonical_output.decision_output_sha256,
            "adaptive_order_artifacts": canonical_artifacts,
        }
    )
    decision_event_index = prior_checkpoint.input_prefix_sequence
    decision_event = replace(events[decision_event_index], payload=decision_payload)
    events[decision_event_index] = decision_event
    checkpoint = CaptureDecisionCheckpoint(
        identity_sha256=logical.identity.identity_sha256,
        decision_id=DECISION_ID,
        symbol="VEEE",
        decision_at=NOW,
        available_at=decision_event.clocks.available_at,
        decision_event_sha256=decision_event.event_sha256,
        input_prefix_sequence=prior_checkpoint.input_prefix_sequence,
        input_prefix_root_sha256=prior_checkpoint.input_prefix_root_sha256,
        required_read_ids=prior_checkpoint.required_read_ids,
        decision_payload=decision_payload,
    )
    request = replace(
        request, decision_checkpoint_sha256=checkpoint.checkpoint_sha256
    )

    available_at = request.exit_end_at + timedelta(seconds=3)

    def append_control(stream: CaptureStream, payload: dict) -> None:
        nonlocal available_at
        available_at += timedelta(microseconds=1)
        events.append(
            CaptureEvent(
                identity=logical.identity,
                sequence=len(events) + 1,
                stream=stream,
                provider="chili_capture",
                clocks=CaptureClocks(
                    received_at=available_at - timedelta(microseconds=1),
                    available_at=available_at,
                ),
                payload=payload,
            )
        )

    for coverage in logical.stream_coverage.values():
        if coverage.watermark is not None:
            append_control(
                CaptureStream.PROVIDER_WATERMARK,
                coverage.watermark.to_dict(),
            )
        append_control(CaptureStream.CAPTURE_HEALTH, coverage.to_dict())
    store = ContentAddressedCaptureStore(
        tmp_path / "adaptive-sealed-capture", compression_codec="zlib"
    )
    ingress = BoundedCaptureIngress(
        max_events=len(events), max_bytes=5_000_000, max_gap_keys=64
    )
    for event in events:
        assert ingress.submit(event)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=5_000_000,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(logical.identity)
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        logical.identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )
    manifest = CaptureCoverageManifest.from_verified_capture(
        verified,
        decision_checkpoints=(checkpoint,),
        stream_coverage=logical.stream_coverage,
        read_receipts=logical.read_receipts,
    )
    return request, manifest, evidence


def _arm(*, symbol: str = "VEEE", with_evidence: bool = True) -> rv3.RecordedArm:
    seed_evidence = None
    if with_evidence:
        request, manifest, _evidence_map = _coverage_context()
        checkpoint = manifest.decision_checkpoints[0]
        seed_evidence = rv3.ReplayEconomicSeedEvidence(
            coverage_request=request,
            coverage_manifest=manifest,
            risk_decision_packet=_resolution().to_decision_packet(),
            decision_available_at=checkpoint.available_at,
        )
    return rv3.RecordedArm(
        symbol=symbol,
        live_eligible_at_utc=NOW.isoformat(),
        economic_seed_evidence=seed_evidence,
    )


def test_sealed_economic_evidence_stays_blocked_before_any_database_write(
    tmp_path,
) -> None:
    resolution = _resolution()
    request, manifest, _evidence_map = _sealed_coverage_context(
        tmp_path, resolution
    )
    checkpoint = manifest.decision_checkpoints[0]
    evidence = rv3.ReplayEconomicSeedEvidence(
        coverage_request=request,
        coverage_manifest=manifest,
        risk_decision_packet=resolution.to_decision_packet(),
        decision_available_at=checkpoint.available_at,
    )

    with pytest.raises(ValueError, match="capture_coverage_not_complete"):
        evidence.validate_for("VEEE", execution_family="alpaca_spot")

    assert manifest.seal_binding is not None
    assert manifest.certification_blockers == (
        "capture_resource_binding_unverified",
        "capture_run_open_or_producer_roster_unverified",
    )


def test_legacy_seed_is_explicitly_noncertifying_without_replay_magic_caps(db) -> None:
    seed = rv3.seed_replay_session(db, _arm(with_evidence=False))
    session = db.get(TradingAutomationSession, seed.session_id)

    assert session is not None
    snapshot = session.risk_snapshot_json
    assert snapshot["momentum_policy_caps"] == dict(
        rv3.LEGACY_DIAGNOSTIC_POLICY_CAPS
    )
    assert snapshot[rv3.REPLAY_ECONOMIC_SEED_KEY] == {
        "mode": "legacy_config_diagnostic",
        "economic_seed_certifiable": False,
        "reason": "recorded_adaptive_risk_evidence_missing",
    }
    assert seed.economic_seed_mode == "legacy_config_diagnostic"
    assert seed.adaptive_risk_decision_sha256 is None

    tree = ast.parse(textwrap.dedent(inspect.getsource(rv3.seed_replay_session)))
    numeric_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    }
    assert 50 not in numeric_literals
    assert 250 not in numeric_literals
    assert 100_000 not in numeric_literals


def test_adaptive_seed_rejects_exact_seal_without_real_producer_lifecycle(
    db, tmp_path
) -> None:
    users_before = db.query(User).count()
    variants_before = db.query(MomentumStrategyVariant).count()
    sessions_before = db.query(TradingAutomationSession).count()
    resolution = _resolution()
    request, manifest, _evidence_map = _sealed_coverage_context(
        tmp_path, resolution
    )
    checkpoint = manifest.decision_checkpoints[0]
    arm = rv3.RecordedArm(
        symbol="VEEE",
        live_eligible_at_utc=NOW.isoformat(),
        economic_seed_evidence=rv3.ReplayEconomicSeedEvidence(
            coverage_request=request,
            coverage_manifest=manifest,
            risk_decision_packet=resolution.to_decision_packet(),
            decision_available_at=checkpoint.available_at,
        ),
    )
    with pytest.raises(ValueError, match="capture_coverage_not_complete"):
        rv3.seed_replay_session(db, arm, execution_family="alpaca_spot")

    assert manifest.seal_binding is not None
    assert "capture_run_open_or_producer_roster_unverified" in (
        manifest.certification_blockers
    )
    assert "capture_resource_binding_unverified" in (
        manifest.certification_blockers
    )
    assert db.query(User).count() == users_before
    assert db.query(MomentumStrategyVariant).count() == variants_before
    assert db.query(TradingAutomationSession).count() == sessions_before


def test_adaptive_packet_must_be_committed_by_the_sealed_fsm_decision(
    tmp_path,
) -> None:
    resolution = _resolution()
    request, manifest, _evidence_map = _sealed_coverage_context(
        tmp_path,
        resolution,
        committed_packet_sha256="f" * 64,
    )
    checkpoint = manifest.decision_checkpoints[0]
    evidence = rv3.ReplayEconomicSeedEvidence(
        coverage_request=request,
        coverage_manifest=manifest,
        risk_decision_packet=resolution.to_decision_packet(),
        decision_available_at=checkpoint.available_at,
    )

    with pytest.raises(
        ValueError,
        match="adaptive_risk_packet_not_committed_by_fsm_decision",
    ):
        evidence.validate_for("VEEE", execution_family="alpaca_spot")


@pytest.mark.parametrize(
    "canonical_action",
    [CaptureDecisionAction.ABSTAIN, CaptureDecisionAction.REJECT],
)
def test_legacy_entry_fields_cannot_override_canonical_non_order_decision(
    tmp_path,
    canonical_action: CaptureDecisionAction,
) -> None:
    resolution = _resolution()
    request, manifest, _evidence_map = _sealed_coverage_context(
        tmp_path,
        resolution,
        canonical_action=canonical_action,
    )
    checkpoint = manifest.decision_checkpoints[0]
    evidence = rv3.ReplayEconomicSeedEvidence(
        coverage_request=request,
        coverage_manifest=manifest,
        risk_decision_packet=resolution.to_decision_packet(),
        decision_available_at=checkpoint.available_at,
    )

    with pytest.raises(
        ValueError,
        match="adaptive_risk_canonical_decision_not_order_intent",
    ):
        evidence.validate_for("VEEE", execution_family="alpaca_spot")


@pytest.mark.parametrize("failure", ["symbol", "coverage"])
def test_bad_economic_evidence_rejects_before_any_seed_write(
    db, tmp_path, failure: str
) -> None:
    users_before = db.query(User).count()
    variants_before = db.query(MomentumStrategyVariant).count()
    resolution = _resolution()
    request, manifest, _evidence_map = _sealed_coverage_context(
        tmp_path, resolution
    )
    checkpoint = manifest.decision_checkpoints[0]
    if failure == "coverage":
        request = replace(
            request,
            expected_identity_sha256="e" * 64,
        )
    arm = rv3.RecordedArm(
        symbol="NXTC" if failure == "symbol" else "VEEE",
        live_eligible_at_utc=NOW.isoformat(),
        economic_seed_evidence=rv3.ReplayEconomicSeedEvidence(
            coverage_request=request,
            coverage_manifest=manifest,
            risk_decision_packet=resolution.to_decision_packet(),
            decision_available_at=checkpoint.available_at,
        ),
    )

    with pytest.raises(ValueError, match="not certifiable"):
        rv3.seed_replay_session(db, arm, execution_family="alpaca_spot")

    assert db.query(User).count() == users_before
    assert db.query(MomentumStrategyVariant).count() == variants_before


@pytest.mark.parametrize("failure", ["tampered_packet", "premature_availability"])
def test_tampered_or_impossible_clock_packet_rejects_before_seed_write(
    db, tmp_path, failure: str
) -> None:
    users_before = db.query(User).count()
    variants_before = db.query(MomentumStrategyVariant).count()
    resolution = _resolution()
    request, manifest, _evidence_map = _sealed_coverage_context(
        tmp_path, resolution
    )
    checkpoint = manifest.decision_checkpoints[0]
    packet = resolution.to_decision_packet()
    available_at = checkpoint.available_at
    if failure == "tampered_packet":
        packet = copy.deepcopy(packet)
        packet["quantity_shares"] += 1
    else:
        available_at = checkpoint.available_at - timedelta(microseconds=1)
    arm = rv3.RecordedArm(
        symbol="VEEE",
        live_eligible_at_utc=NOW.isoformat(),
        economic_seed_evidence=rv3.ReplayEconomicSeedEvidence(
            coverage_request=request,
            coverage_manifest=manifest,
            risk_decision_packet=packet,
            decision_available_at=available_at,
        ),
    )

    with pytest.raises(ValueError):
        rv3.seed_replay_session(db, arm, execution_family="alpaca_spot")

    assert db.query(User).count() == users_before
    assert db.query(MomentumStrategyVariant).count() == variants_before
