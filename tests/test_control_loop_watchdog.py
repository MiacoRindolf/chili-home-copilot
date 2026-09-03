"""Tests for the momentum control-loop deadman.

The bug being prevented is SILENCE, so most of these assert that the alarm still
fires under conditions that silenced every previous watcher: an outage older than
any lookback window, a scheduler that just restarted, a Telegram channel that
dropped the message.

Two groups run the REAL SQL against a real database. They exist because the first
cut of this module shipped two bugs that a mock-only suite structurally could not
see: a fake that replays canned rows cannot model a WHERE clause that filters
away its own evidence.
"""

import logging
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.services.trading.alerts import (
    MOMENTUM_LOOP_DEAD,
    TIER_A,
    _INDIVIDUAL_MSG_TYPES,
    classify_alert_tier,
)
from app.services.trading.batch_job_constants import (
    JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
)
from app.services.trading.momentum_neural import control_loop_watchdog as clw
from app.services.trading.momentum_neural.control_loop_watchdog import (
    DEAD_AFTER_SECONDS,
    EVIDENCE_MIN_BEATS,
    EVIDENCE_WINDOW_SECONDS,
    evaluate_control_loop,
    run_control_loop_watchdog,
)

NOW = datetime(2026, 8, 11, 14, 30, 0)


class FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class FakeDb:
    """Minimal Session stand-in: replays a queued row per execute() call."""

    def __init__(self, rows, raises=False):
        self._rows = list(rows)
        self.raises = raises
        self.queries = []

    def execute(self, stmt, params=None):
        self.queries.append((str(stmt), params or {}))
        if self.raises:
            raise RuntimeError("db down")
        return FakeResult(self._rows.pop(0) if self._rows else None)


def _beats(count, last_beat):
    """evaluate_control_loop issues TWO reads: the head, then the window count."""
    return FakeDb([(last_beat,), (count,)])


def _dead_db(age_seconds=3600, already_paged=False):
    """Head, window count, then the dedupe lookup's answer."""
    return FakeDb([
        (NOW - timedelta(seconds=age_seconds),),
        (500,),
        (1,) if already_paged else None,
    ])


def _ok(sent):
    """dispatch_alert stub that reports a DELIVERED page."""
    return lambda **kw: (sent.append(kw), True)[1]


@pytest.fixture(autouse=True)
def _reset_edge_memory():
    clw._last_signature = None
    yield
    clw._last_signature = None


# ── evaluate_control_loop ─────────────────────────────────────────────────────


def test_fresh_beat_is_alive():
    v = evaluate_control_loop(_beats(500, NOW - timedelta(seconds=20)), now=NOW)
    assert v["state"] == "alive"
    assert v["age_seconds"] == 20.0


def test_beat_exactly_at_threshold_is_still_alive():
    db = _beats(500, NOW - timedelta(seconds=DEAD_AFTER_SECONDS))
    assert evaluate_control_loop(db, now=NOW)["state"] == "alive"


def test_beat_past_threshold_is_dead():
    db = _beats(500, NOW - timedelta(seconds=DEAD_AFTER_SECONDS + 1))
    assert evaluate_control_loop(db, now=NOW)["state"] == "dead"


def test_no_writer_is_absent_not_dead():
    """A fresh DB / CI box / dev laptop must never page."""
    assert evaluate_control_loop(FakeDb([(None,)]), now=NOW)["state"] == "absent"


def test_single_beat_is_absent():
    """One row could be a manual run; two prove a REPEATING writer."""
    db = _beats(EVIDENCE_MIN_BEATS - 1, NOW - timedelta(days=3))
    assert evaluate_control_loop(db, now=NOW)["state"] == "absent"


def test_two_beats_is_enough_evidence_to_arm():
    db = _beats(EVIDENCE_MIN_BEATS, NOW - timedelta(days=3))
    assert evaluate_control_loop(db, now=NOW)["state"] == "dead"


def test_evidence_window_is_anchored_on_the_last_beat_not_on_now():
    """Regression guard for the age-ceiling bug.

    The first cut bounded a single aggregate by `started_at >= now - 7d`, so once
    an outage passed the horizon the count reached zero and the verdict flipped to
    `absent` -- permanently silent at exactly the outages that matter most.
    """
    last_beat = NOW - timedelta(days=30)
    db = _beats(9000, last_beat)
    evaluate_control_loop(db, now=NOW)

    assert len(db.queries) == 2, "head read, then window count"
    _, window_params = db.queries[1]
    assert window_params["window_start"] == last_beat - timedelta(
        seconds=EVIDENCE_WINDOW_SECONDS
    )
    # The decisive property: the window sits back at the OUTAGE, not near `now`.
    assert window_params["window_start"] < NOW - timedelta(days=29)


def test_head_read_carries_no_time_bound():
    """A time bound on the head read would bring the ceiling straight back."""
    db = _beats(9000, NOW - timedelta(days=30))
    evaluate_control_loop(db, now=NOW)
    head_sql, head_params = db.queries[0]
    assert set(head_params) == {"job_type"}
    assert head_params["job_type"] == JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT
    assert "max(started_at)" in head_sql


