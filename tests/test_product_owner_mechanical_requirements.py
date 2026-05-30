from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.project_brain import POQuestion, PORequirement
from app.services.project_brain.agents import product_owner
from app.services.project_brain.agents.product_owner import ProductOwnerAgent


def test_mechanical_requirements_from_explicit_questions():
    reqs = product_owner._mechanical_requirements_from_questions([
        SimpleNamespace(
            id=11,
            category="features",
            question="Which feature matters most?",
            answer="Offline portfolio review must work on mobile",
        ),
        SimpleNamespace(
            id=12,
            category="general",
            question="Anything else?",
            answer="not sure",
        ),
    ])

    assert len(reqs) == 1
    assert reqs[0]["title"] == "Support Offline portfolio review must work on mobile"
    assert reqs[0]["priority"] == "high"
    assert reqs[0]["source_question_ids"] == [11]


def test_mechanical_requirements_skip_ambiguous_answers():
    reqs = product_owner._mechanical_requirements_from_questions([
        SimpleNamespace(
            id=13,
            category="features",
            question="What should we build?",
            answer="not sure yet, maybe dashboards later",
        )
    ])

    assert reqs == []


def test_synthesize_requirements_uses_mechanical_path_without_llm(monkeypatch):
    llm = MagicMock(side_effect=AssertionError("explicit PO answers should not call LLM"))
    monkeypatch.setattr(product_owner, "call_llm", llm)

    question = SimpleNamespace(
        id=21,
        user_id=1,
        status="answered",
        category="success_criteria",
        question="How do we know launch succeeded?",
        answer="Activation must reach 40 percent in the first week",
    )
    added: list[PORequirement] = []

    class Query:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return self._rows

    class Db:
        def query(self, model):
            if model is POQuestion:
                return Query([question])
            if model is PORequirement:
                return Query([])
            return Query([])

        def add(self, obj):
            added.append(obj)

        def commit(self):
            return None

    count = ProductOwnerAgent()._synthesize_requirements(Db(), user_id=1)

    assert count == 1
    assert len(added) == 1
    assert added[0].priority == "high"
    assert "Activation must reach 40 percent" in added[0].description
    llm.assert_not_called()
