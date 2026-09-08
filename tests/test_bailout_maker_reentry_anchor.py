"""The post-bailout maker re-entry was chasing its own bid upward (2026-09-07).

The source comment claimed the ladder "hindi ito hahabulin pataas" because there is no repeg
anchor. That is true INSIDE one order and false ACROSS orders: every new firing posted at the
CURRENT bid, so the sequence ack-timeout -> re-fire -> ack-timeout walks the post price up.

MEASURED on the Ross Parity Bench at the $13,000 / 3% canon:
  * 91 firings, 30 reach a submit (33.0%), 13 fill -- a 43.3% fill rate against 132/132 =
    100.0% for ordinary marketable entries in the same 30 receipts.
  * ALL 17 non-fills ended `entry_ack_timeout` / `entry_limit_left_behind`, and in 17 of 17
    the bid at timeout was strictly HIGHER than the bid the order was posted at (+$0.01..0.04).
  * VEEE doctrine: posted 8.12 -> 8.24 -> 8.32, filled 8.32, when crossing at the first
    attempt would have paid 8.16 (-$57.12 per receipt).
  * PPBT 2026-09-02: posted 2.13 -> 2.14 -> 2.15 over 20.19 s. Because each attempt re-measured
    a RISING ATR, the stop widened and shares shrank at fixed risk (2369 -> 2053 -> 1811), so
    the filled leg carried 545 fewer shares into a 0.52 move: -$301.51 on that one leg, which
    IS the whole -$230.11 regression of the case (legs 1 and 3 give back +$71.40).

The gate itself is NOT removed and should not be: measured net +$166.34 across the current
canon, and it does exactly what it was designed to do when the bid RETRACES (NAMI 7.64 ->
7.53 -> 7.22, filled 7.22, +$80.55, bid then ran to 9.04). The defect is one-directional --
following the bid UP is the failure mode; following it DOWN is the feature.

Runnable: pytest tests/test_bailout_maker_reentry_anchor.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner as lr


def _src() -> str:
    return inspect.getsource(lr.tick_live_session)


def test_the_first_firing_of_an_episode_sets_the_anchor():
    src = _src()
    i = src.find('_emit(db, sess, "live_entry_bailout_maker_reentry"')
    assert i > 0
    block = src[max(0, i - 900):i]
    assert 'le["bailout_maker_anchor_bid"] = float(bid)' in block
    assert 'le["bailout_maker_anchor_at_utc"]' in block


def test_a_later_firing_may_not_post_above_the_anchor():
    """This is the whole fix: the ladder stops walking up."""
    src = _src()
    i = src.find('_emit(db, sess, "live_entry_bailout_maker_reentry"')
    block = src[max(0, i - 900):i]
    assert "elif float(bid) > _bm_anchor:" in block
    assert "_bailout_maker = False" in block
    assert '_bm_reason = "bid_above_episode_anchor"' in block


def test_it_falls_back_to_marketable_rather_than_standing_down():
    """When the pullback does not come, the legacy guarded-ask path must take the entry --
    that is the function's own documented failure mode (fail-toward-legacy), and on PPBT it
    is exactly what the base arm did for +$283 of the delta. Setting the flag False is what
    routes it there, so the maker branch must be a plain `elif` off that flag."""
    src = _src()
    assert "elif _bailout_maker:" in src
    i = src.find("elif _bailout_maker:")
    # the marketable path is the else of the same chain
    assert src.find("else:", i) > i


def test_the_anchor_is_cleared_on_recycle():
    """It belongs to the trade that just closed; inheriting it would refuse a legitimate
    FIRST post on the next leg."""
    for key in ("bailout_maker_anchor_bid", "bailout_maker_anchor_at_utc"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key
    le = {"bailout_maker_anchor_bid": 2.13, "bailout_maker_anchor_at_utc": "x"}
    lr._reset_entry_state_on_recycle(le)
    assert le == {}


def test_the_receipt_says_whether_the_anchor_blocked_the_post():
    """Every firing is now on the record with the anchor it was measured against and whether
    an order was actually posted -- the corpus scan could not tell fires from posts."""
    src = _src()
    i = src.find('_emit(db, sess, "live_entry_bailout_maker_reentry"')
    block = src[i:i + 700]
    assert '"anchor_bid": _bm_anchor' in block
    assert '"posted": bool(_bailout_maker)' in block
    assert '"bid": float(bid)' in block


def test_the_false_claim_is_gone_from_the_comment():
    """The comment asserted the ladder could not chase upward. The bench measured it doing
    exactly that 17 times out of 17. A comment that contradicts a measurement is a defect."""
    src = inspect.getsource(lr)
    i = src.find("POST-BAILOUT MAKER RE-ENTRY")
    assert i > 0
    block = src[i:i + 3000]
    assert "WALANG repeg anchor kaya hindi ito hahabulin" not in block or "MALI IYON" in block


def test_the_pure_decision_function_is_untouched():
    """The fix belongs at the call site (episode state lives on `le`). The pure decision must
    stay zero-I/O and stateless so its own tests keep meaning."""
    from app.services.trading.momentum_neural import risk_policy as rp

    sig = inspect.signature(rp.bailout_maker_reentry_decision)
    assert set(sig.parameters) == {
        "enabled", "last_exit_reason", "last_exit_return_bps",
        "last_exit_at_utc", "now_utc", "window_seconds",
    }, sorted(sig.parameters)
    # no leg dict, no session, no db handle reaches it — checked as IDENTIFIERS, because a
    # naive substring test matches "le[" inside "tuple[" (it did, on the first draft)
    import ast

    names = {
        n.id for n in ast.walk(ast.parse(inspect.getsource(rp.bailout_maker_reentry_decision)))
        if isinstance(n, ast.Name)
    }
    assert not (names & {"le", "db", "sess", "settings"}), sorted(names & {"le", "db", "sess", "settings"})
