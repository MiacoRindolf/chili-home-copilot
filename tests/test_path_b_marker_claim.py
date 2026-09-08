"""PATH B — the durable claim-phase marker (docs/DESIGN/PARTIAL_EXIT_PATH_B.md §3.1).

A partial exit on Alpaca cannot rest while the full-qty deadman stop consumes
``qty_available``, so PATH B shrinks the resting stop from Q to R = Q - f and then
sells f. The process can die between the PATCH and the sell — the 2026-09-01
live-lane host death is exactly that shape — so every step is durable on the
action claim and every phase advance is a compare-and-swap.

This is the DB-backed acceptance test the design asks for as unblocker #4: the
CAS must survive a rollback between the write and the commit, and two pulses
racing the same edge must not both advance it.

Runnable: pytest tests/test_path_b_marker_claim.py -v   (needs TEST_DATABASE_URL)
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import path_b_partial as pb
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    advance_deadman_qty_replacement,
    book_deadman_qty_replacement_partial_fill,
    clear_deadman_qty_replacement,
    open_deadman_qty_replacement,
    read_deadman_qty_replacement,
)

ACCOUNT = "test-alpaca-account"
SCOPE = "alpaca:paper"
Q, F, R = 355.0, 106.0, 249.0
STOP_PX = 3.91


def _request(cid: str, symbol: str, qty: float) -> dict:
    return {
        "alpaca_account_id": ACCOUNT,
        "account_scope": SCOPE,
        "product_id": symbol,
        "side": "sell",
        "order_type": "stop",
        "base_size": qty,
        "stop_price": STOP_PX,
        "time_in_force": "gtc",
        "position_intent": "sell_to_close",
        "extended_hours": False,
        "client_order_id": cid,
    }


@pytest.fixture()
def owner(db):
    """One session + claim + resting deadman transport, exactly as the lane leaves it."""
    symbol = f"PB{uuid.uuid4().hex[:6].upper()}"
    variant = MomentumStrategyVariant(
        family="path_b", variant_key=f"pb_{uuid.uuid4().hex[:8]}",
        label="path b marker test", params_json={},
    )
    db.add(variant)
    db.flush()
    sess = TradingAutomationSession(
        symbol=symbol, mode="live", state="live_entered", execution_family="alpaca_spot",
        variant_id=variant.id,
    )
    token = f"owner-{uuid.uuid4().hex}"
    sess.risk_snapshot_json = {"alpaca_symbol_claim_token": token}
    db.add(sess)
    db.commit()
    pred_cid = f"chili-deadman-{uuid.uuid4().hex[:10]}"
    acquired = acquire_action_claim(
        db, symbol=symbol, action="entry", claim_token=token,
        owner_session_id=int(sess.id), client_order_id=f"entry-{uuid.uuid4().hex[:10]}",
        metadata={"alpaca_account_id": ACCOUNT,
                  "order_request": {"alpaca_account_id": ACCOUNT, "product_id": symbol,
                                    "side": "buy", "base_size": str(Q)}},
        account_scope=SCOPE,
    )
    assert acquired["ok"] is True
    db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = metadata_json || CAST(:m AS jsonb)"
        " WHERE account_scope = :s AND symbol = :sym"
    ), {"m": __import__("json").dumps({"owner_transport": {
        "identity_contract": "alpaca_owner_transport_v1", "transport_kind": "deadman",
        "client_order_id": pred_cid, "broker_order_id": "alpaca-oid-pred",
        "order_request": _request(pred_cid, symbol, Q), "phase": "active"}}),
        "s": SCOPE, "sym": symbol})
    db.commit()
    succ_cid = f"chili-deadman-{uuid.uuid4().hex[:10]}"
    return {
        "db": db, "symbol": symbol, "token": token, "session_id": int(sess.id),
        "pred_cid": pred_cid, "succ_cid": succ_cid,
        "open_kwargs": dict(
            symbol=symbol, claim_token=token, owner_session_id=int(sess.id),
            account_scope=SCOPE, reason="first_target_partial",
            partial_quantity=F, partial_client_order_id=f"chili-part-{uuid.uuid4().hex[:8]}",
            predecessor_client_order_id=pred_cid, predecessor_broker_order_id="alpaca-oid-pred",
            predecessor_order_request=_request(pred_cid, symbol, Q),
            predecessor_filled_size=0.0, successor_client_order_id=succ_cid,
            successor_qty=R, successor_order_request=_request(succ_cid, symbol, R),
        ),
    }


def test_the_marker_opens_at_intent_frozen_and_is_idempotent(owner):
    db = owner["db"]
    out = open_deadman_qty_replacement(db, **owner["open_kwargs"])
    assert out["ok"] is True and out.get("created") is True
    db.commit()
    m = out["marker"]
    assert m["phase"] == "intent_frozen" and m["partial_quantity"] == F
    assert m["edges"][0]["successor_qty"] == R and m["partial_cum_filled"] == 0.0
    # a retry after a crash between commit and HTTP resumes the SAME edge
    again = open_deadman_qty_replacement(db, **owner["open_kwargs"])
    assert again["ok"] is True and again.get("reused") is True
    # a DIFFERENT partial size against the same predecessor is refused
    other = dict(owner["open_kwargs"]); other["partial_quantity"] = 50.0
    assert open_deadman_qty_replacement(db, **other) == {
        "ok": False, "reason": "path_b_marker_generation_mismatch", "marker": again["marker"]}


def test_a_partially_filled_predecessor_is_never_split(owner):
    kw = dict(owner["open_kwargs"]); kw["predecessor_filled_size"] = 12.0
    assert open_deadman_qty_replacement(owner["db"], **kw)["ok"] is False


def test_the_phase_advance_is_a_cas_and_only_legal_edges_pass(owner):
    db = owner["db"]
    assert open_deadman_qty_replacement(db, **owner["open_kwargs"])["ok"] is True
    db.commit()
    common = dict(symbol=owner["symbol"], claim_token=owner["token"], account_scope=SCOPE)
    # an edge the design deleted is refused by the pure core's table, not by SQL
    bad = advance_deadman_qty_replacement(db, **common, expect_phase="intent_frozen", phase="partial_posted")
    assert bad["ok"] is False and bad["reason"].startswith("path_b_marker_illegal_edge")
    ok = advance_deadman_qty_replacement(
        db, **common, expect_phase="intent_frozen", phase="replace_submitted",
        edge_patch={"submitted_at_utc": "2026-09-06T19:00:00+00:00", "submit_outcome": "accepted"},
    )
    assert ok["ok"] is True and ok["marker"]["phase"] == "replace_submitted"
    assert ok["marker"]["edges"][0]["submit_outcome"] == "accepted"
    db.commit()
    # the SECOND pulse racing the same edge loses the CAS — it does not re-advance
    lost = advance_deadman_qty_replacement(db, **common, expect_phase="intent_frozen", phase="replace_submitted")
    assert lost["ok"] is False and lost["reason"] == "path_b_marker_phase_moved"


def test_the_cas_survives_a_rollback_between_write_and_commit(owner):
    """Unblocker #4 (H2): a rollback after the UPDATE must leave the OLD phase."""
    db = owner["db"]
    assert open_deadman_qty_replacement(db, **owner["open_kwargs"])["ok"] is True
    db.commit()
    common = dict(symbol=owner["symbol"], claim_token=owner["token"], account_scope=SCOPE)
    assert advance_deadman_qty_replacement(
        db, **common, expect_phase="intent_frozen", phase="replace_submitted")["ok"] is True
    db.rollback()
    readable, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    assert readable is True and marker is not None
    assert marker["phase"] == "intent_frozen", "a rolled-back advance must not be visible"
    # and the same edge is still takeable afterwards
    assert advance_deadman_qty_replacement(
        db, **common, expect_phase="intent_frozen", phase="replace_submitted")["ok"] is True
    db.commit()


