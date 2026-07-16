from __future__ import annotations

import subprocess
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.code_brain import CodeRepo
from app.models.coding_task import CodingExecutionIteration
from app.services.coding_task import execution_loop
from app.services.coding_task import handoff
from app.services.project_autonomy import orchestrator


def _run(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=True,
    )
    return (proc.stdout or "").strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init")
    _run(repo, "config", "user.email", "chili-test@example.invalid")
    _run(repo, "config", "user.name", "CHILI Test")
    (repo / "hello.py").write_text('VALUE = "old"\n', encoding="utf-8")
    _run(repo, "add", "hello.py")
    _run(repo, "commit", "-m", "initial")
    return repo


def test_worktree_creation_uses_isolated_auto_branch(tmp_path):
    repo = _init_repo(tmp_path)
    original = execution_loop._get_current_branch(repo)

    branch, worktree, base_sha = execution_loop._create_execution_worktree(
        repo,
        "abcdef1234567890",
    )
    try:
        assert branch == "chili/auto/abcdef123456"
        assert execution_loop._get_current_branch(repo) == original
        assert execution_loop._get_current_branch(worktree) == branch
        assert base_sha == _run(repo, "rev-parse", "HEAD")
    finally:
        execution_loop._cleanup_execution_worktree(
            repo,
            worktree,
            branch,
            delete_branch=True,
        )


def test_execution_worktree_preserves_operator_dirty_state(tmp_path):
    repo = _init_repo(tmp_path)
    original = execution_loop._get_current_branch(repo)
    (repo / "hello.py").write_text('VALUE = "operator-dirty"\n', encoding="utf-8")

    branch, worktree, _ = execution_loop._create_execution_worktree(
        repo,
        "dirty1234567890",
    )
    try:
        assert (worktree / "hello.py").read_text(encoding="utf-8") == 'VALUE = "old"\n'
        assert execution_loop._get_current_branch(repo) == original
        assert (repo / "hello.py").read_text(encoding="utf-8") == 'VALUE = "operator-dirty"\n'
    finally:
        execution_loop._cleanup_execution_worktree(
            repo,
            worktree,
            branch,
            delete_branch=True,
        )


def test_lifecycle_rollback_branch_restores_operator_checkout(tmp_path):
    repo = _init_repo(tmp_path)
    original = execution_loop._get_current_branch(repo)
    branch = execution_loop._create_branch(repo, "feedfacecafebeef")

    execution_loop._rollback_branch(repo, original, branch)

    assert execution_loop._get_current_branch(repo) == original
    branches = _run(repo, "branch", "--list")
    assert branch not in branches


def test_reads_applies_valid_diff_then_commits_reviewed_path(tmp_path):
    repo = _init_repo(tmp_path)
    diff = """diff --git a/hello.py b/hello.py
--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-VALUE = "old"
+VALUE = "new"
"""

    ok, message = execution_loop._apply_diffs(repo, [diff])

    assert ok is True, message
    assert (repo / "hello.py").read_text(encoding="utf-8") == 'VALUE = "new"\n'
    assert _run(repo, "log", "-1", "--pretty=%s") == "initial"

    committed, commit_message = execution_loop._commit_reviewed_changes(repo, ["hello.py"])

    assert committed is True, commit_message
    assert "chili: autonomous code change" in _run(repo, "log", "-1", "--pretty=%s")


def test_stages_only_generated_diffs_without_touching_unmentioned_file(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "untouched.py").write_text("UNCHANGED = True\n", encoding="utf-8")
    _run(repo, "add", "untouched.py")
    _run(repo, "commit", "-m", "add untouched")
    (repo / "untouched.py").write_text("OPERATOR_DIRTY = True\n", encoding="utf-8")
    diff = """diff --git a/hello.py b/hello.py
--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-VALUE = "old"
+VALUE = "new"
"""

    ok, message = execution_loop._apply_diffs(repo, [diff])
    committed, commit_message = execution_loop._commit_reviewed_changes(repo, ["hello.py"])

    assert ok is True, message
    assert committed is True, commit_message
    assert (repo / "untouched.py").read_text(encoding="utf-8") == "OPERATOR_DIRTY = True\n"
    assert _run(repo, "diff", "--name-only", "HEAD~1..HEAD") == "hello.py"
    assert _run(repo, "diff", "--name-only") == "untouched.py"


