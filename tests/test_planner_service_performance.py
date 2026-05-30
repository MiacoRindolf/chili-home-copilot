from __future__ import annotations

from types import SimpleNamespace

from app.services import planner_service


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_calls = 0
        self.group_by_calls = 0
        self.join_calls = 0
        self.options_calls = 0
        self.outerjoin_calls = 0
        self.one_or_none_calls = 0
        self.first_calls = 0

    def join(self, *_args, **_kwargs):
        self.join_calls += 1
        return self

    def options(self, *_args, **_kwargs):
        self.options_calls += 1
        return self

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return self

    def outerjoin(self, *_args, **_kwargs):
        self.outerjoin_calls += 1
        return self

    def group_by(self, *_args, **_kwargs):
        self.group_by_calls += 1
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        self.one_or_none_calls += 1
        return self._rows[0] if self._rows else None

    def first(self):
        self.first_calls += 1
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.query_calls = 0

    def query(self, *_args, **_kwargs):
        self.query_calls += 1
        self.last_query = _FakeQuery(self._rows)
        return self.last_query


def _project_for_list(project_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=project_id,
        user_id=7,
        key="PRJ",
        name="Project",
        description="",
        status="active",
        color="#6366f1",
        start_date=None,
        end_date=None,
        created_at=None,
        updated_at=None,
        tasks=[
            SimpleNamespace(id=1, status="done"),
            SimpleNamespace(id=2, status="todo"),
        ],
        members=[],
        labels=[],
    )


