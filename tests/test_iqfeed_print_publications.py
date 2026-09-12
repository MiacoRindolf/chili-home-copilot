"""Real release transactions; no running bridge or live database access."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import pytest
import sqlalchemy as sa

from scripts import iqfeed_trade_bridge as bridge
from scripts import iqfeed_print_publications as journal
from scripts import bench_iqfeed_tape_write_path as bench


NOW = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def source(monkeypatch):
    url = sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "chili_rossbench26_test" or url.database.endswith("_test")
    schema = "test_print_publications_" + uuid4().hex
    admin = sa.create_engine(url)
    with admin.begin() as c:
        c.execute(sa.text(f"CREATE SCHEMA {schema}"))
    engine = sa.create_engine(url, connect_args={
        "options": f"-c search_path={schema} -c timezone=UTC -c statement_timeout=10000",
    })
    with engine.begin() as c:
        for statement in bench.DDL.format(t="iqfeed_trade_ticks", with_opts="").split(";"):
            if statement.strip():
                c.execute(sa.text(statement))
        journal.create_schema(c)
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_ENABLED", False)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def insert(engine, symbol="WAVE", prices=(10, 12, 9)):
    rows = bench._rows(len(prices), [symbol], 1, str(uuid4()), 1, NOW)
    for row, price in zip(rows, prices):
        row["px"] = price
    with engine.begin() as c:
        ids, _ = bridge._insert_pending_batch(c, trade_rows=rows, quote_rows=[], return_row_ids=True)
    return rows, ids


def release(connection, packet, *, ids=True):
    rows, row_ids = packet
    return bridge._release_pending_batch(
        connection, trade_rows=rows, quote_rows=[], available_at=NOW,
        trade_row_ids=row_ids if ids else None,
    )


def read(engine, after=None, *, symbol="WAVE", capacity=20, max_rows=10000):
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
        head = journal.capture_frontier(c)
        return journal.read_symbol_publications(
            c, symbol=symbol, after=after or journal.Cursor(head.epoch, 0),
            max_publications=capacity, max_trade_rows=max_rows,
        )


@pytest.mark.parametrize("use_ids", [True, False])
def test_atomic_spike_reversal_membership_survives_two_consumers(source, use_ids):
    packet = insert(source)
    with source.begin() as c:
        release(c, packet, ids=use_ids)
    selection, held = read(source), read(source)
    assert selection == held
    assert selection.caught_up
    assert len(selection.publications) == 1
    publication = selection.publications[0]
    assert publication.trade_ids == packet[1]
    assert [r["price"] for r in publication.rows] == [10, 12, 9]
    with pytest.raises(TypeError):
        publication.rows[0]["price"] = 99


def test_later_release_of_smaller_database_ids_is_not_skipped(source):
    older = insert(source, prices=(10,))
    newer = insert(source, prices=(12,))
    assert older[1][0] < newer[1][0]
    with source.begin() as c:
        release(c, newer)
    first = read(source)
    with source.begin() as c:
        release(c, older)
    second = read(source, first.consumed)
    assert second.publications[0].trade_ids == older[1]
    # A highest-trade-ID watermark silently loses the late publication.
    with source.connect() as c:
        assert c.execute(sa.text("""
            SELECT id FROM iqfeed_trade_ticks WHERE symbol='WAVE'
            AND id>:watermark AND available_at IS NOT NULL
        """), {"watermark": newer[1][0]}).all() == []


def test_failure_after_publication_rolls_back_release_and_sequence(source):
    seed = insert(source, prices=(10,))
    with source.begin() as c:
        release(c, seed)
    before = read(source)
    packet = insert(source)
    with pytest.raises(RuntimeError, match="fixture after publication"):
        with source.begin() as c:
            release(c, packet)
            raise RuntimeError("fixture after publication")
    assert read(source).observed_frontier == before.observed_frontier
    with source.connect() as c:
        assert c.execute(sa.text("""
            SELECT available_at FROM iqfeed_trade_ticks WHERE id=ANY(:ids)
        """), {"ids": list(packet[1])}).scalars().all() == [None] * 3
    with source.begin() as c:
        release(c, packet)
    assert read(source, before.consumed).consumed.revision == before.consumed.revision + 1


def test_repeat_release_cannot_duplicate_a_committed_publication(source):
    packet = insert(source)
    with source.begin() as c:
        release(c, packet)
    before = read(source)
    with pytest.raises(RuntimeError, match="row-count mismatch"):
        with source.begin() as c:
            release(c, packet)
    assert read(source) == before


def test_slow_symbol_consumer_pages_every_publication_without_rank_membership(source):
    # Journal source does not depend on a selected/watched/HELD ranking table.
    for symbol in ("OTHER", "WAVE", "OTHER", "WAVE"):
        packet = insert(source, symbol=symbol, prices=(10,))
        with source.begin() as c:
            release(c, packet)
    cursor, seen = None, []
    for revision in range(1, 5):
        result = read(source, cursor, capacity=1)
        assert result.consumed.revision == revision
        seen.extend(p.cursor.revision for p in result.publications)
        cursor = result.consumed
    assert result.caught_up and seen == [2, 4]


@pytest.mark.parametrize("remove", ["journal", "trade"])
def test_retention_gap_is_explicit_not_a_new_valid_prefix(source, remove):
    packet = insert(source)
    with source.begin() as c:
        release(c, packet)
    with source.begin() as c:
        if remove == "journal":
            c.execute(sa.text("DELETE FROM iqfeed_print_publications"))
        else:
            c.execute(sa.text("DELETE FROM iqfeed_trade_ticks WHERE id=:id"), {"id": packet[1][1]})
    with pytest.raises(ValueError, match="gap"):
        read(source)


def test_source_reset_cannot_join_prior_cursor_and_migration_is_idempotent(source):
    packet = insert(source)
    with source.begin() as c:
        release(c, packet)
    before = read(source)
    with source.begin() as c:
        journal.create_schema(c)
    assert read(source) == before
    with source.begin() as c:
        c.execute(sa.text("DELETE FROM iqfeed_print_publication_head"))
    packet2 = insert(source)
    with source.begin() as c:
        release(c, packet2)
    with pytest.raises(ValueError, match="epoch_changed"):
        read(source, before.consumed)


def test_reader_requires_stable_snapshot_and_started_source(source):
    with source.connect() as c:
        with pytest.raises(ValueError, match="requires_snapshot"):
            journal.capture_frontier(c)
    with source.connect().execution_options(isolation_level="REPEATABLE READ") as c:
        with pytest.raises(ValueError, match="not_started"):
            journal.capture_frontier(c)


def test_uncommitted_publication_is_invisible_and_publishers_serialize(source):
    seed, first, second = (insert(source, prices=(p,)) for p in (10, 11, 12))
    with source.begin() as c:
        release(c, seed)
    before = read(source)
    started = Event()
    def publish_second():
        with source.begin() as c:
            started.set()
            release(c, second)
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with source.connect() as c1:
            tx = c1.begin()
            try:
                release(c1, first)
                pending = pool.submit(publish_second)
                assert started.wait(5), "second publisher did not start"
                assert read(source).consumed == before.consumed
                # It cannot commit a higher revision past the uncommitted head.
                assert not pending.done()
            finally:
                tx.rollback()
        assert pending.result(timeout=10)
    after = read(source, before.consumed)
    assert after.consumed.revision == before.consumed.revision + 1
    assert after.publications[0].trade_ids == second[1]


def test_repeatable_read_frontier_does_not_move_mid_consumer_read(source):
    first, second = insert(source), insert(source)
    with source.begin() as c:
        release(c, first)
    with source.connect().execution_options(isolation_level="REPEATABLE READ") as reader:
        captured = journal.capture_frontier(reader)
        with source.begin() as writer:
            release(writer, second)
        old = journal.read_symbol_publications(reader, symbol="WAVE",
            after=journal.Cursor(captured.epoch, 0), max_publications=20, max_trade_rows=10000)
        assert old.observed_frontier == captured and len(old.publications) == 1
    new = read(source, old.consumed)
    assert new.publications[0].trade_ids == second[1]


def test_read_capacity_never_splits_a_spike_reversal_publication(source):
    first, second = insert(source), insert(source)
    for packet in (first, second):
        with source.begin() as c:
            release(c, packet)
    with pytest.raises(ValueError, match="atomic_publication_exceeds"):
        read(source, max_rows=2)
    page = read(source, max_rows=4)
    assert len(page.publications) == 1 and not page.caught_up
    assert page.publications[0].trade_ids == first[1]
    next_page = read(source, page.consumed, max_rows=4)
    assert next_page.caught_up and next_page.publications[0].trade_ids == second[1]


def test_print_wake_is_commit_coupled_and_contains_only_journal_cursor(source):
    import select

    raw = source.raw_connection()
    listener = raw.driver_connection
    listener.autocommit = True
    try:
        with listener.cursor() as cursor:
            cursor.execute("LISTEN " + journal.CHANNEL)
        packet = insert(source)
        with source.connect() as c:
            tx = c.begin()
            release(c, packet)
            listener.poll()
            assert listener.notifies == []
            tx.rollback()
        with source.begin() as c:
            release(c, packet)
        assert select.select([listener], [], [], 5)[0]
        listener.poll()
        messages = list(listener.notifies)
        head = read(source).observed_frontier
        assert [(n.channel, n.payload) for n in messages] == [
            (journal.CHANNEL, f"{head.epoch}:{head.revision}")]
    finally:
        listener.autocommit = False
        raw.close()
