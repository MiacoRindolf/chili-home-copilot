from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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


def test_scheduler_roles_keep_deferred_side_effects() -> None:
    from app.main import _deferred_startup_side_effects_disabled

    for role in ("all", "web", "worker", "autotrader_only", "broker_sync_only", "cron_only"):
        assert _deferred_startup_side_effects_disabled(_Settings(role, False)) is False


def test_deferred_startup_checks_side_effect_guard_before_broker_restore() -> None:
    src = (REPO / "app/main.py").read_text()
    idx = src.find("def _run_deferred_startup()")
    assert idx > 0
    body = src[idx : idx + 2500]
    guard_pos = body.find("_deferred_startup_side_effects_disabled(")
    broker_guard_pos = body.find("_startup_broker_restore_enabled(")
    restore_pos = body.find("_restore_broker_sessions()")
    assert guard_pos > 0
    assert broker_guard_pos > 0
    assert restore_pos > 0
    assert guard_pos < restore_pos
    assert broker_guard_pos < restore_pos


def test_app_startup_restores_durable_circuit_breaker_after_kill_switch() -> None:
    src = (REPO / "app/main.py").read_text()
    idx = src.find("def _run_deferred_startup()")
    assert idx > 0
    body = src[idx : idx + 3600]
    kill_pos = body.find("restore_kill_switch_from_db()")
    breaker_pos = body.find("restore_breaker_from_db()")
    assert kill_pos > 0
    assert breaker_pos > 0
    assert kill_pos < breaker_pos
    assert "get_breaker_status" in body


def test_scheduler_startup_restores_durable_circuit_breaker() -> None:
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    idx = src.find("def start_scheduler(")
    assert idx > 0
    body = src[idx : idx + 6200]
    kill_pos = body.find("restore_kill_switch_from_db()")
    breaker_pos = body.find("restore_breaker_from_db()")
    assert kill_pos > 0
    assert breaker_pos > 0
    assert kill_pos < breaker_pos
    assert "Circuit breaker restored ACTIVE" in body


def test_scheduler_worker_restores_durable_circuit_breaker() -> None:
    src = (REPO / "scripts/scheduler_worker.py").read_text()
    idx = src.find("def main()")
    assert idx > 0
    body = src[idx : idx + 4200]
    kill_pos = body.find("restore_kill_switch_from_db()")
    breaker_pos = body.find("restore_breaker_from_db()")
    assert kill_pos > 0
    assert breaker_pos > 0
    assert kill_pos < breaker_pos
    assert "Circuit breaker restored ACTIVE" in body
