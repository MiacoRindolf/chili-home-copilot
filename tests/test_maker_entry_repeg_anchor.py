"""A maker entry could ratchet up one re-peg at a time, unbounded (2026-09-07).

The taker branch sets an anchor and says why: "the ORIGINAL limit bounds the cumulative
drift (the R:R guard), and the re-peg counter resets per fresh entry." The two MAKER
branches never set it -- and the reader falls back silently:

    original_limit_px=float(le.get("entry_original_limit_px") or _lim_px or 0.0)

With the key absent that resolves to the CURRENT limit, so every re-peg re-anchored on the
price it had just chased to. The bound did not exist for a maker entry.

MEASURED on PPBT 2026-09-02. At the identical microsecond 12:45:15.208421 with the identical
BBO (2.13 / 2.14), the baseline took the ask and filled 302 @2.14 in 1.07 s; the other arm
was inside the 90-second bailout-maker window, rested at the 2.13 bid, was left behind, and
re-pegged three times before filling 232 @2.15 -- 20.19 s late. Both arms then SOLD that leg
at the same microsecond and the same price, so the entire -$29.59 is the entry, and 94% of
it is the 70 missing shares rather than the penny of price.

Runnable: pytest tests/test_maker_entry_repeg_anchor.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner as lr


def test_the_anchor_sets_both_keys_the_repeg_reader_needs():
    le: dict = {}
    lr._set_entry_repeg_anchor(le, 2.13)
    assert le["entry_original_limit_px"] == 2.13
    assert le["entry_repeg_count"] == 0
    # a fresh entry resets the counter even when the key already exists
    le["entry_repeg_count"] = 4
    lr._set_entry_repeg_anchor(le, 2.20)
    assert le["entry_original_limit_px"] == 2.20 and le["entry_repeg_count"] == 0


def test_all_three_entry_branches_anchor_not_just_the_taker():
    """maker_entry, bailout_maker and taker. The two maker ones were the gap."""
    src = inspect.getsource(lr.tick_live_session)
    assert src.count("_set_entry_repeg_anchor(le, entry_limit_px)") == 3
    i = src.find("if _maker_entry:")
    j = src.find("le[\"entry_notional_guard\"]", i)
    assert 0 < i < j
    block = src[i:j]
    for branch in ("if _maker_entry:", "elif _bailout_maker:", "else:"):
        assert branch in block, branch
    # no branch may set the key inline any more -- one writer
    assert 'le["entry_original_limit_px"] =' not in block


def test_the_repeg_reader_still_falls_back_so_the_change_is_additive():
    """The fallback stays: this fix supplies the key, it does not remove the safety net for
    any path that still lacks it."""
    src = inspect.getsource(lr)
    assert 'le.get("entry_original_limit_px") or _lim_px or 0.0' in src


def test_the_anchor_is_cleared_on_recycle():
    assert "entry_original_limit_px" in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "entry_repeg_count" in lr._RECYCLE_ENTRY_STATE_KEYS
