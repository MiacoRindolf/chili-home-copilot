"""Time-of-day entry-quality bar (audit 2026-08-21) — ang post-open generic
volume triggers ay dine-defer; structural at premarket ay hindi ginagalaw.

Ebidensya: 162 entered equity trades — PREMARKET +$3,305 PF 20.1 (n=20);
OPEN/MIDDAY gross −$12.5k PF ~0.07 (n=124). PURE (no DB).
Runnable: pytest tests/test_postopen_quality_bar.py -v
"""
from app.services.trading.momentum_neural.risk_policy import (
    generic_trigger_postopen_deferred,
)

GENERIC = ["momentum_ok_rel_vol", "momentum_ok_rel_vol_rate",
           "momentum_ok_abs_vol", "momentum_ok_abs_vol_rate",
           "momentum_ok_tick_stream"]
STRUCTURAL = ["pullback_break", "double_bottom_break_tick_ok", "abcd_break",
              "halt_resume_dip", "flush_dip_buy", "bull_flag_break"]


def test_premarket_never_deferred():
    # None minutes = bago mag-9:30 ET -> buong repertoire, kahit generic.
    for r in GENERIC:
        assert generic_trigger_postopen_deferred(
            r, None, enabled=True, cutoff_min_after_open=30.0
        ) is False


def test_first_half_hour_not_deferred():
    for r in GENERIC:
        assert generic_trigger_postopen_deferred(
            r, 29.9, enabled=True, cutoff_min_after_open=30.0
        ) is False


def test_postopen_generic_deferred():
    """ANG AUDIT CASE: 10:00 ET pataas, bare volume triggers = churn -> defer."""
    for r in GENERIC:
        assert generic_trigger_postopen_deferred(
            r, 30.0, enabled=True, cutoff_min_after_open=30.0
        ) is True
        assert generic_trigger_postopen_deferred(
            r, 240.0, enabled=True, cutoff_min_after_open=30.0
        ) is True


def test_structural_never_deferred():
    """Ang mga pattern na may istruktura ay pumuputok buong araw."""
    for r in STRUCTURAL:
        assert generic_trigger_postopen_deferred(
            r, 240.0, enabled=True, cutoff_min_after_open=30.0
        ) is False


def test_disabled_never_defers():
    assert generic_trigger_postopen_deferred(
        "momentum_ok_rel_vol", 240.0, enabled=False, cutoff_min_after_open=30.0
    ) is False


def test_bad_basis_fails_open():
    assert generic_trigger_postopen_deferred(
        "momentum_ok_rel_vol", float("nan"), enabled=True,
        cutoff_min_after_open=30.0,
    ) is False
    assert generic_trigger_postopen_deferred(
        None, 240.0, enabled=True, cutoff_min_after_open=30.0
    ) is False
