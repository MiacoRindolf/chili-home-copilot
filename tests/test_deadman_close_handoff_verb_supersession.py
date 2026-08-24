"""Ang naka-freeze na close handoff ay hindi dapat mag-deadlock kapag nagbago
ang exit VERB (2026-08-24).

ANG DEADLOCK, SINUKAT SA LIVE (session 15152 / XPON, 340 block sa isang araw):

    qty=51.0  attempts=0  env_reason=bailout  env_phase=intent_frozen
    env_verb=limit  successor_order_request=NULL  huling_verb=market
    -> deadman_close_handoff_identity_mismatch, walang hanggan

Ang dalawang literal-submit na seam sa ``live_runner`` ay nag-a-assert ng
magkaibang successor verb -- ang limit-close ay nagpapasa ng
``successor_order_type="limit"``, ang market-close ng ``"market"``. Kung alin
ang tumakbo MUNA ang nagfi-freeze ng envelope. Hinihingi pagkatapos ng
``_apply_frozen_successor`` na tumugma ang naka-freeze na ``order_type`` sa
KASALUKUYANG derivation, kaya kapag nagbago ang exit decision -- bailout
nag-freeze ng ``limit``, tapos operator_flatten nag-derive ng ``market`` --
permanente ang mismatch at ang posisyon ay hindi kailanman nailalabas.

WALANG LEGAL NA LABASAN SA DATI:
  * ``prepare_deadman_close_handoff`` ay tumatangging mag-overwrite -- ang
    ``successor_intent`` ay nasa ``immutable_keys`` nito;
  * ``retire_deadman_close_handoff`` ay hinihingi ang isang TERMINAL na
    successor generation -- at ang successor ay hindi kailanman naipadala,
    dahil iyon mismo ang na-block.

ANG LUNAS ay ``supersede_unsent_deadman_close_handoff``, na ligtas dahil sa
ISANG katotohanan: ``phase == "intent_frozen"`` AT
``successor_order_request is None`` ay nangangahulugang WALANG order na umabot
sa broker para sa envelope na ito. Isang HANGARIN lang na hindi pa naipapadala
ang pinapalitan -- imposible ang double-fill.

Runnable: pytest tests/test_deadman_close_handoff_verb_supersession.py -v
"""
from __future__ import annotations

import uuid

import pytest

from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    advance_owner_transport,
    finalize_deadman_close_handoff_request,
    lease_owner_transport,
    prepare_deadman_close_handoff,
    read_action_claim,
    resolve_owner_transport_terminal,
    supersede_unsent_deadman_close_handoff,
)

from tests.test_alpaca_deadman_close_handoff import _request, _seed_owner
from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID


def _limit_request(*, symbol: str, cid: str, qty: float) -> dict:
    """Ang hugis na ini-freeze ng LIMIT-close seam (ladder rung / bailout)."""
    return {
        **_request(symbol=symbol, cid=cid, qty=qty, kind="exit"),
        "order_type": "limit",
        "limit_price": 8.17,
    }


def _freeze(db, *, symbol: str, qty: float, order_type: str):
    """Isang buhay na deadman na nagbabantay sa posisyon + isang naka-freeze,
    HINDI PA naipapadalang close envelope. Eksaktong estado ng XPON 15152."""
    sess, context = _seed_owner(db, symbol=symbol, quantity=qty)
    deadman_cid = f"dm-{uuid.uuid4().hex[:12]}"
    deadman_oid = f"dm-oid-{uuid.uuid4().hex[:10]}"
    deadman_request = _request(
        symbol=symbol, cid=deadman_cid, qty=qty, kind="deadman"
    )
    lease_token = f"dm-worker-{uuid.uuid4().hex}"
    assert lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=deadman_cid,
        order_request=deadman_request,
        lease_token=lease_token,
    )["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=deadman_cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=deadman_oid,
    )
    successor_cid = f"close-{uuid.uuid4().hex[:12]}"
    successor = (
        _limit_request(symbol=symbol, cid=successor_cid, qty=qty)
        if order_type == "limit"
        else _request(symbol=symbol, cid=successor_cid, qty=qty, kind="exit")
    )
    handoff_token = f"handoff-{uuid.uuid4().hex}"
    prepared = prepare_deadman_close_handoff(
        db,
        **context,
        handoff_token=handoff_token,
        deadman_client_order_id=deadman_cid,
        deadman_broker_order_id=deadman_oid,
        deadman_order_request=deadman_request,
        successor_transport_kind="emergency_exit",
        successor_intent=successor,
        reason="bailout",
    )
    assert prepared["ok"] is True, prepared
    db.commit()
    return sess, context, {
        "handoff_token": handoff_token,
        "deadman_cid": deadman_cid,
        "deadman_oid": deadman_oid,
        "deadman_request": deadman_request,
        "successor": successor,
    }


