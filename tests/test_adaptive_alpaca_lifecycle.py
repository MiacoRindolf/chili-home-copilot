from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import ast
import hashlib
import inspect
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import models
from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskOpportunityClaim,
    AdaptiveRiskReservation,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    RiskInputEvidence,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
    AdaptiveRiskLedgerSnapshot,
    build_adaptive_risk_reservation_claim,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    mark_entry_transport_started,
    read_action_claim,
    release_entry_and_adaptive_reservation_pre_post,
    release_entry_and_adaptive_reservation_pre_post_committed,
)
from app.services.trading.momentum_neural import first_dip_tape_decision
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.alpaca_paper_identity import (
    alpaca_paper_account_identity_sha256,
)
from app.services.trading.venue.protocol import NormalizedOrder
from tests.first_dip_test_support import (
    captured_first_dip_runtime_for_adaptive_request,
)
from tests.test_adaptive_risk_reservation import _inputs, _request, _snapshot
from tests.test_alpaca_account_risk_reservations import (
    TEST_ALPACA_ACCOUNT_ID,
    _session,
    _variant,
)


def _identity_sha() -> str:
    return alpaca_paper_account_identity_sha256(TEST_ALPACA_ACCOUNT_ID)


def _armed_snapshot(session_id: int, le: dict) -> dict:
    values = {
        "arm_token": "adaptive-arm-token",
        "expires_at_utc": "2099-01-01T00:00:00",
        "alpaca_symbol_claim_token": f"entry-{session_id}",
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "arm_confirmed_at_utc": "2026-07-14T00:00:00",
        live_runner.KEY_LIVE_EXEC: le,
    }
    values["confirmed_arm_generation"] = {
        "version": 1,
        "session_id": session_id,
        "arm_token": values["arm_token"],
        "expires_at_utc": values["expires_at_utc"],
        "alpaca_symbol_claim_token": values["alpaca_symbol_claim_token"],
        "alpaca_account_scope": values["alpaca_account_scope"],
        "alpaca_account_id": values["alpaca_account_id"],
        "confirmed_at_utc": values["arm_confirmed_at_utc"],
    }
    return values


def _reservation_request(*, symbol: str, cid: str):
    snapshot = replace(
        _snapshot(account_scope="alpaca:paper"),
        account_identity_sha256=_identity_sha(),
    )
    inputs = _inputs(
        snapshot,
        symbol=symbol,
        decision_id=cid,
        cluster=f"equity:{symbol.lower()}",
    )
    return _request(
        symbol=symbol,
        decision_id=cid,
        client_order_id=cid,
        snapshot=snapshot,
        inputs=inputs,
        cluster=f"equity:{symbol.lower()}",
    )


def _persist_detector_audit(sess, request, detector_request, resolution) -> None:
    debug = {
        "front_side_via": "first_dip_day_leg",
        "opportunity_key": request.opportunity_key.to_payload(),
        "first_dip_tape_confirmed": True,
        "first_dip_tape": first_dip_tape_decision.first_dip_tape_decision_debug(
            resolution
        ),
        "first_dip_tape_policy": detector_request.policy.to_dict(),
        "first_dip_tape_policy_sha256": detector_request.policy.policy_sha256,
        "first_dip_tape_evaluation": resolution.evaluation.to_dict(),
        "first_dip_tape_evaluation_sha256": (
            resolution.evaluation.evaluation_sha256
        ),
        "first_dip_tape_read_id": resolution.evaluation.read_id,
        "first_dip_tape_run_bound": True,
        "first_dip_tape_decision_receipt": resolution.receipt.to_audit_dict(),
        "first_dip_tape_decision_receipt_binding_sha256": (
            resolution.receipt.binding_sha256
        ),
    }
    snapshot = dict(sess.risk_snapshot_json)
    live_exec = dict(snapshot[live_runner.KEY_LIVE_EXEC])
    live_exec["entry_trigger_debug"] = debug
    snapshot[live_runner.KEY_LIVE_EXEC] = live_exec
    sess.risk_snapshot_json = snapshot


def _captured_first_dip_fixture(sess, request):
    fixture = captured_first_dip_runtime_for_adaptive_request(request)
    _persist_detector_audit(
        sess,
        fixture.request,
        fixture.detector_request,
        fixture.detector_resolution,
    )
    return fixture


