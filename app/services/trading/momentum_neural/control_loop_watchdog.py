"""Deadman for the momentum control loop — the one alarm that reaches a phone.

WHY THIS EXISTS (measured, not assumed)
---------------------------------------
Over the 14 days to 2026-08-11 the control-loop heartbeat
(``brain_batch_jobs.job_type = 'momentum_live_loop_heartbeat'``, written every
~35s) shows **57 outages totalling 170.0 hours across a 333.8-hour span — the
loop was dead 50.9% of the time**, worst single outage 15.58 hours. One of them
ran 6.4 hours straight through a full regular session.

Nothing ever alarmed, because three suppressors sit in series:

1. ``evaluate_lane_health`` only builds a ``live_loop_stalled`` condition inside
   ``elif driver_mode == "event_loop"`` (lane_health.py:1245), and
   ``live_runner_driver_configuration`` short-circuits to ``(None, None)`` at
   :379-380 whenever ``chili_momentum_live_runner_enabled`` is false — which is
   correct in every container, because the loop is a HOST process. The watcher
   was disabled for not being the runner.
2. ``run_lane_health_check`` is registered only under ``include_momentum_exec``
   (trading_scheduler.py:6211-6213), whose role tuple excludes ``rnd_only``.
   The scheduler container is ``rnd_only`` and the web container is ``none``, so
   the periodic check runs NOWHERE. Even the ungated kill-switch and
   equity-tape conditions therefore produce no push.
3. ``_write_alert_row`` writes straight to ``AlertHistory``; it never calls
   ``dispatch_alert``. So even a perfectly hosted check yields a log line, a DB
   row and a red banner on a **pull** surface — all three of which an operator
   who is not currently looking at a screen will miss.

This module is deliberately independent of all three. It does not import or
modify ``lane_health``; it reads the heartbeat table directly and dispatches a
TIER_A individual alert, which is the only path in this codebase that produces
an actual phone notification (``dispatch_alert`` gates delivery on
``tier in (TIER_A, TIER_B)``, and membership in ``_INDIVIDUAL_MSG_TYPES`` is
what forces a new message instead of a silent pinned-panel edit).

DESIGN NOTES THAT ARE LOAD-BEARING
----------------------------------
*Evidence gate anchored on the last beat, never on the clock.* The cold-start
question is "did a repeating writer ever exist on this box?", not "how old is the
newest row?". Two beats in the hour ENDING at the newest beat prove a repeating
writer; fewer means this machine (fresh DB, CI, a dev box, a laptop that never
hosts the lane) never ran the loop and must never page. A box that HAS hosted it
stays armed for as long as the loop stays dead, with no upper bound.

The first cut got this wrong in the most embarrassing way available: it bounded a
single aggregate by `now - 7 days` and asserted in this very docstring that the
bound applied only to the scan. It did not — `count(*)` and `max(...)` shared the
filter, so at +7.0d the verdict flipped to `absent` and the alarm went silent
forever, at exactly the outages that matter most. The mock-only test that
"proved" otherwise never modelled the WHERE clause. There are now tests that run
the real SQL against a real database at 1, 8, 30 and 400 days.

*Detection is clock-free.* The loop is a 24/7 exit owner (lane_health.py:1246-47
says so, and a 36.8h continuous run across Sat/Sun/Mon confirms it). Market
session may tier the PUSH, never the detection — gating detection on a session
predicate would have hidden the 02:05 ET outage entirely. ``_expected_trading_
window_open()`` looks like a constant-true no-op but resolves to
``is_tradeable_now("SPY")`` on this box's settings, measured FALSE at 20:44 ET.

*Identity-blind, single row.* A deadman answers "is anything alive?". Binding it
to the captured-PAPER account would make it go blind exactly while that account
is being reconfigured. Reading exactly one row (newest by ``started_at``) also
makes it structurally immune to the ``live_runner_loop_owner_overlap``
false-positive, whose 2.0s clock tolerance trips on any hand-minted relaunch.

*Edge-triggered, never a timer.* One alert row per outage, keyed on the last
beat — which does not advance while the loop is dead, so the signature is stable
for the whole outage. Re-paging on a fixed cadence would have produced hundreds
of messages against a table that holds 21 freeze rows for its entire lifetime.

*The restart case is checked against the DB, because nothing else does.*
``dispatch_alert`` accepts ``content_signature`` and writes it to
``trading_alerts`` — but never reads it back; there is no dedupe on that column
anywhere in the codebase. Module memory alone would therefore re-page every time
the scheduler restarts, and on this box restarts CORRELATE with outages (the
2026-08-10 Docker crash restarted every container at once), so that is the
common case rather than a rare one. Hence the explicit lookup below.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..alerts import MOMENTUM_LOOP_DEAD, dispatch_alert
from ..batch_job_constants import JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT

logger = logging.getLogger(__name__)

# ~4 missed beats at the observed ~35s cadence. Not tighter: `record_live_runner_
# loop_run` read-back-verifies and raises on non-persist, and this box has a
# documented idle-in-transaction cascade, so a 1-2 beat threshold would read a
# Postgres blip as a death. Measured: 57 outages at 150s vs 61 at 120s — the
# delta is sub-2-minute restart flaps only.
DEAD_AFTER_SECONDS = 150.0

# The evidence window is anchored on the LAST BEAT, never on `now`.
#
# The first cut of this module bounded a single aggregate by
# `started_at >= now - 7 days` and claimed that bounded only the scan, not the
# age. That was wrong, and provably so: `count(*)` and `max(...)` are aggregates
# over the SAME filtered row set, so once an outage passes the horizon the count
# reaches zero, the evidence gate reads `absent`, and the alarm goes permanently
# silent. A sweep against the live table measured the exact cliff -- at +6.999d
# the verdict is `dead`; at +7.0d it is `absent`, and the maximum age reachable
# while still armed (603,908.8s) can never exceed the horizon (604,800.0s). An
# age ceiling silences exactly the longest outages, which is the opposite of the
# job this module exists to do.
#
# Anchoring the window on the last beat removes the ceiling: "were there >= 2
# beats AROUND the last beat" is age-independent, so a 30-day-dead loop still
# pages.
EVIDENCE_WINDOW_SECONDS = 3600.0

# Two beats prove a REPEATING writer. One beat could be a single manual run.
EVIDENCE_MIN_BEATS = 2

# STUCK-AT-DEAD (2026-09-02). This module's premise -- "a box that HAS hosted the
# loop stays armed for as long as the loop stays dead, with no upper bound" -- is
# correct for an OUTAGE and wrong for a DECOMMISSION, and the difference has cost
# the operator every alarm this module was written to raise.
#
# Measured: the newest `momentum_live_loop_heartbeat` row is 2026-08-17
# 12:26:10.871356 UTC. 21,764 rows, then nothing for 16 days. No writer for that
# key exists anywhere in app/ any more -- every remaining reference is a read --
# and the alert text still says "Relaunch the captured-PAPER runner", a lane the
# timeshare supervisor replaced. Meanwhile iqfeed_exact_print_heartbeat,
# iqfeed_drain_metrics_heartbeat and scheduler_worker_heartbeat are all fresh to
# 2026-09-02 23:0x, so the table works; this one key is orphaned.
#
# Because the signature is keyed on the last beat and the last beat no longer
# advances, the change-only cooldown collapsed 16 days of "alarm" into exactly
# two messages: 2026-08-26 15:56:27Z ("219h 30m") and 2026-09-02 17:29:08Z
# ("389h 02m"), both quoting the same frozen last beat. It produced NOTHING on
# 2026-09-01, the day the lane went dark for five hours, because from its point
# of view nothing changed.
#
# A stuck-at-dead alarm must be loud about being STUCK, not quietly repeat a
# corpse. Past this age the verdict is a CONFIGURATION fault with its own
# message and its own signature namespace.
STUCK_CONFIGURATION_AFTER_SECONDS = 3 * 24 * 3600.0

# Both statements are INDEX-ONLY on ix_bbj_live_loop_heartbeat_latest
# (started_at DESC, id DESC) WHERE job_type = 'momentum_live_loop_heartbeat':
# 2.0ms and 73.6ms measured. `started_at` rather than
# COALESCE(ended_at, started_at) deliberately -- ended_at is not in the index, so
# the COALESCE forces a heap fetch per row and the unbounded form measures 4.2s.
# On the live table the two differ by 0.017s, far inside a 150s threshold.
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

# Restart-survival dedupe. `trading_alerts` has no index on alert_type or
# content_signature, so the natural two-column lookup is a 3.3s SEQ SCAN over
# 94k rows (measured) -- unacceptable inside a scheduler job on a box with a
# documented idle-in-transaction cascade. Bounding by the PK turns it into an
# index range scan (115ms measured). No migration: ID 356 is already reserved by
# an unmerged branch.
#
# `success = true` is load-bearing, not defensive. dispatch_alert writes the
# AlertHistory row whether or not delivery worked (sent_via='sms_failed',
# success=false) and never reads content_signature back. Without this predicate a
# FAILED page counts as a delivered one -- and Telegram failure here is not
# hypothetical: 30 of 101 alerts in one measured 40h window on this box were
# dropped, including a genuine 21-minute blackout with zero successful sends.
# MOMENTUM_LOOP_DEAD is not in _STOP_CRITICAL_TYPES, so the 2s/8s retry ladder
# never runs for it either.
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

# ~8 days of alerts at the measured 488/day. The worst observed outage is 15.58h
# (~320 rows), and alert volume COLLAPSES during an outage because most alerts
# come from the lane that just died -- so in wall-clock terms this covers far
# more than 8 days of any outage it needs to remember.
_ALREADY_PAGED_WINDOW = 4000

# Module-level edge memory: the cheap short-circuit that keeps the 60s cadence
# from re-querying. The DB lookup above is what survives a restart.
_last_signature: str | None = None


def _utc_naive(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def evaluate_control_loop(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only verdict on the control loop. Never raises, never writes.

    ``state`` is one of:
      ``absent``  — no repeating writer on this box; this machine does not host
                    the loop, so silence is correct and permanent.
      ``alive``   — a beat within DEAD_AFTER_SECONDS.
      ``dead``    — a repeating writer existed and has stopped.
      ``stuck_configuration`` — the writer has been gone for DAYS. That is a
                    deploy/configuration fault, not a fresh death, and it is the
                    state this module has actually been in since 2026-08-17.
    """
    now = now or datetime.utcnow()
    params = {"job_type": JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT}
    try:
        # Step 1: the newest beat, with NO time bound. This is what keeps an
        # arbitrarily old outage reportable.
        head = db.execute(_LAST_BEAT_SQL, params).first()
        last_beat = _utc_naive(head[0] if head else None)

        beats = 0
        if last_beat is not None:
            # Step 2: how many beats landed in the hour ENDING at that beat.
            # Anchoring here rather than on `now` is the whole fix.
            window_start = last_beat - timedelta(seconds=EVIDENCE_WINDOW_SECONDS)
            tail = db.execute(
                _BEATS_AROUND_SQL, {**params, "window_start": window_start}
            ).first()
            beats = int((tail[0] if tail else 0) or 0)
    except Exception:
        # A watchdog that throws is worse than one that is quiet: it would take
        # the scheduler job down with it.
        logger.warning("[control_loop_watchdog] heartbeat read failed", exc_info=True)
        return {"state": "unknown", "beats": 0, "age_seconds": None, "last_beat_utc": None}

    if beats < EVIDENCE_MIN_BEATS or last_beat is None:
        # No repeating writer here. Fresh DB, CI, dev box, a machine that has
        # never hosted the lane. Hard return — this can never page.
        return {"state": "absent", "beats": beats, "age_seconds": None, "last_beat_utc": None}

    age = max(0.0, (now - last_beat).total_seconds())
    if age > STUCK_CONFIGURATION_AFTER_SECONDS:
        state = "stuck_configuration"
    elif age > DEAD_AFTER_SECONDS:
        state = "dead"
    else:
        state = "alive"
    return {
        "state": state,
        "beats": beats,
        "age_seconds": round(age, 1),
        "last_beat_utc": last_beat.isoformat() + "Z",
        "dead_after_seconds": DEAD_AFTER_SECONDS,
    }


