from __future__ import annotations

from datetime import timedelta
import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db import engine
from app.models.trading import (
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import (
    captured_paper_initial_admission as initial,
)
from app.services.trading.momentum_neural import (
    captured_paper_preowner_promotion as promotion,
)
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    read_action_claim,
)
from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
    CapturedPaperRuntimeUnavailableError,
    validate_captured_paper_session_owner_inventory,
)
from tests.test_captured_paper_initial_admission import (
    _commit,
    _count,
    _pure_material,
    _seed_authority,
)


def _preowner_receipt(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    session_id: int = 41,
) -> initial.CommittedCapturedPaperInitialPreowner:
    marker = initial._preowner_marker(
        material,
        session_id=session_id,
        claim_token=material.material_sha256,
    )
    return initial.CommittedCapturedPaperInitialPreowner(
        session_id=session_id,
        initial_material_sha256=material.material_sha256,
        preowner_marker=marker,
        claim_token=material.material_sha256,
        account_lock_identity=AdaptiveRiskAccountLockIdentity.for_scope(
            initial.ALPACA_PAPER_ACCOUNT_SCOPE
        ),
        created=True,
    )


def _dispatch(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    session_id: int,
    first_dip_policy_mode: str = "candidate",
    capture_receipt_sha256: str | None = None,
) -> CapturedPaperDispatchRequest:
    return CapturedPaperDispatchRequest(
        session_id=session_id,
        symbol=material.symbol,
        execution_family=material.execution_family,
        account_scope=material.account_scope,
        expected_account_id=material.expected_account_id,
        code_build_sha256=material.code_build_sha256,
        config_sha256=material.config_sha256,
        capture_receipt_sha256=(
            capture_receipt_sha256 or material.capture_receipt_sha256
        ),
        runtime_generation=material.runtime_generation,
        first_dip_policy_mode=first_dip_policy_mode,
    )


def _pure_projection():
    material = _pure_material()
    receipt = _preowner_receipt(material)
    request = _dispatch(material, session_id=receipt.session_id)
    projection = promotion.build_captured_paper_pending_owner_projection(
        material=material,
        preowner_receipt=receipt,
        dispatch_request=request,
        arm_token="7ddc5883-c493-4de4-a4e5-e3f959461bfd",
        confirmed_at=material.decision_at + timedelta(milliseconds=1),
    )
    return material, receipt, request, projection


def test_pure_projection_binds_exact_arm_policy_template_and_dispatch():
    material, receipt, request, projection = _pure_projection()
    snapshot = dict(projection.risk_snapshot)
    marker = dict(projection.pending_owner_marker)
    claim_metadata = dict(projection.action_claim_metadata)

    assert projection.session_id == receipt.session_id
    assert projection.arm.symbol_claim_token == (
        f"arm-{projection.arm.arm_token}"
    )
    assert claim_metadata == {
        "stage": "live_arm_reserved",
        "variant_id": material.variant_id,
        "alpaca_account_id": material.expected_account_id,
    }
    assert projection.action_claim_metadata_sha256 == promotion._sha256_json(
        claim_metadata
    )
    assert marker["stage"] == promotion.CAPTURED_PAPER_PENDING_OWNER_STAGE
    assert marker["initial_material_sha256"] == material.material_sha256
    assert marker["preowner_marker_sha256"] == (
        receipt.preowner_marker["content_sha256"]
    )
    assert marker["dispatch_request"]["route_token"] == (
        request.route_token.to_payload()
    )
    assert marker["dispatch_provenance_sha256"] == request.provenance_sha256
    assert marker["adaptive_policy_sha256"] == material.policy_sha256
    assert marker["adaptive_policy_provenance_sha256"] == (
        material.adaptive_policy_provenance_sha256
    )
    assert marker["runner_risk_template_sha256"] == (
        material.runner_risk_template.template_sha256
    )
    assert marker["viability_snapshot_sha256"] == (
        material.viability_snapshot_sha256
    )
    assert marker["opportunity_consumed"] is False
    assert marker["risk_reserved"] is False
    assert marker["outbox_created"] is False
    assert marker["order_posted"] is False
    assert marker["broker_order_post_calls"] == 0

    assert snapshot["arm_token"] == projection.arm.arm_token
    assert snapshot["alpaca_symbol_claim_token"] == (
        projection.arm.symbol_claim_token
    )
    assert snapshot["confirmed_arm_generation"] == {
        "version": 1,
        "session_id": receipt.session_id,
        "arm_token": projection.arm.arm_token,
        "expires_at_utc": projection.arm.expires_at.isoformat(),
        "alpaca_symbol_claim_token": projection.arm.symbol_claim_token,
        "alpaca_account_scope": material.account_scope,
        "alpaca_account_id": material.expected_account_id,
        "confirmed_at_utc": projection.arm.confirmed_at.isoformat(),
    }
    assert snapshot[
        promotion.CAPTURED_PAPER_CONFIRMED_ARM_SHA256_KEY
    ] == projection.arm.confirmed_arm_generation_sha256
    assert promotion._canonical_value(
        snapshot[promotion.CAPTURED_PAPER_INITIAL_MATERIAL_KEY]
    ) == material.to_dict()
    assert snapshot["momentum_risk_policy_summary"] == dict(
        material.runner_risk_template.payload[
            "momentum_risk_policy_summary"
        ]
    )
    assert snapshot["momentum_policy_caps"] == dict(
        material.runner_risk_template.payload["momentum_policy_caps"]
    )
    assert "captured_paper_session_owner" not in snapshot
    assert "momentum_live_execution" not in snapshot


