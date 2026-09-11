"""Actual PostgreSQL clock eligibility for the two exit leg readers.

Temporary-table only; no application startup, schema migration, broker or market
connection. Event cursor, seven-field row shape and untruncated suffix remain
the existing contract; these tests do not certify a captured input prefix.
"""
from datetime import timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural import entry_gates as EG
from tests.test_tape_selection_contract import AT, put, tape_connection


def read(db, kind, *, start, as_of, ident=0, err=None):
    if kind == "between":
        return EG.leg_prints_between(
            "ABC", db=db, after=start, after_id=ident, as_of=as_of, err=err,
        )
    return EG.leg_prints_since_high(
        "ABC", db=db, hi_at=start, hi_id=ident, as_of=as_of, err=err,
    )


@pytest.mark.parametrize("kind", ["between", "since_high"])
@pytest.mark.parametrize("offset", [None, timezone(timedelta(hours=5, minutes=30)),
                                    timezone(timedelta(hours=-7))])
def test_real_reader_eligibility_is_independent_of_input_and_db_timezone(tape_connection, kind, offset):
    # Fixture deliberately uses Pacific/Honolulu for the PostgreSQL session.
    for idx in range(1, 4):
        put(tape_connection, idx, idx - 3, received=AT - timedelta(seconds=1), available=AT)
    put(tape_connection, 90, -.5, received=AT, available=AT + timedelta(seconds=1))
    put(tape_connection, 91, .5, received=AT, available=AT)
    if offset is None:
        decision = AT.replace(tzinfo=None)
        start = (AT - timedelta(seconds=3)).replace(tzinfo=None)
    else:
        decision = AT.astimezone(offset)
        start = (AT - timedelta(seconds=3)).astimezone(offset).isoformat()
    with Session(bind=tape_connection) as db:
        rows = read(db, kind, start=start, as_of=decision)
        assert [r[6] for r in rows] == [1, 2, 3]
        assert all(len(r) == 7 for r in rows)
        assert [r[0] for r in rows] == [1., 2., 3.]
        assert rows[-1][5] == AT.replace(tzinfo=None)
        assert float(rows[-1][4]) == pytest.approx(AT.timestamp())


@pytest.mark.parametrize("kind", ["between", "since_high"])
def test_unknown_nonfinite_and_reversed_clocks_never_qualify(tape_connection, kind):
    put(tape_connection, 1, -1, received=AT, available=AT)
    invalid = [
        (10, None, AT), (11, AT, None), (12, None, None),
        (13, AT, AT - timedelta(seconds=1)),
        (14, "-infinity", AT), (15, AT, "infinity"),
        (16, "-infinity", "-infinity"), (17, "infinity", "infinity"),
        (18, AT + timedelta(seconds=1), AT + timedelta(seconds=2)),
    ]
    for idx, received, available in invalid:
        put(tape_connection, idx, -.5, received=received, available=available)
    for idx, event_time in [(20, "-infinity"), (21, "infinity"), (22, None)]:
        put(tape_connection, idx, -.25, received=AT, available=AT)
        tape_connection.execute(text("UPDATE iqfeed_trade_ticks SET observed_at=:event WHERE id=:id"),
                                dict(event=event_time, id=idx))
    with Session(bind=tape_connection) as db:
        rows = read(db, kind, start=AT - timedelta(seconds=3), as_of=AT)
        assert [r[6] for r in rows] == [1]


@pytest.mark.parametrize("kind", ["between", "since_high"])
def test_existing_event_tuple_and_equal_time_id_order_are_preserved(tape_connection, kind):
    for idx in [8, 4, 7]:
        put(tape_connection, idx, -2, received=AT, available=AT)
    put(tape_connection, 1, -1, received=AT, available=AT)
    with Session(bind=tape_connection) as db:
        rows = read(db, kind, start=AT - timedelta(seconds=2), ident=4, as_of=AT)
        assert [r[6] for r in rows] == [7, 8, 1]
        # Boundary tuple is excluded; different rows at that time remain ordered.
        assert all(r[5] >= (AT - timedelta(seconds=2)).replace(tzinfo=None) for r in rows)


def test_first_between_read_still_uses_strict_entry_event_boundary(tape_connection):
    put(tape_connection, 1, -2, received=AT, available=AT)
    put(tape_connection, 2, -1, received=AT, available=AT)
    with Session(bind=tape_connection) as db:
        rows = EG.leg_prints_between("ABC", db=db, after=AT - timedelta(seconds=2), as_of=AT)
        assert [r[6] for r in rows] == [2]


@pytest.mark.parametrize("kind", ["between", "since_high"])
@pytest.mark.parametrize("column", ["available_at", "received_at"])
def test_missing_clock_column_does_not_fallback_or_poison_transaction(tape_connection, kind, column):
    put(tape_connection, 1, -1, received=AT, available=AT)
    # Parameter is an internal closed allowlist, not external SQL input.
    tape_connection.execute(text(f"ALTER TABLE iqfeed_trade_ticks DROP COLUMN {column}"))
    with Session(bind=tape_connection) as db:
        err = {}
        timeout_before = db.execute(text("SHOW statement_timeout")).scalar()
        assert read(db, kind, start=AT - timedelta(seconds=2), as_of=AT, err=err) is None
        assert err["why"] == "error"
        assert db.execute(text("SELECT 41+1")).scalar() == 42
        assert db.execute(text("SHOW statement_timeout")).scalar() == timeout_before


@pytest.mark.parametrize("kind", ["between", "since_high"])
def test_reversed_event_bounds_remain_unavailable(tape_connection, kind):
    with Session(bind=tape_connection) as db:
        assert read(db, kind, start=AT + timedelta(seconds=1), as_of=AT) is None
        assert db.execute(text("SELECT 41+1")).scalar() == 42
