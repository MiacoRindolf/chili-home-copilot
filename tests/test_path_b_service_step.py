"""PATH B is WIRED: the service step that owns everything after the PATCH (2026-09-06).

`live_partial_exit_filled` has been ZERO on the Alpaca lane since 2026-08-01, because a
resting full-quantity deadman stop consumes the venue's `qty_available` and the partial
sell is refused. PATH B shrinks that stop from Q to R = Q - f and then sells f. Between
those two calls the f shares carry no stop, so the process dying in that window IS the
design problem -- which is why the intent is committed to the action claim BEFORE the
broker is touched, and why a marker left behind by a dead process is picked up by the
next pulse instead of waiting for a human.

Three call sites, and the placement of each is load-bearing:

  * the first-target decision -- freeze the edge durably instead of flattening the whole
    position, which is the trade this program exists to stop making;
  * ABOVE the handoff-priority block -- every exit inside that block returns from the
    tick first, so a marker serviced below it could never advance while a close handoff
    existed, and the deadlock would be permanent;
  * the head of the exit chokepoint -- a tranche posted by PATH B is known to the CLAIM,
    and `_cancel_scale_limit_and_clamp` reads the session JSON, so without the rebuild
    the clamp is a pass-through no-op and the account carries Q + f of sell authority
    against Q shares.

Runnable: pytest tests/test_path_b_service_step.py -v   (one test needs TEST_DATABASE_URL)
"""
from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy import text

from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import path_b_partial as pb
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    read_deadman_qty_replacement,
)

ACCOUNT = "test-alpaca-account"
SCOPE = "alpaca:paper"
Q, F, R = 355.0, 106.0, 249.0
STOP_PX = 3.91


# ── the wiring, proved from the source order (no DB can reach these lines) ──

def test_the_service_step_runs_above_the_handoff_priority_block():
    """Below it, every exit path returns from the tick before the marker advances."""
    src = inspect.getsource(lr.tick_live_session)
    call = src.find("_path_b_step = _service_path_b_marker(")
    assert call > 0
    # it sits INSIDE the alpaca guard that opens the block ...
    assert '== "alpaca_spot":' in src[max(0, call - 600):call]
    # ... and strictly BEFORE the durable-handoff read that opens it
    block = src.find("_read_exact_alpaca_deadman_handoff(sess)", call)
    assert call < block
    # and a pending PATCH reports the position as PROTECTED -- both orders rest
    # at the broker -- instead of letting maintenance disable the software stop
    assert '"path_b_replace_pending": True' in src[call:block]


def test_the_chokepoint_pays_its_debts_before_the_clamp():
    src = inspect.getsource(lr._submit_live_market_exit_impl)
    pre = src.find("_path_b_exit_precheck(db, sess, le=le, reason=reason)")
    clamp = src.find("_cancel_scale_limit_and_clamp(")
    assert 0 < pre < clamp


def test_the_first_target_freezes_the_edge_instead_of_flattening():
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('if str(_tranche_dbg.get("reason") or "") == "deadman_holds_tranche"')
    assert i > 0
    block = src[i:i + 3000]
    assert "_open_path_b_partial_marker(" in block
    assert '"fallback": (' in block and '"path_b_qty_replacement"' in block
    assert '"path_b_partial_pending": True' in block


def test_a_resting_path_b_tranche_reserves_its_shares_instead_of_full_closing():
    """The old `else` branch full-closed on any non-OCO resting limit -- which would
    flatten the runner moments after PATH B posted the tranche it exists to post."""
    le = {"scale_limit_order_id": "oid-1", "scale_limit_is_path_b": True,
          "scale_limit_qty": F}
    assert lr._alpaca_deadman_reserved_tranche_quantity(le) == F
    guard = inspect.getsource(lr._ensure_alpaca_deadman_stop)
    i = guard.find('if le.get("scale_limit_order_id"):')
    assert i > 0
    head = guard[i:i + 2400]
    assert 'le.get("scale_limit_is_oco") or le.get("scale_limit_is_path_b")' in head
    assert "alpaca_legacy_scale_order_conflicts_with_deadman" in head


# ── the pure parts of the step ─────────────────────────────────────────────

def test_an_indeterminate_patch_is_never_read_as_a_refusal():
    """A refusal writes `replace_rejected` and stops. Reading a timeout as a refusal
    would abandon an edge the broker actually accepted."""
    assert lr._path_b_replace_definitely_rejected({"ok": False, "http_status": 422}) is True
    assert lr._path_b_replace_definitely_rejected({"ok": False, "http_status": 404}) is True
    for unsure in ({"ok": False, "http_status": 500}, {"ok": False, "indeterminate": True},
                   {"ok": False}, {"ok": False, "http_status": 429},
                   {"ok": False, "http_status": 408}, {"ok": True}, None, "x"):
        assert lr._path_b_replace_definitely_rejected(unsure) is False, unsure


