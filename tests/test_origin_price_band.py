"""L12 — origin-based price band (pure, walang I/O).

ANG SINUKAT NA KASO (AMIX, 2026-08-04 — ang #1 mover ni Ross, +400%):

  19:11  bumalik ang tape, $17.29   — nasa loob ng band (1.0–20.0)
  19:33  tumawid sa $20             — LUMABAS sa universe, hindi na muling ini-score
         umakyat hanggang $24.68    — nasa loob si Ross

Ang instrumento ay hindi nagbago: parehong ~500k-float na small-cap pa rin ang
AMIX sa $24 gaya noong $4.51 (21:1 reverse split 06-24). Ang nagbago ay ang
presyo — na siya mismong layunin natin. Ang band ay tungkol sa INSTRUMENT CLASS,
kaya dapat itong sukatin sa session ORIGIN, hindi sa live na presyo.

INVARIANT NG LEVER: PURONG PAGPAPALAWAK. Walang pangalang dating tinatanggap ang
matatanggal — ang unang tseke ay ang dating kondisyon mismo.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP as P,
    _price_in_band,
    _session_origin_price,
)


def test_amix_origin_derivation():
    # $24.68 na may +400% = prior close ~$4.94 — nasa loob ng band.
    origin = _session_origin_price(24.68, 400.0)
    assert origin == pytest.approx(4.936, abs=1e-3)
    assert _price_in_band(origin, P) is True
    # ...habang ang KASALUKUYANG presyo ay wala na sa band. Ito ang buong punto.
    assert _price_in_band(24.68, P) is False


def test_hindi_nagbabago_ang_karaniwang_kaso():
    # Isang pangalang nasa band pa rin: ang unang tseke ay pumapasa gaya ng dati.
    assert _price_in_band(9.72, P) is True
    assert _price_in_band(1.0, P) is True
    assert _price_in_band(20.0, P) is True


def test_hindi_nagpapapasok_ng_totoong_large_cap():
    # $250 na may +2% -> origin ~$245: PAREHONG labas. Nananatiling ineksklud.
    origin = _session_origin_price(250.0, 2.0)
    assert _price_in_band(250.0, P) is False
    assert _price_in_band(origin, P) is False


def test_hindi_nagpapapasok_ng_sub_dollar_penny():
    # Ang $0.40 na may +5% -> origin ~$0.38: parehong nasa ilalim ng $1 floor.
    # Sinadya ang eksklusyong ito (manipulative/halt-prone penny tape).
    origin = _session_origin_price(0.40, 5.0)
    assert _price_in_band(0.40, P) is False
    assert _price_in_band(origin, P) is False


def test_sub_dollar_na_umakyat_ay_hindi_pa_rin_pumapasok():
    # $25 mula sa $0.50 (+4900%) -> origin $0.50 < price_min. Parehong tseke
    # bumabagsak, kaya nananatiling labas — tapat sa sinadyang penny exclusion.
    origin = _session_origin_price(25.0, 4900.0)
    assert origin == pytest.approx(0.50, abs=1e-6)
    assert _price_in_band(origin, P) is False


@pytest.mark.parametrize(
    "price,chg",
    [(None, 400.0), (24.68, None), (0.0, 400.0), (-5.0, 400.0),
     (24.68, -100.0), (24.68, -150.0), (float("nan"), 10.0), (24.68, float("inf"))],
)
def test_origin_ay_none_sa_sirang_input(price, chg):
    # Fail-toward-EXISTING: walang origin => ang dating current-price-only na ugali.
    assert _session_origin_price(price, chg) is None


def test_band_helper_ay_matibay_sa_sirang_input():
    for bad in (None, 0.0, -1.0, float("nan"), float("inf"), "hindi-numero"):
        assert _price_in_band(bad, P) is False
