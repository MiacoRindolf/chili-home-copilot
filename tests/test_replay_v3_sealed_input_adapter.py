from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import socket
from types import SimpleNamespace
import uuid

import pytest

from app.config import settings
from app.models.trading import TradingAutomationEvent, TradingAutomationSession
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapeEvaluation,
    FirstDipTapePolicy,
    FirstDipTapeReadQuery,
    evaluate_first_dip_tape,
    first_dip_tape_window_from_capture,
)
from app.services.trading.momentum_neural.first_dip_tape_decision import (
    FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    _FirstDipPriorDetectorReference,
    _installed_sealed_replay_first_dip_final_authority_provider,
    _installed_sealed_replay_first_dip_tape_decision_authority,
    _resolve_first_dip_final_admission_with_active_provider,
    resolve_first_dip_tape_decision,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    build_adaptive_risk_request,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import RiskInputEvidence
from app.services.trading.momentum_neural.live_replay_capture import (
    FirstDipFinalCaptureFrontier,
)
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import market_profile as market_profile
from app.services.trading.momentum_neural import risk_evaluator as risk_evaluator
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
    VerifiedExactPrint,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CAPTURE_PRODUCER_LIFECYCLE_PROVIDER,
    CaptureClocks,
    ActiveCaptureContinuityEvidence,
    ActiveCaptureReadEvidence,
    CaptureBrokerOrderLifecycle,
    CaptureBrokerTransition,
    CaptureCoverageGrade,
    CaptureCoverageManifest,
    CaptureContractError,
    CaptureDecisionCheckpoint,
    CaptureEvent,
    CaptureEventRef,
    CaptureDecisionAction,
    CaptureDecisionOutput,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureOrderIntent,
    CaptureOrderIntentRole,
    CaptureReadReceipt,
    CaptureProducerSpec,
    CaptureRunOpen,
    CaptureRunIdentity,
    CaptureScannerProfile,
    CaptureScannerSnapshotQuery,
    CaptureStream,
    FSM_DEPENDENCY_PROFILE_SCHEMA_VERSION,
    FSMDependencyProfile,
    FSMStreamDependency,
    ProviderWatermark,
    ReplayCoverageRequest,
    StreamCoverage,
    STREAM_POLICIES,
    SCANNER_SNAPSHOT_PROVIDER,
    VerifiedReplayCapture,
    build_scanner_snapshot_payload,
    capture_prefix_root_sha256,
    canonical_json_bytes,
    captured_read_result_sha256,
    capture_final_decision_authority_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureWriterWorker,
    ContentAddressedCaptureStore,
)
from tests.test_adaptive_risk_reservation import _inputs, _request, _snapshot


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
SYMBOL = "VEEE"
DECISION_ID = "sealed-replay-v3-fixture"
PROFILE_STREAMS = frozenset(
    {
        CaptureStream.NBBO_QUOTE,
        CaptureStream.PROVIDER_OHLCV,
        CaptureStream.ADMISSION_ELIGIBILITY,
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
    }
)
REPLAY_STREAMS = PROFILE_STREAMS | {
    CaptureStream.BROKER_ORDER_LIFECYCLE,
}


@dataclass(frozen=True)
class _Fixture:
    capture: VerifiedReplayCapture
    manifest: CaptureCoverageManifest
    request: ReplayCoverageRequest
    initial_quote_sha256: str
    future_quote_sha256: str
    tie_sha256s: tuple[str, ...]
    first_dip_print_sha256s: tuple[str, ...] = ()


def _complete_grade(
    _request: ReplayCoverageRequest,
    manifest: CaptureCoverageManifest,
) -> CaptureCoverageGrade:
    # The focused adapter tests isolate its behavior from the still-active
    # producer-lifecycle/resource certification workstream. The separate
    # incomplete-grade test uses the real grader and proves this adapter cannot
    # bypass those blockers.
    return CaptureCoverageGrade(
        replayable=True,
        grade="complete",
        reasons=(),
        manifest_sha256=manifest.manifest_sha256,
    )


