"""[9] Ang naka-freeze na exit LIMIT ay muling pinepresyuhan BAGO ang cancel, at ang
broker-inert na handoff ay hindi nabubuhay nang mas matagal sa stop na hindi makaupo
(2026-09-11).

ANG DEPEKTO, SINUKAT SA LIVE (30 araw, read-only):
  * 108 freeze (``deadman_successor_intent_frozen_for_next_pulse``, 57 session);
    freeze -> fill/block p50 12.95 s, p90 22.28 s (n=104) -- ang tagal na inuulit
    nang verbatim ang limit na minted sa phase 1.
  * 2 ``frozen_exit_limit_not_marketable_at_literal_post``: BJDX 20293 09-08
    (frozen 0.948 vs bid 0.907) at LBGJ 22135 09-11 (frozen 2.76 vs bid 2.68).
  * BJDX: block -> re-arm TINANGGIHAN (42210000 "stop price must be less than current
    price", stop 0.9744 > market 0.91465) -> 2,533
    ``deadman_close_final_request_not_certified`` + 1,176
    ``deadman_close_handoff_identity_mismatch``, 10:39:49 -> 14:40:19 (4 oras na hubad).
    COIW 14842 08-21: parehong hugis, 802 identity_mismatch hanggang 20:00.

ANG UGAT: ``_apply_frozen_successor`` (phase 2) ay pinapatungan ang sariwang limit ng
pulse ng presyong minted sa phase 1, at ang ``finalize`` ay nilolock ang ``limit_price``.
Ang literal guard ay TAMA. Ang ayos ay dalawang slice:

  1. RE-PRICE BAGO ANG CANCEL -- ``supersede_unsent_deadman_close_handoff_for_price``
     (parehong mga gate ng verb supersession + limit/sell_to_close + mas marketable
     lamang) at muling pag-freeze ng PAREHONG close sa rung ng pulse na ito.
  2. ANG AFTERMATH -- ``retire_deadman_close_handoff`` mula sa
     ``successor_proven_no_transport`` / ``replacement_deadman_proven_no_transport``
     kapag walang deadman na nakaupo, para ang close ay mag-mint ng sariling bagong
     identity sa halip na ``not_certified``/``identity_mismatch`` magpakailanman.

Runnable: pytest tests/test_deadman_close_handoff_price_supersession.py -v
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    advance_owner_transport,
    finalize_deadman_close_handoff_request,
    lease_deadman_handoff_replacement,
    lease_owner_transport,
    prepare_deadman_close_handoff,
    read_action_claim,
    release_owner_transport_pre_post,
    resolve_owner_transport_terminal,
    retire_deadman_close_handoff,
    supersede_unsent_deadman_close_handoff_for_price,
)
from app.services.trading.venue.protocol import NormalizedTicker

from tests.test_alpaca_deadman_close_handoff import _request, _seed_owner
from tests.test_momentum_emergency_exit_recovery import (
    TEST_ALPACA_ACCOUNT_ID,
    _ScriptedAlpaca,
    _fresh,
    _order,
    _run_tick,
)

FREEZE_ERROR = "deadman_successor_intent_frozen_for_next_pulse"


def _limit_request(*, symbol: str, cid: str, qty: float, limit_price: str) -> dict:
    """The shape the LIMIT-close seam freezes (a ladder rung / bailout)."""
    return {
        **_request(symbol=symbol, cid=cid, qty=qty, kind="exit"),
        "order_type": "limit",
        "time_in_force": "gtc",
        "limit_price": limit_price,
    }


def _freeze(db, *, symbol: str, qty: float = 400.0, limit_price: str | None = "2.76"):
    """A live deadman watching the position + one frozen, NEVER-transmitted close
    (limit at ``limit_price``; ``None`` = a market close). The LBGJ 22135 phase-1 state."""
    sess, context = _seed_owner(db, symbol=symbol, quantity=qty)
    deadman_cid = f"dm-{uuid.uuid4().hex[:12]}"
    deadman_oid = f"dm-oid-{uuid.uuid4().hex[:10]}"
    deadman_request = _request(symbol=symbol, cid=deadman_cid, qty=qty, kind="deadman")
    lease_token = f"dm-worker-{uuid.uuid4().hex}"
    assert lease_owner_transport(
        db, **context, transport_kind="deadman", client_order_id=deadman_cid,
        order_request=deadman_request, lease_token=lease_token,
    )["ok"] is True
    assert advance_owner_transport(
        db, **context, client_order_id=deadman_cid, lease_token=lease_token,
        phase="submitted", broker_order_id=deadman_oid,
    )
    successor_cid = f"close-{uuid.uuid4().hex[:12]}"
    successor = (
        _limit_request(symbol=symbol, cid=successor_cid, qty=qty, limit_price=limit_price)
        if limit_price is not None
        else _request(symbol=symbol, cid=successor_cid, qty=qty, kind="exit")
    )
    handoff_token = f"handoff-{uuid.uuid4().hex}"
    prepared = prepare_deadman_close_handoff(
        db, **context, handoff_token=handoff_token,
        deadman_client_order_id=deadman_cid, deadman_broker_order_id=deadman_oid,
        deadman_order_request=deadman_request, successor_transport_kind="ordinary_exit",
        successor_intent=successor, reason="tape_accel_rollover",
    )
    assert prepared["ok"] is True, prepared
    db.commit()
    return sess, context, {
        "handoff_token": handoff_token,
        "deadman_cid": deadman_cid,
        "deadman_oid": deadman_oid,
        "deadman_request": deadman_request,
        "successor": successor,
        "qty": qty,
    }


def _claim(db, symbol: str) -> tuple[dict | None, dict]:
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    metadata = dict(claim["metadata"] or {})
    return metadata.get("deadman_close_handoff"), metadata


def _supersede(
    db,
    context,
    meta,
    *,
    frozen: Any = "2.76",
    new: Any = "2.69",
    handoff_token: str | None = None,
    claim_override: dict | None = None,
):
    return supersede_unsent_deadman_close_handoff_for_price(
        db, **{**context, **(claim_override or {})},
        handoff_token=handoff_token or meta["handoff_token"],
        frozen_limit_price=frozen, superseding_limit_price=new, fresh_bid=2.73,
    )


# ── 1. the price supersession primitive ────────────────────────────────────────────


def test_a_stale_frozen_limit_is_superseded_while_the_deadman_still_watches(db):
    symbol = f"PRS{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)

    assert _supersede(db, context, meta) is True
    db.commit()

    handoff, metadata = _claim(db, symbol)
    assert handoff is None, "the frozen envelope is gone"
    last = (metadata.get("deadman_close_handoff_history") or [])[-1]
    assert last["phase"] == "superseded"
    assert last["supersession_reason"] == "successor_limit_not_marketable"
    assert last["superseded_frozen_limit_price"] == "2.76"
    assert last["superseded_by_limit_price"] == "2.69"
    assert last["superseded_fresh_bid"] == 2.73
    assert last["handoff_token"] == meta["handoff_token"]
    # the deadman was never touched: it is still the unresolved owner transport
    current = metadata["owner_transport"]
    assert current["transport_kind"] == "deadman"
    assert current["client_order_id"] == meta["deadman_cid"]
    assert current["phase"] != "resolved"


def test_the_same_close_refreezes_at_the_fresh_price_with_the_same_cid(db):
    """The CID was never transmitted (proven by the gates), so the SAME close identity
    is re-frozen with the fresh price only; prepare checks the current handoff, not CIDs."""
    symbol = f"PRF{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert _supersede(db, context, meta) is True
    db.commit()

    refrozen = prepare_deadman_close_handoff(
        db, **context, handoff_token=f"handoff-{uuid.uuid4().hex}",
        deadman_client_order_id=meta["deadman_cid"], deadman_broker_order_id=meta["deadman_oid"],
        deadman_order_request=meta["deadman_request"], successor_transport_kind="ordinary_exit",
        successor_intent={**meta["successor"], "limit_price": "2.69"}, reason="tape_accel_rollover",
    )
    assert refrozen["ok"] is True, refrozen
    assert refrozen["handoff"]["phase"] == "intent_frozen"
    assert refrozen["handoff"]["successor_intent"]["limit_price"] == "2.69"
    assert refrozen["handoff"]["successor_client_order_id"] == meta["successor"]["client_order_id"]


@pytest.mark.parametrize("new", ["2.76", "2.7600000001", "2.80"])
def test_price_supersession_never_raises_or_repeats_a_sell_limit(db, new):
    symbol = f"PRU{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert _supersede(db, context, meta, new=new) is False
    handoff, _m = _claim(db, symbol)
    assert handoff is not None and handoff["successor_intent"]["limit_price"] == "2.76"


@pytest.mark.parametrize("new", [None, "nan", "0", "-1", "inf"])
def test_price_supersession_refuses_a_non_price(db, new):
    symbol = f"PRN{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert _supersede(db, context, meta, new=new) is False


def test_refuses_once_a_successor_request_was_finalized(db):
    """Once finalized, a CID may have crossed to the broker: never supersede there."""
    symbol = f"PRT{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert resolve_owner_transport_terminal(
        db, **context, client_order_id=meta["deadman_cid"], broker_order_id=meta["deadman_oid"],
        broker_order_status="canceled", filled_size=0.0, remaining_quantity=meta["qty"],
    )
    final = finalize_deadman_close_handoff_request(
        db, **context, handoff_token=meta["handoff_token"], successor_order_request=dict(meta["successor"]),
    )
    assert final["ok"] is True, final
    db.commit()

    assert _supersede(db, context, meta) is False
    handoff, _m = _claim(db, symbol)
    assert handoff is not None and handoff["phase"] == "successor_ready"


def test_refuses_when_the_deadman_no_longer_watches(db):
    symbol = f"PRW{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert resolve_owner_transport_terminal(
        db, **context, client_order_id=meta["deadman_cid"], broker_order_id=meta["deadman_oid"],
        broker_order_status="canceled", filled_size=0.0, remaining_quantity=meta["qty"],
    )
    db.commit()
    assert _supersede(db, context, meta) is False


def test_refuses_a_market_intent(db):
    """A market close has no price to re-price; the verb supersession owns verb changes."""
    symbol = f"PRM{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, limit_price=None)
    assert _supersede(db, context, meta) is False


def test_refuses_when_the_frozen_price_is_not_the_callers(db):
    """The caller must name the envelope it read; another price = another envelope."""
    symbol = f"PRC{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert _supersede(db, context, meta, frozen="2.80", new="2.69") is False
    handoff, _m = _claim(db, symbol)
    assert handoff is not None


def test_refuses_a_foreign_token_session_claim_or_account(db):
    symbol = f"PRX{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol)
    assert _supersede(db, context, meta, handoff_token=f"handoff-{uuid.uuid4().hex}") is False
    for bad in (
        {"owner_session_id": int(context["owner_session_id"]) + 9_999},
        {"claim_token": f"owner-{uuid.uuid4().hex}"},
        {"alpaca_account_id": str(uuid.uuid4())},
    ):
        assert _supersede(db, context, meta, claim_override=bad) is False
    handoff, _m = _claim(db, symbol)
    assert handoff is not None
    assert context["alpaca_account_id"] == TEST_ALPACA_ACCOUNT_ID


# ── 2. the retire primitive from the two broker-inert phases ──────────────────────


def _successor_released_no_transport(db, *, symbol: str, qty: float = 177.0):
    """Phase 2 cancelled the deadman, finalized + leased the successor, and the literal
    guard released it before its POST: ``successor_proven_no_transport``."""
    sess, context, meta = _freeze(db, symbol=symbol, qty=qty, limit_price="9.77")
    assert resolve_owner_transport_terminal(
        db, **context, client_order_id=meta["deadman_cid"], broker_order_id=meta["deadman_oid"],
        broker_order_status="canceled", filled_size=0.0, remaining_quantity=qty,
    )
    assert finalize_deadman_close_handoff_request(
        db, **context, handoff_token=meta["handoff_token"], successor_order_request=dict(meta["successor"]),
    )["ok"] is True
    successor_token = f"succ-worker-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db, **context, transport_kind="ordinary_exit",
        client_order_id=meta["successor"]["client_order_id"],
        order_request=dict(meta["successor"]), lease_token=successor_token,
    )
    assert leased["ok"] is True, leased
    meta["successor_lease_token"] = successor_token
    return sess, context, meta


def _replacement_rejected(db, context, meta):
    """The re-arm leased a replacement stop and Alpaca refused it before acceptance
    (42210000): ``replacement_deadman_proven_no_transport`` -- the COIW 14842 durable
    state, read from the live claim row (``pre_accept_rejected: true``,
    ``broker_order_id: ""``, no ``proven_no_transport``)."""
    replacement_cid = f"dm2-{uuid.uuid4().hex[:12]}"
    replacement_request = _request(
        symbol=context["symbol"], cid=replacement_cid, qty=meta["qty"], kind="deadman",
    )
    replacement_token = f"rep-worker-{uuid.uuid4().hex}"
    leased = lease_deadman_handoff_replacement(
        db, **context, client_order_id=replacement_cid, order_request=replacement_request,
        lease_token=replacement_token, broker_position_quantity=meta["qty"],
        local_position_quantity=meta["qty"],
    )
    assert leased["ok"] is True, leased
    assert resolve_owner_transport_terminal(
        db, **context, client_order_id=replacement_cid, broker_order_id="",
        broker_order_status="rejected", filled_size=0.0, pre_accept_rejected=True,
        lease_token=replacement_token,
    )
    meta["replacement_cid"] = replacement_cid
    meta["replacement_request"] = replacement_request
    return meta


def test_retire_from_replacement_deadman_proven_no_transport_after_a_pre_accept_reject(db):
    symbol = f"RTR{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _successor_released_no_transport(db, symbol=symbol)
    assert release_owner_transport_pre_post(
        db, **context, client_order_id=meta["successor"]["client_order_id"],
        lease_token=meta["successor_lease_token"], reason="exit_literal_bbo_refresh_failed",
    )
    meta = _replacement_rejected(db, context, meta)
    db.commit()
    handoff, metadata = _claim(db, symbol)
    assert handoff["phase"] == "replacement_deadman_proven_no_transport"
    current = metadata["owner_transport"]
    # the live shape: rejected pre-accept, no broker id, and NO proven_no_transport flag
    assert current["pre_accept_rejected"] is True and current["broker_order_id"] == ""
    assert "proven_no_transport" not in current

    assert retire_deadman_close_handoff(
        db, **context, handoff_token=meta["handoff_token"],
        outcome="replacement_deadman_proven_no_transport",
    ) is True
    db.commit()

    handoff, metadata = _claim(db, symbol)
    assert handoff is None
    last = (metadata.get("deadman_close_handoff_history") or [])[-1]
    assert last["phase"] == "retired"
    assert last["retirement_outcome"] == "replacement_deadman_proven_no_transport"
    # a FRESH close identity can now lease; the retired successor CID cannot be replayed
    fresh_cid = f"chili_ml_x_{uuid.uuid4().hex[:12]}"
    fresh = lease_owner_transport(
        db, **context, transport_kind="emergency_exit", client_order_id=fresh_cid,
        order_request=_request(symbol=symbol, cid=fresh_cid, qty=meta["qty"], kind="exit"),
        lease_token=f"x-{uuid.uuid4().hex}",
    )
    assert fresh["ok"] is True, fresh
    db.rollback()
    replay_cid = meta["successor"]["client_order_id"]
    replay = lease_owner_transport(
        db, **context, transport_kind="ordinary_exit", client_order_id=replay_cid,
        order_request=dict(meta["successor"]), lease_token=f"y-{uuid.uuid4().hex}",
    )
    assert replay["ok"] is False and replay["reason"] == "owner_transport_client_order_id_reused"


def test_the_replacement_outcome_refuses_an_accepted_replacement(db):
    """An ACCEPTED replacement stop is broker protection -- never retired from under it."""
    symbol = f"RTA{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _successor_released_no_transport(db, symbol=symbol)
    assert release_owner_transport_pre_post(
        db, **context, client_order_id=meta["successor"]["client_order_id"],
        lease_token=meta["successor_lease_token"], reason="exit_literal_bbo_refresh_failed",
    )
    replacement_cid = f"dm2-{uuid.uuid4().hex[:12]}"
    replacement_token = f"rep-worker-{uuid.uuid4().hex}"
    assert lease_deadman_handoff_replacement(
        db, **context, client_order_id=replacement_cid,
        order_request=_request(symbol=symbol, cid=replacement_cid, qty=meta["qty"], kind="deadman"),
        lease_token=replacement_token, broker_position_quantity=meta["qty"],
        local_position_quantity=meta["qty"],
    )["ok"] is True
    assert advance_owner_transport(
        db, **context, client_order_id=replacement_cid, lease_token=replacement_token,
        phase="submitted", broker_order_id=f"rep-oid-{uuid.uuid4().hex[:8]}",
    )
    db.commit()
    for outcome in ("replacement_deadman_proven_no_transport", "successor_proven_no_transport"):
        assert retire_deadman_close_handoff(
            db, **context, handoff_token=meta["handoff_token"], outcome=outcome,
        ) is False, outcome
    handoff, _m = _claim(db, symbol)
    assert handoff is not None


def test_retire_from_successor_proven_no_transport_after_the_literal_release(db):
    symbol = f"RTS{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _successor_released_no_transport(db, symbol=symbol)
    assert release_owner_transport_pre_post(
        db, **context, client_order_id=meta["successor"]["client_order_id"],
        lease_token=meta["successor_lease_token"], reason="exit_literal_bbo_refresh_failed",
    )
    db.commit()
    handoff, _m = _claim(db, symbol)
    assert handoff["phase"] == "successor_proven_no_transport"
    # the wrong outcome name is refused: the current transport is the successor
    assert retire_deadman_close_handoff(
        db, **context, handoff_token=meta["handoff_token"],
        outcome="replacement_deadman_proven_no_transport",
    ) is False
    assert retire_deadman_close_handoff(
        db, **context, handoff_token=meta["handoff_token"], outcome="successor_proven_no_transport",
    ) is True


def test_the_successor_outcome_accepts_a_pre_accept_rejected_successor(db):
    """Alpaca refusing the close itself before acceptance leaves the SAME phase without the
    ``proven_no_transport`` flag; the retire reads both proofs of 'never on the book'."""
    symbol = f"RTP{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _successor_released_no_transport(db, symbol=symbol)
    assert resolve_owner_transport_terminal(
        db, **context, client_order_id=meta["successor"]["client_order_id"], broker_order_id="",
        broker_order_status="rejected", filled_size=0.0, pre_accept_rejected=True,
        lease_token=meta["successor_lease_token"],
    )
    db.commit()
    handoff, metadata = _claim(db, symbol)
    assert handoff["phase"] == "successor_proven_no_transport"
    assert "proven_no_transport" not in metadata["owner_transport"]
    assert retire_deadman_close_handoff(
        db, **context, handoff_token=meta["handoff_token"], outcome="successor_proven_no_transport",
    ) is True


# ── 3. the marketability test is ONE definition ────────────────────────────────────


@pytest.mark.parametrize(
    "limit, bid, marketable",
    [
        (0.948, 0.907, False),   # BJDX 20293 09-08 10:39:49
        (2.76, 2.68, False),     # LBGJ 22135 09-11 09:45:13
        (2.69, 2.69, True),      # a limit printed from the bid itself compares equal
        (2.69 + 2.69 * 1e-9, 2.69, True),   # inside the float-equality epsilon
        (2.70, 2.69, False),
        (None, 2.69, False),
        (2.69, None, False),
    ],
)
def test_the_literal_guard_and_the_reprice_share_one_marketability_test(limit, bid, marketable):
    assert lr._exit_limit_marketable_at_bid(limit, bid) is marketable


# ── 4. the REAL runner: phase 2 re-prices before the cancel ────────────────────────


@pytest.fixture
def _alpaca_boundaries(monkeypatch):
    from tests.test_exit_verdict_g_whole_exit_seam import NOW

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(lr, "_utcnow", lambda: NOW)
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid, **_k: True)


class _RejectingStopAlpaca(_ScriptedAlpaca):
    """The scripted Alpaca, plus the broker's answer to a stop through the market."""

    def __init__(self, *, positions: list[float | None] | None = None) -> None:
        super().__init__(positions=positions)
        self.deadman_stop_calls: list[dict[str, Any]] = []

    def place_deadman_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.deadman_stop_calls.append(dict(kwargs))
        return {
            "ok": False,
            "submit_outcome": "broker_rejected",
            "error": (
                '{"code":42210000,"market_price":"9.51","message":'
                '"stop price must be less than current price","stop_price":"9.74"}'
            ),
        }