def _order(
    request,
    *,
    oid: str,
    status: str,
    cumulative: int,
    planned: int,
) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=oid,
        client_order_id=request.client_order_id,
        product_id=request.inputs.symbol,
        side="buy",
        status=status,
        order_type="limit",
        filled_size=float(cumulative),
        average_filled_price=(10.0 if cumulative else None),
        raw={
            "alpaca_status": status,
            "qty": float(planned),
            "limit_price": 10.0,
            "time_in_force": "day",
            "extended_hours": False,
            "position_intent": "buy_to_open",
        },
    )


def _live_session(db, *, symbol: str):
    user = models.User(name=f"adaptive-alpaca-{uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    variant = _variant(db, f"adaptive_alpaca_{uuid.uuid4().hex[:8]}")
    sess = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol=symbol,
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    sess.risk_snapshot_json = _armed_snapshot(int(sess.id), {"side_long": True})
    db.commit()
    return sess


def test_exit_fill_owner_inventory_is_account_bound_and_idempotent() -> None:
    symbol = f"O{uuid.uuid4().hex[:3]}".upper()
    request = _reservation_request(
        symbol=symbol,
        cid=f"chili-owner-{uuid.uuid4().hex[:12]}",
    )
    reservation_id = uuid.uuid4()
    le = {
        live_runner.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST: request.to_payload()
    }

    first = live_runner._retain_adaptive_alpaca_exit_fill_owner(
        le,
        reservation_id=reservation_id,
        provider_order_id="exit-owner-1",
        provider_client_order_id="exit-cid-1",
        owner_authority="resolved_owner_transport",
    )
    repeated = live_runner._retain_adaptive_alpaca_exit_fill_owner(
        le,
        reservation_id=reservation_id,
        provider_order_id="exit-owner-1",
        provider_client_order_id="exit-cid-1",
        owner_authority="resolved_owner_transport",
    )

    assert repeated == first
    assert len(le["alpaca_cycle_exit_fill_owners"]) == 1
    assert first["account_scope"] == "alpaca:paper"
    assert first["account_identity_sha256"] == request.inputs.account_identity_sha256
    assert len(first["binding_sha256"]) == 64
    assert live_runner._adaptive_alpaca_exit_fill_owners(
        le, reservation_id=reservation_id
    ) == [first]

    le["alpaca_cycle_exit_fill_owners"][0]["provider_order_id"] = "tampered"
    with pytest.raises(
        live_runner.AdaptiveRiskContractError,
        match="inventory binding changed",
    ):
        live_runner._adaptive_alpaca_exit_fill_owners(
            le, reservation_id=reservation_id
        )


def _seed_bound_pre_post_pair(db, *, symbol: str):
    cid = f"chili-atomic-release-{uuid.uuid4().hex[:12]}"
    binder = f"binder-{uuid.uuid4().hex}"
    sess = _live_session(db, symbol=symbol)
    empty = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    base_request = _reservation_request(symbol=symbol, cid=cid)
    evidence = dict(base_request.inputs.evidence)
    ledger_evidence = evidence["reservation_ledger"]
    evidence["reservation_ledger"] = RiskInputEvidence(
        source="alpaca_account_advisory_transaction",
        observed_at=ledger_evidence.observed_at,
        available_at=ledger_evidence.available_at,
        content_sha256=empty.content_sha256,
        provider_generation="alpaca-paper-ledger-v1",
    )
    exact_inputs = replace(
        base_request.inputs,
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        policy_buying_power_capacity_usd=base_request.inputs.buying_power_usd,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
        evidence=evidence,
    )
    request = replace(base_request, inputs=exact_inputs)
    resolution = resolve_adaptive_risk(request.policy, request.inputs)
    assert resolution.valid, resolution.rejection_reasons
    request_payload = request.to_payload()
    le = dict(sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC])
    ensured = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request_payload,
    )
    assert ensured["ok"] is True, ensured
    decision = ensured["decision"]
    db.expire_all()
    decision_row = db.get(
        AdaptiveRiskDecisionPacket,
        decision.decision_packet_sha256,
    )
    assert decision_row is not None
    packet = dict(decision_row.decision_packet_json)
    claim_payload = build_adaptive_risk_reservation_claim(
        packet,
        claim_id=cid,
    ).to_payload()
    claim_token = str(sess.risk_snapshot_json["alpaca_symbol_claim_token"])
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=claim_token,
        owner_session_id=int(sess.id),
        client_order_id=cid,
        metadata={
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "entry_post_bind_token": binder,
            "adaptive_risk_decision_packet": packet,
            "adaptive_risk_reservation_claim": claim_payload,
            "adaptive_risk_reservation_request": request_payload,
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"] is True, acquired
    db.commit()
    return {
        "session": sess,
        "request": request,
        "reservation_id": decision.reservation_id,
        "symbol": symbol,
        "claim_token": claim_token,
        "client_order_id": cid,
        "post_bind_token": binder,
    }


def _coordinated_release_kwargs(seed: dict) -> dict:
    return {
        "reservation_id": str(seed["reservation_id"]),
        "symbol": seed["symbol"],
        "claim_token": seed["claim_token"],
        "owner_session_id": int(seed["session"].id),
        "client_order_id": seed["client_order_id"],
        "post_bind_token": seed["post_bind_token"],
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "reason": "focused_transport_fence_not_committed",
    }


def test_expired_bound_adaptive_entry_cannot_split_claim_and_reservation_owner(db) -> None:
    """A different worker cannot take only the claim half of a bound reservation pair."""

    seed = _seed_bound_pre_post_pair(
        db,
        symbol=f"OWN{uuid.uuid4().hex[:3]}".upper(),
    )
    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": seed["symbol"]},
    )
    db.commit()

    attacker = acquire_action_claim(
        db,
        symbol=seed["symbol"],
        action="entry",
        claim_token=f"attacker-{uuid.uuid4().hex}",
        owner_session_id=int(seed["session"].id) + 1,
        client_order_id=f"attacker-cid-{uuid.uuid4().hex[:12]}",
        metadata={"entry_post_bind_token": f"attacker-binder-{uuid.uuid4().hex}"},
        account_scope="alpaca:paper",
    )
    assert attacker["ok"] is False, attacker
    assert attacker["reason"] == "symbol_action_claimed"
    db.rollback()

    readable, claim = read_action_claim(
        db,
        symbol=seed["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    assert claim["claim_token"] == seed["claim_token"]
    assert claim["owner_session_id"] == int(seed["session"].id)
    assert claim["client_order_id"] == seed["client_order_id"]
    assert claim["metadata"]["entry_post_bind_token"] == seed["post_bind_token"]

    reservation = db.get(AdaptiveRiskReservation, seed["reservation_id"])
    assert reservation is not None
    assert reservation.state == "reserved"
    assert reservation.account_scope == "alpaca:paper"
    assert reservation.symbol == seed["symbol"]
    if reservation.opportunity_claim_id is not None:
        opportunity = db.get(
            AdaptiveRiskOpportunityClaim,
            reservation.opportunity_claim_id,
        )
        assert opportunity is not None
        assert opportunity.status == "reserved"
        assert opportunity.reservation_id == reservation.reservation_id


def test_pre_http_claim_and_adaptive_reservation_release_commit_together(db) -> None:
    symbol = f"R{uuid.uuid4().hex[:3]}".upper()
    seed = _seed_bound_pre_post_pair(db, symbol=symbol)

    released = release_entry_and_adaptive_reservation_pre_post_committed(
        **_coordinated_release_kwargs(seed)
    )

    assert released["ok"] is True, released
    assert released["confirmed"] is True
    assert released["adaptive_released"] is True
    assert released["legacy_released"] is True
    retried = release_entry_and_adaptive_reservation_pre_post_committed(
        **_coordinated_release_kwargs(seed)
    )
    assert retried["ok"] is True, retried
    assert retried["reservation_id"] == released["reservation_id"]
    assert retried["reservation_state"] == "released"
    db.rollback()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    assert claim["phase"] == "resolved"
    proof = claim["metadata"]["pre_post_release"]
    assert proof["proven_no_transport"] is True
    assert proof["client_order_id"] == seed["client_order_id"]
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, seed["reservation_id"])
    opportunity = db.get(
        AdaptiveRiskOpportunityClaim,
        reservation.opportunity_claim_id,
    )
    assert reservation.state == "released"
    assert reservation.release_reason == "pre_post_release"
    assert opportunity.status == "available"
    assert opportunity.reservation_id is None


def test_committed_transport_marker_rolls_back_both_pre_http_releases(db) -> None:
    symbol = f"T{uuid.uuid4().hex[:3]}".upper()
    seed = _seed_bound_pre_post_pair(db, symbol=symbol)
    wrong_generation = {
        **_coordinated_release_kwargs(seed),
        "post_bind_token": f"wrong-{seed['post_bind_token']}",
    }
    mismatched = release_entry_and_adaptive_reservation_pre_post_committed(
        **wrong_generation
    )
    assert mismatched["ok"] is False
    assert mismatched["release_blocker"] == "action_claim_identity_mismatch"
    db.rollback()
    untouched = db.get(AdaptiveRiskReservation, seed["reservation_id"])
    assert untouched.state == "reserved"
    readable, untouched_claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and untouched_claim is not None
    assert untouched_claim["phase"] == "claimed"
    db.rollback()
    assert mark_entry_transport_started(
        db,
        symbol=symbol,
        claim_token=seed["claim_token"],
        owner_session_id=int(seed["session"].id),
        client_order_id=seed["client_order_id"],
        post_bind_token=seed["post_bind_token"],
        account_scope="alpaca:paper",
        alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
    ) is True
    db.commit()

    retained = release_entry_and_adaptive_reservation_pre_post_committed(
        **_coordinated_release_kwargs(seed)
    )

    assert retained["ok"] is False
    assert retained["confirmed"] is False
    assert retained["release_blocker"] == (
        "action_claim_transport_state_indeterminate"
    )
    db.rollback()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    assert claim["phase"] == "submit_indeterminate"
    assert "entry_transport_started" in claim["metadata"]
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, seed["reservation_id"])
    opportunity = db.get(
        AdaptiveRiskOpportunityClaim,
        reservation.opportunity_claim_id,
    )
    assert reservation.state == "reserved"
    assert opportunity.status == "reserved"
    assert opportunity.reservation_id == reservation.reservation_id