def _sealed_fixture(
    tmp_path,
    *,
    bad_ohlcv_query_binding: bool = False,
    duplicate_nbbo: bool = False,
    tie_inputs: bool = False,
    same_timestamp_late_nbbo: bool = False,
    decision_publication_delay_seconds: float = 0.0,
    postprefix_earlier_available_nbbo: bool = False,
    bad_broker_authority: bool = False,
    no_order: bool = False,
    in_prefix_unselected_nbbo: bool = False,
    in_prefix_unselected_eligibility: bool = False,
    first_dip_tape: bool = False,
    empty_first_dip_window: bool = False,
    first_dip_receipt_corruption: str | None = None,
    post_receipt_same_clock_first_dip: bool = False,
    post_receipt_first_dip_available_delay_seconds: float = 0.0,
    first_dip_setup_role: str | None = None,
    future_clock_first_dip: bool = False,
    empty_first_dip_evaluation_corruption: str | None = None,
    first_dip_receipt_commit_delay_seconds: float = 0.0,
    first_dip_predecision_control_delay_seconds: float = 0.0,
    first_dip_policy: FirstDipTapePolicy | None = None,
    first_dip_tape_purpose: str | None = None,
    first_dip_final_frontier: bool = False,
    first_dip_final_receipt_corruption: str | None = None,
    scanner_snapshot: bool = False,
    microstructure_trade_flow: bool = False,
    decision_offset_seconds: float = 10.0,
) -> _Fixture:
    identity = CaptureRunIdentity(
        run_id=str(uuid.uuid4()),
        generation=7,
        code_build_sha256="1" * 64,
        config_sha256="2" * 64,
        feature_flags_sha256="3" * 64,
        account_identity_sha256="4" * 64,
        broker="alpaca",
        broker_environment="paper",
    )
    decision_at = BASE + timedelta(seconds=decision_offset_seconds)
    exit_at = BASE + timedelta(seconds=30)
    tape_policy = (
        first_dip_policy
        or FirstDipTapePolicy(
            window_seconds=1.0 if first_dip_final_frontier else 15.0,
            max_source_age_seconds=(
                1.0 if first_dip_final_frontier else 10.0
            ),
            tick_rate_floor_pctile=0.0,
        )
        if first_dip_tape
        else None
    )
    events: list[CaptureEvent] = []

    def add(
        stream: CaptureStream,
        *,
        available_at: datetime,
        payload: dict,
        symbol: str | None,
        provider_event_at: datetime | None = None,
        market_reference_at: datetime | None = None,
        query: dict | None = None,
        provider: str = "fixture",
    ) -> CaptureEvent:
        event = CaptureEvent(
            identity=identity,
            sequence=len(events) + 1,
            stream=stream,
            provider=provider,
            symbol=symbol,
            clocks=CaptureClocks(
                provider_event_at=provider_event_at,
                market_reference_at=market_reference_at,
                received_at=available_at - timedelta(milliseconds=1),
                available_at=available_at,
            ),
            query=query,
            payload=payload,
        )
        events.append(event)
        return event

    final_producer = None
    if (
        first_dip_final_receipt_corruption is not None
        and not first_dip_final_frontier
    ):
        raise AssertionError("final receipt corruption requires a final frontier")
    if first_dip_final_frontier:
        if not first_dip_tape or not no_order:
            raise AssertionError(
                "final first-dip fixture requires tape plus a no-order decision"
            )
        final_producer = CaptureProducerSpec(
            producer_id="sealed_final_iqfeed",
            instance_id=str(uuid.uuid4()),
            generation=identity.generation,
            streams=(CaptureStream.IQFEED_PRINT,),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256="9" * 64,
        )
        run_open = CaptureRunOpen(
            identity_sha256=identity.identity_sha256,
            run_id=identity.run_id,
            generation=identity.generation,
            opened_at=BASE - timedelta(seconds=1),
            heartbeat_timeout_seconds=60.0,
            resource_binding_sha256="9" * 64,
            producers=(final_producer,),
        )
        add(
            CaptureStream.CAPTURE_HEALTH,
            available_at=BASE - timedelta(seconds=1),
            symbol=None,
            provider=CAPTURE_PRODUCER_LIFECYCLE_PROVIDER,
            payload=run_open.to_dict(),
        )

    tied_at = BASE + timedelta(seconds=2)
    quote_available = tied_at if tie_inputs else BASE + timedelta(seconds=1)
    initial_quote = add(
        CaptureStream.NBBO_QUOTE,
        available_at=quote_available,
        provider_event_at=BASE + timedelta(milliseconds=100),
        symbol=SYMBOL,
        payload={
            "schema_version": rv3.SEALED_REPLAY_NBBO_SCHEMA_VERSION,
            "symbol": SYMBOL,
            "bid": 9.99,
            "ask": 10.01,
            "last": 10.00,
        },
    )
    if duplicate_nbbo:
        add(
            CaptureStream.NBBO_QUOTE,
            available_at=BASE + timedelta(seconds=1, milliseconds=500),
            provider_event_at=BASE + timedelta(milliseconds=100),
            symbol=SYMBOL,
            payload={
                "schema_version": rv3.SEALED_REPLAY_NBBO_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "bid": 9.98,
                "ask": 10.02,
                "last": 10.00,
            },
        )

    ohlcv_query = {
        "schema_version": rv3.SEALED_REPLAY_OHLCV_QUERY_SCHEMA_VERSION,
        "call": {"symbol": SYMBOL, "interval": "1m", "period": "1d"},
        "provider_parameters": {
            "adjusted": True,
            "limit": 50_000,
            "sort": "asc",
        },
    }
    ohlcv = add(
        CaptureStream.PROVIDER_OHLCV,
        available_at=tied_at,
        market_reference_at=BASE,
        symbol=SYMBOL,
        query=ohlcv_query,
        payload={
            "schema_version": rv3.SEALED_REPLAY_OHLCV_SCHEMA_VERSION,
            "query_sha256": (
                "f" * 64 if bad_ohlcv_query_binding else sha256_json(ohlcv_query)
            ),
            "rows": [
                {
                    "market_reference_at": (BASE - timedelta(minutes=1)).isoformat(),
                    "open": 9.80,
                    "high": 10.00,
                    "low": 9.75,
                    "close": 9.95,
                    "volume": 125_000.0,
                },
                {
                    "market_reference_at": BASE.isoformat(),
                    "open": 9.95,
                    "high": 10.05,
                    "low": 9.90,
                    "close": 10.00,
                    "volume": 180_000.0,
                },
            ],
        },
    )
    eligibility_available = tied_at if tie_inputs else BASE + timedelta(seconds=3)
    eligibility_reference = BASE + timedelta(seconds=1, milliseconds=900)
    eligibility = add(
        CaptureStream.ADMISSION_ELIGIBILITY,
        available_at=eligibility_available,
        market_reference_at=eligibility_reference,
        symbol=SYMBOL,
        payload={
            "schema_version": rv3.SEALED_REPLAY_ELIGIBILITY_SCHEMA_VERSION,
            "symbol": SYMBOL,
            "live_eligible": True,
            "freshness_at": eligibility_reference.isoformat(),
        },
    )
    account_payload = {
        "schema_version": rv3.SEALED_REPLAY_ACCOUNT_RISK_SCHEMA_VERSION,
        "account_identity_sha256": identity.account_identity_sha256,
        "equity_usd": 75_000.0,
        "buying_power_usd": 300_000.0,
        "cash_usd": 75_000.0,
        "daily_risk_budget_usd": 1_500.0,
        "portfolio_heat_usd": 250.0,
    }
    account_query = {
        "schema_version": rv3.SEALED_REPLAY_ACCOUNT_RISK_QUERY_SCHEMA_VERSION,
        "account_identity_sha256": identity.account_identity_sha256,
        "fields": sorted(key for key in account_payload if key.endswith("_usd")),
    }
    account = add(
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        available_at=BASE + timedelta(seconds=4),
        market_reference_at=BASE + timedelta(seconds=3),
        symbol=None,
        query=account_query,
        payload=account_payload,
    )
    scanner = None
    if scanner_snapshot:
        scanner_profile = CaptureScannerProfile(
            profile_id="equity_ross_smallcap",
            asset_class="equity",
            price_min=1.0,
            price_max=20.0,
            min_dollar_volume=1_000_000.0,
            min_change_pct=5.0,
            snapshot_max_age_seconds=300.0,
        )
        scanner_query = CaptureScannerSnapshotQuery(
            symbol=SYMBOL,
            include_otc=False,
            max_age_seconds=300.0,
            provider_cache_ttl_seconds=300.0,
            profile=scanner_profile,
            profile_sha256=scanner_profile.profile_sha256,
            config_sha256=identity.config_sha256,
        )
        scanner_reference = BASE + timedelta(seconds=3, milliseconds=500)
        scanner = add(
            CaptureStream.SCANNER_SNAPSHOT,
            available_at=BASE + timedelta(seconds=4, milliseconds=500),
            market_reference_at=scanner_reference,
            symbol=SYMBOL,
            provider=SCANNER_SNAPSHOT_PROVIDER,
            query=scanner_query.to_dict(),
            payload=build_scanner_snapshot_payload(
                scanner_query,
                market_reference_at=scanner_reference,
                source_projection={
                    "ticker": SYMBOL,
                    "todaysChangePerc": 31.0,
                    "updated": (
                        int(scanner_reference.timestamp()) * 1_000_000_000
                        + scanner_reference.microsecond * 1_000
                    ),
                    "lastTrade": {
                        "p": 4.20,
                        "t": (
                            int(scanner_reference.timestamp())
                            * 1_000_000_000
                            + scanner_reference.microsecond * 1_000
                        ),
                    },
                    "day": {"c": 4.05, "vw": 3.95, "v": 500_000.0},
                    "min": {"c": 4.18, "av": 600_000.0},
                },
            ),
        )
    first_dip_prints: tuple[CaptureEvent, ...] = ()
    if first_dip_tape or microstructure_trade_flow:
        if first_dip_final_frontier:
            assert tape_policy is not None
            detector_window_start = decision_at - timedelta(
                seconds=tape_policy.window_seconds
            )
            add(
                CaptureStream.IQFEED_PRINT,
                available_at=detector_window_start,
                provider_event_at=detector_window_start
                - timedelta(milliseconds=1),
                symbol=SYMBOL,
                provider="iqfeed",
                payload={
                    "schema_version": (
                        rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION
                    ),
                    "symbol": SYMBOL,
                    "price": 9.99,
                    "size": 50.0,
                    "bid": 9.98,
                    "ask": 9.99,
                    "conditions": ["sealed-final-warmup-boundary"],
                },
            )
        if empty_first_dip_window:
            if first_dip_receipt_corruption is not None:
                raise AssertionError(
                    "empty first-dip fixture cannot also corrupt its receipt"
                )
            add(
                CaptureStream.IQFEED_PRINT,
                available_at=BASE + timedelta(seconds=4),
                provider_event_at=decision_at - timedelta(seconds=16),
                symbol=SYMBOL,
                provider="iqfeed",
                payload={
                    "schema_version": rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION,
                    "symbol": SYMBOL,
                    "price": 9.90,
                    "size": 50.0,
                    "bid": 9.89,
                    "ask": 9.90,
                    "conditions": ["fixture-before-empty-window"],
                },
            )
            if future_clock_first_dip:
                add(
                    CaptureStream.IQFEED_PRINT,
                    available_at=BASE + timedelta(seconds=4, milliseconds=50),
                    provider_event_at=decision_at + timedelta(milliseconds=1),
                    symbol=SYMBOL,
                    provider="iqfeed",
                    payload={
                        "schema_version": (
                            rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION
                        ),
                        "symbol": SYMBOL,
                        "price": 9.91,
                        "size": 25.0,
                        "bid": 9.90,
                        "ask": 9.91,
                        "conditions": ["fixture-future-provider-clock"],
                    },
                )
        else:
            first_dip_prints = tuple(
                add(
                    CaptureStream.IQFEED_PRINT,
                    available_at=(
                        decision_at
                        - timedelta(milliseconds=29 - (10 * index))
                        if first_dip_final_frontier
                        else BASE
                        + timedelta(
                            seconds=4,
                            milliseconds=(
                                310 if first_dip_policy is not None else 100
                            )
                            + index,
                        )
                    ),
                    provider_event_at=(
                        decision_at
                        - timedelta(milliseconds=30 - (10 * index))
                        if first_dip_final_frontier
                        else BASE
                        + timedelta(
                            seconds=4,
                            milliseconds=(
                                300 if first_dip_policy is not None else 90
                            )
                            + index,
                        )
                    ),
                    symbol=SYMBOL,
                    provider="iqfeed",
                    payload={
                        "schema_version": (
                            rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION
                        ),
                        "symbol": SYMBOL,
                        "price": price,
                        "size": size,
                        "bid": price - 0.01,
                        "ask": price,
                        "conditions": ["fixture-only"],
                    },
                )
                for index, (price, size) in enumerate(
                    ((10.00, 100.0), (10.01, 200.0), (10.02, 400.0))
                )
            )
        if first_dip_receipt_corruption == "extra":
            add(
                CaptureStream.IQFEED_PRINT,
                available_at=BASE + timedelta(seconds=4, milliseconds=200),
                provider_event_at=BASE - timedelta(seconds=10),
                symbol=SYMBOL,
                provider="iqfeed",
                payload={
                    "schema_version": (
                        rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION
                    ),
                    "symbol": SYMBOL,
                    "price": 9.99,
                    "size": 50.0,
                    "bid": 9.98,
                    "ask": 9.99,
                    "conditions": ["fixture-only"],
                },
            )
    if in_prefix_unselected_nbbo:
        add(
            CaptureStream.NBBO_QUOTE,
            available_at=BASE + timedelta(seconds=5),
            provider_event_at=BASE + timedelta(seconds=4, milliseconds=900),
            symbol=SYMBOL,
            payload={
                "schema_version": rv3.SEALED_REPLAY_NBBO_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "bid": 10.99,
                "ask": 11.01,
                "last": 11.00,
            },
        )
    if in_prefix_unselected_eligibility:
        flipped_reference = BASE + timedelta(seconds=4, milliseconds=800)
        add(
            CaptureStream.ADMISSION_ELIGIBILITY,
            available_at=BASE + timedelta(seconds=5),
            market_reference_at=flipped_reference,
            symbol=SYMBOL,
            payload={
                "schema_version": rv3.SEALED_REPLAY_ELIGIBILITY_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "live_eligible": False,
                "freshness_at": flipped_reference.isoformat(),
            },
        )
    client_order_id = "client-1"
    intent = CaptureOrderIntent(
        intent_id=str(uuid.uuid4()),
        client_order_id=client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": client_order_id}
        ),
        symbol=SYMBOL,
        side="sell",
        order_type="limit",
        quantity=100,
        time_in_force="day",
        extended_hours=False,
        intent_role=CaptureOrderIntentRole.EXIT,
        risk_increasing=False,
        decision_provenance_sha256="a" * 64,
        limit_price=9.99,
    )
    decision_output = CaptureDecisionOutput(
        decision_id=DECISION_ID,
        symbol=SYMBOL,
        action=(
            CaptureDecisionAction.REJECT
            if no_order
            else CaptureDecisionAction.ORDER_INTENT
        ),
        fsm_state="entry_rejected" if no_order else "risk_exit",
        setup_role=(
            first_dip_setup_role
            or (
                "first_dip_reclaim"
                if first_dip_tape
                else ("adaptive_entry" if no_order else "exit")
            )
        ),
        order_intents=() if no_order else (intent,),
        reason_code="fixture_no_order" if no_order else None,
    )
    profile_streams = PROFILE_STREAMS | (
        frozenset({CaptureStream.IQFEED_PRINT})
        if first_dip_tape or microstructure_trade_flow
        else frozenset()
    ) | (
        frozenset({CaptureStream.SCANNER_SNAPSHOT})
        if scanner_snapshot
        else frozenset()
    )
    replay_streams = profile_streams | (
        frozenset()
        if no_order
        else frozenset({CaptureStream.BROKER_ORDER_LIFECYCLE})
    )

    read_ids = {
        CaptureStream.NBBO_QUOTE: str(uuid.uuid4()),
        CaptureStream.PROVIDER_OHLCV: str(uuid.uuid4()),
        CaptureStream.ADMISSION_ELIGIBILITY: str(uuid.uuid4()),
        CaptureStream.ACCOUNT_RISK_SNAPSHOT: str(uuid.uuid4()),
    }
    if first_dip_tape or microstructure_trade_flow:
        read_ids[CaptureStream.IQFEED_PRINT] = str(uuid.uuid4())
    if scanner_snapshot:
        read_ids[CaptureStream.SCANNER_SNAPSHOT] = str(uuid.uuid4())
    refs = {event.event_sha256: CaptureEventRef.from_event(event) for event in events}
    receipts: list[CaptureReadReceipt] = []
    receipt_sources: list[
        tuple[CaptureStream, tuple[CaptureEvent, ...]]
    ] = [
        (CaptureStream.NBBO_QUOTE, (initial_quote,)),
        (CaptureStream.PROVIDER_OHLCV, (ohlcv,)),
        (CaptureStream.ADMISSION_ELIGIBILITY, (eligibility,)),
        (CaptureStream.ACCOUNT_RISK_SNAPSHOT, (account,)),
    ]
    if first_dip_tape or microstructure_trade_flow:
        receipt_sources.append((CaptureStream.IQFEED_PRINT, first_dip_prints))
    if scanner_snapshot:
        assert scanner is not None
        receipt_sources.append((CaptureStream.SCANNER_SNAPSHOT, (scanner,)))
    for stream, sources in receipt_sources:
        receipt_sources_for_stream = sources
        if stream is CaptureStream.IQFEED_PRINT:
            if first_dip_receipt_corruption == "omitted":
                receipt_sources_for_stream = (sources[0], sources[-1])
            elif first_dip_receipt_corruption == "lowered_frontier":
                receipt_sources_for_stream = (sources[0],)
            elif first_dip_receipt_corruption == "reordered":
                receipt_sources_for_stream = tuple(reversed(sources))
            elif first_dip_receipt_corruption == "extra":
                extra_print = next(
                    event
                    for event in events
                    if event.stream is CaptureStream.IQFEED_PRINT
                    and event not in sources
                )
                receipt_sources_for_stream = (extra_print, *sources)
        returned_at = (
            decision_at
            if stream is CaptureStream.IQFEED_PRINT
            else BASE + timedelta(seconds=5, milliseconds=100)
        )
        receipt_query = None
        if stream is CaptureStream.IQFEED_PRINT:
            matching_prints = tuple(
                event
                for event in events
                if event.stream is CaptureStream.IQFEED_PRINT
                and event.provider == "iqfeed"
                and event.symbol == SYMBOL
                and event.clocks.available_at <= returned_at
            )
            source_frontier_sequence = max(
                event.sequence for event in matching_prints
            )
            if first_dip_receipt_corruption == "unavailable_frontier":
                source_frontier_sequence += 100
            elif first_dip_receipt_corruption == "lowered_frontier":
                source_frontier_sequence = sources[0].sequence
            if microstructure_trade_flow:
                receipt_query = CaptureMicrostructureReadQuery(
                    operation=CaptureMicrostructureOperation.TRADE_FLOW,
                    stream=CaptureStream.IQFEED_PRINT,
                    symbol=SYMBOL,
                    provider="iqfeed",
                    event_start_exclusive=decision_at - timedelta(seconds=15),
                    event_end_inclusive=decision_at,
                    decision_at=decision_at,
                    available_at_most=decision_at,
                    source_frontier_sequence=source_frontier_sequence,
                    source_clock_basis="provider_event_at",
                    parameters={"window_seconds": 15.0},
                ).to_dict()
            else:
                assert tape_policy is not None
                receipt_query = FirstDipTapeReadQuery(
                    symbol=SYMBOL,
                    provider="iqfeed",
                    event_start_exclusive=decision_at
                    - timedelta(seconds=tape_policy.window_seconds),
                    event_end_inclusive=decision_at,
                    decision_at=decision_at,
                    available_at_most=decision_at,
                    source_frontier_sequence=source_frontier_sequence,
                    policy_sha256=tape_policy.policy_sha256,
                ).to_dict()
            if first_dip_receipt_corruption == "missing_query":
                receipt_query = None
        source_refs = tuple(
            refs[source.event_sha256] for source in receipt_sources_for_stream
        )
        receipt = CaptureReadReceipt(
            read_id=read_ids[stream],
            decision_id=DECISION_ID,
            identity_sha256=identity.identity_sha256,
            stream=stream,
            provider=(
                "iqfeed"
                if stream is CaptureStream.IQFEED_PRINT
                else (
                    SCANNER_SNAPSHOT_PROVIDER
                    if stream is CaptureStream.SCANNER_SNAPSHOT
                    else "fixture"
                )
            ),
            symbol=(
                None
                if stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT
                else SYMBOL
            ),
            requested_at=(
                decision_at - timedelta(microseconds=len(receipts) + 1)
                if stream is CaptureStream.IQFEED_PRINT
                else BASE
                + timedelta(seconds=5, microseconds=len(receipts) + 1)
            ),
            returned_at=returned_at,
            query_sha256=(
                sha256_json(receipt_query)
                if receipt_query is not None
                else sources[0].query_sha256 or "0" * 64
            ),
            source_event_sha256s=tuple(
                source.event_sha256 for source in receipt_sources_for_stream
            ),
            empty_result=not receipt_sources_for_stream,
            result_sha256=captured_read_result_sha256(source_refs),
            query=receipt_query,
        )
        receipts.append(receipt)
        receipt_commit_at = max(
            returned_at,
            (
                BASE
                + timedelta(seconds=5, milliseconds=100 + 10 * len(receipts))
                if first_dip_policy is not None
                else BASE
                + timedelta(
                    seconds=6,
                    milliseconds=500 * len(receipts),
                )
            ),
        )
        if (
            first_dip_final_frontier
            and stream is not CaptureStream.IQFEED_PRINT
        ):
            receipt_commit_at = max(
                receipt_commit_at,
                decision_at
                - timedelta(milliseconds=9 - len(receipts)),
            )
        if (
            stream is CaptureStream.IQFEED_PRINT
            and first_dip_receipt_commit_delay_seconds
        ):
            receipt_commit_at += timedelta(
                seconds=first_dip_receipt_commit_delay_seconds
            )
        add(
            CaptureStream.READ_RECEIPT,
            available_at=receipt_commit_at,
            symbol=None,
            provider="chili_capture",
            payload=receipt.to_dict(),
        )

    if post_receipt_same_clock_first_dip:
        if not first_dip_tape:
            raise AssertionError(
                "post-receipt first-dip source requires first_dip_tape"
            )
        # This callback has the same availability clock as the committed tape
        # read, but a higher durable sequence.  The query's source frontier is
        # the causal tie-breaker, so sealed replay must not retroactively add it
        # to the earlier exact receipt.
        add(
            CaptureStream.IQFEED_PRINT,
            available_at=decision_at
            + timedelta(
                seconds=post_receipt_first_dip_available_delay_seconds
            ),
            provider_event_at=decision_at,
            symbol=SYMBOL,
            provider="iqfeed",
            payload={
                "schema_version": rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "price": 10.03,
                "size": 800.0,
                "bid": 10.02,
                "ask": 10.03,
                "conditions": ["fixture-post-receipt"],
            },
        )

    predecision_tape_coverage = None
    if first_dip_tape and (
        first_dip_predecision_control_delay_seconds
        or first_dip_final_frontier
    ):
        iqfeed_rows = tuple(
            event
            for event in events
            if event.stream is CaptureStream.IQFEED_PRINT
        )
        control_base = decision_at + timedelta(
            seconds=first_dip_predecision_control_delay_seconds
        )
        tape_watermark = ProviderWatermark(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            identity_sha256=identity.identity_sha256,
            event_watermark_at=decision_at,
            emitted_available_at=control_base,
            bounded_lateness_seconds=1.0,
            max_observed_lateness_seconds=0.001,
            generation=identity.generation,
            symbol=SYMBOL,
        )
        predecision_tape_coverage = StreamCoverage(
            stream=CaptureStream.IQFEED_PRINT,
            identity_sha256=identity.identity_sha256,
            provider="iqfeed",
            symbol=SYMBOL,
            first_available_at=min(
                event.clocks.available_at for event in iqfeed_rows
            ),
            last_available_at=max(
                event.clocks.available_at for event in iqfeed_rows
            ),
            event_count=len(iqfeed_rows),
            exact_event_clock_complete=True,
            content_verified=True,
            continuity_complete=True,
            watermark=tape_watermark,
        )
        add(
            CaptureStream.PROVIDER_WATERMARK,
            available_at=control_base,
            symbol=None,
            provider="chili_capture",
            payload=tape_watermark.to_dict(),
        )
        add(
            CaptureStream.CAPTURE_HEALTH,
            available_at=(
                control_base
                if first_dip_final_frontier
                else control_base + timedelta(microseconds=1)
            ),
            symbol=None,
            provider="chili_capture",
            payload=predecision_tape_coverage.to_dict(),
        )

    prefix_sequence = len(events)
    prefix_refs = tuple(CaptureEventRef.from_event(event) for event in events)
    prefix_root = capture_prefix_root_sha256(
        prefix_refs,
        identity_sha256=identity.identity_sha256,
        through_sequence=prefix_sequence,
    )
    dependency_profile = FSMDependencyProfile(
        required_streams=profile_streams,
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
                max_source_age_seconds=(
                    tape_policy.max_source_age_seconds
                    if stream is CaptureStream.IQFEED_PRINT
                    and tape_policy is not None
                    else 3_600.0
                ),
                coverage_start_at=(
                    decision_at - timedelta(seconds=tape_policy.window_seconds)
                    if stream is CaptureStream.IQFEED_PRINT
                    and tape_policy is not None
                    else BASE
                ),
            )
            for stream in profile_streams
        ),
    )
    decision_payload = {
        "decision_id": DECISION_ID,
        "symbol": SYMBOL,
        "decision_at": decision_at.isoformat(),
        "input_prefix_sequence": prefix_sequence,
        "input_prefix_root_sha256": prefix_root,
        "required_read_ids": sorted(read_ids.values()),
        "fsm_dependency_profile": dependency_profile.to_dict(),
        "decision_output": decision_output.to_dict(),
        "decision_output_sha256": decision_output.decision_output_sha256,
        "adaptive_order_artifacts": [],
    }
    if first_dip_tape:
        decision_payload["first_dip_tape_read_id"] = read_ids[
            CaptureStream.IQFEED_PRINT
        ]
        if first_dip_tape_purpose is not None:
            decision_payload["first_dip_tape_purpose"] = first_dip_tape_purpose
        assert tape_policy is not None
        tape_receipt = next(
            receipt
            for receipt in receipts
            if receipt.stream is CaptureStream.IQFEED_PRINT
        )
        evaluation_receipt = CaptureReadReceipt(
            read_id=tape_receipt.read_id,
            decision_id=tape_receipt.decision_id,
            identity_sha256=tape_receipt.identity_sha256,
            stream=tape_receipt.stream,
            provider=tape_receipt.provider,
            symbol=tape_receipt.symbol,
            requested_at=tape_receipt.requested_at,
            returned_at=tape_receipt.returned_at,
            query_sha256=tape_receipt.query_sha256,
            source_event_sha256s=tuple(
                event.event_sha256 for event in first_dip_prints
            ),
            empty_result=not first_dip_prints,
            result_sha256=captured_read_result_sha256(
                tuple(
                    refs[event.event_sha256] for event in first_dip_prints
                )
            ),
            query=tape_receipt.query,
        )
        tape_evaluation = evaluate_first_dip_tape(
            first_dip_tape_window_from_capture(
                evaluation_receipt,
                first_dip_prints,
            ),
            policy=tape_policy,
            decision_at=decision_at,
            symbol=SYMBOL,
        )
        tape_evaluation_payload = tape_evaluation.to_dict()
        if empty_first_dip_evaluation_corruption is not None:
            if not empty_first_dip_window:
                raise AssertionError(
                    "empty evaluation corruption requires an empty first-dip window"
                )
            if empty_first_dip_evaluation_corruption == "newest_source_age":
                tape_evaluation_payload["newest_source_age_seconds"] = 123.0
            elif empty_first_dip_evaluation_corruption == "schema":
                tape_evaluation_payload["schema_version"] = (
                    "chili.first-dip-tape-evaluation.v999"
                )
            elif empty_first_dip_evaluation_corruption == "extra_field":
                tape_evaluation_payload["candidate_self_attested"] = True
            elif empty_first_dip_evaluation_corruption == "non_iterable_sources":
                tape_evaluation_payload["source_event_sha256s"] = 7
            else:
                raise AssertionError(
                    "unknown empty first-dip evaluation corruption"
                )
        decision_payload.update(
            {
                "first_dip_tape_policy": tape_policy.to_dict(),
                "first_dip_tape_policy_sha256": tape_policy.policy_sha256,
                "first_dip_tape_evaluation": tape_evaluation_payload,
                "first_dip_tape_evaluation_sha256": (
                    sha256_json(tape_evaluation_payload)
                ),
            }
        )
    final_frontier: FirstDipFinalCaptureFrontier | None = None
    if first_dip_final_frontier:
        assert tape_policy is not None
        assert final_producer is not None
        assert predecision_tape_coverage is not None
        detector_receipt = next(
            receipt
            for receipt in receipts
            if receipt.stream is CaptureStream.IQFEED_PRINT
        )
        detector_receipt_event = next(
            event
            for event in events
            if event.stream is CaptureStream.READ_RECEIPT
            and event.payload.get("read_id") == detector_receipt.read_id
        )
        detector_evaluation = FirstDipTapeEvaluation.from_dict(
            decision_payload["first_dip_tape_evaluation"]
        )
        assert detector_evaluation.status == "valid_positive"

        adaptive_at = decision_at + timedelta(milliseconds=20)
        snapshot = replace(
            _snapshot(account_scope="alpaca:paper:sealed-final"),
            account_identity_sha256=identity.account_identity_sha256,
            observed_at=adaptive_at - timedelta(milliseconds=8),
            available_at=adaptive_at - timedelta(milliseconds=7),
        )
        initial_inputs = _inputs(
            snapshot,
            symbol=SYMBOL,
            decision_id=DECISION_ID,
            cluster="equity:veee",
        )
        account_evidence = RiskInputEvidence(
            source=snapshot.source,
            observed_at=snapshot.observed_at,
            available_at=snapshot.available_at,
            content_sha256=snapshot.snapshot_sha256,
            provider_generation=snapshot.provider_generation,
        )
        adaptive_evidence = {
            name: RiskInputEvidence(
                source=f"sealed-fixture:{name}",
                observed_at=adaptive_at - timedelta(milliseconds=6),
                available_at=adaptive_at - timedelta(milliseconds=5),
                content_sha256=sha256_json({"sealed_fixture": name}),
                provider_generation="sealed-final-fixture-v1",
            )
            for name in initial_inputs.evidence
        }
        adaptive_evidence["account"] = account_evidence
        adaptive_evidence["daily_pnl"] = account_evidence
        adaptive_evidence["code_build"] = replace(
            adaptive_evidence["code_build"],
            content_sha256=identity.code_build_sha256,
        )
        adaptive_evidence["effective_config"] = replace(
            adaptive_evidence["effective_config"],
            content_sha256=identity.config_sha256,
        )
        adaptive_evidence["feature_flags"] = replace(
            adaptive_evidence["feature_flags"],
            content_sha256=identity.feature_flags_sha256,
        )
        adaptive_evidence["capture_prefix"] = RiskInputEvidence(
            source="sealed-fixture:detector-prefix",
            observed_at=decision_at,
            available_at=decision_at,
            content_sha256=prefix_root,
            provider_generation="sealed-detector-prefix-v1",
        )
        adaptive_inputs = replace(
            initial_inputs,
            replay_or_paper_run_id=identity.run_id,
            generation=identity.generation,
            as_of=adaptive_at,
            account_identity_sha256=identity.account_identity_sha256,
            code_build_sha256=identity.code_build_sha256,
            effective_config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            capture_prefix_root_sha256=prefix_root,
            open_structural_risk_usd=0.0,
            pending_reserved_risk_usd=0.0,
            existing_same_symbol_structural_risk_usd=0.0,
            pending_same_symbol_structural_risk_usd=0.0,
            current_cluster_structural_risk_usd=0.0,
            pending_correlation_cluster_risk_usd=0.0,
            portfolio_gross_notional_usd=0.0,
            pending_portfolio_gross_notional_usd=0.0,
            policy_buying_power_capacity_usd=snapshot.buying_power_usd,
            open_buying_power_impact_usd=0.0,
            pending_buying_power_impact_usd=0.0,
            evidence=adaptive_evidence,
        )
        adaptive_request = _request(
            symbol=SYMBOL,
            decision_id=DECISION_ID,
            client_order_id=DECISION_ID,
            cluster="equity:veee",
            snapshot=snapshot,
            inputs=adaptive_inputs,
        )
        assert adaptive_request.opportunity_key is not None
        detector_source_inventory_sha256 = sha256_json(
            {
                "read_id": detector_receipt.read_id,
                "source_event_sha256s": list(
                    detector_receipt.source_event_sha256s
                ),
            }
        )
        prior_reference = _FirstDipPriorDetectorReference(
            run_id=identity.run_id,
            authority_source="captured_db_paper",
            generation=identity.generation,
            symbol=SYMBOL,
            decision_id=DECISION_ID,
            decision_at=decision_at,
            input_prefix_root_sha256=prefix_root,
            decision_checkpoint_sha256=None,
            active_input_attestation_sha256=sha256_json(
                {"detector_prefix": prefix_root}
            ),
            read_receipt_sha256=sha256_json(detector_receipt.to_dict()),
            receipt_event_sha256=detector_receipt_event.event_sha256,
            source_event_inventory_sha256=detector_source_inventory_sha256,
            policy_sha256=tape_policy.policy_sha256,
            evaluation_sha256=detector_evaluation.evaluation_sha256,
            receipt_binding_sha256=sha256_json(
                {"captured_detector": detector_source_inventory_sha256}
            ),
            opportunity_key_sha256=(
                adaptive_request.opportunity_key.key_sha256
            ),
        )

        final_at = decision_at + timedelta(milliseconds=150)
        final_prints = tuple(
            add(
                CaptureStream.IQFEED_PRINT,
                available_at=final_at,
                provider_event_at=final_at - timedelta(milliseconds=offset_ms),
                symbol=SYMBOL,
                provider="iqfeed",
                payload={
                    "schema_version": rv3.SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION,
                    "symbol": SYMBOL,
                    "price": price,
                    "size": size,
                    "bid": price - 0.01,
                    "ask": price,
                    "conditions": ["sealed-final-fixture"],
                },
            )
            for offset_ms, price, size in (
                (30, 10.03, 200.0),
                (20, 10.10, 500.0),
                (10, 10.12, 1_000.0),
            )
        )
        final_query = FirstDipTapeReadQuery(
            symbol=SYMBOL,
            provider="iqfeed",
            event_start_exclusive=final_at
            - timedelta(seconds=tape_policy.window_seconds),
            event_end_inclusive=final_at,
            decision_at=final_at,
            available_at_most=final_at,
            source_frontier_sequence=(
                first_dip_prints[-1].sequence
                if first_dip_final_receipt_corruption == "lowered_frontier"
                else final_prints[-1].sequence
            ),
            policy_sha256=tape_policy.policy_sha256,
        )
        final_window_prints = tuple(
            event
            for event in events
            if event.stream is CaptureStream.IQFEED_PRINT
            and event.provider == "iqfeed"
            and event.symbol == SYMBOL
            and event.sequence <= final_query.source_frontier_sequence
            and event.clocks.available_at <= final_query.available_at_most
            and event.clocks.provider_event_at is not None
            and final_query.event_start_exclusive
            < event.clocks.provider_event_at
            <= final_query.event_end_inclusive
        )
        if first_dip_final_receipt_corruption in {None, "lowered_frontier"}:
            final_receipt_prints = final_window_prints
        elif first_dip_final_receipt_corruption == "omit_in_window_print":
            final_receipt_prints = final_window_prints[:-1]
        else:
            raise AssertionError("unknown final first-dip receipt corruption")
        final_source_refs = tuple(
            CaptureEventRef.from_event(event) for event in final_receipt_prints
        )
        final_receipt = CaptureReadReceipt(
            read_id=str(uuid.uuid4()),
            decision_id=DECISION_ID,
            identity_sha256=identity.identity_sha256,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol=SYMBOL,
            requested_at=final_at - timedelta(milliseconds=1),
            returned_at=final_at,
            query_sha256=sha256_json(final_query.to_dict()),
            source_event_sha256s=tuple(
                event.event_sha256 for event in final_receipt_prints
            ),
            empty_result=False,
            result_sha256=captured_read_result_sha256(final_source_refs),
            query=final_query.to_dict(),
        )
        receipts.append(final_receipt)
        final_receipt_event = add(
            CaptureStream.READ_RECEIPT,
            available_at=final_at,
            symbol=SYMBOL,
            provider="iqfeed",
            payload=final_receipt.to_dict(),
        )
        final_watermark_at = final_at + timedelta(milliseconds=1)
        final_watermark = ProviderWatermark(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            identity_sha256=identity.identity_sha256,
            event_watermark_at=final_at,
            emitted_available_at=final_watermark_at,
            bounded_lateness_seconds=1.0,
            max_observed_lateness_seconds=0.001,
            generation=identity.generation,
            symbol=SYMBOL,
        )
        final_watermark_event = add(
            CaptureStream.PROVIDER_WATERMARK,
            available_at=final_watermark_at,
            market_reference_at=final_at,
            symbol=None,
            provider="chili_capture",
            payload=final_watermark.to_dict(),
        )
        final_source_rows = tuple(
            event
            for event in events
            if event.stream is CaptureStream.IQFEED_PRINT
            and event.provider == "iqfeed"
            and event.symbol == SYMBOL
        )
        final_coverage = StreamCoverage(
            stream=CaptureStream.IQFEED_PRINT,
            identity_sha256=identity.identity_sha256,
            provider="iqfeed",
            symbol=SYMBOL,
            first_available_at=min(
                event.clocks.available_at for event in final_source_rows
            ),
            last_available_at=max(
                event.clocks.available_at for event in final_source_rows
            ),
            event_count=len(final_source_rows),
            exact_event_clock_complete=True,
            content_verified=True,
            continuity_complete=True,
            watermark=final_watermark,
        )
        final_coverage_at = final_at + timedelta(milliseconds=2)
        final_coverage_event = add(
            CaptureStream.CAPTURE_HEALTH,
            available_at=final_coverage_at,
            symbol=None,
            provider="chili_capture",
            payload={
                "live_continuity_checkpoint": True,
                "coverage": final_coverage.to_dict(),
            },
        )
        final_manifest_coverage_at = final_coverage_at + timedelta(microseconds=1)
        final_manifest_coverage_event = add(
            CaptureStream.CAPTURE_HEALTH,
            available_at=final_manifest_coverage_at,
            symbol=None,
            provider="chili_capture",
            payload=final_coverage.to_dict(),
        )
        final_profile = FSMDependencyProfile(
            required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
            required_read_ids=(final_receipt.read_id,),
            stream_dependencies=(
                FSMStreamDependency(
                    stream=CaptureStream.IQFEED_PRINT,
                    exact_provider_event_at_required=True,
                    market_reference_at_required=False,
                    max_source_age_seconds=tape_policy.max_source_age_seconds,
                    coverage_start_at=final_query.event_start_exclusive,
                ),
            ),
        )
        final_read_evidence = ActiveCaptureReadEvidence(
            receipt=final_receipt,
            receipt_sha256=sha256_json(final_receipt.to_dict()),
            receipt_event_sha256=final_receipt_event.event_sha256,
            receipt_event_sequence=final_receipt_event.sequence,
            receipt_committed_available_at=(
                final_receipt_event.clocks.available_at
            ),
            producer_id=final_producer.producer_id,
            producer_generation=final_producer.generation,
            source_event_refs=final_source_refs,
        )
        final_continuity_evidence = ActiveCaptureContinuityEvidence(
            coverage=final_coverage,
            producer_id=final_producer.producer_id,
            producer_generation=final_producer.generation,
            source_frontier_sequence=max(
                event.sequence for event in final_source_rows
            ),
            watermark_event_sha256=final_watermark_event.event_sha256,
            watermark_event_sequence=final_watermark_event.sequence,
            watermark_committed_available_at=(
                final_watermark_event.clocks.available_at
            ),
            coverage_event_sha256=final_coverage_event.event_sha256,
            coverage_event_sequence=final_coverage_event.sequence,
            coverage_committed_available_at=(
                final_coverage_event.clocks.available_at
            ),
        )
        final_prefix_sequence = final_manifest_coverage_event.sequence
        final_prefix_root = capture_prefix_root_sha256(
            tuple(CaptureEventRef.from_event(event) for event in events),
            identity_sha256=identity.identity_sha256,
            through_sequence=final_prefix_sequence,
        )
        final_evaluation = evaluate_first_dip_tape(
            first_dip_tape_window_from_capture(
                final_receipt,
                final_receipt_prints,
            ),
            policy=tape_policy,
            decision_at=final_at,
            symbol=SYMBOL,
        )
        if first_dip_final_receipt_corruption is None:
            assert (
                final_evaluation.status == "valid_positive"
            ), final_evaluation.to_dict()
        final_frontier = FirstDipFinalCaptureFrontier(
            run_id=identity.run_id,
            generation=identity.generation,
            identity_sha256=identity.identity_sha256,
            decision_id=final_receipt.decision_id,
            input_prefix_sequence=final_prefix_sequence,
            input_prefix_root_sha256=final_prefix_root,
            attested_available_at=final_manifest_coverage_at,
            final_boundary_available_at=final_manifest_coverage_at,
            expires_at=final_at
            + timedelta(seconds=tape_policy.max_source_age_seconds),
            dependency_profile_sha256=final_profile.profile_sha256,
            dependency_profile_canonical_json=canonical_json_bytes(
                final_profile.to_dict()
            ).decode("utf-8"),
            required_read_ids=final_profile.required_read_ids,
            read_evidence_inventory_sha256=sha256_json(
                {"read_evidence": [final_read_evidence.to_evidence_dict()]}
            ),
            continuity_evidence_inventory_sha256=sha256_json(
                {
                    "continuity_evidence": [
                        final_continuity_evidence.to_evidence_dict()
                    ]
                }
            ),
            first_dip_tape_read_id=final_receipt.read_id,
            policy_sha256=tape_policy.policy_sha256,
            policy_canonical_json=canonical_json_bytes(
                tape_policy.to_dict()
            ).decode("utf-8"),
            evaluation_sha256=final_evaluation.evaluation_sha256,
            evaluation_canonical_json=canonical_json_bytes(
                final_evaluation.to_dict()
            ).decode("utf-8"),
            decision_receipt_binding_sha256=sha256_json(
                {"captured_final": final_receipt.read_id}
            ),
            prior_detector_reference_sha256=sha256_json(
                prior_reference.to_dict()
            ),
            prior_detector_reference_canonical_json=canonical_json_bytes(
                prior_reference.to_dict()
            ).decode("utf-8"),
            adaptive_request_sha256=adaptive_request.request_sha256,
            adaptive_request_canonical_json=canonical_json_bytes(
                adaptive_request.to_payload()
            ).decode("utf-8"),
            opportunity_key_sha256=(
                adaptive_request.opportunity_key.key_sha256
            ),
        )
        decision_payload["first_dip_final_capture_frontier"] = (
            final_frontier.to_dict()
        )
        decision_payload["first_dip_final_capture_frontier_sha256"] = (
            final_frontier.frontier_sha256
        )
        predecision_tape_coverage = final_coverage
    # A valid capture can append a fact after the attested input prefix but
    # before the decision event is durably published.  Its availability clock
    # is before decision_at, yet its higher sequence keeps it outside the exact
    # prefix consumed by this invocation.
    future_quote = None
    if postprefix_earlier_available_nbbo:
        future_quote = add(
            CaptureStream.NBBO_QUOTE,
            available_at=decision_at - timedelta(seconds=1),
            provider_event_at=BASE - timedelta(seconds=30),
            symbol=SYMBOL,
            payload={
                "schema_version": rv3.SEALED_REPLAY_NBBO_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "bid": 10.99,
                "ask": 11.01,
                "last": 11.00,
            },
        )
    decision_event_available_at = decision_at + timedelta(
        seconds=decision_publication_delay_seconds
    )
    if final_frontier is not None:
        decision_event_available_at = max(
            decision_event_available_at,
            final_frontier.final_boundary_available_at
            + timedelta(microseconds=1),
        )
    decision_event = add(
        CaptureStream.FSM_DECISION,
        available_at=decision_event_available_at,
        market_reference_at=decision_at,
        symbol=SYMBOL,
        payload=decision_payload,
    )
    checkpoint = CaptureDecisionCheckpoint(
        identity_sha256=identity.identity_sha256,
        decision_id=DECISION_ID,
        symbol=SYMBOL,
        decision_at=decision_at,
        available_at=decision_event.clocks.available_at,
        decision_event_sha256=decision_event.event_sha256,
        input_prefix_sequence=prefix_sequence,
        input_prefix_root_sha256=prefix_root,
        required_read_ids=tuple(read_ids.values()),
        decision_payload=decision_payload,
    )

    # This fact has an older provider clock but a later availability clock. In
    # the same-timestamp case it is sequenced after the decision checkpoint and
    # must remain outside that exact input prefix.
    if future_quote is None:
        future_quote = add(
            CaptureStream.NBBO_QUOTE,
            available_at=(
                decision_at
                if same_timestamp_late_nbbo
                else BASE + timedelta(seconds=20)
            ),
            provider_event_at=BASE - timedelta(seconds=30),
            symbol=SYMBOL,
            payload={
                "schema_version": rv3.SEALED_REPLAY_NBBO_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "bid": 10.99,
                "ask": 11.01,
                "last": 11.00,
            },
        )
    if same_timestamp_late_nbbo:
        add(
            CaptureStream.ADMISSION_ELIGIBILITY,
            available_at=decision_at,
            market_reference_at=decision_at,
            symbol=SYMBOL,
            payload={
                "schema_version": rv3.SEALED_REPLAY_ELIGIBILITY_SCHEMA_VERSION,
                "symbol": SYMBOL,
                "live_eligible": False,
                "freshness_at": decision_at.isoformat(),
            },
        )

    if not no_order:
        broker_authority_sha256 = (
            "b" * 64
            if bad_broker_authority
            else capture_final_decision_authority_sha256(
                decision_event, decision_output
            )
        )
        submitted_lifecycle = CaptureBrokerOrderLifecycle(
            decision_id=DECISION_ID,
            order_intent_sha256=intent.order_intent_sha256,
            client_order_id=client_order_id,
            client_order_id_sha256=intent.client_order_id_sha256,
            transition=CaptureBrokerTransition.SUBMITTED,
            order_quantity=100,
            cumulative_filled_quantity=0,
            last_fill_quantity=0,
            prior_transition_event_sha256=None,
            final_decision_attestation_sha256=broker_authority_sha256,
            broker_order_id="broker-1",
        )
        broker = add(
            CaptureStream.BROKER_ORDER_LIFECYCLE,
            available_at=(
                BASE + timedelta(seconds=11)
                if same_timestamp_late_nbbo
                else BASE + timedelta(seconds=21)
            ),
            provider_event_at=(
                BASE + timedelta(seconds=10, milliseconds=900)
                if same_timestamp_late_nbbo
                else BASE + timedelta(seconds=20, milliseconds=900)
            ),
            symbol=SYMBOL,
            payload=submitted_lifecycle.to_dict(),
        )
        canceled_lifecycle = CaptureBrokerOrderLifecycle(
            decision_id=DECISION_ID,
            order_intent_sha256=intent.order_intent_sha256,
            client_order_id=client_order_id,
            client_order_id_sha256=intent.client_order_id_sha256,
            transition=CaptureBrokerTransition.CANCELED,
            order_quantity=100,
            cumulative_filled_quantity=0,
            last_fill_quantity=0,
            prior_transition_event_sha256=broker.event_sha256,
            final_decision_attestation_sha256=broker_authority_sha256,
            broker_order_id="broker-1",
            raw_provider_event_sha256="c" * 64,
            reject_or_cancel_reason="user_requested",
        )
        add(
            CaptureStream.BROKER_ORDER_LIFECYCLE,
            available_at=(
                BASE + timedelta(seconds=12)
                if same_timestamp_late_nbbo
                else BASE + timedelta(seconds=22)
            ),
            provider_event_at=(
                BASE + timedelta(seconds=11, milliseconds=900)
                if same_timestamp_late_nbbo
                else BASE + timedelta(seconds=21, milliseconds=900)
            ),
            symbol=SYMBOL,
            payload=canceled_lifecycle.to_dict(),
        )

    data_by_stream: dict[CaptureStream, list[CaptureEvent]] = {}
    for event in events:
        if event.stream in replay_streams:
            data_by_stream.setdefault(event.stream, []).append(event)
    coverages: dict[CaptureStream, StreamCoverage] = {}
    for stream, rows in data_by_stream.items():
        if (
            stream is CaptureStream.IQFEED_PRINT
            and predecision_tape_coverage is not None
        ):
            coverages[stream] = predecision_tape_coverage
            continue
        coverages[stream] = StreamCoverage(
            stream=stream,
            identity_sha256=identity.identity_sha256,
            provider=rows[0].provider,
            symbol=(None if stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT else SYMBOL),
            first_available_at=min(row.clocks.available_at for row in rows),
            last_available_at=max(row.clocks.available_at for row in rows),
            event_count=len(rows),
            exact_event_clock_complete=stream
            in {
                CaptureStream.NBBO_QUOTE,
                CaptureStream.IQFEED_PRINT,
                CaptureStream.BROKER_ORDER_LIFECYCLE,
            },
            content_verified=True,
            continuity_complete=True,
            query_receipt_count=(1 if stream in read_ids else 0),
        )
    control_at = exit_at + timedelta(seconds=1)
    for coverage in coverages.values():
        if coverage is predecision_tape_coverage:
            continue
        add(
            CaptureStream.CAPTURE_HEALTH,
            available_at=control_at,
            symbol=None,
            provider="chili_capture",
            payload=coverage.to_dict(),
        )
        control_at += timedelta(microseconds=1)

    store = ContentAddressedCaptureStore(
        tmp_path / f"sealed-adapter-{uuid.uuid4().hex}",
        compression_codec="zlib",
    )
    ingress = BoundedCaptureIngress(
        max_events=len(events),
        max_bytes=5_000_000,
        max_gap_keys=32,
    )
    for event in events:
        assert ingress.submit(event)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=len(events),
        batch_bytes=5_000_000,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)
    capture = VerifiedReplayCapture.load_sealed_run(
        store,
        identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )
    manifest = CaptureCoverageManifest.from_verified_capture(
        capture,
        decision_checkpoints=(checkpoint,),
        stream_coverage=coverages,
        read_receipts=tuple(receipts),
    )
    request = ReplayCoverageRequest(
        warmup_start_at=BASE,
        decision_at=decision_at,
        exit_end_at=exit_at,
        required_streams=replay_streams,
        decision_id=DECISION_ID,
        decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
        required_read_ids=frozenset(read_ids.values()),
        symbol=SYMBOL,
        expected_identity_sha256=identity.identity_sha256,
    )
    tie_sha256s = tuple(
        event.event_sha256
        for event in (initial_quote, ohlcv, eligibility)
        if event.clocks.available_at == tied_at
    )
    return _Fixture(
        capture=capture,
        manifest=manifest,
        request=request,
        initial_quote_sha256=initial_quote.event_sha256,
        future_quote_sha256=future_quote.event_sha256,
        tie_sha256s=tie_sha256s,
        first_dip_print_sha256s=tuple(
            event.event_sha256 for event in first_dip_prints
        ),
    )


