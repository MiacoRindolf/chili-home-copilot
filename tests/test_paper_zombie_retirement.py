"""Ang isang PRE-ENTRY na paper session ay dapat magretiro, hindi mabuhay magpakailanman (2026-08-25).

ANG PUWANG. Ang risk-block handler ng paper runner ay may branch para sa QUEUED
(-> ERROR) at para sa ENTERED (-> palabasin ang posisyon), pero **WALA para sa
WATCHING at ENTRY_CANDIDATE**. Ang mga session sa dalawang state na iyon ay
nag-e-emit ng ``paper_blocked_by_risk`` at pagkatapos ay **walang ginagawa** --
kaya bumabalik sila sa susunod na tick at muling nabablock. Habambuhay.

NASUKAT SA BUHAY NA DB (2026-08-25)::

    session 14504  CDTG  entry_candidate  5,569 block   simula 08-20
    session 14511  BEEM  watching         5,298
    session 14509  XRP   watching         5,286
    session 14512  DFDV  watching         5,238
    session 14507  SGLY  entry_candidate  5,019

    64,719 na paper_blocked_by_risk mula 07-01
    iisa ang dahilan sa lahat: "Not paper-eligible per neural viability."

Ang mga session na ito ay HINDI NA maaaring mag-trade. Ineevaluate sila kada tick
nang LIMANG ARAW. Dalawa ang pinsala: nasasayang na cycle, at nalulunod ang action
history na binabasa ng operator -- walo sa labing-isang linya sa screen niya ay
basura mula 08-20.

⚠️ BILANG, HINDI ORASAN. Ang isang panandaliang block ay dapat MAKABAWI, kaya ang
bilang ay nire-reset sa unang tick na hindi naka-block. Ang PAULIT-ULIT lang na
block ang nagreretiro. Walang orasan ⇒ walang pag-asa sa tick cadence, na iba-iba.

⚠️ WALANG POSISYONG NAKATAYA. Ang mga ito ay PRE-entry, kaya walang ilalabas.

Runnable: pytest tests/test_paper_zombie_retirement.py -v
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.trading import TradingAutomationEvent, TradingAutomationSession
from app.services.trading.momentum_neural import paper_runner
from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
from app.services.trading.momentum_neural.paper_fsm import (
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_WATCHING,
)
from app.services.trading.momentum_neural.paper_runner import tick_paper_session

from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid

_BLOCK_EV = {
    "severity": "block",
    "errors": ["Not paper-eligible per neural viability."],
    "evaluated_at_utc": "2026-08-25T13:47:59.307020+00:00",
}


def _qfn(_sym: str) -> dict:
    return {"mid": 100.0, "bid": 99.9, "ask": 100.1, "source": "test"}


def _watching_session(db: Session, monkeypatch, tag: str) -> int:
    """Isang paper session na naiusad na hanggang sa isang PRE-ENTRY na state."""
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    # ⚠️ MALAKING TITIK. Ang create_paper_draft_session ay nag-no-normalize ng
    # simbolo pataas; ang isang maliit na titik na seed ay hindi tumutugma sa
    # viability row at bumabagsak nang may "No durability viability row".
    sym = f"{tag.upper()}-USD"
    vid, _ = _seed_live_eligible_row(db, symbol=sym)
    db.commit()
    r = create_paper_draft_session(
        db, user_id=_uid(db, tag), symbol=sym, variant_id=vid,
        execution_family="coinbase_spot",
    )
    assert r["ok"], r
    sid = int(r["session_id"])
    db.commit()
    tick_paper_session(db, sid, quote_fn=_qfn)  # QUEUED -> WATCHING
    db.commit()
    sess = db.get(TradingAutomationSession, sid)
    assert sess.state in (STATE_WATCHING, STATE_ENTRY_CANDIDATE), sess.state
    return sid


def _always_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        paper_runner, "runner_boundary_risk_ok", lambda _db, _s: (False, dict(_BLOCK_EV))
    )


def test_a_persistently_blocked_pre_entry_session_RETIRES(monkeypatch, db: Session) -> None:
    """ANG PANGUNAHING KASO. Ito ang 5,569-na-block na ikot."""
    monkeypatch.setattr(settings, "chili_momentum_paper_risk_block_retire_after", 5)
    sid = _watching_session(db, monkeypatch, "zra")
    _always_blocked(monkeypatch)

    for _ in range(5):
        tick_paper_session(db, sid, quote_fn=_qfn)
        db.commit()

    sess = db.get(TradingAutomationSession, sid)
    assert sess.state == STATE_ERROR, (
        f"dapat nagretiro matapos ang 5 magkakasunod na block, nasa {sess.state} pa rin"
    )


def test_it_does_NOT_retire_before_the_threshold(monkeypatch, db: Session) -> None:
    """Hindi ito dapat maging trigger-happy -- ang session ay dapat mabuhay pa sa
    ilalim ng hangganan."""
    monkeypatch.setattr(settings, "chili_momentum_paper_risk_block_retire_after", 5)
    sid = _watching_session(db, monkeypatch, "zrb")
    _always_blocked(monkeypatch)

    for _ in range(4):
        tick_paper_session(db, sid, quote_fn=_qfn)
        db.commit()

    sess = db.get(TradingAutomationSession, sid)
    assert sess.state != STATE_ERROR, "masyadong maaga ang pagretiro sa ika-4 na block"


def test_a_TRANSIENT_block_does_not_retire_the_session(monkeypatch, db: Session) -> None:
    """⚠️ ITO ANG DAHILAN NG BILANG SA HALIP NA KABUUAN. Ang isang bugso ng block na
    sinusundan ng isang malinis na tick ay dapat mag-reset -- kung hindi ay
    magreretiro tayo ng session na makakabawi pa naman."""
    monkeypatch.setattr(settings, "chili_momentum_paper_risk_block_retire_after", 5)
    sid = _watching_session(db, monkeypatch, "zrc")

    _always_blocked(monkeypatch)
    for _ in range(4):
        tick_paper_session(db, sid, quote_fn=_qfn)
        db.commit()

    # isang malinis na tick => dapat mag-reset ang bilang
    monkeypatch.setattr(paper_runner, "runner_boundary_risk_ok", lambda _db, _s: (True, {}))
    tick_paper_session(db, sid, quote_fn=_qfn)
    db.commit()

    _always_blocked(monkeypatch)
    for _ in range(4):
        tick_paper_session(db, sid, quote_fn=_qfn)
        db.commit()

    sess = db.get(TradingAutomationSession, sid)
    assert sess.state != STATE_ERROR, (
        "8 block na may malinis na tick sa gitna ay hindi dapat magretiro -- "
        "hindi nag-reset ang bilang"
    )


def test_the_retirement_emits_an_event_that_names_the_reason(monkeypatch, db: Session) -> None:
    """Ang operator ay dapat makakita ng dahilan sa action history, hindi isang
    session na tahimik na nawala."""
    monkeypatch.setattr(settings, "chili_momentum_paper_risk_block_retire_after", 3)
    sid = _watching_session(db, monkeypatch, "zrd")
    _always_blocked(monkeypatch)
    for _ in range(3):
        tick_paper_session(db, sid, quote_fn=_qfn)
        db.commit()

    ev = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == sid,
            TradingAutomationEvent.event_type == "paper_retired_after_persistent_risk_block",
        )
        .one_or_none()
    )
    assert ev is not None, "ang pagretiro ay dapat may sariling event"
    payload = ev.payload_json or {}
    assert payload.get("consecutive_risk_blocks") == 3
    assert payload.get("retire_after") == 3
    assert payload.get("errors"), "dapat dala nito ang dahilan ng risk"


def test_zero_restores_the_old_never_retire_behaviour(monkeypatch, db: Session) -> None:
    """Ang knob ay may tunay na off switch -- mahalaga iyon kung ang pagretiro ay
    magdulot ng sorpresa sa produksyon."""
    monkeypatch.setattr(settings, "chili_momentum_paper_risk_block_retire_after", 0)
    sid = _watching_session(db, monkeypatch, "zre")
    _always_blocked(monkeypatch)
    for _ in range(12):
        tick_paper_session(db, sid, quote_fn=_qfn)
        db.commit()

    sess = db.get(TradingAutomationSession, sid)
    assert sess.state != STATE_ERROR, "ang 0 ay dapat hindi magretiro kailanman"


def test_the_block_event_still_carries_its_reason(monkeypatch, db: Session) -> None:
    """⚠️ Ang dahilan ay NASA DATOS na -- itinatago lang ito ng UI. Bantayan ito
    para hindi ito mawala habang inaayos ang display."""
    monkeypatch.setattr(settings, "chili_momentum_paper_risk_block_retire_after", 50)
    sid = _watching_session(db, monkeypatch, "zrf")
    _always_blocked(monkeypatch)
    tick_paper_session(db, sid, quote_fn=_qfn)
    db.commit()

    ev = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == sid,
            TradingAutomationEvent.event_type == "paper_blocked_by_risk",
        )
        .first()
    )
    assert ev is not None
    payload = ev.payload_json or {}
    assert payload.get("errors") == ["Not paper-eligible per neural viability."]
    assert payload.get("severity") == "block"


@pytest.mark.parametrize("value,expected", [(None, 20), (-5, 0), (999_999, 5000), (7, 7)])
def test_the_knob_is_clamped(monkeypatch, value, expected) -> None:
    """Ang mga hangganan ay tunay -- walang halagang makakalusot."""
    monkeypatch.setattr(
        settings, "chili_momentum_paper_risk_block_retire_after", value, raising=False
    )
    assert paper_runner._paper_risk_block_retire_after() == expected
