"""SPREAD GATE: expected-move frame — ang doctrine change ng 2026-08-20.

Sinusukat ng gate ang crossing cost. Dati, hinahambing ito sa layo ng STOP — pero
ang stop ng momentum entry ay sadyang makipot, kaya sa mga breakout name ay
tinanggihan nito ang mga spread na karaniwan lang para sa inaasahang galaw.
Sinukat live sa TUNAY na libro (hindi phantom):

    BTMD  spread cost $5.16  vs structural risk $4.67   -> VETO
    CJMB  spread cost $13.4  vs structural risk $18.58  -> VETO (72% > 25%)

Si Ross ay nagbabayad ng ~1-3% spread laban sa 10-20% na inaasahang galaw — ang
kanyang aritmetika ay spread laban sa INAASAHANG KITA, hindi laban sa stop.
Desisyon ng operator: expected-move frame, may absolute sanity ceiling, at
FAIL-CLOSED pabalik sa lumang stop-based test kapag walang expected move.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.live_runner import (
    _entry_spread_risk_decision,
)


def _btmd(expected_move_bps=None, **overrides):
    """Ang tunay na BTMD na sandali: 1.57/1.60, qty ~172, stop $0.027/sh."""
    kwargs = dict(
        bid=1.57, ask=1.60, quantity=172.0,
        stop_distance=0.0272, max_fraction=0.25,
        expected_move_bps=expected_move_bps,
    )
    kwargs.update(overrides)
    return _entry_spread_risk_decision(**kwargs)


# ─────────────── ang mga tunay na sandali ng 08-20 ───────────────


def test_btmd_real_book_passes_under_expected_move(monkeypatch):
    """189bps na spread, ~1900bps na expected move = 10% -> PASA na ngayon."""
    ok, gate = _btmd(expected_move_bps=1900.0)
    assert ok is True, gate
    assert gate["gate_frame"] == "expected_move"
    assert gate["reason"] == "within_budget"
    # Ang lumang framing ay nakatala pa rin para sa expectancy honesty.
    assert gate["spread_fraction_of_risk"] is not None


def test_btmd_under_old_frame_still_vetoes():
    """PARITY: walang expected move -> ang lumang stop-based test, VETO pa rin."""
    ok, gate = _btmd(expected_move_bps=None)
    assert ok is False
    assert gate["gate_frame"] == "structural_risk"
    assert gate["reason"] == "spread_consumes_too_much_risk"


def test_flag_off_restores_stop_frame(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_entry_spread_vs_expected_move_enabled", False,
        raising=False,
    )
    ok, gate = _btmd(expected_move_bps=1900.0)
    assert ok is False
    assert gate["gate_frame"] == "structural_risk"


# ─────────────── ang mga hangganan ng bagong frame ───────────────


def test_spread_eating_the_move_is_vetoed():
    """Ang spread na kumakain ng malaking bahagi ng galaw ay hindi babayaran:
    189bps laban sa 600bps na inaasahan = 31% > 15% -> VETO."""
    ok, gate = _btmd(expected_move_bps=600.0)
    assert ok is False
    assert gate["reason"] == "spread_exceeds_expected_move_budget"


def test_abs_ceiling_binds_even_on_huge_expected_moves(monkeypatch):
    """SANITY: kahit 100% ang inaasahang galaw, ang 5%+ na libro ay sira."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_spread_abs_ceiling_bps", 500.0,
        raising=False,
    )
    # 1.34/1.61 = 1830bps (ang phantom IEX flash ng BTMD) laban sa 10000bps move:
    # 18% < ceiling? 1830 > 500 abs -> VETO pa rin.
    ok, gate = _btmd(
        bid=1.34, ask=1.61, expected_move_bps=10_000.0
    )
    assert ok is False
    assert gate["reason"] == "spread_exceeds_expected_move_budget"
    assert gate["spread_budget_bps"] == 500.0


def test_zero_or_negative_expected_move_falls_back():
    for em in (0.0, -100.0, float("nan")):
        ok, gate = _btmd(expected_move_bps=em)
        assert gate["gate_frame"] == "structural_risk", em


def test_crossed_book_is_vetoed_in_both_frames():
    ok, gate = _btmd(bid=1.70, ask=1.60, expected_move_bps=1900.0)
    assert ok is False


def test_settings_wired_and_bounded():
    assert getattr(
        settings, "chili_momentum_entry_spread_vs_expected_move_enabled", None
    ) is True
    frac = float(getattr(
        settings, "chili_momentum_entry_spread_max_fraction_of_expected_move", -1
    ))
    # Dapat tanggapin ang Ross reference na ~10% (200/2000)...
    assert frac >= 0.10
    # ...pero hindi hayaang kainin ng spread ang kalahati ng galaw.
    assert frac <= 0.30
    ceil = float(getattr(
        settings, "chili_momentum_entry_spread_abs_ceiling_bps", -1
    ))
    # Dapat tanggapin ang tunay na Ross-style na 1-3% spread...
    assert ceil >= 300.0
    # ...pero mas makipot kaysa sa tape-sanity na 5000bps.
    assert ceil < 5000.0