def test_transport_start_race_never_splits_claim_and_adaptive_reservation(db) -> None:
    """Two real connections may commit only one of two coherent outcomes.

    The transport fence and the coordinated pre-HTTP release deliberately race.
    If release wins, both ledgers are released and transport cannot start.  If
    transport wins, both ledgers remain retained for exact same-CID recovery.
    No interleaving may resolve only one ledger.
    """

    symbol = f"X{uuid.uuid4().hex[:3]}".upper()
    seed = _seed_bound_pre_post_pair(db, symbol=symbol)
    release_kwargs = _coordinated_release_kwargs(seed)
    transport_kwargs = {
        key: release_kwargs[key]
        for key in (
            "symbol",
            "claim_token",
            "owner_session_id",
            "client_order_id",
            "post_bind_token",
            "account_scope",
            "alpaca_account_id",
        )
    }
    url = os.environ.get("DATABASE_URL") or os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url, poolclass=NullPool)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    start = threading.Barrier(2, timeout=10)

    def _release_worker() -> dict:
        session = Session()
        try:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            start.wait()
            try:
                result = release_entry_and_adaptive_reservation_pre_post(
                    session,
                    **release_kwargs,
                )
                session.commit()
                return {"committed": True, "result": result}
            except Exception as exc:
                session.rollback()
                return {
                    "committed": False,
                    "blocker": getattr(exc, "blocker", type(exc).__name__),
                }
        finally:
            session.rollback()
            session.close()

    def _transport_worker() -> bool:
        session = Session()
        try:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            start.wait()
            marked = mark_entry_transport_started(session, **transport_kwargs)
            session.commit()
            return bool(marked)
        finally:
            session.rollback()
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            release_future = pool.submit(_release_worker)
            transport_future = pool.submit(_transport_worker)
            release_result = release_future.result(timeout=20)
            transport_started = transport_future.result(timeout=20)
    finally:
        engine.dispose()

    release_committed = bool(release_result["committed"])
    assert release_committed is not transport_started, (
        release_result,
        transport_started,
    )

    db.rollback()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, seed["reservation_id"])
    assert reservation is not None
    opportunity = db.get(
        AdaptiveRiskOpportunityClaim,
        reservation.opportunity_claim_id,
    )
    assert opportunity is not None

    if release_committed:
        assert claim["phase"] == "resolved"
        assert "entry_transport_started" not in claim["metadata"]
        assert reservation.state == "released"
        assert opportunity.status == "available"
        assert opportunity.reservation_id is None
    else:
        assert release_result["blocker"] == (
            "action_claim_transport_state_indeterminate"
        )
        assert claim["phase"] == "submit_indeterminate"
        assert "entry_transport_started" in claim["metadata"]
        assert reservation.state == "reserved"
        assert opportunity.status == "reserved"
        assert opportunity.reservation_id == reservation.reservation_id


