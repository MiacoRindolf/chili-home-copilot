"""US equity market calendar helpers for the momentum lane.

Keep this tiny and deterministic: it is a safety calendar, not a trading signal.
Unknown dates fail open to the normal weekday clock; listed holidays fail closed.
"""
from __future__ import annotations

from datetime import date, timedelta


US_MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)


def is_us_market_holiday(d: date) -> bool:
    return d in US_MARKET_HOLIDAYS


def is_pre_holiday(d: date) -> bool:
    """True when the next calendar day is a listed US market holiday."""
    try:
        return (d + timedelta(days=1)) in US_MARKET_HOLIDAYS
    except Exception:
        return False
