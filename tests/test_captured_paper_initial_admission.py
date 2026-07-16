from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.db import engine
from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import captured_paper_initial_admission as initial
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
    AdaptiveRiskPolicy,
    AdaptiveRiskPolicySettingsReceipt,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    read_action_claim,
)
from app.services.trading.momentum_neural.captured_adaptive_risk_source import (
    CapturedAdaptiveRiskPolicySpec,
)
from app.services.trading.momentum_neural.live_fsm import LIVE_RUNNER_RUNNABLE_STATES


UTC = timezone.utc
ACCOUNT_ID = "7ddc5883-c493-4de4-a4e5-e3f959461bfd"
RUNTIME_GENERATION = "97beeb02-84c7-47a8-859d-44d409674ec0"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _inventory_sha256(read_ids: tuple[str, ...]) -> str:
    payload = {
        "schema_version": initial.INITIAL_READ_INVENTORY_SCHEMA_VERSION,
        "read_ids": list(read_ids),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _policy_receipt() -> AdaptiveRiskPolicySettingsReceipt:
    policy = AdaptiveRiskPolicy(
        policy_version="shared-replay-paper-v1",
        policy_source="test:sealed-shared-policy",
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


def _runner_risk_template(
    *,
    receipt: AdaptiveRiskPolicySettingsReceipt,
    code_build_sha256: str,
    capture_config_sha256: str,
    feature_flags_sha256: str,
    viability_snapshot_sha256: str,
    decision_at: datetime,
    symbol: str = "SEAL",
) -> initial.CapturedPaperInitialRunnerRiskTemplate:
    config_sha256 = receipt.settings_projection_sha256
    spec = CapturedAdaptiveRiskPolicySpec(
        policy=receipt.policy,
        code_build_sha256=code_build_sha256,
        effective_config_sha256=receipt.settings_projection_sha256,
        feature_flags_sha256=feature_flags_sha256,
    )
    payload = {
        "momentum_risk_policy_summary": {
            "adaptive_policy_sha256": receipt.policy.policy_sha256,
            "adaptive_policy_provenance_sha256": spec.provenance_sha256,
            "settings_projection_sha256": config_sha256,
            "code_build_sha256": code_build_sha256,
            "capture_config_sha256": capture_config_sha256,
            "feature_flags_sha256": feature_flags_sha256,
            "applies_to_execution_surfaces": ["alpaca_paper", "replay"],
            "disable_live_if_governance_inhibit": True,
        },
        "momentum_risk_policy_resolved_utc": decision_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "momentum_risk": {
            "policy_version": receipt.policy.policy_version,
            "evaluated_at_utc": decision_at.isoformat().replace("+00:00", "Z"),
            "allowed": True,
            "severity": "ok",
            "checks": [{"id": "captured_policy_parity", "ok": True}],
            "warnings": [],
            "errors": [],
        },
        "viability_brief": {
            "symbol": symbol,
            "scope": "symbol",
            "viability_score": 0.82,
            "paper_eligible": True,
            "live_eligible": True,
        },
        "execution_readiness_subset": {
            "captured": True,
            "spread_bps": 7.25,
            "coverage": "complete",
        },
        "momentum_policy_caps": {
            "max_hold_seconds": 1_173,
            "cooldown_after_stopout_seconds": 83,
            "max_notional_per_trade_usd": 1_731.25,
            "max_loss_per_trade_usd": 121.19,
        },
        "momentum_policy_caps_derivation": {
            "source": "captured-adaptive-policy",
            "equity_relative": True,
            "policy_sha256": receipt.policy.policy_sha256,
        },
    }
    return initial.CapturedPaperInitialRunnerRiskTemplate(
        payload=payload,
        payload_sha256=initial._sha256_json(payload),
        source_receipt_sha256s={
            "adaptive_policy_settings": receipt.settings_projection_sha256,
            "capture_config": capture_config_sha256,
            "execution_readiness": viability_snapshot_sha256,
            "momentum_policy_caps": _digest("runner-policy-caps-receipt"),
            "momentum_risk_evaluation": _digest("runner-risk-evaluation-receipt"),
            "viability_snapshot": viability_snapshot_sha256,
        },
    )


def _seed_authority(
    db,
    *,
    symbol: str = "SEAL",
    viability_age_seconds: float = 0.02,
    viability_scope: str = "symbol",
):
    decision_at = datetime.now(UTC).replace(microsecond=123456)
    user = models.User(name=f"captured-preowner-{_digest(symbol)[:12]}")
    db.add(user)
    db.flush()
    variant = MomentumStrategyVariant(
        family="captured_paper",
        variant_key=f"initial-{symbol.lower()}",
        version=1,
        label=f"Captured paper initial {symbol}",
        params_json={"setup_family": "first_dip", "paper_policy": "parity"},
        is_active=True,
        execution_family=initial.ALPACA_SPOT_EXECUTION_FAMILY,
        refinement_meta_json={"source": "sealed_initial_admission_test"},
    )
    db.add(variant)
    db.flush()
    viability = MomentumSymbolViability(
        symbol=symbol,
        scope=viability_scope,
        variant_id=int(variant.id),
        viability_score=0.82,
        paper_eligible=True,
        # PAPER exercises the intended live-entry policy while account scope,
        # settings, and adapter identity separately prohibit live cash.
        live_eligible=True,
        freshness_ts=(
            decision_at - timedelta(seconds=viability_age_seconds)
        ).replace(tzinfo=None),
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json={"captured": True},
        explain_json={"reason": "sealed-test-authority"},
        evidence_window_json={"coverage": "complete"},
        source_node_id="captured_initial_test",
        correlation_id=_digest(f"viability:{symbol}"),
    )
    db.add(viability)
    db.commit()
    db.refresh(variant)
    db.refresh(viability)

    read_ids = (
        f"capture://iqfeed/nbbo/{symbol}/0001",
        f"capture://iqfeed/trade/{symbol}/0001",
        f"capture://selection/{symbol}/0001",
    )
    policy_receipt = _policy_receipt()
    code_build_sha256 = _digest("code-build")
    capture_config_sha256 = _digest("capture-config")
    feature_flags_sha256 = _digest("feature-flags")
    viability_snapshot_sha256 = (
        initial.captured_paper_initial_viability_sha256(viability)
    )
    runner_risk_template = _runner_risk_template(
        receipt=policy_receipt,
        code_build_sha256=code_build_sha256,
        capture_config_sha256=capture_config_sha256,
        feature_flags_sha256=feature_flags_sha256,
        viability_snapshot_sha256=viability_snapshot_sha256,
        decision_at=decision_at,
        symbol=symbol,
    )
    material = initial.CapturedPaperInitialSessionMaterial(
        symbol=symbol,
        user_id=int(user.id),
        variant_id=int(variant.id),
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
        expected_account_id=ACCOUNT_ID,
        runtime_generation=RUNTIME_GENERATION,
        execution_family=initial.ALPACA_SPOT_EXECUTION_FAMILY,
        code_build_sha256=code_build_sha256,
        config_sha256=capture_config_sha256,
        capture_receipt_sha256=_digest("capture-receipt"),
        policy_sha256=policy_receipt.policy.policy_sha256,
        adaptive_policy_settings_projection=(
            policy_receipt.to_settings_projection()
        ),
        settings_projection_sha256=policy_receipt.settings_projection_sha256,
        feature_flags_sha256=feature_flags_sha256,
        adaptive_policy_provenance_sha256=(
            CapturedAdaptiveRiskPolicySpec(
                policy=policy_receipt.policy,
                code_build_sha256=code_build_sha256,
                effective_config_sha256=(
                    policy_receipt.settings_projection_sha256
                ),
                feature_flags_sha256=feature_flags_sha256,
            ).provenance_sha256
        ),
        runner_risk_template=runner_risk_template,
        trigger_read_receipt_sha256=_digest(f"trigger:{symbol}"),
        captured_input_attestation_sha256=_digest(f"attestation:{symbol}"),
        captured_read_ids=read_ids,
        captured_read_inventory_sha256=_inventory_sha256(read_ids),
        selection_receipt_sha256=_digest(f"selection:{symbol}"),
        strategy_variant_sha256=initial.captured_paper_initial_variant_sha256(
            variant
        ),
        viability_snapshot_sha256=viability_snapshot_sha256,
        decision_at=decision_at,
        expires_at=decision_at + timedelta(seconds=30),
    )
    viability_id = int(viability.id)
    db.rollback()
    return material, viability_id


def _pure_material() -> initial.CapturedPaperInitialSessionMaterial:
    decision_at = datetime(2026, 7, 16, 12, 0, 0, 123456, tzinfo=UTC)
    policy_receipt = _policy_receipt()
    code_build_sha256 = _digest("code-build")
    capture_config_sha256 = _digest("capture-config")
    feature_flags_sha256 = _digest("feature-flags")
    viability_snapshot_sha256 = _digest("pure-viability-snapshot")
    runner_risk_template = _runner_risk_template(
        receipt=policy_receipt,
        code_build_sha256=code_build_sha256,
        capture_config_sha256=capture_config_sha256,
        feature_flags_sha256=feature_flags_sha256,
        viability_snapshot_sha256=viability_snapshot_sha256,
        decision_at=decision_at,
    )
    policy_spec = CapturedAdaptiveRiskPolicySpec(
        policy=policy_receipt.policy,
        code_build_sha256=code_build_sha256,
        effective_config_sha256=policy_receipt.settings_projection_sha256,
        feature_flags_sha256=feature_flags_sha256,
    )
    read_ids = (
        "capture://iqfeed/nbbo/SEAL/pure",
        "capture://iqfeed/trade/SEAL/pure",
        "capture://selection/SEAL/pure",
    )
    return initial.CapturedPaperInitialSessionMaterial(
        symbol="SEAL",
        user_id=1,
        variant_id=1,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
        expected_account_id=ACCOUNT_ID,
        runtime_generation=RUNTIME_GENERATION,
        execution_family=initial.ALPACA_SPOT_EXECUTION_FAMILY,
        code_build_sha256=code_build_sha256,
        config_sha256=capture_config_sha256,
        capture_receipt_sha256=_digest("capture-receipt"),
        policy_sha256=policy_receipt.policy.policy_sha256,
        adaptive_policy_settings_projection=(
            policy_receipt.to_settings_projection()
        ),
        settings_projection_sha256=policy_receipt.settings_projection_sha256,
        feature_flags_sha256=feature_flags_sha256,
        adaptive_policy_provenance_sha256=policy_spec.provenance_sha256,
        runner_risk_template=runner_risk_template,
        trigger_read_receipt_sha256=_digest("trigger:SEAL"),
        captured_input_attestation_sha256=_digest("attestation:SEAL"),
        captured_read_ids=read_ids,
        captured_read_inventory_sha256=_inventory_sha256(read_ids),
        selection_receipt_sha256=_digest("selection:SEAL"),
        strategy_variant_sha256=_digest("pure-strategy-variant"),
        viability_snapshot_sha256=viability_snapshot_sha256,
        decision_at=decision_at,
        expires_at=decision_at + timedelta(seconds=30),
    )


def _count(db, table_name: str) -> int:
    allowed = {
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
        "trading_automation_events",
        "trading_automation_sessions",
        "trading_order_state_log",
    }
    assert table_name in allowed
    return int(
        db.execute(text(f'SELECT count(*) FROM "{table_name}"')).scalar_one()
    )


def _assert_test_service_fence_held() -> None:
    return None


def _commit(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    assert_service_fence_held=_assert_test_service_fence_held,
):
    return initial.commit_captured_paper_initial_preowner(
        engine,
        material=material,
        verification_at=material.decision_at + timedelta(milliseconds=1),
        assert_service_fence_held=assert_service_fence_held,
    )


def test_initial_preowner_commit_is_exact_nonrunnable_and_side_effect_free(db):
    material, _ = _seed_authority(db)
    fence_checks = 0

    def _assert_fence() -> None:
        nonlocal fence_checks
        fence_checks += 1

    receipt = _commit(
        material,
        assert_service_fence_held=_assert_fence,
    )

    db.rollback()
    assert fence_checks == 2
    session = db.get(TradingAutomationSession, receipt.session_id)
    assert receipt.created is True
    assert session is not None
    assert session.state == initial.CAPTURED_PAPER_PREOWNER_STATE
    assert session.state not in LIVE_RUNNER_RUNNABLE_STATES
    assert session.mode == "live"
    assert session.venue == "alpaca"
    assert session.execution_family == initial.ALPACA_SPOT_EXECUTION_FAMILY
    assert session.symbol == material.symbol
    assert session.risk_snapshot_json == {
        "schema_version": initial.INITIAL_PREOWNER_RISK_SNAPSHOT_SCHEMA_VERSION,
        "alpaca_account_scope": initial.ALPACA_PAPER_ACCOUNT_SCOPE,
        "alpaca_account_id": ACCOUNT_ID,
        "captured_paper_runtime_generation": RUNTIME_GENERATION,
        "captured_paper_initial_material_sha256": material.material_sha256,
        "captured_paper_settings_projection_sha256": (
            material.settings_projection_sha256
        ),
        "captured_paper_feature_flags_sha256": material.feature_flags_sha256,
        "captured_paper_adaptive_policy_provenance_sha256": (
            material.adaptive_policy_provenance_sha256
        ),
        "captured_paper_adaptive_policy_settings_projection": (
            material.to_body()["adaptive_policy_settings_projection"]
        ),
        "captured_paper_initial_runner_risk_template_sha256": (
            material.runner_risk_template.template_sha256
        ),
        "captured_paper_initial_runner_risk_template": (
            material.runner_risk_template.to_dict()
        ),
        "captured_paper_session_preowner": dict(receipt.preowner_marker),
    }
    assert "captured_paper_session_owner" not in session.risk_snapshot_json
    assert "confirmed_arm_generation" not in session.risk_snapshot_json
    assert session.allocation_decision_json == {}

    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["phase"] == "claimed"
    assert claim["action"] == "entry"
    assert claim["claim_token"] == material.material_sha256
    assert claim["owner_session_id"] == receipt.session_id
    assert claim["client_order_id"] is None
    assert claim["broker_order_id"] is None
    assert claim["metadata"]["preowner_marker_sha256"] == (
        receipt.preowner_marker["content_sha256"]
    )
    assert claim["metadata"]["settings_projection_sha256"] == (
        material.settings_projection_sha256
    )
    assert claim["metadata"]["feature_flags_sha256"] == (
        material.feature_flags_sha256
    )
    assert claim["metadata"]["adaptive_policy_provenance_sha256"] == (
        material.adaptive_policy_provenance_sha256
    )
    assert claim["metadata"]["runner_risk_template_sha256"] == (
        material.runner_risk_template.template_sha256
    )

    event = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == receipt.session_id)
        .one()
    )
    assert event.payload_json["opportunity_consumed"] is False
    assert event.payload_json["risk_reserved"] is False
    assert event.payload_json["outbox_created"] is False
    assert event.payload_json["order_posted"] is False
    assert event.payload_json["broker_order_post_calls"] == 0
    assert event.payload_json["settings_projection_sha256"] == (
        material.settings_projection_sha256
    )
    assert event.payload_json["runner_risk_template_sha256"] == (
        material.runner_risk_template.template_sha256
    )
    for table_name in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_reservations",
        "captured_paper_post_commit_outbox",
        "trading_order_state_log",
    ):
        assert _count(db, table_name) == 0
    assert db.execute(
        text(
            "SELECT count(*) FROM venue_order_idempotency "
            "WHERE venue='alpaca' AND symbol=:symbol"
        ),
        {"symbol": material.symbol},
    ).scalar_one() == 0


