"""Actual PostgreSQL temporal selection, isolated to a temporary test table.

No app startup, migration runner, ``db`` fixture, market connection or broker.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.tape_selection import signed_tape_query, utc_boundaries


@pytest.fixture
def tape_connection():
    url = os.environ["TEST_DATABASE_URL"]
    assert url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar().endswith("_test")
        conn.execute(text("SET LOCAL statement_timeout='20s'"))
        conn.execute(text("SET LOCAL TIME ZONE 'Pacific/Honolulu'"))
        conn.execute(text("CREATE TEMP TABLE iqfeed_trade_ticks (id bigint, symbol text, "
                          "observed_at timestamp, received_at timestamptz, available_at timestamptz, "
                          "price float8, size float8, bid float8, ask float8)"))
        yield conn
        conn.rollback()
    engine.dispose()


AT = datetime(2026, 9, 10, 18, tzinfo=timezone.utc)


def put(conn, idx, seconds, *, received=None, available=None):
    observed = AT + timedelta(seconds=seconds)
    conn.execute(text("INSERT INTO iqfeed_trade_ticks VALUES "
                      "(:id,'ABC',:event,:received,:available,:price,100,:bid,:ask)"),
                 {"id": idx, "event": observed.replace(tzinfo=None),
                  "received": received, "available": available,
                  "price": float(idx), "bid": float(idx) - .01, "ask": float(idx)})


def seed(conn):
    for i in range(1, 7):
        put(conn, i, i - 7, received=AT - timedelta(seconds=1), available=AT)
    # Newer event rows must be removed before LIMIT, not after retrieving N.
    put(conn, 91, -.5, received=AT + timedelta(seconds=1), available=AT + timedelta(seconds=2))
    put(conn, 92, -.4, received=AT - timedelta(seconds=1), available=AT + timedelta(seconds=1))
    put(conn, 93, -.3, received=AT - timedelta(seconds=1), available=None)
    put(conn, 94, -.2, received=None, available=AT)
    put(conn, 95, -.1, received=AT, available=AT - timedelta(seconds=1))
    put(conn, 96, +.1, received=AT, available=AT)
    put(conn, 97, -.01, received="-infinity", available=AT)


def test_latest_n_eligible_before_limit_and_non_utc_asof(tape_connection):
    seed(tape_connection)
    offset = timezone(timedelta(hours=5, minutes=30))
    query, params = signed_tape_query("ABC", as_of=AT.astimezone(offset), window_prints=4, window_s=15)
    rows = tape_connection.execute(text(query), params).fetchall()
    assert [r[0] for r in rows] == [3, 4, 5, 6]
    assert params["as_of"] == AT.replace(tzinfo=None)
    assert params["available_by"] == AT
    assert params["available_by"].tzinfo is timezone.utc


def test_seconds_comparison_obeys_same_arrival_contract(tape_connection):
    seed(tape_connection)
    query, params = signed_tape_query("ABC", as_of=AT, window_prints=None, window_s=3)
    assert [r[0] for r in tape_connection.execute(text(query), params)] == [5, 6]


def test_equal_timestamp_prints_are_preserved_in_id_order(tape_connection):
    for idx in [8, 1, 7, 2, 6, 3]:
        put(tape_connection, idx, -1, received=AT, available=AT)
    query, params = signed_tape_query("ABC", as_of=AT, window_prints=4, window_s=15)
    assert [r[0] for r in tape_connection.execute(text(query), params)] == [3, 6, 7, 8]


def test_probe_and_entry_use_identical_real_selection(tape_connection):
    from app.services.trading.momentum_neural.entry_gates import signed_tape_accel_features
    from scripts.tape_verdict_probe import probe
    seed(tape_connection)
    with Session(bind=tape_connection) as db:
        actual = signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4)
        observed = probe(db, "ABC", AT, prints=4)
        assert actual == observed
        assert actual["n_ticks"] == 4
        assert actual["last_print"] == 6
        assert actual["print_age_s"] == 1
        assert actual["selection_contract"] == "event_received_recorded_publication_v1"


def test_missing_publication_column_does_not_fallback_or_poison_transaction(tape_connection):
    from app.services.trading.momentum_neural.entry_gates import signed_tape_accel_features
    tape_connection.execute(text("ALTER TABLE iqfeed_trade_ticks DROP COLUMN available_at"))
    with Session(bind=tape_connection) as db:
        assert signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=4) is None
        assert db.execute(text("SELECT 41+1")).scalar() == 42


def test_utc_naive_boundary_has_explicit_utc_meaning():
    assert utc_boundaries(AT.replace(tzinfo=None)) == (AT.replace(tzinfo=None), AT)


def test_real_tnon_publication_fixture_replenishes_255_not_164(tape_connection):
    import json
    from decimal import Decimal
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / "fixtures/task29_tnon_publication.json").read_text())
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def instant(value):
        return epoch + timedelta(microseconds=int(Decimal(value) * 1_000_000))

    values = [{"id": r["id"], "symbol": "TNON", "event": instant(r["t"]).replace(tzinfo=None),
               "received": instant(r["received"]), "available": instant(r["available"]),
               "price": r["price"], "size": r["size"], "bid": r["bid"], "ask": r["ask"]}
              for r in fixture["rows"]]
    tape_connection.execute(text("INSERT INTO iqfeed_trade_ticks VALUES "
                                 "(:id,:symbol,:event,:received,:available,:price,:size,:bid,:ask)"), values)
    query, params = signed_tape_query("TNON", as_of=datetime.fromisoformat(fixture["as_of"]),
                                     window_prints=255, window_s=15)
    actual = tape_connection.execute(text(query), params).fetchall()
    by_id = {r["id"]: r for r in fixture["rows"]}
    expected = [by_id[idx] for idx in fixture["expected_ids"]]
    assert len(actual) == fixture["expected_n"] == 255
    assert fixture["filter_after_limit_n"] == 164
    assert fixture["expected_ids"][-1] == fixture["expected_last_id"]
    assert [tuple(r[:4]) for r in actual] == [tuple(r[k] for k in ("price", "size", "bid", "ask")) for r in expected]
    assert [float(r[4]) for r in actual] == pytest.approx([float(r["t"]) for r in expected], abs=1e-6)
