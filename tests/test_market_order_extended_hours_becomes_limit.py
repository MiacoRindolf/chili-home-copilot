"""Ang market exit sa extended hours ay dapat maging MARKETABLE LIMIT.

⚠️ HINDI TUMATANGGAP ANG ALPACA NG MARKET ORDER SA LABAS NG RTH. Limit + DAY +
`extended_hours=true` lamang.

NAPATUNAYAN SA BUHAY NA ACCOUNT (2026-08-25, BDRX)::

    11:16:09  market sell  ext_hrs=False  status=canceled
    11:15:56  market sell  ext_hrs=False  status=canceled
    ... (pito lahat, lahat canceled)
    11:25:57  limit  sell  ext_hrs=True   status=FILLED  880 @ 1.48

Ang IISANG order na na-fill ay ang tanging limit na may `extended_hours=True`.

## Ang bug

Ang `place_market_order` ay may `**_ignored` sa dulo ng signature nito, at doon
tahimik na nahuhulog ang `extended_hours` -- hindi ito kailanman naaabot ang
`_submit`, na TUMATANGGAP naman nito (`extended_hours: bool = False`).

Maingat itong kinukwenta ng exit path sa `live_runner.py`::

    if _exit_extended and _exit_family in ALPACA_EXECUTION_FAMILIES:
        _ext_kwargs = {"extended_hours": True}

at ang komento sa itaas niyon ay nagbababala nang eksakto:

    "...a premarket/after-hours STOP-OUT could not flatten -> naked stranded
     long (the exact AHMA/SMCX class) ... Mirrors the entry-side fix."

Ginawa ang ayos. **Hindi ito umabot sa adapter.** Kaya ang posisyong nasa
premarket ay hindi kayang i-flatten ng lane -- ang mismong bitag na
ipinapangalagaan sana ng nakasulat na babala.

Runnable: pytest tests/test_market_order_extended_hours_becomes_limit.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.venue import alpaca_spot as AS


class _Ticker:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


@pytest.fixture
def adapter(monkeypatch):
    a = AS.AlpacaSpotAdapter.__new__(AS.AlpacaSpotAdapter)
    seen = {}

    def _fake_submit(product_id, side, base_size, client_order_id, **kw):
        seen.clear()
        seen.update({"product_id": product_id, "side": side, "base_size": base_size, **kw})
        return {"ok": True, "order_id": "x"}

    monkeypatch.setattr(a, "_submit", _fake_submit, raising=False)
    monkeypatch.setattr(a, "get_best_bid_ask", lambda pid: (_Ticker(1.50, 1.52), "fresh"),
                        raising=False)
    return a, seen


def test_regular_hours_is_unchanged(adapter):
    """⚠️ WALANG NAGBABAGO SA RTH. Market order pa rin, walang limit price."""
    a, seen = adapter
    a.place_market_order(product_id="BDRX", side="sell", base_size="880")
    assert seen["limit_price"] is None
    assert "extended_hours" not in seen or seen.get("extended_hours") is False


def test_extended_hours_becomes_a_marketable_limit(adapter):
    """Ang tunay na ayos: limit na tumatawid, hindi market."""
    a, seen = adapter
    a.place_market_order(product_id="BDRX", side="sell", base_size="880",
                         extended_hours=True)
    assert seen["extended_hours"] is True, "kailangan ito ng Alpaca sa labas ng RTH"
    assert seen["limit_price"] is not None, "market order ay tatanggihan"
    # sell: tumatawid PABABA sa bid para matiyak ang fill
    assert seen["limit_price"] == pytest.approx(1.50 * (1 - 0.015))
    assert seen["limit_price"] < 1.50
    assert str(seen["time_in_force"]).lower() == "day", "GTC ay hindi tinatanggap sa ext hours"


def test_a_buy_crosses_up_through_the_ask(adapter):
    """Ang cover/buy ay dapat tumawid PATAAS, hindi pababa."""
    a, seen = adapter
    a.place_market_order(product_id="BDRX", side="buy", base_size="880",
                         extended_hours=True)
    assert seen["limit_price"] == pytest.approx(1.52 * (1 + 0.015))
    assert seen["limit_price"] > 1.52


def test_an_explicit_price_is_honoured(adapter):
    """Kung may ibinigay na presyo ang tumatawag ay iyon ang gagamitin."""
    a, seen = adapter
    a.place_market_order(product_id="BDRX", side="sell", base_size="880",
                         extended_hours=True, limit_price=1.40)
    assert seen["limit_price"] == pytest.approx(1.40)


def test_no_usable_book_fails_closed_instead_of_sending_a_doomed_order(monkeypatch):
    """⚠️ FAIL-CLOSED. Kung walang mapagkukunang presyo ay mas mabuting magbalik
    ng malinaw na error kaysa magpadala ng order na ALAM NATING tatanggihan --
    ang tahimik na `canceled` ang eksaktong bitag na inaayos nito."""
    a = AS.AlpacaSpotAdapter.__new__(AS.AlpacaSpotAdapter)
    called = []
    monkeypatch.setattr(a, "_submit", lambda *x, **k: called.append(k), raising=False)
    monkeypatch.setattr(a, "get_best_bid_ask", lambda pid: (_Ticker(0.0, 0.0), None),
                        raising=False)
    out = a.place_market_order(product_id="BDRX", side="sell", base_size="880",
                               extended_hours=True)
    assert out["ok"] is False
    assert "extended_hours" in out["error"]
    assert called == [], "walang order na dapat naipadala"


def test_the_swallowing_kwarg_no_longer_hides_extended_hours():
    """Bantay laban sa pagbabalik ng bug: ang `extended_hours` ay dapat isang
    TAHASANG parameter, hindi nahuhulog sa **_ignored."""
    import inspect

    sig = inspect.signature(AS.AlpacaSpotAdapter.place_market_order)
    assert "extended_hours" in sig.parameters
    assert sig.parameters["extended_hours"].kind is inspect.Parameter.KEYWORD_ONLY
