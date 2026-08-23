"""Isang outcome-scan para sa tatlong day-state gate (2026-08-23 tick cost).

Ang daily-loss cap, ang profit-giveback halt at ang green-to-red breaker ay
tatlong beses na naglalakad sa outcome rows ng araw — magkakaparehong query,
tatlong ORM hydration, tatlong pass ng authoritative_label_for_outcome, kada
evaluation, sa PAREHONG causal frontier. Mas mahalaga ito ngayon: tumatakbo na
ang FSM kada ~2s (wake rails) sa halip na kada 10-30s.

Ang kritikal na katangian ay PARITY: ang gate verdict ay dapat eksaktong pareho.

Runnable: pytest tests/test_day_state_single_scan.py -v
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import risk_evaluator as re_mod


# ── numerical parity ng dalawang scan ──────────────────────────────────────

def test_running_peak_and_total_treats_none_as_zero():
    """Ang _daily_realized_pnl ay nag-SKIP ng None; ang peak-walk ay nagdadagdag
    ng 0.0 — magkapareho ang kabuuan."""
    peak, total = re_mod._running_peak_and_total([10.0, None, -3.0])
    assert total == 7.0
    peak2, total2 = re_mod._running_peak_and_total([10.0, -3.0])
    assert total2 == total and peak2 == peak


def test_peak_is_floored_at_zero():
    """Ang araw na hindi naging berde ay walang PEAK PROFIT na maibabalik."""
    peak, total = re_mod._running_peak_and_total([-5.0, -2.0])
    assert peak == 0.0 and total == -7.0


def test_order_does_not_change_the_total():
    """Ang order_by sa peak-scan ay nagbabago ng PEAK, hindi ng kabuuan — kaya
    ligtas na palitan ang _daily_realized_pnl ng `current`."""
    a_peak, a_total = re_mod._running_peak_and_total([5.0, -8.0, 4.0])
    b_peak, b_total = re_mod._running_peak_and_total([4.0, 5.0, -8.0])
    assert a_total == b_total == 1.0
    assert a_peak == 5.0 and b_peak == 9.0


def test_both_scans_share_identical_filters():
    """Parehong day bounds, frontier at scope — ang order_by lang ang dagdag."""
    a = inspect.getsource(re_mod._daily_realized_pnl)
    b = inspect.getsource(re_mod._daily_realized_pnl_peak_and_current)
    for needle in (
        "MomentumAutomationOutcome.terminal_at >= day_start_utc",
        "MomentumAutomationOutcome.terminal_at < day_end_utc",
        "MomentumAutomationOutcome.terminal_at <= frontier_utc",
        "_scope_daily_outcome_query(",
        "_broker_label_available_as_of(o, frontier_utc=frontier_utc)",
        "authoritative_label_for_outcome(o)",
        "if not is_rec:",
    ):
        assert needle in a, needle
        assert needle in b, needle


# ── precomputed passthrough = byte-identical verdict ───────────────────────

class _Db:
    """Nagbibilang ng scan para mapatunayan na isa lang ang tumakbo."""

    def __init__(self, scans):
        self.scans = scans


def _patch_scan(monkeypatch, value, counter):
    def _fake(db, user_id, execution_family=None, *, as_of_utc=None):
        counter.append(1)
        return value

    monkeypatch.setattr(re_mod, "_daily_realized_pnl_peak_and_current", _fake)


def test_giveback_uses_precomputed_and_skips_the_scan(monkeypatch):
    counter: list[int] = []
    _patch_scan(monkeypatch, (500.0, 200.0), counter)
    monkeypatch.setattr(
        re_mod, "equity_relative_daily_loss_cap", lambda *a, **k: 250.0
    )
    fresh = re_mod.evaluate_profit_giveback_halt(None, user_id=1)
    assert len(counter) == 1
    passed = re_mod.evaluate_profit_giveback_halt(
        None, user_id=1, peak_and_current=(500.0, 200.0)
    )
    assert len(counter) == 1  # walang pangalawang scan
    assert passed == fresh  # eksaktong parehong verdict


def test_green_to_red_uses_precomputed_and_skips_the_scan(monkeypatch):
    counter: list[int] = []
    _patch_scan(monkeypatch, (300.0, -10.0), counter)
    monkeypatch.setattr(
        re_mod, "equity_relative_daily_loss_cap", lambda *a, **k: 250.0
    )
    fresh = re_mod.evaluate_green_to_red_halt(None, user_id=1)
    assert len(counter) == 1
    passed = re_mod.evaluate_green_to_red_halt(
        None, user_id=1, peak_and_current=(300.0, -10.0)
    )
    assert len(counter) == 1
    assert passed == fresh
    assert passed["halted"] is True  # peak 300 >= 125 activation, current <= 0


def test_default_none_preserves_legacy_callers(monkeypatch):
    """Ang ibang caller (auto_arm, automation_query, live_runner) ay hindi
    nagpapasa ng tuple — dapat kumuwenta pa rin sila mismo."""
    counter: list[int] = []
    _patch_scan(monkeypatch, (0.0, 0.0), counter)
    monkeypatch.setattr(
        re_mod, "equity_relative_daily_loss_cap", lambda *a, **k: 250.0
    )
    re_mod.evaluate_profit_giveback_halt(None, user_id=1)
    re_mod.evaluate_green_to_red_halt(None, user_id=1)
    assert len(counter) == 2


def test_halt_verdicts_unchanged_across_the_boundary(monkeypatch):
    """Grid sa paligid ng bawat threshold: pareho ang verdict sa precomputed."""
    monkeypatch.setattr(
        re_mod, "equity_relative_daily_loss_cap", lambda *a, **k: 250.0
    )
    for peak, current in [
        (0.0, 0.0), (249.0, 100.0), (250.0, 125.0), (250.0, 124.9),
        (500.0, 250.0), (500.0, 249.9), (300.0, 0.0), (300.0, 0.1),
        (124.0, -5.0), (125.0, -5.0), (-0.0, -50.0),
    ]:
        counter: list[int] = []
        _patch_scan(monkeypatch, (peak, current), counter)
        assert re_mod.evaluate_profit_giveback_halt(
            None, user_id=1
        ) == re_mod.evaluate_profit_giveback_halt(
            None, user_id=1, peak_and_current=(peak, current)
        ), (peak, current)
        assert re_mod.evaluate_green_to_red_halt(
            None, user_id=1
        ) == re_mod.evaluate_green_to_red_halt(
            None, user_id=1, peak_and_current=(peak, current)
        ), (peak, current)


# ── hot path wiring ────────────────────────────────────────────────────────

def test_evaluator_scans_once_and_shares_it():
    src = inspect.getsource(re_mod.evaluate_proposed_momentum_automation)
    assert "_day_peak, daily_pnl = _daily_realized_pnl_peak_and_current(" in src
    assert src.count("peak_and_current=_day_state") == 2
    # ang lumang hiwalay na daily-loss scan ay wala na sa hot path
    assert "daily_pnl = _daily_realized_pnl(" not in src
