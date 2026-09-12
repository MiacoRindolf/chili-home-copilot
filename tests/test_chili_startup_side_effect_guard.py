from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Ang TUNAY na hangganan ng function, hindi isang nakapirming bilang ng
# character. Ang lumang `src[idx:idx+6200]` ay sumasaklaw lamang ng 5.3%
# ng `start_scheduler` noong 2026-08-25 at tumigil na sa pagbabantay.
from tests.source_region import function_body


class _Settings:
    def __init__(self, role: str | None, runs_externally: bool) -> None:
        self.chili_scheduler_role = role
        self.chili_scheduler_runs_externally = runs_externally


def test_role_none_host_without_external_scheduler_disables_deferred_side_effects() -> None:
    from app.main import _deferred_startup_side_effects_disabled

    assert _deferred_startup_side_effects_disabled(_Settings("none", False)) is True
    assert _deferred_startup_side_effects_disabled(_Settings(None, False)) is True


def test_role_none_compose_web_with_external_scheduler_keeps_deferred_side_effects() -> None:
    from app.main import _deferred_startup_side_effects_disabled

    assert _deferred_startup_side_effects_disabled(_Settings("none", True)) is False


def test_role_none_web_never_restores_broker_sessions() -> None:
    from app.main import _startup_broker_restore_enabled

    assert _startup_broker_restore_enabled(_Settings("none", False)) is False
    assert _startup_broker_restore_enabled(_Settings("none", True)) is False


def test_scheduler_roles_restore_broker_sessions() -> None:
    from app.main import _startup_broker_restore_enabled

    for role in ("all", "web", "worker", "autotrader_only", "broker_sync_only", "cron_only"):
        assert _startup_broker_restore_enabled(_Settings(role, False)) is True


def test_dedicated_alpaca_paper_process_never_restores_other_brokers():
    from app.main import _startup_broker_restore_enabled
    value = _Settings(" MOMENTUM_EXEC_ONLY ", False)
    value.chili_momentum_equity_execution_via_alpaca_paper = True
    assert _startup_broker_restore_enabled(value) is False
    # Disabling Alpaca's PAPER posture must not grant unrelated broker access
    # while the dedicated PAPER routing contract is still selected.
    value.chili_alpaca_paper = False
    assert _startup_broker_restore_enabled(value) is False
    value.chili_scheduler_role = "broker_sync_only"
    assert _startup_broker_restore_enabled(value) is True
    value.chili_scheduler_role = "momentum_exec_only"
    value.chili_momentum_equity_execution_via_alpaca_paper = False
    assert _startup_broker_restore_enabled(value) is True


def test_direct_restore_cannot_reach_broker_or_vault_in_paper_exec_process(monkeypatch):
    import app.main as main
    from app.config import settings
    from app import db
    from app.services import broker_service
    touched = []
    def forbidden(name):
        touched.append(name)
        raise RuntimeError("forbidden startup access")
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_equity_execution_via_alpaca_paper", True)
    monkeypatch.setattr(broker_service, "try_restore_session", lambda: forbidden("robinhood"))
    monkeypatch.setattr(db, "SessionLocal", lambda: forbidden("credential_vault"))
    main._restore_broker_sessions()
    assert touched == []


def test_execution_scheduler_starts_after_risk_restore_without_backtest_maintenance(monkeypatch):
    import app.main as main
    from app.config import settings
    from app.services.trading import governance, portfolio_risk
    calls = []
    monkeypatch.setattr(main, "_under_pytest", False)
    monkeypatch.setattr(settings, "chili_scheduler_role", "momentum_exec_only")
    monkeypatch.setattr(settings, "chili_momentum_equity_execution_via_alpaca_paper", True)
    monkeypatch.setattr(main, "_warn_dual_path_broker_credentials", lambda _: None)
    monkeypatch.setattr(governance, "restore_kill_switch_from_db", lambda: calls.append("restore_kill"))
    monkeypatch.setattr(governance, "get_kill_switch_status", lambda: {"active":False})
    monkeypatch.setattr(portfolio_risk, "restore_breaker_from_db", lambda: calls.append("restore_breaker"))
    monkeypatch.setattr(portfolio_risk, "get_breaker_status", lambda: {"tripped":False})
    for name in ("_restore_broker_sessions", "_dedup_backtests", "_repair_wrongly_deactivated",
                 "_ensure_ticker_scope_columns", "_cleanup_cross_asset_backtests", "_reinfer_pattern_timeframes",
                 "_recompute_all_ticker_scopes", "_prewarm_market_context", "_backfill_backtests"):
        monkeypatch.setattr(main, name, lambda name=name: calls.append("forbidden:"+name))
    for name in ("start_scheduler", "_start_massive_ws", "_start_price_bus"):
        monkeypatch.setattr(main, name, lambda name=name: calls.append(name))
    main._run_deferred_startup()
    assert calls == ["restore_kill", "restore_breaker", "start_scheduler", "_start_massive_ws", "_start_price_bus"]