def test_sealed_adapter_releases_by_available_clock_and_hides_future_fact(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )

    before_future = adapter.advance_to(BASE + timedelta(seconds=10))
    assert fixture.initial_quote_sha256 in before_future.event_sha256s
    assert fixture.future_quote_sha256 not in before_future.event_sha256s
    assert adapter.current_quote().bid == pytest.approx(9.99)

    future_release = adapter.advance_to(BASE + timedelta(seconds=20))
    assert future_release.event_sha256s == (fixture.future_quote_sha256,)
    assert adapter.current_quote().bid == pytest.approx(10.99)
    assert adapter.proof.adapter_network_attempt_count == 0
    assert adapter.proof.os_level_external_network_denial_proven is False
    assert adapter.proof.broker_lifecycle_replayed is False


def test_sealed_adapter_mints_exact_print_objects_only_after_dual_clock_release(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, first_dip_tape=True, no_order=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    inventory = adapter.counterfactual_exact_print_inventory()
    assert inventory.event_sha256s == fixture.first_dip_print_sha256s
    assert adapter.released_counterfactual_exact_prints() == ()

    checkpoint = next(iter(fixture.manifest.decision_checkpoints))
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    released = adapter.released_counterfactual_exact_prints()
    assert released
    assert all(isinstance(value, VerifiedExactPrint) for value in released)
    assert tuple(value.event_sha256 for value in released) == (
        fixture.first_dip_print_sha256s
    )
    assert all(value.available_at <= checkpoint.decision_at for value in released)
    assert all(value.received_at <= value.available_at for value in released)
    assert all(value.product_id == SYMBOL for value in released)

    allocation_broker = MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=True,
        exact_print_fills=True,
        exact_print_order_latency_seconds=0.0,
    )
    allocation_broker.configure_verified_exact_print_inventory(inventory)
    assert allocation_broker.exact_print_terminal_complete is False