def test_the_sibling_mirror_rebuilds_the_session_view_from_the_claim():
    class _Sess:
        id = 1
        symbol = "CANF"
        live_execution_json: dict = {}
        risk_snapshot_json: dict = {}

    marker = {"partial_broker_order_id": "alpaca-part-1", "partial_client_order_id": "chili-part-a",
              "partial_quantity": F, "partial_cum_filled": 40.0, "partial_limit_price": 4.63}
    le: dict = {}
    assert lr._path_b_mirror_sibling_into_le(_Sess(), le, marker) is True
    assert le["scale_limit_order_id"] == "alpaca-part-1"
    assert le["scale_limit_qty"] == F and le["scale_limit_adopted_qty"] == 40.0
    assert le["scale_limit_is_path_b"] is True and le["scale_limit_is_oco"] is False
    # idempotent: the same order is not re-mirrored
    assert lr._path_b_mirror_sibling_into_le(_Sess(), le, marker) is False
    # and an unposted tranche is not invented
    assert lr._path_b_mirror_sibling_into_le(
        _Sess(), {}, {**marker, "partial_broker_order_id": None}) is False


def test_the_ceiling_forces_flatten_for_naked_and_abandon_for_lineage():
    """No marker may wedge a position forever, and `abandoned` is never the exit from a
    phase with uncovered shares -- that would be abandoning the POSITION."""
    assert pb.marker_ceiling_forced_target("partial_posted") == "flatten_queued"
    assert pb.marker_ceiling_forced_target("successor_certified") == "flatten_queued"
    assert pb.marker_ceiling_forced_target("intent_frozen") == "abandoned"
    assert pb.marker_ceiling_exceeded(301.0) is True
    assert pb.marker_ceiling_exceeded(None) is False  # never on a guess


def test_a_session_that_is_not_alpaca_never_touches_the_marker():
    class _Sess:
        id = 7
        symbol = "CANF"
        execution_family = "robinhood_agentic_mcp"
        risk_snapshot_json: dict = {}

    assert lr._service_path_b_marker(
        None, _Sess(), None, le={}, product_id="CANF",
        avg_entry_price=4.0, software_stop_price=3.9,
    ) is None


# ── the PATCH itself, against the replay mock ──────────────────────────────

@pytest.fixture()
def owner(db, monkeypatch):
    """A live Alpaca session with a claim, an owner transport and a resting Q stop."""
    from app.services.trading.momentum_neural.replay_mock_broker import (
        MockBrokerAdapter,
        RecordedQuote,
    )
    from datetime import datetime, timezone

    symbol = f"PB{uuid.uuid4().hex[:6].upper()}"
    variant = MomentumStrategyVariant(
        family="path_b", variant_key=f"pb_{uuid.uuid4().hex[:8]}",
        label="path b service step", params_json={},
    )
    db.add(variant)
    db.flush()
    token = f"owner-{uuid.uuid4().hex}"
    sess = TradingAutomationSession(
        symbol=symbol, mode="live", state="live_trailing",
        execution_family="alpaca_spot", variant_id=variant.id,
    )
    sess.risk_snapshot_json = {
        "alpaca_symbol_claim_token": token,
        "alpaca_account_scope": SCOPE,
        "alpaca_account_id": ACCOUNT,
    }
    db.add(sess)
    db.commit()

    broker = MockBrokerAdapter(slippage_bps=0.0, venue_rt_bps=100.0,
                               resting_limit_fills=True, freshness_mode="sim")
    broker.set_clock(datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc))
    broker.set_quote(symbol, RecordedQuote(bid=4.60, ask=4.62, last=4.61))
    assert broker.place_market_order(product_id=symbol, side="buy",
                                     base_size=str(int(Q)),
                                     client_order_id=f"entry-{uuid.uuid4().hex[:8]}")["ok"]
    pred_cid = f"chili-deadman-{uuid.uuid4().hex[:10]}"
    placed = broker.place_deadman_stop(product_id=symbol, base_size=str(int(Q)),
                                       stop_price=STOP_PX, client_order_id=pred_cid)
    assert placed["ok"] is True
    pred_request = {
        "alpaca_account_id": ACCOUNT, "account_scope": SCOPE, "product_id": symbol,
        "side": "sell", "order_type": "stop", "base_size": Q, "stop_price": STOP_PX,
        "time_in_force": "gtc", "position_intent": "sell_to_close",
        "extended_hours": False, "client_order_id": pred_cid,
    }
    assert acquire_action_claim(
        db, symbol=symbol, action="entry", claim_token=token,
        owner_session_id=int(sess.id), client_order_id=f"entry-{uuid.uuid4().hex[:10]}",
        metadata={"alpaca_account_id": ACCOUNT,
                  "order_request": {"alpaca_account_id": ACCOUNT, "product_id": symbol,
                                    "side": "buy", "base_size": str(Q)}},
        account_scope=SCOPE,
    )["ok"] is True
    db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = metadata_json || CAST(:m AS jsonb)"
        " WHERE account_scope = :s AND symbol = :sym"
    ), {"m": __import__("json").dumps({"owner_transport": {
        "identity_contract": "alpaca_owner_transport_v1", "transport_kind": "deadman",
        "client_order_id": pred_cid, "broker_order_id": placed["order_id"],
        "order_request": pred_request, "phase": "submitted"}}),
        "s": SCOPE, "sym": symbol})
    db.commit()
    return {"db": db, "sess": sess, "broker": broker, "symbol": symbol, "token": token,
            "pred_cid": pred_cid, "pred_oid": placed["order_id"]}


