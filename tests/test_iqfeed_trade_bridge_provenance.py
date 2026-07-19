"""Focused IQFeed L1 v2 causal-provenance contract tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

import scripts.iqfeed_trade_bridge as bridge


def _frame(
    *,
    message_type: str = "Q",
    symbol: str = "ACTU",
    trade_time: str = "09:30:00.000000",
    bid: float = 1.47,
    ask: float = 1.48,
) -> str:
    # IQFeed protocol 6.2 default field positions used by the bridge:
    # Q/P,symbol,last,last-size,last-time,market-center,total-volume,bid,bid-size,ask,ask-size
    return (
        f"{message_type},{symbol},1.48,100,{trade_time},Q,10000,"
        f"{bid},100,{ask},100"
    )


def _reset_parser_state() -> None:
    with bridge._pending_lock:
        bridge._pending.clear()
        bridge._pending_nbbo.clear()
    bridge._last_trade.clear()
    bridge._last_nbbo_append_monotonic = None
    bridge.watched.clear()
    with bridge._connection_state_lock:
        bridge._active_connection_generation = 0
        bridge._frame_sequence_by_generation.clear()
        bridge._selected_fields_ack_sha256_by_generation.clear()
    with bridge._capture_handoff_lock:
        bridge._capture_handoff = None


@pytest.fixture(autouse=True)
def _isolated_bridge_parser(monkeypatch):
    _reset_parser_state()
    monkeypatch.setattr(bridge, "BRIDGE_RUN_ID", "12553525-2da8-4b22-a69f-d3034871e90c")
    yield
    _reset_parser_state()


def _reference_delta(monkeypatch, seconds: float | None) -> None:
    def _parse(_value, received_at):
        if seconds is None:
            return None
        return (received_at - timedelta(seconds=seconds)).replace(tzinfo=None)

    monkeypatch.setattr(bridge, "_trade_time_to_naive_utc", _parse)


def _migration_source_body(function_name: str) -> str:
    source = bridge.Path(
        __import__("app.migrations", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")
    start = source.index(f"def {function_name}")
    next_migration = source.find("\n\ndef _migration_", start + 1)
    end = (
        next_migration
        if next_migration >= 0
        else source.index("\n\nMIGRATIONS =", start)
    )
    return source[start:end]


def test_fresh_post_connect_q_is_the_only_authoritative_nbbo(monkeypatch):
    _reference_delta(monkeypatch, 0.25)

    bridge._parse_l1(_frame(), connection_generation=7)

    assert len(bridge._pending_nbbo) == 1
    row = bridge._pending_nbbo[0]
    assert row["message_type"] == "Q"
    assert row["connection_generation"] == 7
    assert row["bridge_run_id"] == bridge.BRIDGE_RUN_ID
    assert row["basis"] == bridge.AUTHORITATIVE_TIMESTAMP_BASIS
    assert row["provider_at"] is None
    assert row["provider_trade_reference_at"].tzinfo is not None
    assert row["received_at"].tzinfo is not None
    assert row["at"] == row["provider_trade_reference_at"].replace(tzinfo=None)
    assert row["source_frame_sequence"] == 1
    assert len(row["source_frame_sha256"]) == 64
    assert 0 <= (
        row["received_at"] - row["provider_trade_reference_at"]
    ).total_seconds() <= 2


def test_p_summary_never_writes_authoritative_nbbo_or_notify(monkeypatch):
    _reference_delta(monkeypatch, 0.1)

    bridge._parse_l1(_frame(message_type="P"), connection_generation=1)

    assert bridge._pending_nbbo == []
    assert bridge._pending == []


@pytest.mark.parametrize("reference_delta", [2.01, -1.01, None])
def test_stale_future_or_unparseable_q_cannot_create_authoritative_row(
    monkeypatch,
    reference_delta,
):
    _reference_delta(monkeypatch, reference_delta)

    bridge._parse_l1(_frame(), connection_generation=3)

    assert bridge._pending_nbbo == []
    # No generic live trade consumer may treat a stale/unparseable Q as fresh.
    assert bridge._pending == []


def test_preconnect_generation_cannot_create_any_row(monkeypatch):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(_frame(), connection_generation=0)
    assert bridge._pending_nbbo == []
    assert bridge._pending == []


@pytest.mark.parametrize("symbol", ["BTC-USD", "A/B", "ACTU.", "AC..TU"])
def test_non_equity_symbol_never_enters_bridge_queues(monkeypatch, symbol):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(_frame(symbol=symbol), connection_generation=1)
    assert bridge._pending_nbbo == []
    assert bridge._pending == []


def test_notify_payload_carries_complete_certified_tuple(monkeypatch):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(_frame(symbol="SOBR"), connection_generation=4)

    row = bridge._pending_nbbo[0]
    payload = json.loads(bridge._notify_payload(row))
    assert payload == {
        "available_at": None,
        "ask": 1.48,
        "bid": 1.47,
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "bridge_version": bridge.BRIDGE_BUILD,
        "connection_generation": 4,
        "message_type": "Q",
        "observed_at": payload["provider_trade_reference_at"],
        "provider_event_at": None,
        "provider_trade_reference_at": payload["provider_trade_reference_at"],
        "received_at": payload["received_at"],
        "source": "iqfeed_l1",
        "source_frame_sequence": 1,
        "source_frame_sha256": row["source_frame_sha256"],
        "symbol": "SOBR",
        "timestamp_basis": bridge.AUTHORITATIVE_TIMESTAMP_BASIS,
    }


def test_notify_payload_carries_the_release_stamp_used_by_the_commit(monkeypatch):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(
        _frame(symbol="AAPL", trade_time="08:31:15", bid=191.24, ask=191.26),
        connection_generation=1,
    )
    release_at = datetime(2026, 7, 14, 15, 31, 15, 250000, tzinfo=timezone.utc)
    bridge._pending_nbbo[0]["available_at"] = release_at

    payload = json.loads(bridge._notify_payload(bridge._pending_nbbo[0]))

    assert payload["available_at"] == release_at.isoformat()
    assert payload["bridge_run_id"] == bridge.BRIDGE_RUN_ID
    assert payload["connection_generation"] == 1


def test_release_update_is_bound_to_the_exact_batch_row(monkeypatch):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(_frame(symbol="AAPL"), connection_generation=3)
    row = bridge._pending_nbbo[0]
    release_at = datetime(2026, 7, 14, 15, 31, 16, tzinfo=timezone.utc)

    params = bridge._availability_params(row, available_at=release_at)

    assert params == {
        "available_at": release_at,
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "connection_generation": 3,
        "source_frame_sequence": row["source_frame_sequence"],
        "source_frame_sha256": row["source_frame_sha256"],
        "sym": "AAPL",
        "received_at": row["received_at"],
        "provider_trade_reference_at": row["provider_trade_reference_at"],
        "message_type": "Q",
    }
    sql = str(bridge.MARK_NBBO_AVAILABLE).lower()
    assert "symbol = :sym" in sql
    assert "received_at = :received_at" in sql
    assert "provider_trade_reference_at is not distinct from" in sql
    assert "source_frame_sequence = :source_frame_sequence" in sql
    assert "source_frame_sha256 = :source_frame_sha256" in sql


def test_release_identity_distinguishes_colliding_receive_and_reference_clocks(db):
    from app.migrations import _migration_333_iqfeed_source_frame_release_identity

    with db.get_bind().connect() as connection:
        _migration_333_iqfeed_source_frame_release_identity(connection)
    received_at = datetime(2026, 7, 15, 15, 31, 15, tzinfo=timezone.utc)
    reference_at = received_at - timedelta(milliseconds=1)
    released_at = received_at + timedelta(milliseconds=5)
    bridge_run_id = "12553525-2da8-4b22-a69f-d3034871e90c"
    row_ids: list[int] = []
    try:
        for sequence, digest in ((41, "a" * 64), (42, "b" * 64)):
            row_ids.append(db.execute(sa.text(
                "INSERT INTO momentum_nbbo_spread_tape ("
                " symbol, observed_at, bid, ask, source, received_at, "
                " provider_trade_reference_at, message_type, bridge_run_id, "
                " connection_generation, source_frame_sequence, "
                " source_frame_sha256, available_at"
                ") VALUES ("
                " 'M333IDENT', :observed_at, 4.11, 4.12, 'iqfeed_l1', "
                " :received_at, :reference_at, 'Q', :bridge_run_id, 9, "
                " :sequence, :digest, NULL"
                ") RETURNING id"
            ), {
                "observed_at": reference_at,
                "received_at": received_at,
                "reference_at": reference_at,
                "bridge_run_id": bridge_run_id,
                "sequence": sequence,
                "digest": digest,
            }).scalar_one())

        result = db.execute(bridge.MARK_NBBO_AVAILABLE, {
            "available_at": released_at,
            "bridge_run_id": bridge_run_id,
            "connection_generation": 9,
            "source_frame_sequence": 41,
            "source_frame_sha256": "a" * 64,
            "sym": "M333IDENT",
            "received_at": received_at,
            "provider_trade_reference_at": reference_at,
            "message_type": "Q",
        })
        assert result.rowcount == 1
        released = db.execute(sa.text(
            "SELECT source_frame_sequence, available_at "
            "FROM momentum_nbbo_spread_tape WHERE id = ANY(:row_ids) "
            "ORDER BY source_frame_sequence"
        ), {"row_ids": row_ids}).all()
        assert released == [(41, released_at), (42, None)]
    finally:
        db.rollback()
        if row_ids:
            db.execute(sa.text(
                "DELETE FROM momentum_nbbo_spread_tape WHERE id = ANY(:row_ids)"
            ), {"row_ids": row_ids})
            db.commit()


def test_hot_symbols_keep_every_quote_while_broad_symbols_keep_newest(monkeypatch):
    monkeypatch.setattr(bridge, "HOT_FULL_FIDELITY", True)
    rows = [
        {"sym": "HOT", "seq": 1},
        {"sym": "COLD", "seq": 2},
        {"sym": "HOT", "seq": 3},
        {"sym": "OTHER", "seq": 4},
        {"sym": "COLD", "seq": 5},
        {"sym": "HOT", "seq": 6},
    ]

    selected = bridge._select_nbbo_rows_for_capture(rows, hot_symbols={"hot"})

    assert [(row["sym"], row["seq"]) for row in selected] == [
        ("HOT", 1),
        ("HOT", 3),
        ("OTHER", 4),
        ("COLD", 5),
        ("HOT", 6),
    ]


def test_hot_full_fidelity_kill_switch_reverts_to_broad_sampling(monkeypatch):
    monkeypatch.setattr(bridge, "HOT_FULL_FIDELITY", False)
    rows = [
        {"sym": "HOT", "seq": 1},
        {"sym": "HOT", "seq": 2},
        {"sym": "COLD", "seq": 3},
        {"sym": "COLD", "seq": 4},
    ]

    selected = bridge._select_nbbo_rows_for_capture(rows, hot_symbols={"HOT"})

    assert [(row["sym"], row["seq"]) for row in selected] == [
        ("HOT", 2),
        ("COLD", 4),
    ]


def test_trade_reference_is_not_mislabeled_provider_event(monkeypatch):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(_frame(), connection_generation=2)

    trade = bridge._pending[0]
    quote = bridge._pending_nbbo[0]
    assert trade["provider_at"] is None
    assert quote["provider_at"] is None
    assert trade["provider_trade_reference_at"] is not None
    assert quote["provider_trade_reference_at"] is not None
    assert trade["source_frame_sequence"] == quote["source_frame_sequence"] == 1
    assert trade["source_frame_sha256"] == quote["source_frame_sha256"]


def test_released_capture_handoff_receives_exact_commit_clock_without_db_read():
    release_at = datetime(2026, 7, 15, 15, 31, 15, tzinfo=timezone.utc)

    class _Handoff:
        def __init__(self):
            self.calls = []

        def health(self):
            return {"started": True, "accepting": True}

        def offer_released_rows(self, **kwargs):
            self.calls.append(kwargs)
            return 2, 0

        def record_release_failure(self, **_kwargs):
            raise AssertionError("healthy fixture handoff cannot fail")

        def record_connection_boundary(self, **_kwargs):
            return None

    handoff = _Handoff()
    bridge.bind_capture_handoff(handoff)
    try:
        result = bridge._publish_released_capture_rows(
            trade_rows=[{"sym": "VEEE"}],
            quote_rows=[{"sym": "VEEE"}],
            available_at=release_at,
        )
    finally:
        bridge.unbind_capture_handoff(handoff)

    assert result == (2, 0)
    assert handoff.calls == [
        {
            "trade_rows": [{"sym": "VEEE"}],
            "quote_rows": [{"sym": "VEEE"}],
            "available_at": release_at,
        }
    ]


def test_unexpected_capture_handoff_error_is_contained_as_explicit_batch_loss():
    release_at = datetime(2026, 7, 15, 15, 31, 16, tzinfo=timezone.utc)

    class _FailingHandoff:
        def __init__(self):
            self.failure = None

        def health(self):
            return {"started": True, "accepting": True}

        def offer_released_rows(self, **_kwargs):
            raise RuntimeError("fixture handoff defect")

        def record_release_failure(self, **kwargs):
            self.failure = kwargs
            return len(kwargs["trade_rows"]) + len(kwargs["quote_rows"])

        def record_connection_boundary(self, **_kwargs):
            return None

    handoff = _FailingHandoff()
    bridge.bind_capture_handoff(handoff)
    try:
        result = bridge._publish_released_capture_rows(
            trade_rows=[{"sym": "VEEE"}],
            quote_rows=[{"sym": "VEEE"}],
            available_at=release_at,
        )
    finally:
        bridge.unbind_capture_handoff(handoff)

    assert result == (0, 2)
    assert handoff.failure == {
        "trade_rows": [{"sym": "VEEE"}],
        "quote_rows": [{"sym": "VEEE"}],
        "available_at": release_at,
    }


def test_capture_handoff_cannot_bind_or_unbind_mid_connection_generation():
    class _Handoff:
        def health(self):
            return {"started": True, "accepting": True}

        def offer_released_rows(self, **_kwargs):
            return (0, 0)

        def record_release_failure(self, **_kwargs):
            return 0

        def record_connection_boundary(self, **_kwargs):
            return None

    handoff = _Handoff()
    bridge._activate_connection_generation(91)
    try:
        with pytest.raises(RuntimeError, match="cannot bind mid-connection"):
            bridge.bind_capture_handoff(handoff)
    finally:
        bridge._retire_connection_generation(91)

    bridge.bind_capture_handoff(handoff)
    bridge._activate_connection_generation(92)
    try:
        with pytest.raises(RuntimeError, match="cannot unbind mid-connection"):
            bridge.unbind_capture_handoff(handoff)
    finally:
        bridge._retire_connection_generation(92)
    bridge.unbind_capture_handoff(handoff)


def test_old_reader_completion_cannot_stop_new_connection_generation():
    class _NeverReadSocket:
        def recv(self, _size):
            raise AssertionError("inactive old reader touched a rebound socket")

    old_stop = bridge.threading.Event()
    new_stop = bridge.threading.Event()
    bridge._activate_connection_generation(1)
    bridge._activate_connection_generation(2)

    bridge.reader(_NeverReadSocket(), old_stop, 1)

    assert old_stop.is_set() is False
    assert new_stop.is_set() is False
    assert bridge._connection_generation_active(2, new_stop) is True
    bridge._retire_connection_generation(2)


def test_connection_runner_closes_and_joins_reader_before_rebind(monkeypatch):
    class _BlockingSocket:
        def __init__(self):
            self.recv_entered = bridge.threading.Event()
            self.closed = bridge.threading.Event()
            self.reader_exited = bridge.threading.Event()
            self.sent = []

        def settimeout(self, _timeout):
            return None

        def sendall(self, payload):
            self.sent.append(payload)

        def recv(self, _size):
            self.recv_entered.set()
            assert self.closed.wait(timeout=2.0)
            self.reader_exited.set()
            return b""

        def shutdown(self, _how):
            self.closed.set()

        def close(self):
            self.closed.set()

    first = _BlockingSocket()
    second = _BlockingSocket()
    sockets = iter([first, second])
    create_count = {"value": 0}

    def _create_connection(_address, timeout):
        assert timeout == 10
        create_count["value"] += 1
        if create_count["value"] == 2:
            assert first.closed.is_set()
            assert first.reader_exited.is_set()
        return next(sockets)

    def _writer(_forced, _deadline, connection_socket, _stop, _generation):
        assert connection_socket.recv_entered.wait(timeout=2.0)

    monkeypatch.setattr(bridge.socket, "create_connection", _create_connection)
    monkeypatch.setattr(bridge, "writer", _writer)
    monkeypatch.setattr(bridge, "_wait_for_selected_fields_ack", lambda *_a, **_k: True)

    bridge._run_connection(set(), None)
    bridge._run_connection(set(), None)

    assert create_count["value"] == 2
    assert second.closed.is_set()
    assert second.reader_exited.is_set()


def test_nonquiescent_reader_refuses_reconnect(monkeypatch):
    class _StuckSocket:
        def __init__(self):
            self.recv_entered = bridge.threading.Event()
            self.release = bridge.threading.Event()
            self.exited = bridge.threading.Event()

        def settimeout(self, _timeout):
            return None

        def sendall(self, _payload):
            return None

        def recv(self, _size):
            self.recv_entered.set()
            try:
                self.release.wait(timeout=2.0)
                return b""
            finally:
                self.exited.set()

        def shutdown(self, _how):
            return None

        def close(self):
            return None

    stuck = _StuckSocket()
    monkeypatch.setattr(bridge.socket, "create_connection", lambda *_a, **_k: stuck)
    monkeypatch.setattr(bridge, "READER_JOIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(bridge, "_wait_for_selected_fields_ack", lambda *_a, **_k: True)
    monkeypatch.setattr(
        bridge,
        "writer",
        lambda *_args: stuck.recv_entered.wait(timeout=2.0),
    )
    try:
        with pytest.raises(bridge._ReaderQuiescenceError):
            bridge._run_connection(set(), None)
    finally:
        stuck.release.set()
        assert stuck.exited.wait(timeout=2.0)


def test_bridge_build_id_is_content_addressed_v2(tmp_path):
    source = tmp_path / "bridge.py"
    source.write_bytes(b"immutable bridge test\n")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    assert bridge._bridge_build_id(source) == (
        f"{bridge.BRIDGE_VERSION}+sha256:{expected}"
    )
    assert bridge.BRIDGE_VERSION.endswith("-v3")


def test_migration_315_adds_and_preserves_explicit_clocks_idempotently(db):
    from app.migrations import _migration_315_iqfeed_bridge_timestamp_provenance

    engine = db.get_bind()
    with engine.connect() as conn:
        _migration_315_iqfeed_bridge_timestamp_provenance(conn)

    columns = db.execute(sa.text(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "  AND table_name = 'momentum_nbbo_spread_tape' "
        "  AND column_name IN ("
        "    'provider_event_at', 'received_at', 'timestamp_basis', 'bridge_version'"
        "  )"
    )).fetchall()
    schema_by_name = {
        str(column_name): (str(data_type), str(is_nullable))
        for column_name, data_type, is_nullable in columns
    }
    assert schema_by_name == {
        "provider_event_at": ("timestamp with time zone", "YES"),
        "received_at": ("timestamp with time zone", "YES"),
        "timestamp_basis": ("character varying", "YES"),
        "bridge_version": ("character varying", "YES"),
    }

    provider_at = datetime(2026, 7, 13, 16, 3, 57, 73_291, tzinfo=timezone.utc)
    received_at = provider_at + timedelta(milliseconds=125)
    row_id = None
    try:
        row_id = db.execute(sa.text(
            "INSERT INTO momentum_nbbo_spread_tape ("
            "  symbol, observed_at, provider_event_at, received_at, "
            "  timestamp_basis, bridge_version, source"
            ") VALUES ("
            "  'M315CLOCK', :observed_at, :provider_at, :received_at, "
            "  'provider_event_plus_host_receive', 'migration-315-test', 'iqfeed_l1'"
            ") RETURNING id"
        ), {
            "observed_at": provider_at,
            "provider_at": provider_at,
            "received_at": received_at,
        }).scalar_one()
        db.commit()

        with engine.connect() as conn:
            _migration_315_iqfeed_bridge_timestamp_provenance(conn)

        preserved = db.execute(sa.text(
            "SELECT provider_event_at, received_at, timestamp_basis, bridge_version "
            "FROM momentum_nbbo_spread_tape WHERE id = :row_id"
        ), {"row_id": row_id}).one()
        assert preserved == (
            provider_at,
            received_at,
            "provider_event_plus_host_receive",
            "migration-315-test",
        )
    finally:
        db.rollback()
        if row_id is not None:
            db.execute(sa.text(
                "DELETE FROM momentum_nbbo_spread_tape WHERE id = :row_id"
            ), {"row_id": row_id})
            db.commit()


def test_migration_317_adds_nullable_v2_metadata_idempotently(db):
    from app.migrations import _migration_317_iqfeed_bridge_v2_causal_provenance

    _migration_317_iqfeed_bridge_v2_causal_provenance(db.connection())
    columns = db.execute(sa.text(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'momentum_nbbo_spread_tape'"
    )).fetchall()
    nullable_by_name = {str(row[0]): str(row[1]) for row in columns}
    assert {
        "provider_trade_reference_at",
        "message_type",
        "bridge_run_id",
        "connection_generation",
    }.issubset(nullable_by_name)
    assert all(
        nullable_by_name[name] == "YES"
        for name in (
            "provider_trade_reference_at",
            "message_type",
            "bridge_run_id",
            "connection_generation",
        )
    )

    _migration_317_iqfeed_bridge_v2_causal_provenance(db.connection())


def test_migration_317_is_metadata_only_without_backfill_or_index():
    body = _migration_source_body(
        "_migration_317_iqfeed_bridge_v2_causal_provenance"
    ).upper()
    assert "ADD COLUMN IF NOT EXISTS" in body
    assert "CREATE INDEX" not in body
    assert "UPDATE " not in body


def test_migration_318_adds_nullable_post_publication_clock_idempotently(db):
    from app.migrations import _migration_318_iqfeed_strategy_available_at

    _migration_318_iqfeed_strategy_available_at(db.connection())
    columns = db.execute(sa.text(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'momentum_nbbo_spread_tape' "
        "AND column_name = 'available_at'"
    )).one()

    assert columns == ("available_at", "timestamp with time zone", "YES")
    _migration_318_iqfeed_strategy_available_at(db.connection())


def test_migration_318_does_not_fabricate_history_or_build_index():
    body = _migration_source_body(
        "_migration_318_iqfeed_strategy_available_at"
    ).upper()

    assert "ADD COLUMN IF NOT EXISTS AVAILABLE_AT" in body
    assert "CREATE INDEX" not in body
    assert "UPDATE " not in body


def test_migration_333_adds_nullable_source_frame_identity_idempotently(db):
    from app.migrations import _migration_333_iqfeed_source_frame_release_identity

    _migration_333_iqfeed_source_frame_release_identity(db.connection())
    columns = db.execute(sa.text(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'momentum_nbbo_spread_tape' "
        "AND column_name IN ('source_frame_sequence', 'source_frame_sha256') "
        "ORDER BY column_name"
    )).all()

    assert columns == [
        ("source_frame_sequence", "bigint", "YES"),
        ("source_frame_sha256", "character varying", "YES"),
    ]
    _migration_333_iqfeed_source_frame_release_identity(db.connection())


def test_migration_333_does_not_fabricate_history_or_build_index():
    body = _migration_source_body(
        "_migration_333_iqfeed_source_frame_release_identity"
    ).upper()

    assert "SOURCE_FRAME_SEQUENCE" in body
    assert "SOURCE_FRAME_SHA256" in body
    assert "MOMENTUM_NBBO_SPREAD_TAPE" in body
    assert "IQFEED_TRADE_TICKS" in body
    assert "CREATE INDEX" not in body
    assert "UPDATE " not in body


def test_migration_334_owns_host_bridge_schema_idempotently(db):
    from app.migrations import _migration_334_iqfeed_host_bridge_schema_ownership

    with db.get_bind().connect() as connection:
        _migration_334_iqfeed_host_bridge_schema_ownership(connection)
    rows = db.execute(sa.text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name IN ("
        " 'iqfeed_trade_ticks', 'iqfeed_depth_snapshots', "
        " 'momentum_bridge_subscribe_requests', 'momentum_nbbo_spread_tape'"
        ")"
    )).all()
    columns_by_table: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        columns_by_table.setdefault(str(table_name), set()).add(str(column_name))

    assert bridge._TRADE_REQUIRED_COLUMNS <= columns_by_table["iqfeed_trade_ticks"]
    assert bridge._NBBO_REQUIRED_COLUMNS <= columns_by_table[
        "momentum_nbbo_spread_tape"
    ]
    assert bridge._SUBSCRIBE_REQUIRED_COLUMNS <= columns_by_table[
        "momentum_bridge_subscribe_requests"
    ]
    assert {"bids_json", "asks_json"} <= columns_by_table[
        "iqfeed_depth_snapshots"
    ]
    with db.get_bind().connect() as connection:
        _migration_334_iqfeed_host_bridge_schema_ownership(connection)


def test_migration_349_is_append_only_and_prefers_exact_update_identity():
    from app import migrations

    body = _migration_source_body(
        "_migration_349_iqfeed_availability_incident_quarantine"
    )
    upper = body.upper()

    assert "TICK.XMIN::TEXT AS UPDATE_XID" in upper
    assert "HAVING COUNT(*) = 176" in upper
    assert "POSTGRES_XMIN_EXACT_176" in upper
    assert "CONSERVATIVE_BRIDGE_RUN_WINDOW" in upper
    assert "CONSERVATIVE_INCIDENT_WINDOW_NO_RETAINED_IDENTITY" in upper
    assert "REQUIRE_AVAILABLE_EQUALS_OBSERVED" in upper
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE" in upper
    assert "FROM IQFEED_TRADE_TICKS AS TICK" in upper
    assert "INSERT INTO IQFEED_AVAILABILITY_QUARANTINES" in upper
    assert (
        "349_iqfeed_availability_incident_quarantine"
        in migrations.RETIRED_MIGRATIONS
    )
    assert "349_iqfeed_availability_incident_quarantine" not in {
        version_id for version_id, _function in migrations.MIGRATIONS
    }
    assert migrations.MIGRATIONS[-1][0].startswith("348_")
    with pytest.raises(RuntimeError, match="migration 349 is parked"):
        migrations._migration_349_iqfeed_availability_incident_quarantine(None)


def test_trade_bridge_startup_schema_gate_is_read_only_and_current(db):
    bridge._verify_bridge_schema()
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "SUBSCRIBE_DDL" not in source


def test_trade_bridge_schema_failure_precedes_provider_connection(monkeypatch):
    called = {"provider": False}

    def _reject_schema():
        raise RuntimeError("fixture schema drift")

    def _provider(*_args, **_kwargs):
        called["provider"] = True
        raise AssertionError("provider connection happened before schema verification")

    monkeypatch.setattr(bridge, "_verify_bridge_schema", _reject_schema)
    monkeypatch.setattr(bridge, "_run_connection", _provider)
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_trade_bridge.py"])

    with pytest.raises(RuntimeError, match="fixture schema drift"):
        bridge.main()
    assert called["provider"] is False


def test_trade_bridge_unbound_capture_fails_before_provider_connection(monkeypatch):
    called = {"provider": False}
    with bridge._capture_handoff_lock:
        assert bridge._capture_handoff is None
    monkeypatch.setattr(bridge, "_verify_bridge_schema", lambda: None)

    def _provider(*_args, **_kwargs):
        called["provider"] = True
        raise AssertionError("unbound bridge reached provider connection")

    monkeypatch.setattr(bridge, "_run_connection", _provider)
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_trade_bridge.py"])
    with pytest.raises(RuntimeError, match="must be bound before provider connection"):
        bridge.main()
    assert called["provider"] is False


def test_trade_bridge_unbound_loss_is_explicit_only_in_diagnostic_mode(
    monkeypatch, caplog
):
    at = datetime(2026, 7, 15, 15, 30, 1, tzinfo=timezone.utc)
    with bridge._capture_handoff_lock:
        assert bridge._capture_handoff is None
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_trade_bridge.py"])
    with pytest.raises(RuntimeError, match="refusing silent released-row loss"):
        bridge._publish_released_capture_rows(
            trade_rows=[{"sym": "VEEE"}],
            quote_rows=[{"sym": "VEEE"}],
            available_at=at,
        )
    with pytest.raises(RuntimeError, match="refusing silent source-frame loss"):
        bridge._record_unreleased_capture_gap(
            symbol="VEEE",
            streams=("iqfeed_print", "nbbo_quote"),
            available_at=at,
            reason="fixture_unavailable",
        )

    monkeypatch.setattr(
        bridge.sys,
        "argv",
        ["iqfeed_trade_bridge.py", bridge.UNCAPTURED_DIAGNOSTIC_FLAG],
    )
    assert bridge._publish_released_capture_rows(
        trade_rows=[{"sym": "VEEE"}],
        quote_rows=[{"sym": "VEEE"}],
        available_at=at,
    ) == (0, 2)
    assert bridge._record_unreleased_capture_gap(
        symbol="VEEE",
        streams=("iqfeed_print", "nbbo_quote"),
        available_at=at,
        reason="fixture_unavailable",
    ) == 2
    assert "iqfeed_l1_capture_handoff_unbound_diagnostic" in caplog.text
    assert "iqfeed_l1_source_frame_unbound_diagnostic" in caplog.text


def test_bridge_equity_queries_exclude_every_hyphenated_crypto_pair():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert source.count("symbol NOT LIKE '%-%'") == 3
    assert "symbol NOT LIKE '%-USD'" not in source
