"""Freshness helpers for OHLCV-backed trading context panels."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _coerce_bar_time_utc(raw: Any) -> datetime | None:
    """Return a comparable UTC-naive datetime for a dataframe index value."""
    if raw is None:
        return None

    to_pydatetime = getattr(raw, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            raw = to_pydatetime()
        except Exception:
            return None

    if isinstance(raw, datetime):
        return _to_utc_naive(raw)
    if isinstance(raw, date):
        return datetime.combine(raw, time.min)

    if isinstance(raw, str):
        txt = raw.strip()
        if not txt:
            return None
        try:
            parsed = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _to_utc_naive(parsed)

    return None


def latest_ohlcv_bar_time_utc(df: Any) -> datetime | None:
    """Extract the last bar timestamp from a pandas-like OHLCV dataframe."""
    index = getattr(df, "index", None)
    if index is None:
        return None
    try:
        if len(index) <= 0:
            return None
        raw_latest = index[-1]
    except Exception:
        return None
    return _coerce_bar_time_utc(raw_latest)


def daily_ohlcv_staleness_reason(
    df: Any,
    *,
    max_age_days: int,
    now: datetime | None = None,
) -> str | None:
    """Return a reason when a daily OHLCV frame cannot prove freshness."""
    latest = latest_ohlcv_bar_time_utc(df)
    if latest is None:
        return "missing_latest_bar_time"

    try:
        max_age = max(0, int(max_age_days))
    except (TypeError, ValueError):
        max_age = 0

    observed_now = _to_utc_naive(now or datetime.now(timezone.utc))
    age_days = (observed_now.date() - latest.date()).days
    if age_days < -1:
        return f"latest_bar_in_future:latest={latest.isoformat()}"
    if age_days > max_age:
        return (
            f"stale_bar_age_days={age_days}:max_age_days={max_age}:"
            f"latest={latest.isoformat()}"
        )
    return None
