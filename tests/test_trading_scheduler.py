"""Tests for APScheduler job registration (trading brain operational jobs, not duplicate learning)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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


def test_memory_watcher_interval_setting_drives_scheduler_floor(monkeypatch):
    from app.config import Settings
    from app.services.trading_scheduler import _memory_watcher_interval_seconds

    monkeypatch.setenv("CHILI_MEMORY_WATCHER_INTERVAL_S", "75")
    settings_obj = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings_obj.chili_memory_watcher_interval_s == 75
    assert _memory_watcher_interval_seconds(settings_obj) == 75

    monkeypatch.setenv("CHILI_MEMORY_WATCHER_INTERVAL_S", "15")
    settings_obj = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings_obj.chili_memory_watcher_interval_s == 15
    assert _memory_watcher_interval_seconds(settings_obj) == 60


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


def test_momentum_exec_prewarm_registers_viability_refresh_jobs(monkeypatch):
    from app.services import trading_scheduler

    class _FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, **kwargs):
            self.jobs.append((fn, kwargs))

    fake = _FakeScheduler()
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_event_select_primary_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_tape_delta_min_seconds", 5.0, raising=False)

    trading_scheduler._register_momentum_selection_prewarm_jobs(fake, settings)

    job_ids = {kwargs["id"] for _fn, kwargs in fake.jobs}
    assert {"crypto_viability_refresh", "equity_viability_refresh", "tape_delta_ignite"} <= job_ids
    intervals = {kwargs["id"]: kwargs["trigger"].interval.total_seconds() for _fn, kwargs in fake.jobs}
    assert intervals["crypto_viability_refresh"] == 300.0
    assert intervals["equity_viability_refresh"] == 300.0
    assert intervals["tape_delta_ignite"] == 5.0


def test_viability_bridge_rolls_back_before_direct_tick(monkeypatch):
    from app.services import trading_scheduler
    from app.services.trading.momentum_neural import nbbo_tape, pipeline

    db = _FakeBatchSession("bridge")
    calls = []

    def _fake_tick(seen_db, *, meta):
        assert seen_db is db
        assert seen_db.rollbacks >= 1
        calls.append(meta)

    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", False, raising=False)
    monkeypatch.setattr(nbbo_tape, "tape_running_up_symbols", lambda _db: [])
    monkeypatch.setattr(trading_scheduler, "_active_equity_session_symbols", lambda _db: [])
    monkeypatch.setattr(pipeline, "run_momentum_neural_tick", _fake_tick)

    trading_scheduler._bridge_scanner_to_viability(
        db,
        [{"ticker": "MOVE", "daily_change_pct": 35.0, "dollar_volume": 10_000_000}],
        source="equity_viability_refresh",
    )

    assert [call["tickers"] for call in calls] == [["MOVE"]]
    assert db.rollbacks == 1
    assert db.commits == 1


def test_viability_bridge_rolls_back_before_each_chunk_tick(monkeypatch):
    from app.services import trading_scheduler
    from app.services.trading.momentum_neural import nbbo_tape, pipeline

    db = _FakeBatchSession("bridge")
    rollback_counts = []

    def _fake_tick(seen_db, *, meta):
        assert seen_db is db
        rollback_counts.append(seen_db.rollbacks)

    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", True, raising=False)
    monkeypatch.setattr(trading_scheduler, "_VIABILITY_BRIDGE_CHUNK", 2)
    monkeypatch.setattr(nbbo_tape, "tape_running_up_symbols", lambda _db: [])
    monkeypatch.setattr(trading_scheduler, "_active_equity_session_symbols", lambda _db: [])
    monkeypatch.setattr(pipeline, "run_momentum_neural_tick", _fake_tick)

    trading_scheduler._bridge_scanner_to_viability(
        db,
        [
            {"ticker": f"MOVE{i}", "daily_change_pct": 30.0 + i, "dollar_volume": 10_000_000 + i}
            for i in range(5)
        ],
        source="equity_viability_refresh",
    )

    assert rollback_counts == [1, 2, 3]
    assert db.commits == 3


def test_viability_bridge_does_not_commit_when_tick_persistence_fails(monkeypatch, caplog):
    from app.services import trading_scheduler
    from app.services.trading.momentum_neural import nbbo_tape, pipeline

    db = _FakeBatchSession("bridge")

    def _fake_tick(seen_db, *, meta):
        assert seen_db is db
        assert seen_db.rollbacks >= 1
        return {"ok": True, "persistence_ok": False}

    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", False, raising=False)
    monkeypatch.setattr(nbbo_tape, "tape_running_up_symbols", lambda _db: [])
    monkeypatch.setattr(trading_scheduler, "_active_equity_session_symbols", lambda _db: [])
    monkeypatch.setattr(pipeline, "run_momentum_neural_tick", _fake_tick)
    caplog.set_level("WARNING", logger="app.services.trading_scheduler")

    trading_scheduler._bridge_scanner_to_viability(
        db,
        [{"ticker": "MOVE", "daily_change_pct": 35.0, "dollar_volume": 10_000_000}],
        source="equity_viability_refresh",
    )

    assert db.commits == 0
    assert db.rollbacks == 2
    assert "persistence_ok=False" in caplog.text


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
        assert "neural_mesh_drain" in job_ids
        assert "cash_deployment_work_producer" in job_ids
        assert "brain_batch_reconciler" in job_ids
        assert HEARTBEAT_JOB_ID in job_ids
        assert "daily_market_scan" not in job_ids
        assert "broker_sync" not in job_ids
        assert "auto_trader_tick" not in job_ids
        assert "auto_trader_monitor" not in job_ids
        assert "momentum_live_runner_batch" not in job_ids
        assert "stuck_order_watchdog" not in job_ids
        assert "momentum_scanner" not in job_ids
        assert "pattern_imminent_scanner" not in job_ids
        assert "price_monitor" not in job_ids
    finally:
        stop_scheduler()
        monkeypatch.setattr(settings, "chili_scheduler_role", ROLE_ALL)


def test_momentum_exec_event_loop_is_the_only_live_driver(monkeypatch):
    from app.services.trading.momentum_neural import live_runner_loop, tape_ws_recorder
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    started: list[str] = []
    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_loop_enabled", True)
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_scheduler_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_nbbo_tape_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_alpaca_orphan_reconcile_enabled", False)
    monkeypatch.setattr(live_runner_loop, "start_live_runner_loop", lambda: started.append("loop"))
    monkeypatch.setattr(tape_ws_recorder, "start_tape_ws_recorder", lambda: None)
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert started == ["loop"]
        assert "momentum_live_runner_batch" not in job_ids
        assert "momentum_auto_arm_live" not in job_ids
        assert "lane_health_check" in job_ids
    finally:
        stop_scheduler()


def test_momentum_exec_dual_live_driver_config_fails_closed(monkeypatch):
    from app.services.trading.momentum_neural import live_runner_loop
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    started: list[str] = []
    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_loop_enabled", True)
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_scheduler_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_nbbo_tape_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_alpaca_orphan_reconcile_enabled", False)
    monkeypatch.setattr(live_runner_loop, "start_live_runner_loop", lambda: started.append("loop"))
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert started == []
        assert "momentum_live_runner_batch" not in job_ids
        assert "momentum_auto_arm_live" not in job_ids
        # Invalid master-on ownership must stay visible instead of suppressing the
        # one job that reports the misconfiguration.
        assert "lane_health_check" in job_ids
    finally:
        stop_scheduler()


@pytest.mark.parametrize(
    ("batch_on", "loop_on", "bus_on"),
    (
        (False, False, True),
        (False, True, False),
    ),
)
def test_master_on_invalid_live_driver_still_registers_lane_health(
    monkeypatch,
    batch_on,
    loop_on,
    bus_on,
):
    from app.services.trading.momentum_neural import live_runner_loop
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    started = []
    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(
        settings, "chili_momentum_live_runner_scheduler_enabled", batch_on
    )
    monkeypatch.setattr(
        settings, "chili_momentum_live_runner_loop_enabled", loop_on
    )
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", bus_on)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True)
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_live_scheduler_enabled", True
    )
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_nbbo_tape_enabled", False)
    monkeypatch.setattr(
        settings, "chili_momentum_alpaca_orphan_reconcile_enabled", False
    )
    monkeypatch.setattr(
        live_runner_loop,
        "start_live_runner_loop",
        lambda: started.append("loop") or True,
    )
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert started == []
        assert "momentum_live_runner_batch" not in job_ids
        assert "momentum_auto_arm_live" not in job_ids
        assert "lane_health_check" in job_ids
    finally:
        stop_scheduler()


def test_event_loop_start_refusal_blocks_auto_arm_but_keeps_health_loud(
    monkeypatch,
    caplog,
):
    import logging

    from app.services import trading_scheduler as scheduler_module
    from app.services.trading.momentum_neural import live_runner_loop
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_loop_enabled", True)
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True)
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_live_scheduler_enabled", True
    )
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_nbbo_tape_enabled", False)
    monkeypatch.setattr(
        settings, "chili_momentum_alpaca_orphan_reconcile_enabled", False
    )
    # Keep this ownership-registration test hermetic.  The isolated scheduler
    # role normally launches market-data prewarm immediately; letting that job
    # reach Yahoo/IQFeed makes stop_scheduler wait on unrelated network work.
    monkeypatch.setattr(
        scheduler_module,
        "_momentum_event_startup_delay_seconds",
        lambda _settings: 3600.0,
    )
    monkeypatch.setattr(live_runner_loop, "start_live_runner_loop", lambda: False)
    monkeypatch.setattr(live_runner_loop, "is_live_runner_loop_running", lambda: False)
    try:
        with caplog.at_level(logging.CRITICAL):
            start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert "momentum_auto_arm_live" not in job_ids
        assert "lane_health_check" in job_ids
        assert any("refused startup" in rec.message for rec in caplog.records)
    finally:
        stop_scheduler()


def test_already_running_event_loop_allows_same_process_auto_arm(monkeypatch):
    from app.services import trading_scheduler as scheduler_module
    from app.services.trading.momentum_neural import live_runner_loop
    from app.services.trading_scheduler import get_scheduler_info, start_scheduler, stop_scheduler

    stop_scheduler()
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_loop_enabled", True)
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True)
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_live_scheduler_enabled", True
    )
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_nbbo_tape_enabled", False)
    monkeypatch.setattr(
        settings, "chili_momentum_alpaca_orphan_reconcile_enabled", False
    )
    monkeypatch.setattr(
        scheduler_module,
        "_momentum_event_startup_delay_seconds",
        lambda _settings: 3600.0,
    )
    monkeypatch.setattr(live_runner_loop, "start_live_runner_loop", lambda: False)
    monkeypatch.setattr(live_runner_loop, "is_live_runner_loop_running", lambda: True)
    try:
        start_scheduler()
        job_ids = {j["id"] for j in get_scheduler_info().get("jobs", [])}
        assert "momentum_auto_arm_live" in job_ids
        assert "lane_health_check" in job_ids
    finally:
        stop_scheduler()


def test_each_auto_arm_pass_blocks_after_event_loop_owner_or_heartbeat_loss(
    monkeypatch,
):
    import app.db as app_db
    from app.services import trading_scheduler
    from app.services.trading.momentum_neural import (
        auto_arm,
        lane_health,
        live_runner_loop,
    )

    state = {"owner_ready": True, "control_ok": True}
    sessions = []
    arm_calls = []
    scheduler_heartbeats = []

    def _session_local():
        session = _FakeBatchSession(f"auto-arm-{len(sessions) + 1}")
        sessions.append(session)
        return session

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_live_enabled", True
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_auto_arm_live_scheduler_enabled",
        True,
    )
    monkeypatch.setattr(app_db, "SessionLocal", _session_local)
    monkeypatch.setattr(
        trading_scheduler,
        "run_scheduler_job_guarded",
        lambda _job_id, work: work(),
    )
    monkeypatch.setattr(
        lane_health,
        "live_runner_driver_configuration",
        lambda: ("event_loop", None),
    )
    monkeypatch.setattr(
        live_runner_loop,
        "is_live_runner_loop_admission_ready",
        lambda: state["owner_ready"],
    )
    monkeypatch.setattr(
        lane_health,
        "live_runner_loop_control_health",
        lambda _db: {
            "ok": state["control_ok"],
            "reason": (
                None
                if state["control_ok"]
                else "live_runner_loop_heartbeat_stale"
            ),
        },
    )
    monkeypatch.setattr(
        lane_health,
        "record_auto_arm_run",
        lambda: scheduler_heartbeats.append("ran"),
    )
    monkeypatch.setattr(
        auto_arm,
        "run_auto_arm_pass",
        lambda _db: arm_calls.append("armed")
        or {"armed": False, "skipped": "no_candidate"},
    )

    trading_scheduler._run_momentum_auto_arm_live_job()
    assert arm_calls == ["armed"]
    assert scheduler_heartbeats == ["ran"]

    state["control_ok"] = False
    trading_scheduler._run_momentum_auto_arm_live_job()
    assert arm_calls == ["armed"]
    assert scheduler_heartbeats == ["ran"]

    state["control_ok"] = True
    state["owner_ready"] = False
    trading_scheduler._run_momentum_auto_arm_live_job()
    assert arm_calls == ["armed"]
    assert scheduler_heartbeats == ["ran"]
    assert len(sessions) == 3
    assert all(session.closed for session in sessions)


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


def test_promotion_evidence_audit_job_disabled_returns_without_nameerror(monkeypatch):
    """Regression: the guard line used a bare ``settings`` that was never imported,
    so EVERY run crashed with NameError before the disable check could short-circuit.
    With the flag off the job must return cleanly (and never touch the DB)."""
    from app.services import trading_scheduler

    monkeypatch.setattr(settings, "chili_pattern_evidence_audit_enabled", False)

    called: list[bool] = []
    monkeypatch.setattr(
        "app.services.trading.promotion_evidence_audit.run_promotion_evidence_audit",
        lambda *_a, **_k: called.append(True),
    )

    # Must not raise NameError (the bug) — and must short-circuit before any audit work.
    assert trading_scheduler._run_promotion_evidence_audit_job() is None
    assert called == []


def test_promotion_evidence_audit_job_enabled_runs_and_closes_session(monkeypatch):
    """When enabled the job resolves ``settings``, runs the audit, and rolls back +
    closes its session (FIX 46 pattern) without raising."""
    import app.db as app_db
    from app.services import trading_scheduler
    from app.services.trading import promotion_evidence_audit

    monkeypatch.setattr(settings, "chili_pattern_evidence_audit_enabled", True)

    audit_session = _FakeBatchSession("audit")
    monkeypatch.setattr(app_db, "SessionLocal", lambda: audit_session)

    ran: list[object] = []
    monkeypatch.setattr(
        promotion_evidence_audit,
        "run_promotion_evidence_audit",
        lambda sess: ran.append(sess),
    )

    assert trading_scheduler._run_promotion_evidence_audit_job() is None
    assert ran == [audit_session]
    assert audit_session.rollbacks == 1
    assert audit_session.closed is True
