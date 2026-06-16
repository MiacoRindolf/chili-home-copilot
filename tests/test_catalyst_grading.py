"""Ross gap #12: catalyst-TYPE grading (videos 06/36). A cash-raise / compliance / legal
headline is a WEAK catalyst Ross DISTRUSTS (the name will issue shares and fade) — it must
NOT earn the catalyst boost a trial / M&A / contract (STRONG) does. Only the weak class is
de-boosted (to 0); strong/medium keep the existing tilt; crypto + no-feed are unaffected.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.catalyst import (
    _catalyst_tilt,
    _is_weak_catalyst,
    catalyst_viability_delta,
)


# ── classifier ───────────────────────────────────────────────────────────────

def test_strong_headline_not_weak():
    assert _is_weak_catalyst("Acme announces positive Phase 3 trial results") is False
    assert _is_weak_catalyst("BigCo to acquire Acme in $2B merger") is False
    assert _is_weak_catalyst("Acme wins $500M defense contract") is False


def test_weak_headline_detected():
    assert _is_weak_catalyst("Acme announces $50M registered direct offering") is True
    assert _is_weak_catalyst("Acme to effect 1-for-10 reverse stock split") is True
    assert _is_weak_catalyst("Acme regains compliance with Nasdaq listing rule") is True
    assert _is_weak_catalyst("Acme faces securities fraud class action lawsuit") is True
    assert _is_weak_catalyst("Acme prices private placement of common stock") is True


def test_empty_or_none_title_not_weak():
    assert _is_weak_catalyst("") is False
    assert _is_weak_catalyst(None) is False


# ── catalyst_viability_delta de-boost ────────────────────────────────────────

def test_weak_catalyst_is_de_boosted():
    half = _catalyst_tilt() * 0.5
    cat = {"ABCD"}
    assert catalyst_viability_delta("ABCD", cat) == half                       # normal boost
    assert catalyst_viability_delta("ABCD", cat, weak_symbols={"ABCD"}) == 0.0  # weak -> no boost
    assert catalyst_viability_delta("ABCD", cat, weak_symbols={"WXYZ"}) == half  # other set untouched


def test_byte_identical_without_weak_symbols():
    cat = {"ABCD"}
    assert catalyst_viability_delta("ABCD", cat) == catalyst_viability_delta(
        "ABCD", cat, weak_symbols=None
    )


def test_non_catalyst_name_unaffected_by_weak_set():
    # a name with NO news isn't boosted anyway; the weak set is a no-op for it.
    assert catalyst_viability_delta("ABCD", {"OTHER"}, weak_symbols={"ABCD"}) == 0.0


def test_crypto_always_zero():
    assert catalyst_viability_delta("BTC-USD", {"BTC"}, weak_symbols={"BTC"}) == 0.0