def test_the_sibling_fill_is_booked_and_self_heals_but_never_un_books(owner):
    db = owner["db"]
    assert open_deadman_qty_replacement(db, **owner["open_kwargs"])["ok"] is True
    db.commit()
    common = dict(symbol=owner["symbol"], claim_token=owner["token"], account_scope=SCOPE)
    out = book_deadman_qty_replacement_partial_fill(db, **common, partial_cum_filled=40.0,
                                                    partial_broker_order_id="alpaca-oid-part",
                                                    partial_state="partially_filled")
    assert out["ok"] is True and out["partial_cum_filled"] == 40.0
    db.commit()
    # a stale SMALLER read never un-books a booked share
    assert book_deadman_qty_replacement_partial_fill(
        db, **common, partial_cum_filled=0.0)["partial_cum_filled"] == 40.0
    # more than f is impossible and fails closed
    assert book_deadman_qty_replacement_partial_fill(
        db, **common, partial_cum_filled=F + 1.0)["ok"] is False
    # conservation, with f and k SEPARATE (the H2 property the pure core proves)
    assert pb.conservation_holds(
        broker_qty=Q - 40.0, successor_qty=R, partial_qty=F, partial_cum_filled=40.0) is True
    assert pb.conservation_holds(
        broker_qty=Q - 40.0, successor_qty=R, partial_qty=F, partial_cum_filled=0.0) is False


def test_only_a_terminal_marker_can_be_cleared(owner):
    db = owner["db"]
    assert open_deadman_qty_replacement(db, **owner["open_kwargs"])["ok"] is True
    db.commit()
    common = dict(symbol=owner["symbol"], claim_token=owner["token"], account_scope=SCOPE)
    assert clear_deadman_qty_replacement(db, **common) is False  # intent_frozen is in flight
    assert advance_deadman_qty_replacement(
        db, **common, expect_phase="intent_frozen", phase="replace_rejected")["ok"] is True
    db.commit()
    assert pb.is_terminal("replace_rejected") is True
    assert clear_deadman_qty_replacement(db, **common) is True
    db.commit()
    readable, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    assert readable is True and marker is None


def test_the_marker_is_a_sibling_of_owner_transport_not_of_the_close_handoff(owner):
    """§3.1: under ``deadman_close_handoff`` the marker would block every deadman lease."""
    db = owner["db"]
    assert open_deadman_qty_replacement(db, **owner["open_kwargs"])["ok"] is True
    db.commit()
    row = db.execute(text(
        "SELECT metadata_json FROM broker_symbol_action_claims"
        " WHERE account_scope = :s AND symbol = :sym"
    ), {"s": SCOPE, "sym": owner["symbol"]}).fetchone()
    meta = row[0] if not isinstance(row[0], str) else __import__("json").loads(row[0])
    assert "deadman_qty_replacement" in meta
    assert "deadman_close_handoff" not in meta
    assert meta["owner_transport"]["transport_kind"] == "deadman"
