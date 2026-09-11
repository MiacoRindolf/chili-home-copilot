"""Raw quote evidence survives storage, legacy producers and writer fallback.

Only per-test schemas in an explicitly named *_test DB are changed. No app
bootstrap, shared table truncation, socket or provider connection is used.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.migrations import _migration_379_iqfeed_raw_quote_evidence
from scripts import bench_iqfeed_tape_drain as drain
from scripts import bench_iqfeed_tape_write_path as bench
from scripts import iqfeed_trade_bridge as bridge


FIELDS = ("provider_bid_size_raw", "provider_ask_size_raw",
          "provider_bid_time_raw", "provider_ask_time_raw")
# Blank, zero, malformed, whitespace, COPY escapes and non-ASCII remain distinct.
EVIDENCE = [("00200", "0", "11:30:00.123456", "11:29:59.000001"),
            ("", " ", "bad", ""),
            ("NaN", "-1", "\\N\t\r\n", "時計"),
            (None, None, None, None)]


@pytest.fixture
def isolated_engine():
    url = sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database and url.database.endswith("_test")
    assert url.database != "chili_test"
    schema = "test_iqfeed_quote_raw_" + uuid.uuid4().hex
    admin = sa.create_engine(url)
    with admin.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    engine = sa.create_engine(url, connect_args={
        "options": f"-c search_path={schema} -c timezone=UTC -c statement_timeout=20000",
    })
    try:
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _create(engine, ddl, table, **kwargs):
    with engine.begin() as connection:
        for statement in ddl.format(t=table, **kwargs).split(";"):
            if statement.strip():
                connection.execute(sa.text(statement))


def _read(engine, table):
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(sa.text(
            f"SELECT {','.join(FIELDS)} FROM {table} ORDER BY source_frame_sequence"
        ))]


def _rows():
    trades, quotes = drain._rows(bridge, 4, 4)
    for rows in (trades, quotes):
        for row, values in zip(rows, EVIDENCE):
            if values[0] is not None:
                row.update(zip(FIELDS, values))
            else:
                for field in FIELDS:
                    row.pop(field, None)
            # Last row deliberately has no new keys: old/diagnostic producer.
    return trades, quotes


def test_migration_preserves_unknown_history_and_old_producers(isolated_engine):
    engine, _ = isolated_engine
    tables = ("iqfeed_trade_ticks", "momentum_nbbo_spread_tape")
    with engine.begin() as connection:
        for table in tables:
            connection.execute(sa.text(f"CREATE TABLE {table} (id BIGINT, symbol TEXT)"))
            connection.execute(sa.text(f"INSERT INTO {table} VALUES (1,'OLD')"))
    with engine.connect() as connection:
        _migration_379_iqfeed_raw_quote_evidence(connection)
        _migration_379_iqfeed_raw_quote_evidence(connection)
        for table in tables:
            columns = connection.execute(sa.text(
                "SELECT column_name,data_type,is_nullable,column_default "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=:table AND column_name=ANY(:fields)"
            ), {"table": table, "fields": list(FIELDS)}).all()
            assert set(columns) == {(f, "text", "YES", None) for f in FIELDS}
            connection.execute(sa.text(f"INSERT INTO {table} (id,symbol) VALUES (2,'OLD2')"))
            assert connection.execute(sa.text(
                f"SELECT {','.join(FIELDS)} FROM {table} ORDER BY id"
            )).all() == [(None,) * 4, (None,) * 4]


@pytest.mark.parametrize("requested,fail_after_insert,expected", [
    ("copy", (), ["copy"]),
    ("execute_values", (), ["execute_values"]),
    ("values", (), ["values"]),
    ("copy", ("copy",), ["copy", "execute_values"]),
    ("copy", ("copy", "execute_values"), ["copy", "execute_values", "values"]),
])
def test_both_tapes_survive_fallback_and_release(
    isolated_engine, monkeypatch, requested, fail_after_insert, expected,
):
    engine, schema = isolated_engine
    _create(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    _create(engine, bench.NBBO_DDL, "momentum_nbbo_spread_tape")
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES", {
        table: f"{schema}.{table}_id_seq"
        for table in (bridge._TRADE_TAPE, bridge._NBBO_TAPE)
    })
    monkeypatch.setattr(bridge, "_FORCED_WRITE_MODE_FAILURES", set())
    monkeypatch.setattr(bridge, "_write_mode_fallbacks", {})
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_ENABLED", True)
    # Fallback may inherit a COPY batch larger than a VALUES statement permits.
    # Exercise several statements within one transaction.
    monkeypatch.setattr(bridge, "VALUES_MODE_BIND_BUDGET_EVENTS", 2)
    modes = []
    for mode, original in list(bridge._WRITE_MODE_INSERTERS.items()):
        def insert(connection, *, trade_rows, quote_rows, _mode=mode, _original=original):
            modes.append(_mode)
            result = _original(connection, trade_rows=trade_rows, quote_rows=quote_rows)
            if _mode in fail_after_insert:
                raise RuntimeError("fixture failure after both inserts before commit")
            return result
        monkeypatch.setitem(bridge._WRITE_MODE_INSERTERS, mode, insert)
    trades, quotes = _rows()
    tids, qids, used, _ = bridge._write_pending_batch(
        engine.begin, trade_rows=trades, quote_rows=quotes, mode=requested,
    )
    assert modes == expected and used == expected[-1]
    assert len(set(tids)) == len(set(qids)) == 4
    for table in (bridge._TRADE_TAPE, bridge._NBBO_TAPE):
        assert _read(engine, table) == EVIDENCE
        with engine.connect() as connection:
            assert connection.execute(sa.text(
                f"SELECT count(*) FROM {table} WHERE available_at IS NULL"
            )).scalar_one() == 4
    released = datetime.now(timezone.utc)
    with engine.begin() as connection:
        bridge._release_pending_batch(
            connection, trade_rows=trades, quote_rows=quotes, available_at=released,
            trade_row_ids=tids, quote_row_ids=qids,
        )
    for table in (bridge._TRADE_TAPE, bridge._NBBO_TAPE):
        assert _read(engine, table) == EVIDENCE
        with engine.connect() as connection:
            assert connection.execute(sa.text(
                f"SELECT count(*) FROM {table} WHERE available_at=:at"
            ), {"at": released}).scalar_one() == 4


def test_reference_inserts_preserve_raw_and_allow_legacy_dicts(isolated_engine):
    engine, _ = isolated_engine
    _create(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    _create(engine, bench.NBBO_DDL, "momentum_nbbo_spread_tape")
    trades, quotes = _rows()
    for statement, rows, table in ((bridge.INS, trades, bridge._TRADE_TAPE),
                                   (bridge.NBBO_INS, quotes, bridge._NBBO_TAPE)):
        with engine.begin() as connection:
            for row in rows:
                connection.execute(statement, row)
        assert _read(engine, table) == EVIDENCE


def test_late_values_chunk_failure_rolls_back_both_tapes(isolated_engine, monkeypatch):
    engine, _ = isolated_engine
    _create(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    _create(engine, bench.NBBO_DDL, "momentum_nbbo_spread_tape")
    monkeypatch.setattr(bridge, "VALUES_MODE_BIND_BUDGET_EVENTS", 2)
    original = bridge._insert_pending_batch
    chunks = []

    def insert(connection, **kwargs):
        result = original(connection, **kwargs)
        chunks.append(len(kwargs["trade_rows"]))
        if len(chunks) == 2:
            raise RuntimeError("fixture later VALUES chunk failed")
        return result

    monkeypatch.setattr(bridge, "_insert_pending_batch", insert)
    trades, quotes = _rows()
    with pytest.raises(RuntimeError, match="later VALUES chunk"):
        bridge._write_pending_batch(engine.begin, trade_rows=trades, quote_rows=quotes, mode="values")
    assert chunks == [2, 2]
    assert _read(engine, bridge._TRADE_TAPE) == []
    assert _read(engine, bridge._NBBO_TAPE) == []


@pytest.mark.parametrize("writer", ["values", "execute_values", "copy"])
def test_independent_benchmark_writers_round_trip_both_tapes(isolated_engine, monkeypatch, writer):
    engine, _ = isolated_engine
    _create(engine, drain.TRADE_DDL, drain.TRADE_BENCH, with_="")
    _create(engine, drain.NBBO_DDL, drain.NBBO_BENCH, with_="")
    monkeypatch.setattr(bridge, "_TRADE_WRITE_TABLE", drain._bench_table(
        drain.TRADE_BENCH, bridge._TRADE_WRITE_TABLE))
    monkeypatch.setattr(bridge, "_NBBO_WRITE_TABLE", drain._bench_table(
        drain.NBBO_BENCH, bridge._NBBO_WRITE_TABLE))
    trades, quotes = _rows()
    insert = {"values": drain._insert_current, "execute_values": drain._insert_execute_values,
              "copy": drain._insert_copy}[writer]
    tids, qids = insert(bridge, engine, trades, quotes)
    assert len(tids) == len(qids) == 4
    assert _read(engine, drain.TRADE_BENCH) == EVIDENCE
    assert _read(engine, drain.NBBO_BENCH) == EVIDENCE
    assert bench.COLUMNS == drain.TRADE_COLS == bridge._TRADE_INSERT_COLUMNS
    assert bench.NBBO_COLUMNS == drain.NBBO_COLS == bridge._NBBO_INSERT_COLUMNS
    for trade, quote in zip(trades, quotes):
        assert bench._trade_tuple(trade) == bridge._trade_insert_values(trade)
        assert bench._nbbo_tuple(quote) == bridge._nbbo_insert_values(quote)


@pytest.mark.parametrize("table", ["iqfeed_trade_ticks", "momentum_nbbo_spread_tape"])
@pytest.mark.parametrize("field", FIELDS)
def test_missing_column_refuses_startup_before_socket(isolated_engine, monkeypatch, table, field):
    engine, _ = isolated_engine
    _create(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    _create(engine, bench.NBBO_DDL, "momentum_nbbo_spread_tape")
    with engine.begin() as connection:
        connection.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {field}"))
        connection.execute(sa.text("CREATE TABLE schema_version (version_id TEXT PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO schema_version VALUES (:v)"), {
            "v": bridge.IQFEED_SCHEMA_OWNER_MIGRATION_ID,
        })
    monkeypatch.setattr(bridge, "engine", engine)
    monkeypatch.setattr(bridge, "WRITE_NBBO_TAPE", True)
    called = []
    monkeypatch.setattr(bridge, "_run_connection", lambda *a, **k: called.append(True))
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_trade_bridge.py"])
    with pytest.raises(RuntimeError, match=f"missing: {field}"):
        bridge.main()
    assert not called


@pytest.mark.parametrize("writer", ["values", "execute_values", "copy"])
def test_trade_benchmark_helpers_round_trip_raw_text(isolated_engine, writer):
    engine, _ = isolated_engine
    _create(engine, bench.DDL, bench.TABLE, with_opts="")
    rows, _ = _rows()
    with engine.begin() as connection:
        if writer == "values":
            connection.execute(bench._bridge_values_insert(rows))
        else:
            with connection.connection.cursor() as cursor:
                if writer == "execute_values":
                    bench._execute_values_insert(cursor, rows)
                else:
                    bench._copy_insert(cursor, rows, available_at=None)
    assert _read(engine, bench.TABLE) == EVIDENCE
