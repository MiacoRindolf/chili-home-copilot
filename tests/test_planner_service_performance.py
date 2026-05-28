from __future__ import annotations

from types import SimpleNamespace

from app.services import planner_service


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.group_by_calls = 0
        self.outerjoin_calls = 0

    def outerjoin(self, *_args, **_kwargs):
        self.outerjoin_calls += 1
        return self

    def group_by(self, *_args, **_kwargs):
        self.group_by_calls += 1
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.query_calls = 0

    def query(self, *_args, **_kwargs):
        self.query_calls += 1
        self.last_query = _FakeQuery(self._rows)
        return self.last_query


def test_all_users_task_summary_batches_across_users():
    db = _FakeSession([
        SimpleNamespace(
            user_id=1,
            user_name="Alice",
            project_count=2,
            total_tasks=5,
            done_tasks=3,
            overdue_tasks=1,
        ),
        SimpleNamespace(
            user_id=2,
            user_name="Bob",
            project_count=0,
            total_tasks=0,
            done_tasks=0,
            overdue_tasks=0,
        ),
    ])

    result = planner_service.get_all_users_task_summary(db)

    assert result == [
        {
            "user_id": 1,
            "user_name": "Alice",
            "project_count": 2,
            "total_tasks": 5,
            "done_tasks": 3,
            "overdue_tasks": 1,
        },
        {
            "user_id": 2,
            "user_name": "Bob",
            "project_count": 0,
            "total_tasks": 0,
            "done_tasks": 0,
            "overdue_tasks": 0,
        },
    ]
    assert db.query_calls == 1
    assert db.last_query.outerjoin_calls == 2
    assert db.last_query.group_by_calls == 1
