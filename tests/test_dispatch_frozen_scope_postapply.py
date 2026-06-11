"""Post-apply frozen-scope gate (dispatch lane).

The pre-dispatch check runs on ``intended_files``, which the miner often
cannot populate — so a generated diff touching a blocked scope (e.g.
app/services/trading/*) previously sailed straight to commit+push. The
runner now re-checks the APPLIED diff against frozen scope using git truth,
before any commit/push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.code_dispatch import runner
from app.services.code_dispatch.frozen_scope import FrozenHit


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def _hit(path: str, severity: str = "block") -> FrozenHit:
    return FrozenHit(glob="app/services/trading/*", severity=severity,
                     reason="trading brain is frozen to the coding brain", file_path=path)


# ── git truth ────────────────────────────────────────────────────────────


def test_git_changed_files_sees_modified_and_added(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "base.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "newfile.py").write_text("y = 1\n", encoding="utf-8")

    files = runner.git_changed_files(repo)
    assert "base.py" in files
    assert "newfile.py" in files


def test_git_changed_files_outside_repo_returns_empty(tmp_path):
    assert runner.git_changed_files(tmp_path / "not-a-repo") == []


# ── gate behavior ────────────────────────────────────────────────────────


def test_gate_passes_clean_diff(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(runner, "diff_touches_frozen_scope", lambda files: [])
    assert runner.enforce_frozen_scope_post_apply(repo, ["base.py"]) is None


def test_gate_refuses_blocked_scope_with_message_contract(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    blocked = _hit("app/services/trading/auto_trader.py")
    monkeypatch.setattr(runner, "diff_touches_frozen_scope", lambda files: [blocked])

    refusal = runner.enforce_frozen_scope_post_apply(repo, ["app/services/trading/auto_trader.py"])
    assert refusal is not None
    assert refusal["ok"] is False
    # cycle.py keys its escalation path off this prefix — contract, not style.
    assert refusal["message"].startswith("frozen_scope_blocked")
    assert "auto_trader.py" in refusal["message"]
    assert refusal["frozen_hits"][0]["severity"] == "block"


def test_gate_allows_warn_and_review_required(tmp_path, monkeypatch):
    """Only 'block' refuses; review_required still drafts (PR + manual merge
    contract) and warn is audit-only."""
    repo = _init_repo(tmp_path)
    soft_hits = [_hit("app/x.py", "warn"), _hit("app/y.py", "review_required")]
    monkeypatch.setattr(runner, "diff_touches_frozen_scope", lambda files: soft_hits)
    assert runner.enforce_frozen_scope_post_apply(repo, ["app/x.py"]) is None


def test_gate_checks_git_truth_not_just_claimed_files(tmp_path, monkeypatch):
    """A patch that touches a frozen path the snapshot did NOT claim is still
    caught: the gate unions git status with the claimed list."""
    repo = _init_repo(tmp_path)
    sneaky = repo / "app" / "services" / "trading" / "auto_trader.py"
    sneaky.parent.mkdir(parents=True, exist_ok=True)
    sneaky.write_text("# sneaky edit\n", encoding="utf-8")

    seen: list[list[str]] = []

    def spy(files):
        seen.append(list(files))
        return []

    monkeypatch.setattr(runner, "diff_touches_frozen_scope", spy)
    runner.enforce_frozen_scope_post_apply(repo, ["docs/readme.md"])  # claim is innocent
    assert seen, "gate did not consult frozen scope"
    assert any("app/services/trading/auto_trader.py" in f for f in seen[0])