# Both states page. `dead` is a fresh outage; `stuck_configuration` (added
# 2026-09-02) is the same silence once the writer has been gone for days, which
# is a deploy fault and gets its own message. The property these tests defend is
# unchanged: an outage must NEVER read as `absent` or `alive` at any age.
_PAGING_STATES = ("dead", "stuck_configuration")


def test_ancient_outage_still_pages():
    v = evaluate_control_loop(_beats(9000, NOW - timedelta(days=30)), now=NOW)
    assert v["state"] in _PAGING_STATES
    assert v["age_seconds"] == pytest.approx(30 * 86400, abs=1)


def test_timezone_aware_beat_is_compared_as_utc_naive():
    from datetime import timezone

    aware = (NOW - timedelta(seconds=20)).replace(tzinfo=timezone.utc)
    v = evaluate_control_loop(_beats(500, aware), now=NOW)
    assert v["state"] == "alive"
    assert v["age_seconds"] == 20.0


def test_clock_skew_never_yields_negative_age():
    v = evaluate_control_loop(_beats(500, NOW + timedelta(seconds=90)), now=NOW)
    assert v["age_seconds"] == 0.0
    assert v["state"] == "alive"


def test_db_failure_is_unknown_and_never_raises():
    """A watchdog that throws takes the scheduler job down with it."""
    assert evaluate_control_loop(FakeDb([], raises=True), now=NOW)["state"] == "unknown"


