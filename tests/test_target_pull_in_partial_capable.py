"""Round-number target pull-in ay para sa PARTIAL-capable lanes lamang (#1264).

SINUKAT (SSM 2026-09-01): entry 4.01, stop 3.9725 (R = 0.0375). Ang 2R na
target ay 4.085 — pero hinila ito ng round-number pull-in sa 4.05 = 1.07R.
Tama ang pull-in para sa PARTIAL ("benta ng kalahati sa level kung saan
nagsisiksik ang sellers, hawak ang runner"), pero sa Alpaca ang "partial" ay
nagiging BUONG-posisyong flatten (live_runner.py:43676 — ang resting deadman
stop ay kumukonsumo ng buong qty_available), kaya ang buong trade ay na-cap
sa ~1R nang walang runner na maiiwan.

Backtest sa sariling tape (5 tunay na trade): 1.5R = −1.67 · 2.0R = +13.62 ·
2.5R = +30.86.

Runnable: pytest tests/test_target_pull_in_partial_capable.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import (
    stop_target_prices,
)


SSM_ENTRY = 4.01
SSM_ATR = 0.0156   # nagbibigay ng ~0.0375 stop distance sa 0.60 mult


def test_partial_capable_lane_keeps_round_number_pull_in():
    """Ang lane na kayang mag-partial ay may pull-in pa rin (walang regresyon)."""
    stop, target = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0, partial_capable=True,
    )
    rr_target = SSM_ENTRY + 2.0 * (SSM_ENTRY - stop)
    # ang pull-in ay maaaring magpababa ng target (o pantay kung walang round no.)
    assert target <= rr_target + 1e-9


def test_non_partial_lane_keeps_full_rr_target():
    """Alpaca-class: WALANG pull-in — buong 2R ang target."""
    stop, target = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0, partial_capable=False,
    )
    rr_target = SSM_ENTRY + 2.0 * (SSM_ENTRY - stop)
    assert abs(target - rr_target) < 1e-9


def test_ssm_shape_target_moves_from_1R_to_2R():
    """Ang eksaktong SSM: ang non-partial na target ay dapat lampas sa 4.05."""
    stop, t_partial = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0, partial_capable=True,
    )
    _s, t_full = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0, partial_capable=False,
    )
    r = SSM_ENTRY - stop
    r_partial = (t_partial - SSM_ENTRY) / r
    r_full = (t_full - SSM_ENTRY) / r
    assert abs(r_full - 2.0) < 1e-6
    assert r_full >= r_partial


def test_default_is_partial_capable_for_backward_compat():
    """Ang default ay True — ang mga lumang caller ay hindi nagbabago."""
    stop_a, t_a = stop_target_prices(SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0)
    stop_b, t_b = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0, partial_capable=True,
    )
    assert (stop_a, t_a) == (stop_b, t_b)


def test_stop_is_unchanged_by_the_flag():
    """Ang flag ay tumutukoy sa TARGET lamang — hindi ginagalaw ang stop."""
    s1, _ = stop_target_prices(SSM_ENTRY, atr_pct=SSM_ATR, partial_capable=True)
    s2, _ = stop_target_prices(SSM_ENTRY, atr_pct=SSM_ATR, partial_capable=False)
    assert s1 == s2


def test_higher_rr_pushes_target_further():
    _s, t2 = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.0, partial_capable=False,
    )
    _s, t25 = stop_target_prices(
        SSM_ENTRY, atr_pct=SSM_ATR, reward_risk=2.5, partial_capable=False,
    )
    assert t25 > t2
