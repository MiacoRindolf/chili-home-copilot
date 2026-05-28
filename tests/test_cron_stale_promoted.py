"""Tests for f-cron-stale-promoted (Phase 4 of f-overnight-jumbo).

Catches the demote-coverage gap: per-trade-close demote handler only
fires on active patterns. Patterns whose trades stopped firing
entirely sit at lifecycle_stage='promoted' indefinitely. Weekly cron
sweep catches them.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_module_imports_cleanly():
    from app.services.trading.cron_jobs import stale_promoted_sweep
    assert callable(stale_promoted_sweep.run_stale_promoted_sweep)


class _FakeQuery:
    def __init__(self, rows: list[tuple[int | None, datetime | None]]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.group_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def group_by(self, *args: object) -> "_FakeQuery":
        self.group_by_calls += 1
        return self

    def all(self) -> list[tuple[int | None, datetime | None]]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[tuple[int | None, datetime | None]]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_latest_exit_dates_by_pattern_batches_trade_lookup():
    from app.services.trading.cron_jobs.stale_promoted_sweep import (
        _latest_exit_dates_by_pattern,
    )

    latest = datetime(2026, 5, 28, 12, 0)
    older = datetime(2026, 5, 20, 12, 0)
    db = _FakeSession([(2, latest), (1, older), (None, latest)])

    result = _latest_exit_dates_by_pattern(db, [2, 1, 2])

    assert result == {1: older, 2: latest}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1
    assert db.last_query.group_by_calls == 1


def test_latest_exit_dates_by_pattern_skips_empty_lookup():
    from app.services.trading.cron_jobs.stale_promoted_sweep import (
        _latest_exit_dates_by_pattern,
    )

    db = _FakeSession([])

    assert _latest_exit_dates_by_pattern(db, []) == {}
    assert db.query_calls == 0


def test_uses_with_sessionlocal_in_wrapper():
    """Source guard: the scheduler wrapper opens SessionLocal via
    `with` so the session always closes (no leak)."""
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    idx = src.find("def _run_stale_promoted_sweep_job")
    assert idx > 0
    body = src[idx:idx + 1500]
    assert "with SessionLocal() as db:" in body, (
        "wrapper must use `with SessionLocal() as db:` for safe session close"
    )


def test_cron_registration_exists():
    """Source guard: the APScheduler add_job call for the sweep is
    registered with a weekly cron trigger."""
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    assert 'id="stale_promoted_sweep"' in src
    assert "_run_stale_promoted_sweep_job" in src
    assert 'day_of_week="sun"' in src


def test_sweep_module_uses_absolute_imports():
    """Source guard: the sweep module imports via absolute paths
    (the depth-5 cron_jobs/ subdirectory has the same import-depth
    risk as handlers/)."""
    src = (REPO / "app/services/trading/cron_jobs/stale_promoted_sweep.py").read_text()
    assert "from app.models.trading import" in src
    assert "from app.services.trading.realized_ev_gate import" in src


def test_sweep_returns_three_count_keys():
    """Contract guard: the sweep returns the standard
    {patterns_checked, patterns_skipped_recent, patterns_demoted}
    shape so the scheduler log can show progress."""
    src = (REPO / "app/services/trading/cron_jobs/stale_promoted_sweep.py").read_text()
    for key in (
        "patterns_checked",
        "patterns_skipped_recent",
        "patterns_demoted",
    ):
        assert f'"{key}"' in src, (
            f"sweep result must include the {key!r} key"
        )


def test_sweep_skips_patterns_with_recent_trades():
    """Source guard: patterns with a trade in the last 7 days are
    skipped (handler covers them). Pin the cutoff + comparison logic."""
    src = (REPO / "app/services/trading/cron_jobs/stale_promoted_sweep.py").read_text()
    assert "timedelta(days=7)" in src, (
        "stale cutoff must be 7 days (per the design)"
    )
    assert "skipped_recent" in src
