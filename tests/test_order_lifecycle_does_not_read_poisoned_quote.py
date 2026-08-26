"""Ang quote na KUMAKANSELA ng buhay na order (2026-08-26).

ANG PUWANG. Ang PR na nauna rito ay nagruta ng PRE-ENTRY na *tick* palayo sa
hilaw na ``adapter.get_best_bid_ask``. Dalawa pang call site ang naiwan -- at ang
dalawang iyon ang nagpapasya kung **kakanselahin ang isang nakapatong nang
order**::

    _pending_entry_cancel_reason   (ang pending-entry na lifecycle)
    ang inline micro-repeg na re-read

NASUKAT SA ISANG BUHAY NA ORDER (session 16510, RDIB, 2026-08-26)::

    11:19:05  live_entry_final_bbo    bid 12.66 / ask 12.78
                                      source=massive_ws_universe        -- TAMA
    11:19:12  live_entry_submitted    limit 12.54, status=open, RTT 0.078s
    11:19:21  entry_ack_timeout       reason=entry_invalidated_stop_breach
                                      bid=7.31, structural_stop=12.39

⚠️ ANG 7.31 AY HINDI PRESYO NG UMAGANG IYON. Ito ang eksaktong bid na ibinabalik
ng hilaw na landas para sa RDIB, na may `provider_time` 2026-08-25 20:00 -- 15
ORAS ang tanda, katabi ng 20.75 na ask (9,579 bps). Sa parehong sandali ay
ibinabalik ng stand-in ang 12.38/12.40 sa 0.24s.

Nag-imbento ang engine ng stop breach mula sa saradong libro ng kahapon at
kinansela ang sarili nitong buhay na order 9 SEGUNDO matapos itong ipadala. Hindi
"walang trade si CHILI ngayong araw" -- may trade, at pinatay ito ng lasong quote.

⚠️ MAS LIGTAS ITO SA MAGKABILANG DIREKSYON. Ang `_pending_entry_cancel_reason` ay
nagbabalik ng None kapag `bid is None` (manatiling nakapatong), kaya ang WALANG
mapagkakatiwalaang quote ay nangangahulugang "huwag kumansela batay sa ebidensyang
wala tayo" -- samantalang ang dating gawi ay kumakansela batay sa ebidensyang
GAWA-GAWA.

Runnable: pytest tests/test_order_lifecycle_does_not_read_poisoned_quote.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


class _Adapter:
    def __init__(self):
        self.ordinary_calls = 0

    def get_best_bid_ask(self, product_id):
        self.ordinary_calls += 1
        return "POISONED_STALE_QUOTE", "ordinary_freshness"


class _Tick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask
        self.freshness = "fresh_meta"


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def _fake(adapter, product_id, *, max_age_seconds,
              allow_stand_in=False, stand_in_max_age_seconds=None):
        calls.append({"allow_stand_in": allow_stand_in,
                      "max_age_seconds": max_age_seconds})
        return _fake.results.pop(0)

    _fake.results = []
    monkeypatch.setattr(LR, "_final_entry_bbo", _fake)
    return calls, _fake


def test_the_RDIB_cancel_could_not_happen_again(spy):
    """ANG BUHAY NA KASO. Ang hilaw na landas ay nagbibigay ng 7.31 (breach sa
    12.39 na stop); ang stand-in ay nagbibigay ng 12.38 (walang breach)."""
    calls, fake = spy
    fake.results = [(None, {"ok": False}), (_Tick(12.38, 12.40), {"ok": True})]
    a = _Adapter()
    tick, fr = LR._lifecycle_bid_ask(a, "RDIB", execution_family="alpaca_spot")
    assert a.ordinary_calls == 0, "hindi kailanman ang lasong landas"
    assert tick.bid == 12.38
    assert LR._pending_entry_cancel_reason(
        bid=tick.bid, structural_stop=12.39 - 0.01, limit_px=12.54,
        elapsed_s=1.0, rest_bars=2.0, interval_s=60.0) != "entry_invalidated_stop_breach"


def test_the_poisoned_bid_would_have_fabricated_the_breach():
    """ANG KONTROL. Pinapatunayan nitong ang 7.31 ang gumawa ng breach -- kaya
    ang tanong ay TALAGANG tungkol sa pinagmulan ng quote, hindi sa gate."""
    assert LR._pending_entry_cancel_reason(
        bid=7.31, structural_stop=12.39, limit_px=12.54,
        elapsed_s=1.0, rest_bars=2.0, interval_s=60.0
    ) == "entry_invalidated_stop_breach"


def test_no_trustworthy_quote_means_KEEP_RESTING_not_cancel(spy):
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Kapag walang naibigay ang mahigpit AT ang
    stand-in, ang tama ay huwag kumansela -- hindi kumansela sa wala."""
    calls, fake = spy
    fake.results = [(None, {"ok": False}), (None, {"ok": False})]
    tick, fr = LR._lifecycle_bid_ask(_Adapter(), "RDIB",
                                     execution_family="alpaca_spot")
    assert tick is None
    assert LR._pending_entry_cancel_reason(
        bid=None, structural_stop=12.39, limit_px=12.54,
        elapsed_s=1.0, rest_bars=2.0, interval_s=60.0) is None