def test_apply_diffs_rejects_path_traversal_before_mutation(tmp_path):
    repo = _init_repo(tmp_path)
    diff = """--- a/hello.py
+++ b/../outside.py
@@ -1 +1 @@
-VALUE = \"old\"
+VALUE = \"new\"
"""

    ok, message = execution_loop._apply_diffs(repo, [diff])

    assert ok is False
    assert "Unsafe generated patch path" in message
    assert (repo / "hello.py").read_text(encoding="utf-8") == 'VALUE = "old"\n'


def test_acceptance_preflight_binds_branch_to_validated_file_set(tmp_path):
    repo = _init_repo(tmp_path)
    run_id = "lineage123456789"
    base_branch = execution_loop._get_current_branch(repo)
    branch, worktree, base_sha = execution_loop._create_execution_worktree(repo, run_id)
    try:
        (worktree / "hello.py").write_text('VALUE = "validated"\n', encoding="utf-8")
        committed, message = execution_loop._commit_reviewed_changes(worktree, ["hello.py"])
        assert committed is True, message
    finally:
        execution_loop._cleanup_execution_worktree(
            repo,
            worktree,
            branch,
            delete_branch=False,
        )
    metadata = {
        "schema": "chili.coding-execution-trajectory.v1",
        "run_id": run_id,
        "repo_id": 1,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "branch": branch,
        "worktree_isolated": True,
    }

    ready = execution_loop.acceptance_preflight(
        repo,
        run_id=run_id,
        execution_metadata=metadata,
        files_changed=["hello.py"],
        final_state=execution_loop.LoopState.DONE.value,
        test_exit_code=0,
    )

    assert ready["ok"] is True

    tamper_worktree = tmp_path / "tamper"
    _run(repo, "worktree", "add", str(tamper_worktree), branch)
    (tamper_worktree / "extra.py").write_text("TAMPERED = True\n", encoding="utf-8")
    _run(tamper_worktree, "add", "extra.py")
    _run(tamper_worktree, "commit", "-m", "expand unreviewed scope")
    _run(repo, "worktree", "remove", "--force", str(tamper_worktree))

    rejected = execution_loop.acceptance_preflight(
        repo,
        run_id=run_id,
        execution_metadata=metadata,
        files_changed=["hello.py"],
        final_state=execution_loop.LoopState.DONE.value,
        test_exit_code=0,
    )

    assert rejected["ok"] is False
    assert "file set no longer matches" in rejected["reason"]
    _run(repo, "branch", "-D", branch)


