"""Tests for f-tighten-db-watchdog-brain-worker-exemption (Phase 7).

Default brain-worker kill threshold lowered 1800s -> 600s. Was raised
to 1800s under FIX 32 to accommodate the legacy run_learning_cycle's
long-held sessions. Cycle is gated off
(CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0); the exemption is no longer
justified.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent


def test_brain_worker_default_threshold_is_now_600():
    """Source guard: the env-var default for the brain-worker kill
    threshold is 600s (down from 1800s). Pinning the value catches a
    future revert."""
    src = (REPO / "app/services/db_watchdog.py").read_text()
    # Find the bw_kill_sec assignment.
    idx = src.find("bw_kill_sec = _env_int")
    assert idx > 0
    # Look at the assignment line.
    line_end = src.find("\n", idx)
    line = src[idx:line_end]
    assert '"CHILI_DB_WATCHDOG_BRAIN_WORKER_KILL_SEC"' in line
    assert " 600)" in line, (
        f"brain-worker default kill threshold should be 600s, "
        f"got line: {line!r}"
    )


def test_brain_worker_held_700s_now_kills():
    """Behaviour guard: brain-worker pid held 700s should now KILL
    (was previously skipped under the 1800s exemption)."""
    from app.services import db_watchdog

    fake_row = MagicMock(
        pid=99999, dur_s=700, app="chili-brain-worker",
        q="SELECT scan_patterns ...",
    )

    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            m.fetchone.return_value = MagicMock(ok=True)
        else:
            m.fetchall.return_value = [fake_row]
        return m

    fake_sess = MagicMock()
    fake_sess.execute.side_effect = _execute_side

    # Default env -- no override.
    with patch("app.db.SessionLocal", return_value=fake_sess):
        # Clear any env override that previous tests might have set.
        old = os.environ.pop("CHILI_DB_WATCHDOG_BRAIN_WORKER_KILL_SEC", None)
        try:
            warned, killed = db_watchdog._poll_once()
        finally:
            if old is not None:
                os.environ["CHILI_DB_WATCHDOG_BRAIN_WORKER_KILL_SEC"] = old

    assert killed == 1, (
        f"brain-worker held 700s should now KILL under the 600s "
        f"default; got killed={killed}"
    )


def test_env_var_can_restore_legacy_1800():
    """Behaviour guard: operator can override back to 1800s via env
    var if a future cycle re-enable needs the long leash."""
    from app.services import db_watchdog

    fake_row = MagicMock(
        pid=99999, dur_s=700, app="chili-brain-worker", q="SELECT ...",
    )

    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            m.fetchone.return_value = MagicMock(ok=True)
        else:
            m.fetchall.return_value = [fake_row]
        return m

    fake_sess = MagicMock()
    fake_sess.execute.side_effect = _execute_side

    with patch("app.db.SessionLocal", return_value=fake_sess), \
         patch.dict(
             os.environ,
             {"CHILI_DB_WATCHDOG_BRAIN_WORKER_KILL_SEC": "1800"},
         ):
        warned, killed = db_watchdog._poll_once()

    # 700s < 1800s under the override -> warn-only.
    assert killed == 0
    assert warned == 1


def test_other_apps_still_use_standard_600():
    """Regression guard: non-brain-worker apps continue to use the
    standard 600s threshold (didn't accidentally break the
    per-app dispatch)."""
    from app.services import db_watchdog

    fake_row = MagicMock(
        pid=99999, dur_s=700, app="some-other-app", q="SELECT ...",
    )

    def _execute_side(*args, **kwargs):
        sql = str(args[0]) if args else ""
        m = MagicMock()
        if "pg_terminate_backend" in sql:
            m.fetchone.return_value = MagicMock(ok=True)
        else:
            m.fetchall.return_value = [fake_row]
        return m

    fake_sess = MagicMock()
    fake_sess.execute.side_effect = _execute_side

    with patch("app.db.SessionLocal", return_value=fake_sess):
        warned, killed = db_watchdog._poll_once()

    assert killed == 1  # 700s > 600s for non-exempt apps
