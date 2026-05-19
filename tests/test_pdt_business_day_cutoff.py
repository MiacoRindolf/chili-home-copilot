"""f-pdt-business-day-cutoff (2026-05-19) tests.

Validates :func:`_earliest_business_day_in_window` returns midnight of the
correct date for every weekday-today case. Without this, the 9-calendar-day
proxy was over-counting day-trades by 2-4 days and blocking legitimate
entries on accounts below the $25K PDT equity threshold.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.trading.pdt_guard import _earliest_business_day_in_window


@pytest.mark.parametrize(
    "today_iso,expected_iso",
    [
        # Tuesday: window = prev-Wed .. today (Tue)
        ("2026-05-19T15:00:00", "2026-05-13T00:00:00"),
        # Monday: window = prev-Tue .. today (Mon)
        ("2026-05-18T15:00:00", "2026-05-12T00:00:00"),
        # Friday: window = Mon (this wk) .. today (Fri)
        ("2026-05-16T15:00:00", "2026-05-12T00:00:00"),
        # Wednesday: window = prev-Thu .. today (Wed)
        ("2026-05-14T15:00:00", "2026-05-08T00:00:00"),
        # Thursday: window = prev-Fri .. today (Thu)
        ("2026-05-15T15:00:00", "2026-05-09T00:00:00"),
    ],
)
def test_earliest_business_day_for_weekday(today_iso: str, expected_iso: str) -> None:
    now = datetime.fromisoformat(today_iso)
    result = _earliest_business_day_in_window(now, window_business_days=5)
    expected = datetime.fromisoformat(expected_iso)
    assert result == expected, (
        f"today={today_iso}: expected cutoff midnight {expected_iso}, got {result}"
    )


def test_window_includes_today() -> None:
    """The 5-day window must include today as one of the 5 business days."""
    # Today is Tue 2026-05-19. 5 BD window = 5/13, 5/14, 5/15, 5/18, 5/19.
    now = datetime(2026, 5, 19, 15, 0, 0)
    cutoff = _earliest_business_day_in_window(now, window_business_days=5)
    # The 5/19 (today) trade at any time today is AFTER cutoff (5/13 00:00)
    assert datetime(2026, 5, 19, 14, 0, 0) > cutoff
    # The 5/13 trade (first day of window) at 14:01 is AFTER cutoff (5/13 00:00)
    assert datetime(2026, 5, 13, 14, 1, 0) > cutoff


def test_window_excludes_pre_window_trades() -> None:
    """Trades older than the 5-business-day window must NOT pass cutoff."""
    now = datetime(2026, 5, 19, 15, 0, 0)
    cutoff = _earliest_business_day_in_window(now, window_business_days=5)
    # The 5/12 (6 BD ago) and 5/11 (7 BD ago) trades from the bug report
    # are at exit_date 17:00 / 16:49 respectively. They are BEFORE cutoff
    # 5/13 midnight => should NOT count.
    assert datetime(2026, 5, 12, 17, 0, 0) < cutoff
    assert datetime(2026, 5, 11, 16, 49, 0) < cutoff


def test_smaller_windows_work() -> None:
    """Function generalizes for non-5 window sizes."""
    # window=1 on Tue should give midnight of today
    now = datetime(2026, 5, 19, 12, 0, 0)
    assert (
        _earliest_business_day_in_window(now, window_business_days=1)
        == datetime(2026, 5, 19, 0, 0, 0)
    )
    # window=2 on Tue should give midnight of Monday
    assert (
        _earliest_business_day_in_window(now, window_business_days=2)
        == datetime(2026, 5, 18, 0, 0, 0)
    )


def test_weekend_today_walks_to_friday() -> None:
    """If 'today' lands on a weekend (unlikely in practice but valid),
    the walker should produce a sensible result rooted at Friday.
    """
    # Sat 2026-05-16: walker starts at 5/16 (weekend, skip), 5/15 Fri (BD#1),
    # 5/14 Thu (BD#2), ..., 5/11 Mon (BD#5).
    now = datetime(2026, 5, 16, 12, 0, 0)
    result = _earliest_business_day_in_window(now, window_business_days=5)
    assert result == datetime(2026, 5, 11, 0, 0, 0)
