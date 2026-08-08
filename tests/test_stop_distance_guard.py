"""Regression tests for the stop-distance guard (QTEX trade 2338, 2026-06-15).

QTEX was a low-float runner that based near ~$0.40 and ran to a $2.60 high.
The pattern/swing ATR geometry computed a stop at $0.3746 — **82.7% below** a
$2.16 entry — anchoring to the pre-run base instead of recent structure and
putting the whole ~$2,100 position at risk with effectively no protection.

The guard caps the stop DISTANCE at an adaptive (env-overridable, kill-switchable)
fraction of entry, tightening (never widening) the stop and scaling the target
to preserve reward:risk. Genuine swing setups stay byte-identical.

No DB access.
"""
from __future__ import annotations

import pytest

from app.services.trading.stop_distance_guard import (
    bound_stop_distance,
    max_stop_distance_fraction,
)

# QTEX live inputs (trade 2338).
QTEX_ENTRY = 2.16
QTEX_BAD_STOP = 0.3746
QTEX_TARGET = 2.96


# ── The guard primitive ──────────────────────────────────────────────


def test_qtex_like_stop_is_clamped_within_band():
    """The catastrophic 82.7%-away stop is tightened to the stock ceiling."""
    new_stop, new_target, info = bound_stop_distance(
        entry=QTEX_ENTRY, stop=QTEX_BAD_STOP, target=QTEX_TARGET,
        is_long=True, crypto=False,
    )
    assert info is not None, "QTEX-class stop must trip the guard"
    cap = max_stop_distance_fraction(crypto=False)  # 0.30 default
    new_frac = (QTEX_ENTRY - new_stop) / QTEX_ENTRY
    # Stop now sits at the ceiling, not 82.7% below entry.
    assert new_frac == pytest.approx(cap, abs=1e-6)
    assert new_stop > QTEX_BAD_STOP  # tightened (moved up toward entry)
    assert new_stop < QTEX_ENTRY


def test_qtex_clamp_slashes_dollar_risk():
    """Risk drops from ~$2,083 to a bounded fraction (qty=1167)."""
    qty = 1167.0
    orig_risk = (QTEX_ENTRY - QTEX_BAD_STOP) * qty
    new_stop, _t, info = bound_stop_distance(
        entry=QTEX_ENTRY, stop=QTEX_BAD_STOP, target=QTEX_TARGET, crypto=False,
    )
    new_risk = (QTEX_ENTRY - new_stop) * qty
    assert orig_risk > 2000.0
    assert new_risk < orig_risk * 0.40  # 0.30/0.827 ~= 0.36 of original
    assert info["orig_stop_frac"] == pytest.approx(0.8266, abs=1e-3)


def test_reward_risk_ratio_is_preserved_on_clamp():
    """Tightening the stop scales the target by the same factor (R:R intact)."""
    orig_rr = (QTEX_TARGET - QTEX_ENTRY) / (QTEX_ENTRY - QTEX_BAD_STOP)
    new_stop, new_target, info = bound_stop_distance(
        entry=QTEX_ENTRY, stop=QTEX_BAD_STOP, target=QTEX_TARGET, crypto=False,
    )
    assert new_target is not None
    new_rr = (new_target - QTEX_ENTRY) / (QTEX_ENTRY - new_stop)
    assert new_rr == pytest.approx(orig_rr, rel=1e-6)
    # Target tightened too (no fantasy-far ATR target left behind).
    assert QTEX_ENTRY < new_target < QTEX_TARGET


def test_normal_swing_stop_passes_through_unchanged():
    """A 5%-below stop is well under the ceiling — no clamp, no mutation."""
    new_stop, new_target, info = bound_stop_distance(
        entry=100.0, stop=95.0, target=110.0, is_long=True, crypto=False,
    )
    assert info is None
    assert new_stop == 95.0
    assert new_target == 110.0


def test_boundary_just_under_ceiling_not_clamped():
    cap = max_stop_distance_fraction(crypto=False)
    entry = 100.0
    stop = entry * (1.0 - (cap - 0.001))
    _s, _t, info = bound_stop_distance(entry=entry, stop=stop, crypto=False)
    assert info is None


def test_boundary_just_over_ceiling_clamped():
    cap = max_stop_distance_fraction(crypto=False)
    entry = 100.0
    stop = entry * (1.0 - (cap + 0.05))
    new_stop, _t, info = bound_stop_distance(entry=entry, stop=stop, crypto=False)
    assert info is not None
    assert (entry - new_stop) / entry == pytest.approx(cap, abs=1e-9)


def test_short_direction_is_symmetric():
    """A short stop too far ABOVE entry is tightened down to the ceiling."""
    entry = 100.0
    stop = 180.0  # 80% above entry
    target = 40.0
    new_stop, new_target, info = bound_stop_distance(
        entry=entry, stop=stop, target=target, is_long=False, crypto=False,
    )
    assert info is not None
    cap = max_stop_distance_fraction(crypto=False)
    assert (new_stop - entry) / entry == pytest.approx(cap, abs=1e-9)
    assert new_stop < stop  # tightened (moved down toward entry)
    # A short's target sits BELOW entry; preserving R:R keeps it below entry.
    assert new_target is not None
    assert new_target < entry
    assert new_target > target  # target tightened toward entry too