@pytest.mark.parametrize(
    ("mode", "capture_hash", "reason"),
    [
        (
            "baseline",
            None,
            "pending_owner_dispatch_material_mismatch",
        ),
        (
            "candidate",
            "f" * 64,
            "pending_owner_dispatch_material_mismatch",
        ),
    ],
)
def test_pure_projection_rejects_route_or_intended_policy_drift(
    mode,
    capture_hash,
    reason,
):
    material = _pure_material()
    receipt = _preowner_receipt(material)
    request = _dispatch(
        material,
        session_id=receipt.session_id,
        first_dip_policy_mode=mode,
        capture_receipt_sha256=capture_hash,
    )
    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match=reason,
    ):
        promotion.build_captured_paper_pending_owner_projection(
            material=material,
            preowner_receipt=receipt,
            dispatch_request=request,
            arm_token="7ddc5883-c493-4de4-a4e5-e3f959461bfd",
            confirmed_at=material.decision_at + timedelta(milliseconds=1),
        )


def test_pure_projection_rejects_tampered_preowner_marker():
    material = _pure_material()
    receipt = _preowner_receipt(material)
    marker = dict(receipt.preowner_marker)
    marker["policy_sha256"] = "e" * 64
    tampered = initial.CommittedCapturedPaperInitialPreowner(
        session_id=receipt.session_id,
        initial_material_sha256=receipt.initial_material_sha256,
        preowner_marker=marker,
        claim_token=receipt.claim_token,
        account_lock_identity=receipt.account_lock_identity,
        created=receipt.created,
    )
    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match="pending_owner_preowner_marker_mismatch",
    ):
        promotion.build_captured_paper_pending_owner_projection(
            material=material,
            preowner_receipt=tampered,
            dispatch_request=_dispatch(
                material,
                session_id=receipt.session_id,
            ),
            arm_token="7ddc5883-c493-4de4-a4e5-e3f959461bfd",
            confirmed_at=material.decision_at + timedelta(milliseconds=1),
        )


def test_pure_projection_rejects_expired_authority_without_side_effect():
    material = _pure_material()
    receipt = _preowner_receipt(material)
    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match="pending_owner_material_expired",
    ):
        promotion.build_captured_paper_pending_owner_projection(
            material=material,
            preowner_receipt=receipt,
            dispatch_request=_dispatch(
                material,
                session_id=receipt.session_id,
            ),
            arm_token="7ddc5883-c493-4de4-a4e5-e3f959461bfd",
            confirmed_at=material.expires_at,
        )