def test_identical_initial_material_is_idempotent_without_duplicate_event(db):
    material, _ = _seed_authority(db)

    first = _commit(material)
    second = _commit(material)

    db.rollback()
    assert first.created is True
    assert second.created is False
    assert second.session_id == first.session_id
    assert dict(second.preowner_marker) == dict(first.preowner_marker)
    assert _count(db, "trading_automation_sessions") == 1
    assert _count(db, "trading_automation_events") == 1
    assert _count(db, "broker_symbol_action_claims") == 1
    assert _count(db, "adaptive_risk_opportunity_claims") == 0
    assert _count(db, "adaptive_risk_reservations") == 0
    assert _count(db, "captured_paper_post_commit_outbox") == 0


def test_stale_initial_material_fails_before_any_durable_write(db):
    material, _ = _seed_authority(db)

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_material_stale_or_future",
    ):
        initial.commit_captured_paper_initial_preowner(
            engine,
            material=material,
            verification_at=material.expires_at + timedelta(microseconds=1),
            assert_service_fence_held=_assert_test_service_fence_held,
        )

    db.rollback()
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "broker_symbol_action_claims") == 0


def test_stale_viability_rejects_and_rolls_back_preowner_and_claim(db):
    context_max_age = _policy_receipt().policy.context_data_max_age_seconds
    material, _ = _seed_authority(
        db,
        viability_age_seconds=context_max_age + 0.001,
    )

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_viability_stale",
    ):
        _commit(material)

    db.rollback()
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "trading_automation_events") == 0
    assert _count(db, "broker_symbol_action_claims") == 0
    assert _count(db, "adaptive_risk_opportunity_claims") == 0
    assert _count(db, "adaptive_risk_reservations") == 0
    assert _count(db, "captured_paper_post_commit_outbox") == 0


