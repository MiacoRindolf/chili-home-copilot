"""Tests for f-add-pg-stat-snapshot-logger (Phase 9 of f-overnight-jumbo).

5-minute snapshot of pg_stat_activity to scripts/_pg_stat_log/<iso>.txt.
Forensic trail for the next leak.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent


def test_module_imports_cleanly():
    from app.services.trading.cron_jobs import pg_stat_snapshot
    assert callable(pg_stat_snapshot.run_pg_stat_snapshot)


def test_snapshot_writes_file_and_returns_metadata(tmp_path, monkeypatch):
    """Run the snapshot with a fake DB session; verify a file lands and
    the returned metadata includes path + rows_captured."""
    from app.services.trading.cron_jobs import pg_stat_snapshot

    # Redirect snapshot dir to a temp path.
    monkeypatch.setattr(pg_stat_snapshot, "_SNAPSHOT_DIR", tmp_path)

    fake_rows = [
        MagicMock(
            pid=11111, app="chili-brain-worker", state="idle",
            wait_event_type="", wait_event="", held_s=42,
            q="SELECT 1",
        ),
        MagicMock(
            pid=22222, app="chili-scheduler-worker",
            state="idle in transaction",
            wait_event_type="Client", wait_event="ClientRead",
            held_s=300, q="SELECT scan_patterns ...",
        ),
    ]
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchall.return_value = fake_rows

    result = pg_stat_snapshot.run_pg_stat_snapshot(fake_db)
    assert result["rows_captured"] == 2
    written = Path(result["path"])
    assert written.exists()
    content = written.read_text()
    assert "# pg_stat_activity snapshot" in content
    assert "pid=11111" in content
    assert "pid=22222" in content
    assert "held_s=300" in content


def test_snapshot_filter_uses_chili_prefix():
    """Source guard: query filter is `application_name LIKE 'chili%'`
    so foreign apps don't pollute the snapshot."""
    src = (REPO / "app/services/trading/cron_jobs/pg_stat_snapshot.py").read_text()
    assert "application_name LIKE 'chili%'" in src


def test_snapshot_top_30_by_held_s():
    """Source guard: ORDER BY held_s DESC + LIMIT 30 so the slowest
    sessions land first."""
    src = (REPO / "app/services/trading/cron_jobs/pg_stat_snapshot.py").read_text()
    assert "ORDER BY held_s DESC" in src
    assert "LIMIT 30" in src


def test_cron_registration_exists():
    """Source guard: 5-minute IntervalTrigger registration."""
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    assert 'id="pg_stat_snapshot"' in src
    assert "_run_pg_stat_snapshot_job" in src
    assert "IntervalTrigger(minutes=5)" in src


def test_wrapper_uses_with_sessionlocal():
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    idx = src.find("def _run_pg_stat_snapshot_job")
    assert idx > 0
    body = src[idx:idx + 1200]
    assert "with SessionLocal() as db:" in body


def test_module_uses_absolute_imports():
    src = (REPO / "app/services/trading/cron_jobs/pg_stat_snapshot.py").read_text()
    # No 4-dot relative imports.
    assert "from ....db" not in src
