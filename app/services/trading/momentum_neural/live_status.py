"""Operator-facing status derivation for the live observer surface.

The live observer used to ship a single ``latest_runtime_utc`` and a raw
``captured_watch_status`` string buried in a diagnostics blob.  Both are loop
*heartbeat* facts.  Neither says whether the lane is actually seeing the tape, and
``_build_state_snapshot`` falls back to the runner control heartbeat precisely when
the captured identity is broken -- so the one health signal on the page went green
at the exact moment the lane went blind.

This module turns the raw signals into an operator verdict with two rules that are
never violated:

1. The control-loop age and the tape age are *separate* numbers, always reported
   side by side, and never collapsed into one "freshness".
2. The status vocabulary is OPEN, not a closed enum.  ``_captured_runtime_identity``
   passes arbitrary reason strings through from ``lane_health``, so anything this
   module does not recognise resolves to ``unknown``/``warn`` -- never to ``ok``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .market_calendar import is_us_market_holiday
from .market_profile import _premarket_start_min, market_session_now

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Tape is considered dead when the market is open and no quote row has landed for
# this long.  Deliberately generous: a thin premarket symbol can be quiet, but two
# minutes of nothing across the WHOLE tape while the market is open is a fault.
TAPE_BLIND_SECONDS = 120.0

# Statuses that mean "the captured PAPER lane cannot be trusted to be watching".
# Anything captured_watch_* that is not in HEALTHY_STATUSES is treated as blind even
# if it is not listed here -- see ``classify_watch_status``.
HEALTHY_STATUSES = frozenset({"ok"})
DEGRADED_STATUSES = frozenset({"runtime_stale", "live_runner_loop_heartbeat_stale"})

# One sentence per known status, written for the operator at 04:00 with no context.
# A status missing from this map still renders (see ``describe_watch_status``); the
# map exists to say something *actionable*, not to gate the branch.
STATUS_DETAIL: dict[str, str] = {
    "captured_watch_heartbeat_missing": (
        "No captured-PAPER heartbeat row exists today. The live runner loop is not "
        "writing. Check the runner task and the IQFeed bridge."
    ),
    "captured_watch_heartbeat_not_today": (
        "The newest heartbeat is from a previous ET day. The runner has not started "
        "this session."
    ),
    "captured_watch_heartbeat_hash_mismatch": (
        "The heartbeat's content hash does not match its body. A stale or forged "
        "generation is being written - check for two runner instances."
    ),
    "captured_watch_heartbeat_future": (
        "The heartbeat is timestamped in the future. Clock skew between the runner "
        "host and the database."
    ),
    "captured_watch_heartbeat_clock_invalid": (
        "The heartbeat failed clock validation. It is present but cannot be trusted "
        "to name the running generation."
    ),
    "captured_watch_heartbeat_identity_invalid": (
        "The heartbeat failed identity validation. It is present but cannot be "
        "trusted to name the running generation."
    ),
    "captured_watch_heartbeat_invalid": (
        "The heartbeat failed validation. It is present but cannot be trusted to "
        "name the running generation."
    ),
    "captured_watch_heartbeat_unreadable": (
        "The captured-PAPER heartbeat row could not be read. Database or "
        "serialisation fault."
    ),
    "captured_watch_generation_ambiguous": (
        "Two active PAPER activation generations are bound. The watch list cannot "
        "resolve. Stop the older runner."
    ),
    "captured_watch_generation_missing": (
        "No active PAPER activation generation is bound. Selection has nothing to "
        "attach to."
    ),
    "captured_watch_generation_invalid": (
        "The bound PAPER activation generation is malformed. Selection cannot attach "
        "to it."
    ),
    "captured_watch_generation_unreadable": (
        "The PAPER activation generation could not be read. Database fault."
    ),
    "captured_watch_inventory_unreadable": (
        "The heartbeat is valid but the watch-route read failed. Database or query "
        "fault - the selection list is unavailable."
    ),
    "captured_watch_user_scope_mismatch": (
        "Observer scope is misconfigured - this account is not the one the runner "
        "writes for. Check chili_autotrader_user_id."
    ),
    "captured_watch_user_scope_unconfigured": (
        "Observer user scope is not configured. Set chili_autotrader_user_id."
    ),
    "captured_watch_account_unconfigured": (
        "Expected broker account is not configured. Set "
        "chili_alpaca_expected_account_id."
    ),
    "runtime_stale": (
        "The control loop is beating but behind schedule. Positions are still "
        "managed; selection is lagging."
    ),
}


def classify_watch_status(status: str | None) -> str:
    """Map a raw ``captured_watch_status`` onto ``ok`` | ``degraded`` | ``blind``.

    Open vocabulary: ``_captured_runtime_identity`` (live_monitor.py) returns
    ``str(reason)`` for arbitrary upstream reasons, and ``lane_health`` has its own
    vocabulary (``captured_paper_heartbeat_account_mismatch``, ...).  An unrecognised
    status must NEVER read as healthy, so the default is ``blind`` for anything that
    looks like a lane status and ``unknown`` for anything else.
    """
    text = str(status or "").strip().lower()
    if not text:
        return "unknown"
    if text in HEALTHY_STATUSES:
        return "ok"
    if text in DEGRADED_STATUSES:
        return "degraded"
    if text.startswith(("captured_watch_", "captured_paper_", "live_runner_loop_")):
        # A novel failure in a known family is a failure, not an all-clear.
        return "blind"
    return "unknown"


def describe_watch_status(status: str | None) -> str:
    text = str(status or "").strip()
    known = STATUS_DETAIL.get(text.lower())
    if known:
        return known
    return (
        f"Lane health reported an unrecognized status: {text or 'none'}. "
        "Treat the lane as blind until confirmed."
    )


def _holiday_name(day: date) -> str | None:
    """Best-effort label for a market holiday.

    ``market_calendar`` stores bare dates, not names, so this derives the common
    name from the month/day.  It is cosmetic; the phase itself never depends on it.
    """
    table = {
        (1, 1): "New Year's Day",
        (6, 19): "Juneteenth",
        (7, 4): "Independence Day",
        (12, 25): "Christmas Day",
    }
    fixed = table.get((day.month, day.day))
    if fixed:
        return fixed
    # Fixed-date holidays land on the adjacent weekday when they fall on a weekend
    # (2026-07-04 is a Saturday, so the market closes Friday 2026-07-03).
    if day.weekday() == 4:  # Friday closes for a Saturday holiday
        nxt = table.get(((day + timedelta(days=1)).month, (day + timedelta(days=1)).day))
        if nxt:
            return f"{nxt} (observed)"
    if day.weekday() == 0:  # Monday closes for a Sunday holiday
        prv = table.get(((day - timedelta(days=1)).month, (day - timedelta(days=1)).day))
        if prv:
            return f"{prv} (observed)"
    if day.month == 1 and day.weekday() == 0 and 15 <= day.day <= 21:
        return "Martin Luther King Jr. Day"
    if day.month == 2 and day.weekday() == 0 and 15 <= day.day <= 21:
        return "Presidents' Day"
    if day.month == 3 or day.month == 4:
        if day.weekday() == 4:
            return "Good Friday"
    if day.month == 5 and day.weekday() == 0 and day.day >= 25:
        return "Memorial Day"
    if day.month == 9 and day.weekday() == 0 and day.day <= 7:
        return "Labor Day"
    if day.month == 11 and day.weekday() == 3 and 22 <= day.day <= 28:
        return "Thanksgiving"
    return None


def _next_session_open_et(now_et: datetime, open_minute: int) -> datetime:
    """First ET datetime at ``open_minute`` on a future trading day."""
    candidate = now_et.replace(
        hour=open_minute // 60, minute=open_minute % 60, second=0, microsecond=0
    )
    if candidate <= now_et:
        candidate = candidate + timedelta(days=1)
    for _ in range(10):
        if candidate.weekday() < 5 and not is_us_market_holiday(candidate.date()):
            return candidate
        candidate = candidate + timedelta(days=1)
    return candidate


def _human_delta(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    # Keep raw seconds well past a minute: these ages are compared against a 75s
    # threshold on screen, and rounding 94s to "1m" reads as if it were UNDER it.
    if seconds < 120:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        rem = int(seconds % 60)
        return f"{minutes}m {rem}s" if rem else f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rem = minutes % 60
        return f"{hours}h {rem}m" if rem else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def market_session_snapshot(now_utc: datetime) -> dict[str, Any]:
    """Expand ``market_session_now``'s four-value answer into an operator phase.

    ``market_session_now`` collapses weekend, holiday and overnight into a single
    ``closed``, and returns ``regular`` unconditionally for crypto -- so it is called
    here with ``None``, which ``asset_class_for_symbol`` resolves to ``stock``.
    """
    aware = (
        now_utc.replace(tzinfo=timezone.utc)
        if now_utc.tzinfo is None
        else now_utc.astimezone(timezone.utc)
    )
    now_et = aware.astimezone(_ET)
    # None -> asset_class_for_symbol("") -> "stock". Never pass a crypto symbol here:
    # market_session_now short-circuits crypto to "regular" 24/7.
    raw = market_session_now(None, now=aware)

    holiday_name: str | None = None
    if raw == "closed":
        if now_et.weekday() >= 5:
            phase = "closed_weekend"
            label = "Weekend - market closed"
        elif is_us_market_holiday(now_et.date()):
            phase = "closed_holiday"
            holiday_name = _holiday_name(now_et.date())
            label = f"Market closed - {holiday_name}" if holiday_name else "Market holiday"
        else:
            phase = "closed_overnight"
            label = "Overnight - market closed"
    else:
        phase = raw
        label = {
            "premarket": "Premarket",
            "regular": "Regular hours",
            "afterhours": "After hours",
        }.get(raw, raw)

    open_minute = min(_premarket_start_min(), 9 * 60 + 30)
    next_open_et = _next_session_open_et(now_et, open_minute)
    next_open_utc = next_open_et.astimezone(timezone.utc)
    weekday = next_open_et.strftime("%A")
    clock = next_open_et.strftime("%H:%M")
    until = (next_open_utc - aware).total_seconds()

    return {
        "phase": phase,
        "label": label,
        "holiday_name": holiday_name,
        "is_open": phase in ("premarket", "regular", "afterhours"),
        "et_now": now_et.isoformat(),
        "et_clock": now_et.strftime("%H:%M:%S"),
        "next_open_utc": next_open_utc.replace(tzinfo=None).isoformat() + "Z",
        "next_open_label": f"{weekday} {clock} ET",
        "next_open_in": _human_delta(until),
    }


def resolve_lane_verdict(
    *,
    watch_status: str | None,
    market_open: bool,
    market_label: str,
    control_age_s: float | None,
    tape_age_s: float | None,
    stale_after_s: float,
    lane_health: dict[str, Any] | None,
    symbol_count: int,
) -> dict[str, Any]:
    """Single source of truth for what the page says at the top.

    Resolution order is deliberate and total -- every path assigns a verdict, and the
    fallthrough is ``unknown``/``warn``, never ``ok``.
    """
    health = lane_health or {}
    enabled = bool(health.get("enabled"))
    frozen = bool(health.get("frozen"))

    # 1. Halted beats everything: kill switch / drawdown breaker need a manual reset.
    if enabled and frozen:
        return {
            "verdict": "halted",
            "severity": "critical",
            "headline": "Chili is halted. Manual reset required.",
            "detail": " ".join(
                part
                for part in (health.get("headline"), health.get("detail"))
                if part
            )
            or "A lane-health condition is holding trading closed.",
        }

    bucket = classify_watch_status(watch_status)

    # 2. Blind: the watch inventory cannot be trusted at all.
    if bucket == "blind":
        return {
            "verdict": "blind",
            "severity": "critical",
            "headline": "Chili is not seeing the market.",
            "detail": describe_watch_status(watch_status),
        }

    # 3. Unrecognised status: warn, never all-clear.
    if bucket == "unknown":
        return {
            "verdict": "unknown",
            "severity": "warn",
            "headline": "Chili's state cannot be confirmed.",
            "detail": describe_watch_status(watch_status),
        }

    # 4. Loop late.
    if bucket == "degraded":
        age = _human_delta(control_age_s) if control_age_s is not None else "unknown"
        return {
            "verdict": "degraded",
            "severity": "warn",
            "headline": "Chili's loop is late.",
            "detail": (
                f"Last loop tick {age} ago; the threshold is {int(stale_after_s)}s. "
                "Positions are still managed but selection is behind."
            ),
        }

    # 5. Awake but blind to the tape -- the failure that looks healthy.
    if market_open and (tape_age_s is None or tape_age_s > TAPE_BLIND_SECONDS):
        seen = "no tape row has landed today" if tape_age_s is None else (
            f"no tape row for {_human_delta(tape_age_s)}"
        )
        return {
            "verdict": "tape_blind",
            "severity": "warn",
            "headline": "Chili is awake but blind to the tape.",
            "detail": (
                f"The control loop is beating normally, but {seen}. "
                "This is the failure that looks healthy."
            ),
        }

    # 6. Correctly idle.
    if not market_open:
        return {
            "verdict": "idle_closed",
            "severity": "info",
            "headline": f"Chili is idle. {market_label}.",
            "detail": "Nothing is expected to happen until the next session opens.",
        }

    # 7. Healthy and open.
    if symbol_count:
        return {
            "verdict": "ok",
            "severity": "ok",
            "headline": f"Chili is trading. {symbol_count} symbol"
            + ("s" if symbol_count != 1 else "")
            + " in view.",
            "detail": "",
        }
    return {
        "verdict": "ok",
        "severity": "ok",
        "headline": "Chili is watching. Nothing has qualified yet.",
        "detail": "",
    }