def test_decision_uses_receipt_selected_continuous_facts_not_latest_prefix_state(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        in_prefix_unselected_nbbo=True,
        in_prefix_unselected_eligibility=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = next(iter(fixture.manifest.decision_checkpoints))
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )

    # The continuous state inventory legitimately contains later in-prefix
    # facts, but the live receipt named the earlier quote/eligibility values.
    assert adapter.current_quote().bid == pytest.approx(10.99)
    assert adapter.current_eligibility()[0] is False

    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    assert adapter.current_quote().bid == pytest.approx(9.99)
    assert adapter.current_eligibility()[0] is True
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()

    # Pinning prevents latest-value drift, but certification remains closed
    # until consumption is acknowledged by the real broker/ORM read seams.
    assert adapter.continuous_decision_reads_observed_by_fsm is False


def test_sealed_scanner_snapshot_runs_through_real_risk_seam_once(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, scanner_snapshot=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    def forbidden_live_snapshot(*_args, **_kwargs):
        raise AssertionError("sealed scanner replay reached Massive")

    from app.services import massive_client

    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        forbidden_live_snapshot,
    )
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    assert adapter.runtime_input_capabilities == frozenset(
        {"macro", "scanner_snapshot"}
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)

    class ForbiddenViabilityRead:
        @property
        def execution_readiness_json(self):
            raise AssertionError("sealed scanner replay read mutable viability JSON")

    with lr.replay_clock(checkpoint.decision_at), (
        risk_evaluator.replay_scanner_snapshot_provider(
            adapter.scanner_snapshot_provider
        )
    ):
        allowed, reason, detail = risk_evaluator._ross_lane_universe_check(
            SYMBOL,
            ForbiddenViabilityRead(),
        )

    assert allowed is True
    assert reason == "ross_universe_profile_ok"
    assert detail["snapshot_authority"] == "sealed_replay_receipt"
    assert detail["price"] == pytest.approx(4.20)
    assert detail["dollar_volume"] == pytest.approx(2_520_000.0)
    adapter.complete_decision_read_plan()
    assert adapter.rejected_provider_request_count == 0
    assert adapter.network_attempt_count == 0


