from __future__ import annotations

import copy
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings as runtime_settings
from app.db import engine
from app.models.captured_paper_selection_frontier import (
    CapturedPaperSelectionRouteState,
)
from app.models.trading import (
    BrainGraphNode,
    BrainNodeState,
    MomentumStrategyVariant,
    MomentumSymbolViability,
)
from app.services.trading.momentum_neural import (
    captured_paper_selection_source as selection_source_module,
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
    ViabilityExternalInputs,
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
    observed_at: datetime | None = None,
) -> FundamentalsReceipt:
    return FundamentalsReceipt(
        symbol=symbol,
        status=FundamentalsReceiptStatus.FRESH_DATA,
        provider_state=FundamentalsProviderState.AVAILABLE,
        origin=FundamentalsReceiptOrigin.NETWORK,
        observed_at=observed_at or datetime.now(UTC),
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


def _source(
    material,
    *,
    fundamentals_reader,
    wall_clock=lambda: datetime.now(UTC),
    settings_projection: ViabilitySettingsProjection | None = None,
):
    return SqlAlchemyCapturedViabilitySnapshotSource(
        engine,
        variant_application=material["application"],
        selection_authority=material["selection_authority"],
        settings_projection=(
            settings_projection
            or ViabilitySettingsProjection.from_runtime(runtime_settings)
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
        wall_clock=wall_clock,
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


def test_source_preserves_leveraged_etf_resolver_short_circuit_parity(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_leveraged_etf_name",
        lambda _name: True,
    )

    def excluded_classifier_must_not_run(_name):
        raise AssertionError(
            "fund classifier ran after leveraged-ETF short circuit"
        )

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_excluded_fund_name",
        excluded_classifier_must_not_run,
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(
            symbol,
            short_name="Example 2X Daily ETF",
        ),
    )

    snapshot = source.read_snapshot()[0]
    assert snapshot.external == ViabilityExternalInputs.neutral(
        leveraged_etf=True
    )
    assert snapshot.source_payload["instrument_classification"][
        "leveraged_etf"
    ] is True


def test_source_does_not_apply_disabled_leveraged_classifier(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db)
    projection = replace(
        ViabilitySettingsProjection.from_runtime(runtime_settings),
        chili_momentum_exclude_leveraged_etfs=False,
    )

    def leveraged_classifier_must_not_run(_name):
        raise AssertionError("disabled leveraged classifier was evaluated")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_leveraged_etf_name",
        leveraged_classifier_must_not_run,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_excluded_fund_name",
        lambda _name: False,
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(
            symbol,
            short_name="Example 2X Daily ETF",
        ),
        settings_projection=projection,
    )

    snapshot = source.read_snapshot()[0]
    assert snapshot.external.leveraged_etf is False
    assert snapshot.external.ross_quality_viability_tilt == 0.2
    assert snapshot.source_payload["instrument_classification"][
        "leveraged_etf"
    ] is False


def test_post_prefetch_symbol_receipt_does_not_claim_a_cache_lookup(db) -> None:
    material = _seed_source(db)
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
    )
    decision_at = datetime.now(UTC)

    receipt = source._fundamentals_receipt_at_decision(
        symbol="LATE",
        receipt=None,
        decision_at=decision_at,
    )

    assert receipt["lookup_performed"] is False
    assert receipt["operation"] == (
        "cache_lookup_not_performed_"
        "authoritative_symbol_arrived_after_prefetch"
    )
    assert receipt["started_at"] == decision_at
    assert receipt["returned_at"] == decision_at


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


