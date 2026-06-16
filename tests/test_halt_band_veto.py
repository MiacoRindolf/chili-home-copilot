"""Re-analysis survivor S3: LULD halt-band proximity veto (video 60). Buying a dip whose
STOP sits inside / just above the LULD down-halt band risks getting halt-TRAPPED on a
further drop (the stock halts, the position can't be exited at the stop — an asymmetric
small-cap tail loss). A protective veto on the dip-buy path; equity-only.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.entry_gates import halt_band_trapped, luld_down_band


# ── luld_down_band tiers ─────────────────────────────────────────────────────

def test_band_tier_above_3():
    assert abs(luld_down_band(10.0) - 9.0) < 1e-9       # 10% band


def test_band_tier_075_to_3():
    assert abs(luld_down_band(2.0) - 1.6) < 1e-9        # 20% band


def test_band_tier_subdollar():
    assert abs(luld_down_band(0.50) - 0.35) < 1e-9      # lesser of 75% / $0.15 -> 0.30 -> 0.35
    assert abs(luld_down_band(0.10) - 0.025) < 1e-9     # 0.15/0.10=1.5 capped to 0.75


def test_band_bad_input():
    assert luld_down_band(0) == 0.0
    assert luld_down_band(-1) == 0.0


# ── halt_band_trapped ────────────────────────────────────────────────────────

def test_stop_inside_band_is_trapped():
    # entry 10, band 9.0; stop 9.2 -> risk 0.8, band+0.5*risk=9.4; 9.2<=9.4 -> trapped
    assert halt_band_trapped(10.0, 9.2) is True


def test_stop_clear_of_band_is_safe():
    # entry 10, band 9.0; stop 9.6 -> risk 0.4, band+0.5*risk=9.2; 9.6>9.2 -> safe
    assert halt_band_trapped(10.0, 9.6) is False


def test_stop_below_band_is_trapped():
    assert halt_band_trapped(10.0, 8.5) is True   # stop already below the halt band


def test_degenerate_inputs_fail_open():
    assert halt_band_trapped(10.0, 10.0) is False   # zero risk
    assert halt_band_trapped(0, 0) is False
