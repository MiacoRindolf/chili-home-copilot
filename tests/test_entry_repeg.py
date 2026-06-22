"""G1 marketable re-peg entry chase (2026-06-22): bound-safety unit tests.

The chase re-prices a left-behind entry UP to the live ask, but the price is bounded by a
CUMULATIVE ceiling off the ORIGINAL limit (one adaptive spread budget), so total entry
drift — and the 2:1 R:R against the fixed structural stop — can never erode past that
budget no matter how many re-pegs accumulate, and it can never sweep a thin book.
"""
from __future__ import annotations

from app.services.trading.momentum_neural import live_runner as lr


def _ceiling(orig: float, emb: float) -> float:
    return orig * (1.0 + lr._adaptive_live_max_spread_bps(emb) / 10_000.0)


def test_repeg_price_is_capped_at_cumulative_ceiling():
    orig, emb = 10.0, 500.0
    ceiling = _ceiling(orig, emb)
    px = lr._entry_repeg_price(original_limit_px=orig, live_ask=ceiling - 0.001, expected_move_bps=emb)
    assert px is not None
    assert px <= ceiling + 1e-9          # never above the cumulative budget
    assert px >= ceiling - 0.001 - 1e-9  # marketable (>= the live ask)


def test_repeg_returns_none_when_ask_ran_past_the_ceiling():
    # The move left for good -> do NOT chase past the budget; caller falls back to re-watch.
    orig, emb = 10.0, 500.0
    ceiling = _ceiling(orig, emb)
    assert lr._entry_repeg_price(original_limit_px=orig, live_ask=ceiling + 0.50, expected_move_bps=emb) is None


def test_repeg_capped_even_with_enormous_expected_move():
    # A huge expected move cannot blow the ceiling past the adaptive spread cap.
    orig, emb = 5.0, 99_999.0
    ceiling = _ceiling(orig, emb)
    px = lr._entry_repeg_price(original_limit_px=orig, live_ask=ceiling - 0.01, expected_move_bps=emb)
    assert px is not None and px <= ceiling + 1e-9


def test_repeg_invalid_inputs_return_none():
    assert lr._entry_repeg_price(original_limit_px=0.0, live_ask=10.0, expected_move_bps=100.0) is None
    assert lr._entry_repeg_price(original_limit_px=10.0, live_ask=0.0, expected_move_bps=100.0) is None
    assert lr._entry_repeg_price(original_limit_px=10.0, live_ask=-1.0, expected_move_bps=None) is None


def test_repeg_total_drift_bounded_by_one_spread_budget():
    # No matter the live ask, the returned price never exceeds original*(1+spread_budget) —
    # so R:R against the FIXED stop is bounded regardless of how many re-pegs accumulate.
    orig, emb = 8.0, 1200.0
    ceiling = _ceiling(orig, emb)
    for ask in (orig * 1.001, orig * 1.01, ceiling - 1e-6):
        px = lr._entry_repeg_price(original_limit_px=orig, live_ask=ask, expected_move_bps=emb)
        assert px is None or px <= ceiling + 1e-9
