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

    payload = json.loads(bridge._notify_payload(bridge._pending_nbbo[0]))
    assert payload == {
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
        "symbol": "SOBR",
        "timestamp_basis": bridge.AUTHORITATIVE_TIMESTAMP_BASIS,
    }


def test_trade_reference_is_not_mislabeled_provider_event(monkeypatch):
    _reference_delta(monkeypatch, 0.1)
    bridge._parse_l1(_frame(), connection_generation=2)

    trade = bridge._pending[0]
    quote = bridge._pending_nbbo[0]
    assert trade["provider_at"] is None
    assert quote["provider_at"] is None
    assert trade["provider_trade_reference_at"] is not None
    assert quote["provider_trade_reference_at"] is not None


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
    assert bridge.BRIDGE_VERSION.endswith("-v2")


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
    source = bridge.Path(
        __import__("app.migrations", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")
    start = source.index("def _migration_317_iqfeed_bridge_v2_causal_provenance")
    end = source.index("\n\nMIGRATIONS =", start)
    body = source[start:end].upper()
    assert "ADD COLUMN IF NOT EXISTS" in body
    assert "CREATE INDEX" not in body
    assert "UPDATE " not in body


def test_bridge_equity_queries_exclude_every_hyphenated_crypto_pair():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert source.count("symbol NOT LIKE '%-%'") == 3
    assert "symbol NOT LIKE '%-USD'" not in source
