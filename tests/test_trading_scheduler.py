"""Tests for APScheduler job registration (trading brain operational jobs, not duplicate learning)."""

from __future__ import annotations

from app.config import settings


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
    assert "pattern_imminent_scanner" in job_ids
    assert "daily_prescreen" in job_ids
    assert "daily_market_scan" in job_ids
    assert "brain_market_snapshots" in job_ids


def test_brain_learning_cycle_config_defaults():
    """Flags added to slim run_learning_cycle — defaults match product intent."""
    from app.config import settings

    assert getattr(settings, "brain_secondary_miners_on_cycle", None) is True
    assert int(getattr(settings, "brain_snapshot_top_tickers", 0)) == 1000
    assert getattr(settings, "brain_intraday_snapshots_enabled", None) is True
    assert int(getattr(settings, "brain_intraday_max_tickers", 0)) == 1000
    assert getattr(settings, "brain_market_snapshot_scheduler_enabled", None) is True


def test_scheduler_web_role_omits_crypto_breakout(monkeypatch):
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "web")
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert "broker_sync" in job_ids
        assert "crypto_breakout_scanner" not in job_ids
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", "all")


def test_scheduler_none_role_disables_apscheduler(monkeypatch):
    """CHILI_SCHEDULER_ROLE=none — no BackgroundScheduler (Docker ``chili`` service)."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "none")
    try:
        start_scheduler()
        info = get_scheduler_info()
        assert info.get("running") is False
        assert info.get("jobs") == []
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", "all")


def test_scheduler_all_emits_heartbeat_only_when_env(monkeypatch):
    """CHILI_SCHEDULER_ROLE=all registers heartbeat only if CHILI_SCHEDULER_EMIT_HEARTBEAT is set."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "all")
    monkeypatch.delenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", raising=False)
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert "scheduler_worker_heartbeat" not in job_ids
    finally:
        stop_scheduler()

    stop_scheduler()
    monkeypatch.setenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", "1")
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert "scheduler_worker_heartbeat" in job_ids
        assert "broker_sync" in job_ids
    finally:
        stop_scheduler()
        monkeypatch.delenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", raising=False)
        monkeypatch.setattr(settings, "chili_scheduler_role", "all")


def test_scheduler_worker_role_registers_heavy_not_broker(monkeypatch):
    """CHILI_SCHEDULER_ROLE=worker should omit web-light jobs (e.g. broker_sync)."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "worker")
    try:
        start_scheduler()
        info = get_scheduler_info()
        job_ids = {j["id"] for j in info.get("jobs", [])}
        assert "crypto_breakout_scanner" in job_ids
        assert "brain_market_snapshots" in job_ids
        assert "scheduler_worker_heartbeat" in job_ids
        assert "broker_sync" not in job_ids
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", "all")


def test_brain_worker_default_interval_five_minutes():
    """scripts/brain_worker.py default idle sleep when queue empty (override with --interval)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "brain_worker.py").read_text(encoding="utf-8")
    assert "DEFAULT_CYCLE_INTERVAL = 5" in text
