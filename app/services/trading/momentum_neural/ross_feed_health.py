from __future__ import annotations

import logging
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)

HOT_START_ET = time(4, 0)
HOT_END_ET = time(11, 0)

_last_health_signature: str | None = None
_last_health_emit_monotonic: float | None = None


@dataclass(frozen=True)
class FeedHealth:
    ok: bool
    severity: str
    reason: str
    details: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    raw = date(year, month, day)
    if raw.weekday() == 5:
        return raw - timedelta(days=1)
    if raw.weekday() == 6:
        return raw + timedelta(days=1)
    return raw


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    cur = date(year, month, 1)
    while cur.weekday() != weekday:
        cur += timedelta(days=1)
    return cur + timedelta(days=7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    cur = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while cur.weekday() != weekday:
        cur -= timedelta(days=1)
    return cur


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm. Needed because NYSE observes Good Friday.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nyse_closed_date(day: date) -> bool:
    """NYSE full-day holiday rules for the live Ross equity clock.

    The feed gate is only hard during tradable equity sessions. A simple weekday
    check falsely treats observed holidays (for example 2026-07-03) as hot, which
    turns correct stale-tape silence into an erroneous live-readiness failure.
    """
    y = day.year
    holidays = {
        _observed_fixed_holiday(y, 1, 1),
        _nth_weekday(y, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(y, 2, 0, 3),  # Washington's Birthday / Presidents Day
        _easter_sunday(y) - timedelta(days=2),  # Good Friday
        _last_weekday(y, 5, 0),  # Memorial Day
        _observed_fixed_holiday(y, 6, 19),  # Juneteenth
        _observed_fixed_holiday(y, 7, 4),  # Independence Day
        _nth_weekday(y, 9, 0, 1),  # Labor Day
        _nth_weekday(y, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(y, 12, 25),
    }
    return day in holidays


def latest_source_rows(db, *, source: str) -> dict[str, Any]:
    row = db.execute(
        sa.text(
            """
            SELECT observed_at AS latest
            FROM momentum_nbbo_spread_tape
            WHERE source = :source
              AND observed_at > (now() at time zone 'utc') - interval '2 days'
            ORDER BY observed_at DESC
            LIMIT 1
            """
        ),
        {"source": source},
    ).mappings().first()
    latest = row.get("latest") if row is not None else None
    age_s = None
    if latest is not None:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_s = max(0.0, (_utcnow() - latest.astimezone(timezone.utc)).total_seconds())
    return {"latest": latest, "age_s": age_s, "present": latest is not None}


def fresh_live_rows(db) -> int:
    return int(
        db.execute(
            sa.text(
                """
                SELECT count(*)
                FROM momentum_symbol_viability
                WHERE live_eligible
                  AND updated_at > (now() at time zone 'utc') - interval '30 minutes'
                """
            )
        ).scalar()
        or 0
    )


def market_clock(now_utc: datetime | None = None) -> dict[str, Any]:
    try:
        from zoneinfo import ZoneInfo

        et = (now_utc or _utcnow()).astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et = now_utc or _utcnow()
    weekday = et.weekday()
    closed_full_day = weekday >= 5 or _nyse_closed_date(et.date())
    in_hot_window = not closed_full_day and HOT_START_ET <= et.time() <= HOT_END_ET
    return {
        "et": et.isoformat(),
        "weekday": weekday,
        "equity_market_closed_full_day": closed_full_day,
        "in_hot_window": in_hot_window,
    }


def evaluate_feed_health(
    *,
    iqfeed: dict[str, Any],
    massive: dict[str, Any],
    fresh_live_rows: int,
    clock: dict[str, Any],
    max_iqfeed_age_hot_s: float = 60.0,
) -> FeedHealth:
    details = {
        "iqfeed": iqfeed,
        "massive": massive,
        "fresh_live_eligible_30m": fresh_live_rows,
        "clock": clock,
        "max_iqfeed_age_hot_s": max_iqfeed_age_hot_s,
    }
    in_hot = bool(clock.get("in_hot_window"))
    massive_age = massive.get("age_s")
    if massive_age is None or float(massive_age) > 180.0:
        if in_hot:
            return FeedHealth(False, "error", "massive_snapshot_tape_stale", details)
        return FeedHealth(True, "warn", "massive_snapshot_tape_stale_outside_hot_window", details)

    iq_age = iqfeed.get("age_s")
    has_fresh_live = int(fresh_live_rows or 0) > 0
    if in_hot and has_fresh_live and (iq_age is None or float(iq_age) > float(max_iqfeed_age_hot_s)):
        return FeedHealth(False, "error", "iqfeed_l1_stale_during_hot_live_window", details)
    if in_hot and iq_age is None:
        return FeedHealth(False, "warn", "iqfeed_l1_missing_during_hot_window_no_live_candidates", details)
    if iq_age is None:
        return FeedHealth(True, "warn", "iqfeed_l1_missing_outside_hot_window", details)
    if not in_hot and float(iq_age) > 3600.0:
        return FeedHealth(True, "warn", "iqfeed_l1_stale_outside_hot_window", details)
    return FeedHealth(True, "ok", "ross_lane_feed_runtime_ok", details)


def check_feed_health(db, *, max_iqfeed_age_hot_s: float = 60.0) -> FeedHealth:
    return evaluate_feed_health(
        iqfeed=latest_source_rows(db, source="iqfeed_l1"),
        massive=latest_source_rows(db, source="massive_snapshot"),
        fresh_live_rows=fresh_live_rows(db),
        clock=market_clock(),
        max_iqfeed_age_hot_s=max_iqfeed_age_hot_s,
    )


def run_feed_health_check(
    db,
    *,
    max_iqfeed_age_hot_s: float = 60.0,
    emit_cooldown_s: float = 300.0,
) -> FeedHealth:
    global _last_health_signature, _last_health_emit_monotonic

    health = check_feed_health(db, max_iqfeed_age_hot_s=max_iqfeed_age_hot_s)
    if health.severity == "ok":
        _last_health_signature = None
        _last_health_emit_monotonic = None
        return health

    signature = f"{health.severity}:{health.reason}"
    now_mono = time_module.monotonic()
    should_emit = (
        signature != _last_health_signature
        or _last_health_emit_monotonic is None
        or (now_mono - _last_health_emit_monotonic) >= max(30.0, float(emit_cooldown_s or 0.0))
    )
    if not should_emit:
        return health

    _last_health_signature = signature
    _last_health_emit_monotonic = now_mono
    log = logger.error if not health.ok else logger.warning
    log("[ross_feed_health] %s details=%s", health.reason, health.details)
    return health