def _task_for_detail(task_id: int = 1, *, subtasks: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    assignee = SimpleNamespace(name="Alice")
    reporter = SimpleNamespace(name="Reporter")
    return SimpleNamespace(
        id=task_id,
        project_id=1,
        parent_id=None,
        title="Do it",
        description="",
        status="todo",
        priority="high",
        start_date=None,
        end_date=None,
        assigned_to=7,
        assignee=assignee,
        reporter_id=8,
        reporter=reporter,
        depends_on=None,
        progress=0,
        sort_order=0,
        created_at=None,
        updated_at=None,
        task_labels=[],
        watchers=[],
        subtasks=subtasks or [],
        coding_workflow_mode="tracked",
        coding_readiness_state="not_started",
    )


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


def test_list_projects_eager_loads_summary_relationships():
    db = _FakeSession([_project_for_list()])

    result = planner_service.list_projects(db, user_id=7)

    assert result[0]["task_count"] == 2
    assert result[0]["done_count"] == 1
    assert db.query_calls == 1
    assert db.last_query.join_calls == 1
    assert db.last_query.options_calls == 1
    assert db.last_query.filter_calls == 1


def test_list_all_projects_eager_loads_summary_relationships():
    db = _FakeSession([_project_for_list()])

    result = planner_service.list_all_projects(db)

    assert result[0]["task_count"] == 2
    assert result[0]["done_count"] == 1
    assert db.query_calls == 1
    assert db.last_query.options_calls == 1


def test_get_project_eager_loads_detail_relationships(monkeypatch):
    project = _project_for_list()
    project.tasks = [_task_for_detail()]
    db = _FakeSession([project])
    monkeypatch.setattr(planner_service, "_user_can_access", lambda *_args, **_kwargs: True)

    result = planner_service.get_project(db, project_id=1, user_id=7)

    assert result is not None
    assert result["tasks"][0]["title"] == "Do it"
    assert result["tasks"][0]["assignee_name"] == "Alice"
    assert result["tasks"][0]["reporter_name"] == "Reporter"
    assert db.query_calls == 1
    assert db.last_query.options_calls == 1
    assert db.last_query.filter_calls == 1
    assert db.last_query.first_calls == 1


def test_list_tasks_eager_loads_task_detail_relationships(monkeypatch):
    db = _FakeSession([_task_for_detail()])
    monkeypatch.setattr(planner_service, "_user_can_access", lambda *_args, **_kwargs: True)

    result = planner_service.list_tasks(db, project_id=1, user_id=7)

    assert result[0]["title"] == "Do it"
    assert result[0]["assignee_name"] == "Alice"
    assert result[0]["reporter_name"] == "Reporter"
    assert db.query_calls == 1
    assert db.last_query.options_calls == 1
    assert db.last_query.filter_calls == 1


def test_list_tasks_filtered_eager_loads_task_detail_relationships(monkeypatch):
    db = _FakeSession([_task_for_detail()])
    monkeypatch.setattr(planner_service, "_user_can_access", lambda *_args, **_kwargs: True)

    result = planner_service.list_tasks_filtered(db, project_id=1, user_id=7)

    assert result[0]["title"] == "Do it"
    assert result[0]["assignee_name"] == "Alice"
    assert result[0]["reporter_name"] == "Reporter"
    assert db.query_calls == 1
    assert db.last_query.options_calls == 1
    assert db.last_query.filter_calls == 1


def test_get_task_eager_loads_task_detail_relationships(monkeypatch):
    db = _FakeSession([_task_for_detail()])
    monkeypatch.setattr(planner_service, "_user_can_access", lambda *_args, **_kwargs: True)

    result = planner_service.get_task(db, task_id=1, user_id=7)

    assert result is not None
    assert result["title"] == "Do it"
    assert result["assignee_name"] == "Alice"
    assert result["reporter_name"] == "Reporter"
    assert db.query_calls == 1
    assert db.last_query.options_calls == 1
    assert db.last_query.filter_calls == 1
    assert db.last_query.first_calls == 1


def test_user_project_summary_eager_loads_tasks_and_assignees():
    assignee = SimpleNamespace(name="Alice")
    project = SimpleNamespace(
        id=1,
        key="PRJ",
        name="Project",
        end_date=None,
        tasks=[
            SimpleNamespace(
                id=1,
                sort_order=0,
                status="todo",
                priority="high",
                end_date=None,
                assigned_to=7,
                assignee=assignee,
                title="Do it",
            ),
            SimpleNamespace(
                id=2,
                sort_order=1,
                status="done",
                priority="medium",
                end_date=None,
                assigned_to=None,
                assignee=None,
                title="Done",
            ),
        ],
    )
    db = _FakeSession([project])

    summary = planner_service.get_user_project_summary(db, user_id=7)

    assert "Project: Project (PRJ)" in summary
    assert "1/2 tasks done" in summary
    assert "- [todo] [high] Do it @Alice" in summary
    assert db.query_calls == 1
    assert db.last_query.join_calls == 1
    assert db.last_query.options_calls == 1
    assert db.last_query.filter_calls == 1


def test_user_task_summary_stats_aggregates_in_one_query():
    db = _FakeSession([
        SimpleNamespace(
            user_name="Alice",
            project_count=2,
            total_tasks=5,
            done_tasks=3,
            overdue_tasks=1,
        )
    ])

    result = planner_service.get_user_task_summary_stats(db, user_id=7)

    assert result == [
        {
            "user_id": 7,
            "user_name": "Alice",
            "project_count": 2,
            "total_tasks": 5,
            "done_tasks": 3,
            "overdue_tasks": 1,
        }
    ]
    assert db.query_calls == 1
    assert db.last_query.outerjoin_calls == 2
    assert db.last_query.filter_calls == 1
    assert db.last_query.group_by_calls == 1
    assert db.last_query.one_or_none_calls == 1


def test_user_task_summary_stats_handles_missing_user_without_followup_queries():
    db = _FakeSession([])

    result = planner_service.get_user_task_summary_stats(db, user_id=404)

    assert result == [
        {
            "user_id": 404,
            "user_name": "",
            "project_count": 0,
            "total_tasks": 0,
            "done_tasks": 0,
            "overdue_tasks": 0,
        }
    ]
    assert db.query_calls == 1
    assert db.last_query.one_or_none_calls == 1