def test_non_symbol_viability_scope_rejects_and_rolls_back_preowner(db):
    material, _ = _seed_authority(db, viability_scope="market")

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_viability_unavailable",
    ):
        _commit(material)

    db.rollback()
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "trading_automation_events") == 0
    assert _count(db, "broker_symbol_action_claims") == 0
    assert _count(db, "adaptive_risk_opportunity_claims") == 0
    assert _count(db, "adaptive_risk_reservations") == 0
    assert _count(db, "captured_paper_post_commit_outbox") == 0


def test_missing_service_fence_capability_rejects_before_transaction():
    material = _pure_material()

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_service_fence_capability_unavailable",
    ):
        initial.commit_captured_paper_initial_preowner(
            engine,
            material=material,
            verification_at=material.decision_at + timedelta(milliseconds=1),
        )


def test_service_fence_loss_after_account_locks_rolls_back_everything(db):
    material, _ = _seed_authority(db)
    calls = 0

    def _lose_fence_on_second_check() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated-service-fence-loss")

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_service_fence_not_held",
    ):
        _commit(
            material,
            assert_service_fence_held=_lose_fence_on_second_check,
        )

    db.rollback()
    assert calls == 2
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "trading_automation_events") == 0
    assert _count(db, "broker_symbol_action_claims") == 0
    assert _count(db, "adaptive_risk_opportunity_claims") == 0
    assert _count(db, "adaptive_risk_reservations") == 0
    assert _count(db, "captured_paper_post_commit_outbox") == 0


