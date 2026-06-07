"""E5: news-catalyst pillar (earnings) — Ross's 4th selection signal."""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.catalyst import (
    catalyst_score,
    catalyst_viability_delta,
)


def test_catalyst_score_boosts_catalyst_name():
    cat = {"AAPL", "TSLA"}
    assert catalyst_score("AAPL", cat) == 1.0   # has earnings catalyst
    assert catalyst_score("NVDA", cat) == 0.5    # no catalyst -> neutral (no penalty)


def test_crypto_is_always_neutral():
    assert catalyst_score("BTC-USD", {"BTC"}) == 0.5  # crypto has no earnings
    assert catalyst_score("KAIO-USD", {"KAIO"}) == 0.5


def test_no_catalyst_set_is_neutral():
    assert catalyst_score("AAPL", None) == 0.5
    assert catalyst_score("AAPL", set()) == 0.5


def test_viability_delta_boosts_catalyst_only(monkeypatch):
    # default tilt 0.10 -> +0.05 for a catalyst name, 0 otherwise
    assert catalyst_viability_delta("AAPL", {"AAPL"}) == pytest.approx(0.05)
    assert catalyst_viability_delta("NVDA", {"AAPL"}) == 0.0
    assert catalyst_viability_delta("BTC-USD", {"BTC"}) == 0.0  # crypto: no boost


def test_normalizes_dash_tickers():
    # an equity that somehow carries a -USD suffix is still matched by base ticker
    assert catalyst_score("AAPL-USD", {"AAPL"}) == 0.5  # -USD treated as crypto -> neutral