def test_alpaca_lifecycle_survives_restart_and_tracks_fill_to_flat(db) -> None:
    symbol = f"A{uuid.uuid4().hex[:3]}".upper()
    cid = f"chili-adaptive-{uuid.uuid4().hex[:12]}"
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(symbol=symbol, cid=cid)
    le = dict(sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC])

    ensured = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request.to_payload(),
    )
    assert ensured["ok"] is True, ensured
    decision = ensured["decision"]
    planned = int(decision.quantity_shares)
    assert planned > 1

    assert live_runner._mark_adaptive_alpaca_submit_indeterminate(
        sess,
        le,
        reason="focused-timeout",
    )

    # A new process has only the durable request/claim payload.  Re-reserving the
    # same account/CID/request must recover the original reservation, not consume
    # the once-per-day setup opportunity or create a second risk claim.
    restarted_le: dict = {}
    restarted = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        restarted_le,
        request_payload=request.to_payload(),
    )
    assert restarted["ok"] is True
    assert restarted["decision"].reservation_id == decision.reservation_id
    assert restarted["decision"].idempotent_retry is True

    oid = f"alpaca-order-{uuid.uuid4().hex[:10]}"
    accepted = live_runner._sync_adaptive_alpaca_order_lifecycle(
        sess,
        restarted_le,
        order=_order(
            request,
            oid=oid,
            status="open",
            cumulative=0,
            planned=planned,
        ),
    )
    assert accepted["ok"] is True, accepted
    assert accepted["state"].state == "submitted"
    assert accepted["state"].opportunity_status == "reserved"

    cumulative = max(1, planned // 2)
    partial_terminal = live_runner._sync_adaptive_alpaca_order_lifecycle(
        sess,
        restarted_le,
        order=_order(
            request,
            oid=oid,
            status="canceled",
            cumulative=cumulative,
            planned=planned,
        ),
    )
    assert partial_terminal["ok"] is True, partial_terminal
    assert partial_terminal["state"].state == "filled"
    assert partial_terminal["state"].opportunity_status == "consumed"
    assert partial_terminal["state"].cumulative_filled_quantity_shares == cumulative
    assert float(partial_terminal["state"].pending_structural_risk_usd) == 0.0

    remaining = cumulative // 2

    class _PositionAdapter:
        def __init__(self, quantity: int) -> None:
            self.quantity = quantity

        def get_position_quantity(self, _symbol: str) -> float:
            return float(self.quantity)

    adapter = _PositionAdapter(remaining)
    if remaining > 0:
        reduced = live_runner._sync_adaptive_alpaca_position_lifecycle(
            sess,
            restarted_le,
            adapter=adapter,
            expected_remaining=remaining,
        )
        assert reduced["ok"] is True, reduced
        assert reduced["state"].open_quantity_shares == remaining

    adapter.quantity = 0
    flat = live_runner._sync_adaptive_alpaca_position_lifecycle(
        sess,
        restarted_le,
        adapter=adapter,
        expected_remaining=0,
    )
    assert flat["ok"] is True, flat
    assert flat["state"].state == "flat_pending_settlement"
    assert flat["state"].open_quantity_shares == 0
    assert flat["state"].opportunity_status == "consumed"

    durable = ensured["store"].read_state(decision.reservation_id)
    assert durable.state == "flat_pending_settlement"
    assert durable.open_quantity_shares == 0


def test_zero_fill_terminal_releases_first_dip_opportunity(db) -> None:
    symbol = f"Z{uuid.uuid4().hex[:3]}".upper()
    cid = f"chili-zero-{uuid.uuid4().hex[:12]}"
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(symbol=symbol, cid=cid)
    le = dict(sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC])
    ensured = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request.to_payload(),
    )
    assert ensured["ok"] is True
    planned = int(ensured["decision"].quantity_shares)
    terminal = live_runner._sync_adaptive_alpaca_order_lifecycle(
        sess,
        le,
        order=_order(
            request,
            oid=f"alpaca-zero-{uuid.uuid4().hex[:10]}",
            status="canceled",
            cumulative=0,
            planned=planned,
        ),
    )
    assert terminal["ok"] is True, terminal
    assert terminal["state"].state == "released"
    assert terminal["state"].opportunity_status == "available"


