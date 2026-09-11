"""[39] numeric provider Delay survives every transport, including rollback/fallback.

Only small, uniquely named schemas in TEST_DATABASE_URL are used. These tests
do not load app.main or bootstrap/truncate the application's test schema.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.migrations import _migration_378_iqfeed_provider_delay_minutes
from scripts import bench_iqfeed_tape_drain as drain
from scripts import bench_iqfeed_tape_write_path as bench
from scripts import iqfeed_trade_bridge as bridge


@pytest.fixture
def isolated_engine():
    url = sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database and url.database.endswith("_test")
    schema = "test_iqfeed_delay_" + uuid.uuid4().hex
    admin = sa.create_engine(url)
    with admin.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    engine = sa.create_engine(
        url, connect_args={"options": f"-c search_path={schema} -c timezone=UTC"},
    )
    try:
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _create_trade_table(engine, ddl, table, **format_args):
    with engine.begin() as connection:
        for statement in ddl.format(t=table, **format_args).split(";"):
            if statement.strip():
                connection.execute(sa.text(statement))


def _read_values(engine, table):
    with engine.connect() as connection:
        return connection.execute(sa.text(
            f"SELECT provider_delay_minutes FROM {table} "
            "ORDER BY source_frame_sequence"
        )).scalars().all()


def test_delay_migration_is_nullable_idempotent_and_preserves_legacy_rows(isolated_engine):
    engine, _schema = isolated_engine
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE iqfeed_trade_ticks (id BIGINT PRIMARY KEY, price FLOAT)"
        ))
        connection.execute(sa.text("INSERT INTO iqfeed_trade_ticks VALUES (1, 2.5)"))
    with engine.connect() as connection:
        _migration_378_iqfeed_provider_delay_minutes(connection)
        _migration_378_iqfeed_provider_delay_minutes(connection)
        column = connection.execute(sa.text(
            "SELECT data_type,is_nullable,column_default FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='iqfeed_trade_ticks' "
            "AND column_name='provider_delay_minutes'"
        )).one()
        assert tuple(column) == ("integer", "YES", None)
        assert connection.execute(sa.text(
            "SELECT id,price,provider_delay_minutes FROM iqfeed_trade_ticks"
        )).all() == [(1, 2.5, None)]
        # An old producer remains valid after the additive migration.
        connection.execute(sa.text("INSERT INTO iqfeed_trade_ticks (id,price) VALUES (2,3)"))
        assert connection.execute(sa.text(
            "SELECT provider_delay_minutes FROM iqfeed_trade_ticks WHERE id=2"
        )).scalar_one() is None


@pytest.mark.parametrize(
    ("requested", "fail_after_insert", "expected_modes"),
    [
        ("copy", (), ["copy"]),
        ("execute_values", (), ["execute_values"]),
        ("values", (), ["values"]),
        ("copy", ("copy",), ["copy", "execute_values"]),
        ("copy", ("copy", "execute_values"), ["copy", "execute_values", "values"]),
    ],
)
def test_all_writers_and_rollback_fallback_retain_delay_and_release_once(
    isolated_engine, monkeypatch, requested, fail_after_insert, expected_modes,
):
    engine, schema = isolated_engine
    _create_trade_table(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES", {
        bridge._TRADE_TAPE: f"{schema}.iqfeed_trade_ticks_id_seq",
    })
    monkeypatch.setattr(bridge, "_FORCED_WRITE_MODE_FAILURES", set())
    monkeypatch.setattr(bridge, "_write_mode_fallbacks", {})
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_ENABLED", False)
    modes = []
    for mode, original in list(bridge._WRITE_MODE_INSERTERS.items()):
        def insert(connection, *, trade_rows, quote_rows, _mode=mode, _original=original):
            modes.append(_mode)
            result = _original(connection, trade_rows=trade_rows, quote_rows=quote_rows)
            if _mode in fail_after_insert:
                raise RuntimeError("fixture failure after insert, before commit")
            return result
        monkeypatch.setitem(bridge._WRITE_MODE_INSERTERS, mode, insert)
    now = datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc)
    rows = bench._rows(4, ["DLY"], 1, str(uuid.uuid4()), 1, now)
    rows[3].pop("provider_delay_minutes")  # Legacy/diagnostic producer.
    ids, quote_ids, used_mode, _timings = bridge._write_pending_batch(
        engine.begin, trade_rows=rows, quote_rows=[], mode=requested,
    )
    assert modes == expected_modes
    assert used_mode == expected_modes[-1]
    assert quote_ids == ()
    assert len(set(ids)) == len(rows)
    assert _read_values(engine, "iqfeed_trade_ticks") == [None, 0, 15, None]
    with engine.connect() as connection:
        stored = connection.execute(sa.text(
            "SELECT id,source_frame_sha256,available_at FROM iqfeed_trade_ticks "
            "ORDER BY source_frame_sequence"
        )).all()
    assert stored == [(row_id, row["source_frame_sha256"], None)
                      for row_id, row in zip(ids, rows)]
    with engine.begin() as connection:
        bridge._release_pending_batch(
            connection, trade_rows=rows, quote_rows=[], available_at=now,
            trade_row_ids=ids, quote_row_ids=(),
        )
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT count(*) FROM iqfeed_trade_ticks WHERE available_at=:at"
        ), {"at": now}).scalar_one() == len(rows)
    assert _read_values(engine, "iqfeed_trade_ticks") == [None, 0, 15, None]


def test_reference_insert_retains_explicit_delay(isolated_engine):
    engine, _schema = isolated_engine
    _create_trade_table(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    row = bridge._selftest_row(1)
    row["provider_delay_minutes"] = 15
    with engine.begin() as connection:
        connection.execute(bridge.INS, row)
    assert _read_values(engine, "iqfeed_trade_ticks") == [15]


@pytest.mark.parametrize("writer", ["values", "execute_values", "copy"])
def test_write_path_benchmark_schema_and_each_writer_retain_delay(isolated_engine, writer):
    engine, _schema = isolated_engine
    _create_trade_table(engine, bench.DDL, bench.TABLE, with_opts="")
    rows = bench._rows(3, ["DLY"], 1, str(uuid.uuid4()), 1, datetime.now(timezone.utc))
    with engine.begin() as connection:
        if writer == "values":
            connection.execute(bench._bridge_values_insert(rows))
        else:
            with connection.connection.cursor() as cursor:
                if writer == "execute_values":
                    bench._execute_values_insert(cursor, rows)
                else:
                    bench._copy_insert(cursor, rows, available_at=None)
    assert _read_values(engine, bench.TABLE) == [None, 0, 15]
    # The extended parity runner uses its own types/tuple with the same DDL.
    assert bench.COLUMNS == bridge._TRADE_INSERT_COLUMNS
    for row in rows:
        assert bench._trade_tuple(row) == bridge._trade_insert_values(row)
    assert isinstance(bench._TRADE_COL_TYPES["provider_delay_minutes"], sa.Integer)


@pytest.mark.parametrize("writer", ["values", "execute_values", "copy"])
def test_drain_benchmark_real_bridge_remap_and_writers_retain_delay(
    isolated_engine, monkeypatch, writer,
):
    engine, _schema = isolated_engine
    _create_trade_table(engine, drain.TRADE_DDL, drain.TRADE_BENCH, with_="")
    monkeypatch.setattr(bridge, "_TRADE_WRITE_TABLE", drain._bench_table(
        drain.TRADE_BENCH, bridge._TRADE_WRITE_TABLE,
    ))
    rows, quotes = drain._rows(bridge, 3, 0)
    inserter = {
        "values": drain._insert_current,
        "execute_values": drain._insert_execute_values,
        "copy": drain._insert_copy,
    }[writer]
    ids, quote_ids = inserter(bridge, engine, rows, quotes)
    assert len(ids) == 3 and not quote_ids
    assert _read_values(engine, drain.TRADE_BENCH) == [None, 0, 15]


def test_missing_delay_column_fails_read_only_schema_gate_before_socket(
    isolated_engine, monkeypatch,
):
    engine, _schema = isolated_engine
    _create_trade_table(engine, bench.DDL, "iqfeed_trade_ticks", with_opts="")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "ALTER TABLE iqfeed_trade_ticks DROP COLUMN provider_delay_minutes"
        ))
        connection.execute(sa.text("CREATE TABLE schema_version (version_id TEXT PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO schema_version VALUES (:version)"), {
            "version": bridge.IQFEED_SCHEMA_OWNER_MIGRATION_ID,
        })
    monkeypatch.setattr(bridge, "engine", engine)
    called = []
    monkeypatch.setattr(bridge, "_run_connection", lambda *a, **k: called.append(True))
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_trade_bridge.py"])
    with pytest.raises(RuntimeError, match="missing: provider_delay_minutes"):
        bridge.main()
    assert not called