def test_source_publishes_one_complete_hot_symbol_cohort_per_hub_snapshot(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db, symbols=("ACTU", "MISS"))
    clock = {"now": material["tick_at"] + timedelta(seconds=2)}
    resolved_symbols: list[str] = []
    original_resolver = (
        selection_source_module.resolve_viability_external_inputs_for_capture
    )

    def resolve_one_cohort(symbol, *args, **kwargs):
        resolved_symbols.append(symbol)
        return original_resolver(symbol, *args, **kwargs)

    monkeypatch.setattr(
        selection_source_module,
        "resolve_viability_external_inputs_for_capture",
        resolve_one_cohort,
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
        wall_clock=lambda: clock["now"],
    )

    first = source.read_snapshot()
    assert [item.symbol for item in first] == ["ACTU"]
    assert {item.source_variant_id for item in first} == set(
        source.source_variant_ids
    )
    assert resolved_symbols == ["ACTU"]

    next_tick = material["tick_at"] + timedelta(seconds=1)
    hub = db.get(BrainNodeState, HUB_NODE_ID)
    assert hub is not None
    hub_state = copy.deepcopy(dict(hub.local_state or {}))
    # The same hot hub symbol may repeat on every tape-delta generation.
    # A complete eligible peer must still receive the next atomic cohort;
    # otherwise one symbol can monopolize the capture frontier forever.
    hub_state["symbols_evaluated"] = ["ACTU"]
    hub_state["last_tick_utc"] = next_tick.isoformat()
    hub_regime = copy.deepcopy(dict(hub_state["regime"]))
    hub_regime["utc_iso"] = next_tick.isoformat()
    hub_regime["utc_hour"] = next_tick.hour
    hub_state["regime"] = hub_regime
    hub.local_state = hub_state
    hub.updated_at = _naive(next_tick)

    actu = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "ACTU")
        .one()
    )
    actu.freshness_ts = _naive(next_tick)
    actu_regime = copy.deepcopy(dict(actu.regime_snapshot_json or {}))
    actu_regime["utc_iso"] = next_tick.isoformat()
    actu_regime["utc_hour"] = next_tick.hour
    actu.regime_snapshot_json = actu_regime
    actu.updated_at = _naive(next_tick)
    db.commit()

    second = source.read_snapshot()
    assert [item.symbol for item in second] == ["MISS"]
    assert {item.source_variant_id for item in second} == set(
        source.source_variant_ids
    )
    assert resolved_symbols == ["ACTU", "MISS"]


def test_source_skips_symbol_whose_route_market_clock_regressed(db) -> None:
    material = _seed_source(db, symbols=("ACTU", "SAFE"))
    tick_at = material["tick_at"]
    target_variant_id = material["selection_authority"].variant_ids[0]
    route_available_at = tick_at + timedelta(seconds=1)
    route_body = {
        "schema_version": "chili.captured-paper-selection-route-state.v1",
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "activation_generation": material["generation"],
        "execution_family": "alpaca_spot",
        "authority_sha256": material["selection_authority"].authority_sha256,
        "symbol": "ACTU",
        "variant_id": target_variant_id,
        "latest_source_sequence": 7,
        "state": "eligible",
        "evidence_sha256": "1" * 64,
        "batch_sha256": "2" * 64,
        "source_event_at": tick_at.isoformat().replace("+00:00", "Z"),
        "source_available_at": route_available_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "version": 1,
    }
    db.add(
        CapturedPaperSelectionRouteState(
            account_scope="alpaca:paper",
            expected_account_id=ACCOUNT_ID,
            activation_generation=material["generation"],
            execution_family="alpaca_spot",
            authority_sha256=material["selection_authority"].authority_sha256,
            symbol="ACTU",
            variant_id=target_variant_id,
            latest_source_sequence=7,
            state="eligible",
            evidence_sha256="1" * 64,
            batch_sha256="2" * 64,
            source_event_at=tick_at,
            source_available_at=route_available_at,
            version=1,
            state_sha256=sha256_json(route_body),
            created_at=route_available_at,
            updated_at=route_available_at,
        )
    )
    regressed = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == "ACTU",
            MomentumSymbolViability.variant_id
            == int(material["source_variant"].id),
        )
        .one()
    )
    regressed_context = copy.deepcopy(dict(regressed.regime_snapshot_json or {}))
    regressed_at = tick_at - timedelta(seconds=1)
    regressed_context["utc_iso"] = regressed_at.isoformat()
    regressed_context["utc_hour"] = regressed_at.hour
    regressed.regime_snapshot_json = regressed_context
    db.commit()

    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(
            symbol,
            observed_at=tick_at,
        ),
        wall_clock=lambda: tick_at + timedelta(seconds=5),
    )

    snapshots = source.read_snapshot()

    assert [item.symbol for item in snapshots] == ["SAFE"]

    equal_context = copy.deepcopy(dict(regressed.regime_snapshot_json or {}))
    equal_context["utc_iso"] = tick_at.isoformat()
    equal_context["utc_hour"] = tick_at.hour
    regressed.regime_snapshot_json = equal_context
    db.commit()
    equal_source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(
            symbol,
            observed_at=tick_at,
        ),
        wall_clock=lambda: tick_at + timedelta(seconds=5),
    )

    equal_snapshots = equal_source.read_snapshot()

    assert [item.symbol for item in equal_snapshots] == ["ACTU"]