def test_sealed_scanner_snapshot_rejects_profile_or_ttl_drift(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, scanner_snapshot=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)

    with pytest.raises(
        rv3.SealedReplayInputError,
        match="scanner provider call differs from its exact receipt",
    ):
        adapter.scanner_snapshot_provider(
            SYMBOL,
            include_otc=False,
            max_age_seconds=301.0,
            profile_id="equity_ross_smallcap",
            asset_class="equity",
            price_min=1.0,
            price_max=20.0,
            min_dollar_volume=1_000_000.0,
            min_change_pct=5.0,
        )

    adapter.abort_decision_read_plan()
    assert adapter.rejected_provider_request_count == 1
    assert adapter.network_attempt_count == 0


def test_first_dip_consumes_ordered_multi_print_receipt_without_fallback(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("sealed first-dip replay attempted network fallback")

    monkeypatch.setattr(socket, "socket", _network_forbidden)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    policy = FirstDipTapePolicy(
        window_seconds=15.0,
        max_source_age_seconds=10.0,
        tick_rate_floor_pctile=0.0,
    )
    evaluation = adapter.evaluate_first_dip_tape(
        policy=policy,
        symbol=SYMBOL,
    )

    assert evaluation.source_event_sha256s == fixture.first_dip_print_sha256s
    assert evaluation.status == "valid_positive"
    assert evaluation.confirmed is True
    assert evaluation.features is not None
    assert evaluation.features["n_ticks"] == 3
    assert evaluation.policy_sha256 == policy.policy_sha256
    assert adapter.network_attempt_count == 0

    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()


def test_first_dip_private_decision_receipt_binds_exact_sealed_run_and_coverage(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        first_dip_predecision_control_delay_seconds=0.001,
        decision_publication_delay_seconds=0.002,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("sealed first-dip decision attempted network fallback")

    monkeypatch.setattr(socket, "socket", _network_forbidden)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    policy = FirstDipTapePolicy(
        window_seconds=15.0,
        max_source_age_seconds=10.0,
        tick_rate_floor_pctile=0.0,
    )
    authority = adapter.prepare_first_dip_tape_decision_authority()
    assert adapter.prepare_first_dip_tape_decision_authority() is authority
    with _installed_sealed_replay_first_dip_tape_decision_authority(authority):
        resolution = resolve_first_dip_tape_decision(
            symbol=SYMBOL,
            decision_at=checkpoint.decision_at,
            policy=policy,
        )
        reused = resolve_first_dip_tape_decision(
            symbol=SYMBOL,
            decision_at=checkpoint.decision_at,
            policy=policy,
        )

    assert resolution.status == "valid_positive"
    assert resolution.run_bound is True
    assert resolution.receipt is not None
    receipt = resolution.receipt
    assert receipt.run_id == fixture.manifest.identity.run_id
    assert receipt.generation == fixture.manifest.identity.generation
    assert receipt.identity_sha256 == fixture.manifest.identity.identity_sha256
    assert receipt.decision_id == checkpoint.decision_id
    assert receipt.decision_checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert receipt.input_prefix_sequence == checkpoint.input_prefix_sequence
    assert receipt.input_prefix_root_sha256 == checkpoint.input_prefix_root_sha256
    assert receipt.final_capture_seal_sha256 == fixture.capture.final_seal_sha256
    assert receipt.coverage_manifest_sha256 == fixture.manifest.manifest_sha256
    assert receipt.evaluation.source_event_sha256s == (
        fixture.first_dip_print_sha256s
    )
    assert receipt.reservation_authority is False
    assert receipt.order_authority is False
    assert reused.status == "coverage_unavailable"
    assert reused.reason == "first_dip_tape_decision_provider_already_consumed"
    assert reused.run_bound is False
    assert adapter.network_attempt_count == 0

    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()


def test_sealed_first_dip_final_frontier_replays_exact_adaptive_and_tape_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        first_dip_final_frontier=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("sealed final first-dip replay attempted network fallback")

    monkeypatch.setattr(socket, "socket", _network_forbidden)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture,
        fixture.manifest,
        fixture.request,
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    final_frontier = decision_tick.first_dip_final_frontier
    assert final_frontier is not None
    assert final_frontier.evaluation.status == "valid_positive"
    assert len(final_frontier.tape_read.receipt.source_event_sha256s) == 6

    adapter.begin_decision_read_plan(decision_tick)
    policy = FirstDipTapePolicy.from_dict(
        checkpoint.decision_payload["first_dip_tape_policy"]
    )
    detector_authority = adapter.prepare_first_dip_tape_decision_authority()
    adaptive_material = adapter.prepare_first_dip_adaptive_risk_material()
    expected_request = final_frontier.adaptive_request
    assert expected_request.opportunity_key is not None

    with _installed_sealed_replay_first_dip_tape_decision_authority(
        detector_authority
    ):
        detector_resolution = resolve_first_dip_tape_decision(
            symbol=SYMBOL,
            decision_at=checkpoint.decision_at,
            policy=policy,
        )
        built = build_adaptive_risk_request(
            adaptive_material.source,
            client_order_id=expected_request.client_order_id,
            entry_limit_price=expected_request.entry_limit_price,
            opportunity_key=expected_request.opportunity_key.to_payload(),
            sealed_replay_attestation=(
                adaptive_material.sealed_replay_attestation
            ),
        )
        with pytest.raises(
            rv3.SealedReplayInputError,
            match="final authority request escaped its frontier",
        ):
            adapter.prepare_first_dip_final_tape_decision_handoff(
                adaptive_request=object(),
                detector_policy=policy,
                final_boundary_available_at=checkpoint.decision_at,
            )
        with pytest.raises(
            rv3.SealedReplayInputError,
            match="final authority request escaped its frontier",
        ):
            adapter.prepare_first_dip_final_tape_decision_handoff(
                adaptive_request=built.request,
                detector_policy=policy,
                final_boundary_available_at=(
                    checkpoint.decision_at - timedelta(microseconds=1)
                ),
            )
        with _installed_sealed_replay_first_dip_final_authority_provider(
            adapter.prepare_first_dip_final_tape_decision_handoff
        ):
            final_resolution = (
                _resolve_first_dip_final_admission_with_active_provider(
                    adaptive_request=built.request,
                    detector_policy=policy,
                    symbol=SYMBOL,
                    adaptive_decision_at=built.request.inputs.as_of,
                    run_id=built.request.inputs.replay_or_paper_run_id,
                    generation=built.request.inputs.generation,
                    adaptive_decision_id=built.request.inputs.decision_id,
                    adaptive_input_prefix_root_sha256=(
                        built.request.inputs.capture_prefix_root_sha256
                    ),
                    adaptive_request_sha256=built.request.request_sha256,
                    opportunity_key_sha256=(
                        built.request.opportunity_key.key_sha256
                    ),
                    final_boundary_available_at=checkpoint.decision_at,
                    expected_execution_surface="sealed_replay",
                    detector_policy_sha256=policy.policy_sha256,
                    detector_authority_source="sealed_replay",
                    detector_receipt_binding_sha256=(
                        detector_resolution.receipt.binding_sha256
                    ),
                    detector_opportunity_key_sha256=(
                        built.request.opportunity_key.key_sha256
                    ),
                )
            )

    assert detector_resolution.status == "valid_positive"
    assert detector_resolution.receipt is not None
    assert built.request.to_payload() == expected_request.to_payload()
    assert final_resolution.admitted is True
    assert final_resolution.reason == (
        "first_dip_final_admission_typed_receipt_verified"
    )
    assert final_resolution.reservation_authority is False
    assert final_resolution.order_authority is False
    assert adapter.network_attempt_count == 0
    with pytest.raises(
        AdaptiveRiskBuilderError,
        match="sealed_replay_adaptive_risk_attestation_already_consumed",
    ):
        build_adaptive_risk_request(
            adaptive_material.source,
            client_order_id=expected_request.client_order_id,
            entry_limit_price=expected_request.entry_limit_price,
            opportunity_key=expected_request.opportunity_key.to_payload(),
            sealed_replay_attestation=(
                adaptive_material.sealed_replay_attestation
            ),
        )

    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()


@pytest.mark.parametrize(
    ("corruption", "message"),
    (
        ("omit_in_window_print", "final tape evaluation differs from captured facts"),
        ("lowered_frontier", "final tape evaluation cannot be reproduced"),
    ),
)
def test_sealed_first_dip_final_receipt_cannot_hide_an_in_window_print(
    tmp_path,
    monkeypatch,
    corruption,
    message,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        first_dip_final_frontier=True,
        first_dip_final_receipt_corruption=corruption,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    with pytest.raises(
        rv3.SealedReplayInputError,
        match=message,
    ):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture,
            fixture.manifest,
            fixture.request,
        )


def test_sealed_first_dip_final_frontier_cannot_attach_to_another_setup(
    tmp_path,
) -> None:
    with pytest.raises(
        CaptureContractError,
        match="typed first-dip evidence is bound to another setup",
    ):
        _sealed_fixture(
            tmp_path,
            first_dip_tape=True,
            no_order=True,
            first_dip_final_frontier=True,
            first_dip_setup_role="breakout_reclaim",
        )


def test_first_dip_decision_receipt_fails_closed_without_watermark_proof(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    policy = FirstDipTapePolicy(
        window_seconds=15.0,
        max_source_age_seconds=10.0,
        tick_rate_floor_pctile=0.0,
    )
    with pytest.raises(
        rv3.SealedReplayInputError,
        match="coverage proof is unavailable",
    ):
        adapter.prepare_first_dip_tape_decision_authority()
    assert adapter.network_attempt_count == 0
    adapter.abort_decision_read_plan()


def test_sealed_boundary_scopes_one_shot_first_dip_authority_around_step_only(
    tmp_path,
    monkeypatch,
) -> None:
    """The real sealed adapter, not a callable stub, supplies the authority."""

    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        first_dip_predecision_control_delay_seconds=0.001,
        decision_publication_delay_seconds=0.002,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture,
        fixture.manifest,
        fixture.request,
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    policy = FirstDipTapePolicy.from_dict(
        checkpoint.decision_payload["first_dip_tape_policy"]
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    driver = object.__new__(rv3.ReplayV3Driver)
    driver.sealed_inputs = adapter
    driver.seed = SimpleNamespace(symbol=SYMBOL)
    driver.mock = SimpleNamespace(recorded_bound_client_ids=())
    driver._released_recorded_lifecycle_count = 0
    driver._broker_lifecycle_architectural_blockers = []
    inside = []

    def _step(t, quote):
        assert t == checkpoint.decision_at
        inside.append(
            resolve_first_dip_tape_decision(
                symbol=SYMBOL,
                decision_at=checkpoint.decision_at,
                policy=policy,
            )
        )
        inside.append(
            resolve_first_dip_tape_decision(
                symbol=SYMBOL,
                decision_at=checkpoint.decision_at,
                policy=policy,
            )
        )
        adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
        adapter.account_equity_provider(prefer_equity=True)
        return rv3.TickTrace(
            ts=checkpoint.decision_at,
            state_before=decision_tick.output.fsm_state,
            state_after=decision_tick.output.fsm_state,
            result={},
        )

    driver.step = _step
    trace = driver._advance_sealed_boundary(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    outside = resolve_first_dip_tape_decision(
        symbol=SYMBOL,
        decision_at=checkpoint.decision_at,
        policy=policy,
    )

    assert trace is not None
    assert inside[0].status == "valid_positive"
    assert inside[0].run_bound is True
    assert inside[1].status == "coverage_unavailable"
    assert inside[1].reason == "first_dip_tape_decision_provider_already_consumed"
    assert outside.reason == "first_dip_tape_decision_provider_missing"
    assert adapter.network_attempt_count == 0


def test_first_dip_multi_print_receipt_must_be_consumed_exactly_once(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    adapter.consume_first_dip_tape_read(SYMBOL)
    with pytest.raises(
        rv3.SealedReplayInputError,
        match="consumed more than once",
    ):
        adapter.consume_first_dip_tape_read(SYMBOL)
    adapter.abort_decision_read_plan()


def test_first_dip_sealed_read_uses_sequence_frontier_for_same_clock_callback(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        post_receipt_same_clock_first_dip=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    receipt = next(
        row
        for row in fixture.manifest.read_receipts
        if row.stream is CaptureStream.IQFEED_PRINT
    )
    assert receipt.query is not None
    query = FirstDipTapeReadQuery.from_dict(receipt.query)
    receipt_commit = next(
        ref
        for ref in fixture.manifest.event_index.values()
        if ref.stream is CaptureStream.READ_RECEIPT
        and ref.payload_sha256 == sha256_json(receipt.to_dict())
    )
    same_clock_prints = tuple(
        ref
        for ref in fixture.manifest.event_index.values()
        if ref.stream is CaptureStream.IQFEED_PRINT
        and ref.available_at == query.available_at_most
    )
    assert any(
        ref.sequence > receipt_commit.sequence > query.source_frontier_sequence
        for ref in same_clock_prints
    )

    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    policy = FirstDipTapePolicy(
        window_seconds=15.0,
        max_source_age_seconds=10.0,
        tick_rate_floor_pctile=0.0,
    )
    evaluation = adapter.evaluate_first_dip_tape(
        policy=policy,
        symbol=SYMBOL,
    )
    assert evaluation.source_event_sha256s == fixture.first_dip_print_sha256s
    assert evaluation.features is not None
    assert evaluation.features["n_ticks"] == 3
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()


def test_first_dip_sealed_read_ignores_later_unselected_post_receipt_callback(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        post_receipt_same_clock_first_dip=True,
        post_receipt_first_dip_available_delay_seconds=0.0005,
        decision_publication_delay_seconds=0.001,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    checkpoint = fixture.manifest.decision_checkpoints[0]
    later_unselected = tuple(
        ref
        for ref in fixture.manifest.event_index.values()
        if ref.stream is CaptureStream.IQFEED_PRINT
        and checkpoint.decision_at < ref.available_at < checkpoint.available_at
    )
    assert len(later_unselected) == 1
    assert later_unselected[0].sequence <= checkpoint.input_prefix_sequence

    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    assert decision_tick.input_prefix_available_at <= checkpoint.decision_at

    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    adapter.begin_decision_read_plan(decision_tick)
    evaluation = adapter.evaluate_first_dip_tape(
        policy=FirstDipTapePolicy(
            window_seconds=15.0,
            max_source_age_seconds=10.0,
            tick_rate_floor_pctile=0.0,
        ),
        symbol=SYMBOL,
    )
    assert evaluation.source_event_sha256s == fixture.first_dip_print_sha256s
    assert later_unselected[0].event_sha256 not in evaluation.source_event_sha256s
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()


def test_sealed_complete_empty_first_dip_window_replays_as_valid_negative(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        empty_first_dip_window=True,
        no_order=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("sealed empty first-dip replay attempted network fallback")

    monkeypatch.setattr(socket, "socket", _network_forbidden)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture,
        fixture.manifest,
        fixture.request,
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    evaluation = adapter.evaluate_first_dip_tape(
        policy=FirstDipTapePolicy(
            window_seconds=15.0,
            max_source_age_seconds=10.0,
            tick_rate_floor_pctile=0.0,
        ),
        symbol=SYMBOL,
    )
    assert evaluation.status == "valid_negative"
    assert evaluation.reason == "first_dip_tape_no_prints"
    assert evaluation.source_event_sha256s == ()
    assert evaluation.features is None
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()
    assert adapter.proof.adapter_network_attempt_count == 0


def test_real_coverage_grader_accepts_typed_empty_first_dip_absence_proof(
    tmp_path,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        empty_first_dip_window=True,
        no_order=True,
    )

    grade = rv3.grade_replay_coverage(fixture.request, fixture.manifest)

    # This synthetic fixture intentionally lacks the independent resource and
    # producer-lifecycle attestations, so the whole session is not complete.
    # The real (unmocked) coverage grader must nevertheless recognize that the
    # receipt-bound, frontier-complete empty tape window is an authoritative
    # absence rather than fabricate a missing exact event-clock defect.
    assert "capture_resource_binding_unverified" in grade.reasons
    assert not any(
        reason.startswith("first_dip_tape_receipt_")
        for reason in grade.reasons
    )
    tape_receipt = next(
        receipt
        for receipt in fixture.manifest.read_receipts
        if receipt.stream is CaptureStream.IQFEED_PRINT
    )
    assert (
        f"read_receipt_exact_event_clock_missing:{tape_receipt.read_id}"
        not in grade.reasons
    )


def test_delayed_receipt_proof_is_bounded_by_checkpoint_not_decision_clock(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        empty_first_dip_window=True,
        no_order=True,
        first_dip_receipt_commit_delay_seconds=0.001,
        first_dip_predecision_control_delay_seconds=0.0015,
        decision_publication_delay_seconds=0.002,
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    tape_receipt = next(
        receipt
        for receipt in fixture.manifest.read_receipts
        if receipt.stream is CaptureStream.IQFEED_PRINT
    )
    receipt_commit = next(
        ref
        for ref in fixture.manifest.event_index.values()
        if ref.stream is CaptureStream.READ_RECEIPT
        and ref.payload_sha256 == sha256_json(tape_receipt.to_dict())
    )
    assert receipt_commit.available_at > checkpoint.decision_at
    assert receipt_commit.available_at <= checkpoint.available_at
    delayed_proof_refs = tuple(
        ref
        for ref in fixture.manifest.event_index.values()
        if ref.stream
        in {CaptureStream.READ_RECEIPT, CaptureStream.PROVIDER_WATERMARK,
            CaptureStream.CAPTURE_HEALTH}
        and checkpoint.decision_at < ref.available_at <= checkpoint.available_at
    )
    assert {
        CaptureStream.READ_RECEIPT,
        CaptureStream.PROVIDER_WATERMARK,
        CaptureStream.CAPTURE_HEALTH,
    }.issubset({ref.stream for ref in delayed_proof_refs})

    real_grade = rv3.grade_replay_coverage(
        fixture.request,
        fixture.manifest,
    )
    assert not any(
        reason.startswith("first_dip_tape_receipt_")
        for reason in real_grade.reasons
    )

    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture,
        fixture.manifest,
        fixture.request,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    assert decision_tick.input_prefix_available_at <= checkpoint.decision_at


@pytest.mark.parametrize(
    "corruption",
    (
        "omitted",
        "extra",
        "reordered",
        "lowered_frontier",
        "unavailable_frontier",
    ),
)
def test_real_coverage_grader_rebuilds_first_dip_inventory_fail_closed(
    tmp_path,
    corruption: str,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        first_dip_receipt_corruption=corruption,
    )

    grade = rv3.grade_replay_coverage(fixture.request, fixture.manifest)

    assert any(
        reason.startswith("first_dip_tape_receipt_")
        for reason in grade.reasons
    )


def test_real_coverage_grader_rejects_first_dip_future_provider_clock(
    tmp_path,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        empty_first_dip_window=True,
        no_order=True,
        future_clock_first_dip=True,
    )

    grade = rv3.grade_replay_coverage(fixture.request, fixture.manifest)

    tape_receipt = next(
        receipt
        for receipt in fixture.manifest.read_receipts
        if receipt.stream is CaptureStream.IQFEED_PRINT
    )
    assert (
        f"first_dip_tape_receipt_source_clock_from_future:{tape_receipt.read_id}"
        in grade.reasons
    )


@pytest.mark.parametrize(
    "corruption",
    (
        "newest_source_age",
        "schema",
        "extra_field",
        "non_iterable_sources",
    ),
)
def test_real_coverage_grader_rejects_rehashed_noncanonical_empty_evaluation(
    tmp_path,
    corruption: str,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        empty_first_dip_window=True,
        no_order=True,
        empty_first_dip_evaluation_corruption=corruption,
    )

    grade = rv3.grade_replay_coverage(fixture.request, fixture.manifest)

    tape_receipt = next(
        receipt
        for receipt in fixture.manifest.read_receipts
        if receipt.stream is CaptureStream.IQFEED_PRINT
    )
    assert (
        f"first_dip_tape_receipt_empty_evaluation_mismatch:{tape_receipt.read_id}"
        in grade.reasons
    )


def test_checkpoint_rejects_first_dip_evidence_bound_to_another_setup(
    tmp_path,
) -> None:
    with pytest.raises(
        CaptureContractError,
        match="typed first-dip evidence is bound to another setup",
    ):
        _sealed_fixture(
            tmp_path,
            first_dip_tape=True,
            empty_first_dip_window=True,
            no_order=True,
            first_dip_setup_role="adaptive_entry",
        )


def test_checkpoint_rejects_empty_first_dip_negative_with_order_intent(
    tmp_path,
) -> None:
    with pytest.raises(
        CaptureContractError,
        match="first-dip order lacks a positive tape verdict",
    ):
        _sealed_fixture(
            tmp_path,
            first_dip_tape=True,
            empty_first_dip_window=True,
            no_order=False,
            first_dip_tape_purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
        )


def test_checkpoint_rejects_first_dip_order_with_no_typed_receipt(
    tmp_path,
) -> None:
    with pytest.raises(
        CaptureContractError,
        match="first-dip order lacks typed tape evidence",
    ):
        _sealed_fixture(
            tmp_path,
            first_dip_tape=False,
            no_order=False,
            first_dip_setup_role="first_dip_reclaim",
        )


def test_checkpoint_keeps_positive_first_dip_v1_mechanics_non_authorizing(
    tmp_path,
) -> None:
    with pytest.raises(
        CaptureContractError,
        match="IQFeed v1 evidence is mechanics-only",
    ):
        _sealed_fixture(
            tmp_path,
            first_dip_tape=True,
            no_order=False,
            first_dip_tape_purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
        )


@pytest.mark.parametrize(
    ("corruption", "error"),
    (
        ("missing_query", "lacks its typed query"),
        ("omitted", "receipt inventory mismatch"),
        ("extra", "receipt inventory mismatch"),
        ("reordered", "receipt inventory mismatch"),
        ("lowered_frontier", "source frontier is unavailable"),
        ("unavailable_frontier", "source frontier is unavailable"),
    ),
)
def test_first_dip_receipt_inventory_is_rebuilt_from_the_sealed_manifest(
    tmp_path,
    monkeypatch,
    corruption: str,
    error: str,
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        first_dip_tape=True,
        no_order=True,
        first_dip_receipt_corruption=corruption,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("sealed first-dip validation attempted network fallback")

    monkeypatch.setattr(socket, "socket", _network_forbidden)
    with pytest.raises(rv3.SealedReplayInputError, match=error):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture,
            fixture.manifest,
            fixture.request,
        )


def test_sealed_adapter_tie_order_and_release_root_are_deterministic(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, tie_inputs=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    first = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    second = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )

    first_release = first.advance_to(BASE + timedelta(seconds=2))
    second_release = second.advance_to(BASE + timedelta(seconds=2))
    assert first_release.event_sha256s == fixture.tie_sha256s
    assert second_release.event_sha256s == fixture.tie_sha256s
    assert (
        first.proof.release_order_root_sha256
        == second.proof.release_order_root_sha256
    )
    assert first.proof.final_capture_seal_sha256 == fixture.capture.final_seal_sha256
    assert first.proof.manifest_sha256 == fixture.manifest.manifest_sha256
    assert (
        first.proof.decision_checkpoint_sha256
        == fixture.request.decision_checkpoint_sha256
    )


def test_checkpoint_sequence_frontier_hides_same_clock_postdecision_fact(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, same_timestamp_late_nbbo=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )

    decision_release = adapter.advance_to_frontier(
        fixture.request.decision_at,
        sequence_at_most=adapter.proof.input_prefix_sequence,
    )

    assert fixture.future_quote_sha256 not in decision_release.event_sha256s
    assert adapter.current_quote().bid == pytest.approx(9.99)
    assert adapter.current_eligibility()[0] is True

    postdecision = adapter.advance_to_frontier(
        fixture.request.decision_at,
        sequence_at_most=adapter.proof.input_prefix_sequence + 10,
    )
    assert postdecision.event_sha256s[0] == fixture.future_quote_sha256
    assert postdecision.streams == (
        CaptureStream.NBBO_QUOTE,
        CaptureStream.ADMISSION_ELIGIBILITY,
    )
    assert adapter.current_quote().bid == pytest.approx(10.99)
    assert adapter.current_eligibility()[0] is False


def test_only_manifest_bound_fsm_checkpoint_schedules_a_replay_tick(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )

    assert adapter.decision_tick_count == 1
    assert (
        adapter.decision_tick_for_frontier(
            BASE + timedelta(seconds=2),
            1,
        )
        is None
    )
    scheduled = adapter.decision_tick_for_frontier(
        fixture.request.decision_at,
        adapter.proof.input_prefix_sequence,
    )
    assert scheduled is not None
    assert scheduled.checkpoint.checkpoint_sha256 == (
        fixture.request.decision_checkpoint_sha256
    )
    assert scheduled.output.decision_id == DECISION_ID
    assert scheduled.output.decision_output_sha256 == (
        scheduled.checkpoint.decision_payload["decision_output_sha256"]
    )


def test_decision_uses_decision_clock_not_prefix_or_publication_clock(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        decision_publication_delay_seconds=2.0,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )

    scheduled = adapter.decision_tick_for_frontier(
        fixture.request.decision_at,
        adapter.proof.input_prefix_sequence,
    )

    assert scheduled is not None
    # The causal input frontier is the latest source fact (account snapshot at
    # +4s), not the later durable READ_RECEIPT proof publication at +8s.
    assert scheduled.input_prefix_available_at == BASE + timedelta(seconds=4)
    assert scheduled.checkpoint.decision_at == BASE + timedelta(seconds=10)
    assert scheduled.checkpoint.available_at == BASE + timedelta(seconds=12)
    assert scheduled.frontier == (
        BASE + timedelta(seconds=10),
        adapter.proof.input_prefix_sequence,
    )
    assert adapter.decision_tick_for_frontier(
        scheduled.input_prefix_available_at,
        adapter.proof.input_prefix_sequence,
    ) is None
    assert adapter.decision_tick_for_frontier(
        scheduled.checkpoint.available_at,
        adapter.proof.input_prefix_sequence,
    ) is None
    assert adapter.replay_frontiers == (
        scheduled.frontier,
        (fixture.request.exit_end_at, None),
    )


def test_checkpoint_prefix_hides_postprefix_fact_with_earlier_available_clock(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        postprefix_earlier_available_nbbo=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )

    with pytest.raises(
        rv3.SealedReplayInputError,
        match="exact checkpoint sequence frontier",
    ):
        adapter.advance_to(fixture.request.decision_at)

    decision_release = adapter.advance_to_frontier(
        fixture.request.decision_at,
        sequence_at_most=adapter.proof.input_prefix_sequence,
    )

    assert fixture.future_quote_sha256 not in decision_release.event_sha256s
    assert adapter.current_quote().bid == pytest.approx(9.99)

    postdecision_release = adapter.advance_to_frontier(
        fixture.request.decision_at,
        sequence_at_most=adapter.proof.input_prefix_sequence + 10,
    )
    assert fixture.future_quote_sha256 in postdecision_release.event_sha256s
    assert adapter.current_quote().bid == pytest.approx(10.99)




def test_sealed_adapter_rejects_payload_query_mismatch(tmp_path, monkeypatch) -> None:
    fixture = _sealed_fixture(tmp_path, bad_ohlcv_query_binding=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    with pytest.raises(rv3.SealedReplayInputError, match="payload/query mismatch"):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture, fixture.manifest, fixture.request
        )


def test_sealed_adapter_rejects_duplicate_logical_fact(tmp_path, monkeypatch) -> None:
    fixture = _sealed_fixture(tmp_path, duplicate_nbbo=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    with pytest.raises(rv3.SealedReplayInputError, match="duplicate or ambiguous"):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture, fixture.manifest, fixture.request
        )


def test_sealed_adapter_rejects_unbound_broker_decision_authority(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, bad_broker_authority=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    with pytest.raises(rv3.SealedReplayInputError, match="decision authority"):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture, fixture.manifest, fixture.request
        )


def test_sealed_adapter_refuses_real_incomplete_coverage_grade(tmp_path) -> None:
    fixture = _sealed_fixture(tmp_path)
    grade = rv3.grade_replay_coverage(fixture.request, fixture.manifest)

    assert "fsm_dependency_profile_stream_set_mismatch" not in grade.reasons
    assert (
        "fsm_dependency_profile_receipt_stream_set_mismatch"
        not in grade.reasons
    )
    assert "read_receipt_outside_window" not in ",".join(grade.reasons)
    assert "capture_resource_binding_unverified" in grade.reasons

    with pytest.raises(rv3.SealedReplayInputError, match="coverage is not complete"):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture, fixture.manifest, fixture.request
        )


def test_canonical_no_order_window_enables_strict_zero_order_broker(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, no_order=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    mock = MockBrokerAdapter()

    assert adapter.canonical_order_intents == ()
    assert adapter.canonical_broker_lifecycle_complete is True
    adapter.configure_recorded_broker(mock)
    assert mock.recorded_lifecycle_enabled is True

    # A canonical REJECT/ABSTAIN window must not fall back to the mock's
    # quote-generated fill model. Any replay-issued order is a parity failure.
    mock.set_quote(SYMBOL, RecordedQuote(bid=9.99, ask=10.01, last=10.00))
    result = mock.place_limit_order_gtc(
        product_id=SYMBOL,
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="unexpected-replay-order",
        time_in_force="gfd",
    )
    assert result["ok"] is False
    assert mock.recorded_request_violations

    adapter.advance_to_frontier(
        fixture.request.exit_end_at,
        sequence_at_most=None,
    )
    adapter.mark_broker_lifecycle_replayed()
    assert adapter.proof.broker_lifecycle_replayed is True


def test_sealed_adapter_missing_query_never_falls_back_to_network(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    calls: list[tuple] = []

    def forbidden_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network fallback attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden_connect)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = next(
        row
        for row in fixture.manifest.decision_checkpoints
        if row.checkpoint_sha256 == fixture.request.decision_checkpoint_sha256
    )
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    frame = adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]

    with pytest.raises(rv3.SealedReplayInputError, match="extra or out-of-order"):
        adapter.ohlcv_provider(SYMBOL, interval="5m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)
    adapter.complete_decision_read_plan()
    assert calls == []
    assert adapter.network_attempt_count == 0
    assert adapter.rejected_provider_request_count == 1


def test_decision_query_reads_must_follow_one_global_cross_stream_order(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)

    with pytest.raises(rv3.SealedReplayInputError, match="out-of-order"):
        adapter.account_equity_provider(prefer_equity=True)

    adapter.abort_decision_read_plan()
    assert adapter.rejected_provider_request_count == 1


def test_sealed_microstructure_recomputes_the_exact_receipted_print_window(
    tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        no_order=True,
        microstructure_trade_flow=True,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    network_calls: list[tuple] = []

    def forbidden_connect(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("network fallback attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden_connect)
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    checkpoint = fixture.manifest.decision_checkpoints[0]
    adapter.advance_to_frontier(
        checkpoint.decision_at,
        sequence_at_most=checkpoint.input_prefix_sequence,
    )
    decision_tick = adapter.decision_tick_for_frontier(
        checkpoint.decision_at,
        checkpoint.input_prefix_sequence,
    )
    assert decision_tick is not None
    adapter.begin_decision_read_plan(decision_tick)
    adapter.ohlcv_provider(SYMBOL, interval="1m", period="1d")
    adapter.account_equity_provider(prefer_equity=True)

    with pytest.raises(rv3.SealedReplayInputError, match="differs"):
        adapter.read_microstructure(
            operation=CaptureMicrostructureOperation.TRADE_FLOW,
            symbol=SYMBOL,
            decision_at=checkpoint.decision_at,
            parameters={"window_seconds": 14.0},
        )
    result = adapter.read_microstructure(
        operation=CaptureMicrostructureOperation.TRADE_FLOW,
        symbol=SYMBOL,
        decision_at=checkpoint.decision_at,
        parameters={"window_seconds": 15.0},
    )
    adapter.complete_decision_read_plan()

    assert result == pytest.approx(1.0)
    assert network_calls == []
    assert adapter.network_attempt_count == 0
    assert adapter.rejected_provider_request_count == 1
    assert "microstructure" not in adapter.runtime_input_capabilities


@pytest.mark.parametrize(
    "corruption",
    ("omitted", "lowered_frontier", "reordered"),
)
def test_sealed_microstructure_rejects_incomplete_or_reordered_print_inventory(
    tmp_path, monkeypatch, corruption
) -> None:
    fixture = _sealed_fixture(
        tmp_path,
        no_order=True,
        microstructure_trade_flow=True,
        first_dip_receipt_corruption=corruption,
    )
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)

    with pytest.raises(
        rv3.SealedReplayInputError,
        match="complete source window",
    ):
        rv3.SealedReplayV3InputAdapter(
            fixture.capture,
            fixture.manifest,
            fixture.request,
        )


def test_actual_fsm_sealed_driver_fails_before_unsealed_runtime_inputs_are_observed(
    db, tmp_path, monkeypatch
) -> None:
    fixture = _sealed_fixture(tmp_path, scanner_snapshot=True)
    monkeypatch.setattr(rv3, "grade_replay_coverage", _complete_grade)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(
        market_profile, "is_tradeable_now", lambda _symbol, **_kwargs: True
    )
    adapter = rv3.SealedReplayV3InputAdapter(
        fixture.capture, fixture.manifest, fixture.request
    )
    seed = rv3.seed_replay_session(
        db,
        rv3.RecordedArm(
            symbol=SYMBOL,
            live_eligible_at_utc=(BASE - timedelta(seconds=30)).isoformat(),
        ),
        execution_family="robinhood_spot",
    )
    with pytest.raises(
        rv3.SealedReplayInputError,
        match="forbids wall-clock broker freshness",
    ):
        rv3.ReplayV3Driver.from_sealed_inputs(
            db,
            seed,
            mock=MockBrokerAdapter(freshness_mode="wall"),
            sealed_inputs=adapter,
        )

    driver = rv3.ReplayV3Driver.from_sealed_inputs(
        db,
        seed,
        mock=MockBrokerAdapter(freshness_mode="sim"),
        sealed_inputs=adapter,
    )

    session_before = db.get(TradingAutomationSession, seed.session_id)
    assert session_before is not None
    state_before = session_before.state
    risk_before = deepcopy(session_before.risk_snapshot_json)
    event_count_before = db.query(TradingAutomationEvent).count()
    tick_calls: list[tuple] = []

    def forbidden_tick(*args, **kwargs):
        tick_calls.append((args, kwargs))
        raise AssertionError("real FSM ran before sealed runtime capability gate")

    monkeypatch.setattr(lr, "tick_live_session", forbidden_tick)

    with pytest.raises(
        rv3.SealedReplayInputError,
        match=(
            "sealed_runtime_input_family_unavailable:"
            "governance,microstructure,selection_pipeline"
        ),
    ):
        driver.run()

    session_after = db.get(TradingAutomationSession, seed.session_id)
    assert session_after is not None
    assert session_after.state == state_before
    assert session_after.risk_snapshot_json == risk_before
    assert db.query(TradingAutomationEvent).count() == event_count_before
    assert tick_calls == []
    assert adapter.advanced_to is None
    assert adapter.rejected_provider_request_count == 0
    assert adapter.network_attempt_count == 0
    assert driver.python_network_attempt_count == 0
