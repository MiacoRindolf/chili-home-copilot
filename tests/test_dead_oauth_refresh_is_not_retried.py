"""Ang patay na refresh token ay 20 segundo kada arm pass (2026-08-26).

Ang `invalid_grant` ay TERMINAL: sinasabi ng Robinhood na patay na ang refresh
token at hinding-hindi ito magtatagumpay hangga't hindi nagbabago ang kredensyal.
Walang ganoong kaalaman ang lumang kodigo, kaya ang bawat tumatawag ay sumusubok
ulit -- gaano man kadalas.

NASUKAT SA BUHAY NA LANE (2026-08-26)::

    2,439  na tinanggihang refresh sa isang log window
     22.4  kada auto-arm pass, sa 102 pass
    12:27:19 -> 12:27:38 sa loob ng ISANG pass = ~20 SEGUNDO ng HTTP na
             garantisadong mabibigo

⚠️ AT ANG LANE NA ITO AY HINDI GUMAGAMIT NG ROBINHOOD. Ang execution family ay
`alpaca_spot`, ang account scope ay `alpaca:paper`. Ang buong 20 segundo ay para
sa isang rail na hindi nagpapadala ng order dito.

ANG EPEKTO SA KALAKALAN: ang auto-arm ay naka-schedule kada 10s pero p50 25.8s
ang tagal, kaya 81 tick ang nalaktawan
(`skipped: maximum number of running instances reached`). Ang session ay
tinitick kada ~7 minuto, kaya luma na ang planadong limit sa pagpapadala at
hindi nakikita ng lane ang sariling fill (RDIB 9 @ 14.94 -- nanatiling
pre-entry ang tingin nito sa sarili sa buong hawak).

⚠️ HINDI NITO PINIPIGIL ANG PAGBAWI: ang TERMINAL lamang na pagtanggi ang
naka-cache. Ang timeout, 5xx, at network error ay muling sinusubukan gaya ng
dati.

Runnable: pytest tests/test_dead_oauth_refresh_is_not_retried.py -v
"""
from __future__ import annotations

import pytest

from app.services import broker_service as BS


@pytest.fixture(autouse=True)
def _clean():
    BS.reset_dead_refresh_tokens()
    yield
    BS.reset_dead_refresh_tokens()


class _Resp:
    def __init__(self, ok, status, body):
        self.ok, self.status_code, self._body = ok, status, body
        self.text = str(body)

    def json(self):
        return self._body


def _patch_post(monkeypatch, resp):
    calls = []

    class _Req:
        @staticmethod
        def post(url, data=None, timeout=None):
            calls.append(url)
            return resp

    import sys
    import types
    mod = types.ModuleType("requests")
    mod.post = _Req.post
    monkeypatch.setitem(sys.modules, "requests", mod)
    return calls


def test_a_terminal_rejection_is_tried_exactly_once(monkeypatch):
    """ANG PANGUNAHING KASO. Ang 22.4 na tawag kada pass ay dapat maging ISA."""
    calls = _patch_post(monkeypatch, _Resp(False, 401, {"error": "invalid_grant"}))
    for _ in range(25):
        assert BS._refresh_oauth_token("dead-token-abc") is None
    assert len(calls) == 1, "inaasahang ISANG HTTP na tawag, nakita: %d" % len(calls)


def test_the_token_is_remembered_as_dead(monkeypatch):
    _patch_post(monkeypatch, _Resp(False, 401, {"error": "invalid_grant"}))
    assert BS.refresh_token_is_known_dead("dead-token-abc") is False
    BS._refresh_oauth_token("dead-token-abc")
    assert BS.refresh_token_is_known_dead("dead-token-abc") is True


@pytest.mark.parametrize("err", ["invalid_grant", "invalid_request", "unauthorized_client"])
def test_every_terminal_error_code_breaks_the_circuit(monkeypatch, err):
    calls = _patch_post(monkeypatch, _Resp(False, 400, {"error": err}))
    for _ in range(5):
        BS._refresh_oauth_token("t-%s" % err)
    assert len(calls) == 1


@pytest.mark.parametrize("status,body", [
    (500, {"error": "server_error"}),
    (503, {"error": "temporarily_unavailable"}),
    (429, {"error": "rate_limited"}),
    (502, "gateway"),
])
def test_a_TRANSIENT_failure_is_still_retried(monkeypatch, status, body):
    """⚠️⚠️ ANG DIREKSYON NG KALIGTASAN. Ang breaker ay hindi dapat mag-cache ng
    lumilipas na pagkabigo -- iyon ay magpapatay ng isang BUHAY na kredensyal
    dahil sa isang sandaling pagkakamali ng network."""
    calls = _patch_post(monkeypatch, _Resp(False, status, body))
    for _ in range(4):
        BS._refresh_oauth_token("live-token")
    assert len(calls) == 4, "ang lumilipas na pagkabigo ay dapat muling subukan"
    assert BS.refresh_token_is_known_dead("live-token") is False


def test_a_different_token_is_unaffected(monkeypatch):
    """Ang bagong kredensyal ay hindi kailanman naaapektuhan ng luma."""
    calls = _patch_post(monkeypatch, _Resp(False, 401, {"error": "invalid_grant"}))
    BS._refresh_oauth_token("old-dead")
    assert BS.refresh_token_is_known_dead("new-fresh") is False
    BS._refresh_oauth_token("new-fresh")
    assert len(calls) == 2, "ang bawat natatanging token ay nakakakuha ng sariling pagsubok"


def test_the_reset_reopens_the_circuit(monkeypatch):
    """Matapos mag-relink ay dapat muling subukan."""
    calls = _patch_post(monkeypatch, _Resp(False, 401, {"error": "invalid_grant"}))
    BS._refresh_oauth_token("t1")
    assert BS.reset_dead_refresh_tokens() == 1
    BS._refresh_oauth_token("t1")
    assert len(calls) == 2


def test_the_fingerprint_never_contains_the_token():
    """⚠️ Ang cache key ay hindi kailanman dapat maglantad ng kredensyal."""
    secret = "super-secret-refresh-token-value"
    fp = BS._refresh_token_fingerprint(secret)
    assert secret not in fp
    assert len(fp) == 16
    assert fp == BS._refresh_token_fingerprint(secret), "dapat matatag"


def test_the_knob_reverts_it(monkeypatch):
    """Gawi bago ang 2026-08-26, nang walang deploy."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_broker_dead_refresh_breaker_enabled", False, raising=False)
    calls = _patch_post(monkeypatch, _Resp(False, 401, {"error": "invalid_grant"}))
    for _ in range(4):
        BS._refresh_oauth_token("t2")
    assert len(calls) == 4


def test_a_success_still_works(monkeypatch):
    """Ang masayang landas ay hindi ginagalaw."""
    _patch_post(monkeypatch, _Resp(True, 200, {"access_token": "ok", "expires_in": 86400}))
    out = BS._refresh_oauth_token("good-token")
    assert isinstance(out, dict) and out.get("access_token") == "ok"
    assert BS.refresh_token_is_known_dead("good-token") is False