def test_source_drains_complete_cohorts_from_one_unchanged_hub_snapshot(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db, symbols=("ACTU", "MISS"))
    clock = {"now": material["tick_at"] + timedelta(seconds=2)}
    resolved_symbols: list[str] = []
    original_resolver = (
        selection_source_module.resolve_viability_external_inputs_for_capture
    )

    def resolve_one_cohort(symbol, *args, **kwargs):
        resolved_symbols.append(symbol)
        return original_resolver(symbol, *args, **kwargs)

    monkeypatch.setattr(
        selection_source_module,
        "resolve_viability_external_inputs_for_capture",
        resolve_one_cohort,
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
        wall_clock=lambda: clock["now"],
    )

    first = source.read_snapshot()
    second = source.read_snapshot()
    third = source.read_snapshot()

    assert [item.symbol for item in first] == ["ACTU"]
    assert [item.symbol for item in second] == ["MISS"]
    assert third == ()
    assert resolved_symbols == ["ACTU", "MISS"]


def test_post_transaction_stale_tail_does_not_revoke_prior_hub_survivor(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db, symbols=("ACTU", "MISS"))
    clock = {"now": material["tick_at"]}
    for row in (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "MISS")
        .all()
    ):
        old_at = material["tick_at"] - timedelta(seconds=59)
        row.freshness_ts = _naive(old_at)
        regime = copy.deepcopy(dict(row.regime_snapshot_json or {}))
        regime["utc_iso"] = old_at.isoformat()
        regime["utc_hour"] = old_at.hour
        row.regime_snapshot_json = regime
    db.commit()

    original_resolver = (
        selection_source_module.resolve_viability_external_inputs_for_capture
    )
    advanced = False

    def age_miss_after_transaction_snapshot(symbol, *args, **kwargs):
        nonlocal advanced
        result = original_resolver(symbol, *args, **kwargs)
        if symbol == "MISS" and not advanced:
            clock["now"] += timedelta(seconds=2)
            advanced = True
        return result

    monkeypatch.setattr(
        selection_source_module,
        "resolve_viability_external_inputs_for_capture",
        age_miss_after_transaction_snapshot,
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
        wall_clock=lambda: clock["now"],
    )

    assert [item.symbol for item in source.read_snapshot()] == ["ACTU"]
    assert source.read_snapshot() == ()
    assert source.read_snapshot() == ()


def test_source_prefers_trade_eligible_peer_over_ineligible_hub_symbol(db) -> None:
    material = _seed_source(
        db,
        symbols=("HUBONLY",),
        row_symbols=("ACTU", "HUBONLY"),
    )
    blocked = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "HUBONLY")
        .one()
    )
    blocked.paper_eligible = False
    blocked.live_eligible = False
    db.commit()
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
    )

    snapshots = source.read_snapshot()

    assert [item.symbol for item in snapshots] == ["ACTU"]
    assert {item.source_variant_id for item in snapshots} == set(
        source.source_variant_ids
    )


