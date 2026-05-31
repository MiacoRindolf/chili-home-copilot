from __future__ import annotations

import json
from types import SimpleNamespace

from app.services import brain_worker_signals as bws


class _FakeDb:
    def __init__(self, row=None):
        self.row = row
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def get(self, _model, _pk):
        return self.row

    def add(self, row):
        self.added.append(row)
        self.row = row

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_persist_learning_live_json_skips_unchanged_commit() -> None:
    payload = {"running": True, "phase": "mining"}
    row = SimpleNamespace(
        learning_live_json=json.dumps(payload, default=str),
        updated_at="unchanged",
    )
    db = _FakeDb(row)

    bws.persist_learning_live_json(db, payload)

    assert db.commits == 0
    assert row.updated_at == "unchanged"


def test_persist_learning_live_json_commits_changed_payload() -> None:
    row = SimpleNamespace(learning_live_json=json.dumps({"phase": "old"}), updated_at=None)
    db = _FakeDb(row)

    bws.persist_learning_live_json(db, {"phase": "new"})

    assert db.commits == 1
    assert json.loads(row.learning_live_json) == {"phase": "new"}
    assert row.updated_at is not None


def test_persist_control_json_adds_missing_row_once() -> None:
    db = _FakeDb()

    bws.persist_last_cycle_digest_json(db, {"cycle": 1})

    assert db.commits == 1
    assert len(db.added) == 1
    assert json.loads(db.added[0].last_cycle_digest_json) == {"cycle": 1}


def test_set_wake_requested_skips_already_queued_write() -> None:
    row = SimpleNamespace(wake_requested=True, updated_at="unchanged")
    db = _FakeDb(row)

    bws.set_wake_requested(db)

    assert row.wake_requested is True
    assert row.updated_at == "unchanged"
    assert db.commits == 0


def test_set_stop_requested_skips_already_queued_write() -> None:
    row = SimpleNamespace(stop_requested=True, updated_at="unchanged")
    db = _FakeDb(row)

    bws.set_stop_requested(db)

    assert row.stop_requested is True
    assert row.updated_at == "unchanged"
    assert db.commits == 0


def test_clear_stop_requested_skips_already_cleared_write() -> None:
    row = SimpleNamespace(stop_requested=False, updated_at="unchanged")
    db = _FakeDb(row)

    bws.clear_stop_requested(db)

    assert row.stop_requested is False
    assert row.updated_at == "unchanged"
    assert db.commits == 0


def test_clear_worker_heartbeat_skips_already_cleared_write() -> None:
    row = SimpleNamespace(last_heartbeat_at=None, updated_at="unchanged")
    db = _FakeDb(row)

    bws.clear_worker_heartbeat(db)

    assert row.last_heartbeat_at is None
    assert row.updated_at == "unchanged"
    assert db.commits == 0
