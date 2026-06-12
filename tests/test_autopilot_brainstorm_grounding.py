"""Brainstorm chat grounding — the 'baby' fix.

Live operator finding (2026-06-12): asked "what enhancement would you
recommend to the autopilot system of the project domain?" and the chat
answered about VEHICLE sensors — the prompt carried zero project context
and a 1B-3B local model. The chat must see the repo, the brain's insights,
recent activity, and code hits for the actual question.
"""

from __future__ import annotations

from app.models.code_brain import CodeRepo, CodeSearchEntry
from app.models.core import User
from app.services.project_autonomy import orchestrator
from app.services.project_autonomy.orchestrator import (
    ProjectAutonomyRun,
    _brainstorm_context_block,
    _chat_reply,
)


def _seed(db):
    db.add(User(email="t@t.local", name="t"))
    repo = CodeRepo(name="chili-home-copilot", path="/workspace", user_id=None,
                    framework_tags="fastapi,sqlalchemy", language_stats='{"python": 90}')
    db.add(repo)
    db.flush()
    db.add(CodeSearchEntry(
        repo_id=repo.id, file_path="app/services/project_autonomy/orchestrator.py",
        symbol_name="generate_diffs_from_plan", symbol_type="function",
        signature="def generate_diffs_from_plan(...)", docstring="autopilot diff engine",
        line_number=6225,
    ))
    run = ProjectAutonomyRun(run_id="pa_ctx_test", prompt="autopilot enhancements",
                             status="chatting", current_stage="chat", repo_id=repo.id)
    db.add(run)
    db.commit()
    return run


def test_context_block_contains_repo_insights_and_code_hits(db):
    run = _seed(db)
    block = _brainstorm_context_block(
        db, run, "what enhancement would you recommend to the autopilot system?"
    )
    assert "chili-home-copilot" in block
    assert "orchestrator.py" in block  # tokenized search found the real module
    assert "fastapi" in block


def test_context_block_includes_overview_doc_and_repo_map(db, tmp_path):
    """Live tester finding: without the project's own overview the chat
    suggested 'integrating FastAPI' to a FastAPI app, and search surfaced
    only peripheral name-matches."""
    (tmp_path / "CLAUDE.md").write_text(
        "# CLAUDE.md\nCHILI is a local-first household assistant whose most "
        "sophisticated subsystem is an autonomous trading brain.\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    db.add(User(email="t2@t.local", name="t2"))
    repo = CodeRepo(name="r2", path=str(tmp_path), user_id=None)
    db.add(repo)
    db.flush()
    run = ProjectAutonomyRun(run_id="pa_ctx_doc", prompt="x", status="chatting",
                             current_stage="chat", repo_id=repo.id)
    db.add(run)
    db.commit()
    block = _brainstorm_context_block(db, run, "what should we improve?")
    assert "local-first household assistant" in block  # CLAUDE.md excerpt
    assert "Repo map" in block
    assert "app/" in block and "scripts/" in block


def test_chat_reply_grounds_the_system_prompt(db, monkeypatch):
    run = _seed(db)
    captured: dict = {}

    def fake_gateway(**kwargs):
        captured.update(kwargs)
        return {"reply": "Grounded answer about the dispatch loop.", "model": "llama-70b"}

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat", fake_gateway
    )
    out = _chat_reply(db, run, "what enhancement would you recommend to the autopilot system?")
    assert "Grounded answer" in out
    sys_prompt = captured.get("system_prompt") or ""
    assert "THIS specific repository" in sys_prompt
    assert "chili-home-copilot" in sys_prompt
    assert "orchestrator.py" in sys_prompt


def test_implementation_shaped_message_no_longer_hijacked(db, monkeypatch):
    """'fix/add/implement' keywords used to short-circuit to a canned
    redirect, making real conversations impossible."""
    run = _seed(db)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        lambda **k: {"reply": "Let's look at scorer.py specifically.", "model": "m"},
    )
    out = _chat_reply(db, run, "how would you fix the tier scoring design?")
    assert "Let's look at scorer.py specifically." in out
