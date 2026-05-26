"""Tests for APScheduler job registration (trading brain operational jobs, not duplicate learning)."""

from __future__ import annotations

from app.config import settings

ROLE_ALL = "all"
HEARTBEAT_JOB_ID = "scheduler_worker_heartbeat"


def test_scheduler_excludes_web_pattern_research_job(monkeypatch):
    """Web pattern research runs inside run_learning_cycle; it must not be a separate cron job."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)
    monkeypatch.setattr(settings, "chili_alpha_portfolio_gate_enabled", True)
    monkeypatch.setattr(settings, "chili_alpha_portfolio_maintenance_enabled", True)
    try:
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
        assert "realized_stats_sync" in job_ids
        assert "alpha_portfolio_gate_maintenance" in job_ids
        assert "recert_queue_dispatch" in job_ids
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


def test_brain_learning_cycle_config_defaults():
    """Flags added to slim run_learning_cycle — defaults match product intent."""
    from app.config import settings

    assert getattr(settings, "brain_secondary_miners_on_cycle", None) is True
    assert int(getattr(settings, "brain_snapshot_top_tickers", 0)) == 1000
    assert getattr(settings, "brain_intraday_snapshots_enabled", None) is True
    assert int(getattr(settings, "brain_intraday_max_tickers", 0)) == 1000
    assert getattr(settings, "brain_market_snapshot_scheduler_enabled", None) is True
    assert getattr(settings, "chili_realized_sync_include_paper_dynamic", None) is True
    assert int(getattr(settings, "chili_realized_sync_interval_minutes", 0)) == 30
    assert getattr(settings, "brain_recert_queue_mode", None) == "shadow"
    assert int(getattr(settings, "brain_recert_queue_dispatch_interval_minutes", 0)) == 60


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
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


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
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


def test_scheduler_all_emits_heartbeat_by_default_unless_env_disables(monkeypatch):
    """CHILI_SCHEDULER_ROLE=all registers heartbeat unless env explicitly disables it."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)
    monkeypatch.delenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", raising=False)
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert HEARTBEAT_JOB_ID in job_ids
    finally:
        stop_scheduler()

    stop_scheduler()
    monkeypatch.setenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", "0")
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert HEARTBEAT_JOB_ID not in job_ids
    finally:
        stop_scheduler()

    stop_scheduler()
    monkeypatch.setenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", "1")
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert HEARTBEAT_JOB_ID in job_ids
        assert "broker_sync" in job_ids
    finally:
        stop_scheduler()
        monkeypatch.delenv("CHILI_SCHEDULER_EMIT_HEARTBEAT", raising=False)
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


def test_scheduler_worker_role_registers_heavy_without_legacy_breakout(monkeypatch):
    """CHILI_SCHEDULER_ROLE=worker keeps active CHILI jobs, not v1 breakout scanners."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "worker")
    try:
        start_scheduler()
        info = get_scheduler_info()
        job_ids = {j["id"] for j in info.get("jobs", [])}
        assert "crypto_breakout_scanner" not in job_ids
        assert "stock_breakout_scanner" not in job_ids
        assert "brain_market_snapshots" in job_ids
        assert HEARTBEAT_JOB_ID in job_ids
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


def test_brain_worker_default_interval_five_minutes():
    """scripts/brain_worker.py default idle sleep when queue empty (override with --interval)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "brain_worker.py").read_text(encoding="utf-8")
    assert "DEFAULT_CYCLE_INTERVAL = 5" in text
