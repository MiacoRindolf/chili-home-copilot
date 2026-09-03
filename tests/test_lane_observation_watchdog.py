"""DEFECT 3 — the lane stopped observing and nothing raised an alarm.

On 2026-09-01 the last three lines the lane ever wrote were

    08:03:11 WARNING auto_arm: reaped stale pre-entry session=19430 SSM
             state=watching_live (watched > 300s, never entered)
    08:03:16 INFO ignition_loop: symbol=RDAC move_pct=67.55 scored_ok=True
    08:03:17 INFO ignition_loop: symbol=ARCT move_pct=11.13 scored_ok=True

and the next line in the 573 MB log is the 2026-09-02 startup. 296 of that
day's 390 RTH minutes (75.9%) produced no observation. Across the 11 sessions
2026-08-19..09-02, 886 of 4,290 RTH minutes were dark (20.7%), with 9 blackouts
of >= 15 contiguous minutes in 6 sessions — three straight through the opening
bell. 24 confirmed market movers made their session high inside a blackout; 8
were never observed at all that day. Nothing alarmed on any of them.

Every detector that could have paged was blind, and each for its own reason:
  * `run_lane_health_check` is IN-PROCESS. It ran fine at 08:03:12
    ("phase=ok duration_ms=265") and died 5 seconds later.
  * `\\CHILI-live-runtime-watchdog`: Disabled since 2026-07-27 01:04:50, its
    target script no longer on disk, and Docker-scoped by its own comment.
  * `\\CHILI-liveness-watchdog`: disabled in the same four minutes.
  * `control_loop_watchdog`: SATURATED. Its heartbeat key's newest row is
    2026-08-17 12:26:10.871356 UTC — 21,764 rows, then nothing for 16 days, and
    no writer for it left anywhere in app/. Its change-only cooldown collapsed
    that into two messages in fifteen days (2026-08-26 15:56:27Z "219h 30m" and
    2026-09-02 17:29:08Z "389h 02m", quoting the SAME frozen last beat). It
    produced nothing on 2026-09-01 because from its point of view nothing
    changed.

Every test below fails on origin/main. All but one are pure unit tests with
every DB seam faked; the exception runs the new JSONB observation-sum query
against a real database, because a fake replaying canned rows cannot model a
predicate that filters away its own evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from app.services.trading.alerts import (
    LANE_OBSERVATION_SILENT,
    TIER_A,
    _INDIVIDUAL_MSG_TYPES,
    classify_alert_tier,
)
from app.services.trading.batch_job_constants import (
    JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT,
)
from app.services.trading.momentum_neural import control_loop_watchdog as CLW
from app.services.trading.momentum_neural import observation_watchdog as OW

# 2026-09-01 11:03:17 ET = 15:03:17 UTC — inside the session, the exact minute.
DEATH_UTC = datetime(2026, 9, 1, 15, 3, 17)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    """Replays a queued row per execute(); mirrors tests/test_control_loop_watchdog.py."""

    def __init__(self, rows, raises=False):
        self._rows = list(rows)
        self.raises = raises
        self.queries: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.queries.append((str(stmt), params or {}))
        if self.raises:
            raise RuntimeError("db down")
        return _FakeResult(self._rows.pop(0) if self._rows else None)


def _db(last_beat, beats=120, paged=False):
    return _FakeDb([(last_beat,), (beats,), (1,) if paged else None])


@pytest.fixture(autouse=True)
def _reset_module_memory():
    OW._last_signature = None
    CLW._last_signature = None
    yield
    OW._last_signature = None
    CLW._last_signature = None


# ── the alarm surface ────────────────────────────────────────────────────────


def test_observation_silence_is_tier_a_and_gets_its_own_message():
    """TIER_A is what dispatch_alert gates phone delivery on; membership in
    _INDIVIDUAL_MSG_TYPES is what stops it being edited into a pinned panel."""
    assert classify_alert_tier(LANE_OBSERVATION_SILENT) == TIER_A
    assert LANE_OBSERVATION_SILENT in _INDIVIDUAL_MSG_TYPES


# ── detection ────────────────────────────────────────────────────────────────


def test_fresh_heartbeat_is_observing():
    beat = DEATH_UTC - timedelta(seconds=20)
    assert OW.evaluate_lane_observation(_db(beat), now=DEATH_UTC)["state"] == "observing"


def test_the_2026_09_01_shape_is_silent():
    """Heartbeats stop at 08:03:17 PT; four minutes later the lane is silent."""
    beat = DEATH_UTC
    now = DEATH_UTC + timedelta(minutes=4)
    v = OW.evaluate_lane_observation(_db(beat), now=now)
    assert v["state"] == "silent"
    assert v["in_session"] is True


def test_a_box_that_never_hosted_the_lane_never_pages():
    """One beat could be a manual run; two prove a repeating writer."""
    v = OW.evaluate_lane_observation(_db(DEATH_UTC, beats=1), now=DEATH_UTC)
    assert v["state"] == "absent"


def test_empty_table_is_absent():
    assert OW.evaluate_lane_observation(_db(None, beats=0), now=DEATH_UTC)["state"] == "absent"


def test_db_failure_is_unknown_and_never_raises():
    v = OW.evaluate_lane_observation(_FakeDb([], raises=True), now=DEATH_UTC)
    assert v["state"] == "unknown"


def test_evidence_window_is_anchored_on_the_last_beat_not_on_now():
    """The bug control_loop_watchdog shipped twice: bounding the evidence scan on
    `now` silences exactly the longest outages."""
    beat = DEATH_UTC - timedelta(days=400)
    db = _db(beat)
    OW.evaluate_lane_observation(db, now=DEATH_UTC)
    _, params = db.queries[1]
    assert params["window_start"] == beat - timedelta(
        seconds=OW.EVIDENCE_WINDOW_SECONDS
    )


def test_head_read_carries_no_time_bound():
    db = _db(DEATH_UTC)
    OW.evaluate_lane_observation(db, now=DEATH_UTC)
    sql, params = db.queries[0]
    assert "started_at >=" not in sql
    assert set(params) == {"job_type"}
    assert params["job_type"] == JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT


# ── the session gate ─────────────────────────────────────────────────────────


def test_silence_outside_the_session_does_not_page(monkeypatch):
    """The lane legitimately observes nothing at 02:00 ET. A clock-free alarm
    here would page every night — which is why control_loop_watchdog's
    (correct, for a 24/7 exit owner) clock-free rule is NOT copied."""
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    # 2026-09-01 06:00 UTC = 02:00 ET.
    night = datetime(2026, 9, 1, 6, 0, 0)
    v = OW.run_observation_watchdog(_db(night - timedelta(hours=2)), now=night)
    assert v["state"] == "silent"
    assert v["suppressed"] == "outside_session"
    assert sent == []


def test_weekend_silence_does_not_page(monkeypatch):
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    saturday = datetime(2026, 9, 5, 16, 0, 0)  # 12:00 ET, Saturday
    v = OW.run_observation_watchdog(_db(saturday - timedelta(hours=2)), now=saturday)
    assert v["suppressed"] == "outside_session"
    assert sent == []


def test_the_opening_bell_is_inside_the_session():
    """08-21 (74 min) and 08-27 (109 min) were dark THROUGH the open."""
    assert OW.in_market_session(datetime(2026, 8, 27, 13, 35, 0)) is True   # 09:35 ET
    assert OW.in_market_session(datetime(2026, 8, 27, 9, 0, 0)) is True     # 05:00 ET
    assert OW.in_market_session(datetime(2026, 8, 27, 21, 0, 0)) is False   # 17:00 ET


# ── paging ───────────────────────────────────────────────────────────────────


def test_in_session_silence_pages_once_and_loudly(monkeypatch, caplog):
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    now = DEATH_UTC + timedelta(minutes=10)

    with caplog.at_level(logging.CRITICAL, logger=OW.__name__):
        v1 = OW.run_observation_watchdog(_db(DEATH_UTC), now=now)
    v2 = OW.run_observation_watchdog(_db(DEATH_UTC), now=now + timedelta(minutes=1))

    assert v1["emitted"] is True
    assert v2["emitted"] is False, "the alarm re-paged on the same outage"
    assert len(sent) == 1
    assert sent[0]["alert_type"] == LANE_OBSERVATION_SILENT
    assert sent[0]["skip_throttle"] is True
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_a_dropped_page_is_retried_not_latched(monkeypatch):
    """~30% of alerts in one measured 40h window on this box were dropped."""
    calls: list = []

    def _fail(**kw):
        calls.append(kw)
        return False

    monkeypatch.setattr(OW, "dispatch_alert", _fail)
    now = DEATH_UTC + timedelta(minutes=10)
    OW.run_observation_watchdog(_db(DEATH_UTC), now=now)
    OW.run_observation_watchdog(_db(DEATH_UTC), now=now + timedelta(minutes=1))
    assert len(calls) == 2


def test_a_restarted_process_does_not_re_page(monkeypatch):
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    now = DEATH_UTC + timedelta(minutes=10)
    OW.run_observation_watchdog(_db(DEATH_UTC, paged=True), now=now)
    assert sent == []


def test_recovery_clears_the_latch(monkeypatch, caplog):
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    now = DEATH_UTC + timedelta(minutes=10)
    OW.run_observation_watchdog(_db(DEATH_UTC), now=now)

    later = now + timedelta(minutes=5)
    with caplog.at_level(logging.WARNING, logger=OW.__name__):
        v = OW.run_observation_watchdog(_db(later - timedelta(seconds=10)), now=later)
    assert v["state"] == "observing"
    assert OW._last_signature is None
    assert any("RECOVERED" in r.message for r in caplog.records)


# ── stuck-at-dead is a CONFIGURATION fault, not a fresh death ────────────────


def test_a_writer_gone_for_weeks_is_stuck_configuration_not_silent():
    beat = DEATH_UTC - timedelta(days=16)
    assert (
        OW.evaluate_lane_observation(_db(beat), now=DEATH_UTC)["state"]
        == "stuck_configuration"
    )


def test_stuck_configuration_says_so_in_the_message(monkeypatch):
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    beat = DEATH_UTC - timedelta(days=16)
    OW.run_observation_watchdog(_db(beat), now=DEATH_UTC)
    assert len(sent) == 1
    assert "CONFIGURATION" in sent[0]["message"]
    assert sent[0]["content_signature"].startswith("stuck_configuration:")


def test_stuck_configuration_pages_even_outside_the_session(monkeypatch):
    """A blind watchdog is a standing fault, not a market-hours event."""
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    night = datetime(2026, 9, 1, 6, 0, 0)  # 02:00 ET
    OW.run_observation_watchdog(_db(night - timedelta(days=16)), now=night)
    assert len(sent) == 1


# ── the SAME repair, applied to the existing saturated deadman ───────────────


def test_control_loop_watchdog_classifies_its_own_16_day_corpse():
    """The measured state since 2026-08-17: 21,764 rows, then nothing, and no
    writer left in the tree. It reported that as a fresh death for 16 days."""
    beat = datetime(2026, 8, 17, 12, 26, 10, 871356)
    now = datetime(2026, 9, 2, 17, 29, 8)  # the second and last message it sent
    v = CLW.evaluate_control_loop(_db(beat), now=now)
    assert v["state"] == "stuck_configuration"


def test_control_loop_watchdog_still_reports_a_real_outage_as_dead():
    beat = DEATH_UTC - timedelta(minutes=30)
    assert CLW.evaluate_control_loop(_db(beat), now=DEATH_UTC)["state"] == "dead"


def test_control_loop_stuck_message_does_not_claim_a_fresh_death(monkeypatch):
    sent: list = []
    monkeypatch.setattr(CLW, "dispatch_alert", lambda **kw: sent.append(kw) or True)
    beat = datetime(2026, 8, 17, 12, 26, 10, 871356)
    CLW.run_control_loop_watchdog(_db(beat), now=datetime(2026, 9, 2, 17, 29, 8))
    assert len(sent) == 1
    assert "CONFIGURATION" in sent[0]["message"]
    assert "Relaunch the captured-PAPER runner" not in sent[0]["message"]
    assert sent[0]["content_signature"].startswith("stuck_configuration:")


# ── alive but BLIND: the 2026-08-21 / 2026-08-27 shape ──────────────────────


def _db_obs(last_beat, *, beats=120, recent_beats=30, observations=0, paged=False):
    """Rows in the order evaluate_lane_observation issues them: last beat,
    evidence count, recent (beats, observations), then the dedupe lookup."""
    return _FakeDb(
        [
            (last_beat,),
            (beats,),
            (recent_beats, observations),
            (1,) if paged else None,
        ]
    )


# 2026-08-27 13:35 UTC = 09:35 ET — inside the 06:30-08:18 PT blackout, which
# ran straight through the opening bell for 109 minutes.
BLIND_UTC = datetime(2026, 8, 27, 13, 35, 0)


def test_a_beating_process_with_zero_observations_is_not_observing():
    """A port check or liveness probe calls this healthy. It is not."""
    v = OW.evaluate_lane_observation(
        _db_obs(BLIND_UTC - timedelta(seconds=20)), now=BLIND_UTC
    )
    assert v["state"] == "not_observing"
    assert v["observations"] == 0


def test_a_beating_process_that_is_observing_is_healthy():
    v = OW.evaluate_lane_observation(
        _db_obs(BLIND_UTC - timedelta(seconds=20), observations=417), now=BLIND_UTC
    )
    assert v["state"] == "observing"
    assert v["observations"] == 417


def test_too_few_recent_beats_is_not_enough_evidence_to_call_it_blind():
    """One slow refresh cycle must not manufacture the verdict."""
    v = OW.evaluate_lane_observation(
        _db_obs(BLIND_UTC - timedelta(seconds=20), recent_beats=2), now=BLIND_UTC
    )
    assert v["state"] == "observing"


def test_zero_observations_in_premarket_is_not_an_alarm():
    """A genuinely quiet 04:05 ET premarket legitimately produces none."""
    premarket = datetime(2026, 8, 27, 8, 5, 0)  # 04:05 ET
    v = OW.evaluate_lane_observation(
        _db_obs(premarket - timedelta(seconds=20)), now=premarket
    )
    assert v["state"] == "observing"


def test_blind_lane_pages_once_per_session(monkeypatch):
    """The heartbeat keeps advancing while the lane is blind, so a last-beat
    signature would re-page every 60s."""
    sent: list = []
    monkeypatch.setattr(OW, "dispatch_alert", lambda **kw: sent.append(kw) or True)

    OW.run_observation_watchdog(
        _db_obs(BLIND_UTC - timedelta(seconds=20)), now=BLIND_UTC
    )
    later = BLIND_UTC + timedelta(minutes=5)
    OW.run_observation_watchdog(_db_obs(later - timedelta(seconds=20)), now=later)

    assert len(sent) == 1
    assert sent[0]["content_signature"] == "not_observing:2026-08-27"
    assert "ALIVE BUT BLIND" in sent[0]["message"]


def test_an_unreadable_observation_count_does_not_manufacture_an_alarm():
    """Fail QUIET: a failed read is not evidence that the lane is blind, and the
    silence detector is unaffected either way."""

    class _PartialDb(_FakeDb):
        def execute(self, stmt, params=None):
            if "observations" in str(stmt):
                raise RuntimeError("jsonb cast failed")
            return super().execute(stmt, params)

    db = _PartialDb([(BLIND_UTC - timedelta(seconds=20),), (120,)])
    assert OW.evaluate_lane_observation(db, now=BLIND_UTC)["state"] == "observing"


# ── real SQL, real database ─────────────────────────────────────────────────
#
# ONE test, deliberately. tests/test_control_loop_watchdog.py already runs the
# identical `_LAST_BEAT_SQL` / `_BEATS_AROUND_SQL` shapes against a real
# database at 1, 8, 30 and 400 days, and duplicating that here would only add
# more of the heavy TRUNCATE fixture to a box with a documented lock-contention
# problem. What is NOT covered anywhere else is the JSONB cast in
# `_RECENT_OBSERVATIONS_SQL`, which the fakes above cannot exercise at all —
# and the lesson that file records is exactly that a fake replaying canned rows
# cannot model a predicate that filters away its own evidence.


def test_real_sql_sums_observations_out_of_jsonb(db):
    import json
    import uuid as _uuid

    from sqlalchemy import text as _text

    now = datetime.utcnow()
    for i in range(40):
        db.execute(
            _text(
                "INSERT INTO brain_batch_jobs "
                "(id, job_type, status, started_at, ended_at, meta_json) "
                "VALUES (:id, :jt, 'ok', :ts, :ts, CAST(:meta AS jsonb))"
            ),
            {
                "id": str(_uuid.uuid4()),
                "jt": JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT,
                "ts": now - timedelta(seconds=i * 30),
                "meta": json.dumps({"observations": 5}),
            },
        )
    db.commit()

    row = db.execute(
        OW._RECENT_OBSERVATIONS_SQL,
        {
            "job_type": JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT,
            "window_start": now - timedelta(seconds=OW.NOT_OBSERVING_WINDOW_SECONDS),
        },
    ).first()

    # 900s window at a 30s cadence = 31 rows inclusive of `now`.
    assert row[0] == 31, "the window predicate does not select what it claims"
    assert row[1] == 31 * 5, "the JSONB cast did not sum"

    # And the same rows must NOT read as blind.
    v = OW.evaluate_lane_observation(db, now=now)
    assert v["state"] == "observing"
