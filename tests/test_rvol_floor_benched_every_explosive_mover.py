"""Ang RVOL floor ay nag-bench sa BAWAT explosive na mover (2026-08-26).

ANG PUWANG. Ang ``below_explosive_floor`` ay nangangako na mag-bench LAMANG sa
datos na AFFIRMATIVELY nagpapakitang hindi explosive ang pangalan, at fail-open
sa kulang. Ang ``vol_ratio`` na ibinibigay ng ``ws_ignition`` ay hindi nakakatugon
sa pangakong iyon -- sinukat na sistemikong mali ito.

NASUKAT SA BUONG BOARD (2026-08-26 11:40Z, premarket -- ang mismong mga pangalan
sa scanner ni Ross)::

    simbolo  vol_ratio   float_rotation  chg%    live_eligible
    LBGJ          0.93            33.71   12.9   f
    YYGH          1.30            12.69   89.6   f
    DAIC          0.09            10.92   63.4   f
    SMTK          1.88             5.05   20.8   f
    CDTG          3.38             4.38   38.3   f
    RDIB       3074.05             2.10   35.3   t   <-- ANG TANGING PUMASA
    XPON          0.16             1.59   13.9   f
    SDOT          0.23             0.37   17.8   f
    WVVIP         0.33             0.26   43.8   f
    BRNX          0.10             0.25   14.9   f

⚠️ BALIGTAD ANG GATE SA PRAKTIKA. Ang TANGING pangalang nakalusot sa 5.0 na floor
ay ang isa na ang `vol_ratio` ay basura sa KABILANG direksyon (3,074.05). Ang
pangalang umikot ang float nang 33.7 BESES ay na-bench sa 0.93.

⚠️ ANG `float_rotation` AY PANLOOB NA MAGKAKATUGMA: sa DAIC, 13,208,232 shares /
1,210,000 float = 10.9159 -- eksaktong tugma sa naitalang volume at float sa
parehong dict. Ito ang mismong dami na inilalarawan ni Ross, at hindi ito umaasa
sa isang sirang average.

⚠️ HINDI ITO NAGPAPALUWAG NG GATE: ang RVOL leg LAMANG ang nilalaktawan;
tumatakbo pa rin ang change floor.

Runnable: pytest tests/test_rvol_floor_benched_every_explosive_mover.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_ELIGIBILITY_RVOL_FLOOR,
    below_explosive_floor,
)

# Ang eksaktong hilera na nakuha sa buhay na board, 2026-08-26 11:40Z.
MEASURED_BOARD = [
    # (symbol, vol_ratio, float_rotation, todays_change_perc, dapat_ba_i-bench)
    ("LBGJ", 0.9325, 33.71, 12.9, False),
    ("YYGH", 1.2984, 12.69, 89.6, False),
    ("DAIC", 0.0914, 10.92, 63.4, False),
    ("SMTK", 1.8805, 5.05, 20.8, False),
    ("CDTG", 3.3825, 4.38, 38.3, False),
    ("XPON", 0.1605, 1.59, 13.9, False),
    # Sa ilalim ng isang buong pag-ikot ng float -- nananatiling benched.
    ("SDOT", 0.2312, 0.37, 17.8, True),
    ("WVVIP", 0.3268, 0.26, 43.8, True),
    ("BRNX", 0.1049, 0.25, 14.9, True),
]


def _sig(vol_ratio, float_rotation, chg):
    return {
        "source": "ws_ignition",
        "vol_ratio": vol_ratio,
        "float_rotation": float_rotation,
        "todays_change_perc": chg,
    }


@pytest.mark.parametrize("sym,vr,rot,chg,expect_bench", MEASURED_BOARD)
def test_the_measured_board_now_resolves_correctly(sym, vr, rot, chg, expect_bench):
    """ANG PANGUNAHING KASO -- ang buhay na board, hilera kada hilera."""
    assert below_explosive_floor(_sig(vr, rot, chg)) is expect_bench


def test_DAIC_the_name_Ross_actually_scalped_is_no_longer_benched():
    """Ang pangalang ini-scalp ni Ross nang 07:13 ET habang na-bench ito ni CHILI."""
    assert below_explosive_floor(_sig(0.0914, 10.92, 63.4)) is False


def test_a_high_rotation_name_that_is_NOT_moving_is_still_benched():
    """⚠️ HINDI ITO NAGPAPALUWAG NG GATE. Tumatakbo pa rin ang change floor:
    ang umikot nang marami ngunit halos hindi gumagalaw ay dapat pa ring
    ma-bench."""
    assert below_explosive_floor(_sig(0.09, 25.0, 1.2)) is True


def test_a_low_rotation_name_still_faces_the_rvol_floor():
    """Kapag walang pag-ikot ng float na masasandalan, ang RVOL leg ay
    pinagkakatiwalaan gaya ng dati."""
    assert below_explosive_floor(_sig(0.5, 0.1, 40.0)) is True


def test_a_missing_float_rotation_keeps_the_old_behaviour():
    """⚠️ Walang bagong pagpapalagay sa kulang na datos."""
    sig = {"source": "scanner", "vol_ratio": 0.5, "todays_change_perc": 40.0}
    assert below_explosive_floor(sig) is True


def test_missing_everything_still_fails_open():
    """Ang orihinal na kontrata: hindi kailanman mag-bench sa kawalan."""
    assert below_explosive_floor({"source": "scanner"}) is False


def test_the_knob_reverts_it(monkeypatch):
    """0 = gawi bago ang 2026-08-26, nang walang deploy."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_momentum_float_rotation_rvol_override", 0.0, raising=False)
    assert below_explosive_floor(_sig(0.0914, 10.92, 63.4)) is True


def test_a_genuinely_dull_name_is_still_rejected():
    """ANG PROTEKSYON. Isang normal na pangalan: walang pag-ikot, mababang RVOL,
    walang galaw."""
    assert below_explosive_floor(_sig(1.1, 0.02, 0.4)) is True


def test_the_rvol_floor_constant_did_not_move():
    """⚠️ Ang ayos ay tungkol sa PINAGMULAN ng RVOL, hindi sa taas ng bar. Kung
    may nagpababa ng floor mismo, ibang pasya iyon."""
    assert ROSS_ELIGIBILITY_RVOL_FLOOR == 5.0


def test_the_override_only_ever_prevents_a_bench():
    """⚠️ RESTRICT-ONLY SA KABILANG DIREKSYON. Ang override ay hindi kailanman
    dapat gumawa ng BAGONG bench -- sa bawat input, ang resulta nito ay dapat
    pareho o mas kaunting benched kaysa sa lumang gawi."""
    from app.config import settings
    old_value = getattr(settings, "chili_momentum_float_rotation_rvol_override", 1.0)
    cases = [(vr, rot, chg) for _s, vr, rot, chg, _e in MEASURED_BOARD]
    cases += [(0.0, 0.0, 0.0), (100.0, 100.0, 100.0), (5.0, 1.0, 9.9)]
    try:
        for vr, rot, chg in cases:
            settings.chili_momentum_float_rotation_rvol_override = 0.0
            before = below_explosive_floor(_sig(vr, rot, chg))
            settings.chili_momentum_float_rotation_rvol_override = 1.0
            after = below_explosive_floor(_sig(vr, rot, chg))
            assert not (after and not before), (
                "vol_ratio=%s rot=%s chg=%s: bagong bench na hindi umiiral dati"
                % (vr, rot, chg))
    finally:
        settings.chili_momentum_float_rotation_rvol_override = old_value
