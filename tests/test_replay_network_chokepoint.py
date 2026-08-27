"""Isang bantay sa chokepoint ng HTTP, hindi dalawampu sa bawat tumatawag.

ANG PATTERN NA PAULIT-ULIT (2026-08-27). Ipinagbabawal ng replay ang lahat ng
network at itinuturing na NAKAMAMATAY kahit ang NALAMONG pagtatangka::

    ReplayNetworkAccessError: diagnostic ReplayV3 provider swallowed a
    forbidden network attempt

Sinasadya iyon: ang tahimik na nalamong tawag ay nangangahulugang lumihis ang
replay sa buhay na gawi nang walang nakakaalam. Ngunit halos BAWAT tumatawag sa
landas ng tick ay may ``except Exception`` sa paligid ng pagkuha nito, kaya LAHAT
sila ay lumalamon at LAHAT sila ay pumapatay sa takbo.

NASUKAT. Sa magkasunod na takbo ng nightly replay counterfactual::

    takbo 1  entry_gates._prior_day_close        -> get_last_quote
    takbo 2  catalyst.strong_catalyst_symbols    -> get_recent_news_items

...at may 20 pang lugar sa momentum_neural na nag-i-import mula sa
massive_client. Ang isa-isang pag-ayos ay whack-a-mole.

May DALAWA lamang na tunay na HTTP call site sa buong massive_client, at ang
pangalawa ay pagination na hindi umaabot kung walang unang pahina. Kaya ang isang
bantay sa ``_get`` ay humihinto sa lahat.

⚠️ ANG BALIK NA HALAGA AY ``None`` -- ang KAPAREHONG bagay na ibinabalik ng ``_get``
kapag walang API key o kapag bukas ang circuit breaker. Bawat tumatawag ay hawak
na ang landas na iyon; walang bagong kontrata.

PAGKATAPOS: ang XPON 13:00-14:00Z ay tumakbo mula dulo hanggang dulo -- 4 entry,
3 exit, PnL -27.10.

Runnable: pytest tests/test_replay_network_chokepoint.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import app.services.massive_client as MC
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.replay_capture_runtime import (
    replay_forbids_network,
)


def _replay():
    state = LR.DecisionRuntimeState()
    state.clock_domain = "replay_utc"
    return LR.decision_runtime_state(state)


# ── Ang helper ───────────────────────────────────────────────────────────────


def test_outside_a_replay_it_is_false():
    """Fail-open. Ang buhay na lane ay byte-identical."""
    assert replay_forbids_network() is False


def test_inside_a_replay_it_is_true():
    with _replay():
        assert replay_forbids_network() is True
    assert replay_forbids_network() is False, "dapat naibabalik sa paglabas"


def test_it_reads_live_runner_without_importing_it():
    """⚠️ Ang live_runner ang nag-i-import NG entry_gates at ng catalyst, kaya ang
    tuwirang import mula sa mga iyon ay magiging pabilog. Ang helper ay dapat
    tumingin sa sys.modules, hindi mag-import."""
    src = pathlib.Path(
        LR.__file__).parent.joinpath("replay_capture_runtime.py").read_text(
        encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "replay_forbids_network"
    )
    body = ast.unparse(fn)
    assert "sys.modules" in body or "_sys.modules" in body
    assert "from .live_runner" not in body
    assert "import live_runner" not in body


# ── Ang chokepoint ───────────────────────────────────────────────────────────


def test_the_http_chokepoint_returns_none_in_a_replay(monkeypatch):
    """ANG PANGUNAHING KASO. Walang HTTP sa loob ng replay, at ang balik ay ang
    kaparehong ``None`` na hawak na ng bawat tumatawag."""
    touched = []

    class _Boom:
        @staticmethod
        def get(*a, **k):
            touched.append(a)
            raise AssertionError("humipo ng network sa loob ng replay")

    monkeypatch.setattr(MC, "_session", _Boom, raising=False)
    monkeypatch.setattr(MC, "_api_key", lambda: "key-na-hindi-dapat-gamitin", raising=False)

    with _replay():
        assert MC._get("https://example.invalid/v1/thing") is None
    assert touched == []


def test_outside_a_replay_the_chokepoint_still_calls_out(monkeypatch):
    """⚠️ ANG BUHAY NA LANE AY HINDI NAGBABAGO. Ito ang bantay laban sa isang
    lunas na tahimik na pinapatay ang market data sa produksyon."""
    calls = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class _Sess:
        @staticmethod
        def get(url, **k):
            calls.append(url)
            return _Resp()

    monkeypatch.setattr(MC, "_session", _Sess, raising=False)
    monkeypatch.setattr(MC, "_api_key", lambda: "k", raising=False)
    monkeypatch.setattr(MC, "_breaker_allow_request", lambda: True, raising=False)
    monkeypatch.setattr(MC, "_rate_limit_wait", lambda: None, raising=False)
    monkeypatch.setattr(MC, "_breaker_record_success", lambda: None, raising=False)
    monkeypatch.setattr(MC, "_bump", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(MC, "_entitlement_denied_active", lambda *a, **k: False, raising=False)

    out = MC._get("https://example.invalid/v1/thing")
    assert out == {"ok": True}
    assert len(calls) == 1


def test_the_guard_precedes_the_api_key_check():
    """⚠️ AST, hindi text window. Ang pagkakasunod ay mahalaga: ang tseke ng
    replay ay dapat ang UNANG bagay sa `_get`, bago pa ang anumang bagay na
    maaaring mag-network o maghintay sa rate limiter."""
    src = pathlib.Path(MC.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_get"
    )
    guard_line = None
    session_line = None
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "replay_forbids_network"
        ):
            guard_line = n.lineno if guard_line is None else min(guard_line, n.lineno)
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "_session"
        ):
            session_line = n.lineno if session_line is None else min(session_line, n.lineno)
    assert guard_line is not None, "nawawala ang tseke ng replay sa _get"
    assert session_line is not None, "inaasahang _session.get sa _get"
    assert guard_line < session_line


def test_there_are_only_two_http_call_sites():
    """⚠️ BANTAY LABAN SA PAGLAGO. Ang lunas ay nakasalalay sa `_get` bilang ang
    chokepoint. Ang bagong `_session.get` sa ibang lugar ay isang bagong butas --
    ang pangalawa (pagination) ay hindi kailanman umaabot kung walang unang
    pahina, kaya nasasakop ito."""
    src = pathlib.Path(MC.__file__).read_text(encoding="utf-8")
    n = sum(
        1 for line in src.splitlines()
        if "_session.get(" in line and not line.strip().startswith("#")
    )
    assert n == 2, (
        "inaasahang 2 HTTP call site sa massive_client, nakita: %d. Ang bagong "
        "site ay kailangan ng sarili nitong bantay o dapat dumaan sa _get." % n
    )


@pytest.mark.parametrize("fn_name", [
    "get_last_quote",
    "get_recent_news_items",
])
def test_the_known_killers_are_neutralised_in_a_replay(monkeypatch, fn_name):
    """Ang dalawang tumatawag na tunay na pumatay sa nightly replay sa magkasunod
    na takbo. Wala sa mga ito ang dapat humipo ng network ngayon."""
    touched = []

    class _Boom:
        @staticmethod
        def get(*a, **k):
            touched.append(a)
            raise AssertionError("humipo ng network")

    monkeypatch.setattr(MC, "_session", _Boom, raising=False)
    monkeypatch.setattr(MC, "_api_key", lambda: "k", raising=False)

    fn = getattr(MC, fn_name)
    with _replay():
        try:
            fn("XPON") if fn_name == "get_last_quote" else fn(max_age_min=60)
        except TypeError:
            pytest.skip("naiba ang signature; sakop pa rin ng chokepoint")
    assert touched == [], "%s ay humipo ng network sa loob ng replay" % fn_name
