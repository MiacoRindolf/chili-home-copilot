"""Midday-lull entry de-weight (project_profitability_levers).

A SOFT raise of the entry viability bar for NEW equity entries during the existing
10:30-14:30 ET ``schedule_window_now`` "midday" window (live data: 6% midday win-rate
vs 29% morning). Entry-side ONLY; crypto exempt; OFF / bump<=0 => byte-identical.

These tests cover the two pure, load-bearing units:
  * ``market_profile.in_midday_lull``           — the window/crypto/DST predicate
  * ``live_runner._midday_viability_bump``       — the kill-switch + knob resolver
  * ``live_runner._effective_entry_viability_min`` — the bar-raise + clamp the
    WATCHING_LIVE advance consumes (the only place the bump enters the FSM).
"""

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural.market_profile import in_midday_lull
from app.services.trading.momentum_neural.live_runner import (
    _effective_entry_viability_min,
    _midday_viability_bump,
)


# EDT (summer, June 2026, UTC-4): ET = UTC - 4 ; EST (winter, Jan 2026, UTC-5): ET = UTC - 5.
def _edt(hh, mm, ss=0, day=17):  # 2026-06-17 is a Wednesday
    return datetime(2026, 6, day, hh + 4, mm, ss, tzinfo=timezone.utc)


@pytest.fixture
def midday_on(monkeypatch):
    """Lever armed at the default bump (0.05)."""
    monkeypatch.setattr(settings, "chili_momentum_midday_deweight_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_midday_viability_bump", 0.05, raising=False)


@pytest.fixture
def midday_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_midday_deweight_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_midday_viability_bump", 0.05, raising=False)


# ---------------------------------------------------------------- resolver ----
def test_resolver_off_returns_zero(midday_off):
    assert _midday_viability_bump() == 0.0


def test_resolver_on_returns_bump(midday_on):
    assert _midday_viability_bump() == pytest.approx(0.05)


def test_resolver_enabled_but_zero_bump_is_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_midday_deweight_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_midday_viability_bump", 0.0, raising=False)
    assert _midday_viability_bump() == 0.0


def test_resolver_negative_bump_treated_as_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_midday_deweight_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_midday_viability_bump", -0.10, raising=False)
    assert _midday_viability_bump() == 0.0


# ------------------------------------------------------------ window truth ----
@pytest.mark.parametrize("h,m", [(10, 30), (11, 0), (12, 0), (13, 59), (14, 29)])
def test_in_midday_lull_true_inside(h, m):
    assert in_midday_lull("AAPL", now=_edt(h, m)) is True


@pytest.mark.parametrize("h,m", [(10, 29), (14, 30), (9, 35), (15, 30), (3, 0), (16, 30)])
def test_in_midday_lull_false_outside(h, m):
    assert in_midday_lull("AAPL", now=_edt(h, m)) is False


def test_in_midday_lull_boundaries():
    # half-open [10:30, 14:30): morning + power-hour windows UNTOUCHED
    assert in_midday_lull("AAPL", now=_edt(10, 29, 59)) is False  # open drive
    assert in_midday_lull("AAPL", now=_edt(10, 30, 0)) is True
    assert in_midday_lull("AAPL", now=_edt(14, 29, 59)) is True
    assert in_midday_lull("AAPL", now=_edt(14, 30, 0)) is False   # late/power-hr


def test_in_midday_lull_weekend_false():
    # 2026-06-20 Sat, 2026-06-21 Sun — noon ET, never a lull
    assert in_midday_lull("AAPL", now=_edt(12, 0, day=20)) is False
    assert in_midday_lull("AAPL", now=_edt(12, 0, day=21)) is False


def test_in_midday_lull_crypto_exempt():
    assert in_midday_lull("BTC-USD", now=_edt(12, 0)) is False
    assert in_midday_lull("ETH-USD", now=_edt(11, 30)) is False


def test_in_midday_lull_dst_correct():
    # SAME 11:00 ET wall-clock in EDT (summer, 15:00Z) and EST (winter, 16:00Z)
    # must BOTH be a lull — proves the America/New_York clock, not a fixed offset.
    summer_11et = datetime(2026, 6, 17, 15, 0, tzinfo=timezone.utc)   # EDT UTC-4
    winter_11et = datetime(2026, 1, 14, 16, 0, tzinfo=timezone.utc)   # EST UTC-5 (Wed)
    assert in_midday_lull("AAPL", now=summer_11et) is True
    assert in_midday_lull("AAPL", now=winter_11et) is True


# -------------------------------------------------- effective bar (wiring) ----
def test_effective_min_off_byte_identical(midday_off):
    # even AT a midday timestamp, OFF => flat bar, not a lull, no raise
    assert _effective_entry_viability_min(0.60, "AAPL", now=_edt(12, 0)) == (0.60, False, 0.0)


def test_effective_min_midday_raises(midday_on):
    eff, lull, bump = _effective_entry_viability_min(0.60, "AAPL", now=_edt(12, 0))
    assert lull is True
    assert bump == pytest.approx(0.05)
    assert eff == pytest.approx(0.65)


def test_effective_min_morning_untouched(midday_on):
    # 10:00 ET (open drive) — armed but outside the lull => flat bar unchanged
    eff, lull, bump = _effective_entry_viability_min(0.60, "AAPL", now=_edt(10, 0))
    assert (eff, lull) == (pytest.approx(0.60), False)


def test_effective_min_powerhour_untouched(midday_on):
    eff, lull, _ = _effective_entry_viability_min(0.60, "AAPL", now=_edt(15, 0))
    assert (eff, lull) == (pytest.approx(0.60), False)


def test_effective_min_clamped_to_ceiling(midday_on):
    # flat 0.93 + 0.05 would be 0.98 -> clamp to the 0.95 schema ceiling
    eff, lull, _ = _effective_entry_viability_min(0.93, "AAPL", now=_edt(12, 0))
    assert lull is True
    assert eff == pytest.approx(0.95)


def test_effective_min_crypto_untouched_even_when_on(midday_on):
    # crypto is exempt: the bump is resolved (0.05) but in_midday_lull -> False
    eff, lull, bump = _effective_entry_viability_min(0.60, "BTC-USD", now=_edt(12, 0))
    assert (eff, lull) == (pytest.approx(0.60), False)
    assert bump == pytest.approx(0.05)


def test_effective_min_marginal_cohort_deweighted(midday_on):
    # the cohort the lever targets: viability in [flat, eff) is held back at midday
    flat = 0.60
    eff, lull, _ = _effective_entry_viability_min(flat, "AAPL", now=_edt(12, 0))
    marginal = 0.63  # would clear flat 0.60, fails the raised 0.65
    exceptional = 0.72  # clears the raised bar -> still arms
    assert flat <= marginal < eff           # de-weighted out
    assert exceptional >= eff               # exceptional mover still admits