def test_changed_viability_rejects_and_rolls_back_claim_and_session(db):
    material, viability_id = _seed_authority(db)
    viability = db.get(MomentumSymbolViability, viability_id)
    assert viability is not None
    viability.explain_json = {"reason": "changed-after-sealed-selection"}
    flag_modified(viability, "explain_json")
    db.commit()

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_viability_mismatch",
    ):
        _commit(material)

    db.rollback()
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "broker_symbol_action_claims") == 0
    assert _count(db, "adaptive_risk_opportunity_claims") == 0
    assert _count(db, "adaptive_risk_reservations") == 0
    assert _count(db, "captured_paper_post_commit_outbox") == 0


def test_foreign_unresolved_symbol_claim_blocks_without_consuming_authority(db):
    material, _ = _seed_authority(db)
    foreign = acquire_action_claim(
        db,
        symbol=material.symbol,
        action="entry",
        claim_token=_digest("foreign-generation"),
        owner_session_id=None,
        client_order_id=None,
        metadata={"source": "foreign-test-owner"},
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert foreign["ok"] is True
    db.commit()

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_symbol_owned_by_other_generation",
    ):
        _commit(material)

    db.rollback()
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "broker_symbol_action_claims") == 1
    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["claim_token"] == _digest("foreign-generation")
    assert claim["owner_session_id"] is None


