"""Same-query observations, not captured prefixes or executable replay inputs."""
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import held_evaluation_audit as audit
from app.services.trading.momentum_neural import optional_db_read as reads
from tests.test_tape_selection_contract import AT, put, seed, tape_connection


@contextmanager
def collecting(role="G"):
    owner = SimpleNamespace(role=role, database="temporary_test_connection", reads=[], roles={}, coverage_errors=[])
    token = audit._ACTIVE.set(owner)
    try:
        yield owner
    finally:
        audit._ACTIVE.reset(token)


def test_same_query_ids_clocks_and_original_five_column_math(tape_connection, monkeypatch):
    seed(tape_connection)
    source_queries = []
    event.listen(tape_connection, "before_cursor_execute",
                 lambda conn, cursor, statement, params, ctx, many:
                 source_queries.append(statement) if "FROM iqfeed_trade_ticks" in statement else None)
    seen = []
    original_math = eg._signed_tape_features

    def math(rows, **kwargs):
        seen.append(list(rows))
        assert all(len(row) == 5 for row in rows)
        return original_math(rows, **kwargs)

    monkeypatch.setattr(eg, "_signed_tape_features", math)
    with Session(bind=tape_connection) as db:
        untraced = eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4,
                                               feature_contract="legacy_time_split")
        with collecting() as owner:
            traced = eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4,
                                                feature_contract="legacy_time_split")
    assert traced == untraced and seen[0] == seen[1]
    assert len(source_queries) == 2  # one per call, never a receipt reread
    record, = owner.reads
    assert record["ordered_ids"] == [3, 4, 5, 6]
    assert record["returned_count"] == 4
    assert record["feature_contract"] == "legacy_time_split"
    assert record["math_input_sha256"] == audit._hash_rows(seen[0], audit._FEATURE_COLS)
    assert record["first_row_clocks"][0] == 3
    assert record["last_row_clocks"] == [6, (AT-timedelta(seconds=1)).isoformat(), (AT-timedelta(seconds=1)).isoformat(), AT.isoformat()]
    assert record["requested_at_utc"] <= record["returned_at_utc"]
    assert record["elapsed_ms"] >= 0 and record["audit_hash_elapsed_ms"] >= 0
    assert record["captured_read"] is False and record["source_values_archived"] is False
    assert record["source_generation_identity"] is None


def test_after_fetch_insertion_cannot_change_recorded_membership(tape_connection, monkeypatch):
    seed(tape_connection)
    original = audit.QueryObservation.returned

    def after_fetch(self, rows=None, error=None):
        if error is None:
            put(tape_connection, 800, -.001, received=AT, available=AT)
        return original(self, rows=rows, error=error)

    monkeypatch.setattr(audit.QueryObservation, "returned", after_fetch)
    with Session(bind=tape_connection) as db, collecting() as owner:
        result = eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4)
    assert owner.reads[0]["ordered_ids"] == [3, 4, 5, 6]
    assert result["last_print"] == 6
    assert tape_connection.execute(text("SELECT count(*) FROM iqfeed_trade_ticks WHERE id=800")).scalar() == 1


def test_entry_base_and_g_keep_distinct_event_and_delivery_bounds(tape_connection):
    seed(tape_connection)
    with Session(bind=tape_connection) as db, collecting("entry_base") as owner:
        eg.signed_tape_accel_features("ABC", db=db, as_of=AT-timedelta(seconds=2),
                                     available_by=AT, window_prints=4)
        audit.role("G")
        eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4)
    base, current = owner.reads
    assert base["read_id"] != current["read_id"]
    assert base["ordered_ids"] == [2, 3, 4, 5] and current["ordered_ids"] == [3, 4, 5, 6]
    assert base["query_bounds"]["as_of"] != current["query_bounds"]["as_of"]
    assert base["query_bounds"]["available_by"] == current["query_bounds"]["available_by"]


def test_n_payload_bounded_and_walk_d_never_inline_membership():
    row = (10., 100., 9.99, 10., AT.timestamp(), 1, AT, AT, AT)
    with collecting() as owner:
        exact = audit.query_observation({}, exact_n=255)
        exact.requested()
        exact.returned(rows=[(*row[:5], n, *row[6:]) for n in range(255)])
        audit.role("D")
        digest = audit.query_observation({"as_of": AT})
        digest.requested()
        digest.returned(rows=[(*row[:5], AT, n) for n in range(10000)])
    n, d = owner.reads
    assert len(n["ordered_ids"]) == 255 and len(json.dumps(n)) < 6500
    assert d["membership_recovery"] == "digest_only" and d["returned_count"] == 10000
    assert "ordered_ids" not in d and len(json.dumps(d)) < 2500
    assert d["first_event_id"][1] == 0 and d["last_event_id"][1] == 9999


@pytest.mark.parametrize("stage", ["execute", "fetch"])
@pytest.mark.parametrize("broken_audit", [False, True])
def test_timing_hooks_preserve_original_failure_and_savepoint_owner(stage, broken_audit):
    order = []
    failure = RuntimeError(stage)

    class DB:
        @contextmanager
        def begin_nested(self):
            order.append("savepoint")
            try:
                yield
            except RuntimeError as exc:
                assert exc is failure
                order.append("rollback_original")
                raise

        def execute(self, *args):
            order.append("execute")
            if stage == "execute":
                raise failure
            return self

        def fetchall(self):
            order.append("fetch")
            raise failure

    class Sink:
        def requested(self):
            order.append("requested")
            if broken_audit:
                raise ValueError("audit only")

        def returned(self, error=None):
            assert error is failure
            order.append("returned_original")
            if broken_audit:
                raise ValueError("audit only")

    with pytest.raises(RuntimeError) as caught:
        reads.optional_fetchall(DB(), "select", audit=Sink())
    assert caught.value is failure
    assert order[:3] == ["savepoint", "requested", "execute"]
    assert order[-2:] == ["returned_original", "rollback_original"]


def test_broken_observation_hook_does_not_change_successful_rows(tape_connection, monkeypatch):
    seed(tape_connection)
    monkeypatch.setattr(audit.QueryObservation, "returned", lambda *a, **k: (_ for _ in ()).throw(ValueError("audit only")))
    with Session(bind=tape_connection) as db, collecting() as owner:
        result = eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4)
        assert db.execute(text("SELECT 42")).scalar() == 42
    assert result["last_print"] == 6
    assert owner.reads[0]["membership_recovery"] == "unavailable"
    assert owner.coverage_errors == ["query_observation_ValueError"]


def test_real_source_failure_still_unreadable_and_outer_connection_usable(tape_connection):
    tape_connection.execute(text("ALTER TABLE iqfeed_trade_ticks DROP COLUMN available_at"))
    with Session(bind=tape_connection) as db, collecting() as owner:
        assert eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4) is None
        assert db.execute(text("SELECT 42")).scalar() == 42
    record, = owner.reads
    assert record["status"] == "error" and record["error_type"] == "ProgrammingError"
    assert record["requested_at_utc"] <= record["returned_at_utc"]
    assert record["returned_count"] is None and "ordered_ids" not in record
