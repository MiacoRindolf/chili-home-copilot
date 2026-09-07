"""Six front-side / flow strength markers survived recycle (2026-09-07).

Found while tracing a bench divergence: on JWEL 2026-08-10 the SAME tape at the SAME
second scored ``frontside`` 1.0000 in one arm and 0.7034 in the other, and the winner leg
was sized 169 -> 87 shares. Of ~25 risk multipliers only ``frontside`` differed.

The score itself turned out NOT to read any of these keys -- it is recomputed per tick from
six locals -- so this file does not claim to fix that divergence (60% of the score's weight
comes from ``_entry_df``, the 15m frame served by a process-global 600 s TTL cache in
``market_data.py`` whose key carries no clock, no session and no leg; that cache is the
carrier and needs its own A/B). What the trace DID establish is that six per-trade
measurements are written to ``le`` and cleared by nobody, so a recycled watcher can read the
PREVIOUS leg's reference. Two of them are live decision inputs, not just forensics.

This is the same shape as the burst-window stamp (#1275): a per-trade marker whose owner
says "the caller clears on exit or new position", and no caller did. That one killed every
re-entry 1-2 s after fill for weeks before it was measured.

Runnable: pytest tests/test_recycle_clears_frontside_state.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner as lr


# the two that are DECISION inputs on the next pass, and the four that corrupt the record
_DECISION_INPUTS = ("chase_defer_episode", "chase_defer_ticks", "flow_veto_latched",
                    "flow_veto_clear_since")
_STRENGTH_REFS = ("entry_tick_rate", "frontside_size_tilt")


def test_every_front_side_marker_is_cleared_on_recycle():
    for key in _DECISION_INPUTS + _STRENGTH_REFS:
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key


def test_the_reset_actually_pops_them():
    """The tuple is only a list; `_reset_entry_state_on_recycle` is what has to do the work,
    and it must report what it cleared (the `live_recycled` audit reads that)."""
    le = {k: "carried-over" for k in _DECISION_INPUTS + _STRENGTH_REFS}
    le["symbol_identity_do_not_touch"] = "PRESERVE"
    cleared = lr._reset_entry_state_on_recycle(le)
    for key in _DECISION_INPUTS + _STRENGTH_REFS:
        assert key not in le, key
        assert key in cleared, key
    # identity / cumulative state is NOT in the tuple and must survive
    assert le["symbol_identity_do_not_touch"] == "PRESERVE"


def test_the_burst_stamp_precedent_is_still_covered():
    """This fix is the same class as #1275. If the burst keys ever leave the tuple, the
    lesson has been lost and this file should fail loudly alongside them."""
    for key in ("burst_started_epoch", "burst_track", "burst_window_dbg"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key


def test_cross_cycle_state_is_still_deliberately_preserved():
    """The recycle contract's whole point is that SOME state must survive. Guard the
    documented survivors so a later 'clear everything' sweep cannot pass this file."""
    for key in ("trade_cycles", "realized_pnl_usd", "fees_usd_total",
                "per_symbol_fatigue", "post_exit_excursion_pending"):
        assert key not in lr._RECYCLE_ENTRY_STATE_KEYS, key


# ── the observability half: the tilt must be recordable on EVERY pass ─────────

def test_the_front_side_tilt_is_recorded_even_when_it_does_not_bite():
    """It was written only under `if _frontside_mult < 1.0 or _fs_defer:`, so the arm that
    sized at FULL strength left no per-term record -- and `mult == 1.0` is produced by three
    different states (strength >= s_hi, `strength is None`, and `stale_tape`) that the
    receipt could not tell apart. A divergence needs BOTH sides on the record."""
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('le["frontside_size_tilt"] = {')
    assert i > 0
    # the guard is the last CODE line before the write -- match on that, not on a window,
    # because the incident comment above it necessarily quotes the old conditional
    before = [ln.strip() for ln in src[:i].splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    assert before[-1] == "if True:", before[-1]
    # the fields that say whether it BIT are still on the record
    block = src[i:i + 2600]
    for field in ('"mult"', '"defer"', '"strength"', '"adaptive_warm"', '"adaptive_n"'):
        assert field in block, field


def test_the_receipt_carries_the_frame_the_tick_actually_read():
    """60% of the score's weight is frame-derived and the frame comes from a 600 s TTL cache
    keyed without a clock. Two receipts standing at the same second must be comparable on
    WHICH frame each was holding, or the divergence is unattributable."""
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('le["frontside_size_tilt"] = {')
    block = src[i:i + 2200]
    assert '"frame_last_bar": _fs_frame_stamp' in block
    assert '"frame_bars": _fs_frame_bars' in block
    # and it is derived from the frame, never used to decide
    assert "_fs_frame_stamp = str(_entry_df.index[-1])" in src


def test_the_tilt_travels_with_the_entry_submission():
    """It lived on `le` and was read by nothing -- no receipt, no log line, no event."""
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('_emit(db, sess, "live_entry_submitted", {')
    assert i > 0
    block = src[i:i + 2400]
    assert '"frontside_size_tilt": le.get("frontside_size_tilt")' in block
    # the sizing basis it explains is still alongside it
    assert '"risk_mults": le.get("risk_mults")' in block