def test_scheduler_roles_keep_deferred_side_effects() -> None:
    from app.main import _deferred_startup_side_effects_disabled

    for role in ("all", "web", "worker", "autotrader_only", "broker_sync_only", "cron_only"):
        assert _deferred_startup_side_effects_disabled(_Settings(role, False)) is False


def test_scheduler_worker_broker_restore_only_for_broker_roles(monkeypatch) -> None:
    from scripts import scheduler_worker

    for role in ("all", "web", "worker", "autotrader_only", "broker_sync_only"):
        assert scheduler_worker._scheduler_worker_broker_restore_enabled(role) is True

    for role in ("cron_only", "market_snapshot_only", "none", "unknown", ""):
        assert scheduler_worker._scheduler_worker_broker_restore_enabled(role) is False

    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", " MARKET_SNAPSHOT_ONLY ")
    assert scheduler_worker._scheduler_worker_role() == "market_snapshot_only"
    assert scheduler_worker._scheduler_worker_broker_restore_enabled() is False


def test_deferred_startup_checks_side_effect_guard_before_broker_restore() -> None:
    body = function_body(REPO / "app/main.py", "_run_deferred_startup")
    guard_pos = body.find("_deferred_startup_side_effects_disabled(")
    broker_guard_pos = body.find("_startup_broker_restore_enabled(")
    restore_pos = body.find("_restore_broker_sessions()")
    assert guard_pos > 0
    assert broker_guard_pos > 0
    assert restore_pos > 0
    assert guard_pos < restore_pos
    assert broker_guard_pos < restore_pos


def test_app_startup_restores_durable_circuit_breaker_after_kill_switch() -> None:
    body = function_body(REPO / "app/main.py", "_run_deferred_startup")
    kill_pos = body.find("restore_kill_switch_from_db()")
    breaker_pos = body.find("restore_breaker_from_db()")
    assert kill_pos > 0
    assert breaker_pos > 0
    assert kill_pos < breaker_pos
    assert "get_breaker_status" in body


def test_scheduler_startup_restores_durable_circuit_breaker() -> None:
    body = function_body(REPO / "app/services/trading_scheduler.py", "start_scheduler")
    kill_pos = body.find("restore_kill_switch_from_db()")
    breaker_pos = body.find("restore_breaker_from_db()")
    assert kill_pos > 0
    assert breaker_pos > 0
    assert kill_pos < breaker_pos
    assert "Circuit breaker restored ACTIVE" in body


def test_scheduler_worker_restores_durable_circuit_breaker() -> None:
    body = function_body(REPO / "scripts/scheduler_worker.py", "main")
    kill_pos = body.find("restore_kill_switch_from_db()")
    breaker_pos = body.find("restore_breaker_from_db()")
    assert kill_pos > 0
    assert breaker_pos > 0
    assert kill_pos < breaker_pos
    assert "Circuit breaker restored ACTIVE" in body


def test_scheduler_worker_checks_role_before_broker_session_restore() -> None:
    body = function_body(REPO / "scripts/scheduler_worker.py", "main")
    role_pos = body.find("role = _scheduler_worker_role()")
    gate_pos = body.find("_scheduler_worker_broker_restore_enabled(role)")
    restore_pos = body.find("broker_service.try_restore_session()")
    skip_pos = body.find("Broker session restore skipped")
    assert role_pos > 0
    assert gate_pos > 0
    assert restore_pos > 0
    assert skip_pos > 0
    assert role_pos < gate_pos < restore_pos
