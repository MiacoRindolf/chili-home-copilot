"""Tests for the teacher-skill semantic index (app/services/skill_memory.py).

Chroma is mocked — no real vector store / Ollama. Covers doc flattening, upsert
on index, query mapping on retrieve, the never-raises / degrade-to-noop contract,
and the teacher_hook combined saver (file save + best-effort index).
"""
from unittest.mock import MagicMock, patch

from app.services import skill_memory as sm


_SKILL = {
    "name": "open-x-chat",
    "description": "How to open the user's X chat",
    "when_to_use": "When the user says 'open my X chat'",
    "procedure": ["Step 1: call list_sessions", "Step 2: respond with the link"],
    "source": "teacher-escalation",
}


class TestSkillDoc:
    def test_flattens_name_desc_procedure(self):
        doc = sm._skill_doc(_SKILL)
        assert "open-x-chat" in doc
        assert "How to open" in doc
        assert "call list_sessions" in doc

    def test_handles_missing_fields(self):
        assert sm._skill_doc({"name": "x"}) == "x"
        assert sm._skill_doc({}) == ""


class TestIndexSkill:
    def test_upserts_on_index(self):
        col = MagicMock()
        with patch.object(sm, "_collection", return_value=col):
            ok = sm.index_skill(_SKILL)
        assert ok is True
        col.upsert.assert_called_once()
        kwargs = col.upsert.call_args.kwargs
        assert kwargs["ids"] == ["open-x-chat"]
        assert "open-x-chat" in kwargs["documents"][0]
        assert kwargs["metadatas"][0]["name"] == "open-x-chat"

    def test_rejects_unnamed(self):
        # No name -> rejected before the store is even touched.
        with patch.object(sm, "_collection") as col_fn:
            assert sm.index_skill({"procedure": ["x"]}) is False
            assert sm.index_skill({}) is False
            assert sm.index_skill("not a dict") is False
            col_fn.assert_not_called()

    def test_name_only_still_indexes(self):
        # A name-only skill yields a doc (= the name), so it indexes.
        col = MagicMock()
        with patch.object(sm, "_collection", return_value=col):
            assert sm.index_skill({"name": "solo"}) is True

    def test_noop_when_store_unavailable(self):
        with patch.object(sm, "_collection", return_value=None):
            assert sm.index_skill(_SKILL) is False

    def test_never_raises_on_upsert_error(self):
        col = MagicMock()
        col.upsert.side_effect = Exception("chroma down")
        with patch.object(sm, "_collection", return_value=col):
            assert sm.index_skill(_SKILL) is False


class TestRetrieveSkills:
    def test_maps_query_results(self):
        col = MagicMock()
        col.query.return_value = {
            "ids": [["open-x-chat"]],
            "documents": [["doc text"]],
            "metadatas": [[{"name": "open-x-chat", "description": "desc"}]],
        }
        with patch.object(sm, "_collection", return_value=col):
            out = sm.retrieve_skills("how do I open X", k=2)
        assert out == [{"name": "open-x-chat", "description": "desc", "document": "doc text"}]
        col.query.assert_called_once()

    def test_empty_query_returns_empty(self):
        assert sm.retrieve_skills("  ") == []

    def test_noop_when_store_unavailable(self):
        with patch.object(sm, "_collection", return_value=None):
            assert sm.retrieve_skills("q") == []

    def test_never_raises_on_query_error(self):
        col = MagicMock()
        col.query.side_effect = Exception("boom")
        with patch.object(sm, "_collection", return_value=col):
            assert sm.retrieve_skills("q") == []


class TestCombinedSaver:
    def test_saves_to_file_and_indexes(self):
        from app.services import teacher_hook as th
        saved = {}
        with patch("app.teacher_escalation.FileSkillStore") as Store, \
             patch.object(sm, "index_skill") as idx:
            Store.return_value.save.side_effect = lambda s: saved.update(s) or True
            ok = th._combined_skill_saver(_SKILL)
        assert ok is True
        assert saved["name"] == "open-x-chat"   # file store got the skill
        idx.assert_called_once_with(_SKILL)      # and it was indexed

    def test_index_failure_does_not_block_file_save(self):
        from app.services import teacher_hook as th
        with patch("app.teacher_escalation.FileSkillStore") as Store, \
             patch.object(sm, "index_skill", side_effect=Exception("idx down")):
            Store.return_value.save.return_value = True
            ok = th._combined_skill_saver(_SKILL)
        assert ok is True  # file save success returned despite index error
