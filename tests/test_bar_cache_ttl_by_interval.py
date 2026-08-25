"""Ang TTL ng bar cache ay dapat sumunod sa interval, hindi isang patag na oras.

ANG PUWANG. Ang ``_TTL_BARS`` ay 3600 -- isang oras -- para sa BAWAT bar
interval. Katanggap-tanggap iyon para sa daily bar at nakamamatay para sa 1m.

May umiiral nang mitigation, at ang komento nito ang pinakamalinaw na paglalarawan
ng pinsala (``massive_client.py`` sa tabi ng invalidator)::

    # Invalidate BOTH bar-cache layers so the next trigger evaluation includes
    # the just-closed minute. Without the massive-layer drop, _TTL_BARS (1h)
    # left "live" triggers reading bars up to an hour old -- fatal for a 1m
    # entry timeframe.

⚠️ HINDI ITO NAAABOT SA PROSESONG NANGANGALAKAL, at ang tanikala ay
napapatunayan sa pagbabasa, hindi sa hinuha:

1. Ang invalidation ay nasa ``CandleAggregator._emit``.
2. Ang ``_emit`` ay tumatakbo lamang kung may ginawang aggregator.
3. Ang aggregator ay ginagawa LAMANG ng ``get_candle_aggregator``.
4. Ang ``get_candle_aggregator`` ay may IISANG tumatawag sa buong repo:
   ``register_candle_listener``.
5. Ang ``register_candle_listener`` ng massive_client ay tinatawag LAMANG mula sa
   ``app/routers/trading.py`` -- ang WEB container, ``CHILI_SCHEDULER_ROLE=none``.

Kaya sa host exec lane -- ang prosesong aktuwal na nagpapasya -- ay walang
aggregator, walang ``_emit``, at walang invalidation. Ang 3600 ang TUNAY na
hangganan doon, at ito ang bumabasa ng ``bos_exit_live``.

⚠️ HINDI ITO ANG TAMANG AYOS. Ang tamang ayos ay magparehistro ng candle
listener sa exec process para tumakbo talaga ang invalidator. Bagong wiring iyon
sa isang buhay na landas at hindi dapat isulat ilang oras bago ang isang session.
Ito ang makitid na panangga: ginagawa lang nitong mas sariwa ang datos, at ang
failure mode ay dami ng tawag sa upstream -- hindi maling presyo. Ang
market-data priority chain (Massive -> Polygon -> yfinance -> CoinGecko) ang
sumasalo kung mag-rate-limit.

Runnable: pytest tests/test_bar_cache_ttl_by_interval.py -v
"""
from __future__ import annotations

import pytest

from app.services.massive_client import (
    _TIMESPAN_MAP,
    _TTL_BARS,
    _TTL_BARS_FLOOR,
    _bar_cache_ttl,
)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("massive:agg:AAPL:1m:5d:pg1", 180.0),
        ("massive:agg:AAPL:2m:5d:pg1", 360.0),
        ("massive:agg:AAPL:5m:1mo:pg1", 900.0),
        ("massive:agg:AAPL:15m:1mo:pg1", 2700.0),
        # ang mas mahaba sa 20 min ay naka-cap sa lumang oras -- walang lumalala
        ("massive:agg:AAPL:30m:1mo:pg1", 3600.0),
        ("massive:agg:AAPL:1d:1y:pg1", 3600.0),
    ],
)
def test_the_ttl_follows_the_interval(key, expected):
    assert _bar_cache_ttl(key) == expected


def test_the_one_minute_frame_is_twenty_times_fresher():
    """Ang buong punto. Ang 1m ang entry timeframe."""
    assert _bar_cache_ttl("massive:agg:AAPL:1m:5d:pg1") == pytest.approx(180.0)
    assert _TTL_BARS / _bar_cache_ttl("massive:agg:AAPL:1m:5d:pg1") == pytest.approx(20.0)


def test_a_crypto_key_with_a_colon_in_the_ticker_still_finds_the_interval():
    """⚠️ ANG BITAG. Ang crypto ay naka-key bilang ``X:BTCUSD`` -- MAY COLON --
    kaya lumilipat ang bawat posisyon pagkatapos ng ticker. Ang isang parser na
    kumukuha ng nakapirming index ay babasahin ang ``BTCUSD`` bilang interval at
    tahimik na babalik sa isang oras."""
    assert _bar_cache_ttl("massive:agg:X:BTCUSD:1m:5d|ic:pg1") == pytest.approx(180.0)
    assert _bar_cache_ttl("massive:agg:X:ETHUSD:1d:1y:pg1") == pytest.approx(3600.0)


def test_an_unknown_interval_falls_back_to_the_old_bound():
    """Fail-SAFE sa direksyon ng dating gawi: kapag hindi mabasa ay walang
    nagbabago. Isang bagong TTL na mas maikli para sa hindi kilalang key ay
    magiging tahimik na pagtaas ng upstream load."""
    assert _bar_cache_ttl("massive:agg:AAPL:walanganito:pg1") == float(_TTL_BARS)
    assert _bar_cache_ttl("massive:agg:AAPL") == float(_TTL_BARS)
    assert _bar_cache_ttl("") == float(_TTL_BARS)


def test_no_interval_ever_gets_a_longer_ttl_than_before():
    """⚠️ ANG BANTAY LABAN SA PAGLALA. Ang bawat interval sa mapa ay dapat may
    TTL na <= sa dating patag na 3600, kailanman ay hindi hihigit."""
    for interval in _TIMESPAN_MAP:
        ttl = _bar_cache_ttl(f"massive:agg:AAPL:{interval}:1y:pg1")
        assert ttl <= float(_TTL_BARS), f"{interval} ay lumala: {ttl} > {_TTL_BARS}"
        assert ttl >= _TTL_BARS_FLOOR, f"{interval} ay mas maikli sa floor: {ttl}"


def test_quote_and_snapshot_ttls_are_untouched():
    """Ang tanging binago ay ang sangang pang-bar. Ang quote at snapshot ay may
    sariling TTL at hindi dumadaan dito."""
    from app.services.massive_client import _TTL_QUOTE, _TTL_SNAPSHOT

    assert _TTL_QUOTE == 30
    assert _TTL_SNAPSHOT == 60


def test_the_invalidator_is_still_unreachable_in_the_exec_lane():
    """⚠️ Pinipin ang PREMISE. Kung magparehistro ng candle listener ang isang
    exec-side na proseso ay gagana na ang tunay na invalidator, at ang panangga
    na ito ay maaari nang lumuwag muli -- pero dapat iyon ay isang sadyang pasya,
    hindi isang bagay na natuklasan makalipas ang isang taon."""
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parents[1]
    hits: list[str] = []
    for path in list((repo / "app").rglob("*.py")) + list((repo / "scripts").rglob("*.py")):
        if path.name == "massive_client.py":
            continue
        try:
            src = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        # ang massive_client na anyo lang -- ang price_bus ay may sariling
        # kapangalan na method na hindi nag-i-invalidate ng bar cache
        if re.search(r"from .*massive_client import [^\n]*register_candle_listener", src) or \
           re.search(r"massive_client\.register_candle_listener", src):
            hits.append(str(path.relative_to(repo)).replace("\\", "/"))
    assert hits == ["app/routers/trading.py"], (
        "nagbago ang hanay ng nagpaparehistro ng massive candle listener; "
        f"muling suriin ang panangga ng TTL: {hits}"
    )
