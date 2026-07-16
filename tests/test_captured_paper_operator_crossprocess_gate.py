from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import automation_query as query


_OWNER_KEY = "captured_paper_session_owner"
_PREOWNER_KEY = "captured_paper_session_preowner"
_PENDING_OWNER_KEY = "captured_paper_session_pending_owner"


class _Query:
    def __init__(self, db, row):
        self._db = db
        self._row = row

    def filter(self, *_args):
        return self

    def with_for_update(self, **kwargs):
        self._db.calls.append(("row_lock", dict(kwargs)))
        return self

    def populate_existing(self):
        self._db.calls.append(("populate_existing", None))
        return self

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, *rows):
        self._rows = list(rows)
        self._query_index = 0
        self.calls: list[tuple[str, object]] = []
        self.flush_calls = 0

    def in_transaction(self):
        return True

    def query(self, *_args):
        index = min(self._query_index, len(self._rows) - 1)
        self._query_index += 1
        self.calls.append(("query", index))
        return _Query(self, self._rows[index])

    def execute(self, statement, params):
        self.calls.append(("execute", (str(statement), dict(params))))
        return SimpleNamespace()

    def flush(self):
        self.flush_calls += 1
        self.calls.append(("flush", None))


def _session(
    *,
    owner=...,
    owner_key=_OWNER_KEY,
    state=query.STATE_WATCHING_LIVE,
):
    snapshot = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": "d7cc580c-2b8f-432f-b771-1cecfb3fe87a",
    }
    if owner is not ...:
        snapshot[owner_key] = deepcopy(owner)
    return SimpleNamespace(
        id=41,
        user_id=7,
        mode="live",
        symbol="ACTU",
        execution_family="alpaca_spot",
        state=state,
        risk_snapshot_json=snapshot,
        correlation_id="captured-paper-operator-gate",
        updated_at=None,
        ended_at=None,
    )


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(query, "_tables_present", lambda _db: True)
    events: list[tuple] = []
    monkeypatch.setattr(
        query,
        "append_trading_automation_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        query,
        "tick_live_session",
        lambda *_args, **_kwargs: pytest.fail(
            "captured owner control must not invoke the live runner"
        ),
    )
    monkeypatch.setattr(
        query,
        "_flatten_live_session_for_stop",
        lambda *_args, **_kwargs: pytest.fail(
            "captured owner control must not contact broker/terminalizer paths"
        ),
    )
    return events


def _assert_canonical_locks_before_row(db: _Db) -> None:
    actions = [name for name, _value in db.calls]
    execute_indexes = [i for i, name in enumerate(actions) if name == "execute"]
    row_index = actions.index("row_lock")
    assert len(execute_indexes) == 2
    assert execute_indexes[0] < execute_indexes[1] < row_index
    statements = [
        value[0]
        for name, value in db.calls
        if name == "execute"
    ]
    assert statements == [
        "SELECT pg_advisory_xact_lock(:key)",
        "SELECT pg_advisory_xact_lock(:namespace, hashtext(:account_scope))",
    ]


def test_run_rejects_owned_session_without_mutation_or_tick() -> None:
    row = _session(owner={"partial": True})
    before = deepcopy(row.risk_snapshot_json)
    db = _Db(row, row)

    result = query.run_automation_session(db, user_id=7, session_id=41)

    assert result["error"] == "captured_paper_session_owned_by_isolated_runtime"
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    assert row.risk_snapshot_json == before
    assert db.flush_calls == 0
    _assert_canonical_locks_before_row(db)


def test_null_owner_marker_still_fail_closes_and_pause_is_durable() -> None:
    row = _session(owner=None)
    db = _Db(row, row)

    result = query.pause_automation_session(db, user_id=7, session_id=41)

    assert result["ok"] is True
    assert result["captured_paper_deferred"] is True
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    assert row.risk_snapshot_json[_OWNER_KEY] is None
    assert row.risk_snapshot_json["operator_pause"]["active"] is True
    assert db.flush_calls == 1
    _assert_canonical_locks_before_row(db)


def test_partial_preowner_marker_blocks_run_before_initial_owner_promotion() -> None:
    row = _session(owner=None, owner_key=_PREOWNER_KEY, state="captured_paper_preowner")
    before = deepcopy(row.risk_snapshot_json)
    db = _Db(row, row)

    result = query.run_automation_session(db, user_id=7, session_id=41)

    assert result["error"] == "captured_paper_session_owned_by_isolated_runtime"
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    assert row.risk_snapshot_json == before
    _assert_canonical_locks_before_row(db)