def test_source_admits_complete_fresh_symbol_outside_hub_tick_universe(db) -> None:
    # 2026-07-24 (a86-1306): the hub's symbols_evaluated is per-tick — a
    # tape-delta tick overwrites it with ONE symbol — so pinning the admission
    # universe to the last hub tick emptied the eligible set whenever that
    # tick's symbols lacked complete fresh routes (measured live: hub held
    # ["ARHS","S"] while 98 other symbols had complete fresh route-sets).  A
    # symbol with a complete fresh viability route-set must be admitted even
    # when the last hub tick did not evaluate it.
    material = _seed_source(
        db,
        symbols=("HUBONLY",),
        row_symbols=("ACTU",),
    )
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
    )
    snapshots = source.read_snapshot()
    assert {item.symbol for item in snapshots} == {"ACTU"}


def test_source_admits_fresh_equity_routes_when_latest_hub_tick_is_crypto_only(
    db,
) -> None:
    material = _seed_source(
        db,
        symbols=("HUBONLY",),
        row_symbols=("ACTU",),
    )
    hub = db.get(BrainNodeState, HUB_NODE_ID)
    assert hub is not None
    local_state = copy.deepcopy(dict(hub.local_state or {}))
    local_state["symbols_evaluated"] = ["BTC-USD"]
    hub.local_state = local_state
    db.commit()

    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(symbol),
    )

    snapshots = source.read_snapshot()
    assert {item.symbol for item in snapshots} == {"ACTU"}


def test_source_survives_nonfinite_floats_in_fundamentals(db) -> None:
    # 2026-07-24 (a86-0948): provider fundamentals (yfinance-shaped) can carry
    # NaN/Infinity for missing fields.  Canonical JSON cannot represent them,
    # so an unsanitized receipt raised ValueError inside sha256_json and killed
    # the whole fenced start.  Non-finite floats must map to None (missing) and
    # the read must succeed.
    material = _seed_source(db)

    def nan_fundamentals(symbol: str) -> FundamentalsReceipt:
        return FundamentalsReceipt(
            symbol=symbol,
            status=FundamentalsReceiptStatus.FRESH_DATA,
            provider_state=FundamentalsProviderState.AVAILABLE,
            origin=FundamentalsReceiptOrigin.NETWORK,
            observed_at=datetime.now(UTC),
            data={
                "short_name": symbol,
                "market_cap": float("nan"),
                "float_shares": float("inf"),
                "nested": {"ratio": float("-inf"), "ok": 1.5},
            },
            cache_ttl_seconds=86_400.0,
        )

    source = _source(material, fundamentals_reader=nan_fundamentals)
    snapshots = source.read_snapshot()
    assert len(snapshots) == 1
    receipt = snapshots[0].source_payload  # smoke: payload built + hashable


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


def test_source_binds_in_transaction_hub_when_hub_ticks_during_provider_query(
    db,
) -> None:
    # 2026-07-24 (a86-1532): the probe-vs-in-transaction hub sha EQUALITY pin
    # was unsatisfiable at production cadence (hub ticks every 5-25s; the
    # fundamentals prefetch takes multiples of that), so a mid-query hub tick
    # rejected EVERY read.  The in-transaction hub read is now authoritative:
    # a hub change during the provider query must NOT reject the read, and the
    # published snapshot must bind the IN-TRANSACTION hub sha (not the probe's).
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

    initial_hub_sha = source._probe_hub()["hub_snapshot_sha256"]
    snapshots = source.read_snapshot()
    assert {item.symbol for item in snapshots} == {"ACTU"}
    advanced_hub_sha = source._probe_hub()["hub_snapshot_sha256"]
    assert advanced_hub_sha != initial_hub_sha
    assert {item.hub_snapshot_sha256 for item in snapshots} == {
        advanced_hub_sha
    }
    assert source.read_snapshot() == ()