def test_late_fill_quarantine_stays_reconcilable_and_blocks_new_entries(db) -> None:
    symbol = f"Q{uuid.uuid4().hex[:3]}".upper()
    cid = f"chili-late-quarantine-{uuid.uuid4().hex[:12]}"
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(symbol=symbol, cid=cid)
    le = dict(sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC])
    ensured = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request.to_payload(),
    )
    assert ensured["ok"] is True, ensured
    decision = ensured["decision"]
    planned = int(decision.quantity_shares)
    assert planned > 1
    released = ensured["store"].release_zero_fill(
        decision.reservation_id,
        reason="pre_post_release",
    )
    assert released.state == "released"
    stale_outer_le = dict(le)

    late_quantity = max(1, planned // 2)
    late = live_runner._sync_adaptive_alpaca_order_lifecycle(
        sess,
        le,
        order=_order(
            request,
            oid=f"alpaca-late-{uuid.uuid4().hex[:10]}",
            status="canceled",
            cumulative=late_quantity,
            planned=planned,
        ),
    )
    assert late["ok"] is True, late
    assert late["exposure_quarantined"] is True
    assert late["adaptive_risk_reconciliation_required"] is True
    assert late["state"].state == "exposure_quarantined"
    assert float(late["state"].pending_structural_risk_usd) == 0.0
    blocker = le["adaptive_risk_alpaca_lifecycle_blocker"]
    assert blocker["reason"] == "adaptive_risk_exposure_quarantined"
    assert blocker["reservation_id"] == str(decision.reservation_id)
    assert blocker["reconciliation_required"] is True
    assert live_runner._alpaca_entries_quarantined(sess) is True
    same_cid_recovery = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request.to_payload(),
        expected_quantity=planned,
    )
    assert same_cid_recovery["ok"] is True
    assert same_cid_recovery["exposure_quarantined"] is True
    assert stale_outer_le[live_runner.KEY_ADAPTIVE_ALPACA_LIFECYCLE][
        "state"
    ] == "reserved"
    assert live_runner._refresh_adaptive_alpaca_lifecycle_from_session(
        sess,
        stale_outer_le,
    ) is True
    assert stale_outer_le[live_runner.KEY_ADAPTIVE_ALPACA_LIFECYCLE][
        "state"
    ] == "exposure_quarantined"
    assert stale_outer_le["adaptive_risk_alpaca_lifecycle_blocker"][
        "reason"
    ] == "adaptive_risk_exposure_quarantined"

    class _FlatPositionAdapter:
        @staticmethod
        def get_position_quantity(_symbol: str) -> float:
            return 0.0

    flat = live_runner._sync_adaptive_alpaca_position_lifecycle(
        sess,
        le,
        adapter=_FlatPositionAdapter(),
        expected_remaining=0,
    )
    assert flat["ok"] is True, flat
    assert flat["exposure_quarantined"] is True
    assert flat["state"].state == "exposure_quarantined"
    assert flat["state"].open_quantity_shares == 0
    assert le["adaptive_risk_alpaca_lifecycle_blocker"]["reason"] == (
        "adaptive_risk_exposure_quarantined"
    )
    assert live_runner._alpaca_entries_quarantined(sess) is True
    durable = ensured["store"].read_state(decision.reservation_id)
    assert durable.state == "exposure_quarantined"
    assert durable.open_quantity_shares == 0

    other_symbol = f"N{uuid.uuid4().hex[:3]}".upper()
    other_sess = _live_session(db, symbol=other_symbol)
    other_request = _reservation_request(
        symbol=other_symbol,
        cid=f"chili-quarantine-new-cid-{uuid.uuid4().hex[:12]}",
    )
    other_le = dict(
        other_sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC]
    )
    new_cid = live_runner._ensure_adaptive_alpaca_reservation(
        other_sess,
        other_le,
        request_payload=other_request.to_payload(),
    )
    assert new_cid["ok"] is False
    assert new_cid["error"] == "adaptive_risk_exposure_quarantined"
    assert new_cid["adaptive_risk_reconciliation_required"] is True
    assert other_le["adaptive_risk_alpaca_lifecycle_blocker"]["reason"] == (
        "adaptive_risk_exposure_quarantined"
    )
    assert live_runner._alpaca_entries_quarantined(other_sess) is True


