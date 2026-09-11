"""Actual SQL publication eligibility + paired geometry on frozen returned rows."""
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import exit_verdict as ev
from tests.test_tape_selection_contract import AT, tape_connection
from tests.test_held_evaluation_observation import collecting
from tests.test_exit_count_contract import frozen_rows


def test_real_count_g_and_d_exclude_future_publication_and_pair_same_d_membership(tape_connection):
    for row in frozen_rows():
        price, size, bid, ask, epoch, _, idx = row
        event_at = AT - timedelta(seconds=5.2) + timedelta(seconds=epoch-frozen_rows()[0][4])
        tape_connection.execute(text("INSERT INTO iqfeed_trade_ticks VALUES "
                                     "(:id,'ABC',:event,:received,:available,:price,:size,:bid,:ask)"),
                                {"id": idx, "event": event_at.replace(tzinfo=None),
                                 "received": event_at+timedelta(milliseconds=20),
                                 "available": event_at+timedelta(milliseconds=40),
                                 "price": price, "size": size, "bid": bid, "ask": ask})
    # A newer event that was not received/published at the decision frontier
    # cannot replace the last N print, nor enter the since-high population.
    tape_connection.execute(text("INSERT INTO iqfeed_trade_ticks VALUES "
                                 "(99999,'ABC',:event,:received,:available,20,90000,19.99,20)"),
                            {"event": (AT-timedelta(milliseconds=50)).replace(tzinfo=None),
                             "received": AT+timedelta(milliseconds=10), "available": AT+timedelta(milliseconds=20)})
    with Session(bind=tape_connection) as db:
        with collecting("G") as g_observation:
            g = eg.signed_tape_accel_features("ABC", db=db, as_of=AT, window_prints=8,
                                            feature_contract="count_v1")
        with collecting("D") as d_observation:
            rows = eg.leg_prints_since_high("ABC", db=db, hi_at=AT-timedelta(seconds=6), hi_id=0, as_of=AT)
        before = tuple(tuple(r) for r in rows)
        count = ev.since_high_verdict(rows, feature_contract="count_v1", as_of_ts=AT.timestamp())
        legacy = ev.since_high_verdict(rows, feature_contract="legacy_time_split", window_s=15.)
    assert g_observation.reads[0]["ordered_ids"] == list(range(9100, 9108))
    assert g["signed_tape_accel"] == 200 and g["split"] == "count"
    assert [row[6] for row in rows] == list(range(9100, 9108))
    assert tuple(tuple(r) for r in rows) == before
    assert count["signed_tape_accel"] == g["signed_tape_accel"] == 200
    assert legacy["signed_tape_accel"] == -1000
    assert count["fired"] is False and legacy["fired"] is True
    assert count["print_stale"] is False and count["print_age_bound_s"] == 14.69
    record, = d_observation.reads
    assert record["returned_count"] == 8 and record["membership_recovery"] == "digest_only"
    assert "ordered_ids" not in record and record["first_event_id"][1] == 9100
    assert record["last_event_id"][1] == 9107
