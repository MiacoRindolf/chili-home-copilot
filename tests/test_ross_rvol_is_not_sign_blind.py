"""Ang RVOL nang mag-isa ay hindi dapat mag-arm ng bumabagsak na stock bilang LONG (2026-08-25).

ANG PUWANG. Ang ``_ross_threshold_crossed`` ay may tatlong sangay. Dalawa sa kanila ay
may tanda::

    g = _f(gap_pct)
    if g is not None and g >= float(_CHG_FLOOR):   # +10%
        return True
    mv = _f(move_pct)
    if mv is not None and mv >= float(_CHG_FLOOR): # +10%
        return True

Ang pangatlo ay wala::

    rv = _f(rvol)
    if rv is not None and rv >= float(_RVOL_FLOOR):
        return True                                 # <- direksyon? wala.

Ang RVOL ay WALANG TANDA -- ang dami ng kalakalan ay sumasabog sa MAGKABILANG
direksyon. At ang sariling docstring ng function ay nagsasabing "True when a name
**AFFIRMATIVELY** crosses ANY Ross explosiveness axis."

NASUKAT SA BUHAY NA LANE (2026-08-25, buong araw ng kalakalan)::

    live_arm_requested  418
    live_arm_confirmed  376
    live_declined       309   <- WVVIP lamang: 268  (87%)
    live_watch_started   67
    live_entry_filled     1

    WVVIP: rvol 911.8, todays_change_perc -54.08, live_eligible=t,
           at ang ignition_loop ay nagse-stamp ng direction="long" (ignition_loop.py:775).
           ZERO tick sa nakaraang 45 minuto -- walang librong maka-fill.
           284 arm mula 08:12 hanggang 14:48, tig-84 segundo, bawat isa ay
           namamatay sa `no_bbo` matapos ang ~13 segundo.

ISANG bumabagsak na preferred share ang kumain ng **87% ng buong decline funnel** sa
buong araw, at ina-arm ito bilang LONG.

⚠️ MAKITID SA SADYA ANG AYOS. Hindi nito ipinapataw ang +10% na palapag sa RVOL na
daan -- iyon ay magpapaliit ng lehitimong admission. Ang ipinagbabawal lang ay ang
BUMABAGSAK. Walang alam na direksyon ⇒ walang pagbabago sa gawi.

Runnable: pytest tests/test_ross_rvol_is_not_sign_blind.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.nbbo_tape import _ross_threshold_crossed
from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT as CHG_FLOOR,
    ROSS_ELIGIBILITY_RVOL_FLOOR as RVOL_FLOOR,
)

# Ang mga within-band guard (presyo 1-20, $-volume >= 1M) ay AND-gate, kaya ibinibigay
# ang mga ito sa bawat kaso -- kung hindi ay ang mga iyon ang susuriin natin.
BAND = {"price": 5.0, "dollar_volume": 5_000_000.0, "float_shares": 3_000_000.0}


def test_the_WVVIP_case_no_longer_arms_a_crashing_stock(monkeypatch):
    """ANG PANGUNAHING KASO, mga tunay na numero mula sa buhay na lane."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "WVVIP", rvol=911.8, move_pct=-54.08, **BAND
    ) is False, "ang isang -54% na stock ay hindi dapat maging LONG trigger"


def test_a_rising_stock_on_rvol_alone_still_fires(monkeypatch):
    """⚠️ WALANG PAGPAPALIIT. Ang RVOL na daan ay dapat pa ring magpaputok para sa
    isang pangalang umaakyat kahit hindi pa nito naaabot ang +10% na palapag --
    iyon ang buong punto ng magkahiwalay na axis."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "UPUP", rvol=float(RVOL_FLOOR) + 1.0, move_pct=3.0, **BAND
    ) is True


def test_no_directional_evidence_keeps_the_old_behaviour(monkeypatch):
    """Kapag wala tayong alam sa direksyon ay hindi tayo nanghuhula -- ang RVOL ay
    nagpapaputok pa rin, gaya ng dati."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "UNKN", rvol=float(RVOL_FLOOR) + 1.0, **BAND
    ) is True


