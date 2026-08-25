"""Ang IQFeed L2 depth ay maaaring tumayong panandalian sa entry BBO (2026-08-25).

ANG NASIRANG PREMISE. Ganito ang sabi ng docstring ng ``get_execution_bbo``::

    "IQFeed Q/reference rows still cannot stand in: they carry no quote-event
     clock at all, and a trade-time proxy cannot authorize an order."

Totoo iyon noong isinulat. Idinagdag ng **migration 371** ang ``provider_at`` sa
``iqfeed_depth_snapshots``, pinar-parse ng depth bridge mula sa SARILING date+time
field ng L2 line -- isang tunay na quote-event clock, hindi trade-time proxy. At
isinusulat ng bridge ang **MAS LUMANG binti** ng pares, kaya ang isang BBO ay hindi
kailanman lilitaw na mas sariwa kaysa sa pinakamatanda nitong bahagi.

NASUKAT (2026-08-25, buhay na RTH, bounded 5 min)::

    bridge lag (observed_at - provider_at):  p10 0.51s   p50 7.03s   p90 63.96s
    per-simbolo na pinakabagong quote:       102 sa 121 (84%) ay <60s
    median na edad kada simbolo:             8.8s

Ang account ay may karapatan sa **IEX lamang**. Ang IQFeed ay 26-39 venue.

⚠️ ANG ARI-ARIANG PANGKALIGTASAN, at ito ang pinakamahalagang test dito: ENTRY-ONLY.
Isang mas malawak na pinagsama-samang bid ay HAHATOL na marketable ang isang exit --
o magpepresyo nito -- sa antas na hindi kayang abutin ng venue, na ginagawang
"pumasok at na-stuck" ang dating "walang entry". Ang exit ay hindi kailanman
nagpapasa ng ``allow_stand_in``, at may bantay dito.

Runnable: pytest tests/test_iqfeed_depth_execution_standin.py -v
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services.trading.venue.alpaca_spot import (
    _IQFEED_DEPTH_BASIS,
    AlpacaSpotAdapter,
)

_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "venue" / "alpaca_spot.py"
)


@pytest.fixture()
def adapter():
    return AlpacaSpotAdapter.__new__(AlpacaSpotAdapter)


def _no_direct(monkeypatch, adapter):
    """Walang magamit na direktang Alpaca quote -- ang premarket/IEX na kaso."""
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_alpaca_latest_quote", lambda self, pid: (None, None)
    )
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_execution_bbo_from_direct",
        lambda self, tick, meta, cap: None,
    )


def _no_sip(monkeypatch):
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_massive_sip_execution_bbo", lambda self, pid, age: None
    )


_SENTINEL_IQFEED = ("IQFEED_TICK", "IQFEED_META")
_SENTINEL_SIP = ("SIP_TICK", "SIP_META")


def test_the_iqfeed_tier_fires_when_direct_and_sip_are_both_empty(monkeypatch, adapter):
    """ANG PANGUNAHING KASO -- ang premarket/IEX na butas."""
    _no_direct(monkeypatch, adapter)
    _no_sip(monkeypatch)
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_execution_bbo",
        lambda self, pid, age: _SENTINEL_IQFEED,
    )
    got = adapter.get_execution_bbo("AAPL", max_age_seconds=2.0, allow_stand_in=True)
    assert got == _SENTINEL_IQFEED


def test_the_EXIT_can_never_see_the_stand_in(monkeypatch, adapter):
    """⚠️ ANG ARI-ARIANG PANGKALIGTASAN. Ang isang exit ay hindi nagpapasa ng
    allow_stand_in, at hindi ito dapat makakita ng IQFeed na hilera kahit sariwa
    ito -- ang isang pinagsamang bid ay hahatol na marketable ang isang exit sa
    antas na hindi kayang abutin ng venue."""
    _no_direct(monkeypatch, adapter)
    _no_sip(monkeypatch)
    called = []
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_execution_bbo",
        lambda self, pid, age: called.append(pid) or _SENTINEL_IQFEED,
    )
    tick, meta = adapter.get_execution_bbo("AAPL", max_age_seconds=2.0)
    assert tick is None, "ang exit na daan ay hindi dapat makakuha ng stand-in tick"
    assert called == [], "hindi dapat man lang ito tinatanong ng exit na daan"


def test_the_MASSIVE_SIP_tier_still_wins(monkeypatch, adapter):
    """⚠️ WALANG REGRESSION. Ang SIP ay ang pinagsama-samang tape at may mas
    mahigpit na kontrata; ito pa rin ang nauuna."""
    _no_direct(monkeypatch, adapter)
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_massive_sip_execution_bbo",
        lambda self, pid, age: _SENTINEL_SIP,
    )
    iqfeed_called = []
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_execution_bbo",
        lambda self, pid, age: iqfeed_called.append(pid) or _SENTINEL_IQFEED,
    )
    got = adapter.get_execution_bbo("AAPL", max_age_seconds=2.0, allow_stand_in=True)
    assert got == _SENTINEL_SIP
    assert iqfeed_called == [], "hindi dapat naabot ang IQFeed kapag may SIP"


def test_the_DIRECT_quote_still_wins_over_everything(monkeypatch, adapter):
    """Ang authority ay hindi lumilipat -- ang direktang Alpaca quote ang una."""
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_alpaca_latest_quote", lambda self, pid: (None, None)
    )
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_execution_bbo_from_direct",
        lambda self, tick, meta, cap: ("DIRECT_TICK", "DIRECT_META"),
    )
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_massive_sip_execution_bbo",
        lambda self, pid, age: _SENTINEL_SIP,
    )
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_execution_bbo",
        lambda self, pid, age: _SENTINEL_IQFEED,
    )
    got = adapter.get_execution_bbo("AAPL", max_age_seconds=2.0, allow_stand_in=True)
    assert got == ("DIRECT_TICK", "DIRECT_META")


def test_crypto_is_refused(monkeypatch, adapter):
    """Ang IQFeed depth ay tape ng equity; ang crypto ay hindi dapat dumaan dito."""
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_quote",
        lambda self, sym, *, max_age_seconds: _SENTINEL_IQFEED,
    )
    assert adapter._iqfeed_depth_execution_bbo("BTC-USD", 20.0) is None


def test_the_flag_off_restores_the_two_tier_behaviour(monkeypatch, adapter):
    """Ang knob ay may tunay na off switch."""
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_iqfeed_depth_fallback_enabled",
        False, raising=False,
    )
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_quote",
        lambda self, sym, *, max_age_seconds: _SENTINEL_IQFEED,
    )
    assert adapter._iqfeed_depth_execution_bbo("AAPL", 20.0) is None


def test_a_malformed_quote_result_is_refused(monkeypatch, adapter):
    """Ang mali ang hugis na resulta ay hindi dapat makapag-authorize ng order."""
    for bad in (None, "nope", (), ("only-one",), (None, None)):
        monkeypatch.setattr(
            AlpacaSpotAdapter, "_iqfeed_depth_quote",
            lambda self, sym, *, max_age_seconds, _b=bad: _b,
        )
        assert adapter._iqfeed_depth_execution_bbo("AAPL", 20.0) is None


def test_a_non_FreshnessMeta_is_refused(monkeypatch, adapter):
    """⚠️ Ang meta ang nagdadala ng quote clock. Kung hindi ito tunay na
    FreshnessMeta ay walang mapagkakatiwalaang edad, kaya walang authority."""
    monkeypatch.setattr(
        AlpacaSpotAdapter, "_iqfeed_depth_quote",
        lambda self, sym, *, max_age_seconds: ("tick", {"provider_time_utc": None}),
    )
    assert adapter._iqfeed_depth_execution_bbo("AAPL", 20.0) is None


def test_the_tier_has_its_OWN_ceiling_not_the_direct_cap():
    """⚠️ ANG ARAL NG AUTHORITY-AWARE CEILING. Ang tape na ito ay may 7s na median
    na bridge lag; ang pagsukat dito sa 2.0s na cap ng direktang quote ay
    nangangahulugang hindi ito kailanman papuputok."""
    ceiling = settings.chili_alpaca_execution_bbo_iqfeed_depth_max_age_seconds
    assert ceiling > 2.0, "ang sariling ceiling ay dapat mas maluwag kaysa sa direct cap"
    assert ceiling <= 60.0, "at dapat mas mahigpit pa rin kaysa sa 60s na entry ceiling"


def test_the_reader_enforces_both_clocks_and_a_sane_book():
    """BANTAY (AST). Ang tatlong pagsusuri na pumipigil sa isang na-replay o sirang
    hilera na mag-authorize ng order ay dapat manatili sa reader."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_iqfeed_depth_quote"
    )
    body = "\n".join(src.splitlines()[fn.lineno - 1: fn.end_lineno])
    assert "provider_age" in body and "received_age" in body, (
        "ang DALAWANG orasan ay dapat parehong sinusuri"
    )
    assert "ask < bid" in body, "ang tumawid na libro ay dapat tanggihan"
    assert "FUTURE_TOLERANCE" in body, "ang hinaharap na orasan ay dapat may hangganan"
    assert "provider_at IS NOT NULL" in body, (
        "ang isang hilerang walang quote clock ay hindi dapat kailanman piliin"
    )


def test_the_stale_docstring_premise_was_corrected():
    """⚠️ Ang docstring ang nagdala ng premise na 'walang quote-event clock ang
    IQFeed'. Ang pag-iwan niyon ay magtuturo sa susunod na magbabasa nang mali."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "get_execution_bbo"
    )
    doc = ast.get_docstring(fn) or ""
    assert "371" in doc, "dapat banggitin ng docstring kung ano ang nagpabago ng premise"
    assert "STALE" in doc.upper()


def test_the_basis_string_names_the_clock_source():
    """Ang bawat stand-in ay nagsasabi kung SAAN galing ang orasan nito."""
    assert _IQFEED_DEPTH_BASIS == "iqfeed_l2_provider_at"
