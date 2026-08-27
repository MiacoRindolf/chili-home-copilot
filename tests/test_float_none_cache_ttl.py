"""Ang None sa float cache ay hindi na habambuhay (2026-08-27).

ANG INSIDENTE: pagkatapos ng 18:31Z lane restart, 37 minutong ZERO arms sa
2,226 ignition attempts. Mekanismo: walang laman ang float cache pagkatapos ng
restart, ang burst ng lookups sa ilalim ng rate pressure ay nagka-cache ng
None HABAMBUHAY kada simbolo, at ang leg-4 ng A-setup quality floor ay
fail-closed sa missing float ⇒ live_eligible=false hanggang restart. Ang
lunas: ang tagumpay ay process-lifetime pa rin, pero ang None ay may 300s TTL
para makapag-retry.

Runnable: pytest tests/test_float_none_cache_ttl.py -v
"""
from __future__ import annotations

from app.services import massive_client as MC


def _reset():
    MC._FLOAT_CACHE.clear()
    MC._FLOAT_NONE_AT.clear()


def test_a_transient_failure_is_retried_after_the_ttl(monkeypatch):
    """ANG PANGUNAHING KASO: unang tawag nabigo (None), pagkatapos ng TTL ang
    susunod na tawag ay TUMATAWAG ULIT sa provider at nakakakuha ng totoong
    halaga."""
    _reset()
    calls = []

    def _fake_get(url, params):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("rate limited")
        return {"results": {"share_class_shares_outstanding": 4_990_000}}

    monkeypatch.setattr(MC, "_get", _fake_get)
    assert MC.get_ticker_float("ONDS") is None
    assert len(calls) == 1
    # bago mag-expire: walang bagong provider call
    assert MC.get_ticker_float("ONDS") is None
    assert len(calls) == 1
    # i-expire ang None
    MC._FLOAT_NONE_AT["ONDS"] = MC._FLOAT_NONE_AT["ONDS"] - (
        MC._FLOAT_NONE_TTL_SEC + 1.0)
    assert MC.get_ticker_float("ONDS") == 4_990_000.0
    assert len(calls) == 2


def test_a_successful_value_stays_for_the_process_lifetime(monkeypatch):
    """Ang share count ay ~static — ang tagumpay ay hindi nag-e-expire."""
    _reset()
    calls = []

    def _fake_get(url, params):
        calls.append(url)
        return {"results": {"share_class_shares_outstanding": 1_000_000}}

    monkeypatch.setattr(MC, "_get", _fake_get)
    assert MC.get_ticker_float("JFB") == 1_000_000.0
    assert MC.get_ticker_float("JFB") == 1_000_000.0
    assert len(calls) == 1
    assert "JFB" not in MC._FLOAT_NONE_AT, "walang None stamp ang tagumpay"


def test_a_young_none_does_not_hammer_the_provider(monkeypatch):
    """⚠️ Ang TTL ay hindi dapat maging bawat-tawag na retry — sa loob ng TTL,
    zero provider calls (rate-limit protection ang orihinal na layunin ng
    cache)."""
    _reset()
    calls = []

    def _fake_get(url, params):
        calls.append(url)
        raise RuntimeError("down")

    monkeypatch.setattr(MC, "_get", _fake_get)
    for _ in range(5):
        assert MC.get_ticker_float("TJGC") is None
    assert len(calls) == 1


def test_the_incident_is_recorded_at_the_cache():
    import inspect

    src = inspect.getsource(MC.get_ticker_float)
    mod_src = open(MC.__file__, encoding="utf-8").read()
    assert "_FLOAT_NONE_TTL_SEC" in src
    assert "2026-08-27" in mod_src.split("_FLOAT_NONE_AT")[0].rsplit(
        "_FLOAT_CACHE", 2)[-1] or "2026-08-27" in mod_src, (
        "dapat nakatala ang insidente sa komento ng cache"
    )
