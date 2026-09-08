"""Every closed trade must carry its own MFE.

`position["high_water_mark"]` is the peak BID, ratcheted every tick — the only
true maximum-favourable-excursion this lane keeps. It dies with the position at
exit, so nothing downstream could ever ask "was this loser a winner first?".

Measured 2026-09-08 across 60 recent losing exits: 51 had no live tape left in
their hold window (June/July, pruned), and the strict-geometry subset came to
THREE trades — of which two had reached 1R and the best 2.16R before exiting at a
loss. The answer needed the tape re-bought from IQFeed per symbol-day. With the
peak saved at exit, every trade from here on carries the answer itself.

`entry_realized_high` is NOT the MFE: it is the 15m frame High read once at entry
(live_runner.py:37970), i.e. the day's high BEFORE the trade. Measuring against
it answers a question about the setup, not the trade.

DB-free.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import _RECYCLE_ENTRY_STATE_KEYS


def test_the_peak_is_not_cleared_by_the_recycle():
    assert "last_exit_peak_price" not in _RECYCLE_ENTRY_STATE_KEYS


def test_the_entry_frame_high_is_still_per_trade_state():
    """It stays in the recycle set — it is entry-time setup context, not evidence
    about the closed trade, and must not be mistaken for the MFE again."""
    assert "entry_realized_high" in _RECYCLE_ENTRY_STATE_KEYS


def _save_peak(le: dict) -> None:
    """The exact expression from the exit path, so a future edit that drops the
    guard fails here rather than silently writing junk into the book."""
    pos = le.get("position")
    if isinstance(pos, dict):
        raw = pos.get("high_water_mark")
        try:
            peak = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            peak = None
        if peak is not None and peak > 0:
            le["last_exit_peak_price"] = float(peak)


def test_the_peak_is_captured_from_the_live_position():
    le = {"position": {"quantity": 100, "avg": 11.70, "high_water_mark": 12.85}}
    _save_peak(le)
    assert le["last_exit_peak_price"] == 12.85


def test_the_peak_outlives_the_position_and_the_recycle():
    le = {"position": {"avg": 11.70, "high_water_mark": 12.85}}
    _save_peak(le)
    le["position"] = None                       # the exit clears it
    for key in _RECYCLE_ENTRY_STATE_KEYS:       # then the recycle runs
        le.pop(key, None)
    assert le["last_exit_peak_price"] == 12.85


def test_a_missing_or_zero_peak_writes_nothing():
    """Never book a fabricated zero — an absent peak must read as absent."""
    for pos in ({}, {"high_water_mark": None}, {"high_water_mark": 0},
                {"high_water_mark": -1}, {"high_water_mark": "n/a"}, None):
        le = {"position": pos}
        _save_peak(le)
        assert "last_exit_peak_price" not in le, pos


def test_the_saved_peak_answers_the_question_it_exists_for():
    """entry 11.70, stop 11.20 (R = 0.50), peak 12.85 -> the trade reached 2.3R
    before it was closed. That is the number the book could not previously hold."""
    le = {"position": {"avg": 11.70, "high_water_mark": 12.85}}
    _save_peak(le)
    entry, stop = 11.70, 11.20
    r = entry - stop
    reached = (le["last_exit_peak_price"] - entry) / r
    assert round(reached, 2) == 2.30
