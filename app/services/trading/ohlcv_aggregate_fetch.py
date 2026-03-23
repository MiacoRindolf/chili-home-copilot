"""Helpers for Polygon/Massive aggregate OHLCV range requests.

Wide ``from``/``to`` windows for **minute** or **hour** bars are split into
smaller calendar chunks. Some tiers or edge cases return only a partial span
from a single REST call even when ``limit=50000`` would allow more bars —
chunking matches how Polygon/Massive clients recommend pulling long intraday
history.
"""
from __future__ import annotations

from datetime import date, timedelta


# Calendar days per request for intraday (minute/hour) aggregates.
_INTRADAY_CHUNK_DAYS = 21


def iter_intraday_date_chunks(from_str: str, to_str: str) -> list[tuple[str, str]]:
    """Inclusive YYYY-MM-DD chunk boundaries covering [from_str, to_str]."""
    fd = date.fromisoformat(from_str)
    td = date.fromisoformat(to_str)
    out: list[tuple[str, str]] = []
    cur = fd
    while cur <= td:
        end = min(cur + timedelta(days=_INTRADAY_CHUNK_DAYS - 1), td)
        out.append((cur.isoformat(), end.isoformat()))
        cur = end + timedelta(days=1)
    return out
