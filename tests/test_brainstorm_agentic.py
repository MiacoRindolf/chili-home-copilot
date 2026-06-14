"""Agentic brainstorm mechanics: read-loop, doc targeting, escalation, lessons.

All adaptive — nothing keyed to project-specific strings: the MODEL decides
when to read or escalate; doc scoring/excerpts derive from the query terms;
lessons persist into the existing insights store.
"""

from __future__ import annotations

from app.models.code_brain import CodeInsight, CodeRepo
from app.models.core import User
from app.services.project_autonomy.orchestrator import (
    ProjectAutonomyRun,
    _best_excerpt,
    _brainstorm_context_block,
    _chat_reply,
    _parse_protocol_directive,
    _safe_repo_file_read,
    _score_doc,
    _store_lesson,
)


# ── doc targeting ────────────────────────────────────────────────────────


def test_best_excerpt_centers_on_dense_region():
    text = ("filler " * 300) + "the momentum fill failure root cause was spread crossing " + ("tail " * 300)
    out = _best_excerpt(text, {"momentum", "fill", "spread"}, width=200)
    assert "root cause" in out
    assert out != text[:200]  # not the head — centered on the finding


def test_score_doc_weights_filename_and_dampens_repeats():
    d1, w1 = _score_doc("momentum-fill-postmortem.md", "spread " * 50, {"momentum", "fill", "spread"})
    d2, w2 = _score_doc("unrelated.md", "momentum " * 500, {"momentum", "fill", "spread"})
    assert d1 == 3 and d2 == 1
    assert w1 > w2  # filename hits + distinct coverage beat one spammy term


def test_context_block_picks_the_right_doc_among_decoys(db, tmp_path):
    docs = tmp_path / "docs" / "REPORTS"
    docs.mkdir(parents=True)
    (docs / "broker-mcp-notes.md").write_text("broker mcp rail notes " * 40, encoding="utf-8")
    (docs / "momentum-fill-postmortem.md").write_text(
        ("intro " * 200) + "momentum equity trades never fill because market orders cross the spread",
        encoding="utf-8",
    )
    db.add(User(email="d@t.local", name="d"))
    repo = CodeRepo(name="rd", path=str(tmp_path), user_id=None)
    db.add(repo)
    db.flush()
    run = ProjectAutonomyRun(run_id="pa_docs", prompt="x", status="chatting",
                             current_stage="chat", repo_id=repo.id)
    db.add(run)
    db.commit()
    block = _brainstorm_context_block(db, run, "why do momentum trades never fill?")
    assert "momentum-fill-postmortem.md" in block
    assert "cross the spread" in block  # excerpt centered on the finding, not the intro


# ── protocol parsing + safe reads ────────────────────────────────────────


def test_protocol_directive_parsing():
    assert _parse_protocol_directive("READ: app/a.py app/b.py") == ("read", "app/a.py app/b.py")
    assert _parse_protocol_directive("escalate: costly tradeoff") == ("escalate", "costly tradeoff")
    assert _parse_protocol_directive("Here is my answer...") is None