def _bbo(symbol: str, *, bid: float, ask: float, row: int) -> tuple[NormalizedTicker, Any]:
    meta = _fresh()
    return (
        NormalizedTicker(product_id=symbol, bid=bid, ask=ask, mid=(bid + ask) / 2.0,
                         freshness=meta, raw={"feed": "iqfeed_l1", "tape_row_id": row}),
        meta,
    )


def _frozen_leg(db, monkeypatch, *, symbol: str, adapter_cls=_ScriptedAlpaca):
    """A held Alpaca leg (Q = 10) on the REAL owner claim with ONE resting deadman that the
    broker reports ACTIVE until it is cancelled, and the phase-1 freeze at bid 9.95."""
    from tests.test_exit_verdict_g_whole_exit_seam import _decided_marker, _resting_deadman_session

    sess, le, scripted, deadman_oid, deadman_cid = _resting_deadman_session(
        db, symbol=symbol, deadman_qty=10.0, marker=_decided_marker("accel_rollover"),
    )
    adapter = adapter_cls(positions=[10.0])
    adapter.execution_bbo = scripted.execution_bbo
    active = _order(oid=deadman_oid, cid=deadman_cid, symbol=symbol, side="sell", status="open",
                    filled=0.0, avg=None, qty=10.0, order_type="stop", time_in_force="gtc",
                    position_intent="sell_to_close",
                    raw_overrides={"stop_price": 7.5, "alpaca_status": "new"})
    terminal = scripted.cid_orders[deadman_cid]
    # the broker truth of the ONE deadman: active until the lane cancels it, then terminal
    adapter.truth[deadman_cid] = lambda owner, _cid: terminal if owner.cancel_calls else active
    adapter.cid_orders[deadman_cid] = lambda owner, _cid: terminal if owner.cancel_calls else active
    adapter.orders[deadman_oid] = lambda owner, _oid: terminal if owner.cancel_calls else active
    le["position"]["target_price"] = 12.0
    reason, tag = lr._EXIT_VERDICT_ACTIONS["accel_rollover"]
    le["pending_exit_reason"] = reason  # the #1385 verdict block is pending-first
    out1 = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id=f"chili_ml_{tag}_{sess.id}_{uuid.uuid4().hex[:12]}",
        reason=reason, bid=9.95, ask=9.97, mid=9.96,
    )
    assert out1.get("error") == FREEZE_ERROR, out1
    assert out1.get("deadman_retained_active") is True, out1
    frozen = dict(le["deadman_released_for_close"]["successor_intent_pre_cancel"])
    assert lr._poll_live_exit_fill(db, sess, adapter, le=le, reason=reason, quantity=10.0)["why"] == (
        "pre_place_handback"
    )
    db.commit()
    return sess, le, adapter, deadman_oid, reason, frozen


