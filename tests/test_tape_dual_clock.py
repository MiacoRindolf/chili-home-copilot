"""Actual PostgreSQL entry-event/decision-publication window contracts.

Uses only the isolated temporary table fixture, never application startup,
shared schema fixtures, market data or broker clients.
"""
from datetime import timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.entry_gates import (
    signed_tape_accel_features, tape_window_receipt,
)
from app.services.trading.momentum_neural.tape_selection import signed_tape_query
from tests.test_tape_selection_contract import AT, put, tape_connection


def _late_pre_fill_rows(conn):
    for idx in range(1, 7):
        put(conn, idx, idx - 6, received=AT + timedelta(seconds=.5),
            available=AT + timedelta(seconds=1))


@pytest.mark.parametrize("prints,expected", [(4, [3, 4, 5, 6]), (None, [4, 5, 6])])
def test_dual_bounds_filter_before_limit_with_non_utc_clocks(tape_connection, prints, expected):
    _late_pre_fill_rows(tape_connection)
    tick = AT + timedelta(seconds=3)
    # These newer rows must not crowd eligible rows out of the N population.
    invalid = [
        (91, -.01, tick, tick + timedelta(seconds=1)),
        (92, -.02, tick + timedelta(seconds=1), tick + timedelta(seconds=2)),
        (93, -.03, None, tick),
        (94, -.04, tick, None),
        (95, -.05, tick, tick - timedelta(seconds=1)),
        (96, -.06, "-infinity", tick),
        (97, -.07, tick, "infinity"),
        (98, -.08, "infinity", "infinity"),
        # Already published at decision, but AFTER the event endpoint.
        (99, .01, tick, tick),
        (100, 4, tick, tick),
    ]
    for idx, offset, received, available in invalid:
        put(tape_connection, idx, offset, received=received, available=available)
    # Also reject nonfinite event time independently of publication clocks.
    put(tape_connection, 101, -.09, received=tick, available=tick)
    tape_connection.execute(text("UPDATE iqfeed_trade_ticks SET observed_at='-infinity' WHERE id=101"))
    query, params = signed_tape_query(
        "ABC", as_of=tick.astimezone(timezone(timedelta(hours=-7))),
        observed_through=AT.astimezone(timezone(timedelta(hours=5, minutes=30))),
        window_prints=prints, window_s=3,
    )
    assert [r[0] for r in tape_connection.execute(text(query), params)] == expected
    assert params["as_of"] == AT.replace(tzinfo=None)
    assert params["available_by"] == tick
    assert params["available_by"].tzinfo is timezone.utc


@pytest.mark.parametrize("prints", [4, None])
def test_default_and_explicit_same_bound_are_identical(tape_connection, prints):
    for idx in range(1, 7):
        put(tape_connection, idx, idx - 6, received=AT, available=AT)
    kwargs = dict(as_of=AT, window_prints=prints, window_s=3)
    default_query = signed_tape_query("ABC", **kwargs)
    explicit_query = signed_tape_query("ABC", observed_through=AT, **kwargs)
    assert default_query == explicit_query
    query, params = default_query
    assert len(tape_connection.execute(text(query), params).fetchall()) == (4 if prints else 3)
    with Session(bind=tape_connection) as db:
        wrapper_kwargs = dict(db=db, as_of=AT, window_prints=prints, window_s=3)
        assert signed_tape_accel_features("ABC", **wrapper_kwargs) == signed_tape_accel_features(
            "ABC", available_by=AT, **wrapper_kwargs)


@pytest.mark.parametrize("contract", ["count_v1", "legacy_time_split"])
def test_actual_wrapper_records_both_bounds_and_age_at_decision(tape_connection, contract):
    _late_pre_fill_rows(tape_connection)
    tick = AT + timedelta(seconds=30)
    with Session(bind=tape_connection) as db:
        # At the fill, none of these prints has been published yet.
        assert signed_tape_accel_features(
            "ABC", db=db, as_of=AT, window_prints=4, feature_contract=contract) is None
        tape = signed_tape_accel_features(
            "ABC", db=db, as_of=AT.astimezone(timezone(timedelta(hours=9))),
            available_by=tick.astimezone(timezone(timedelta(hours=-4))),
            window_prints=4, feature_contract=contract,
        )
    assert tape["n_ticks"] == 4 and tape["last_print"] == 6
    assert tape["observed_through"] == AT.isoformat()
    assert tape["available_by"] == tick.isoformat()
    assert tape["source_age_s"] == 30
    assert tape["split"] == ("count" if contract == "count_v1" else "time")
    if contract == "count_v1":
        assert tape["print_age_s"] == 30 and tape["print_stale"] is True
    else:
        # Legacy geometry did not enforce a freshness verdict before integration.
        assert tape["print_age_s"] is None and tape["print_stale"] is None
    receipt = tape_window_receipt(tape, "base_")
    assert receipt["base_observed_through"] == AT.isoformat()
    assert receipt["base_available_by"] == tick.isoformat()
    assert receipt["base_source_age_s"] == 30


@pytest.mark.parametrize("prints", [4, None])
def test_reversed_bounds_are_invalid_and_never_clamped(tape_connection, monkeypatch, prints):
    _late_pre_fill_rows(tape_connection)
    with pytest.raises(ValueError, match="observed_through"):
        signed_tape_query("ABC", as_of=AT - timedelta(seconds=1), observed_through=AT,
                          window_prints=prints, window_s=3)
    from app.services.trading.momentum_neural import optional_db_read

    def forbidden_read(*args, **kwargs):
        pytest.fail("invalid dual bounds must not read using a fabricated later frontier")

    monkeypatch.setattr(optional_db_read, "optional_fetchall", forbidden_read)
    with Session(bind=tape_connection) as db:
        assert signed_tape_accel_features(
            "ABC", db=db, as_of=AT, available_by=AT - timedelta(seconds=1),
            window_prints=prints, window_s=3,
        ) is None
        assert db.execute(text("SELECT 41+1")).scalar() == 42


@pytest.mark.parametrize("invalid", ["not-a-clock", float("nan"), float("inf")])
def test_invalid_delivery_clock_is_unavailable_without_read(monkeypatch, invalid):
    from app.services.trading.momentum_neural import optional_db_read

    def forbidden_read(*args, **kwargs):
        pytest.fail("invalid delivery clock must not reach SQL")

    monkeypatch.setattr(optional_db_read, "optional_fetchall", forbidden_read)
    assert signed_tape_accel_features(
        "ABC", db=object(), as_of=AT, available_by=invalid, window_prints=4,
    ) is None
