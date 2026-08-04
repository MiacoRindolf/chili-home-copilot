"""L10d — noise floor sa scale-ladder rungs (pure, walang I/O).

BAKIT: ang ladder targets ay R-multiples ng STOP DISTANCE, kaya sa mahigpit na
stop ay nagiging kalapit-lapit sa entry ang unang rung. Sukat mula sa L10b/L10c
proof (parehong sealed, arm B nag-reproduce ng lumang canon sa 4/4):

  HYFM  entry 3.66 -> rung 3.92 = +7.1%   window range 36.0%   -> TAMANG bangko
  JLHL  entry 9.50 -> rung 9.61 = +1.2%   window range 422.5%  -> MALING bangko

Nagbenta ang ladder ng KALAHATI ng posisyon para sa 1.2% sa isang pangalang
umakyat ng 422%. Ang naunang hipotesis (L10c: i-gate sa leg age + halt) ay BIGO —
ang JLHL rungs ay pumuputok 0-1s pagkatapos ng entry, bago pa may halt, kaya
parehong kondisyon ay bulag. Ang sukatan ay DISTANSYA, hindi oras.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.paper_execution import rung_clears_noise


def test_hyfm_rung_pumapasa():
    # +7.1% laban sa 5% ATR.
    assert rung_clears_noise(entry=3.66, target=3.92, atr_pct=0.05) is True


def test_jlhl_rung_bumabagsak():
    # +1.2% laban sa 5% ATR — nasa loob ng ingay.
    assert rung_clears_noise(entry=9.50, target=9.61, atr_pct=0.05) is False


def test_eksaktong_isang_atr_ay_pumapasa():
    # Hangganan: mahigpit na hindi-bababa-sa, kaya ang eksaktong 1 ATR ay pasado.
    assert rung_clears_noise(entry=10.0, target=10.5, atr_pct=0.05) is True
    assert rung_clears_noise(entry=10.0, target=10.49, atr_pct=0.05) is False


def test_mas_mabagal_na_pangalan_ay_mas_mababang_hadlang():
    # ADAPTIVE: ang parehong 2% na rung ay ingay sa 5%-ATR na pangalan pero
    # tunay na galaw sa 1%-ATR na pangalan.
    assert rung_clears_noise(entry=10.0, target=10.2, atr_pct=0.05) is False
    assert rung_clears_noise(entry=10.0, target=10.2, atr_pct=0.01) is True


@pytest.mark.parametrize(
    "kw",
    [
        {"atr_pct": None},
        {"atr_pct": 0.0},
        {"atr_pct": float("nan")},
        {"entry": None},
        {"target": None},
        {"entry": 0.0},
        {"entry": "hindi-numero"},
    ],
)
def test_fail_toward_KEEP_sa_kulang_na_datos(kw):
    # Ang kawalan ng datos ay HINDI dahilan para bawasan ang umiiral nang ladder.
    base = dict(entry=9.50, target=9.61, atr_pct=0.05)
    base.update(kw)
    assert rung_clears_noise(**base) is True, kw


def test_walang_natirang_rung_ay_bumabalik_sa_single_scale_out():
    # Integration-shape: kapag NAHULOG ang lahat ng rung, ang ladder ay nagiging
    # walang laman at ang caller ay bumabalik sa single scale-out (umiiral nang
    # ugali) — ito ang JLHL na kaso.
    from app.services.trading.momentum_neural.paper_execution import scale_grid_levels

    # Napakahigpit na stop => napakalapit na R-rungs; mataas na ATR => lahat ingay.
    levels = scale_grid_levels(9.50, 9.40, side_long=True, atr_pct=0.20)
    assert levels == []
