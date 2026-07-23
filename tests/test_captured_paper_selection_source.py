from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings as runtime_settings
from app.db import engine
from app.models.trading import (
    BrainGraphNode,
    BrainNodeState,
    MomentumStrategyVariant,
    MomentumSymbolViability,
)
from app.services.trading.momentum_neural import viability as viability_module
from app.services.trading.momentum_neural.captured_paper_selection_producer import (
    CapturedPaperSelectionAuthority,
    CapturedPaperSelectionVariantBinding,
)
from app.services.trading.momentum_neural.captured_paper_selection_source import (
    CapturedPaperSelectionSourceUnavailable,
    SqlAlchemyCapturedViabilitySnapshotSource,
)
from app.services.trading.momentum_neural.captured_paper_variant_binding import (
    CapturedPaperVariantBindingAuthority,
    apply_captured_paper_variant_bindings,
    plan_captured_paper_variant_bindings,
)
from app.services.trading.momentum_neural.captured_viability_adapter import (
    COVERAGE_UNAVAILABLE,
    SCORED,
    score_captured_viability,
)
from app.services.trading.momentum_neural.context import (
    build_momentum_regime_context,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureStream,
    sha256_json,
)
from app.services.trading.momentum_neural.viability import (
    ViabilitySettingsProjection,
)
from app.services.yf_session import (
    FundamentalsProviderState,
    FundamentalsReceipt,
    FundamentalsReceiptOrigin,
    FundamentalsReceiptStatus,
)


UTC = timezone.utc
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HUB_NODE_ID = "nm_momentum_crypto_intel"