def test_source_revalidates_fast_market_after_supplemental_delay(
    db,
    monkeypatch,
) -> None:
    material = _seed_source(db)
    clock = {"now": material["tick_at"]}
    calls: list[str] = []

    def wall_clock() -> datetime:
        return clock["now"]

    def fundamentals(symbol: str) -> FundamentalsReceipt:
        calls.append(symbol)
        clock["now"] += timedelta(seconds=75)
        with Session(bind=engine) as other:
            hub = other.get(BrainNodeState, HUB_NODE_ID)
            assert hub is not None
            hub_payload = copy.deepcopy(dict(hub.local_state or {}))
            hub_payload["last_tick_utc"] = clock["now"].isoformat()
            hub_payload["correlation_id"] = (
                f"refreshed-{uuid.uuid4().hex[:20]}"
            )
            hub.local_state = hub_payload
            hub.updated_at = _naive(clock["now"])
            hub.last_activated_at = _naive(clock["now"])
            for row in (
                other.query(MomentumSymbolViability)
                .filter(MomentumSymbolViability.symbol == symbol)
                .all()
            ):
                row.freshness_ts = _naive(clock["now"])
                regime = copy.deepcopy(dict(row.regime_snapshot_json or {}))
                regime["utc_iso"] = clock["now"].isoformat()
                regime["utc_hour"] = clock["now"].hour
                row.regime_snapshot_json = regime
                row.updated_at = _naive(clock["now"])
            other.commit()
        return FundamentalsReceipt(
            symbol=symbol,
            status=FundamentalsReceiptStatus.FRESH_DATA,
            provider_state=FundamentalsProviderState.AVAILABLE,
            origin=FundamentalsReceiptOrigin.NETWORK,
            observed_at=clock["now"],
            data={"short_name": "Actuate Therapeutics Inc."},
            cache_ttl_seconds=86_400.0,
        )

    def classify(_short_name: str | None) -> bool:
        clock["now"] += timedelta(seconds=5)
        return False

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_leveraged_etf_name",
        classify,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.captured_paper_selection_source.is_excluded_fund_name",
        classify,
    )
    source = _source(
        material,
        fundamentals_reader=fundamentals,
        wall_clock=wall_clock,
    )

    snapshots = source.read_snapshot()
    assert len(snapshots) == 1
    assert calls == ["ACTU"]
    assert snapshots[0].read_at == (
        material["tick_at"] + timedelta(seconds=75)
    )
    occurrence = source.build_occurrence(snapshots[0], source_sequence=1)
    assert occurrence.bundle.read_receipts[0].requested_at == (
        snapshots[0].source_payload["fundamentals_query_receipt"]["started_at"]
    )
    assert occurrence.bundle.read_receipts[0].returned_at == snapshots[0].read_at
    result = score_captured_viability(
        occurrence.bundle,
        authority=occurrence.scoring_authority,
        evaluation_at=occurrence.bundle.read_at,
    )
    assert result.status == SCORED
    assert occurrence.bundle.coverage_gaps == ()
    assert source.read_snapshot() == ()
    assert calls == ["ACTU"]


def test_scored_unchanged_hub_suspends_after_source_ttl(db) -> None:
    material = _seed_source(db)
    clock = {"now": material["tick_at"]}
    source = _source(
        material,
        fundamentals_reader=lambda symbol: _fresh_fundamentals(
            symbol,
            observed_at=clock["now"],
        ),
        wall_clock=lambda: clock["now"],
    )

    snapshot = source.read_snapshot()[0]
    occurrence = source.build_occurrence(snapshot, source_sequence=1)
    result = score_captured_viability(
        occurrence.bundle,
        authority=occurrence.scoring_authority,
        evaluation_at=occurrence.bundle.read_at,
    )
    assert result.status == SCORED

    clock["now"] += timedelta(seconds=61)
    with pytest.raises(CapturedPaperSelectionSourceUnavailable) as rejected:
        source.read_snapshot()
    assert rejected.value.reason == "derived_source_hub_snapshot_stale"


