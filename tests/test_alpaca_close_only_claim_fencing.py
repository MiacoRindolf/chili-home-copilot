from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    CLAIMED,
    SUBMIT_INDETERMINATE,
    SUBMITTED,
    acquire_action_claim,
    advance_orphan_close_claim_phase,
    bind_orphan_close_request,
    mark_orphan_close_transport_started,
    read_action_claim,
    release_orphan_close_pre_post,
    update_action_claim_phase,
)


ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"


def _identity(prefix: str) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:10].upper()
    symbol = f"{prefix}{suffix[:4]}"
    return symbol, f"close-{suffix}", f"claim-{suffix}"


def _request(*, symbol: str, cid: str, qty: float = 7.0) -> dict:
    return {
        "account_scope": "alpaca:paper",
        "alpaca_account_id": ACCOUNT_ID,
        "product_id": symbol,
        "side": "sell",
        "base_size": str(float(qty)),
        "client_order_id": cid,
        "position_intent": "sell_to_close",
        "order_type": "market",
        "time_in_force": "day",
        "extended_hours": False,
        "limit_price": None,
    }


def _seed_close_only_claim(db, *, symbol: str, cid: str, claim_token: str) -> None:
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="orphan_flatten",
        claim_token=claim_token,
        owner_session_id=123,
        client_order_id=cid,
        metadata={
            "runner_emergency_close_only": True,
            "owner_session_id": 123,
            "max_close_qty": 7.0,
            "broker_position_qty_at_recertification": 7.0,
            "broker_unattributed_quantity_floor": 0.0,
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"] is True
    db.commit()


def _expire_transport_lease(db, *, symbol: str) -> None:
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    metadata = dict(claim["metadata"])
    metadata["close_transport_lease_expires_at_utc"] = "2000-01-01T00:00:00+00:00"
    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET metadata_json = CAST(:metadata AS jsonb), updated_at = NOW() "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {
            "metadata": json.dumps(metadata, separators=(",", ":")),
            "symbol": symbol,
        },
    )
    db.commit()


def test_pre_post_release_recycles_exact_authority_without_resolving_claim(db):
    symbol, cid, claim_token = _identity("RC")
    request = _request(symbol=symbol, cid=cid)
    _seed_close_only_claim(db, symbol=symbol, cid=cid, claim_token=claim_token)

    first = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="worker-one",
        account_scope="alpaca:paper",
    )
    assert first["ok"] is True
    assert first["transport_generation"] == 1
    db.commit()

    assert release_orphan_close_pre_post(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="worker-one",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        reason="literal_bbo_unavailable",
        account_scope="alpaca:paper",
    )
    db.commit()

    readable, retained = read_action_claim(
        db, symbol=symbol, account_scope="alpaca:paper"
    )
    assert readable and retained is not None
    assert retained["phase"] == CLAIMED
    assert retained["resolved_at"] is None
    assert retained["client_order_id"] == cid
    assert retained["metadata"]["close_request"] == request
    assert retained["metadata"]["close_transport_state"] == "recyclable_no_transport"

    same_token = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="worker-one",
        account_scope="alpaca:paper",
    )
    assert same_token["ok"] is False
    assert same_token["reason"] == "orphan_close_new_generation_token_required"

    second = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="worker-two",
        account_scope="alpaca:paper",
    )
    assert second["ok"] is True
    assert second["recycled_no_transport"] is True
    assert second["transport_generation"] == 2
    db.commit()

    _expire_transport_lease(db, symbol=symbol)
    assert not mark_orphan_close_transport_started(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="worker-two",
        transport_generation=2,
        expected_claim_phase=CLAIMED,
        account_scope="alpaca:paper",
    )
    third = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="worker-three",
        account_scope="alpaca:paper",
    )
    assert third["ok"] is True
    assert third["transport_generation"] == 3
    assert third["same_cid_replay"] is False
    db.commit()

    changed = {**request, "base_size": "6.0"}
    mismatch = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=changed,
        post_bind_token="worker-four",
        account_scope="alpaca:paper",
    )
    assert mismatch["ok"] is False
    assert mismatch["reason"] == "orphan_close_bind_generation_mismatch"


