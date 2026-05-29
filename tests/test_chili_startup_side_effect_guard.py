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
    restore_pos = body.find("_restore_broker_sessions()")
    assert guard_pos > 0
    assert restore_pos > 0
    assert guard_pos < restore_pos
