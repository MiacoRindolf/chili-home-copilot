"""LIVE BOS EXIT — a confirmed close below structure needs a bar that closed AFTER the entry.

MEASURED on the gate-15 baseline (@ 9383324b2, 62 receipts, 2026-09-06): all 14 live BOS exits
fired 1.1–2.5 s after the entry fill (WETO 08-14 ×8, NXTC 07-14 ×6 — every one a Ross winner),
each followed by a bailout a second later. The frame's last close was the pre-entry bar (or the
forming bar's first prints): the structure read had not seen one close since the fill. WETO RH:
lockout-watch exemption 09:46:25 ET → fill 10.34 → "close below structure" at bid 10.29 one
second later → bailout 10.37 → the name ran to 12.95.

The guard is pure: until one full bar interval has elapsed since the fill the read is not a
confirmed close. The intrabar trail, the stop and the max-loss circuit own that window unchanged.

Runnable: pytest tests/test_bos_exit_post_entry_close.py -v  (DB-free)
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from app.services.trading.momentum_neural import live_runner as lr

T0 = datetime(2026, 8, 14, 13, 46, 31, tzinfo=timezone.utc)  # the WETO fill


def test_one_second_after_the_fill_is_not_a_confirmed_close():
    ready, dbg = lr.bos_exit_post_entry_close_ready(T0.isoformat(), T0 + timedelta(seconds=1), "5m")
    assert ready is False
    assert dbg["interval_s"] == 300.0 and dbg["since_entry_s"] == 1.0


def test_a_full_bar_after_the_fill_is():
    ready, dbg = lr.bos_exit_post_entry_close_ready(T0.isoformat(), T0 + timedelta(seconds=300), "5m")
    assert ready is True
    ready, _ = lr.bos_exit_post_entry_close_ready(T0.isoformat(), T0 + timedelta(seconds=299), "5m")
    assert ready is False
    ready, _ = lr.bos_exit_post_entry_close_ready(T0.isoformat(), T0 + timedelta(seconds=61), "1m")
    assert ready is True


def test_unreadable_basis_fails_open_to_the_prior_behaviour():
    # no fill stamp / bad interval => the BOS read runs as before (the guard never blocks a
    # real exit path on its own bookkeeping)
    for stamp in (None, "", "not-a-date"):
        ready, dbg = lr.bos_exit_post_entry_close_ready(stamp, T0 + timedelta(seconds=1), "5m")
        assert ready is True, stamp
        assert dbg["reason"] == "no_entry_fill_basis"
    ready, dbg = lr.bos_exit_post_entry_close_ready(T0.isoformat(), T0 + timedelta(seconds=1), "weird")
    assert ready is True and dbg["reason"] == "no_interval_basis"


def test_naive_and_aware_stamps_both_read():
    naive = T0.replace(tzinfo=None).isoformat()
    ready, dbg = lr.bos_exit_post_entry_close_ready(naive, T0 + timedelta(seconds=10), "5m")
    assert ready is False and dbg["since_entry_s"] == 10.0


def test_the_live_bos_block_asks_the_guard_before_it_fetches():
    src = inspect.getsource(lr.tick_live_session)
    i = src.find("ROSS GAP 2: LIVE CLOSE-BELOW-STRUCTURE (BOS) EXIT")
    j = src.find('_emit(db, sess, "live_bos_exit", {', i)
    block = src[i:j]
    k = block.find('bos_exit_post_entry_close_ready(')
    f = block.find('_replay_aware_fetch_ohlcv_df(sess.symbol, interval=_bos_iv, period="5d")')
    assert 0 < k < f, "the guard must run before the frame fetch"
    assert '"live_bos_exit_deferred_no_post_entry_close"' in block
    assert 'le.get("bos_exit_warmup_noted")' in block