def _fresh_fundamentals(
    symbol: str,
    *,
    short_name: str | None = None,
) -> FundamentalsReceipt:
    return FundamentalsReceipt(
        symbol=symbol,
        status=FundamentalsReceiptStatus.FRESH_DATA,
        provider_state=FundamentalsProviderState.AVAILABLE,
        origin=FundamentalsReceiptOrigin.NETWORK,
        observed_at=datetime.now(UTC),
        data={"short_name": short_name or symbol},
        cache_ttl_seconds=86_400.0,
    )


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _seed_source(
    db,
    *,
    symbols: tuple[str, ...] = ("ACTU",),
    row_symbols: tuple[str, ...] | None = None,
):
    tick_at = datetime.now(UTC).replace(microsecond=0)
    generation = str(uuid.uuid4())
    adaptive_policy = {
        "schema_version": "test.captured-paper-adaptive-policy.v1",
        "adaptive_sizing": True,
        "paper_policy_matches_replay": True,
    }
    code_build = {
        "schema_version": "test.captured-paper-build.v1",
        "git_tree": "test-sealed-tree",
        "live_cash_authorized": False,
    }
    service_settings_sha256 = sha256_json(
        {
            "schema_version": "test.captured-paper-settings.v1",
            "account_scope": "alpaca:paper",
            "strategy_policy": "intended",
        }
    )
    binding_authority = CapturedPaperVariantBindingAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=generation,
        policy_sha256=sha256_json(adaptive_policy),
        settings_projection_sha256=service_settings_sha256,
        code_build_sha256=sha256_json(code_build),
        bound_at=tick_at,
    )
    source_variant = MomentumStrategyVariant(
        family="impulse_breakout",
        variant_key="impulse_breakout",
        version=1,
        label="Intended impulse breakout policy",
        params_json={
            "entry_style": "breakout",
            "adaptive_sizing": True,
        },
        is_active=True,
        execution_family="coinbase_spot",
        refinement_meta_json={"policy_surface": "replay_and_paper"},
        created_at=_naive(tick_at),
        updated_at=_naive(tick_at),
    )
    db.add(source_variant)
    db.flush()
    plan = plan_captured_paper_variant_bindings(
        db,
        authority=binding_authority,
        source_variant_ids=(int(source_variant.id),),
    )
    application = apply_captured_paper_variant_bindings(db, plan=plan)
    applied = application.items[0]
    selection_authority = CapturedPaperSelectionAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=generation,
        policy_sha256=binding_authority.policy_sha256,
        settings_projection_sha256=(
            binding_authority.settings_projection_sha256
        ),
        code_build_sha256=binding_authority.code_build_sha256,
        variant_bindings=(
            CapturedPaperSelectionVariantBinding(
                variant_id=applied.target_variant_id,
                family=applied.family,
                version=applied.version,
                variant_key=applied.target_variant_key,
                target_after_sha256=applied.target_after_sha256,
            ),
        ),
    )
    context = build_momentum_regime_context(
        now=tick_at,
        atr_pct=0.03,
        meta={
            "ross_scores": {symbol: 0.88 for symbol in symbols},
            "ross_signals": {
                symbol: {
                    "rvol": 8.0,
                    "daily_change_pct": 32.0,
                    "float_shares": 2_500_000.0,
                    "squeeze_fuel_rank_pct": 0.91,
                }
                for symbol in symbols
            },
            "spread_regime": "tight",
            "liquidity_regime": "hot",
            "rolling_range_state": "compression",
            "breakout_continuity": "holding",
        },
    )
    regime = context.to_public_dict()
    correlation_id = f"capture-{uuid.uuid4().hex[:24]}"
    node = db.get(BrainGraphNode, HUB_NODE_ID)
    if node is None:
        node = BrainGraphNode(
            id=HUB_NODE_ID,
            domain="trading",
            graph_version=1,
            node_type="momentum_intel",
            layer=1,
            label="Momentum viability hub",
            enabled=True,
            created_at=_naive(tick_at),
            updated_at=_naive(tick_at),
        )
        db.add(node)
        db.flush()
    state_payload = {
        "symbols_evaluated": list(symbols),
        "last_tick_utc": tick_at.isoformat(),
        "correlation_id": correlation_id,
        "regime": copy.deepcopy(regime),
    }
    state = db.get(BrainNodeState, HUB_NODE_ID)
    if state is None:
        state = BrainNodeState(
            node_id=HUB_NODE_ID,
            activation_score=0.9,
            confidence=0.9,
            local_state=state_payload,
            last_activated_at=_naive(tick_at),
            updated_at=_naive(tick_at),
        )
        db.add(state)
    else:
        state.activation_score = 0.9
        state.confidence = 0.9
        state.local_state = state_payload
        state.last_activated_at = _naive(tick_at)
        state.updated_at = _naive(tick_at)
    for symbol in row_symbols if row_symbols is not None else symbols:
        db.add(
            MomentumSymbolViability(
                symbol=symbol,
                scope="symbol",
                variant_id=int(source_variant.id),
                viability_score=0.84,
                paper_eligible=True,
                live_eligible=True,
                freshness_ts=_naive(tick_at),
                regime_snapshot_json=copy.deepcopy(regime),
                execution_readiness_json={
                    "spread_bps": 18.0,
                    "ofi": 0.55,
                    "micro_price_edge": 7.0,
                    "trade_flow": 0.62,
                    "product_tradable": True,
                },
                explain_json={"setup": "front_side_breakout"},
                evidence_window_json={"coverage": "derived_snapshot"},
                source_node_id=HUB_NODE_ID,
                correlation_id=correlation_id,
                created_at=_naive(tick_at),
                updated_at=_naive(tick_at),
            )
        )
    db.commit()
    return {
        "tick_at": tick_at,
        "generation": generation,
        "adaptive_policy": adaptive_policy,
        "code_build": code_build,
        "binding_authority": binding_authority,
        "application": application,
        "selection_authority": selection_authority,
        "source_variant": source_variant,
    }


def _source(material, *, fundamentals_reader):
    return SqlAlchemyCapturedViabilitySnapshotSource(
        engine,
        variant_application=material["application"],
        selection_authority=material["selection_authority"],
        settings_projection=ViabilitySettingsProjection.from_runtime(
            runtime_settings
        ),
        expected_account_id=ACCOUNT_ID,
        activation_generation=material["generation"],
        policy_sha256=material["binding_authority"].policy_sha256,
        service_settings_projection_sha256=(
            material["binding_authority"].settings_projection_sha256
        ),
        candidate_code_build_sha256=(
            material["binding_authority"].code_build_sha256
        ),
        adaptive_policy_snapshot=material["adaptive_policy"],
        code_build_payload=material["code_build"],
        fundamentals_reader=fundamentals_reader,
        context_max_age_seconds=60.0,
        tenbeat_entry_tilt_weight=0.0,
        wall_clock=lambda: datetime.now(UTC),
    )