def test_the_first_target_freezes_the_edge_and_the_service_step_issues_the_patch(owner):
    """P0-P2 end to end: the intent is durable BEFORE the broker moves, the PATCH is
    issued once, and the successor rests for R with the marker's own cid."""
    db, sess, broker = owner["db"], owner["sess"], owner["broker"]
    le: dict = {"deadman_stop": {"qty": Q, "order_id": owner["pred_oid"],
                                 "client_order_id": owner["pred_cid"]}}

    # the tranche is NOT sellable while the full-qty stop rests -- that is the
    # measured suppression PATH B exists to remove
    sellable, dbg = lr.alpaca_partial_tranche_sellable(le, partial_qty=F, position_qty=Q)
    assert sellable is False and dbg["reason"] == "deadman_holds_tranche"

    opened = lr._open_path_b_partial_marker(
        db, sess, broker, le=le, partial_qty=F, position_qty=Q,
        reason="first_target_partial",
    )
    assert opened["ok"] is True, opened
    readable, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    assert readable and marker is not None
    assert marker["phase"] == "intent_frozen"
    edge = marker["edges"][-1]
    assert edge["successor_qty"] == R and marker["partial_quantity"] == F
    # the envelope shown to the lineage matcher carries R, not Q -- a predecessor
    # copy would fail `|Q - R| > tol` on every pulse, forever
    assert edge["successor_order_request"]["base_size"] == R
    assert edge["successor_order_request"]["stop_price"] == STOP_PX

    stepped = lr._service_path_b_marker(
        db, sess, broker, le=le, product_id=owner["symbol"],
        avg_entry_price=4.30, software_stop_price=STOP_PX,
    )
    assert stepped is not None and stepped["phase"] == "replace_submitted", stepped
    readable, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    assert marker["phase"] == "replace_submitted"
    assert marker["edges"][-1]["submit_outcome"] == "accepted"

    predecessor, _ = broker.get_order(owner["pred_oid"])
    assert lr._alpaca_protective_order_lifecycle(predecessor) == "replaced"
    successor, _ = broker.get_order(predecessor.raw["replaced_by"])
    assert successor.raw["qty"] == R
    assert successor.client_order_id == marker["edges"][-1]["successor_client_order_id"]
    assert lr._alpaca_protective_order_is_certifiably_active(successor) is True


def test_a_crash_between_the_patch_and_the_phase_write_is_recovered_not_repeated(owner):
    """The edge HAPPENED. Re-submitting it would be refused as a duplicate cid and then
    mis-read as a rejection, abandoning a lineage the broker actually granted."""
    db, sess, broker = owner["db"], owner["sess"], owner["broker"]
    le: dict = {"deadman_stop": {"qty": Q, "order_id": owner["pred_oid"]}}
    assert lr._open_path_b_partial_marker(
        db, sess, broker, le=le, partial_qty=F, position_qty=Q,
        reason="first_target_partial")["ok"] is True
    _, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    succ_cid = marker["edges"][-1]["successor_client_order_id"]
    # the PATCH lands at the venue, then the process dies before the phase write
    assert broker.replace_order_qty(order_id=owner["pred_oid"], new_qty=str(int(R)),
                                    client_order_id=succ_cid)["ok"] is True
    stepped = lr._service_path_b_marker(
        db, sess, broker, le=le, product_id=owner["symbol"],
        avg_entry_price=4.30, software_stop_price=STOP_PX,
    )
    assert stepped["phase"] == "replace_submitted"
    _, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    assert marker["edges"][-1]["submit_outcome"] == "recovered_from_broker"
    # exactly ONE successor exists for this lineage
    predecessor, _ = broker.get_order(owner["pred_oid"])
    successor, _ = broker.get_order(predecessor.raw["replaced_by"])
    assert successor.client_order_id == succ_cid and successor.raw["qty"] == R


def test_a_partially_filled_stop_is_refused_before_the_broker_is_touched(owner):
    """A stop that has already sold part of the position must never be split: the PATCH
    would re-authorise shares that are gone."""
    db, sess, broker = owner["db"], owner["sess"], owner["broker"]
    le: dict = {"deadman_stop": {"qty": Q, "order_id": owner["pred_oid"]}}
    # f >= Q leaves no runner -- that is a whole exit, not a partial
    refused = lr._open_path_b_partial_marker(
        db, sess, broker, le=le, partial_qty=Q, position_qty=Q,
        reason="first_target_partial")
    assert refused["ok"] is False
    assert refused["reason"].startswith("path_b_split_refused:")
    readable, marker = read_deadman_qty_replacement(db, symbol=owner["symbol"], account_scope=SCOPE)
    assert readable is True and marker is None