def test_lifecycle_conflict_cannot_replace_durable_quarantine_marker(db) -> None:
    symbol = f"M{uuid.uuid4().hex[:3]}".upper()
    cid = f"chili-malformed-late-{uuid.uuid4().hex[:12]}"
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(symbol=symbol, cid=cid)
    le = dict(sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC])
    ensured = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request.to_payload(),
    )
    assert ensured["ok"] is True
    planned = int(ensured["decision"].quantity_shares)
    assert planned > 1
    ensured["store"].release_zero_fill(
        ensured["decision"].reservation_id,
        reason="pre_post_release",
    )

    malformed = live_runner._sync_adaptive_alpaca_order_lifecycle(
        sess,
        le,
        order=_order(
            request,
            oid=f"alpaca-malformed-{uuid.uuid4().hex[:10]}",
            status="filled",
            cumulative=max(1, planned // 2),
            planned=planned,
        ),
    )
    assert malformed["ok"] is False
    assert malformed["error"] == "adaptive_risk_order_lifecycle_conflict"
    durable = ensured["store"].read_state(ensured["decision"].reservation_id)
    assert durable.state == "exposure_quarantined"
    assert le["adaptive_risk_alpaca_lifecycle_blocker"]["reason"] == (
        "adaptive_risk_exposure_quarantined"
    )
    assert live_runner._alpaca_entries_quarantined(sess) is True


def test_primary_entry_refreshes_adaptive_lifecycle_before_outer_commit() -> None:
    source = inspect.getsource(live_runner.tick_live_session)
    place_at = source.index("\n        res = _governed_place(")
    refresh_at = source.index(
        "_refresh_adaptive_alpaca_lifecycle_from_session(sess, le)",
        place_at,
    )
    first_outer_branch_at = source.index(
        'if res.get("pre_place_blocked"):',
        place_at,
    )
    first_outer_commit_at = source.index("_commit_le(sess, le)", place_at)
    assert place_at < refresh_at < first_outer_branch_at < first_outer_commit_at

    prepare_source = inspect.getsource(live_runner._prepare_alpaca_place_claim)
    ensure_at = prepare_source.index("adaptive_binding = _ensure_adaptive")
    quarantine_at = prepare_source.index(
        'if adaptive_binding.get("exposure_quarantined"):',
        ensure_at,
    )
    transport_authority_at = prepare_source.index(
        'claim["_adaptive_reservation_id"]',
        ensure_at,
    )
    assert ensure_at < quarantine_at < transport_authority_at


def test_first_dip_diagnostic_tape_cannot_authorize_without_typed_receipt(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = f"T{uuid.uuid4().hex[:3]}".upper()
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(
        symbol=symbol,
        cid=f"chili-first-dip-{uuid.uuid4().hex[:12]}",
    )
    fixture = _captured_first_dip_fixture(sess, request)
    request = fixture.request

    def _diagnostic_tape_must_not_run(*_args, **_kwargs):
        raise AssertionError("mutable DB tape cannot become order authority")

    monkeypatch.setattr(
        live_runner,
        "tape_confirms_hold",
        _diagnostic_tape_must_not_run,
    )
    allowed, evidence = live_runner._final_first_dip_adaptive_confirmation(
        sess,
        request.to_payload(),
    )

    assert allowed is False
    assert evidence["reason"] == (
        "first_dip_final_typed_capture_receipt_unavailable"
    )
    assert evidence["diagnostic_tape_is_order_authority"] is False


def test_first_dip_typed_receipt_reaches_final_admission_once(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = f"P{uuid.uuid4().hex[:3]}".upper()
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(
        symbol=symbol,
        cid=f"chili-first-dip-positive-{uuid.uuid4().hex[:12]}",
    )
    fixture = _captured_first_dip_fixture(sess, request)
    request = fixture.request

    def _diagnostic_tape_must_not_run(*_args, **_kwargs):
        raise AssertionError("mutable DB tape cannot become order authority")

    monkeypatch.setattr(
        live_runner,
        "tape_confirms_hold",
        _diagnostic_tape_must_not_run,
    )
    monkeypatch.setattr(
        live_runner,
        "_utcnow_aware",
        lambda: fixture.final_proof.attested_available_at,
    )
    with (
        first_dip_tape_decision
        ._installed_captured_db_paper_first_dip_tape_decision_authority(
            fixture.final_authority
        )
    ):
        allowed, evidence = live_runner._final_first_dip_adaptive_confirmation(
            sess,
            request.to_payload(),
        )
        duplicate_allowed, duplicate = (
            live_runner._final_first_dip_adaptive_confirmation(
                sess,
                request.to_payload(),
            )
        )

    assert allowed is True
    assert evidence["reason"] == (
        "first_dip_final_admission_typed_receipt_verified"
    )
    assert evidence["reservation_authority"] is False
    assert evidence["order_authority"] is False
    assert evidence["diagnostic_tape_is_order_authority"] is False
    assert duplicate_allowed is False
    assert duplicate["reason"] == "first_dip_final_admission_already_asked"


def test_first_dip_final_admission_rejects_mismatched_run_without_fallback(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = f"M{uuid.uuid4().hex[:3]}".upper()
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(
        symbol=symbol,
        cid=f"chili-first-dip-mismatch-{uuid.uuid4().hex[:12]}",
    )
    fixture = _captured_first_dip_fixture(sess, request)
    request = fixture.request
    mismatched = replace(
        request,
        inputs=replace(
            request.inputs,
            replay_or_paper_run_id=str(uuid.uuid4()),
        ),
    )

    monkeypatch.setattr(
        live_runner,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mutable DB tape cannot become order authority")
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "_utcnow_aware",
        lambda: fixture.final_proof.attested_available_at,
    )
    with (
        first_dip_tape_decision
        ._installed_captured_db_paper_first_dip_tape_decision_authority(
            fixture.final_authority
        )
    ):
        allowed, evidence = live_runner._final_first_dip_adaptive_confirmation(
            sess,
            mismatched.to_payload(),
        )

    assert allowed is False
    assert evidence["reason"] == (
        "first_dip_final_admission_request_context_mismatch:run_id"
    )
    assert evidence["reservation_authority"] is False
    assert evidence["order_authority"] is False


def test_detector_receipt_cannot_cross_tick_into_final_admission(db) -> None:
    symbol = f"C{uuid.uuid4().hex[:3]}".upper()
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(
        symbol=symbol,
        cid=f"chili-first-dip-cross-tick-{uuid.uuid4().hex[:12]}",
    )
    fixture = _captured_first_dip_fixture(sess, request)
    request = fixture.request

    allowed, evidence = live_runner._final_first_dip_adaptive_confirmation(
        sess,
        request.to_payload(),
    )

    assert allowed is False
    assert evidence["reason"] == (
        "first_dip_final_typed_capture_receipt_unavailable"
    )


def test_unconsumed_detector_purpose_cannot_satisfy_final_admission(db) -> None:
    symbol = f"D{uuid.uuid4().hex[:3]}".upper()
    sess = _live_session(db, symbol=symbol)
    request = _reservation_request(
        symbol=symbol,
        cid=f"chili-first-dip-purpose-{uuid.uuid4().hex[:12]}",
    )
    fixture = _captured_first_dip_fixture(sess, request)
    request = fixture.request
    detector_authority = fixture.runtime.prepare_captured_first_dip_tape_authority(
        attestation=fixture.detector_proof,
        policy=fixture.policy,
        purpose=first_dip_tape_decision.FIRST_DIP_TAPE_PURPOSE_DETECTOR,
    )

    with (
        first_dip_tape_decision
        ._installed_captured_db_paper_first_dip_tape_decision_authority(
            detector_authority
        )
    ):
        allowed, evidence = live_runner._final_first_dip_adaptive_confirmation(
            sess,
            request.to_payload(),
        )

    assert allowed is False
    assert evidence["reason"] == "first_dip_final_admission_purpose_mismatch"


def test_missing_triplet_has_no_legacy_dollar_fallback() -> None:
    source = inspect.getsource(
        __import__(
            "app.services.trading.momentum_neural.alpaca_orphan_claims",
            fromlist=["_reserve_alpaca_entry_risk"],
        )._reserve_alpaca_entry_risk
    )
    assert "adaptive_risk_request_packet_claim_required" in source
    assert "50.0" not in source
    governed = inspect.getsource(live_runner._governed_place)
    assert "adaptive_risk_alpaca_lifecycle_not_migrated" not in governed
    assert "KEY_ADAPTIVE_RISK_RESERVATION_REQUEST" in governed


def test_legacy_fifty_dollar_literals_are_unreachable_on_adaptive_primary() -> None:
    source = inspect.getsource(live_runner.tick_live_session)
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    lines = source.splitlines()
    legacy_literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value == 50.0
        and (
            "chili_momentum_risk_max_loss_per_trade_usd"
            in lines[node.lineno - 1]
            or "_ovn_irreducible" in lines[node.lineno - 1]
        )
    ]
    assert legacy_literals
    for literal in legacy_literals:
        cursor: ast.AST | None = literal
        guarded = False
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(cursor, ast.If) and (
                "_adaptive_primary_build is None" in ast.unparse(cursor.test)
            ):
                guarded = True
                break
        assert guarded, f"legacy literal at tick line {literal.lineno} is unguarded"


def test_generic_alpaca_entry_cancel_requires_durable_exact_broker_oid() -> None:
    source = inspect.getsource(
        live_runner._cancel_exact_owned_alpaca_entry_order
    )
    assert 'str(claim.get("broker_order_id") or "").strip() == oid' in source
    assert 'str(claim.get("broker_order_id") or oid)' not in source
    assert "_alpaca_claim_order_matches(post, claim, request)" in source
    assert "_sync_adaptive_alpaca_order_lifecycle" in source
