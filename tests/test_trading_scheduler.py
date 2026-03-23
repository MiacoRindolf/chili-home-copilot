"""Tests for APScheduler job registration (trading brain operational jobs, not duplicate learning)."""

from __future__ import annotations


def test_scheduler_excludes_web_pattern_research_job():
    """Web pattern research runs inside run_learning_cycle; it must not be a separate cron job."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler

    start_scheduler()
    info = get_scheduler_info()
    assert info.get("running") is True, "scheduler should be running after app/client startup"
    job_ids = {j["id"] for j in info.get("jobs", [])}
    assert "web_pattern_research" not in job_ids
    # Regression anchors: operational jobs we still expect
    assert "broker_sync" in job_ids
    assert "price_monitor" in job_ids


def test_brain_learning_cycle_config_defaults():
    """Flags added to slim run_learning_cycle — defaults match product intent."""
    from app.config import settings

    assert getattr(settings, "brain_insight_backtest_on_cycle", None) is False
    assert getattr(settings, "brain_secondary_miners_on_cycle", None) is True


def test_brain_worker_default_interval_five_minutes():
    """scripts/brain_worker.py default idle sleep when queue empty (override with --interval)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "brain_worker.py").read_text(encoding="utf-8")
    assert "DEFAULT_CYCLE_INTERVAL = 5" in text