def test_crash_between_session_and_claim_binding_rolls_back_everything(db, monkeypatch):
    material, _ = _seed_authority(db)
    real_acquire = initial.acquire_action_claim
    calls = 0

    def _crash_on_bind(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated-preowner-bind-crash")
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(initial, "acquire_action_claim", _crash_on_bind)

    with pytest.raises(RuntimeError, match="simulated-preowner-bind-crash"):
        _commit(material)

    db.rollback()
    assert calls == 2
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "trading_automation_events") == 0
    assert _count(db, "broker_symbol_action_claims") == 0
    assert _count(db, "adaptive_risk_opportunity_claims") == 0
    assert _count(db, "adaptive_risk_reservations") == 0
    assert _count(db, "captured_paper_post_commit_outbox") == 0


def test_capability_rejects_injected_provider_route_mismatch(db):
    material, _ = _seed_authority(db)
    wrong_generation = replace(
        material,
        runtime_generation="181f313f-1822-4145-829f-d09aa2bc5d2d",
    )

    class _Provider:
        def prepare_initial_session(self, *, symbol, trigger_read_receipt_sha256):
            assert symbol == material.symbol
            assert trigger_read_receipt_sha256 == material.trigger_read_receipt_sha256
            return wrong_generation

    capability = initial.CapturedPaperInitialAdmissionCapability(
        provider=_Provider(),
        expected_account_id=material.expected_account_id,
        runtime_generation=material.runtime_generation,
        code_build_sha256=material.code_build_sha256,
        config_sha256=material.config_sha256,
        capture_receipt_sha256=material.capture_receipt_sha256,
        policy_sha256=material.policy_sha256,
        settings_projection_sha256=material.settings_projection_sha256,
        feature_flags_sha256=material.feature_flags_sha256,
        adaptive_policy_provenance_sha256=(
            material.adaptive_policy_provenance_sha256
        ),
    )

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_material_provider_route_mismatch",
    ):
        capability.prepare(
            symbol=material.symbol,
            trigger_read_receipt_sha256=material.trigger_read_receipt_sha256,
        )

    db.rollback()
    assert _count(db, "trading_automation_sessions") == 0
    assert _count(db, "broker_symbol_action_claims") == 0