def test_safe_repo_file_read_never_escapes(tmp_path):
    (tmp_path / "inside.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    try:
        assert "x = 1" in _safe_repo_file_read(str(tmp_path), "inside.py")
        assert _safe_repo_file_read(str(tmp_path), "../secret.txt") is None
    finally:
        outside.unlink(missing_ok=True)


# ── read-loop + escalation + lessons (gateway mocked) ────────────────────


def _mk_run(db, tmp_path):
    db.add(User(email="a@t.local", name="a"))
    (tmp_path / "core.py").write_text("THE_ANSWER = 42\n", encoding="utf-8")
    repo = CodeRepo(name="ra", path=str(tmp_path), user_id=None)
    db.add(repo)
    db.flush()
    run = ProjectAutonomyRun(run_id="pa_agentic", prompt="x", status="chatting",
                             current_stage="chat", repo_id=repo.id)
    db.add(run)
    db.commit()
    return run


def test_read_loop_feeds_file_contents_back(db, tmp_path, monkeypatch):
    run = _mk_run(db, tmp_path)
    calls: list[dict] = []

    def fake_gateway(**k):
        calls.append(k)
        if len(calls) == 1:
            return {"reply": "READ: core.py", "model": "m"}
        joined = " ".join(m["content"] for m in k["messages"] if m["role"] == "user")
        assert "THE_ANSWER = 42" in joined  # the harness fed the real file back
        return {"reply": "Grounded in core.py: the answer is 42.", "model": "m"}

    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", fake_gateway)
    out = _chat_reply(db, run, "what is the core answer constant?")
    assert "the answer is 42" in out
    assert len(calls) == 2


def test_read_loop_is_iterative_follows_reference_chain(db, tmp_path, monkeypatch):
    """Fable-style 'keep reading': READ core.py -> it points at deep.py ->
    READ deep.py -> answer. A single-pass loop could not follow the chain."""
    run = _mk_run(db, tmp_path)
    (tmp_path / "core.py").write_text("# see deep.py for the real value\nFROM_CORE = 1\n", encoding="utf-8")
    (tmp_path / "deep.py").write_text("REAL_VALUE = 99\n", encoding="utf-8")
    calls: list[dict] = []

    def fake_gateway(**k):
        calls.append(k)
        joined = " ".join(m["content"] for m in k["messages"] if m["role"] == "user")
        if len(calls) == 1:
            return {"reply": "READ: core.py", "model": "m"}
        if "see deep.py" in joined and "REAL_VALUE" not in joined:
            return {"reply": "READ: deep.py", "model": "m"}  # follow the reference
        assert "REAL_VALUE = 99" in joined
        return {"reply": "The real value is 99, per deep.py.", "model": "m"}

    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", fake_gateway)
    out = _chat_reply(db, run, "what is the real value?")
    assert "99" in out
    assert len(calls) == 3  # initial + 2 read rounds


def test_read_loop_bounded_forces_answer(db, tmp_path, monkeypatch):
    """A model that keeps asking to READ must be cut off and forced to answer."""
    run = _mk_run(db, tmp_path)
    (tmp_path / "a.py").write_text("a=1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b=2\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("c=3\n", encoding="utf-8")
    (tmp_path / "d.py").write_text("d=4\n", encoding="utf-8")
    seq = iter(["READ: a.py", "READ: b.py", "READ: c.py", "READ: d.py", "READ: a.py"])

    def fake_gateway(**k):
        # On the forced-answer round the system prompt lacks the protocol.
        sysp = k.get("system_prompt") or ""
        if "PROTOCOL" not in sysp:
            return {"reply": "Final answer after reading my budget.", "model": "m"}
        return {"reply": next(seq, "Done."), "model": "m"}

    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", fake_gateway)
    out = _chat_reply(db, run, "tell me everything")
    assert "Final answer" in out
    assert "READ:" not in out


def test_escalation_routes_to_frontier_when_configured(db, tmp_path, monkeypatch):
    run = _mk_run(db, tmp_path)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        lambda **k: {"reply": "ESCALATE: irreversible architecture tradeoff", "model": "m"},
    )
    from app.config import settings

    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)
    monkeypatch.setattr(settings, "frontier_api_key", "k")
    monkeypatch.setattr(settings, "frontier_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "frontier_model", "gpt-5.5")
    captured: dict = {}

    def fake_chat(msgs, **k):
        captured.update(k)
        return {"reply": "Frontier verdict: no, grep first.", "model": "gpt-5.5"}

    monkeypatch.setattr("app.openai_client.chat", fake_chat)
    out = _chat_reply(db, run, "should we adopt an embeddings index?")
    assert "Frontier verdict" in out
    assert captured.get("model_override") == "gpt-5.5"


def test_lesson_line_is_stored_and_stripped(db, tmp_path, monkeypatch):
    run = _mk_run(db, tmp_path)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        lambda **k: {"reply": "Real answer here.\nLESSON: this project gates merges on sandbox validation.",
                     "model": "m"},
    )
    out = _chat_reply(db, run, "how do merges work here?")
    assert "LESSON:" not in out
    rows = db.query(CodeInsight).filter(CodeInsight.category == "lesson").all()
    assert any("sandbox validation" in r.description for r in rows)
    # Dedupe: same lesson again does not duplicate.
    _store_lesson(db, run.repo_id, "this project gates merges on sandbox validation.")
    rows2 = db.query(CodeInsight).filter(CodeInsight.category == "lesson").all()
    assert len(rows2) == len(rows)
