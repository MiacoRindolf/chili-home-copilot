"""Tests for APScheduler job registration (trading brain operational jobs, not duplicate learning)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import settings

ROLE_ALL = "all"
HEARTBEAT_JOB_ID = "scheduler_worker_heartbeat"


class _SettingsWithLearningStale:
    learning_cycle_stale_seconds = 3600


class _FakeBatchSession:
    def __init__(self, name: str):
        self.name = name
        self.rollbacks = 0
        self.commits = 0
        self.invalidates = 0
        self.closed = False

    def rollback(self) -> None:
        self.rollbacks += 1

    def invalidate(self) -> None:
        self.invalidates += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _FakeIdQuery:
    def __init__(self, ids: list[int]):
        self.ids = ids

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return [(id_,) for id_ in self.ids]


class _FakeIdSession(_FakeBatchSession):
    def __init__(self, name: str, ids: list[int]):
        super().__init__(name)
        self.ids = ids

    def query(self, *_args, **_kwargs):
        return _FakeIdQuery(self.ids)


class _FakeGetQuery:
    def __init__(self, obj):
        self.obj = obj

    def get(self, _id):
        return self.obj


class _FakeGetSession(_FakeBatchSession):
    def __init__(self, name: str, obj):
        super().__init__(name)
        self.obj = obj

    def query(self, *_args, **_kwargs):
        return _FakeGetQuery(self.obj)


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


def test_scheduler_failure_recording_falls_back_after_broken_primary_session():
    from app.services.trading_scheduler import _record_batch_job_failure_resilient

    primary = _FakeBatchSession("primary")
    fresh = _FakeBatchSession("fresh")
    calls: list[tuple[str, str, bool, str]] = []

    def _session_factory():
        return fresh

    def _finish_fn(db, job_id, *, ok, error, **_kwargs):
        calls.append((db.name, job_id, ok, error))
        if db is primary:
            raise RuntimeError("pending rollback")

    mode = _record_batch_job_failure_resilient(
        primary,
        session_factory=_session_factory,
        finish_fn=_finish_fn,
        job_id="job-1",
        error=RuntimeError("server closed the connection unexpectedly"),
        log_label="unit_job",
    )

    assert mode == "fresh_session"
    assert calls == [
        ("primary", "job-1", False, "server closed the connection unexpectedly"),
        ("fresh", "job-1", False, "server closed the connection unexpectedly"),
    ]
    assert primary.rollbacks >= 2
    assert primary.invalidates == 1
    assert fresh.commits == 1
    assert fresh.closed is True


def test_daily_market_scan_failure_uses_fresh_session_and_surfaces_to_guard(monkeypatch):
    import app.db as app_db
    from app.services import trading_scheduler
    from app.services.trading import brain_batch_job_log, scanner

    primary = _FakeBatchSession("primary")
    fresh = _FakeBatchSession("fresh")
    sessions = [primary, fresh]
    finish_calls: list[tuple[str, str, bool, str | None]] = []
    captured: dict[str, object] = {}
    cleared: list[bool] = []

    monkeypatch.setattr(settings, "brain_daily_market_scan_scheduler_enabled", True)
    monkeypatch.setattr(settings, "brain_default_user_id", 7)
    monkeypatch.setattr(app_db, "SessionLocal", lambda: sessions.pop(0))
    monkeypatch.setattr(
        brain_batch_job_log,
        "brain_batch_job_begin",
        lambda db, job_type, user_id=None: "scan-job-1",
    )

    def _finish_fn(db, job_id, *, ok, error=None, **_kwargs):
        finish_calls.append((db.name, job_id, ok, error))
        if db is primary:
            raise RuntimeError("pending rollback")

    def _raise_scan(*_args, **_kwargs):
        raise RuntimeError("server closed the connection unexpectedly")

    def _guard(job_id, fn):
        captured["job_id"] = job_id
        try:
            fn()
        except Exception as exc:  # matches run_scheduler_job_guarded visibility path
            captured["exc"] = exc

    monkeypatch.setattr(brain_batch_job_log, "brain_batch_job_finish", _finish_fn)
    monkeypatch.setattr(scanner, "run_full_market_scan", _raise_scan)
    monkeypatch.setattr(scanner, "clear_scanner_caches", lambda: cleared.append(True))
    monkeypatch.setattr(trading_scheduler, "run_scheduler_job_guarded", _guard)

    trading_scheduler._run_daily_market_scan_job()

    assert captured["job_id"] == "daily_market_scan"
    assert "server closed" in str(captured["exc"])
    assert finish_calls == [
        ("primary", "scan-job-1", False, "server closed the connection unexpectedly"),
        ("fresh", "scan-job-1", False, "server closed the connection unexpectedly"),
    ]
    assert primary.rollbacks >= 2
    assert primary.invalidates == 1
    assert primary.closed is True
    assert fresh.commits == 1
    assert fresh.closed is True
    assert cleared == [True]


def test_check_paper_exits_isolated_closes_listing_session_before_trade_work(monkeypatch):
    from app.services.trading import paper_trading

    list_session = _FakeIdSession("list", [101, 102])
    trade_sessions = [
        _FakeBatchSession("trade-101"),
        _FakeBatchSession("trade-102"),
    ]
    sessions = [list_session, *trade_sessions]
    calls: list[tuple[str, set[int] | None]] = []

    def _factory():
        return sessions.pop(0)

    def _fake_check(db, user_id=None, *, skip_trade_ids=None, trade_ids=None):
        assert list_session.closed is True
        calls.append((db.name, trade_ids))
        return {
            "checked": 1,
            "closed": 1 if trade_ids == {101} else 0,
            "trailing_updated": 0,
        }

    monkeypatch.setattr(paper_trading, "check_paper_exits", _fake_check)

    result = paper_trading.check_paper_exits_isolated(_factory)

    assert result == {"checked": 2, "closed": 1, "trailing_updated": 0}
    assert calls == [("trade-101", {101}), ("trade-102", {102})]
    assert list_session.rollbacks == 1
    assert all(s.closed for s in [list_session, *trade_sessions])


def test_run_exit_engine_isolated_closes_listing_session_before_position_work(monkeypatch):
    from app.services.trading import live_exit_engine

    list_session = _FakeIdSession("list", [201, 202])
    position_sessions = [
        _FakeBatchSession("position-201"),
        _FakeBatchSession("position-202"),
    ]
    sessions = [list_session, *position_sessions]
    calls: list[tuple[str, set[int] | None]] = []

    def _factory():
        return sessions.pop(0)

    def _fake_run(db, user_id=None, *, position_ids=None):
        assert list_session.closed is True
        calls.append((db.name, position_ids))
        action = {"position_id": next(iter(position_ids)), "action": "partial"}
        return {
            "ok": True,
            "evaluated": 1,
            "actions": [],
            "partial_actions": [action],
            "all": [action],
            "skipped_options": 0,
        }

    monkeypatch.setattr(live_exit_engine, "run_exit_engine", _fake_run)

    result = live_exit_engine.run_exit_engine_isolated(_factory)

    assert result["evaluated"] == 2
    assert [a["position_id"] for a in result["partial_actions"]] == [201, 202]
    assert calls == [("position-201", {201}), ("position-202", {202})]
    assert list_session.rollbacks == 1
    assert all(s.closed for s in [list_session, *position_sessions])


def test_paper_trade_check_job_uses_isolated_helpers_and_bounded_partial_sessions(monkeypatch):
    import app.db as app_db
    from app.services import trading_scheduler
    from app.services.trading import live_exit_engine, paper_trading

    partial_position = SimpleNamespace(id=303, ticker="AAOX", partial_taken=False)
    partial_session = _FakeGetSession("partial-303", partial_position)
    sessions = [partial_session]
    helper_factories: list[object] = []
    partial_calls: list[tuple[str, int, float, float]] = []

    def _session_factory():
        return sessions.pop(0)

    def _paper_helper(factory, *_args, **_kwargs):
        helper_factories.append(factory)
        return {"checked": 2, "closed": 0, "trailing_updated": 0}

    def _exit_helper(factory, *_args, **_kwargs):
        helper_factories.append(factory)
        return {
            "ok": True,
            "evaluated": 1,
            "actions": [],
            "partial_actions": [
                {
                    "position_id": 303,
                    "partial_close_fraction": 0.25,
                    "current_price": 12.5,
                    "r_multiple": 1.1,
                }
            ],
            "all": [],
            "skipped_options": 0,
        }

    def _place_partial(db, pos, fraction, *, current_price=None):
        partial_calls.append((db.name, pos.id, fraction, current_price))
        return {"ok": True, "quantity": 1.0, "price": current_price}

    monkeypatch.setattr(app_db, "SessionLocal", _session_factory)
    monkeypatch.setattr(paper_trading, "check_paper_exits_isolated", _paper_helper)
    monkeypatch.setattr(live_exit_engine, "run_exit_engine_isolated", _exit_helper)
    monkeypatch.setattr(paper_trading, "place_partial_close", _place_partial)
    monkeypatch.setattr(trading_scheduler, "run_scheduler_job_guarded", lambda _id, fn: fn())

    trading_scheduler._run_paper_trade_check_job()

    assert helper_factories == [_session_factory, _session_factory]
    assert partial_calls == [("partial-303", 303, 0.25, 12.5)]
    assert partial_session.rollbacks == 1
    assert partial_session.closed is True


def test_price_monitor_job_uses_short_sessions_per_phase(monkeypatch):
    import app.db as app_db
    from app.services import trading_scheduler
    from app.services.trading import alerts

    user_listing = _FakeIdSession("user_listing", [1, 2])
    user_one = _FakeBatchSession("user_one")
    user_two = _FakeBatchSession("user_two")
    pattern_listing = _FakeIdSession("pattern_listing", ["AAA", "BBB"])
    sessions = [user_listing, user_one, user_two, pattern_listing]
    made_sessions = list(sessions)
    monitor_calls: list[tuple[str, int | None]] = []
    triggered: list[tuple[list[str], str]] = []

    def _session_factory():
        return sessions.pop(0)

    def _run_price_monitor(db, *, user_id):
        monitor_calls.append((db.name, user_id))
        return {"alerted_tickers": [f"T{user_id}"]}

    monkeypatch.setattr(app_db, "SessionLocal", _session_factory)
    monkeypatch.setattr(alerts, "run_price_monitor", _run_price_monitor)
    monkeypatch.setattr(
        trading_scheduler,
        "trigger_pattern_monitor_for_tickers",
        lambda tickers, reason: triggered.append((tickers, reason)),
    )
    monkeypatch.setattr(trading_scheduler, "run_scheduler_job_guarded", lambda _id, fn: fn())

    trading_scheduler._run_price_monitor_job()

    assert sessions == []
    assert monitor_calls == [("user_one", 1), ("user_two", 2)]
    assert triggered == [(["AAA", "BBB"], "price_monitor")]
    for session in made_sessions:
        assert session.rollbacks == 1
        assert session.closed is True


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


def test_scheduler_market_snapshot_only_role_is_dedicated_lane(monkeypatch):
    """The minimal trading stack has a dedicated market snapshot producer."""
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "market_snapshot_only")
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert "brain_market_snapshots" in job_ids
        assert "cash_deployment_work_producer" in job_ids
        assert "brain_batch_reconciler" in job_ids
        assert HEARTBEAT_JOB_ID in job_ids
        assert "daily_market_scan" not in job_ids
        assert "broker_sync" not in job_ids
        assert "autotrader_tick" not in job_ids
        assert "price_monitor" not in job_ids
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


def test_market_snapshots_defer_for_fresh_learning_status():
    from app.services.trading_scheduler import _learning_status_blocks_market_snapshots

    status = {
        "running": True,
        "started_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    }

    blocks, reason = _learning_status_blocks_market_snapshots(
        status,
        _SettingsWithLearningStale,
    )

    assert blocks is True
    assert reason.startswith("learning_running_age_s=")


def test_market_snapshots_ignore_stale_learning_status():
    from app.services.trading_scheduler import _learning_status_blocks_market_snapshots

    status = {
        "running": True,
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }

    blocks, reason = _learning_status_blocks_market_snapshots(
        status,
        _SettingsWithLearningStale,
    )

    assert blocks is False
    assert reason.startswith("stale_learning_running_age_s=")


def test_brain_worker_default_interval_five_minutes():
    """scripts/brain_worker.py default idle sleep when queue empty (override with --interval)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "brain_worker.py").read_text(encoding="utf-8")
    assert "DEFAULT_CYCLE_INTERVAL = 5" in text