def test_crypto_uses_its_own_ceiling():
    assert max_stop_distance_fraction(crypto=True) == pytest.approx(0.35)
    assert max_stop_distance_fraction(crypto=False) == pytest.approx(0.30)
    # A 60%-below crypto stop clamps to 0.35, not 0.30.
    new_stop, _t, info = bound_stop_distance(
        entry=10.0, stop=4.0, crypto=True,
    )
    assert info is not None
    assert (10.0 - new_stop) / 10.0 == pytest.approx(0.35, abs=1e-9)


def test_kill_switch_via_env_disables_guard(monkeypatch):
    """Setting the knob >= 2.0 reverts to legacy unbounded geometry."""
    monkeypatch.setenv("CHILI_MAX_STOP_DISTANCE_FRACTION_STOCK", "2.0")
    new_stop, new_target, info = bound_stop_distance(
        entry=QTEX_ENTRY, stop=QTEX_BAD_STOP, target=QTEX_TARGET, crypto=False,
    )
    assert info is None
    assert new_stop == QTEX_BAD_STOP
    assert new_target == QTEX_TARGET


def test_env_override_tightens_band(monkeypatch):
    monkeypatch.setenv("CHILI_MAX_STOP_DISTANCE_FRACTION_STOCK", "0.15")
    assert max_stop_distance_fraction(crypto=False) == pytest.approx(0.15)
    new_stop, _t, info = bound_stop_distance(
        entry=100.0, stop=80.0, crypto=False,  # 20% away
    )
    assert info is not None  # 20% > 15% ceiling now trips
    assert (100.0 - new_stop) / 100.0 == pytest.approx(0.15, abs=1e-9)


def test_bad_inputs_are_left_untouched():
    # Non-positive entry / stop, wrong-sided stop, None stop → no-op.
    assert bound_stop_distance(entry=0.0, stop=1.0)[2] is None
    assert bound_stop_distance(entry=10.0, stop=0.0)[2] is None
    assert bound_stop_distance(entry=10.0, stop=None)[2] is None
    # Wrong-sided long stop (above entry) is a different bug — don't mask it.
    s, t, info = bound_stop_distance(entry=10.0, stop=12.0, is_long=True)
    assert info is None and s == 12.0


# ── Integration: the scanner geometry chokepoint ─────────────────────


def test_scanner_long_atr_levels_clamps_qtex():
    from app.services.trading.scanner import _long_atr_trade_levels

    # ATR ~= 0.7142 reproduces the live stop (2.16 - 2.5*0.7142 = 0.3745),
    # and 0.7142/2.16 = 0.331 < 0.35 so it passes the ATR/price guard.
    levels = _long_atr_trade_levels(
        2.16, 0.7142, stop_mult=2.5, target_mult=1.13, crypto=False,
    )
    assert levels is not None
    entry, stop, target = levels
    frac = (entry - stop) / entry
    # ~0.30 ceiling, plus up to one tick of price rounding on a ~$2 equity.
    assert frac == pytest.approx(0.30, abs=0.01), f"stop {frac:.1%} below entry"
    assert stop > QTEX_BAD_STOP  # decisively tighter than the live bug


def test_scanner_normal_candidate_unaffected():
    from app.services.trading.scanner import _long_atr_trade_levels

    # AAPL-like: ATR 2% of price, mult 2.0 -> 4% stop, well under ceiling.
    levels = _long_atr_trade_levels(
        100.0, 2.0, stop_mult=2.0, target_mult=3.0, crypto=False,
    )
    assert levels is not None
    entry, stop, target = levels
    assert stop == pytest.approx(96.0, abs=1e-6)
    assert target == pytest.approx(106.0, abs=1e-6)


# ── Integration: the live-bracket chokepoint ─────────────────────────


def test_compute_initial_stop_clamps_qtex():
    from app.services.trading.stop_engine import _compute_initial_stop

    sl, tp = _compute_initial_stop(
        entry=2.16, direction="long", atr=0.7142, price=2.16,
        stop_model="atr_swing", is_crypto=False, brain=None,
    )
    frac = (2.16 - sl) / 2.16
    # ~0.30 ceiling, plus up to one tick of price rounding on a ~$2 equity.
    assert frac == pytest.approx(0.30, abs=0.01)
    assert sl > QTEX_BAD_STOP


def test_compute_initial_stop_normal_unaffected():
    from app.services.trading.stop_engine import _compute_initial_stop

    sl, tp = _compute_initial_stop(
        entry=100.0, direction="long", atr=2.0, price=100.0,
        stop_model="atr_swing", is_crypto=False, brain=None,
    )
    # atr_swing stop_mult_normal=2.0 -> stop 96.0, unchanged by the guard.
    assert sl == pytest.approx(96.0, abs=1e-3)
