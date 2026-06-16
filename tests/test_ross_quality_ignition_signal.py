"""Selection fix (2026-06-16 live): the WS ignition loop emits an exploding name's
move as ``todays_change_perc`` (ignition_loop.py:346), but ross_momentum._extract_pillars
did NOT read that key — so every ignition-discovered mover read momentum=None →
Ross quality 0.00 "generic setup" → out-ranked by smaller, fully-enriched names and
never armed. SLBT ripped +479% premarket and was never armed for exactly this reason.
The one-line fix adds ``todays_change_perc`` (the vendor change-vs-prior-close field)
to the momentum key list. These tests pin the fix + the no-regression parity.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.ross_momentum import _extract_pillars, score_universe


def _ignition_signal(move_pct: float) -> dict:
    # the EXACT shape ignition_loop._score_symbol builds (ignition_loop.py:342-349)
    return {
        "ticker": "SLBT",
        "direction": "long",
        "todays_change_perc": float(move_pct),
        "signal_type": "ws_ignition",
        "source": "ws_ignition",
    }


# ── the fix: todays_change_perc is now read as momentum ───────────────────────

def test_ignition_move_read_as_momentum():
    rvol, mom, liq, tl = _extract_pillars(_ignition_signal(479.6))
    assert mom == 479.6   # was None before the fix


def test_ignition_only_signal_scores_nonzero_quality():
    sc = score_universe({"SLBT": _ignition_signal(479.6)})
    assert sc["SLBT"].score > 0.2   # was exactly 0.00 ("generic setup") before


def test_bigger_mover_outranks_smaller_in_batch():
    # the regression that lost SLBT: a +479% ignition mover must NOT rank below a
    # +63% one when both reach the Ross quality scorer.
    batch = {"SLBT": _ignition_signal(479.6), "CRVO": _ignition_signal(63.0)}
    sc = score_universe(batch)
    assert sc["SLBT"].score >= sc["CRVO"].score


# ── no-regression parity: enriched signals are unchanged ──────────────────────

def test_daily_change_pct_still_wins():
    # a fully-enriched scanner signal already sets daily_change_pct; it must still
    # win over todays_change_perc (it is first in the OR-list) — byte-identical path.
    rvol, mom, liq, tl = _extract_pillars(
        {"daily_change_pct": 63.0, "vol_ratio": 8.0, "todays_change_perc": 999.0}
    )
    assert mom == 63.0


def test_no_change_field_still_none():
    # a signal with neither the old keys nor todays_change_perc still reads None
    # (fail-safe — no invented momentum from a fieldless dict).
    rvol, mom, liq, tl = _extract_pillars({"ticker": "X", "vol_ratio": 5.0})
    assert mom is None
