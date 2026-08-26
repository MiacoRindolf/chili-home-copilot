"""Ang 12 bps na spread floor ay tumatanggi sa 1-sentimong spread (2026-08-26).

ANG PUWANG. Ang fallback na `chili_momentum_risk_max_spread_bps_live` (12.0) ay
isang PORSIYENTONG hangganan na ipinapataw sa isang bagay na may DISKRETONG
hakbang. Ang minimum na tick ng US equity ay isang sentimo sa itaas ng $1.00,
kaya ang pinakamasikip na POSIBLENG merkado sa isang $1.30 na stock ay 77 bps
ayon lamang sa aritmetika::

    0.01 / 1.30 * 10,000 = 76.9 bps

Sa ilalim ng ~$8.33 ay HINDI KAILANMAN maaabot ang 12 bps. Iyon ang mismong banda
ng presyo na tinatrade ni Ross.

NASUKAT (2026-08-26, premarket)::

    32 wide_bbo_spread na harang sa 12.0 na fallback, avg mid $2.05
    26 sa mga iyon: spread na EKSAKTONG $0.0100 -- ang literal na minimum

    {"bid": 1.29, "ask": 1.30, "mid": 1.295, "reason": "wide_bbo_spread",
     "spread_bps": 77.2201, "max_spread_bps": 12.0, "expected_move_bps": null}

⚠️ HINDI SIRA ANG ADAPTIVE NA LANDAS. Kapag may expected-move ang tumatawag, ang
nasukat na tolerance ay 134.28 bps -- tama nitong tinatanggap ang 77 at
tinatanggihan ang 297/313/329. Ang WALANG-DATOS na fallback lamang ang inaayos.

Runnable: pytest tests/test_spread_floor_rejects_a_one_cent_spread.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR


class _Fresh:
    def age_seconds(self, now=None):
        return 0.1

    provider_time_utc = None
    max_age_seconds = 60.0


class _Tick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask
        self.mid = (bid + ask) / 2.0
        self.spread_bps = (ask - bid) / self.mid * 10_000.0
        self.freshness = _Fresh()
        self.raw = {"source": "iqfeed_l1", "feed": "iqfeed_l1"}


def _block(bid, ask, max_spread_bps=None):
    """Ibinabalik ang block dict o None."""
    return LR._quote_quality_block(
        _Tick(bid, ask), _Fresh(), max_spread_bps, symbol="TEST")


def _reason(bid, ask, max_spread_bps=None):
    b = _block(bid, ask, max_spread_bps)
    return (b or {}).get("reason")


def test_the_measured_case_is_no_longer_blocked():
    """ANG PANGUNAHING KASO -- ang eksaktong quote mula sa buhay na payload."""
    assert _reason(1.29, 1.30) != "wide_bbo_spread"


@pytest.mark.parametrize("bid,ask", [
    (1.29, 1.30),   # 77.2 bps -- ang nasukat na kaso
    (1.12, 1.13),   # 88.9 bps -- CDTG sa 11:58Z
    (2.21, 2.22),   # 45.1 bps
    (6.42, 6.43),   # 15.6 bps -- DAIC, LAGPAS PA RIN sa 12 bps
    (8.15, 8.16),   # 12.3 bps -- CRE, halos katabi ng lumang 12.0 na hangganan
])
def test_no_one_tick_market_is_ever_called_wide(bid, ask):
    """⚠️ ANG PRINSIPYO. Kung ang merkado ay kasing-sikip na ng kaya nitong
    i-quote, walang kahulugan ang tawaging malapad ito."""
    assert _reason(bid, ask) != "wide_bbo_spread"


@pytest.mark.parametrize("bid,ask", [
    (1.28, 1.30),   # DALAWANG sentimo -- 2x ang minimum
    (1.20, 1.30),   # sampung sentimo
    (6.00, 6.43),   # malapad sa isang $6 na pangalan
])
def test_anything_wider_than_one_tick_is_still_blocked(bid, ask):
    """⚠️ HINDI ITO NAGPAPALUWAG NG GATE. Ang aritmetikong imposible LAMANG ang
    inaayos; ang 2 tick ay tunay na mas malapad kaysa sa minimum."""
    assert _reason(bid, ask) == "wide_bbo_spread"


def test_a_one_cent_spread_on_a_sub_dollar_name_is_NOT_one_tick():
    """⚠️ ITINATAMA ANG SARILING TABLA KO. Inilista ko muna ang TRUG (0.68/0.69,
    145.9 bps) bilang dapat pumasa. Mali iyon: ang sub-$1 ay nagta-trade sa apat
    na decimal, kaya ang isang sentimo doon ay ISANG DAANG TICK ang lapad at
    tunay ngang malapad. Nahuli ako ng test."""
    assert _reason(0.68, 0.69) == "wide_bbo_spread"


def test_a_sub_dollar_name_uses_the_four_decimal_tick():
    """Ang sub-$1 ay nagta-trade sa apat na decimal, kaya ang isang tick doon ay
    $0.0001 -- hindi isang sentimo. Ang isang 1-sentimong spread sa $0.50 ay
    100 TICK ang lapad at dapat pa ring ma-block."""
    assert _reason(0.5000, 0.5001) != "wide_bbo_spread"
    assert _reason(0.50, 0.51) == "wide_bbo_spread"


def test_the_adaptive_tolerance_still_wins_when_supplied():
    """⚠️ Ang nasukat na adaptive na cap ay 134.28 bps: tinatanggap ang 77,
    tinatanggihan ang 297. Iyon ay dapat manatiling eksakto."""
    assert _reason(1.29, 1.30, 134.2812006319117) != "wide_bbo_spread"
    assert _reason(1.00, 1.35, 134.2812006319117) == "wide_bbo_spread"


def test_a_deliberate_zero_cap_still_blocks_everything():
    """⚠️ ANG SADYANG BLOCK-ALL. Ang 0.0 na cap ay isang pasya, hindi isang
    nawawalang halaga, at hindi ito dapat maging isang tick."""
    assert _reason(1.29, 1.30, 0.0) == "wide_bbo_spread"
    assert _reason(6.42, 6.43, 0.0) == "wide_bbo_spread"


def test_the_knob_reverts_it(monkeypatch):
    """Gawi bago ang 2026-08-26, nang walang deploy."""
    monkeypatch.setattr(
        settings, "chili_momentum_spread_floor_allows_one_tick", False, raising=False)
    assert _reason(1.29, 1.30) == "wide_bbo_spread"


def test_a_crossed_or_zero_book_is_not_rescued_by_this():
    """⚠️ Ang one-tick floor ay tungkol sa LAPAD, hindi sa BISA. Ang sirang libro
    ay dapat pa ring bumagsak sa sarili nitong tseke."""
    for bid, ask in ((0.0, 1.30), (1.30, 0.0), (1.31, 1.30)):
        b = _block(bid, ask)
        assert b is not None
        assert b.get("reason") != "wide_bbo_spread", (
            "ang sirang libro ay dapat may sariling dahilan, hindi 'wide'")


def test_the_twelve_bps_default_itself_did_not_move():
    """⚠️ Ang ayos ay tungkol sa isang PALAPAG sa ilalim ng cap, hindi sa
    pagpapababa ng cap. Kung may nagbago sa default ay ibang pasya iyon."""
    assert float(getattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)) == 12.0