def test_a_flat_tape_still_fires(monkeypatch):
    """Ang eksaktong zero ay hindi bumabagsak."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "FLAT", rvol=float(RVOL_FLOOR) + 1.0, move_pct=0.0, **BAND
    ) is True


def test_move_pct_is_preferred_over_gap_pct(monkeypatch):
    """Ang intraday move ang mas bago at siyang tunay na sinusukat ng ignition path,
    kaya ito ang dapat manaig kapag hindi sila magkasundo."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    # ⚠️ ANG GAP AY DAPAT NASA IBABA NG CHANGE FLOOR DITO. Kung hindi ay dadaan ang
    # pangalan sa GAP axis at hindi na natin masusuri ang RVOL na daan -- iyon
    # mismo ang ipinapatunay ng test na `test_a_crasher_that_ALSO_gapped_up_hugely`.
    _below = float(CHG_FLOOR) - 5.0
    # gumapang paitaas nang bahagya, ngayon ay bumabagsak => hindi dapat magpaputok
    assert _ross_threshold_crossed(
        "FADE", rvol=float(RVOL_FLOOR) + 1.0, gap_pct=_below, move_pct=-40.0, **BAND
    ) is False
    # gumapang pababa, ngayon ay umaakyat => dapat magpaputok sa RVOL na daan
    assert _ross_threshold_crossed(
        "RVRS", rvol=float(RVOL_FLOOR) + 1.0, gap_pct=-30.0, move_pct=8.0, **BAND
    ) is True


def test_the_SIGNED_change_floors_are_untouched(monkeypatch):
    """⚠️ WALANG REGRESSION. Ang dalawang change floor ay may tanda na dati pa;
    dapat pa rin silang magpaputok nang mag-isa, kahit mababa ang RVOL."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "GAPU", rvol=0.1, gap_pct=float(CHG_FLOOR) + 1.0, **BAND
    ) is True
    assert _ross_threshold_crossed(
        "MOVU", rvol=0.1, move_pct=float(CHG_FLOOR) + 1.0, **BAND
    ) is True
    # at hindi pa rin sila nagpapaputok sa negatibo
    assert _ross_threshold_crossed("GAPD", rvol=0.1, gap_pct=-30.0, **BAND) is False
    assert _ross_threshold_crossed("MOVD", rvol=0.1, move_pct=-30.0, **BAND) is False


def test_a_crasher_that_ALSO_gapped_up_hugely_still_fires(monkeypatch):
    """Ang mga change floor ay sinusuri PAGKATAPOS ng RVOL, kaya ang isang pangalang
    may +30% na gap pero -40% intraday ay dapat pa ring dumaan sa gap axis. Ang
    ayos ay tinatanggal LAMANG ang daan ng RVOL, hindi ang iba."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "GAPX", rvol=float(RVOL_FLOOR) + 1.0, gap_pct=float(CHG_FLOOR) + 20.0,
        move_pct=-40.0, **BAND
    ) is True


def test_false_restores_the_old_sign_blind_behaviour(monkeypatch):
    """Ang knob ay may tunay na off switch."""
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", False, raising=False
    )
    assert _ross_threshold_crossed(
        "WVVIP", rvol=911.8, move_pct=-54.08, **BAND
    ) is True


@pytest.mark.parametrize("move", [-0.01, -1.0, -54.08, -99.0])
def test_every_negative_move_is_refused_on_the_rvol_path(monkeypatch, move):
    monkeypatch.setattr(
        settings, "chili_momentum_ross_rvol_requires_nonnegative_move", True, raising=False
    )
    assert _ross_threshold_crossed(
        "NEG", rvol=float(RVOL_FLOOR) + 50.0, move_pct=move, **BAND
    ) is False
