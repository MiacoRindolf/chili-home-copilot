"""AUTHORITY-AWARE PRE-PLACE CEILING — ang huling metro na pumatay sa stand-in.

Sinukat live 2026-08-20 14:43 (IPST): pumasa ang stand-in sa entry final-BBO sa
3.39s, tapos namatay sa pre-place re-check laban sa hard 2.0s na cap
(`alpaca_final_bbo_stale_at_place`). Ang tape recorder ay nagfa-flush sa DB
tuwing 5s, kaya ang SIP stand-in ay PISIKAL na hindi makakapasa sa 2.0s — bawat
stand-in entry ay mamamatay sa huling hakbang bago ang broker.

Ang lunas: para sa `stand_in_massive_sip` ang ceiling ay ang sariling configured
ceiling ng stand-in (ang parehong hangganang ipinatupad ng adapter sa fetch).
LIGTAS dahil hindi nagpepresyo ang stand-in — naka-pin ang planned limit, kaya
ang bahagyang mas lumang quote ay nagpapatunay lang na may merkado, hindi
gumagalaw ng presyo. Ang `alpaca_direct` ay nananatili sa 2.0s.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.live_runner import (
    _pre_place_bbo_age_ceiling,
)


def test_direct_authority_keeps_the_two_second_cap():
    assert _pre_place_bbo_age_ceiling(30.0, "alpaca_direct") == 2.0
    assert _pre_place_bbo_age_ceiling(60.0, "") == 2.0
    assert _pre_place_bbo_age_ceiling(60.0, None) == 2.0


def test_stand_in_authority_uses_its_own_ceiling(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 10.0,
        raising=False,
    )
    # Ang IPST na sandali: configured 30s sa seam, stand-in authority -> 10.0,
    # kaya ang 3.39s na edad ay PAPASA na ngayon.
    assert _pre_place_bbo_age_ceiling(30.0, "stand_in_massive_sip") == 10.0


def test_configured_tighter_than_ceiling_still_wins(monkeypatch):
    """Ang caller na humihingi ng MAS MAKIPOT ay iginagalang pa rin."""
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 10.0,
        raising=False,
    )
    assert _pre_place_bbo_age_ceiling(1.0, "stand_in_massive_sip") == 1.0
    assert _pre_place_bbo_age_ceiling(1.0, "alpaca_direct") == 1.0


def test_unknown_authority_fails_toward_the_tight_cap():
    """Ang hindi kilalang authority ay HINDI nakakakuha ng maluwag na ceiling."""
    assert _pre_place_bbo_age_ceiling(30.0, "some_future_source") == 2.0


def test_bad_configured_values_default_to_two_seconds():
    assert _pre_place_bbo_age_ceiling(None, "alpaca_direct") == 2.0
    assert _pre_place_bbo_age_ceiling("garbage", "alpaca_direct") == 2.0
    assert _pre_place_bbo_age_ceiling(-5.0, "alpaca_direct") == 0.0


def test_zero_stand_in_ceiling_disables_the_widening(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 0.0,
        raising=False,
    )
    assert _pre_place_bbo_age_ceiling(30.0, "stand_in_massive_sip") == 0.0
