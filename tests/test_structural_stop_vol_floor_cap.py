"""STRUCTURE-CAPPED VOL FLOOR — ang stop na kumain ng R sa YJ (2026-08-19).

Kapag mas MAKIPOT ang structure kaysa sa vol floor, ang floor ang nananalo — iyon
ang shake-out guard at MANANATILI. Pero WALANG HANGGAN ito dati, kaya sa pangalang
may mataas na expected move ay puwede itong umupo sa halos DOBLE ng layong
kailangan ng pattern. At ang bawat dagdag na puntos ng stop ay **bayad nang
dalawang beses**: pinapaliit nito ang R-multiple AT (risk-first sizing) pinapaliit
ang posisyon.

Sinukat sa naitalang tape ng YJ:
    entry 5.75, double-bottom low ~5.37  -> structural = 6.6%
    expected_move 1950 bps               -> floor 0.5 x 19.5% = 9.75%
    stop = 5.1894
Hindi na muling binisita ng presyo ang 5.37 pagkatapos ng entry, kaya ang dagdag
na 3.2% ay WALANG binili — at pinutol nito ang trade mula ~3.4R tungo sa 1.30R.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.paper_execution import (
    structural_or_vol_floored_atr_pct,
)


def _yj(cap=None, monkeypatch=None):
    """Ang tunay na YJ na sandali: entry 5.75, structural stop 5.37, floor 9.75%."""
    if cap is not None and monkeypatch is not None:
        monkeypatch.setattr(
            settings, "chili_momentum_structural_stop_vol_floor_cap_mult", cap,
            raising=False,
        )
    return structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.0975,      # 0.5 x 1950bps
        structural_stop_price=5.37,
        entry_price=5.75,
        stop_atr_mult=1.0,
    )


def test_yj_stop_is_capped_toward_structure(monkeypatch):
    """Ang structural ay 6.61%; sa cap 1.25 ang stop ay 8.26%, hindi 9.75%."""
    eff, model = _yj(cap=1.25, monkeypatch=monkeypatch)
    assert model == "structure_capped_vol_floor", model
    assert eff == pytest.approx(0.0661 * 1.25, rel=0.02), eff
    assert eff < 0.0975


def test_cap_off_restores_the_unbounded_floor(monkeypatch):
    """PARITY: cap <= 1.0 ⇒ ang lumang walang-hanggang floor."""
    eff, model = _yj(cap=1.0, monkeypatch=monkeypatch)
    assert model == "vol_floored_atr"
    assert eff == pytest.approx(0.0975)
    eff0, model0 = _yj(cap=0.0, monkeypatch=monkeypatch)
    assert model0 == "vol_floored_atr"
    assert eff0 == pytest.approx(0.0975)


def test_floor_still_wins_when_there_is_no_structure(monkeypatch):
    """ANG MAHALAGA: walang structural level ⇒ ang floor ang buong may-ari.
    Hindi ito hinihipo ng cap."""
    monkeypatch.setattr(
        settings, "chili_momentum_structural_stop_vol_floor_cap_mult", 1.25,
        raising=False,
    )
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.0975,
        structural_stop_price=None,
        entry_price=5.75,
        stop_atr_mult=1.0,
    )
    assert model == "vol_floored_atr"
    assert eff == pytest.approx(0.0975)


def test_structure_wider_than_floor_is_unchanged(monkeypatch):
    """Kapag mas MALAPAD ang structure, ito pa rin ang nananalo — walang binago
    ang cap sa direksyong iyon."""
    monkeypatch.setattr(
        settings, "chili_momentum_structural_stop_vol_floor_cap_mult", 1.25,
        raising=False,
    )
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.04,
        structural_stop_price=5.00,   # 13% mula 5.75
        entry_price=5.75,
        stop_atr_mult=1.0,
    )
    assert model == "structural_pullback"
    assert eff > 0.04


def test_cap_never_makes_the_stop_tighter_than_structure(monkeypatch):
    """INVARIANT: ang capped na stop ay hindi kailanman mas makipot kaysa sa
    structure mismo — laging >= 1.0x."""
    for cap in (1.01, 1.25, 2.0):
        monkeypatch.setattr(
            settings, "chili_momentum_structural_stop_vol_floor_cap_mult", cap,
            raising=False,
        )
        eff, _ = _yj()
        assert eff >= (5.75 - 5.37) / 5.75 - 1e-9, (cap, eff)


def test_bad_inputs_fall_back_to_the_floor(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_structural_stop_vol_floor_cap_mult", 1.25,
        raising=False,
    )
    for sp, ep in ((None, 5.75), (0.0, 5.75), (6.50, 5.75), (5.37, 0.0)):
        eff, model = structural_or_vol_floored_atr_pct(
            vol_floored_atr_pct=0.0975,
            structural_stop_price=sp,
            entry_price=ep,
            stop_atr_mult=1.0,
        )
        assert eff == pytest.approx(0.0975), (sp, ep, eff)
        assert model == "vol_floored_atr"


def test_setting_is_wired_and_bounded():
    v = getattr(
        settings, "chili_momentum_structural_stop_vol_floor_cap_mult", None
    )
    assert v is not None
    assert 0.0 <= float(v) <= 5.0
    # Dapat may TUNAY na wick room lampas sa structure...
    assert float(v) > 1.0
    # ...pero hindi kasing-luwag ng dating walang-hanggang floor sa YJ (1.47x).
    assert float(v) < 1.47