def _handoff(db, symbol: str):
    readable, claim = read_action_claim(
        db, symbol=symbol, account_scope="alpaca:paper"
    )
    assert readable and claim is not None
    return (claim["metadata"] or {}).get("deadman_close_handoff"), dict(
        claim["metadata"] or {}
    )


# ── ang deadlock mismo ─────────────────────────────────────────────────────

def test_the_old_path_really_is_a_deadlock(db):
    """Idokumento ang bitag: ang muling pag-freeze sa BAGONG verb ay tinatanggihan."""
    symbol = f"DLK{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")

    retry = prepare_deadman_close_handoff(
        db,
        **context,
        handoff_token=f"handoff-{uuid.uuid4().hex}",
        deadman_client_order_id=meta["deadman_cid"],
        deadman_broker_order_id=meta["deadman_oid"],
        deadman_order_request=meta["deadman_request"],
        successor_transport_kind="emergency_exit",
        successor_intent=_request(
            symbol=symbol, cid=f"close-{uuid.uuid4().hex[:12]}", qty=51.0, kind="exit"
        ),  # order_type="market" -- ang BAGONG desisyon
        reason="operator_flatten",
    )
    assert retry["ok"] is False
    assert retry["reason"] == "deadman_close_handoff_generation_mismatch"


# ── ang lunas ──────────────────────────────────────────────────────────────

def test_verb_change_supersedes_an_unsent_frozen_envelope(db):
    """ANG PANGUNAHING KASO: limit na naka-freeze, market ngayon ang kailangan."""
    symbol = f"SUP{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")

    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",
        superseding_order_type="market",
    ) is True
    db.commit()

    handoff, metadata = _handoff(db, symbol)
    assert handoff is None, "ang naka-freeze na envelope ay dapat naalis na"

    history = metadata.get("deadman_close_handoff_history") or []
    assert history, "ang supersession ay dapat naaaudit"
    last = history[-1]
    assert last["phase"] == "superseded"
    assert last["supersession_reason"] == "successor_order_type_changed"
    assert last["superseded_frozen_order_type"] == "limit"
    assert last["superseded_by_order_type"] == "market"
    assert last["handoff_token"] == meta["handoff_token"]


def test_the_next_freeze_succeeds_after_supersession(db):
    """Ang buong punto: pagkatapos ng supersession ay dumadaan na ang BAGONG verb."""
    symbol = f"NXT{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",
        superseding_order_type="market",
    ) is True
    db.commit()

    market_intent = _request(
        symbol=symbol, cid=f"close-{uuid.uuid4().hex[:12]}", qty=51.0, kind="exit"
    )
    assert market_intent["order_type"] == "market"
    refrozen = prepare_deadman_close_handoff(
        db,
        **context,
        handoff_token=f"handoff-{uuid.uuid4().hex}",
        deadman_client_order_id=meta["deadman_cid"],
        deadman_broker_order_id=meta["deadman_oid"],
        deadman_order_request=meta["deadman_request"],
        successor_transport_kind="emergency_exit",
        successor_intent=market_intent,
        reason="operator_flatten",
    )
    assert refrozen["ok"] is True, refrozen
    assert refrozen["handoff"]["successor_intent"]["order_type"] == "market"
    assert refrozen["handoff"]["phase"] == "intent_frozen"


# ── ang mga gate na gumagawa nitong ligtas ─────────────────────────────────