def test_pending_projection_has_no_final_owner_so_generic_inventory_rejects():
    material, receipt, _request, projection = _pure_projection()
    session = SimpleNamespace(
        id=receipt.session_id,
        symbol=material.symbol,
        execution_family=material.execution_family,
        risk_snapshot_json=promotion._canonical_value(
            projection.risk_snapshot
        ),
    )
    with pytest.raises(
        CapturedPaperRuntimeUnavailableError,
        match="captured_paper_session_owner_missing",
    ):
        validate_captured_paper_session_owner_inventory(
            session,
            expected_account_id=material.expected_account_id,
            expected_runtime_generation=material.runtime_generation,
        )


def test_pending_arm_claim_keeps_exact_live_runner_recovery_shape(monkeypatch):
    material, receipt, _request, projection = _pure_projection()
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_expected_account_id",
        material.expected_account_id,
    )
    session = SimpleNamespace(
        id=receipt.session_id,
        state=promotion.CAPTURED_PAPER_PENDING_OWNER_STATE,
        symbol=material.symbol,
        variant_id=material.variant_id,
        execution_family=material.execution_family,
        risk_snapshot_json=promotion._canonical_value(
            projection.risk_snapshot
        ),
    )
    claim = {
        "account_scope": material.account_scope,
        "symbol": material.symbol,
        "claim_token": projection.arm.symbol_claim_token,
        "action": "entry",
        "phase": "claimed",
        "owner_session_id": receipt.session_id,
        "client_order_id": None,
        "broker_order_id": None,
        "metadata": promotion._canonical_value(
            projection.action_claim_metadata
        ),
        "lease_expires_at": material.expires_at,
        "resolved_at": None,
    }
    assert live_runner._is_confirmed_pre_http_alpaca_arm_claim(
        session,
        claim,
        le={},
    ) is True


def test_promotion_source_has_no_provider_adapter_or_order_transport_calls():
    source = inspect.getsource(promotion)
    forbidden = (
        "requests.",
        "httpx.",
        "AlpacaSpotAdapter(",
        "post_limit_buy(",
        "post_limit_sell(",
        "reserve_adaptive_risk(",
        "claim_adaptive_risk_opportunity(",
        "commit_captured_paper_outbox(",
    )
    assert all(value not in source for value in forbidden)


def _promote(material, preowner):
    return promotion.promote_captured_paper_preowner(
        engine,
        material=material,
        preowner_receipt=preowner,
        dispatch_request=_dispatch(
            material,
            session_id=preowner.session_id,
        ),
        verification_at=material.decision_at + timedelta(milliseconds=2),
        assert_service_fence_held=lambda: None,
    )


