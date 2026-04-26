"""Phase D.2 sandboxed path — order and cleanup (mocked heavy ops)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models import PlanProject, PlanTask, User
from app.services.code_dispatch.miner import Candidate
from app.services.code_dispatch.rule_gate import RuleGateResult
from app.services.code_dispatch import runner


def _seed_minimal_task(db):
    u = User(name="dispatch_sand_test")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="dispatch_sand", key="DSX")
    db.add(p)
    db.commit()
    db.refresh(p)
    t = PlanTask(
        project_id=p.id,
        title="sbox",
        coding_readiness_state="not_started",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t.id


def test_sandboxed_calls_draft_apply_validate_in_order(
    db,
    monkeypatch,
) -> None:
    """Sandboxed mode calls worktree + apply + validate and cleans up."""
    monkeypatch.setenv("CHILI_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("CHILI_DISPATCH_MODE", "sandboxed")
    from app.services.code_dispatch import cycle

    tid = _seed_minimal_task(db)
    monkeypatch.setattr(
        cycle,
        "passes_code_rule_gate",
        lambda ctx: RuleGateResult(True, "ok", {}),
    )
    fake = Candidate(
        task_id=tid,
        repo_id=1,
        source="planner",
        reason="test",
        estimated_diff_loc=20,
        intended_files=[],
        prior_failure_count=0,
        force_tier=None,
    )
    # cycle imported pick_next_task by name; patch on cycle module, not miner.
    monkeypatch.setattr(cycle, "pick_next_task", lambda: fake)

    monkeypatch.setattr(
        cycle,
        "_dispatch_draft_suggestion",
        lambda tid, uid: (101, {"model": "m", "suggestion_id": 101}, 12.0),
    )

    monkeypatch.setattr(
        "app.services.coding_task.workspaces.get_bound_workspace_repo_for_profile",
        lambda db, prof, user_id: MagicMock(id=99),
    )
    monkeypatch.setattr(
        "app.services.code_brain.runtime.resolve_repo_runtime_path",
        lambda r: Path("/tmp/fake-root"),
    )

    calls: list[str] = []
    h = runner.WorktreeHandle(f"dispatch/{tid}", f"/tmp/dispatch-{tid}")

    def fake_create(repo_root, task_id):
        calls.append("create")
        return h

    def fake_apply(db, task_id, user_id, sid, hand):
        calls.append("apply")
        return {"ok": True, "files": ["a.py"], "loc": 3}

    def fake_val(db, task_id, worktree, *, validation_timeout_sec: int = 300):
        calls.append("validate")
        return (55, True, False)

    cleaned: list[bool] = []

    def fake_cleanup(handle, repo_root, *, keep_branch=True):
        cleaned.append(keep_branch is False)

    monkeypatch.setattr(runner, "create_dispatch_worktree", fake_create)
    monkeypatch.setattr(runner, "apply_suggestion_in_worktree", fake_apply)
    monkeypatch.setattr(runner, "run_validation_in_worktree", fake_val)
    monkeypatch.setattr(runner, "cleanup_worktree", fake_cleanup)

    from app.services.code_dispatch.cycle import run_code_learning_cycle

    out = run_code_learning_cycle()
    assert str(out.get("status", "")).startswith("sandboxed")
    assert calls == ["create", "apply", "validate"]
    assert cleaned and cleaned[0] is True, "worktree must be cleaned up (keep_branch=False)"


def test_sandboxed_cleans_up_on_validation_failure(db, monkeypatch) -> None:
    monkeypatch.setenv("CHILI_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("CHILI_DISPATCH_MODE", "sandboxed")
    from app.services.code_dispatch import cycle
    from app.services.code_dispatch.cycle import run_code_learning_cycle

    tid2 = _seed_minimal_task(db)
    monkeypatch.setattr(
        cycle,
        "passes_code_rule_gate",
        lambda ctx: RuleGateResult(True, "ok", {}),
    )
    fake = Candidate(
        task_id=tid2,
        repo_id=1,
        source="planner",
        reason="test",
        estimated_diff_loc=20,
        intended_files=[],
        prior_failure_count=0,
        force_tier=None,
    )
    # cycle imported pick_next_task by name; patch on cycle module, not miner.
    monkeypatch.setattr(cycle, "pick_next_task", lambda: fake)
    monkeypatch.setattr(
        cycle,
        "_dispatch_draft_suggestion",
        lambda tid, uid: (201, {"model": "m", "suggestion_id": 201}, 1.0),
    )
    monkeypatch.setattr(
        "app.services.coding_task.workspaces.get_bound_workspace_repo_for_profile",
        lambda db, prof, user_id: MagicMock(id=99),
    )
    monkeypatch.setattr(
        "app.services.code_brain.runtime.resolve_repo_runtime_path",
        lambda r: Path("/tmp/fake-root"),
    )
    h = runner.WorktreeHandle(f"dispatch/{tid2}", f"/tmp/dispatch-{tid2}")
    cleaned: list[bool] = []
    monkeypatch.setattr(
        runner, "create_dispatch_worktree", lambda r, t: h
    )
    monkeypatch.setattr(
        runner, "apply_suggestion_in_worktree", lambda *a, **k: {"ok": True, "files": ["x.py"], "loc": 1}
    )
    monkeypatch.setattr(
        runner,
        "run_validation_in_worktree",
        lambda *a, **k: (66, False, False),
    )
    monkeypatch.setattr(
        runner, "cleanup_worktree", lambda *a, **k: cleaned.append(True)
    )

    run_code_learning_cycle()
    assert cleaned, "worktree must be cleaned on validation failed path"


def test_reaper_marks_stuck_rows(monkeypatch, db) -> None:
    """Runs older than reap age with finished_at NULL become draft_timeout."""
    from sqlalchemy import text

    from app.db import engine
    from app.services.code_dispatch.cycle import _reap_stuck_runs

    monkeypatch.setenv("CHILI_DISPATCH_REAP_AGE_MIN", "15")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO code_agent_runs (started_at, cycle_step, rule_snapshot) "
                "VALUES (NOW() - INTERVAL '20 minutes', 'draft', '{}'::jsonb)"
            )
        )
    n = _reap_stuck_runs()
    assert n >= 1
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT decision FROM code_agent_runs "
                "WHERE finished_at IS NOT NULL "
                "  AND escalation_reason LIKE 'reaped:%' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "draft_timeout"


def test_reaper_skips_recent_rows(monkeypatch, db) -> None:
    """Recent unfinished runs are not reaped."""
    from sqlalchemy import text

    from app.db import engine
    from app.services.code_dispatch.cycle import _reap_stuck_runs

    monkeypatch.setenv("CHILI_DISPATCH_REAP_AGE_MIN", "15")
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "INSERT INTO code_agent_runs (started_at, cycle_step, rule_snapshot) "
                "VALUES (NOW() - INTERVAL '2 minutes', 'draft', '{}'::jsonb) "
                "RETURNING id"
            )
        )
        new_id = int(r.fetchone()[0])
    _reap_stuck_runs()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT decision, finished_at FROM code_agent_runs WHERE id = :i"),
            {"i": new_id},
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
