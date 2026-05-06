"""Tests for f-cron-stale-promoted (Phase 4 of f-overnight-jumbo).

Catches the demote-coverage gap: per-trade-close demote handler only
fires on active patterns. Patterns whose trades stopped firing
entirely sit at lifecycle_stage='promoted' indefinitely. Weekly cron
sweep catches them.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_module_imports_cleanly():
    from app.services.trading.cron_jobs import stale_promoted_sweep
    assert callable(stale_promoted_sweep.run_stale_promoted_sweep)


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