def test_source_rejects_already_stale_rows_before_fundamentals(db) -> None:
    material = _seed_source(db)
    stale_at = material["tick_at"] - timedelta(seconds=70)
    row = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "ACTU")
        .one()
    )
    row.freshness_ts = _naive(stale_at)
    regime = copy.deepcopy(dict(row.regime_snapshot_json or {}))
    regime["utc_iso"] = stale_at.isoformat()
    regime["utc_hour"] = stale_at.hour
    row.regime_snapshot_json = regime
    db.commit()
    calls: list[str] = []
    source = _source(
        material,
        fundamentals_reader=lambda symbol: (
            calls.append(symbol) or _fresh_fundamentals(symbol)
        ),
        wall_clock=lambda: material["tick_at"],
    )

    with pytest.raises(CapturedPaperSelectionSourceUnavailable) as rejected:
        source.read_snapshot()
    assert rejected.value.reason == "derived_source_current_snapshot_empty"
    assert calls == ["ACTU"]


def test_source_rejects_future_rows_before_fundamentals(db) -> None:
    material = _seed_source(db)
    future_at = material["tick_at"] + timedelta(seconds=10)
    row = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "ACTU")
        .one()
    )
    row.freshness_ts = _naive(future_at)
    regime = copy.deepcopy(dict(row.regime_snapshot_json or {}))
    regime["utc_iso"] = future_at.isoformat()
    regime["utc_hour"] = future_at.hour
    row.regime_snapshot_json = regime
    db.commit()
    calls: list[str] = []
    source = _source(
        material,
        fundamentals_reader=lambda symbol: (
            calls.append(symbol) or _fresh_fundamentals(symbol)
        ),
        wall_clock=lambda: material["tick_at"],
    )

    with pytest.raises(CapturedPaperSelectionSourceUnavailable) as rejected:
        source.read_snapshot()
    assert rejected.value.reason == "derived_source_current_snapshot_empty"
    assert calls == ["ACTU"]


def test_source_excludes_corrupt_symbol_before_enrichment_but_publishes_peer(
    db,
) -> None:
    material = _seed_source(db, symbols=("ACTU", "BAD"))
    bad = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "BAD")
        .one()
    )
    regime = copy.deepcopy(dict(bad.regime_snapshot_json or {}))
    regime["utc_iso"] = "not-a-timestamp"
    bad.regime_snapshot_json = regime
    db.commit()
    calls: list[str] = []

    def fundamentals(symbol: str) -> FundamentalsReceipt:
        calls.append(symbol)
        return _fresh_fundamentals(symbol)

    source = _source(material, fundamentals_reader=fundamentals)
    snapshots = source.read_snapshot()
    assert {item.symbol for item in snapshots} == {"ACTU"}
    assert source.read_snapshot() == ()
    assert source.read_snapshot() == ()
    assert calls == ["ACTU", "BAD", "ACTU", "BAD"]


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
    assert seen == [
        snapshots[0].source_payload["source_snapshot"][
            "authoritative_transaction_snapshot_at"
        ]
    ]


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
            "provider_error",
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
            "cache_stale",
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
            "circuit_open",
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
            "name_missing",
        ),
    ),
)
def test_optional_fundamentals_failure_is_neutral_for_symbol_cohort(
    db,
    monkeypatch,
    receipt_factory,
    expected_reason: str,
) -> None:
    material = _seed_source(db, symbols=("MISS",))

    def fundamentals(symbol: str):
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
    assert [item.symbol for item in snapshots] == ["MISS"]
    snapshot = snapshots[0]
    unavailable_classification = snapshot.source_payload[
        "instrument_classification"
    ]
    assert unavailable_classification["status"] == "optional_unavailable"
    assert unavailable_classification["coverage_reason"] == expected_reason
    assert unavailable_classification["leveraged_etf"] is False
    assert unavailable_classification["excluded_fund"] is False
    assert unavailable_classification["required_for_decision"] is False
    assert (
        unavailable_classification["unavailable_policy"]
        == "neutral_fail_open_as_intended"
    )
    assert classified_names == []

    unavailable = source.build_occurrence(snapshot, source_sequence=1)
    unavailable_result = score_captured_viability(
        unavailable.bundle,
        authority=unavailable.scoring_authority,
        evaluation_at=unavailable.bundle.read_at,
    )
    assert unavailable_result.status == SCORED
    assert unavailable.bundle.coverage_gaps == ()
    assert unavailable_result.observation is not None


