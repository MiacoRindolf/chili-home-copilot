from __future__ import annotations

from app.services.code_brain import event_bus, trigger_watcher


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _WatcherDb:
    def __init__(self, *, task_rows=(), validation_rows=(), queued_ids=(), table_exists=True):
        self.task_rows = list(task_rows)
        self.validation_rows = list(validation_rows)
        self.queued_ids = set(queued_ids)
        self.table_exists = table_exists
        self.calls: list[dict] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        self.calls.append({"sql": sql, "params": params})
        if "FROM plan_tasks" in sql:
            return _Rows(self.task_rows)
        if "information_schema.tables" in sql:
            return _Rows([(1,)] if self.table_exists else [])
        if "FROM coding_task_validation_run" in sql:
            return _Rows(self.validation_rows)
        if "FROM code_brain_events" in sql and "subject_id = ANY(:ids)" in sql:
            return _Rows([(task_id,) for task_id in params.get("ids", []) if task_id in self.queued_ids])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_watch_plan_tasks_batches_existing_event_lookup(monkeypatch) -> None:
    enqueued: list[int] = []

    def fake_enqueue(_db, *, subject_id, **_kwargs):
        enqueued.append(subject_id)
        return 100 + subject_id

    monkeypatch.setattr(event_bus, "enqueue", fake_enqueue)
    db = _WatcherDb(
        task_rows=[(1, "one"), (2, "two"), (3, "three")],
        queued_ids={2},
    )

    assert trigger_watcher.watch_plan_tasks(db) == 2

    event_lookup_calls = [
        call for call in db.calls
        if "FROM code_brain_events" in call["sql"]
    ]
    assert len(event_lookup_calls) == 1
    assert event_lookup_calls[0]["params"]["ids"] == [1, 2, 3]
    assert enqueued == [1, 3]


def test_watch_validation_failures_batches_existing_event_lookup(monkeypatch) -> None:
    enqueued: list[int] = []

    def fake_enqueue(_db, *, subject_id, **_kwargs):
        enqueued.append(subject_id)
        return 200 + subject_id

    monkeypatch.setattr(event_bus, "enqueue", fake_enqueue)
    db = _WatcherDb(
        validation_rows=[(10,), (11,), (12,)],
        queued_ids={10, 12},
    )

    assert trigger_watcher.watch_validation_failures(db) == 1

    event_lookup_calls = [
        call for call in db.calls
        if "FROM code_brain_events" in call["sql"]
    ]
    assert len(event_lookup_calls) == 1
    assert event_lookup_calls[0]["params"]["ids"] == [10, 11, 12]
    assert enqueued == [11]