def test_supersede_refuses_once_a_successor_request_was_finalized(db):
    """ANG PINAKAMAHALAGANG GATE: kapag may na-finalize nang request, MAAARING
    may CID na naipadala sa broker. Bawal ang supersession doon."""
    symbol = f"SNT{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")

    assert resolve_owner_transport_terminal(
        db,
        **context,
        client_order_id=meta["deadman_cid"],
        broker_order_id=meta["deadman_oid"],
        broker_order_status="canceled",
        filled_size=0.0,
        remaining_quantity=51.0,
    )
    final = finalize_deadman_close_handoff_request(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        successor_order_request=dict(meta["successor"]),
    )
    assert final["ok"] is True, final
    db.commit()

    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",
        superseding_order_type="market",
    ) is False
    db.commit()
    handoff, _meta = _handoff(db, symbol)
    assert handoff is not None, "ang naipadalang envelope ay dapat BUO pa rin"


def test_supersede_refuses_when_the_deadman_is_no_longer_watching(db):
    """Ang deadman ay dapat AKTIBO PA RIN ang pagbabantay sa posisyon habang
    tayo ay nagpapalit ng hangarin; kung resolved na ito, ibang code path na."""
    symbol = f"RES{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    assert resolve_owner_transport_terminal(
        db,
        **context,
        client_order_id=meta["deadman_cid"],
        broker_order_id=meta["deadman_oid"],
        broker_order_status="canceled",
        filled_size=0.0,
        remaining_quantity=51.0,
    )
    db.commit()

    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",
        superseding_order_type="market",
    ) is False


def test_supersede_refuses_identical_verbs(db):
    """Walang churn: ito ay para LANG sa tunay na pagbabago ng verb."""
    symbol = f"SAM{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",
        superseding_order_type="limit",
    ) is False
    handoff, _m = _handoff(db, symbol)
    assert handoff is not None


@pytest.mark.parametrize("frozen,new", [("limit", "stop"), ("stop", "market")])
def test_supersede_accepts_only_limit_and_market(db, frozen, new):
    """Ang dalawang seam ay nag-a-assert ng limit o market -- wala nang iba."""
    symbol = f"VRB{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type=frozen,
        superseding_order_type=new,
    ) is False


def test_supersede_refuses_a_foreign_handoff_token(db):
    symbol = f"TOK{uuid.uuid4().hex[:5].upper()}"
    _sess, context, _meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=f"handoff-{uuid.uuid4().hex}",
        frozen_order_type="limit",
        superseding_order_type="market",
    ) is False
    handoff, _m = _handoff(db, symbol)
    assert handoff is not None


def test_supersede_refuses_a_foreign_session_and_claim(db):
    symbol = f"FRN{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    for bad in (
        {**context, "owner_session_id": int(context["owner_session_id"]) + 9_999},
        {**context, "claim_token": f"owner-{uuid.uuid4().hex}"},
        {**context, "alpaca_account_id": "00000000-0000-0000-0000-000000000000"},
    ):
        assert supersede_unsent_deadman_close_handoff(
            db,
            **bad,
            handoff_token=meta["handoff_token"],
            frozen_order_type="limit",
            superseding_order_type="market",
        ) is False
    handoff, _m = _handoff(db, symbol)
    assert handoff is not None


def test_supersede_refuses_when_the_frozen_intent_carries_another_verb(db):
    """Kung ang envelope ay hindi nagdadala ng verb na sinasabi ng caller, hindi
    ito ang deadlock na inaakala natin -- huwag sumulat."""
    symbol = f"MIS{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="market")
    assert supersede_unsent_deadman_close_handoff(
        db,
        **context,
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",   # ang envelope ay talagang "market"
        superseding_order_type="market",
    ) is False
    handoff, _m = _handoff(db, symbol)
    assert handoff is not None


def test_account_identity_is_still_enforced(db):
    """Ang identidad ng account ay hindi kailanman niluluwagan ng supersession."""
    symbol = f"ACC{uuid.uuid4().hex[:5].upper()}"
    _sess, context, meta = _freeze(db, symbol=symbol, qty=51.0, order_type="limit")
    assert context["alpaca_account_id"] == TEST_ALPACA_ACCOUNT_ID
    assert supersede_unsent_deadman_close_handoff(
        db,
        **{**context, "alpaca_account_id": str(uuid.uuid4())},
        handoff_token=meta["handoff_token"],
        frozen_order_type="limit",
        superseding_order_type="market",
    ) is False
