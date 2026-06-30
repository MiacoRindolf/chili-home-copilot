"""DESIGN #3 — adaptive profit targets (pure unit, no DB).

Covers adaptive_first_target_reward_risk, the scale_out_fraction vol tilt,
stop_target_prices threading + parity, and the live_runner _adaptive_scale_vol_pctl
mapping. Uses monkeypatch on app.config.settings attributes for the flags.
"""

import math

import pytest

from app.config import settings
from app.services.trading.momentum_neural.paper_execution import (
    adaptive_first_target_reward_risk,
    scale_out_fraction,
    stop_target_prices,
)
from app.services.trading.momentum_neural.live_runner import _adaptive_scale_vol_pctl


@pytest.fixture(autouse=True)
def _default_adaptive_on(monkeypatch):
    # Most cases assume the master flag ON with documented defaults.
    monkeypatch.setattr(settings, "chili_momentum_adaptive_target_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_adaptive_target_room_capture", 0.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_adaptive_target_rr_cap", 6.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_adaptive_scale_vol_tilt", 0.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_adaptive_scale_vol_ref_pct", 0.05, raising=False)


# ── adaptive_first_target_reward_risk ────────────────────────────────────────
def test_flag_off_returns_base(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_adaptive_target_enabled", False, raising=False)
    rr, meta = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=14.0)
    assert rr == 2.0
    assert meta["adaptive"] is False


def test_no_realized_high_returns_base():
    rr, meta = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=None)
    assert rr == 2.0
    rr2, _ = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=9.5)  # <= entry
    assert rr2 == 2.0


def test_room_lifts_rr():
    # entry 10, stop 9.4 (R=0.60), HOD 14 -> room_R = 4/0.6 = 6.667; capture 0.5 -> 3.333.
    # (Test the PURE rr helper here; stop_target_prices threading is tested separately on a
    # non-round entry so the round-number pull-in does not collapse the difference.)
    rr, meta = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=14.0)
    assert abs(rr - 3.3333333) < 1e-4
    assert meta["adaptive"] is True
    # raw rr_target (before any round-number pull-in) = 10 + 3.333*0.6 = 12.00.
    raw_target = 10.0 + rr * (10.0 - 9.4)
    assert abs(raw_target - 12.0) < 1e-3


def test_rr_capped():
    # huge realized_high -> rr_eff == rr_cap (6.0), never above.
    rr, meta = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=100.0)
    assert rr == 6.0
    assert meta["rr_cap"] == 6.0


def test_floor_respected():
    # tiny room (realized_high just above entry) -> rr_eff == base (max(base, ...)).
    rr, _ = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=10.05)
    assert rr == 2.0


def test_wider_stop_lowers_rr():
    # literature property: same realized_high, double the stop distance -> room_R halves
    # -> rr_eff strictly LOWER (monotone).
    rr_tight, _ = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.7, realized_high=14.0)  # R=0.3
    rr_wide, _ = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=9.4, realized_high=14.0)  # R=0.6
    assert rr_tight > rr_wide


def test_short_side_noop():
    rr, _ = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=10.0, stop=10.6, realized_high=8.0, side_long=False)
    assert rr == 2.0


# ── scale_out_fraction vol tilt ──────────────────────────────────────────────
def test_vol_pctl_none_identical():
    assert scale_out_fraction(symbol="AAPL", vol_pctl=None) == scale_out_fraction(symbol="AAPL")


def test_median_vol_identical(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    base = scale_out_fraction(symbol="AAPL")
    tilted = scale_out_fraction(symbol="AAPL", vol_pctl=0.5)
    assert abs(tilted - base) < 1e-9


def test_high_vol_smaller_runner_bigger(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    base = scale_out_fraction(symbol="AAPL")
    tilted = scale_out_fraction(symbol="AAPL", vol_pctl=1.0)  # tilt 0.5 -> 0.5*(1-0.5)=0.25
    assert abs(tilted - 0.25) < 1e-9
    assert tilted < base


def test_low_vol_larger_partial(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    base = scale_out_fraction(symbol="AAPL")
    tilted = scale_out_fraction(symbol="AAPL", vol_pctl=0.0)  # 0.5*(1+0.5)=0.75
    assert abs(tilted - 0.75) < 1e-9
    assert tilted > base


def test_clamp_floor_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_adaptive_scale_vol_tilt", 1.0, raising=False)
    hi = scale_out_fraction(symbol="AAPL", vol_pctl=1.0)
    lo = scale_out_fraction(symbol="AAPL", vol_pctl=0.0)
    assert 0.05 <= hi <= 0.95
    assert 0.05 <= lo <= 0.95


def test_flag_off_no_tilt(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_adaptive_target_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    base = scale_out_fraction(symbol="AAPL")
    tilted = scale_out_fraction(symbol="AAPL", vol_pctl=1.0)
    assert tilted == base


# ── stop_target_prices integration (still pure) ──────────────────────────────
def test_stop_target_realized_high_threads_through():
    # Use a non-round entry (12.73) so the round-number pull-in does not collapse both
    # targets to the SAME nearby psych level. Base 2:1 -> ~14.26 (no pull-in below it);
    # adaptive lifts the rr -> raw target clears $15 so the pull-in lands ON $15.
    _, target_with = stop_target_prices(
        12.73, atr_pct=0.06, side_long=True, stop_atr_mult=1.0,
        reward_risk=2.0, realized_high=12.73 * 1.4)
    _, target_without = stop_target_prices(
        12.73, atr_pct=0.06, side_long=True, stop_atr_mult=1.0,
        reward_risk=2.0, realized_high=None)
    assert target_with > target_without
    # passing None reproduces the existing 2:1 target EXACTLY (parity guard).
    _, parity = stop_target_prices(
        12.73, atr_pct=0.06, side_long=True, stop_atr_mult=1.0, reward_risk=2.0)
    assert target_without == parity


def test_round_number_pull_in_still_applies():
    # With a round number between 1R and the rr_eff target, the first-scale target still
    # pulls in (compose order unchanged) — the adaptive rr never pushes BELOW the pull-in.
    entry = 12.73
    stop_px, target_px = stop_target_prices(
        entry, atr_pct=0.06, side_long=True, stop_atr_mult=1.0,
        reward_risk=2.0, realized_high=entry * 1.4)
    risk = entry - stop_px
    # the raw adaptive rr_eff target (uncapped pull-in) is the ceiling; pull-in only pulls IN.
    rr_eff, _ = adaptive_first_target_reward_risk(
        base_reward_risk=2.0, entry=entry, stop=stop_px, realized_high=entry * 1.4)
    raw_target = entry + rr_eff * risk
    assert target_px <= raw_target + 1e-6
    # and at least 1R above entry.
    assert target_px >= entry + risk - 1e-6


# ── live_runner _adaptive_scale_vol_pctl ─────────────────────────────────────
def test_adaptive_scale_vol_pctl_maps_range():
    assert abs(_adaptive_scale_vol_pctl({"entry_day_range_pct": 0.05}) - 0.5) < 1e-9
    assert abs(_adaptive_scale_vol_pctl({"entry_day_range_pct": 0.10}) - 1.0) < 1e-9
    assert _adaptive_scale_vol_pctl({"entry_day_range_pct": 0.0}) is None
    assert _adaptive_scale_vol_pctl({}) is None
