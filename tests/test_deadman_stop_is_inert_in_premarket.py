"""Ang deadman stop ay INERT sa premarket -- at hindi ito sinasabi (2026-08-26).

ANG PUWANG. Ang deadman ay isinusumite bilang ``order_type="stop"``,
``time_in_force="gtc"``, ``extended_hours=False``. Ang Alpaca ay tumatanggap
LAMANG ng ``limit`` na order sa extended hours, kaya ang isang ``stop`` ay
hinding-hindi lalahok doon -- nakaupo ito sa ``status="new"`` at nagiging buhay
lamang sa susunod na regular open.

NASUKAT SA ISANG BUHAY NA POSISYON (session 16534, CDTG, 2026-08-26)::

    11:41:24  live_deadman_stop_placed   {"ok": true, "stop_price": 1.31}
    11:47Z    broker: CDTG sell 191 stop=1.31 status=new ext=False
              presyo 1.26  -- LIMA NA SENTIMO SA IBABA NG STOP, hindi pumutok
              unrealized -$21.00 (-8.03%)

⚠️ ANG PANGANIB AY HINDI ANG INERT NA ORDER -- ITO AY ANG ULAT. Ang event ay
nagsasabing ``ok: true`` katabi ng presyo ng stop, kaya ang bawat magbabasa --
tao man o makina -- ay maniniwalang protektado ang posisyon. Ganoon nga ang
nangyari: binasa ang event, pinaniwalaang may proteksyon, at ang posisyon ay
lumagpas sa stop nito nang walang anumang pumutok.

Sa extended hours ang runner LAMANG ang proteksyon. Kapag patay ang runner ay
wala talaga. Iyon ay maaaring tanggapin -- pero dapat itong SABIHIN.

Runnable: pytest tests/test_deadman_stop_is_inert_in_premarket.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


class _Sess:
    symbol = "CDTG"


@pytest.fixture
def session_is(monkeypatch):
    def _set(value):
        import app.services.trading.momentum_neural.market_profile as MP
        monkeypatch.setattr(MP, "market_session_now",
                            lambda *a, **k: value, raising=False)
    return _set


def test_a_premarket_deadman_reports_itself_as_not_live(session_is):
    """ANG PANGUNAHING KASO -- ang eksaktong kalagayan ng CDTG nang 11:41Z."""
    session_is("premarket")
    live, ev = LR._deadman_protection_is_live(_Sess())
    assert live is False
    assert ev["market_session"] == "premarket"
    assert ev["broker_stop_can_trigger"] is False
    assert ev["sole_protection"] == "runner_software_stop"


def test_a_regular_session_deadman_is_live(session_is):
    """Sa RTH ay tunay na proteksyon ito, at dapat ganoon ang sabihin."""
    session_is("regular")
    live, ev = LR._deadman_protection_is_live(_Sess())
    assert live is True
    assert ev["sole_protection"] == "broker_stop"


@pytest.mark.parametrize("session", [
    "premarket", "afterhours", "postmarket", "closed", "overnight", "europe", "asia",
])
def test_every_non_regular_session_is_treated_as_not_live(session_is, session):
    """⚠️ Ang bawat window maliban sa regular ay may parehong depekto -- walang
    stop na lalahok sa labas ng RTH."""
    session_is(session)
    assert LR._deadman_protection_is_live(_Sess())[0] is False


def test_an_unclassifiable_session_fails_SUSPICIOUS(monkeypatch):
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Kapag hindi maiuri ang session, ang tamang
    sagot ay HINDI protektado -- ang maling pag-aakalang protektado ang eksaktong
    pagkakamaling pinipigilan nito."""
    import app.services.trading.momentum_neural.market_profile as MP

    def _boom(*a, **k):
        raise RuntimeError("walang calendar")

    monkeypatch.setattr(MP, "market_session_now", _boom, raising=False)
    live, ev = LR._deadman_protection_is_live(_Sess())
    assert live is False
    assert ev["market_session"] == "unknown"


def test_the_placed_event_carries_protection_active():
    """ANG BANTAY SA MISMONG KASINUNGALINGAN. Ang `live_deadman_stop_placed` ay
    hindi na dapat lumabas nang walang tahasang sagot kung makakaputok ba ito."""
    src = _SRC.read_text(encoding="utf-8")
    idx = src.find('"live_deadman_stop_placed"')
    assert idx > 0, "dapat umiiral ang event"
    # ⚠️ Ang tawag ay nauuna sa emit, kaya kailangang tingnan ang MAGKABILANG
    # panig -- ang isang forward-only na window ay tahimik na papalya rito.
    window = src[max(0, idx - 400): idx + 700]
    assert '"protection_active"' in window, (
        "ang naipadalang deadman ay dapat nag-uulat kung makakaputok ba ito, "
        "hindi lamang kung naipadala ba")
    assert "_deadman_protection_is_live(" in window


def test_the_inert_case_gets_its_own_event():
    """Ang isang naka-place na deadman na hindi makakaputok ay ibang KALAGAYAN,
    hindi isang detalye ng isang matagumpay na paglalagay."""
    src = _SRC.read_text(encoding="utf-8")
    assert '"live_deadman_stop_inert_until_rth"' in src
    assert 'le["deadman_inert"]' in src


def test_the_inert_flag_is_cleared_when_protection_becomes_live():
    """⚠️ Ang isang nakadikit na babala ay kasing-mapanlinlang ng nawawalang
    babala."""
    src = _SRC.read_text(encoding="utf-8")
    assert 'le.pop("deadman_inert", None)' in src


def test_the_order_shape_itself_is_unchanged():
    """⚠️ HINDI KO INAYOS ANG HUGIS NG ORDER, at hindi dapat sabihin ng testong
    ito na inayos. Ang Alpaca ay walang extended-hours na stop na order type --
    ang limit LAMANG ang lumalahok doon. Ang pagpapalit ng disaster floor sa
    isang marketable limit ay ibang pasya na may sariling panganib (agad itong
    magbebenta sa halip na sa trigger). Ang naayos rito ay ang PAG-UULAT."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_ensure_alpaca_deadman_stop")
    body = "\n".join(_SRC.read_text(encoding="utf-8").splitlines()[fn.lineno - 1: fn.end_lineno])
    assert '"order_type": "stop"' in body
    assert '"extended_hours": False' in body
