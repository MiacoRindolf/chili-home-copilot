"""FX session tagging.

Maps a UTC datetime to one of:

  - ``sydney``        20:00-05:00 UTC (Sun-Thu)
  - ``tokyo``         23:00-08:00 UTC (Sun-Thu)
  - ``london``        07:00-16:00 UTC (Mon-Fri)
  - ``ny``            12:00-21:00 UTC (Mon-Fri)
  - ``tokyo_london``  07:00-08:00 UTC (overlap)
  - ``london_ny``     12:00-16:00 UTC (overlap, highest liquidity)
  - ``weekend``       Fri 21:00 - Sun 20:00 UTC (FX market closed)

Volatility and spread profiles differ dramatically across sessions:
``london_ny`` overlap is the highest-liquidity / tightest-spread window;
``weekend`` is closed; ``tokyo`` is range-bound for major pairs.

Strategy code reads the session tag to gate entries (e.g., London
breakout only fires during ``london``, news fade only during overlap).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional


SYDNEY = "sydney"
TOKYO = "tokyo"
LONDON = "london"
NY = "ny"
TOKYO_LONDON = "tokyo_london"
LONDON_NY = "london_ny"
WEEKEND = "weekend"


def session_for_utc(ts: datetime) -> str:
    """Return the FX session tag for a UTC timestamp.

    Args:
        ts: UTC datetime. Naive datetimes are interpreted as UTC.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    elif ts.tzinfo != timezone.utc:
        ts = ts.astimezone(timezone.utc)

    weekday = ts.weekday()  # Mon=0 .. Sun=6
    h = ts.hour

    # FX market closed Fri 21:00 UTC -> Sun 20:00 UTC.
    if weekday == 5:  # Saturday
        return WEEKEND
    if weekday == 4 and h >= 21:  # Friday after 21:00
        return WEEKEND
    if weekday == 6 and h < 20:   # Sunday before 20:00
        return WEEKEND

    # London-NY overlap (highest liquidity).
    if 12 <= h < 16:
        return LONDON_NY
    # Tokyo-London overlap (brief, 07-08 UTC).
    if h == 7:
        return TOKYO_LONDON
    # Pure London (08-12 UTC).
    if 8 <= h < 12:
        return LONDON
    # Pure NY (16-21 UTC).
    if 16 <= h < 21:
        return NY
    # Sydney (20-23 UTC).
    if 20 <= h < 23:
        return SYDNEY
    # Tokyo (23-07 UTC, with rollover).
    if h >= 23 or h < 7:
        return TOKYO
    return TOKYO  # fallback


def is_news_blackout_window(
    ts: datetime,
    upcoming_high_impact_events: list[datetime],
    minutes_before: int = 5,
    minutes_after: int = 5,
) -> bool:
    """Return True if ``ts`` falls within ±N minutes of any 3-star event.

    Used to block new FX entries around economic releases (NFP, CPI,
    rate decisions). The hardcoded 5/5 default is conservative — strategy
    code can override per-strategy.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    for event_ts in upcoming_high_impact_events:
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=timezone.utc)
        delta_min = (ts - event_ts).total_seconds() / 60.0
        if -minutes_before <= delta_min <= minutes_after:
            return True
    return False


def session_pair_active(session: str, pair: str) -> bool:
    """Filter pairs that should NOT be traded outside their natural session.

    Some pairs are illiquid outside their home regions:
      - AUD_*, NZD_*  : avoid in london (low volume)
      - EUR_*, GBP_*  : avoid in tokyo
      - USD_JPY       : OK in tokyo, london, ny
    """
    pair_upper = pair.upper().replace("/", "_")
    base, quote = (pair_upper.split("_") + [""])[:2]

    if session == TOKYO:
        # Tokyo session: AUD/NZD/JPY pairs are fine; EUR/GBP majors are thin
        if base in ("EUR", "GBP") and quote not in ("JPY", "AUD", "NZD"):
            return False
    if session == LONDON:
        # London: AUD/NZD pairs are illiquid
        if base in ("AUD", "NZD") and quote == "USD":
            return False
    return True
