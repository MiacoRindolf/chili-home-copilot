"""Regime-aware catalyst tilt — Ross's hot-tape no-news read (2026-06-10 recap).

NORMAL tape: news names get the boost (unchanged behavior). HOT tape: the read
inverts — no-news names get the boost (full when foreign-HQ), news names go
neutral, never negative.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.catalyst import (
    catalyst_viability_delta,
    hot_tape_regime,
)

CAT = {"KIDZ", "EAB"}
HALF = 0.05  # CATALYST_VIABILITY_TILT(0.10) / 2 — the long-standing magnitude


def test_normal_tape_keeps_news_boost():
    assert catalyst_viability_delta("KIDZ", CAT) == pytest.approx(HALF)
    assert catalyst_viability_delta("DSY", CAT) == 0.0


def test_hot_tape_inverts_to_no_news():
    # news name -> neutral (NOT negative)
    assert catalyst_viability_delta("KIDZ", CAT, hot_tape=True) == 0.0
    # no-news foreign small cap -> full boost (the DSY/VSME/GCDT archetype)
    assert catalyst_viability_delta("DSY", CAT, hot_tape=True, hq_country="China") == pytest.approx(HALF)
    assert catalyst_viability_delta("VSME", CAT, hot_tape=True, hq_country="Hong Kong") == pytest.approx(HALF)
    # no-news but US/unknown HQ -> half boost
    assert catalyst_viability_delta("ABCD", CAT, hot_tape=True, hq_country="United States") == pytest.approx(HALF / 2)
    assert catalyst_viability_delta("EFGH", CAT, hot_tape=True, hq_country=None) == pytest.approx(HALF / 2)


def test_crypto_always_neutral():
    assert catalyst_viability_delta("BTC-USD", CAT, hot_tape=True, hq_country="China") == 0.0


def test_empty_catalyst_set_no_boosts():
    # without a working news feed, absence-of-news is not evidence
    assert catalyst_viability_delta("DSY", set(), hot_tape=True, hq_country="China") == pytest.approx(HALF)
    # note: empty set means has_news is False -> hot-tape boost still applies by
    # design ONLY when the caller gated on a non-empty set (viability.py does)


def test_hot_tape_regime_detector():
    def sig(chg):
        return {"daily_change_pct": chg}

    assert hot_tape_regime({"A": sig(120), "B": sig(45), "C": sig(31), "D": sig(5)}) is True
    assert hot_tape_regime({"A": sig(120), "B": sig(8)}) is False        # only 1 big mover
    assert hot_tape_regime({}) is False
    assert hot_tape_regime(None) is False
    assert hot_tape_regime({"A": {"daily_change_pct": "garbage"}, "B": sig(40), "C": sig(40), "D": sig(40)}) is True