def test_started_generation_replays_only_same_request_after_expiry_and_strict_absence(db):
    symbol, cid, claim_token = _identity("RP")
    request = _request(symbol=symbol, cid=cid)
    _seed_close_only_claim(db, symbol=symbol, cid=cid, claim_token=claim_token)
    first = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="paused-worker",
        account_scope="alpaca:paper",
    )
    assert first["ok"] is True
    db.commit()
    assert mark_orphan_close_transport_started(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="paused-worker",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        account_scope="alpaca:paper",
    )
    db.commit()
    _expire_transport_lease(db, symbol=symbol)

    without_strict_absence = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="recovery-worker",
        account_scope="alpaca:paper",
    )
    assert without_strict_absence["ok"] is False
    assert without_strict_absence["reason"] == "orphan_close_transport_reconcile_required"

    replay = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="recovery-worker",
        strict_cid_absent_after_expiry=True,
        account_scope="alpaca:paper",
    )
    assert replay["ok"] is True
    assert replay["same_cid_replay"] is True
    assert replay["transport_generation"] == 2
    assert replay["post_bind_token"] == "recovery-worker"
    db.commit()

    assert not mark_orphan_close_transport_started(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="paused-worker",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        account_scope="alpaca:paper",
    )
    assert mark_orphan_close_transport_started(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="recovery-worker",
        transport_generation=2,
        expected_claim_phase=CLAIMED,
        account_scope="alpaca:paper",
    )
    db.commit()


def test_close_only_phase_cas_rejects_stale_generation_and_records_exact_submit(db):
    symbol, cid, claim_token = _identity("CS")
    request = _request(symbol=symbol, cid=cid)
    _seed_close_only_claim(db, symbol=symbol, cid=cid, claim_token=claim_token)
    leased = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="active-worker",
        account_scope="alpaca:paper",
    )
    assert leased["ok"] is True
    db.commit()
    assert mark_orphan_close_transport_started(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="active-worker",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        account_scope="alpaca:paper",
    )
    db.commit()

    assert not advance_orphan_close_claim_phase(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="stale-worker",
        transport_generation=0,
        expected_claim_phase=CLAIMED,
        phase=SUBMITTED,
        broker_order_id="wrong-oid",
        account_scope="alpaca:paper",
    )
    assert advance_orphan_close_claim_phase(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="active-worker",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        phase=SUBMITTED,
        broker_order_id="exact-oid",
        metadata={"submit_status": "accepted"},
        account_scope="alpaca:paper",
    )
    db.commit()

    readable, submitted = read_action_claim(
        db, symbol=symbol, account_scope="alpaca:paper"
    )
    assert readable and submitted is not None
    assert submitted["phase"] == SUBMITTED
    assert submitted["broker_order_id"] == "exact-oid"
    assert submitted["metadata"]["close_transport_state"] == SUBMITTED


def test_resolved_claim_cannot_be_resurrected_by_generic_or_close_only_worker(db):
    symbol, cid, claim_token = _identity("RS")
    request = _request(symbol=symbol, cid=cid)
    _seed_close_only_claim(db, symbol=symbol, cid=cid, claim_token=claim_token)
    leased = bind_orphan_close_request(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="paused-worker",
        account_scope="alpaca:paper",
    )
    assert leased["ok"] is True
    db.commit()
    assert mark_orphan_close_transport_started(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="paused-worker",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        account_scope="alpaca:paper",
    )
    db.commit()

    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET phase = 'resolved', resolved_at = NOW(), updated_at = NOW() "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": symbol},
    )
    db.commit()

    assert not advance_orphan_close_claim_phase(
        db,
        symbol=symbol,
        claim_token=claim_token,
        client_order_id=cid,
        close_request=request,
        post_bind_token="paused-worker",
        transport_generation=1,
        expected_claim_phase=CLAIMED,
        phase=SUBMIT_INDETERMINATE,
        broker_order_id=None,
        account_scope="alpaca:paper",
    )
    assert not update_action_claim_phase(
        db,
        symbol=symbol,
        claim_token=claim_token,
        phase=SUBMIT_INDETERMINATE,
        client_order_id=cid,
        broker_order_id=None,
        account_scope="alpaca:paper",
    )
    db.commit()

    readable, resolved = read_action_claim(
        db, symbol=symbol, account_scope="alpaca:paper"
    )
    assert readable and resolved is not None
    assert resolved["phase"] == "resolved"