def test_unknown_state_does_not_page(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    run_control_loop_watchdog(FakeDb([], raises=True), now=NOW)
    assert sent == []


# ── real SQL, real database ──────────────────────────────────────────────────


def _insert_beats(db, *, count, oldest, step_seconds=35):
    for i in range(count):
        db.execute(
            text(
                "INSERT INTO brain_batch_jobs (id, job_type, status, started_at, ended_at) "
                "VALUES (:id, :jt, 'ok', :ts, :ts)"
            ),
            {
                "id": str(uuid.uuid4()),
                "jt": JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
                "ts": oldest + timedelta(seconds=i * step_seconds),
            },
        )
    db.commit()


@pytest.mark.parametrize("age_days", [1, 8, 30, 400])
def test_real_sql_pages_at_any_outage_age(db, age_days):
    """THE test that was missing.

    The old query returned `absent` past 7 days. A sweep against the live table
    measured the cliff exactly: +6.999d was `dead`, +7.0d was `absent`. These
    straddle it so a reintroduced lookback fails here instead of as production
    silence.
    """
    now = datetime.utcnow()
    last_beat = now - timedelta(days=age_days)
    _insert_beats(db, count=120, oldest=last_beat - timedelta(seconds=120 * 35))

    v = evaluate_control_loop(db, now=now)

    assert v["state"] in _PAGING_STATES, f"{age_days}d outage must still page"
    assert v["beats"] >= EVIDENCE_MIN_BEATS
    assert v["age_seconds"] == pytest.approx(age_days * 86400, abs=120)


def test_real_sql_empty_table_is_absent(db):
    """No writer ever on this box -- must never page, at any age."""
    assert evaluate_control_loop(db, now=datetime.utcnow())["state"] == "absent"


def test_real_sql_lone_beat_is_absent(db):
    """One row is a manual run, not a repeating writer."""
    _insert_beats(db, count=1, oldest=datetime.utcnow() - timedelta(days=3))
    assert evaluate_control_loop(db, now=datetime.utcnow())["state"] == "absent"


def test_real_dedupe_sql_ignores_a_failed_delivery(db):
    """A dropped page must not count as a delivered one.

    dispatch_alert writes the row with success=false when Telegram fails and
    never reads content_signature back. 30 of 101 alerts were dropped this way in
    one measured 40h window on this box.
    """
    sig = "dead:2026-08-11T00:00:00Z"
    for delivered in (False, True):
        db.execute(
            text(
                "INSERT INTO trading_alerts (alert_type, message, content_signature, "
                "sent_via, success, created_at) VALUES (:t, 'x', :s, :v, :ok, now())"
            ),
            {
                "t": MOMENTUM_LOOP_DEAD,
                "s": sig if delivered else sig + "-dropped",
                "v": "twilio" if delivered else "sms_failed",
                "ok": delivered,
            },
        )
    db.commit()

    assert clw._already_paged(db, sig) is True, "a delivered page is remembered"
    assert clw._already_paged(db, sig + "-dropped") is False, "a dropped one is not"


# ── run_control_loop_watchdog ────────────────────────────────────────────────


def test_dead_loop_dispatches_tier_a_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    v = run_control_loop_watchdog(_dead_db(), now=NOW)

    assert v["emitted"] is True
    assert len(sent) == 1
    assert sent[0]["alert_type"] == MOMENTUM_LOOP_DEAD
    assert sent[0]["skip_throttle"] is True
    assert sent[0]["content_signature"] == v["signature"]


def test_alive_loop_is_silent(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    run_control_loop_watchdog(_beats(500, NOW - timedelta(seconds=10)), now=NOW)
    assert sent == []


def test_repeated_ticks_page_exactly_once(monkeypatch):
    """The loop is dead for hours and this runs every 60s. One page, not 240."""
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    last_beat = NOW - timedelta(hours=6)

    for _ in range(240):
        run_control_loop_watchdog(FakeDb([(last_beat,), (500,), None]), now=NOW)

    assert len(sent) == 1


def test_a_dropped_page_is_retried_on_the_next_tick(monkeypatch):
    """Regression guard for the silent-drop bug.

    The first cut latched module memory BEFORE dispatch and discarded
    dispatch_alert's bool, so one transient Telegram failure silenced the alarm
    for the whole outage -- at a measured ~30% channel failure rate, the likely
    case rather than the edge case.
    """
    outcomes = [False, False, True]
    sent = []

    def flaky(**kw):
        sent.append(kw)
        return outcomes[len(sent) - 1]

    monkeypatch.setattr(clw, "dispatch_alert", flaky)
    last_beat = NOW - timedelta(hours=2)

    emitted = [
        run_control_loop_watchdog(FakeDb([(last_beat,), (500,), None]), now=NOW)["emitted"]
        for _ in range(3)
    ]

    assert emitted == [False, False, True]
    assert len(sent) == 3, "must keep trying while undelivered"

    # ...and stop once delivered.
    run_control_loop_watchdog(FakeDb([(last_beat,), (500,), None]), now=NOW)
    assert len(sent) == 3


def test_undelivered_page_still_logs_critical(monkeypatch, caplog):
    """The log is the record that survives a channel outage."""
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: False)
    with caplog.at_level(logging.CRITICAL):
        run_control_loop_watchdog(_dead_db(), now=NOW)
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_restart_during_outage_does_not_re_page(monkeypatch):
    """Module memory is gone after a restart; the DB lookup must catch it."""
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    run_control_loop_watchdog(_dead_db(), now=NOW)
    assert len(sent) == 1

    clw._last_signature = None  # simulate the restart
    v = run_control_loop_watchdog(_dead_db(already_paged=True), now=NOW)

    assert len(sent) == 1
    assert v["emitted"] is False
    assert v["suppressed"] == "already_paged"


def test_new_outage_after_a_recovery_pages_again(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    run_control_loop_watchdog(_dead_db(age_seconds=3600), now=NOW)
    run_control_loop_watchdog(_beats(500, NOW - timedelta(seconds=5)), now=NOW)
    run_control_loop_watchdog(_dead_db(age_seconds=200), now=NOW)

    assert len(sent) == 2
    assert sent[0]["content_signature"] != sent[1]["content_signature"]


def test_dedupe_query_is_pk_bounded_and_success_filtered():
    """Unbounded this is a 3.3s seq scan over 94k rows inside a scheduler job."""
    db = _dead_db()
    clw._already_paged(db, "dead:x")
    sql, params = db.queries[0]
    assert "max(id)" in sql
    assert "success = true" in sql
    assert params["window"] == clw._ALREADY_PAGED_WINDOW


def test_dispatch_exception_never_breaks_the_job(monkeypatch, caplog):
    def boom(**kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(clw, "dispatch_alert", boom)
    with caplog.at_level(logging.CRITICAL):
        v = run_control_loop_watchdog(_dead_db(), now=NOW)
    assert v["emitted"] is False, "an exception is not a delivery"
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_message_names_the_age_and_the_consequence(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", _ok(sent))
    run_control_loop_watchdog(_dead_db(age_seconds=6 * 3600), now=NOW)
    msg = sent[0]["message"]
    assert "6h" in msg
    assert "exits are unowned" in msg


def test_recovery_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: True)
    run_control_loop_watchdog(_dead_db(), now=NOW)
    with caplog.at_level(logging.WARNING):
        run_control_loop_watchdog(_beats(500, NOW - timedelta(seconds=5)), now=NOW)
    assert any("RECOVERED" in r.getMessage() for r in caplog.records)


# ── delivery contract ────────────────────────────────────────────────────────


def test_alert_is_tier_a():
    assert classify_alert_tier(MOMENTUM_LOOP_DEAD, None, 0.0) == TIER_A


def test_alert_bypasses_the_pinned_panel():
    """push_to_telegram_panel EDITS a pinned message — no phone notification."""
    assert MOMENTUM_LOOP_DEAD in _INDIVIDUAL_MSG_TYPES


def test_alert_type_fits_the_column():
    from app.models.trading import AlertHistory

    assert len(MOMENTUM_LOOP_DEAD) <= AlertHistory.__table__.c.alert_type.type.length


def test_watchdog_does_not_import_lane_health():
    """Independence is the whole point: three suppressors live in that module."""
    import inspect

    src = inspect.getsource(clw)
    assert "lane_health" not in src.split('"""', 2)[2]
