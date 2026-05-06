"""Tests for f-handler-validate-evolve (Phase 5 of f-overnight-jumbo).

Wraps the legacy run_learning_cycle's validate_and_evolve step into a
6-hourly cron. The function mines 500 tickers of OHLCV per run --
heavy, broad-market work -- so cron is the right shape, not
event-handler.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_module_imports_cleanly():
    from app.services.trading.cron_jobs import validate_evolve
    assert callable(validate_evolve.run_validate_evolve)


def test_cron_module_uses_absolute_imports():
    src = (REPO / "app/services/trading/cron_jobs/validate_evolve.py").read_text()
    assert "from app.services.trading.learning import" in src


def test_cron_registration_exists():
    """Source guard: APScheduler add_job for validate_evolve exists with
    a 6-hour cron trigger."""
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    assert 'id="validate_evolve"' in src
    assert "_run_validate_evolve_job" in src
    assert 'hour="*/6"' in src, (
        "validate_evolve cron must run every 6 hours per the brief"
    )


def test_wrapper_uses_with_sessionlocal():
    """Source guard: the wrapper opens SessionLocal via `with` so the
    session always closes (no leak even if validate_and_evolve raises)."""
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    idx = src.find("def _run_validate_evolve_job")
    assert idx > 0
    body = src[idx:idx + 1500]
    assert "with SessionLocal() as db:" in body, (
        "wrapper must use `with SessionLocal() as db:` for safe close"
    )


def test_cron_module_passes_through_to_learning():
    """Pinning: run_validate_evolve is a thin wrapper that calls
    learning.validate_and_evolve. Catches a future refactor that adds
    extra logic (which would belong in learning.py, not the cron)."""
    src = (REPO / "app/services/trading/cron_jobs/validate_evolve.py").read_text()
    idx = src.find("def run_validate_evolve(")
    assert idx > 0
    body = src[idx:idx + 800]
    assert "validate_and_evolve" in body, (
        "wrapper must call learning.validate_and_evolve"
    )