def _rung(bid: float) -> str:
    """Rung 1 of the RTH ladder, exactly as the chokepoint mints it."""
    return lr._fmt_limit_price_sell(bid * (1.0 - lr._exit_ladder_guard_fraction(attempt=1, extended=False)))


def test_phase_two_reprices_before_the_cancel_and_the_next_pulse_posts_the_fresh_rung(
    db, monkeypatch, _alpaca_boundaries
):
    """BJDX 20293 shape on the REAL runner: the bid falls between the freeze (9.95) and
    phase 2 (9.80). The frozen rung is no longer marketable, so the SAME close is re-frozen
    at 9.80's rung and the deadman is never cancelled; the next pulse posts the fresh rung."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    symbol = "RPXA"
    sess, le, adapter, deadman_oid, reason, frozen = _frozen_leg(db, monkeypatch, symbol=symbol)
    assert frozen["limit_price"] == _rung(9.95) == "9.92"

    # ── pulse 2: the durable-handoff priority branch runs phase 2 at bid 9.80 ──
    adapter.execution_bbo = _bbo(symbol, bid=9.80, ask=9.82, row=9902)
    out2 = _run_tick(db, sess, adapter)

    assert out2.get("deadman_handoff_priority_serviced") is True, out2
    assert out2.get("deadman_retained_active") is True, out2
    assert adapter.cancel_calls == [], "the deadman is never cancelled for a stale price"
    assert adapter.limit_calls == [] and adapter.market_calls == []
    handoff, metadata = _claim(db, symbol)
    assert handoff["phase"] == "intent_frozen"
    assert handoff["successor_order_request"] is None
    assert handoff["successor_intent"]["limit_price"] == _rung(9.80) == "9.77"
    assert handoff["successor_intent"]["client_order_id"] == frozen["client_order_id"]
    assert handoff["successor_intent"]["base_size"] == frozen["base_size"]
    assert handoff["successor_transport_kind"] == "ordinary_exit"
    assert handoff["reason"] == reason
    superseded = (metadata.get("deadman_close_handoff_history") or [])[-1]
    assert superseded["supersession_reason"] == "successor_limit_not_marketable"
    assert superseded["superseded_frozen_limit_price"] == "9.92"
    assert superseded["superseded_by_limit_price"] == "9.77"
    assert metadata["owner_transport"]["transport_kind"] == "deadman"
    assert metadata["owner_transport"]["phase"] != "resolved"
    receipts = [p for ev, p in log if ev == "deadman_close_handoff_repriced"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["binding"] == "frozen_limit_above_fresh_bid"
    assert receipt["frozen_limit_price"] == "9.92"
    assert receipt["fresh_bid"] == 9.80
    assert receipt["fresh_limit_price"] == "9.77"
    assert receipt["fresh_rung"]["attempt"] == 1 and receipt["fresh_rung"]["extended_rung"] is False
    assert receipt["fresh_rung"]["guard_fraction"] == lr._exit_ladder_guard_fraction(attempt=1, extended=False)
    assert receipt["marketability_epsilon"] == max(1e-9, 9.80 * 1e-8)
    assert receipt["superseded_handoff_token"] != receipt["handoff_token"]
    assert receipt["refrozen"] is True and receipt["deadman_cancelled"] is False
    assert receipt["client_order_id"] == frozen["client_order_id"]
    names = [ev for ev, _ in log]
    assert "live_exit_literal_bbo_refresh_blocked" not in names
    assert "live_deadman_protection_unavailable_full_close_queued" not in names

    # ── pulse 3: phase 2 at the fresh rung -- one cancel, one POST, the same identity ──
    adapter.limit_results.append(lambda _o, cid: {"ok": True, "order_id": "succ-oid-1", "client_order_id": cid})
    out3 = _run_tick(db, sess, adapter)

    assert out3.get("deadman_handoff_priority_serviced") is True, out3
    assert adapter.cancel_calls == [deadman_oid]
    assert len(adapter.limit_calls) == 1 and adapter.market_calls == []
    posted = adapter.limit_calls[0]
    assert posted["client_order_id"] == frozen["client_order_id"]
    assert posted["limit_price"] == "9.77"
    assert posted["base_size"] == "10" and posted["position_intent"] == "sell_to_close"
    names = [ev for ev, _ in log]
    assert names.count("deadman_close_handoff_repriced") == 1, "a marketable frozen limit is never re-priced"
    assert "live_exit_literal_bbo_refresh_blocked" not in names


def test_phase_two_with_a_still_marketable_frozen_limit_is_unchanged(
    db, monkeypatch, _alpaca_boundaries
):
    """No drift, no re-price: the frozen rung is posted verbatim (the #1410 shape)."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    symbol = "RPXB"
    sess, le, adapter, deadman_oid, _reason, frozen = _frozen_leg(db, monkeypatch, symbol=symbol)
    adapter.limit_results.append(lambda _o, cid: {"ok": True, "order_id": "succ-oid-2", "client_order_id": cid})

    out = _run_tick(db, sess, adapter)

    assert out.get("deadman_handoff_priority_serviced") is True, out
    assert adapter.cancel_calls == [deadman_oid]
    assert len(adapter.limit_calls) == 1
    assert adapter.limit_calls[0]["limit_price"] == frozen["limit_price"] == "9.92"
    assert adapter.limit_calls[0]["client_order_id"] == frozen["client_order_id"]
    assert "deadman_close_handoff_repriced" not in [ev for ev, _ in log]


def test_failed_price_supersession_retains_the_stop_and_retries_from_durable_state(
    db, monkeypatch, _alpaca_boundaries
):
    from tests.test_exit_verdict_g_whole_exit_seam import _session_hours

    _session_hours(monkeypatch, "regular")
    symbol = "RPCAS"
    sess, le, adapter, deadman_oid, reason, frozen = _frozen_leg(db, monkeypatch, symbol=symbol)
    original_handoff, _ = _claim(db, symbol)
    adapter.execution_bbo = _bbo(symbol, bid=9.80, ask=9.82, row=9902)
    supersede = lr.supersede_unsent_deadman_close_handoff_for_price_committed
    with monkeypatch.context() as failed_write:
        failed_write.setattr(lr, "supersede_unsent_deadman_close_handoff_for_price_committed", lambda **kw: False)
        out = _run_tick(db, sess, adapter)

    assert adapter.cancel_calls == [], "failed durable supersession must not cancel protection"
    assert adapter.limit_calls == [] and adapter.market_calls == []
    assert out.get("pre_place_error") == "deadman_frozen_limit_reprice_not_certified", out
    assert out.get("deadman_retained_active") is True, out
    handoff, metadata = _claim(db, symbol)
    assert handoff["handoff_token"] == original_handoff["handoff_token"]
    assert handoff["successor_intent"] == frozen
    assert metadata["owner_transport"]["phase"] != "resolved"
    assert lr.supersede_unsent_deadman_close_handoff_for_price_committed is supersede

    # A later certified write may re-freeze the same close; it still cannot cancel.
    retried = _run_tick(db, sess, adapter)
    assert retried.get("deadman_retained_active") is True, retried
    assert adapter.cancel_calls == []
    handoff, _ = _claim(db, symbol)
    assert handoff["successor_intent"]["limit_price"] == "9.77"
    assert handoff["successor_intent"]["client_order_id"] == frozen["client_order_id"]


def _premarket_flatten_leg(db, *, symbol: str, adapter_cls=_ScriptedAlpaca):
    """A held premarket leg with ONE resting deadman and a queued operator flatten (the
    emergency path runs BEFORE the durable-handoff priority branch)."""
    from tests.test_exit_verdict_g_whole_exit_seam import _resting_deadman_session

    sess, le, scripted, deadman_oid, deadman_cid = _resting_deadman_session(
        db, symbol=symbol, deadman_qty=10.0, marker=None,
    )
    adapter = adapter_cls(positions=[10.0])
    active = _order(oid=deadman_oid, cid=deadman_cid, symbol=symbol, side="sell", status="open",
                    filled=0.0, avg=None, qty=10.0, order_type="stop", time_in_force="gtc",
                    position_intent="sell_to_close",
                    raw_overrides={"stop_price": 7.5, "alpaca_status": "new"})
    terminal = scripted.cid_orders[deadman_cid]
    adapter.truth[deadman_cid] = lambda owner, _cid: terminal if owner.cancel_calls else active
    adapter.cid_orders[deadman_cid] = lambda owner, _cid: terminal if owner.cancel_calls else active
    adapter.orders[deadman_oid] = lambda owner, _oid: terminal if owner.cancel_calls else active
    le["operator_flatten_requested_utc"] = "2026-09-10T14:00:30"
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot[lr.KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    return sess, adapter, deadman_oid


def _ext_rung(bid: float) -> str:
    """The extended-hours rung (urgent flatten) exactly as the chokepoint mints it."""
    return lr._fmt_limit_price_sell(bid * (1.0 - lr._exit_ladder_guard_fraction(attempt=1, extended=True)))


def test_a_premarket_emergency_phase_two_binds_the_frozen_price_not_its_own_rung(
    db, monkeypatch, _alpaca_boundaries
):
    """The PRE-EXISTING defect on the queued-flatten path, with no re-price involved: the
    bid rises between the freeze (9.95) and phase 2 (10.10), so the frozen 9.75 is still
    marketable but phase 2's own rung is 9.89. The emergency request used to bind 9.89
    while the finalized successor carries 9.75 -> `alpaca_owner_transport_kind_mismatch`
    after the deadman was ALREADY cancelled. Now the frozen price is bound and posted."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "premarket")
    symbol = "EMUP"
    sess, adapter, deadman_oid = _premarket_flatten_leg(db, symbol=symbol)
    adapter.execution_bbo = _bbo(symbol, bid=9.95, ask=9.97, row=9921)
    _run_tick(db, sess, adapter)
    handoff, _m = _claim(db, symbol)
    assert handoff["successor_intent"]["limit_price"] == _ext_rung(9.95)
    authority_cid = handoff["successor_client_order_id"]

    adapter.execution_bbo = _bbo(symbol, bid=10.10, ask=10.12, row=9922)
    assert _ext_rung(10.10) != _ext_rung(9.95)
    mark = len(log)
    _run_tick(db, sess, adapter)
    pulse2 = [(ev, p.get("error") or p.get("reason")) for ev, p in log[mark:]]

    assert adapter.cancel_calls == [deadman_oid], pulse2
    assert len(adapter.limit_calls) == 1, pulse2
    assert adapter.limit_calls[0]["limit_price"] == _ext_rung(9.95)
    assert adapter.limit_calls[0]["client_order_id"] == authority_cid
    assert "deadman_close_handoff_repriced" not in [ev for ev, _ in log]
    live = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert live["exit_limit_price"] == float(_ext_rung(9.95)), "the receipt names the POSTed price"


def test_the_retired_emergency_successor_cid_rotates_and_the_next_pulse_posts_it(
    db, monkeypatch, _alpaca_boundaries
):
    """Slice 2 when the flatten ITSELF froze the successor (its authority CID is the retired
    successor CID, already in the owner history): the retire rotates the authority to its
    next deterministic CID and returns; the next pulse proves that CID absent and POSTs."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "premarket")
    symbol = "EMRT"
    sess, adapter, deadman_oid = _premarket_flatten_leg(db, symbol=symbol, adapter_cls=_RejectingStopAlpaca)
    high = _bbo(symbol, bid=9.95, ask=9.97, row=9931)
    low = _bbo(symbol, bid=9.50, ask=9.52, row=9932)
    adapter.execution_bbo = lambda owner, _sym: low if owner.cancel_calls else high

    _run_tick(db, sess, adapter)        # pulse 1: freeze at 9.95's rung
    handoff, _m = _claim(db, symbol)
    retired_cid = handoff["successor_client_order_id"]
    _run_tick(db, sess, adapter)        # pulse 2: cancel -> literal block at 9.50 -> re-arm refused
    handoff, _m = _claim(db, symbol)
    assert handoff["phase"] == "replacement_deadman_proven_no_transport", handoff["phase"]
    assert adapter.cancel_calls == [deadman_oid] and adapter.limit_calls == []
    assert len(adapter.deadman_stop_calls) == 1

    mark = len(log)
    _run_tick(db, sess, adapter)        # pulse 3: retire + rotate the authority, post nothing
    pulse3 = [(ev, p.get("error") or p.get("binding")) for ev, p in log[mark:]]
    assert ("deadman_close_handoff_retired_proven_no_transport", "handoff_broker_inert_and_stop_cannot_rest") in pulse3
    assert ("live_deadman_stop_release_blocked", "deadman_close_handoff_retired_successor_cid_rotated") in pulse3
    assert adapter.limit_calls == []
    authority = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["emergency_exit_authority"]
    assert authority["client_order_id"] != retired_cid and authority["attempt_no"] == 2
    assert authority["terminal_attempts"][-1]["client_order_id"] == retired_cid

    _run_tick(db, sess, adapter)        # pulse 4: the rotated CID POSTs at this pulse's rung
    assert len(adapter.limit_calls) == 1, [(ev, p.get("error")) for ev, p in log[mark:]]
    posted = adapter.limit_calls[0]
    assert posted["client_order_id"] == authority["client_order_id"]
    assert posted["limit_price"] == _ext_rung(9.50)
    handoff, metadata = _claim(db, symbol)
    assert handoff is None and metadata["owner_transport"]["client_order_id"] == posted["client_order_id"]


def test_a_premarket_emergency_close_reprices_before_the_cancel_and_posts_the_frozen_rung(
    db, monkeypatch, _alpaca_boundaries
):
    """The queued-flatten path (runs BEFORE the priority branch, so its phase 2 is NOT a
    handoff recovery). Premarket = the extended 8x rung. Pulse 1 freezes at bid 9.95; the
    bid falls to 9.60 (frozen 9.75 no longer marketable) -> re-frozen at 9.60's rung, deadman
    untouched; pulse 3 at 9.70 cancels once and POSTs the FROZEN 9.40 with the authority CID
    -- the emergency request carries the frozen price, not pulse 3's own rung."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "premarket")
    symbol = "EMRP"
    sess, adapter, deadman_oid = _premarket_flatten_leg(db, symbol=symbol)
    ext_rung = _ext_rung

    # ── pulse 1: the flatten freezes its successor at 9.95's extended rung ──
    adapter.execution_bbo = _bbo(symbol, bid=9.95, ask=9.97, row=9911)
    _run_tick(db, sess, adapter)
    handoff, _m = _claim(db, symbol)
    assert handoff is not None and handoff["phase"] == "intent_frozen", handoff
    authority_cid = handoff["successor_client_order_id"]
    assert authority_cid.startswith(f"chili_ml_x_{sess.id}_")
    assert handoff["successor_transport_kind"] == "emergency_exit"
    assert handoff["successor_intent"]["limit_price"] == ext_rung(9.95)
    assert adapter.cancel_calls == [] and adapter.limit_calls == []

    # ── pulse 2: bid 9.60 -- the frozen rung is above it: re-price, never cancel ──
    adapter.execution_bbo = _bbo(symbol, bid=9.60, ask=9.62, row=9912)
    _run_tick(db, sess, adapter)
    handoff, _m = _claim(db, symbol)
    assert handoff["phase"] == "intent_frozen"
    assert handoff["successor_intent"]["limit_price"] == ext_rung(9.60)
    assert handoff["successor_client_order_id"] == authority_cid
    assert handoff["successor_transport_kind"] == "emergency_exit"
    assert adapter.cancel_calls == [] and adapter.limit_calls == []
    receipts = [p for ev, p in log if ev == "deadman_close_handoff_repriced"]
    assert len(receipts) == 1 and receipts[0]["fresh_rung"]["extended_rung"] is True
    assert receipts[0]["fresh_limit_price"] == ext_rung(9.60)

    # ── pulse 3: bid 9.70 -- still marketable: one cancel, one POST at the FROZEN price ──
    adapter.execution_bbo = _bbo(symbol, bid=9.70, ask=9.72, row=9913)
    assert ext_rung(9.70) != ext_rung(9.60), "pulse 3's own rung differs from the frozen one"
    mark = len(log)
    _run_tick(db, sess, adapter)
    pulse3 = [(ev, p.get("error") or p.get("reason")) for ev, p in log[mark:]]

    assert adapter.cancel_calls == [deadman_oid], pulse3
    assert len(adapter.limit_calls) == 1, (pulse3, sess.risk_snapshot_json[lr.KEY_LIVE_EXEC].get("alpaca_owner_transport_block"))
    posted = adapter.limit_calls[0]
    assert posted["client_order_id"] == authority_cid
    assert posted["limit_price"] == ext_rung(9.60)
    assert posted.get("extended_hours") is True and posted["position_intent"] == "sell_to_close"
    assert "live_exit_literal_bbo_refresh_blocked" not in [ev for ev, _ in log]


# ── 5. the REAL runner: the BJDX aftermath ────────────────────────────────────────


def _bjdx_lockout(db, monkeypatch, log, *, symbol: str):
    """Phase 2 posts nothing: the frozen rung is marketable at the pulse's bid (9.95), then
    the book falls during the cancel/finalize (9.50) and the literal guard refuses; the
    re-arm is refused by the broker (42210000). This is the BJDX 20293 chain, measured."""
    sess, le, adapter, deadman_oid, _reason, frozen = _frozen_leg(
        db, monkeypatch, symbol=symbol, adapter_cls=_RejectingStopAlpaca,
    )
    high = _bbo(symbol, bid=9.95, ask=9.97, row=9903)
    low = _bbo(symbol, bid=9.50, ask=9.52, row=9904)
    adapter.execution_bbo = lambda owner, _sym: low if owner.cancel_calls else high

    _run_tick(db, sess, adapter)

    assert adapter.cancel_calls == [deadman_oid]
    assert adapter.limit_calls == [] and adapter.market_calls == []
    assert len(adapter.deadman_stop_calls) == 1, "the re-arm was attempted and refused"
    names = {ev: p for ev, p in log}
    assert names["live_exit_literal_bbo_refresh_blocked"]["error"] == (
        "frozen_exit_limit_not_marketable_at_literal_post"
    )
    assert names["live_deadman_protection_unavailable_full_close_queued"]["error"] == (
        "alpaca_deadman_explicitly_rejected"
    )
    handoff, metadata = _claim(db, symbol)
    assert handoff["phase"] == "replacement_deadman_proven_no_transport"
    assert metadata["owner_transport"]["pre_accept_rejected"] is True
    live = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert live.get("operator_flatten_requested_utc"), "the full close is queued"
    assert not live.get("deadman_stop")
    return sess, adapter, frozen, handoff


def test_bjdx_aftermath_the_queued_close_retires_the_inert_handoff_and_posts_a_fresh_identity(
    db, monkeypatch, _alpaca_boundaries
):
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    symbol = "BJXA"
    sess, adapter, frozen, stuck = _bjdx_lockout(db, monkeypatch, log, symbol=symbol)
    log.clear()

    # ── the next pulse: the queued operator flatten runs FIRST and closes ──
    out = _run_tick(db, sess, adapter)

    assert len(adapter.market_calls) == 1, (out, [ev for ev, _ in log])
    posted = adapter.market_calls[0]
    assert posted["client_order_id"].startswith(f"chili_ml_x_{sess.id}_")
    assert posted["client_order_id"] != frozen["client_order_id"], "a fresh identity, not the retired one"
    assert posted["base_size"] == "10" and posted["position_intent"] == "sell_to_close"
    names = [ev for ev, _ in log]
    retired = [p for ev, p in log if ev == "deadman_close_handoff_retired_proven_no_transport"]
    assert len(retired) == 1
    receipt = retired[0]
    assert receipt["binding"] == "handoff_broker_inert_and_stop_cannot_rest"
    assert receipt["phase"] == "replacement_deadman_proven_no_transport"
    assert receipt["handoff_token"] == stuck["handoff_token"]
    assert receipt["successor_client_order_id"] == frozen["client_order_id"]
    assert receipt["reprotect_error"] == "alpaca_deadman_explicitly_rejected"
    assert "42210000" in str(receipt["reprotect_broker_error"])
    assert receipt["broker_position_quantity"] == 10.0 and receipt["local_position_quantity"] == 10.0
    assert receipt["close_reason"] == "operator_flatten"
    assert receipt["close_client_order_id"] == posted["client_order_id"]
    for loop in ("deadman_close_final_request_not_certified", "deadman_close_handoff_identity_mismatch"):
        assert not any(
            ev == "live_deadman_stop_release_blocked" and p.get("error") == loop for ev, p in log
        ), loop
    handoff, metadata = _claim(db, symbol)
    assert handoff is None
    assert (metadata.get("deadman_close_handoff_history") or [])[-1]["retirement_outcome"] == (
        "replacement_deadman_proven_no_transport"
    )
    assert metadata["owner_transport"]["transport_kind"] == "emergency_exit"
    assert "live_exit_submitted" in names


def test_without_the_retire_the_queued_close_is_the_measured_forever_loop(
    db, monkeypatch, _alpaca_boundaries
):
    """The named fallback, exercised: with the retire refused, the queued RTH flatten meets
    the frozen LIMIT with a MARKET verb and blocks on identity_mismatch every pulse -- the
    1,176 BJDX / 802 COIW rows. Nothing is posted and nothing is re-armed."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, adapter, _frozen, _stuck = _bjdx_lockout(db, monkeypatch, log, symbol="BJXB")
    monkeypatch.setattr(lr, "retire_deadman_close_handoff_committed", lambda **_k: False)
    log.clear()

    for _pulse in range(2):
        _run_tick(db, sess, adapter)

    assert adapter.market_calls == [] and adapter.limit_calls == []
    blocks = [p.get("error") for ev, p in log if ev == "live_deadman_stop_release_blocked"]
    assert blocks and set(blocks) == {"deadman_close_handoff_identity_mismatch"}, blocks
    assert "deadman_close_handoff_retired_proven_no_transport" not in [ev for ev, _ in log]
