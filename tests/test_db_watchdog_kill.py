"""Tests for f-fix-db-watchdog-kill-action (Phase 6 of f-overnight-cleanup).

The watchdog already had pg_terminate_backend wired (per pre-existing FIX
5/32 pre-existing code). This phase confirmed-implemented and improved
the log surface so the kill outcome (success/permission-failure/
exception) is unambiguous in production logs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Synthetic 700s held idle-in-tx triggers KILLED log
# ---------------------------------------------------------------------------

def test_synthetic_kill_logs_killed_line(caplog):
    """When _poll_once sees a non-exempt app held > 600s, it calls
    pg_terminate_backend and logs '[db_watchdog] KILLED'."""
    import logging
    from app.services import db_watchdog

    fake_row = MagicMock(pid=99999, dur_s=700, app="some-other-app", q="SELECT ...")
    fake_kill = MagicMock(ok=True)

    fake_sess = MagicMock()
    fake_sess.execute.return_value.fetchall.return_value = [fake_row]

    # Second .execute call is the pg_terminate_backend; chain it to fetchone.
    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            m.fetchone.return_value = fake_kill
        else:
            m.fetchall.return_value = [fake_row]
        return m

    fake_sess.execute.side_effect = _execute_side

    with patch("app.db.SessionLocal", return_value=fake_sess), \
         caplog.at_level(logging.WARNING, logger="app.services.db_watchdog"):
        warned, killed = db_watchdog._poll_once()

    assert killed == 1
    assert any(
        "[db_watchdog] KILLED" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# 2. Permission failure (pg_terminate_backend returns FALSE) logs ERROR
# ---------------------------------------------------------------------------

def test_kill_failure_logs_error(caplog):
    """When pg_terminate_backend returns FALSE (permission denied or
    pid already terminated), log an ERROR with KILL-FAILED prefix."""
    import logging
    from app.services import db_watchdog

    fake_row = MagicMock(pid=99999, dur_s=700, app="some-other-app", q="SELECT ...")
    fake_kill = MagicMock(ok=False)

    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            m.fetchone.return_value = fake_kill
        else:
            m.fetchall.return_value = [fake_row]
        return m

    fake_sess = MagicMock()
    fake_sess.execute.side_effect = _execute_side

    with patch("app.db.SessionLocal", return_value=fake_sess), \
         caplog.at_level(logging.ERROR, logger="app.services.db_watchdog"):
        warned, killed = db_watchdog._poll_once()

    assert killed == 0
    assert any(
        "KILL-FAILED" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# 3. Brain-worker exemption (1800s threshold) — under threshold, just warns
# ---------------------------------------------------------------------------

def test_brain_worker_exemption_under_threshold_just_warns(caplog):
    """A chili-brain-worker pid held 800s should warn (still under 1800s
    exempt threshold), NOT kill. Pre-fix this was the source of the
    "claimed kill at 600s but didn't fire" confusion."""
    import logging
    from app.services import db_watchdog

    fake_row = MagicMock(pid=99999, dur_s=800, app="chili-brain-worker", q="SELECT ...")

    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            # Should NOT be called.
            m.fetchone.return_value = MagicMock(ok=True)
        else:
            m.fetchall.return_value = [fake_row]
        return m

    fake_sess = MagicMock()
    fake_sess.execute.side_effect = _execute_side

    with patch("app.db.SessionLocal", return_value=fake_sess), \
         caplog.at_level(logging.WARNING, logger="app.services.db_watchdog"):
        warned, killed = db_watchdog._poll_once()

    # 800s > 600s base, but < 1800s brain-worker exemption -> warn-only.
    assert killed == 0
    assert warned == 1
    assert not any(
        "KILLED" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# 4. Exception during pg_terminate_backend logs KILL-EXCEPTION
# ---------------------------------------------------------------------------

def test_kill_exception_logs_kill_exception(caplog):
    import logging
    from app.services import db_watchdog

    fake_row = MagicMock(pid=99999, dur_s=700, app="some-other-app", q="SELECT ...")

    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            raise RuntimeError("simulated pg failure")
        m.fetchall.return_value = [fake_row]
        return m

    fake_sess = MagicMock()
    fake_sess.execute.side_effect = _execute_side

    with patch("app.db.SessionLocal", return_value=fake_sess), \
         caplog.at_level(logging.ERROR, logger="app.services.db_watchdog"):
        warned, killed = db_watchdog._poll_once()

    assert killed == 0
    assert any(
        "KILL-EXCEPTION" in rec.message for rec in caplog.records
    )