def test_capability_rejects_missing_typed_material_without_db_or_broker_access():
    class _MissingProvider:
        def prepare_initial_session(self, *, symbol, trigger_read_receipt_sha256):
            return None

    settings_projection_sha256 = _digest("settings-projection")
    capability = initial.CapturedPaperInitialAdmissionCapability(
        provider=_MissingProvider(),
        expected_account_id=ACCOUNT_ID,
        runtime_generation=RUNTIME_GENERATION,
        code_build_sha256=_digest("code-build"),
        config_sha256=_digest("capture-config"),
        capture_receipt_sha256=_digest("capture-receipt"),
        policy_sha256=_digest("intended-paper-policy"),
        settings_projection_sha256=settings_projection_sha256,
        feature_flags_sha256=_digest("feature-flags"),
        adaptive_policy_provenance_sha256=_digest("policy-provenance"),
    )

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_material_provider_result_invalid",
    ):
        capability.prepare(
            symbol="SEAL",
            trigger_read_receipt_sha256=_digest("trigger:SEAL"),
        )


def test_initial_material_reconstructs_shared_replay_paper_policy_exactly():
    material = _pure_material()
    projection = material.to_body()["adaptive_policy_settings_projection"]

    assert material.config_sha256 != material.settings_projection_sha256
    assert projection["settings_projection_sha256"] == (
        material.settings_projection_sha256
    )
    assert projection["policy_sha256"] == material.policy_sha256
    assert material.runner_risk_template.source_receipt_sha256s[
        "adaptive_policy_settings"
    ] == material.settings_projection_sha256
    assert material.runner_risk_template.source_receipt_sha256s[
        "capture_config"
    ] == material.config_sha256
    assert material.runner_risk_template.payload[
        "momentum_risk_policy_summary"
    ]["capture_config_sha256"] == material.config_sha256
    assert material.runner_risk_template.payload[
        "momentum_risk_policy_summary"
    ]["applies_to_execution_surfaces"] == ("alpaca_paper", "replay")
    assert set(material.runner_risk_template.payload) == {
        "momentum_risk_policy_summary",
        "momentum_risk_policy_resolved_utc",
        "momentum_risk",
        "viability_brief",
        "execution_readiness_subset",
        "momentum_policy_caps",
        "momentum_policy_caps_derivation",
    }
    material.verify()


def test_typed_provider_material_rejects_non_symbol_viability_scope():
    material = _pure_material()
    payload = material.runner_risk_template.to_body()["payload"]
    payload["viability_brief"]["scope"] = "market"
    wrong_scope_template = initial.CapturedPaperInitialRunnerRiskTemplate(
        payload=payload,
        payload_sha256=initial._sha256_json(payload),
        source_receipt_sha256s=dict(
            material.runner_risk_template.source_receipt_sha256s
        ),
    )

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_runner_risk_template_viability_scope_mismatch",
    ):
        replace(material, runner_risk_template=wrong_scope_template)