def test_null_pending_owner_marker_blocks_run_before_final_owner_bind() -> None:
    row = _session(
        owner=None,
        owner_key=_PENDING_OWNER_KEY,
        state=query.STATE_QUEUED_LIVE,
    )
    before = deepcopy(row.risk_snapshot_json)
    db = _Db(row, row)

    result = query.run_automation_session(db, user_id=7, session_id=41)

    assert result["error"] == "captured_paper_session_owned_by_isolated_runtime"
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    assert row.risk_snapshot_json == before
    _assert_canonical_locks_before_row(db)


def test_operator_cancel_preowner_is_deferred_not_legacy_terminalized() -> None:
    row = _session(
        owner={"schema_version": "chili.captured-paper-session-preowner.v1"},
        owner_key=_PREOWNER_KEY,
        state="captured_paper_preowner",
    )
    db = _Db(row, row)

    result = query.cancel_automation_session(
        db,
        user_id=7,
        session_id=41,
        cancelled_by="operator",
    )

    assert result["ok"] is True
    assert result["terminalization_deferred"] is True
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    assert row.state == "captured_paper_preowner"
    assert row.ended_at is None
    assert row.risk_snapshot_json["operator_pause"]["active"] is True
    live_exec = row.risk_snapshot_json[query.KEY_LIVE_EXEC]
    assert live_exec["operator_flatten_requested_utc"]
    assert live_exec["operator_cancel_reconcile_requested_utc"]
    _assert_canonical_locks_before_row(db)


@pytest.mark.parametrize("action", ["stop", "cancel"])
def test_operator_stop_cancel_only_publish_deferred_emergency_intent(action) -> None:
    row = _session(owner={"schema_version": "partial"})
    initial_state = row.state
    db = _Db(row, row)

    if action == "stop":
        result = query.stop_automation_session(db, user_id=7, session_id=41)
    else:
        result = query.cancel_automation_session(
            db,
            user_id=7,
            session_id=41,
            cancelled_by="operator",
        )

    assert result["ok"] is True
    assert result["terminalization_deferred"] is True
    assert result["pending"] == "captured_paper_isolated_emergency_service"
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    assert row.state == initial_state
    assert row.ended_at is None
    live_exec = row.risk_snapshot_json[query.KEY_LIVE_EXEC]
    assert live_exec["operator_flatten_requested_utc"]
    assert live_exec[f"operator_{action}_reconcile_requested_utc"]
    assert live_exec[f"operator_{action}_requested"] is True
    assert row.risk_snapshot_json["operator_pause"]["active"] is True
    assert db.flush_calls == 1
    _assert_canonical_locks_before_row(db)


def test_automated_cancel_cannot_mutate_owned_session() -> None:
    row = _session(owner={"schema_version": "partial"})
    before = deepcopy(row.risk_snapshot_json)
    db = _Db(row)

    result = query.cancel_automation_session(
        db,
        user_id=7,
        session_id=41,
        cancelled_by="automation_monitor",
    )

    assert result == {
        "ok": False,
        "error": "captured_paper_session_owned_by_isolated_runtime",
        "session_id": 41,
        "broker_calls": 0,
        "order_posted": False,
    }
    assert row.risk_snapshot_json == before
    assert db.flush_calls == 0
    assert all(name != "execute" for name, _value in db.calls)


def test_owner_bound_after_preliminary_read_never_falls_through_to_runner() -> None:
    initially_unowned = _session()
    subsequently_owned = _session(owner=None)
    db = _Db(initially_unowned, subsequently_owned)

    result = query.run_automation_session(db, user_id=7, session_id=41)

    assert result == {
        "ok": False,
        "error": "captured_paper_session_owner_raced_operator_control_retry",
        "session_id": 41,
    }
    assert db.flush_calls == 0
    assert any(name == "row_lock" for name, _value in db.calls)


def test_operator_flatten_is_deferred_without_direct_service_call() -> None:
    row = _session(owner={}, state=query.STATE_LIVE_ENTERED)
    db = _Db(row, row)

    result = query.request_flatten_session(db, user_id=7, session_id=41)

    assert result["ok"] is True
    assert result["captured_paper_deferred"] is True
    assert result["broker_calls"] == 0
    assert result["order_posted"] is False
    live_exec = row.risk_snapshot_json[query.KEY_LIVE_EXEC]
    assert live_exec["operator_flatten_requested_utc"]
    assert live_exec["operator_flatten_reconcile_requested_utc"]
    assert row.risk_snapshot_json["operator_pause"]["active"] is True
    _assert_canonical_locks_before_row(db)
