"""CTNT re-entry collision fix (sid 9763, 2026-06-29).

CTNT entered, stopped, then the #1 re-entry lever fired to re-enter a +17% move but the
broker rejected the order: 409 'Reference ID must be unique'. Root cause: the entry
client_order_id seed used only the per-session ``entry_place_count`` (place_n), which is
CLEARED on recycle (it lives in _RECYCLE_ENTRY_STATE_KEYS). So after a stop-out + recycle
the re-entry's first place restarted at place_n=1 and reproduced the FIRST entry's cid.

The fix folds the PERSISTENT recycle counters (trade_cycles / stopout_cycles, deliberately
KEPT across a recycle) into the seed. Idempotency is preserved: a true RETRY of the SAME
attempt (same cycle + same place_n) reuses the SAME cid.

Pure helper — NO DB.
"""
from app.services.trading.momentum_neural.live_runner import _entry_client_order_id


def test_same_attempt_retry_produces_same_cid():
    """IDEMPOTENT: a benign double-submit of the SAME place attempt (same recycle cycle +
    same place_n) yields the SAME cid so the broker de-dupes it."""
    kw = dict(session_id=9763, correlation_id="ctnt-corr", trade_cycles=0, stopout_cycles=0, place_n=1)
    assert _entry_client_order_id(**kw) == _entry_client_order_id(**kw)


def test_two_consecutive_reentries_on_recycled_session_differ():
    """THE BUG: first entry (cycle 0) vs the re-entry after one stop-out recycle (cycle 1,
    place_n RESET to 1) must produce DIFFERENT cids."""
    first = _entry_client_order_id(
        session_id=9763, correlation_id="ctnt-corr", trade_cycles=0, stopout_cycles=0, place_n=1
    )
    reentry = _entry_client_order_id(
        session_id=9763, correlation_id="ctnt-corr", trade_cycles=1, stopout_cycles=1, place_n=1
    )
    assert first != reentry


def test_distinct_place_counts_within_a_cycle_differ():
    """Within ONE cycle, successive re-peg places (place_n 1,2,3) are still distinct."""
    cids = {
        _entry_client_order_id(
            session_id=9763, correlation_id="c", trade_cycles=2, stopout_cycles=0, place_n=n
        )
        for n in (1, 2, 3)
    }
    assert len(cids) == 3


def test_many_recycles_all_distinct():
    """A chopper that re-enters many times produces a distinct cid for each cycle's first place."""
    cids = {
        _entry_client_order_id(
            session_id=9763, correlation_id="c", trade_cycles=tc, stopout_cycles=tc, place_n=1
        )
        for tc in range(0, 12)
    }
    assert len(cids) == 12


def test_profit_recycle_distinct_from_stopout_recycle():
    """A profit recycle (trade_cycles bumps, stopout_cycles does NOT) is still distinct from a
    same-trade_cycles stop-out recycle — both counters participate in the namespace."""
    profit_recycle = _entry_client_order_id(
        session_id=9763, correlation_id="c", trade_cycles=2, stopout_cycles=0, place_n=1
    )
    stopout_recycle = _entry_client_order_id(
        session_id=9763, correlation_id="c", trade_cycles=2, stopout_cycles=1, place_n=1
    )
    assert profit_recycle != stopout_recycle


def test_cid_shape_and_length_preserved():
    """Format/prefix/length unchanged (robin_stocks ignores ref_id, RH 409s on dup): the cid
    starts with the chili_ml_e_ prefix and stays <= 120 chars."""
    cid = _entry_client_order_id(
        session_id=9763, correlation_id="a-very-long-correlation-id-string", trade_cycles=3, stopout_cycles=1, place_n=2
    )
    assert cid.startswith("chili_ml_e_9763_")
    assert len(cid) <= 120


def test_different_sessions_differ():
    a = _entry_client_order_id(session_id=1, correlation_id="c", trade_cycles=0, stopout_cycles=0, place_n=1)
    b = _entry_client_order_id(session_id=2, correlation_id="c", trade_cycles=0, stopout_cycles=0, place_n=1)
    assert a != b
