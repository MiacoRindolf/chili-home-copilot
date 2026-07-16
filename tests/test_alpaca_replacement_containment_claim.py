from __future__ import annotations

import uuid

from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    activate_deadman_replacement_containment_committed,
    advance_owner_transport,
    lease_owner_transport,
    prepare_deadman_replacement_containment_committed,
    read_action_claim,
)
from tests.test_momentum_emergency_exit_recovery import (
    TEST_ALPACA_ACCOUNT_ID,
    _seed_session,
)


def _request(*, symbol: str, cid: str, quantity: float, kind: str) -> dict:
    common = {
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "product_id": symbol,
        "side": "sell",
        "base_size": str(float(quantity)),
        "client_order_id": cid,
        "position_intent": "sell_to_close",
        "extended_hours": False,
    }
    if kind == "deadman":
        return {
            **common,
            "order_type": "stop",
            "time_in_force": "gtc",
            "stop_price": 7.5,
        }
    return {
        **common,
        "order_type": "market",
        "time_in_force": "day",
        "limit_price": None,
    }


def _seed_submitted_deadman(db):
    symbol = f"RC{uuid.uuid4().hex[:5].upper()}"
    session = _seed_session(db, symbol=symbol, quantity=10.0)
    claim_token = f"replacement-owner-{uuid.uuid4().hex}"
    snapshot = dict(session.risk_snapshot_json or {})
    snapshot["alpaca_symbol_claim_token"] = claim_token
    session.risk_snapshot_json = snapshot
    db.add(session)
    db.commit()

    entry_cid = f"entry-{uuid.uuid4().hex[:12]}"
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=claim_token,
        owner_session_id=int(session.id),
        client_order_id=entry_cid,
        metadata={
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "order_request": {
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
                "product_id": symbol,
                "side": "buy",
                "base_size": "10.0",
                "client_order_id": entry_cid,
            },
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"] is True

    predecessor_cid = f"chili_dm_{int(session.id)}_1_focused"
    predecessor_oid = f"old-{uuid.uuid4().hex[:12]}"
    predecessor_request = _request(
        symbol=symbol,
        cid=predecessor_cid,
        quantity=10.0,
        kind="deadman",
    )
    lease_token = f"lease-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db,
        symbol=symbol,
        claim_token=claim_token,
        owner_session_id=int(session.id),
        account_scope="alpaca:paper",
        alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
        transport_kind="deadman",
        client_order_id=predecessor_cid,
        order_request=predecessor_request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        symbol=symbol,
        claim_token=claim_token,
        owner_session_id=int(session.id),
        account_scope="alpaca:paper",
        alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
        client_order_id=predecessor_cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=predecessor_oid,
    )
    db.commit()
    return session, claim_token, predecessor_cid, predecessor_oid, predecessor_request


def test_replacement_containment_exact_activation_replays_but_changed_truth_rejects(db):
    (
        session,
        claim_token,
        predecessor_cid,
        predecessor_oid,
        predecessor_request,
    ) = _seed_submitted_deadman(db)
    successor_cid = f"successor-{uuid.uuid4().hex[:12]}"
    successor_oid = f"successor-{uuid.uuid4().hex[:12]}"
    successor_request = _request(
        symbol=session.symbol,
        cid=successor_cid,
        quantity=10.0,
        kind="deadman",
    )
    close_intent = _request(
        symbol=session.symbol,
        cid=f"contain-{uuid.uuid4().hex[:12]}",
        quantity=10.0,
        kind="exit",
    )
    context = {
        "symbol": session.symbol,
        "claim_token": claim_token,
        "owner_session_id": int(session.id),
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
    }
    prepared = prepare_deadman_replacement_containment_committed(
        **context,
        predecessor_client_order_id=predecessor_cid,
        predecessor_broker_order_id=predecessor_oid,
        predecessor_order_request=predecessor_request,
        predecessor_reported_filled_size=0.0,
        successor_client_order_id=successor_cid,
        successor_broker_order_id=successor_oid,
        successor_order_request=successor_request,
        successor_broker_status="open",
        successor_broker_lifecycle="partially_filled",
        successor_reported_filled_size=2.0,
        close_intent=close_intent,
    )
    assert prepared["ok"] is True
    assert prepared["containment"]["state"] == "prepared"
    assert prepared["handoff"]["phase"] == "replacement_lineage_containment_prepared"
    assert prepared["handoff"]["successor_order_request"] is None

    activation = {
        **context,
        "containment_id": prepared["containment"]["containment_id"],
        "predecessor_broker_lifecycle": "replaced",
        "successor_broker_status": "canceled",
        "successor_broker_lifecycle": "canceled",
        "predecessor_reported_filled_size": 0.0,
        "successor_reported_filled_size": 2.0,
        "broker_remaining_quantity": 8.0,
    }
    activated = activate_deadman_replacement_containment_committed(**activation)
    assert activated["ok"] is True
    assert activated["broker_flat"] is False
    assert activated["handoff"]["phase"] == "successor_ready"
    assert float(activated["handoff"]["successor_order_request"]["base_size"]) == 8.0

    exact_replay = activate_deadman_replacement_containment_committed(**activation)
    assert exact_replay["ok"] is True
    assert exact_replay["reused"] is True

    changed_truth = activate_deadman_replacement_containment_committed(
        **{**activation, "broker_remaining_quantity": 7.0}
    )
    assert changed_truth == {
        "ok": False,
        "reason": "replacement_containment_replay_truth_mismatch",
    }

    db.rollback()
    readable, claim = read_action_claim(
        db,
        symbol=session.symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    metadata = claim["metadata"]
    assert metadata["replacement_lineage_containment"]["state"] == "successor_ready"
    quarantines = metadata["protective_attribution_quarantine_ledger"]
    assert len(quarantines) == 1
    assert quarantines[0]["broker_remaining_quantity"] == 8.0
    assert metadata["owner_transport"]["broker_order_status"] == "replaced"
    assert metadata["owner_transport"]["fill_attribution_quarantined"] is True