def test_execution_loop_runs_in_worktree_and_preserves_operator_checkout(monkeypatch, tmp_path):
    repo_path = _init_repo(tmp_path)
    test_file = repo_path / "tests/test_hello.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from hello import VALUE\n\n\ndef test_value():\n    assert VALUE == 'new'\n",
        encoding="utf-8",
    )
    _run(repo_path, "add", "tests/test_hello.py")
    _run(repo_path, "commit", "-m", "add focused test")
    dirty_file = repo_path / "operator_notes.txt"
    dirty_file.write_text("do not stage me\n", encoding="utf-8")
    original_branch = execution_loop._get_current_branch(repo_path)
    original_head = _run(repo_path, "rev-parse", "HEAD")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[CodeRepo.__table__, CodingExecutionIteration.__table__],
    )
    db = sessionmaker(bind=engine)()
    repo = CodeRepo(path=str(repo_path), name="fixture", active=True)
    db.add(repo)
    db.commit()

    monkeypatch.setattr(
        execution_loop,
        "_gather_context",
        lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
    )

    def fake_chat(messages, system_prompt, trace_id, max_tokens=2000):
        if "-plan-" in trace_id:
            return {
                "reply": '{"analysis":"change value","files":[{"path":"hello.py","action":"modify","description":"return the new value"}]}',
                "model": "fixture",
            }
        return {
            "reply": """```diff
--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-VALUE = \"old\"
+VALUE = \"new\"
```""",
            "model": "fixture",
        }

    monkeypatch.setattr(execution_loop, "_llm_chat", fake_chat)
    try:
        result = execution_loop.run_execution_loop(db, "Change VALUE to new.", repo.id)

        assert result.status == "success"
        assert execution_loop._get_current_branch(repo_path) == original_branch
        assert _run(repo_path, "rev-parse", "HEAD") == original_head
        assert (repo_path / "hello.py").read_text(encoding="utf-8") == 'VALUE = "old"\n'
        assert dirty_file.read_text(encoding="utf-8") == "do not stage me\n"
        assert _run(repo_path, "status", "--porcelain") == "?? operator_notes.txt"
        assert _run(repo_path, "show", f"{result.branch_name}:hello.py") == 'VALUE = "new"'
        row = db.query(CodingExecutionIteration).filter_by(run_id=result.run_id).one()
        assert row.state == execution_loop.LoopState.DONE.value
        assert row.test_exit_code == 0
        assert '"worktree_isolated": true' in row.plan_json
    finally:
        _run(repo_path, "branch", "-D", result.branch_name) if "result" in locals() and result.branch_name else None
        db.close()


def test_handoff_module_exports_read_only_summary_builders():
    assert callable(handoff.build_handoff_dict)
    assert callable(handoff.get_coding_summary_dict)
    assert callable(handoff.list_blockers_dict)


def test_publication_export_handoff_copy_preserves_permission_boundary():
    copy = orchestrator._pr_publication_preflight_handoff_copy(
        {"top_pr": "282", "top_branch": "codex/test", "top_merge": "clean", "top_ci": "pending"},
        missing_evidence=["current_head_check_receipt"],
        required_evidence=["current_head_check_receipt", "publication URL"],
        recovery_decision={
            "label": "Wait for current-head check",
            "owner": "PR owner",
            "first_action": "Attach current-head check proof.",
            "proof": "Receipt with PR, branch, head SHA, check, URL, and timestamp.",
        },
    )

    assert "Project Autopilot PR publication decision packet" in copy
    assert "current_head_check_receipt" in copy
    assert "does not authorize source/test edits" in copy


def test_pr_publication_preflight_blocks_without_current_head_receipt():
    packet = {
        "delivery_blocker_groups": [
            {
                "key": "pr_blocker_train",
                "top_pr": "282",
                "top_branch": "codex/test",
                "top_merge": "clean",
                "top_ci": "success",
            }
        ]
    }

    enriched = orchestrator._quality_bar_with_pr_publication_preflight(packet)
    group = enriched["delivery_blocker_groups"][0]

    assert group["pr_publish_verdict"] == "not_publishable"
    assert group["publication_receipt"]["publication_proof_ready"] is False
    assert "current_head_check_receipt" in group["publication_receipt"]["missing_evidence"]


def test_pr_status_and_pr_repair_fields_guide_safe_recovery():
    group = orchestrator._pr_publication_preflight_group(
        {
            "key": "pr_blocker_train",
            "top_pr": "282",
            "top_branch": "codex/test",
            "top_merge": "dirty",
            "top_ci": "failure",
            "failing_check_names": ["pytest"],
        },
        {},
    )

    assert "push_or_pr_creation" in group["pr_publish_forbidden_actions"]
    assert group["pr_recovery_owner"]
    assert group["pr_recovery_safe_next_step"]
    assert group["publication_receipt"]["status"] == "warning"