def _already_paged(db: Session, signature: str) -> bool:
    """Did a previous PROCESS already page for this exact outage?

    Fails OPEN (returns False → page anyway). A watchdog that goes quiet because
    its dedupe query errored is the failure mode this module exists to end; a
    duplicate page is merely annoying.
    """
    try:
        row = db.execute(
            _ALREADY_PAGED_SQL,
            {
                "window": _ALREADY_PAGED_WINDOW,
                "alert_type": MOMENTUM_LOOP_DEAD,
                "signature": signature,
            },
        ).first()
        return row is not None
    except Exception:
        logger.warning("[control_loop_watchdog] dedupe lookup failed; paging", exc_info=True)
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


def run_control_loop_watchdog(
    db: Session, *, user_id: int | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Periodic hook: detect a dead control loop and page ONCE per outage.

    Detection and the critical log are clock-free. Only the phone push is tiered
    by session, and an open position overrides that unconditionally.
    """
    global _last_signature

    verdict = evaluate_control_loop(db, now=now)
    state = verdict.get("state")

    if state not in ("dead", "stuck_configuration"):
        if _last_signature is not None and state == "alive":
            logger.warning(
                "[control_loop_watchdog] RECOVERED — control loop beating again "
                "(age %ss, was dead since %s)",
                verdict.get("age_seconds"),
                _last_signature.removeprefix("dead:"),
            )
            _last_signature = None
        return verdict

    # The last beat does not advance while the loop is dead, so this signature is
    # stable for the whole outage: one row per outage, idempotent across restarts.
    signature = f"{state}:{verdict['last_beat_utc']}"
    verdict["signature"] = signature
    if signature == _last_signature:
        verdict["emitted"] = False
        return verdict

    # Fresh process (or first sight of this outage). Ask the DB whether a
    # previous process already DELIVERED a page for this same outage.
    #
    # Note what is deliberately NOT done here: the edge is not claimed in module
    # memory yet. Latching before delivery is confirmed would mean one transient
    # Telegram timeout silences the alarm for the entire outage -- and at the
    # measured ~30% channel failure rate on this box that is the likely case, not
    # the edge case. The 60s tick IS the retry; it only has to be allowed to run.
    if _already_paged(db, signature):
        _last_signature = signature
        logger.warning(
            "[control_loop_watchdog] control loop still %s (%s) — already paged "
            "for this outage by a previous process; not re-paging",
            state, _human(verdict.get("age_seconds")),
        )
        verdict["emitted"] = False
        verdict["suppressed"] = "already_paged"
        return verdict

    human = _human(verdict.get("age_seconds"))
    if state == "stuck_configuration":
        # Do NOT report this as a fresh death. Since 2026-08-17 this module has
        # been asserting exactly that against a heartbeat key with no writer
        # left in the tree, which is how it produced nothing at all on
        # 2026-09-01 while the lane was dark for five hours.
        message = (
            f"CONTROL-LOOP HEARTBEAT MISSING FOR {human} — this is a "
            f"CONFIGURATION fault, not a fresh outage (last beat "
            f"{verdict['last_beat_utc']}). Nothing has written "
            f"momentum_live_loop_heartbeat in days, so this deadman is BLIND: "
            f"it cannot signal a real death until a writer exists again."
        )
    else:
        message = (
            f"CONTROL LOOP DEAD — no momentum heartbeat for {human} "
            f"(last beat {verdict['last_beat_utc']}). The lane is not selecting or "
            f"arming; exits are unowned. Relaunch the captured-PAPER runner."
        )
    # Unconditional, and BEFORE the send: this is the record that survives a
    # channel outage, so it must not be contingent on the channel.
    logger.critical("[control_loop_watchdog] %s", message)

    delivered = False
    try:
        delivered = bool(
            dispatch_alert(
                db=db,
                user_id=user_id,
                alert_type=MOMENTUM_LOOP_DEAD,
                ticker=None,
                message=message,
                # One page per outage; the signature already carries the dedupe,
                # and the generic per-ticker throttle would suppress it for 5
                # minutes on a None ticker shared with every other tickerless
                # alert.
                skip_throttle=True,
                content_signature=signature,
            )
        )
    except Exception:
        # Never let a delivery failure break the scheduler job — the critical log
        # above has already recorded the fact.
        logger.warning("[control_loop_watchdog] alert dispatch failed", exc_info=True)

    verdict["emitted"] = delivered
    if delivered:
        # Latch ONLY on confirmed delivery. dispatch_alert returns the send
        # result; discarding it was how a dropped page became a silent one.
        _last_signature = signature
    else:
        logger.warning(
            "[control_loop_watchdog] page NOT delivered for %s — retrying on the "
            "next tick (the loop is still dead)",
            signature,
        )

    return verdict
