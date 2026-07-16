"""Streak-adaptive risk dial — self-relative multiplier on per-trade max loss.

2026-06-13 de-contamination: the window now (a) segregates by execution_family (a
crypto/paper-twin loss must not de-risk the equity lane) and (b) counts only REAL
ENTERED trades via is_real_entry_outcome (a $0.00 cancelled_pre_entry was miscounted
as a loss). Bounds/formula are UNCHANGED — the 6 legacy tests below pass plain pnls
(defaulted to a real-entry class) and assert IDENTICAL results = no-op parity proof.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural.risk_policy import streak_risk_multiplier


def _row(r):
    # plain pnl -> (pnl, real-entry class); tuple -> (pnl, outcome_class) as given
    if isinstance(r, tuple):
        return (float(r[0]), str(r[1]))
    return (float(r), "stop_loss" if float(r) <= 0 else "success")


class _Q:
    def __init__(self, rows, counter):
        self._rows = rows
        self._counter = counter

    def filter(self, *a, **k):
        self._counter["filter"] += 1
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        # code fetches headroom then prunes + slices to 10 in Python; tests pass <40 rows
        return self

    def all(self):
        return list(self._rows)


def _db(rows):
    counter = {"filter": 0}
    norm = [_row(r) for r in rows]
    ns = SimpleNamespace(query=lambda *a, **k: _Q(norm, counter))
    ns._counter = counter
    return ns


# ---- legacy parity: identical results with default-None family + all real entries ----

def test_neutral_with_insufficient_history():
    m, meta = streak_risk_multiplier(_db([100, -50]))
    assert m == 1.0 and meta["reason"] == "insufficient_history"


def test_hot_streak_sizes_up():
    m, meta = streak_risk_multiplier(_db([200, 150, -50, 300, 120, 90, -30, 250, 180, 60]))  # 8/10 wins
    assert m == pytest.approx(1.3)
    assert meta["win_rate"] == pytest.approx(0.8)


def test_cold_streak_sizes_down():
    m, _ = streak_risk_multiplier(_db([-50, 100, -30, -80, -20, 110, -40, -60, 90, -10]))  # 3/10 wins
    assert m == pytest.approx(0.8)


def test_three_consecutive_losses_hard_floor():
    m, meta = streak_risk_multiplier(_db([-50, -30, -80, 200, 150, 120, 90, 250, 180, 60]))
    assert m == 0.5 and meta["consecutive_losses"] == 3


def test_bounds_never_exceeded():
    m_hi, _ = streak_risk_multiplier(_db([10] * 10))
    m_lo, _ = streak_risk_multiplier(_db([-10] * 10))
    assert m_hi == 1.5 and m_lo == 0.5


def test_error_fails_neutral():
    class _Boom:
        def query(self, *a, **k): raise RuntimeError("db down")
    m, meta = streak_risk_multiplier(_Boom())
    assert m == 1.0 and meta["reason"] == "error_fail_neutral"


# ---- de-contamination behaviour ----

def test_zero_notional_cancelled_pre_entry_excluded():
    # a $0.00 cancelled_pre_entry (realized NOT NULL) must NOT count as a loss
    rows = [(0.0, "cancelled_pre_entry"), (100, "success"), (150, "success"),
            (120, "success"), (90, "success"), (200, "success")]
    m, meta = streak_risk_multiplier(_db(rows))
    assert meta["n"] == 5                      # the cancelled row was pruned
    assert meta["win_rate"] == pytest.approx(1.0)
    assert m == 1.5


def test_stale_data_abort_real_loss_is_kept():
    # entered-then-force-closed real loss MUST still count (verified live -$238.68)
    rows = [(-238.68, "stale_data_abort"), (100, "success"), (150, "success"),
            (120, "success"), (90, "success")]
    m, meta = streak_risk_multiplier(_db(rows))
    assert meta["n"] == 5                       # stale_data_abort kept
    assert meta["win_rate"] == pytest.approx(0.8)
    assert m == pytest.approx(1.3)


def test_broker_truth_reclassified_kept_and_consec_floor():
    # mirrors the live RH window: 3 newest real losses -> hard floor; reclassified kept
    rows = [(-7.0, "stop_loss"), (-33.6, "stop_loss"), (-14.1, "stop_loss"),
            (15.62, "broker_truth_reclassified"), (-63.24, "broker_truth_reclassified")]
    m, meta = streak_risk_multiplier(_db(rows))
    assert meta["n"] == 5
    assert meta["consecutive_losses"] == 3 and m == 0.5


def test_consec_loss_counts_only_real_entries():
    # 2 leading NON-entry rows must not become 2 leading losses
    rows = [(0.0, "cancelled_pre_entry"), (0.0, "no_fill"), (-50, "stop_loss"),
            (100, "success"), (150, "success"), (120, "success"), (90, "success")]
    m, meta = streak_risk_multiplier(_db(rows))
    assert meta["n"] == 5                       # 2 non-entries pruned
    assert meta["consecutive_losses"] == 1     # only the real stop_loss, NOT 3 -> no floor
    assert m == pytest.approx(1.3)             # win_rate 0.8


def test_ambiguous_class_only_outcomes_remain_eligible_for_legacy_callers():
    from app.services.trading.momentum_neural.outcome_labels import (
        is_real_entry_outcome,
    )

    assert is_real_entry_outcome("cancelled_in_trade") is True
    assert is_real_entry_outcome("error_exit") is True
    assert is_real_entry_outcome("no_fill") is False


def test_window_headroom_finds_real_entries_past_non_entries():
    rows = ([(0.0, "cancelled_pre_entry")] * 3) + [(100, "success")] * 10
    m, meta = streak_risk_multiplier(_db(rows))
    assert meta["n"] == 10 and m == 1.5         # found the 10 real entries past the non-entries


def test_family_filter_fires_only_when_supplied():
    d1 = _db([100, 150, 120, 90, 200])
    streak_risk_multiplier(d1)                          # no family -> 1 filter call
    assert d1._counter["filter"] == 1
    d2 = _db([100, 150, 120, 90, 200])
    streak_risk_multiplier(d2, execution_family="robinhood_spot")  # +family -> 2 calls
    assert d2._counter["filter"] == 2
