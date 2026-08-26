"""Ang prior-day close ay buhay na HTTP sa loob ng tick (2026-08-26).

Ang halagang ito ay HINDI NAGBABAGO sa buong araw ng kalakalan. Nakasakay ito sa
`massive:quote:` cache, na may `_TTL_QUOTE = 30` segundo dahil iyon ay para sa
BUHAY na quote. Kada 30 segundo kada simbolo ay nag-e-expire ito at nagpapadala
ng `GET /v2/snapshot/...` na may `timeout=15` -- sa loob mismo ng entry
evaluation.

PAANO ITO NAHULI. Hindi sa log -- ang log ay hindi nagpapakita nito. Ang TAPE
REPLAY ay nagbabawal ng network at itinuro ang eksaktong daan::

    tick_live_session
      -> red_to_green_confirmation      (entry_gates.py:10672)
        -> _prior_day_close             (entry_gates.py:2041)
          -> get_last_quote             (massive_client.py:821)
            -> requests.get(timeout=15)

⚠️ DALAWANG PINSALA:
  1. Latency sa mainit na daan -- isang round trip kada simbolo kada 30s, na may
     15-segundong timeout kapag mabagal ang provider.
  2. Ang replay ay HINDI DETERMINISTIKO, kaya hindi masusubok ang pagbabago
     laban sa tape at kailangan pang maghintay ng buhay na araw ng kalakalan.

Runnable: pytest tests/test_prior_day_close_is_not_http_per_tick.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import entry_gates as EG


@pytest.fixture(autouse=True)
def _clean():
    EG.reset_prior_day_close_cache()
    yield
    EG.reset_prior_day_close_cache()


def _patch_quote(monkeypatch, value):
    calls = []

    def _fake(sym):
        calls.append(sym)
        return value

    import app.services.massive_client as MC
    monkeypatch.setattr(MC, "get_last_quote", _fake, raising=False)
    return calls


def test_one_http_call_per_symbol_per_day(monkeypatch):
    """ANG PANGUNAHING KASO. Ang isang static na halaga ay hindi dapat kunin
    kada 30 segundo."""
    calls = _patch_quote(monkeypatch, {"previous_close": 12.34})
    for _ in range(50):
        assert EG._prior_day_close("VCIG") == 12.34
    assert len(calls) == 1, "inaasahang ISANG tawag, nakita: %d" % len(calls)


def test_a_missing_prior_close_is_also_cached(monkeypatch):
    """⚠️ Ang pangalang WALANG prior close ay ang pinaka-mapaminsala kung hindi
    naka-cache -- ito ang uulit-ulitin kada tick nang walang hanggan."""
    calls = _patch_quote(monkeypatch, {})
    for _ in range(30):
        assert EG._prior_day_close("NOPC") is None
    assert len(calls) == 1


def test_a_non_dict_response_is_cached_too(monkeypatch):
    calls = _patch_quote(monkeypatch, None)
    for _ in range(20):
        assert EG._prior_day_close("BAD") is None
    assert len(calls) == 1


def test_distinct_symbols_get_distinct_entries(monkeypatch):
    calls = _patch_quote(monkeypatch, {"previous_close": 5.0})
    EG._prior_day_close("AAA")
    EG._prior_day_close("BBB")
    EG._prior_day_close("AAA")
    assert len(calls) == 2


def test_the_key_carries_the_trading_day():
    """⚠️ Ang prior-day close ay NAGBABAGO sa hangganan ng araw. Ang cache na
    walang petsa ay magsisinungaling bukas."""
    k = EG._prior_day_close_cache_key("VCIG")
    assert isinstance(k, tuple) and len(k) == 2
    assert k[0] == "VCIG"
    assert len(k[1]) == 10 and k[1].count("-") == 2, "dapat ISO na petsa: %r" % (k,)


def test_crypto_is_still_rejected_without_any_call(monkeypatch):
    """Ang fail-closed na crypto na daan ay hindi ginagalaw."""
    calls = _patch_quote(monkeypatch, {"previous_close": 1.0})
    assert EG._prior_day_close("BTC-USD") is None
    assert calls == []


def test_the_knob_reverts_it(monkeypatch):
    """Gawi bago ang 2026-08-26, nang walang deploy."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_momentum_prior_day_close_daily_cache", False, raising=False)
    calls = _patch_quote(monkeypatch, {"previous_close": 9.9})
    for _ in range(6):
        EG._prior_day_close("VCIG")
    assert len(calls) == 6


def test_a_zero_or_negative_close_is_still_None(monkeypatch):
    """Ang orihinal na kontrata ay hindi ginagalaw."""
    _patch_quote(monkeypatch, {"previous_close": 0})
    assert EG._prior_day_close("ZERO") is None
