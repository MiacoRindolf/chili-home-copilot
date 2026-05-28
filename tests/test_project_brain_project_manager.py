from __future__ import annotations

from app.models import PORequirement
from app.services.project_brain.agents.project_manager import _mark_requirements_in_planner


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_calls = 0

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0
        self.last_query = None
        self.query_calls = 0

    def query(self, model):
        assert model is PORequirement
        self.query_calls += 1
        self.last_query = _FakeQuery(self._rows)
        return self.last_query

    def commit(self):
        self.commits += 1


def test_mark_requirements_in_planner_batches_lookup_and_commit():
    rows = [
        PORequirement(id=1, status="draft"),
        PORequirement(id=2, status="ready"),
        PORequirement(id=3, status="done"),
    ]
    db = _FakeSession(rows)

    updated = _mark_requirements_in_planner(
        db,
        [
            {"id": 1},
            {"id": 2},
            {"id": 3},
            {"id": "missing"},
            {"bad": "row"},
        ],
    )

    assert updated == 2
    assert rows[0].status == "in_planner"
    assert rows[1].status == "in_planner"
    assert rows[2].status == "done"
    assert db.query_calls == 1
    assert db.commits == 1


def test_mark_requirements_in_planner_skips_empty_ids():
    db = _FakeSession([])

    assert _mark_requirements_in_planner(db, [{"bad": "row"}]) == 0
    assert db.query_calls == 0
    assert db.commits == 0