def test_a_live_direct_book_still_wins(spy):
    """RTH: walang nagbabago."""
    calls, fake = spy
    fake.results = [(_Tick(6.42, 6.43), {"ok": True})]
    a = _Adapter()
    tick, fr = LR._lifecycle_bid_ask(a, "DAIC", execution_family="alpaca_spot")
    assert tick.bid == 6.42
    assert len(calls) == 1 and calls[0]["allow_stand_in"] is False
    assert a.ordinary_calls == 0


@pytest.mark.parametrize("family", ["coinbase_spot", "robinhood_spot", "", None])
def test_non_alpaca_is_byte_identical(spy, family):
    calls, fake = spy
    a = _Adapter()
    tick, fr = LR._lifecycle_bid_ask(a, "BTC-USD", execution_family=family)
    assert tick == "POISONED_STALE_QUOTE"
    assert fr == "ordinary_freshness"
    assert a.ordinary_calls == 1
    assert calls == []


def test_the_knob_reverts_it(spy, monkeypatch):
    calls, fake = spy
    monkeypatch.setattr(LR.settings, "chili_momentum_preentry_stand_in_enabled",
                        False, raising=False)
    fake.results = [(None, {"ok": False})]
    tick, fr = LR._lifecycle_bid_ask(_Adapter(), "RDIB",
                                     execution_family="alpaca_spot")
    assert tick is None
    assert len(calls) == 1


def test_no_raw_quote_read_survives_inside_tick_live_session():
    """⚠️⚠️ ANG BANTAY. Ito ang PANGALAWANG beses na nakaligtaan ang isang call
    site: inayos ng naunang PR ang `_live_tick_bbo` at naiwan ang dalawang ito,
    at ang naiwan ang siyang pumatay ng tunay na order. Ang natitirang hilaw na
    tawag ay dapat nasa loob LANG ng dalawang ruter."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    routers = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in (
            "_live_tick_bbo", "_lifecycle_bid_ask"
        ):
            routers[n.name] = (n.lineno, n.end_lineno)
    assert set(routers) == {"_live_tick_bbo", "_lifecycle_bid_ask"}

    stray = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == "get_best_bid_ask"):
            continue
        if getattr(f.value, "id", None) != "adapter":
            continue
        if any(lo <= n.lineno <= hi for lo, hi in routers.values()):
            continue
        stray.append(n.lineno)
    assert stray == [], (
        "linya %r: hilaw na `adapter.get_best_bid_ask` sa labas ng dalawang "
        "ruter. Sa premarket ay nagbabalik ito ng saradong libro ng kahapon "
        "bilang buhay -- ganito nakansela ang RDIB order noong 11:19." % (stray,))


def test_both_lifecycle_sites_pass_the_execution_family():
    """Ang ruter ay walang magagawa kung hindi nito alam ang pamilya."""
    src = _SRC.read_text(encoding="utf-8")
    assert src.count("_lifecycle_bid_ask(") >= 3, (
        "kahulugan + dalawang call site")
    assert src.count("execution_family=ef") >= 3, (
        "ang dalawang lifecycle site at ang tick site ay dapat pumapasa ng `ef`")
