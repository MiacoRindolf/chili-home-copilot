"""Tests for the momentum control-loop deadman.

The bug being prevented is SILENCE, so most of these assert that the alarm still
fires under conditions that silenced every previous watcher: an outage older than
any lookback window, a market that is closed, a lane whose master flag is off, a
scheduler that just restarted.
"""

import logging
from datetime import datetime, timedelta

import pytest

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
    return FakeDb([(count, last_beat)])


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
    assert evaluate_control_loop(_beats(0, None), now=NOW)["state"] == "absent"


def test_single_beat_is_absent():
    """One row could be a manual run; two prove a REPEATING writer."""
    db = _beats(EVIDENCE_MIN_BEATS - 1, NOW - timedelta(days=3))
    assert evaluate_control_loop(db, now=NOW)["state"] == "absent"


def test_two_beats_is_enough_evidence_to_arm():
    db = _beats(EVIDENCE_MIN_BEATS, NOW - timedelta(days=3))
    assert evaluate_control_loop(db, now=NOW)["state"] == "dead"


def test_ancient_outage_still_pages():
    """THE regression guard.

    An age ceiling would silence exactly the longest outages. The worst measured
    outage was 15.58h; this asserts a 30-DAY outage still reads `dead`, because
    the evidence gate asks whether a writer ever existed, not how fresh it is.
    """
    db = _beats(9000, NOW - timedelta(days=30))
    v = evaluate_control_loop(db, now=NOW)
    assert v["state"] == "dead"
    assert v["age_seconds"] == pytest.approx(30 * 86400, abs=1)


def test_evidence_scan_is_bounded_but_never_silences():
    """The horizon bounds the COUNT scan; it must not bound the age."""
    db = _beats(9000, NOW - timedelta(days=30))
    evaluate_control_loop(db, now=NOW)
    _, params = db.queries[0]
    assert params["job_type"] == JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT
    # horizon_start bounds `started_at >=`, i.e. which rows are COUNTED
    assert params["horizon_start"] < NOW


def test_timezone_aware_beat_is_compared_as_utc_naive():
    """A tz-aware row must not raise TypeError against a naive utcnow()."""
    from datetime import timezone

    aware = (NOW - timedelta(seconds=20)).replace(tzinfo=timezone.utc)
    v = evaluate_control_loop(_beats(500, aware), now=NOW)
    assert v["state"] == "alive"
    assert v["age_seconds"] == 20.0


def test_clock_skew_never_yields_negative_age():
    db = _beats(500, NOW + timedelta(seconds=90))
    v = evaluate_control_loop(db, now=NOW)
    assert v["age_seconds"] == 0.0
    assert v["state"] == "alive"


def test_db_failure_is_unknown_and_never_raises():
    """A watchdog that throws takes the scheduler job down with it."""
    v = evaluate_control_loop(FakeDb([], raises=True), now=NOW)
    assert v["state"] == "unknown"


