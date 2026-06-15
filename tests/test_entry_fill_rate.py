"""Entry fill-rate fix — the equity 0-fill blocker.

The marketable limit was cancelled the instant the bid pipped one tick past it (the
`entry_limit_left_behind` reason firing on the 2s WS cadence) while it was at the front
of the book and ABOUT TO FILL → orphaned (BATL/CTNT/SDOT). These prove: the chase ceiling
TOLERATES a resting limit without re-pegging, the stop-breach invariant is unconditional,
and BOTH new helpers default to TODAY's exact values (parity / kill-switch).
"""
from app.config import settings
from app.services.trading.momentum_neural.live_runner import (
    _adaptive_notional_guard_multiplier,
    _entry_chase_ceiling_px,
    _notional_guard_multiplier,
    _pending_entry_cancel_reason,
)

import pytest


def _reason(bid, *, limit=10.0, stop=9.0, ceiling=0.0, elapsed=1.0):
    return _pending_entry_cancel_reason(
        bid=bid, structural_stop=stop, limit_px=limit, elapsed_s=elapsed,
        rest_bars=2.0, interval_s=300.0, chase_ceiling_px=ceiling)


@pytest.fixture(autouse=True)
def _knobs():
    old = (settings.chili_momentum_entry_chase_ceiling_bps,
           settings.chili_momentum_entry_chase_move_ratio,
           settings.chili_momentum_entry_guard_move_ratio)
    yield
    (settings.chili_momentum_entry_chase_ceiling_bps,
     settings.chili_momentum_entry_chase_move_ratio,
     settings.chili_momentum_entry_guard_move_ratio) = old


# ── _pending_entry_cancel_reason ─────────────────────────────────────────────

def test_cancel_parity_default_ceiling_zero():
    """ceiling=0 ⇒ ceiling collapses to limit_px ⇒ bid>limit cancels (today's behavior)."""
    assert _reason(10.01, ceiling=0.0) == "entry_limit_left_behind"   # pipped past limit
    assert _reason(9.99, ceiling=0.0) is None                         # under limit -> rest


def test_cancel_tolerates_bid_within_ceiling():
    """With a ceiling above the limit, a bid pipping just past the limit is TOLERATED
    (the order rests + fills) — but a bid past the CEILING is still abandoned."""
    ceil = 10.0 * 1.006  # ~60 bps over the 10.00 limit
    assert _reason(10.03, limit=10.0, ceiling=ceil) is None                # within ceiling -> rest
    assert _reason(10.10, limit=10.0, ceiling=ceil) == "entry_limit_left_behind"  # past ceiling


def test_stop_breach_is_first_and_unconditional():
    """A bid below the structural stop INVALIDATES the setup FIRST — even with a high
    chase ceiling (the invariant: never fill a dead setup)."""
    assert _reason(8.5, limit=10.0, stop=9.0, ceiling=10.0 * 1.05) == "entry_invalidated_stop_breach"


def test_rest_backstop_still_fires():
    assert _reason(9.99, limit=10.0, ceiling=10.0 * 1.01, elapsed=10_000.0) == "entry_rest_backstop"


# ── _entry_chase_ceiling_px ──────────────────────────────────────────────────

def test_chase_ceiling_parity_when_bps_zero():
    settings.chili_momentum_entry_chase_ceiling_bps = 0.0
    assert _entry_chase_ceiling_px(limit_px=10.0, expected_move_bps=500.0) == 10.0  # == limit (parity)


def test_chase_ceiling_widens_above_limit_and_caps():
    settings.chili_momentum_entry_chase_ceiling_bps = 60.0
    settings.chili_momentum_entry_chase_move_ratio = 0.25
    ceil = _entry_chase_ceiling_px(limit_px=10.0, expected_move_bps=200.0)
    assert ceil > 10.0                                   # widened above the limit
    assert ceil <= 10.0 * (1.0 + 300.0 / 10_000.0)       # never past the ~300bps abs spread cap


# ── _adaptive_notional_guard_multiplier ──────────────────────────────────────

def test_guard_parity_when_ratio_zero():
    settings.chili_momentum_entry_guard_move_ratio = 0.0
    assert _adaptive_notional_guard_multiplier(expected_move_bps=500.0) == _notional_guard_multiplier()


def test_guard_widens_on_volatile_name_and_caps():
    settings.chili_momentum_entry_guard_move_ratio = 0.5
    m = _adaptive_notional_guard_multiplier(expected_move_bps=200.0)
    assert m >= _notional_guard_multiplier()             # at least the base 25bps
    assert m <= 1.0 + 300.0 / 10_000.0                   # capped at the spread cap
