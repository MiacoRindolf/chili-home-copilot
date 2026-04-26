"""Phase D.1 sanity test — shadow cycle never tries to apply diffs."""

import os
from unittest.mock import patch

from sqlalchemy import text

from app.db import engine
from app.services.code_dispatch.cycle import run_code_learning_cycle


def test_shadow_returns_status_when_disabled():
    os.environ.pop("CHILI_DISPATCH_ENABLED", None)
    result = run_code_learning_cycle()
    assert result["status"] == "disabled"


def test_shadow_never_calls_apply_when_mode_is_shadow(monkeypatch):
    monkeypatch.setenv("CHILI_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("CHILI_DISPATCH_MODE", "shadow")
    with patch("app.services.code_dispatch.runner.apply_suggestion_in_worktree") as mock_apply:
        run_code_learning_cycle()
    mock_apply.assert_not_called()


def test_idle_writes_heartbeat_row(monkeypatch, db):
    """Phase D.1.1: idle cycles emit a heartbeat audit row."""
    monkeypatch.setenv("CHILI_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("CHILI_DISPATCH_MODE", "shadow")
    from app.services.code_dispatch import miner

    monkeypatch.setattr(miner, "pick_next_task", lambda: None)
    with engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM code_agent_runs")).scalar() or 0
    result = run_code_learning_cycle()
    assert result["status"] == "idle"
    assert result.get("run_id") is not None
    with engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM code_agent_runs")).scalar() or 0
    assert after == before + 1
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT cycle_step, decision FROM code_agent_runs "
                "ORDER BY id DESC LIMIT 1"
            )
        ).mappings().first()
    assert row is not None
    assert row["cycle_step"] == "mine"
    assert row["decision"] == "idle"