def test_semantic_ttl_is_bound_to_one_batch_decision_clock(db) -> None:
    material = _seed_source(db, symbols=("MISS",))
    clock = {"now": material["tick_at"]}

    def fundamentals(symbol: str) -> FundamentalsReceipt:
        observed_at = clock["now"]
        clock["now"] += timedelta(seconds=2)
        return FundamentalsReceipt(
            symbol=symbol,
            status=FundamentalsReceiptStatus.FRESH_DATA,
            provider_state=FundamentalsProviderState.AVAILABLE,
            origin=FundamentalsReceiptOrigin.CACHE,
            observed_at=observed_at,
            data={"short_name": "Example Company"},
            cache_age_seconds=4.0,
            cache_ttl_seconds=5.0,
        )

    source = _source(
        material,
        fundamentals_reader=fundamentals,
        wall_clock=lambda: clock["now"],
    )
    snapshots = source.read_snapshot()
    assert {item.read_at for item in snapshots} == {clock["now"]}
    assert [item.symbol for item in snapshots] == ["MISS"]
    snapshot = snapshots[0]
    miss_receipt = snapshot.source_payload[
        "fundamentals_query_receipt"
    ]
    assert miss_receipt["semantic_age_at_decision_seconds"] == 6.0
    assert miss_receipt["classification_usable_at_decision"] is False
    assert miss_receipt["classification_coverage_reason"] == (
        "fundamentals_semantic_ttl_expired"
    )
    assert (
        snapshot.source_payload["instrument_classification"]["status"]
        == "optional_unavailable"
    )
    for sequence, snapshot in enumerate(snapshots, start=1):
        occurrence = source.build_occurrence(
            snapshot,
            source_sequence=sequence,
        )
        result = score_captured_viability(
            occurrence.bundle,
            authority=occurrence.scoring_authority,
            evaluation_at=occurrence.bundle.read_at,
        )
        assert result.status == SCORED
        assert occurrence.bundle.coverage_gaps == ()


def test_missing_typed_fundamentals_receipt_is_recorded_but_optional(db) -> None:
    material = _seed_source(db)
    source = _source(
        material,
        fundamentals_reader=lambda _symbol: None,
    )

    snapshot = source.read_snapshot()[0]
    classification = snapshot.source_payload["instrument_classification"]
    assert classification["status"] == "optional_unavailable"
    assert classification["coverage_reason"] == (
        "typed_fundamentals_receipt_missing_or_mismatched"
    )
    assert classification["leveraged_etf"] is False
    assert classification["excluded_fund"] is False
    assert classification["required_for_decision"] is False
    occurrence = source.build_occurrence(snapshot, source_sequence=1)
    result = score_captured_viability(
        occurrence.bundle,
        authority=occurrence.scoring_authority,
        evaluation_at=occurrence.bundle.read_at,
    )
    assert result.status == SCORED
    assert occurrence.bundle.coverage_gaps == ()