def test_source_captures_full_four_stream_envelope_and_scores_without_fallback(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db)
    calls: list[str] = []

    def fundamentals(symbol: str):
        calls.append(symbol)
        return _fresh_fundamentals(
            symbol,
            short_name="Actuate Therapeutics Inc.",
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("process-global classifier fallback was consulted")

    monkeypatch.setattr(viability_module, "symbol_is_leveraged_etf", forbidden)
    monkeypatch.setattr(viability_module, "symbol_is_excluded_fund", forbidden)
    source = _source(material, fundamentals_reader=fundamentals)

    snapshots = source.read_snapshot()
    assert calls == ["ACTU"]
    assert len(snapshots) == 1
    assert source.capture_identity.generation == 2
    occurrence = source.build_occurrence(snapshots[0], source_sequence=7)
    assert tuple(event.sequence for event in occurrence.source_events) == (
        25,
        26,
        27,
        28,
    )
    assert tuple(event.stream for event in occurrence.source_events) == (
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.CODE_BUILD,
        CaptureStream.CAPTURED_VIABILITY_INPUT,
    )
    assert all(
        event.identity == source.capture_identity
        for event in occurrence.source_events
    )
    assert occurrence.source_events[-1].clocks.available_at == snapshots[0].read_at
    assert occurrence.source_events[-1].clocks.market_reference_at == (
        snapshots[0].event_at
    )
    assert occurrence.scoring_authority.selection_authority_sha256 == (
        material["selection_authority"].authority_sha256
    )
    result = score_captured_viability(
        occurrence.bundle,
        authority=occurrence.scoring_authority,
        evaluation_at=occurrence.bundle.read_at,
    )
    assert result.status == SCORED
    assert result.observation is not None
    assert result.observation.variant_id == (
        material["selection_authority"].variant_ids[0]
    )
    assert result.opportunity_consumed is False
    assert result.risk_reserved is False
    assert result.order_posted is False
    assert source.read_snapshot() == ()


def test_source_fails_closed_when_symbol_family_universe_is_partial(db) -> None:
    # 2026-07-23: the production viability writer is incremental/sparse, so a
    # partial universe is the NORMAL live state.  Symbols with incomplete
    # routes are now excluded from the cycle (fail-soft) instead of failing
    # the whole read; a fully-empty eligible set still fails closed.
    material = _seed_source(
        db,
        symbols=("ACTU", "MISS"),
        row_symbols=("ACTU",),
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
    )

    snapshots = source.read_snapshot()
    assert {item.symbol for item in snapshots} == {"ACTU"}


def test_source_fails_closed_when_no_symbol_survives_eligibility(db) -> None:
    material = _seed_source(
        db,
        symbols=("NONE",),
        row_symbols=(),
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
    )
    with pytest.raises(CapturedPaperSelectionSourceUnavailable) as rejected:
        source.read_snapshot()
    assert rejected.value.reason == "derived_source_current_snapshot_empty"


def test_source_rejects_hub_generation_drift_during_provider_query(db) -> None:
    material = _seed_source(db)

    def fundamentals(_symbol: str):
        with Session(bind=engine) as other:
            row = other.get(BrainNodeState, HUB_NODE_ID)
            assert row is not None
            changed = copy.deepcopy(dict(row.local_state or {}))
            changed["correlation_id"] = f"drift-{uuid.uuid4().hex[:20]}"
            row.local_state = changed
            other.commit()
        return _fresh_fundamentals(
            "ACTU",
            short_name="Actuate Therapeutics Inc.",
        )

    source = _source(material, fundamentals_reader=fundamentals)

    with pytest.raises(CapturedPaperSelectionSourceUnavailable) as rejected:
        source.read_snapshot()
    assert rejected.value.reason == "derived_source_hub_changed_during_capture"


def test_source_binds_dilution_clock_to_transaction_read_time(db, monkeypatch) -> None:
    material = _seed_source(db)
    seen: list[datetime] = []

    def dilution(_db, _symbol: str, *, now_utc: datetime):
        seen.append(now_utc)
        return 0.0

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.dilution_history.dilution_history_derate",
        dilution,
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(
            symbol,
            short_name="Actuate Therapeutics Inc.",
        ),
    )

    snapshots = source.read_snapshot()
    assert len(snapshots) == 1
    assert seen == [snapshots[0].read_at]


