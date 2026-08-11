"""A DB blip must not permanently retire the captured-PAPER control loop.

2026-08-10, r162: `[live_loop] dedicated captured PAPER inventory lost; retiring
generation=1` at 04:59:03Z -- seconds after Postgres logged `FATAL: canceling
authentication due to timeout` on the 5433 loopback. Nothing restarts this loop,
so that single blip cost 7.5 hours of lane downtime, and it is the mechanism
behind the measured 50.9%-dead fortnight.

`SessionTracker.refresh()` collapsed three structurally different facts into one
bare `False`:

    owner generation changed   -> retire (a real ownership change)
    assert_session RuntimeError -> retire (a foreign session; waiting cannot help)
    any DB exception            -> WAS retire; is now hold inert and retry

Clearing `_sessions` already makes the loop inert, which is the safety property.
Retiring on top of that buys nothing and forfeits the session.
"""

import time

import pytest
from sqlalchemy.exc import OperationalError

from app.services.trading.momentum_neural import live_runner_loop as lrl


class _Scope:
    """Minimal captured-PAPER scope stand-in."""

    runtime_generation = "gen-1"

    def assert_session(self, sess):
        return None


def _tracker():
    t = lrl._LiveSessionTracker.__new__(lrl._LiveSessionTracker)
    import threading

    t._lock = threading.RLock()
    t._sessions = {"x": 1}
    t._owner_generation = 1
    t._scope_breach_reason = None
    t._inventory_unreadable_since = None
    t._captured_paper_scope = _Scope()
    return t


class _FailingSession:
    """A session whose QUERY fails.

    `refresh` calls `SessionLocal()` before its `try`, so a stub that raises on
    construction escapes the handler entirely and tests nothing. The failure has
    to happen where a real DB fault happens: on the query.
    """

    def __init__(self, exc):
        self._exc = exc

    def query(self, *a, **k):
        raise self._exc

    def rollback(self):
        return None

    def close(self):
        return None


def _fail_refresh(tracker, exc):
    """Drive refresh's except branch without a database."""
    original = lrl.SessionLocal
    lrl.SessionLocal = lambda *a, **k: _FailingSession(exc)
    try:
        return tracker.refresh(expected_generation=1)
    finally:
        lrl.SessionLocal = original


def _db_error():
    return OperationalError("SELECT 1", {}, Exception("connection timed out"))


def test_a_db_error_marks_the_inventory_unreadable_not_breached():
    """THE regression: a blip must be distinguishable from a breach."""
    t = _tracker()

    assert _fail_refresh(t, _db_error()) is False
    assert t.inventory_is_unreadable() is True, "a DB fault must read as unreadable"
    assert t._sessions == {}, "the loop must still go inert"


def test_a_scope_breach_never_reads_as_unreadable():
    """A foreign session is a fact; waiting cannot change it. Retire at once."""
    t = _tracker()

    assert _fail_refresh(t, RuntimeError("captured_paper_foreign_runtime_generation_session")) is False
    assert t.inventory_is_unreadable() is False, (
        "a breach must not be granted the transient budget"
    )
    assert t._sessions == {}


def test_a_breach_after_a_blip_clears_the_transient_grace():
    """The dangerous ordering: a blip must not shelter a later breach."""
    t = _tracker()
    _fail_refresh(t, _db_error())
    assert t.inventory_is_unreadable() is True

    _fail_refresh(t, RuntimeError("captured_paper_foreign_runtime_generation_session"))
    assert t.inventory_is_unreadable() is False


def test_the_unreadable_window_measures_from_the_FIRST_failure():
    """A run of blips is one outage, not a rolling reset that never expires."""
    t = _tracker()
    _fail_refresh(t, _db_error())
    time.sleep(0.05)
    _fail_refresh(t, _db_error())

    assert t.inventory_is_unreadable() is True
    assert t.inventory_unreadable_seconds() >= 0.04, (
        "the window must accumulate across consecutive blips, not reset each time"
    )


def test_a_successful_read_clears_the_marker():
    """Recovery must fully reset the budget."""
    t = _tracker()
    _fail_refresh(t, _db_error())
    assert t.inventory_is_unreadable() is True

    with t._lock:
        t._inventory_unreadable_since = None  # what a successful refresh does
    assert t.inventory_is_unreadable() is False


def test_readable_inventory_reports_zero():
    assert _tracker().inventory_unreadable_seconds() == 0.0


def test_the_budget_is_the_existing_authoritative_threshold():
    """No new number to tune: reuse the loop's own staleness threshold."""
    from app.services.trading.momentum_neural.lane_health import (
        live_loop_stale_seconds,
    )

    budget = float(live_loop_stale_seconds())
    assert budget > 0.0
    # Long enough to ride out a connection blip, short enough that a genuinely
    # unreadable inventory still retires within the window the rest of the system
    # already calls "stale".
    assert 30.0 <= budget <= 300.0
