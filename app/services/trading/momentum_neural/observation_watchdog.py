"""Deadman for the thing the operator actually depends on: LANE OBSERVATION.

WHY THIS EXISTS (measured, not assumed)
---------------------------------------
On 2026-09-01 the lane stopped observing at 08:03:17 PT and never resumed. The
last three lines it ever wrote that day were

    08:03:11 WARNING auto_arm: reaped stale pre-entry session=19430 SSM
             state=watching_live (watched > 300s, never entered)
    08:03:16 INFO ignition_loop: symbol=RDAC move_pct=67.55 scored_ok=True
    08:03:17 INFO ignition_loop: symbol=ARCT move_pct=11.13 scored_ok=True

and the next line in the 573 MB log file is the 2026-09-02 startup. 296 of that
day's 390 RTH minutes (75.9%) produced no observation. Across the 11 sessions
2026-08-19..2026-09-02, 886 of 4,290 RTH minutes were dark (20.7%), including 9
blackouts of >= 15 contiguous minutes in 6 different sessions -- three of them
straight through the opening bell (08-20 42 min, 08-21 74 min, 08-27 109 min).
24 independently-confirmed market movers made their SESSION HIGH inside a
blackout; 8 of those were never observed by the lane at any point that day.
Nothing alarmed, on any of them.

THREE FAILURES IN SERIES, and why none of the existing detectors could fire
--------------------------------------------------------------------------
1. THE DEATH. The lane app is a HOST process (timeshare_supervisor.py:608-617
   Popen's uvicorn into a Job Object with KILL_ON_JOB_CLOSE), which is why
   `docker logs | grep <job>` returned zero. It dies abruptly and often: 26 of
   41 supervisor receipts carry shutdown_reason='app_died', spread over 08-26
   (x1), 08-27 (x2), 08-28 (x6), 08-29 (x2), 08-31 (x7), 09-01 (x4), 09-02 (x3).
   No traceback, no graceful-shutdown line (101 "Started server process" lines
   in the whole log; ZERO shutdown lines, ever), no Windows Error Reporting
   event. The underlying cause is a SEPARATE defect and is not addressed here;
   detection comes first, because right now the death is invisible.
2. NO RESTART. timeshare_supervisor.py:628-639 has three exits and none is
   "restart"; on app_died it flattens, writes a PREPARED receipt (stamped
   15:03:36Z -- 16 seconds after the death, so detection was never the problem)
   and returns. Its caller pipes it to Out-File and never reads $LASTEXITCODE,
   so Task Scheduler logged `Last Result: 0` for the day the lane went dark for
   five hours. That launcher is not in this repository.
3. NOTHING PAGED. `run_lane_health_check` is IN-PROCESS -- it ran fine at
   08:03:12 ("phase=ok duration_ms=265") and died 5 seconds later; a dead
   process cannot report its own death, and its output goes to a pull surface
   anyway (sent_via='cockpit'). `\\CHILI-live-runtime-watchdog` has been Disabled
   since 2026-07-27 01:04:50, its target script no longer exists on disk, and
   its own comment scopes it to Docker compose services. `\\CHILI-liveness-
   watchdog` was disabled in the same four minutes. `\\CHILI-Infra-Guard` runs
   hourly but only measures C: free space, TCP connection count and Docker
   backend uptime. And `control_loop_watchdog`, the one alarm designed to reach
   a phone, is SATURATED: its heartbeat key's newest row is 2026-08-17
   12:26:10.871356 UTC and no writer for it exists anywhere in the tree any
   more, so it reports the same 16-day-old death forever and its change-only
   cooldown collapsed that to two messages in fifteen days. It produced nothing
   on 2026-09-01 because, from its point of view, nothing changed that day.

WHAT THIS MODULE DOES DIFFERENTLY
---------------------------------
*It watches OBSERVATIONS, not process liveness.* A port check would have missed
08-21 and 08-27, where the app was up and simply not observing across the open.

*It runs OUT OF PROCESS.* Registered on the scheduler like control_loop_watchdog,
reading a row the lane app commits every 30s. The record has to outlive the
process that writes it.

*Detection IS session-gated, unlike control_loop_watchdog -- deliberately.* That
module is right to be clock-free: the control loop is a 24/7 exit owner. This
one is not. The lane legitimately observes nothing at 02:00 ET, so a clock-free
silence alarm here would page every single night. The gate is an explicit
US/Eastern predicate rather than `is_tradeable_now`, which lane_health's own
comment records as resolving FALSE at 20:44 ET on this box.

*The threshold derives from the lane's own cadence*, not a magic number: the
heartbeat is written every 30s, and silence is declared after
`SILENT_AFTER_BEATS` missed beats.

*"Has not started yet" is not "has stopped."* The first cut of this module got
this wrong and would have paged on every trading morning: the session gate opens
at 04:00 ET, the lane has never once started before it (measured first
observation, ET: 04:01-04:05 on six days, 05:20, 06:45, 07:06, 11:19, 13:23 on
the rest), and the previous session's last beat is 12-16 hours old -- so at the
first tick past 04:00 ET the state was `silent`, `in_session` was True, and the
signature was NEW, which is one false phone page per day against one genuine
one. `silent` now requires a beat on the CURRENT ET trading date. Without one
the state is `not_started`, which pages on its own rule -- from RTH open, when
"it starts late" has stopped being a plausible explanation.

*`stuck_configuration` is deliberately NOT session-gated.* It is a claim about
the deployment ("nothing has written this key in days"), not about today's
market, and its signature is a frozen last-beat timestamp so it pages exactly
once for the whole fault. Making it wait for a session before reporting that the
watchdog itself is blind would reproduce control_loop_watchdog's failure.

*Evidence gate anchored on the last beat, never on the clock* -- the lesson
control_loop_watchdog paid for twice. "Did a repeating writer ever exist on this
box?" is age-independent, so a box that never hosts the lane never pages while a
box that HAS hosted it stays armed with no upper bound. And a writer that has
been gone for WEEKS is classified as `stuck_configuration`, not as a fresh
death: that is exactly the state control_loop_watchdog has been stuck in since
2026-08-17, quietly re-asserting a stale corpse twice a fortnight.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..alerts import LANE_OBSERVATION_SILENT, dispatch_alert
from ..batch_job_constants import JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# The lane writes one heartbeat per 30s (ignition_loop._OBSERVATION_HEARTBEAT_S).
# Six missed beats = 3 minutes, which is far inside the shortest blackout worth
# alarming on (the census counts only >= 15 contiguous RTH minutes) and far
# outside a Postgres blip on a box with a documented idle-in-transaction cascade.
HEARTBEAT_INTERVAL_SECONDS = 30.0
SILENT_AFTER_BEATS = 6
SILENT_AFTER_SECONDS = HEARTBEAT_INTERVAL_SECONDS * SILENT_AFTER_BEATS

# Same evidence contract as control_loop_watchdog: two beats in the hour ENDING
# at the newest beat prove a repeating writer. Anchored on the beat, never on
# `now`, so an arbitrarily old outage stays reportable.
EVIDENCE_WINDOW_SECONDS = 3600.0
EVIDENCE_MIN_BEATS = 2

# Past this, the writer is GONE, not late -- a deploy that dropped it, a renamed
# job type, a lane that moved. That is a configuration fault and must not be
# reported as "the lane just died", which is the failure mode that made
# control_loop_watchdog useless: its key has been dead since 2026-08-17 and it
# has spent 16 days asserting a fresh death.
STUCK_CONFIGURATION_AFTER_SECONDS = 3 * 24 * 3600.0

# UP BUT NOT OBSERVING. Process death is only one of the two shapes. On
# 2026-08-21 (74 min) and 2026-08-27 (109 min) the app was up and simply
# produced no observations straight through the opening bell — a port check or a
# liveness probe would have called both of those healthy. 900s because that is
# the same >= 15 contiguous RTH minutes the blackout census uses; anything
# shorter is log jitter. Requires the heartbeat to have been beating throughout,
# so this state is unambiguously "alive and blind" rather than "dead".
NOT_OBSERVING_WINDOW_SECONDS = 900.0
NOT_OBSERVING_MIN_BEATS = int(NOT_OBSERVING_WINDOW_SECONDS / 60.0)  # half the
# nominal rate, so one slow refresh cycle cannot manufacture the verdict.

# Extended session, ET. Premarket from 04:00 (the lane's own premarket window;
# the 09-01 death at 11:03 ET and the 08-21/08-27 blackouts through the open are
# all inside this) to the 16:00 close.
SESSION_OPEN_ET = dtime(4, 0)
SESSION_CLOSE_ET = dtime(16, 0)

_LAST_BEAT_SQL = text(
    """
    SELECT max(started_at) AS last_beat_at
      FROM brain_batch_jobs
     WHERE job_type = :job_type
    """
)

_BEATS_AROUND_SQL = text(
    """
    SELECT count(*) AS beats
      FROM brain_batch_jobs
     WHERE job_type = :job_type
       AND started_at >= :window_start
    """
)

# Bounded by the PK for the same reason control_loop_watchdog is: `trading_alerts`
# has no index on alert_type or content_signature, so the natural two-column
# lookup is a seq scan over ~94k rows. `success = true` is load-bearing --
# dispatch_alert writes the row whether or not delivery worked, and ~30% of
# alerts in one measured 40h window on this box were dropped.
_ALREADY_PAGED_SQL = text(
    """
    SELECT 1
      FROM trading_alerts
     WHERE id > (SELECT max(id) - :window FROM trading_alerts)
       AND alert_type = :alert_type
       AND content_signature = :signature
       AND success = true
     LIMIT 1
    """
)
_ALREADY_PAGED_WINDOW = 4000

# Observations summed over the recent window. `meta_json` is JSONB, so the cast
# is index-free, but the row count is tiny (2/min, bounded by the window).
# `->>' ' IS NOT NULL` rather than the JSONB `?` containment operator: `?` is a
# paramstyle marker in several DBAPI drivers and is a trap inside `text()`.
_RECENT_OBSERVATIONS_SQL = text(
    """
    SELECT count(*) AS beats,
           coalesce(sum((meta_json->>'observations')::bigint), 0) AS observations
      FROM brain_batch_jobs
     WHERE job_type = :job_type
       AND started_at >= :window_start
       AND meta_json->>'observations' IS NOT NULL
    """
)

# RTH only for the not-observing verdict. A genuinely quiet 04:05 ET premarket
# can legitimately produce zero observations; 10:00 ET cannot.
RTH_OPEN_ET = dtime(9, 30)
RTH_CLOSE_ET = dtime(16, 0)

# A LANE THAT HAS NOT STARTED YET TODAY DID NOT STOP.
#
# The first cut of this module declared `silent` on two facts only — a last beat
# older than SILENT_AFTER_SECONDS, and `in_market_session`. That pages on EVERY
# trading morning, because the session gate opens at 04:00 ET and the lane has
# never once started before it. From the measured record (coverage_by_day.csv,
# first ignition observation converted to ET):
#   08-18 13:23 | 08-19 04:02 | 08-20 04:01 | 08-21 04:01 | 08-24 04:02
#   08-25 04:05 | 08-26 06:45 | 08-27 11:19 | 08-28 05:20 | 08-31 07:06
#   09-01 04:38 | 09-02 04:39
# All twelve are after 04:00 ET; three are hours after. Meanwhile the previous
# session's last beat is 12-16 hours old, so at the first tick past 04:00 ET the
# state was `silent`, `in_session` was True, and the signature
# `silent:<yesterday's beat>` was NEW — so neither the in-process latch nor the
# cross-process dedupe suppressed it. One genuine page (2026-09-01) against one
# false page every trading day, on the ONE alert type routed to a real phone
# notification. That is the alarm fatigue this module was written to end.
#
# THE GATE: `silent` requires a beat on the CURRENT ET trading date — evidence
# that the lane was up today and then stopped. When the newest beat predates
# today, the state is `not_started`, which is a DIFFERENT fact and pages on a
# different rule (below). It is deliberately a distinct state and not a
# suppression: "the lane never came up today" must still be reportable.
#
# WHEN `not_started` PAGES: only from RTH open. Before 09:30 ET a lane that has
# not started is indistinguishable from a lane that starts late, and it started
# after 06:00 ET on 4 of the 12 days above. By 09:30 ET the benign explanation is
# gone — and the two days it fires on in the record (08-27, first observation
# 11:19 ET; 08-18, 13:23 ET) are exactly the sessions the blackout census counts
# as dark through the open. Those pages are correct, not false.
NOT_STARTED_PAGES_FROM_ET = RTH_OPEN_ET

_last_signature: str | None = None


def _utc_naive(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def in_market_session(now_utc: datetime | None = None) -> bool:
    """True inside the extended US equity session (Mon-Fri, 04:00-16:00 ET).

    Explicit rather than `is_tradeable_now`: lane_health's own comment records
    that helper resolving FALSE at 20:44 ET on this box, and a session predicate
    that is wrong in the quiet direction silences the alarm.
    """
    now = now_utc or datetime.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(_ET)
    if et.weekday() >= 5:
        return False
    return SESSION_OPEN_ET <= et.time() < SESSION_CLOSE_ET


def _et_date(value: datetime) -> str:
    """ET calendar date of a naive-UTC or aware datetime, as ``YYYY-MM-DD``."""
    dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET).date().isoformat()


def in_regular_hours(now_utc: datetime | None = None) -> bool:
    """True inside RTH (Mon-Fri, 09:30-16:00 ET) — the window in which zero
    observations is not a defensible state for a live lane."""
    now = now_utc or datetime.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(_ET)
    if et.weekday() >= 5:
        return False
    return RTH_OPEN_ET <= et.time() < RTH_CLOSE_ET


def evaluate_lane_observation(
    db: Session, *, now: datetime | None = None
) -> dict[str, Any]:
    """Read-only verdict on lane observation. Never raises, never writes.

    ``state`` is one of:
      ``absent``               — no repeating writer on this box; silence is
                                 correct and permanent (fresh DB, CI, dev box).
      ``observing``            — a beat within SILENT_AFTER_SECONDS, producing
                                 observations (or outside RTH, where zero is
                                 legitimate).
      ``silent``               — a repeating writer existed, beat TODAY, and has
                                 stopped. This is the 2026-09-01 11:03 ET shape.
      ``not_started``          — a repeating writer exists on this box but has
                                 not beaten at all on the current ET trading
                                 date. A lane that has not started yet did not
                                 stop, so this must not read as a death; it
                                 pages on its own rule, from RTH open.
      ``not_observing``        — the process is ALIVE and beating but has
                                 produced zero observations for the whole
                                 not-observing window during RTH. This is the
                                 2026-08-21 / 2026-08-27 shape, which a port
                                 check or liveness probe calls healthy.
      ``stuck_configuration``  — the writer has been gone for days. A deploy
                                 problem, not a fresh death.
      ``unknown``              — the read failed.
    """
    now = now or datetime.utcnow()
    params = {"job_type": JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT}
    try:
        head = db.execute(_LAST_BEAT_SQL, params).first()
        last_beat = _utc_naive(head[0] if head else None)

        beats = 0
        if last_beat is not None:
            window_start = last_beat - timedelta(seconds=EVIDENCE_WINDOW_SECONDS)
            tail = db.execute(
                _BEATS_AROUND_SQL, {**params, "window_start": window_start}
            ).first()
            beats = int((tail[0] if tail else 0) or 0)
    except Exception:
        logger.warning(
            "[observation_watchdog] heartbeat read failed", exc_info=True
        )
        return {
            "state": "unknown", "beats": 0, "age_seconds": None,
            "last_beat_utc": None,
        }

    if beats < EVIDENCE_MIN_BEATS or last_beat is None:
        return {
            "state": "absent", "beats": beats, "age_seconds": None,
            "last_beat_utc": None,
        }

    age = max(0.0, (now - last_beat).total_seconds())
    observations: int | None = None
    if age > STUCK_CONFIGURATION_AFTER_SECONDS:
        state = "stuck_configuration"
    elif age > SILENT_AFTER_SECONDS:
        # "Stopped" requires evidence the lane was RUNNING today. Without a beat
        # on the current ET trading date, the only defensible reading is that it
        # has not started — which it never had, on any of the 12 measured
        # sessions, by the time the 04:00 ET session gate opened.
        state = (
            "silent"
            if _et_date(last_beat) == _et_date(now)
            else "not_started"
        )
    else:
        state = "observing"
        # ALIVE BUT BLIND. Only meaningful when the process is demonstrably up,
        # so it is checked here and nowhere else.
        if in_regular_hours(now.replace(tzinfo=timezone.utc)):
            try:
                row = db.execute(
                    _RECENT_OBSERVATIONS_SQL,
                    {
                        **params,
                        "window_start": now
                        - timedelta(seconds=NOT_OBSERVING_WINDOW_SECONDS),
                    },
                ).first()
                recent_beats = int((row[0] if row else 0) or 0)
                observations = int((row[1] if row else 0) or 0)
                if recent_beats >= NOT_OBSERVING_MIN_BEATS and observations == 0:
                    state = "not_observing"
            except Exception:
                # Fail QUIET here, not open: an unreadable observation count is
                # not evidence that the lane is blind, and the silence detector
                # above is unaffected.
                logger.warning(
                    "[observation_watchdog] observation-count read failed",
                    exc_info=True,
                )
    return {
        "state": state,
        "beats": beats,
        "observations": observations,
        "age_seconds": round(age, 1),
        "last_beat_utc": last_beat.isoformat() + "Z",
        "silent_after_seconds": SILENT_AFTER_SECONDS,
        "in_session": in_market_session(now.replace(tzinfo=timezone.utc)),
        "in_regular_hours": in_regular_hours(now.replace(tzinfo=timezone.utc)),
        "et_date": _et_date(now),
    }


def _already_paged(db: Session, signature: str) -> bool:
    """Fails OPEN (page anyway). A watchdog silenced by its own dedupe query is
    the failure mode this module exists to end."""
    try:
        row = db.execute(
            _ALREADY_PAGED_SQL,
            {
                "window": _ALREADY_PAGED_WINDOW,
                "alert_type": LANE_OBSERVATION_SILENT,
                "signature": signature,
            },
        ).first()
        return row is not None
    except Exception:
        logger.warning(
            "[observation_watchdog] dedupe lookup failed; paging", exc_info=True
        )
        return False


def _human(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(max(0.0, seconds))
    if s < 120:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h {m % 60:02d}m" if m % 60 else f"{h}h"


def run_observation_watchdog(
    db: Session, *, user_id: int | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Periodic hook: page ONCE when the lane stops observing during a session.

    The critical log is unconditional and precedes the send, so the record
    survives a channel outage. The edge is latched only on CONFIRMED delivery --
    latching first is how a dropped page becomes a silent one on a box with a
    measured ~30% channel failure rate.
    """
    global _last_signature

    verdict = evaluate_lane_observation(db, now=now)
    state = verdict.get("state")

    if state == "observing":
        if _last_signature is not None:
            logger.warning(
                "[observation_watchdog] RECOVERED — the lane is observing again "
                "(age %ss, was silent since %s)",
                verdict.get("age_seconds"),
                _last_signature.split(":", 1)[-1],
            )
            _last_signature = None
        return verdict

    if state not in (
        "silent", "stuck_configuration", "not_observing", "not_started"
    ):
        return verdict

    if state == "silent" and not verdict.get("in_session"):
        # The lane legitimately observes nothing overnight and at the weekend.
        # This is the ONE place a clock is allowed to suppress, and it is
        # suppression of a page, not of detection: the verdict above is
        # already computed and returned.
        verdict["emitted"] = False
        verdict["suppressed"] = "outside_session"
        return verdict

    if state == "not_started" and not verdict.get("in_regular_hours"):
        # Not yet a fact. Before RTH open, "has not beaten today" is the normal
        # state of a lane that starts at 04:0x -- and of one that starts at
        # 06:45 or 07:06, both of which are in the measured record. This is the
        # gate that stops the module paging on 12 of 12 trading mornings.
        verdict["emitted"] = False
        verdict["suppressed"] = "before_regular_hours"
        return verdict

    # `stuck_configuration` is deliberately NOT session-gated. It is a statement
    # about the DEPLOYMENT ("nothing has written this key in days"), not about
    # today's market, and its signature is a frozen last-beat timestamp, so it
    # pages exactly once for the whole duration of the fault. Waiting for a
    # session to page about a watchdog that is blind is the failure mode
    # control_loop_watchdog has been living in since 2026-08-17.

    if state in ("not_observing", "not_started"):
        # `not_observing`: the last beat ADVANCES while the process is alive, so
        # keying on it would re-page every 60s.
        # `not_started`: the last beat is FROZEN at a previous session, so keying
        # on it would page once and then stay silent for every LATER day the lane
        # also failed to come up.
        # Both key on the ET session date instead: one page per session, which is
        # the "loud, single, unmissable" bar.
        signature = f"{state}:{verdict.get('et_date') or _et_date(now or datetime.utcnow())}"
    else:
        signature = f"{state}:{verdict['last_beat_utc']}"
    verdict["signature"] = signature
    if signature == _last_signature:
        verdict["emitted"] = False
        return verdict

    if _already_paged(db, signature):
        _last_signature = signature
        logger.warning(
            "[observation_watchdog] lane still %s (%s) — already paged for this "
            "outage by a previous process; not re-paging",
            state, _human(verdict.get("age_seconds")),
        )
        verdict["emitted"] = False
        verdict["suppressed"] = "already_paged"
        return verdict

    human = _human(verdict.get("age_seconds"))
    if state == "stuck_configuration":
        message = (
            f"LANE OBSERVATION HEARTBEAT MISSING FOR {human} — this is a "
            f"CONFIGURATION fault, not a fresh death (last beat "
            f"{verdict['last_beat_utc']}). Nothing has written the observation "
            f"heartbeat in days: the watchdog is blind until a writer exists "
            f"again."
        )
    elif state == "not_started":
        message = (
            f"LANE NEVER STARTED OBSERVING TODAY — regular trading hours are "
            f"open and the observation heartbeat has not beaten once on the "
            f"current ET session (newest beat {verdict['last_beat_utc']}, "
            f"{human} ago). The lane is not up. Nothing is reaching discovery "
            f"ranking, the arm bridge, or the learner."
        )
    elif state == "not_observing":
        message = (
            f"LANE IS ALIVE BUT BLIND — the observation heartbeat is beating "
            f"(last beat {verdict['last_beat_utc']}) but ZERO ignition "
            f"observations have been produced in the last "
            f"{int(NOT_OBSERVING_WINDOW_SECONDS / 60)} minutes of regular "
            f"trading hours. A liveness check would call this healthy. Nothing "
            f"is reaching discovery ranking or the arm bridge."
        )
    else:
        message = (
            f"LANE HAS STOPPED OBSERVING for {human} during the market session "
            f"(last beat {verdict['last_beat_utc']}). No ignition observations "
            f"are being produced: nothing is reaching discovery ranking, the arm "
            f"bridge, or the learner. Check the host lane process."
        )
    logger.critical("[observation_watchdog] %s", message)

    delivered = False
    try:
        delivered = bool(
            dispatch_alert(
                db=db,
                user_id=user_id,
                alert_type=LANE_OBSERVATION_SILENT,
                ticker=None,
                message=message,
                skip_throttle=True,
                content_signature=signature,
            )
        )
    except Exception:
        logger.warning(
            "[observation_watchdog] alert dispatch failed", exc_info=True
        )

    verdict["emitted"] = delivered
    if delivered:
        _last_signature = signature
    else:
        logger.warning(
            "[observation_watchdog] page NOT delivered for %s — retrying on the "
            "next tick (the lane is still %s)",
            signature, state,
        )
    return verdict