@pytest.mark.parametrize(
    ("field_name", "reason"),
    (
        ("config_sha256", "initial_runner_risk_template_source_mismatch"),
        ("policy_sha256", "initial_adaptive_policy_hash_mismatch"),
        (
            "adaptive_policy_provenance_sha256",
            "initial_adaptive_policy_provenance_mismatch",
        ),
    ),
)
def test_initial_material_rejects_policy_identity_mutation(
    field_name: str,
    reason: str,
):
    material = _pure_material()

    with pytest.raises(initial.CapturedPaperInitialAdmissionError, match=reason):
        replace(material, **{field_name: _digest(f"mutated:{field_name}")})


def test_capture_config_and_adaptive_settings_digests_cannot_be_swapped():
    material = _pure_material()

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_runner_risk_template_source_mismatch",
    ):
        replace(material, config_sha256=material.settings_projection_sha256)
    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_adaptive_settings_projection_mismatch",
    ):
        replace(material, settings_projection_sha256=material.config_sha256)


def test_settings_projection_rejects_activation_only_magic_field():
    material = _pure_material()
    projection = material.to_body()["adaptive_policy_settings_projection"]
    projection["settings"]["paper_only_max_loss_usd"] = 50
    unsigned = dict(projection)
    unsigned.pop("settings_projection_sha256")
    mutated_projection_sha256 = initial._sha256_json(unsigned)
    projection["settings_projection_sha256"] = mutated_projection_sha256

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="adaptive_policy_settings_names_mismatch",
    ):
        replace(
            material,
            adaptive_policy_settings_projection=projection,
            settings_projection_sha256=mutated_projection_sha256,
            config_sha256=mutated_projection_sha256,
        )


@pytest.mark.parametrize(
    "forbidden_key",
    (
        "momentum_live_execution",
        "position",
        "opportunity_claim",
        "risk_reservation",
        "post_commit_outbox",
        "owner_transport",
    ),
)
def test_runner_risk_template_rejects_execution_state_fields(
    forbidden_key: str,
):
    material = _pure_material()
    payload = material.runner_risk_template.to_body()["payload"]
    payload["momentum_risk"][forbidden_key] = {"present": True}

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_runner_risk_template_execution_field_forbidden",
    ):
        initial.CapturedPaperInitialRunnerRiskTemplate(
            payload=payload,
            payload_sha256=initial._sha256_json(payload),
            source_receipt_sha256s=dict(
                material.runner_risk_template.source_receipt_sha256s
            ),
        )


def test_runner_risk_template_rejects_paper_only_activation_cap():
    material = _pure_material()
    payload = material.runner_risk_template.to_body()["payload"]
    payload["momentum_policy_caps"]["paper_only_max_notional_usd"] = 250

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_runner_risk_template_activation_field_forbidden",
    ):
        initial.CapturedPaperInitialRunnerRiskTemplate(
            payload=payload,
            payload_sha256=initial._sha256_json(payload),
            source_receipt_sha256s=dict(
                material.runner_risk_template.source_receipt_sha256s
            ),
        )


def test_runner_risk_template_rejects_payload_hash_mismatch():
    material = _pure_material()
    payload = material.runner_risk_template.to_body()["payload"]
    payload["momentum_policy_caps"]["max_hold_seconds"] += 1

    with pytest.raises(
        initial.CapturedPaperInitialAdmissionError,
        match="initial_runner_risk_template_payload_hash_mismatch",
    ):
        initial.CapturedPaperInitialRunnerRiskTemplate(
            payload=payload,
            payload_sha256=material.runner_risk_template.payload_sha256,
            source_receipt_sha256s=dict(
                material.runner_risk_template.source_receipt_sha256s
            ),
        )


def test_shared_adaptive_policy_binding_has_no_activation_only_setting():
    bound_names = {
        value.lower()
        for pair in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
        for value in pair
    }
    forbidden_fragments = {
        "activation_only",
        "paper_only",
        "one_symbol",
        "single_symbol",
        "max_symbols",
        "max_concurrent",
        "fixed_dollar",
    }

    assert not any(
        fragment in name
        for name in bound_names
        for fragment in forbidden_fragments
    )