def test_real_db_promotion_atomicity_and_fail_closed_scenarios(db, monkeypatch):
    material, _ = _seed_authority(db, symbol="PEND")
    preowner = _commit(material)

    promoted = _promote(material, preowner)

    db.rollback()
    session = db.get(TradingAutomationSession, promoted.session_id)
    assert promoted.created is True
    assert session is not None
    assert session.state == promotion.CAPTURED_PAPER_PENDING_OWNER_STATE
    assert session.source_node_id == "captured_paper_preowner_promotion"
    assert session.correlation_id == material.material_sha256
    snapshot = session.risk_snapshot_json
    assert snapshot == promotion._canonical_value(
        promotion.build_captured_paper_pending_owner_projection(
            material=material,
            preowner_receipt=preowner,
            dispatch_request=_dispatch(
                material,
                session_id=preowner.session_id,
            ),
            arm_token=promoted.arm_token,
            confirmed_at=promotion._arm_from_legacy_marker(
                snapshot["confirmed_arm_generation"]
            ).confirmed_at,
        ).risk_snapshot
    )
    assert "captured_paper_session_preowner" not in snapshot
    assert "captured_paper_session_owner" not in snapshot
    assert snapshot[promotion.CAPTURED_PAPER_PENDING_OWNER_KEY][
        "content_sha256"
    ] == promoted.pending_owner_marker["content_sha256"]

    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["claim_token"] == promoted.arm_claim_token
    assert claim["phase"] == "claimed"
    assert claim["owner_session_id"] == promoted.session_id
    assert claim["client_order_id"] is None
    assert claim["broker_order_id"] is None
    assert claim["lease_expires_at"] >= promotion._arm_from_legacy_marker(
        snapshot["confirmed_arm_generation"]
    ).expires_at
    assert claim["metadata"] == {
        "stage": "live_arm_reserved",
        "variant_id": material.variant_id,
        "alpaca_account_id": material.expected_account_id,
    }

    events = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == promoted.session_id,
            TradingAutomationEvent.event_type
            == "captured_paper_pending_owner_committed",
        )
        .all()
    )
    assert len(events) == 1
    assert events[0].payload_json["opportunity_consumed"] is False
    assert events[0].payload_json["risk_reserved"] is False
    assert events[0].payload_json["outbox_created"] is False
    assert events[0].payload_json["order_posted"] is False
    assert events[0].payload_json["broker_order_post_calls"] == 0
    assert events[0].payload_json["config_sha256"] == material.config_sha256
    assert events[0].payload_json["capture_receipt_sha256"] == (
        material.capture_receipt_sha256
    )
    assert events[0].payload_json["policy_sha256"] == material.policy_sha256
    for table_name in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_reservations",
        "captured_paper_post_commit_outbox",
        "trading_order_state_log",
    ):
        assert _count(db, table_name) == 0

    # Keep every real-Postgres scenario in one serialized fixture.  The shared
    # test database's full truncate is intentionally expensive; distinct
    # symbols preserve scenario isolation without multiplying cleanup windows.
    _assert_real_db_identical_retry_is_idempotent(db)
    _assert_real_db_route_mismatch_leaves_exact_preowner_untouched(db)
    _assert_real_db_lost_service_fence_under_locks_preserves_preowner(db)
    _assert_real_db_post_lock_expiry_preserves_preowner(db, monkeypatch)
    _assert_real_db_failure_after_claim_cas_rolls_back_to_preowner(
        db,
        monkeypatch,
    )


def _assert_real_db_identical_retry_is_idempotent(db):
    material, _ = _seed_authority(db, symbol="IDEM")
    preowner = _commit(material)

    first = _promote(material, preowner)
    second = promotion.promote_captured_paper_preowner(
        engine,
        material=material,
        preowner_receipt=preowner,
        dispatch_request=_dispatch(material, session_id=preowner.session_id),
        verification_at=material.decision_at + timedelta(milliseconds=3),
        assert_service_fence_held=lambda: None,
    )

    assert first.created is True
    assert second.created is False
    assert second.arm_token == first.arm_token
    assert second.arm_claim_token == first.arm_claim_token
    assert second.projection_sha256 == first.projection_sha256
    db.rollback()
    assert (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == first.session_id,
            TradingAutomationEvent.event_type
            == "captured_paper_pending_owner_committed",
        )
        .count()
        == 1
    )


def _assert_real_db_route_mismatch_leaves_exact_preowner_untouched(db):
    material, viability_id = _seed_authority(db, symbol="DRFT")
    preowner = _commit(material)
    bad_request = _dispatch(
        material,
        session_id=preowner.session_id,
        capture_receipt_sha256="f" * 64,
    )

    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match="pending_owner_dispatch_material_mismatch",
    ):
        promotion.promote_captured_paper_preowner(
            engine,
            material=material,
            preowner_receipt=preowner,
            dispatch_request=bad_request,
            verification_at=material.decision_at + timedelta(milliseconds=2),
            assert_service_fence_held=lambda: None,
        )
    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match="pending_owner_material_expired",
    ):
        promotion.promote_captured_paper_preowner(
            engine,
            material=material,
            preowner_receipt=preowner,
            dispatch_request=_dispatch(
                material,
                session_id=preowner.session_id,
            ),
            verification_at=material.expires_at,
            assert_service_fence_held=lambda: None,
        )
    viability = db.get(MomentumSymbolViability, viability_id)
    assert viability is not None
    viability.viability_score = float(viability.viability_score) - 0.01
    db.commit()
    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match="initial_viability_mismatch",
    ):
        promotion.promote_captured_paper_preowner(
            engine,
            material=material,
            preowner_receipt=preowner,
            dispatch_request=_dispatch(
                material,
                session_id=preowner.session_id,
            ),
            verification_at=material.decision_at + timedelta(milliseconds=2),
            assert_service_fence_held=lambda: None,
        )

    db.rollback()
    session = db.get(TradingAutomationSession, preowner.session_id)
    assert session is not None
    assert session.state == initial.CAPTURED_PAPER_PREOWNER_STATE
    assert session.risk_snapshot_json[
        "captured_paper_session_preowner"
    ] == dict(preowner.preowner_marker)
    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["claim_token"] == material.material_sha256
    assert claim["phase"] == "claimed"


