"""Dispatch worktree cleanup must NEVER touch other worktrees.

create_dispatch_worktree used to run blanket `git worktree prune` against
the shared repo; host-side worktrees (agent worktrees, deploy checkouts)
are invisible from inside the container, so the prune severed THEIR
registrations (verified live 2026-06-11). Cleanup is now scoped to the
task's own path/registration.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.services.code_dispatch.runner import create_dispatch_worktree


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (p.stdout or "") + (p.stderr or "")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_stale_foreign_worktree_survives_dispatch_cleanup(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)

    # A "foreign" worktree whose path then disappears (= a host-side
    # worktree as seen from inside the container).
    foreign = tmp_path / "foreign-wt"
    subprocess.run(["git", "worktree", "add", "-b", "agent/x", str(foreign), "main"],
                   cwd=repo, check=True)
    shutil.rmtree(foreign)
    assert "foreign-wt" in _git(repo, "worktree", "list", "--porcelain")

    monkeypatch.setenv("CHILI_DISPATCH_WORKTREE_DIR", str(tmp_path / "dispatch"))
    handle = create_dispatch_worktree(str(repo), 99)

    listing = _git(repo, "worktree", "list", "--porcelain")
    # The regression: blanket prune would have dropped the foreign entry.
    assert "foreign-wt" in listing
    assert "task-99" in listing
    assert handle.branch == "dispatch/99"


def test_own_stale_registration_is_cleared_and_recreated(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CHILI_DISPATCH_WORKTREE_DIR", str(tmp_path / "dispatch"))

    h1 = create_dispatch_worktree(str(repo), 7)
    # Simulate container recreate: the dispatch path vanishes, registration stays.
    shutil.rmtree(h1.path)

    h2 = create_dispatch_worktree(str(repo), 7)  # must not raise exit-128
    assert Path(h2.path).is_dir()
    assert "task-7" in _git(repo, "worktree", "list", "--porcelain")