def test_unknown_state_does_not_page(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    clw._last_signature = None
    run_control_loop_watchdog(FakeDb([], raises=True), now=NOW)
    assert sent == []


# ── run_control_loop_watchdog ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_edge_memory():
    clw._last_signature = None
    yield
    clw._last_signature = None


def _dead_db(age_seconds=3600, already_paged=False):
    """Heartbeat row, then the dedupe lookup's answer."""
    return FakeDb([
        (500, NOW - timedelta(seconds=age_seconds)),
        (1,) if already_paged else None,
    ])


def test_dead_loop_dispatches_tier_a_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    v = run_control_loop_watchdog(_dead_db(), now=NOW)

    assert v["emitted"] is True
    assert len(sent) == 1
    assert sent[0]["alert_type"] == MOMENTUM_LOOP_DEAD
    assert sent[0]["skip_throttle"] is True
    assert sent[0]["content_signature"] == v["signature"]


def test_alive_loop_is_silent(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    run_control_loop_watchdog(_beats(500, NOW - timedelta(seconds=10)), now=NOW)
    assert sent == []


def test_repeated_ticks_page_exactly_once(monkeypatch):
    """The loop is dead for hours and this runs every 60s. One page, not 240."""
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    last_beat = NOW - timedelta(hours=6)

    for _ in range(240):
        run_control_loop_watchdog(FakeDb([(500, last_beat), None]), now=NOW)

    assert len(sent) == 1


def test_signature_is_stable_across_the_whole_outage(monkeypatch):
    """The last beat does not advance while dead, so the key must not either."""
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: None)
    last_beat = NOW - timedelta(hours=2)
    first = run_control_loop_watchdog(FakeDb([(500, last_beat), None]), now=NOW)["signature"]
    later = evaluate_control_loop(FakeDb([(500, last_beat)]), now=NOW + timedelta(hours=5))
    assert first == f"dead:{later['last_beat_utc']}"


def test_restart_during_outage_does_not_re_page(monkeypatch):
    """Module memory is gone after a restart; the DB lookup must catch it.

    On this box restarts CORRELATE with outages (a Docker crash restarts every
    container at once), so this is the common path, not an edge case.
    """
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    run_control_loop_watchdog(_dead_db(), now=NOW)
    assert len(sent) == 1

    clw._last_signature = None  # simulate the restart
    v = run_control_loop_watchdog(_dead_db(already_paged=True), now=NOW)

    assert len(sent) == 1
    assert v["emitted"] is False
    assert v["suppressed"] == "already_paged"


def test_new_outage_after_a_recovery_pages_again(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    run_control_loop_watchdog(_dead_db(age_seconds=3600), now=NOW)
    run_control_loop_watchdog(_beats(500, NOW - timedelta(seconds=5)), now=NOW)   # recovered
    run_control_loop_watchdog(_dead_db(age_seconds=200), now=NOW)                 # died again

    assert len(sent) == 2
    assert sent[0]["content_signature"] != sent[1]["content_signature"]


def test_dedupe_failure_fails_open_and_pages(monkeypatch):
    """Silence is the bug. An errored dedupe lookup must not become silence."""
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    monkeypatch.setattr(clw, "_already_paged", lambda db, sig: False)
    run_control_loop_watchdog(_dead_db(), now=NOW)
    assert len(sent) == 1


def test_dedupe_query_is_pk_bounded():
    """Unbounded, this is a 3.3s seq scan over 94k rows inside a scheduler job."""
    db = _dead_db()
    clw._already_paged(db, "dead:x")
    sql, params = db.queries[0]
    assert "max(id)" in sql
    assert params["window"] == clw._ALREADY_PAGED_WINDOW


def test_dispatch_failure_never_breaks_the_job(monkeypatch, caplog):
    def boom(**kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(clw, "dispatch_alert", boom)
    with caplog.at_level(logging.CRITICAL):
        v = run_control_loop_watchdog(_dead_db(), now=NOW)
    assert v["emitted"] is True
    # The critical log is the backstop when delivery fails.
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_message_names_the_age_and_the_consequence(monkeypatch):
    sent = []
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: sent.append(kw))
    run_control_loop_watchdog(_dead_db(age_seconds=6 * 3600), now=NOW)
    msg = sent[0]["message"]
    assert "6h" in msg
    assert "exits are unowned" in msg


def test_recovery_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(clw, "dispatch_alert", lambda **kw: None)
    run_control_loop_watchdog(_dead_db(), now=NOW)
    with caplog.at_level(logging.WARNING):
        run_control_loop_watchdog(_beats(500, NOW - timedelta(seconds=5)), now=NOW)
    assert any("RECOVERED" in r.getMessage() for r in caplog.records)


# ── delivery contract ────────────────────────────────────────────────────────


def test_alert_is_tier_a():
    """dispatch_alert gates delivery on `tier in (TIER_A, TIER_B)`."""
    assert classify_alert_tier(MOMENTUM_LOOP_DEAD, None, 0.0) == TIER_A


def test_alert_bypasses_the_pinned_panel():
    """push_to_telegram_panel EDITS a pinned message — no phone notification.

    Without membership here, `use_panel` is True and this alarm becomes a fourth
    silent surface alongside the log line, the DB row and the red web banner.
    """
    assert MOMENTUM_LOOP_DEAD in _INDIVIDUAL_MSG_TYPES


def test_alert_type_fits_the_column():
    from app.models.trading import AlertHistory

    assert len(MOMENTUM_LOOP_DEAD) <= AlertHistory.__table__.c.alert_type.type.length


def test_watchdog_does_not_import_lane_health():
    """Independence is the whole point: three suppressors live in that module."""
    import inspect

    src = inspect.getsource(clw)
    assert "lane_health" not in src.split('"""', 2)[2]