def _assert_real_db_lost_service_fence_under_locks_preserves_preowner(db):
    material, _ = _seed_authority(db, symbol="FENC")
    preowner = _commit(material)
    checks = 0

    def lose_on_second_check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("service fence lost")

    with pytest.raises(
        promotion.CapturedPaperPreownerPromotionError,
        match="pending_owner_service_fence_not_held",
    ):
        promotion.promote_captured_paper_preowner(
            engine,
            material=material,
            preowner_receipt=preowner,
            dispatch_request=_dispatch(
                material,
                session_id=preowner.session_id,
            ),
            verification_at=material.decision_at + timedelta(milliseconds=2),
            assert_service_fence_held=lose_on_second_check,
        )

    assert checks == 2
    db.rollback()
    session = db.get(TradingAutomationSession, preowner.session_id)
    assert session is not None
    assert session.state == initial.CAPTURED_PAPER_PREOWNER_STATE
    assert session.risk_snapshot_json[
        "captured_paper_session_preowner"
    ] == dict(preowner.preowner_marker)
    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["claim_token"] == material.material_sha256
    assert claim["metadata"]["stage"] == initial.CAPTURED_PAPER_PREOWNER_STATE


def _assert_real_db_post_lock_expiry_preserves_preowner(db, monkeypatch):
    material, _ = _seed_authority(db, symbol="TIME")
    preowner = _commit(material)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            promotion,
            "_locked_database_clock",
            lambda _db: material.expires_at,
        )
        with pytest.raises(
            promotion.CapturedPaperPreownerPromotionError,
            match="pending_owner_material_expired",
        ):
            _promote(material, preowner)

    db.rollback()
    session = db.get(TradingAutomationSession, preowner.session_id)
    assert session is not None
    assert session.state == initial.CAPTURED_PAPER_PREOWNER_STATE
    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["claim_token"] == material.material_sha256
    assert claim["metadata"]["stage"] == initial.CAPTURED_PAPER_PREOWNER_STATE


def _assert_real_db_failure_after_claim_cas_rolls_back_to_preowner(
    db,
    monkeypatch,
):
    material, _ = _seed_authority(db, symbol="ROLL")
    preowner = _commit(material)

    def fail_event(*args, **kwargs):
        raise RuntimeError("injected_after_claim_cas")

    monkeypatch.setattr(promotion, "TradingAutomationEvent", fail_event)
    with pytest.raises(RuntimeError, match="injected_after_claim_cas"):
        _promote(material, preowner)

    db.rollback()
    session = db.get(TradingAutomationSession, preowner.session_id)
    assert session is not None
    assert session.state == initial.CAPTURED_PAPER_PREOWNER_STATE
    assert session.risk_snapshot_json[
        "captured_paper_session_preowner"
    ] == dict(preowner.preowner_marker)
    readable, claim = read_action_claim(
        db,
        symbol=material.symbol,
        account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
    )
    assert readable is True
    assert claim is not None
    assert claim["claim_token"] == material.material_sha256
    assert claim["metadata"]["stage"] == initial.CAPTURED_PAPER_PREOWNER_STATE
    assert db.execute(
        text(
            "SELECT count(*) FROM trading_automation_events "
            "WHERE session_id=:session_id "
            "AND event_type='captured_paper_pending_owner_committed'"
        ),
        {"session_id": preowner.session_id},
    ).scalar_one() == 0