@pytest.mark.parametrize(
    ("receipt_factory", "expected_reason"),
    (
        (
            lambda symbol: FundamentalsReceipt(
                symbol=symbol,
                status=FundamentalsReceiptStatus.UNAVAILABLE,
                provider_state=FundamentalsProviderState.ERROR,
                origin=FundamentalsReceiptOrigin.NETWORK,
                observed_at=datetime.now(UTC),
                cache_ttl_seconds=86_400.0,
                reason="provider_error",
            ),
            "fundamentals_unavailable_error",
        ),
        (
            lambda symbol: FundamentalsReceipt(
                symbol=symbol,
                status=FundamentalsReceiptStatus.STALE,
                provider_state=FundamentalsProviderState.AVAILABLE,
                origin=FundamentalsReceiptOrigin.CACHE,
                observed_at=datetime.now(UTC),
                data={"short_name": "Stale Leveraged ETF"},
                cache_age_seconds=86_401.0,
                cache_ttl_seconds=86_400.0,
                reason="cache_stale",
            ),
            "fundamentals_stale_available",
        ),
        (
            lambda symbol: FundamentalsReceipt(
                symbol=symbol,
                status=FundamentalsReceiptStatus.UNAVAILABLE,
                provider_state=FundamentalsProviderState.CIRCUIT_OPEN,
                origin=FundamentalsReceiptOrigin.NONE,
                observed_at=datetime.now(UTC),
                cache_ttl_seconds=86_400.0,
                reason="circuit_open",
            ),
            "fundamentals_unavailable_circuit_open",
        ),
        (
            lambda symbol: FundamentalsReceipt(
                symbol=symbol,
                status=FundamentalsReceiptStatus.AMBIGUOUS_EMPTY,
                provider_state=FundamentalsProviderState.AVAILABLE,
                origin=FundamentalsReceiptOrigin.NETWORK,
                observed_at=datetime.now(UTC),
                cache_ttl_seconds=86_400.0,
                reason="name_missing",
            ),
            "fundamentals_ambiguous_empty_available",
        ),
    ),
)
def test_fundamentals_failure_is_decision_local_coverage_unavailable(
    db,
    monkeypatch,
    receipt_factory,
    expected_reason: str,
) -> None:
    material = _seed_source(db, symbols=("ACTU", "MISS"))

    def fundamentals(symbol: str):
        if symbol == "ACTU":
            return _fresh_fundamentals(
                symbol,
                short_name="Actuate Therapeutics Inc.",
            )
        return receipt_factory(symbol)

    classified_names: list[str | None] = []

    def classifier(name):
        classified_names.append(name)
        return False

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_leveraged_etf_name",
        classifier,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_excluded_fund_name",
        classifier,
    )
    source = _source(material, fundamentals_reader=fundamentals)

    snapshots = source.read_snapshot()
    assert {item.symbol for item in snapshots} == {"ACTU", "MISS"}
    by_symbol = {item.symbol: item for item in snapshots}
    assert by_symbol["ACTU"].source_payload["instrument_classification"] == {
        "short_name": "Actuate Therapeutics Inc.",
        "status": "available",
        "coverage_reason": None,
        "leveraged_etf": False,
        "excluded_fund": False,
        "scorer_placeholders_fail_closed": None,
    }
    unavailable_classification = by_symbol["MISS"].source_payload[
        "instrument_classification"
    ]
    assert unavailable_classification["status"] == "coverage_unavailable"
    assert unavailable_classification["coverage_reason"] == expected_reason
    assert unavailable_classification["leveraged_etf"] is None
    assert unavailable_classification["excluded_fund"] is None
    assert classified_names == [
        "Actuate Therapeutics Inc.",
        "Actuate Therapeutics Inc.",
    ]

    fresh = source.build_occurrence(by_symbol["ACTU"], source_sequence=1)
    unavailable = source.build_occurrence(by_symbol["MISS"], source_sequence=2)
    fresh_result = score_captured_viability(
        fresh.bundle,
        authority=fresh.scoring_authority,
        evaluation_at=fresh.bundle.read_at,
    )
    unavailable_result = score_captured_viability(
        unavailable.bundle,
        authority=unavailable.scoring_authority,
        evaluation_at=unavailable.bundle.read_at,
    )
    assert fresh_result.status == SCORED
    assert unavailable_result.status == COVERAGE_UNAVAILABLE
    assert any(expected_reason in reason for reason in unavailable_result.reasons)
    assert unavailable_result.observation is None
    assert unavailable_result.opportunity_consumed is False
    assert unavailable_result.risk_reserved is False
    assert unavailable_result.order_posted is False


def test_missing_typed_fundamentals_receipt_fails_only_that_decision(db) -> None:
    material = _seed_source(db)
    source = _source(
        material,
        fundamentals_reader=lambda _symbol: None,
    )

    snapshot = source.read_snapshot()[0]
    classification = snapshot.source_payload["instrument_classification"]
    assert classification["status"] == "coverage_unavailable"
    assert classification["leveraged_etf"] is None
    assert classification["excluded_fund"] is None
    occurrence = source.build_occurrence(snapshot, source_sequence=1)
    result = score_captured_viability(
        occurrence.bundle,
        authority=occurrence.scoring_authority,
        evaluation_at=occurrence.bundle.read_at,
    )
    assert result.status == COVERAGE_UNAVAILABLE
    assert result.opportunity_consumed is False
    assert result.risk_reserved is False
    assert result.order_posted is False
