"""A resting tranche covers what is left of it, not what it was placed at.

THE DEFECT (2026-09-09). `scale_limit_qty` is the size a scale tranche was PLACED at and
is never reduced; its fills accumulate separately in `scale_limit_adopted_qty`. Two
consumers read the placed size as though it were live coverage, and both are position
arithmetic:

  * the deadman head guard arms the stop for `position - tranche`, where `position` is the
    CURRENT broker quantity — already shrunk by whatever the tranche filled. With a tranche
    of f that has filled k, the stop was armed for Q-k-f while the position was Q-k and the
    tranche still covered f-k. Exactly k shares carried no protection, silently.
  * `_alpaca_deadman_reserved_tranche_quantity`, whose docstring promises it "MIRRORS that
    function's head guard exactly".

The invariant both owe: deadman + resting tranche == position, at every k.

DB-free — the helper is pure.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.live_runner import _tranche_open_quantity as opn


def test_an_untouched_tranche_covers_its_whole_placed_size():
    assert opn({"scale_limit_qty": 355.0, "scale_limit_adopted_qty": 0.0}) == pytest.approx(355.0)


@pytest.mark.parametrize("filled,expected", [(1.0, 354.0), (100.0, 255.0), (354.0, 1.0)])
def test_a_partly_filled_tranche_covers_only_the_remainder(filled, expected):
    assert opn({"scale_limit_qty": 355.0, "scale_limit_adopted_qty": filled}) == pytest.approx(expected)


def test_a_fully_filled_tranche_covers_nothing():
    """Its order id may not be released yet; it must not be credited with coverage."""
    assert opn({"scale_limit_qty": 355.0, "scale_limit_adopted_qty": 355.0}) == 0.0


def test_an_over_filled_tranche_never_returns_a_negative_reserve():
    """A negative reserve is the same defect inverted — it would ADD phantom coverage."""
    assert opn({"scale_limit_qty": 355.0, "scale_limit_adopted_qty": 400.0}) == 0.0


@pytest.mark.parametrize("k", [0.0, 1.0, 37.0, 200.0, 354.0, 355.0])
def test_the_protection_invariant_holds_at_every_fill_level(k):
    """THE PROPERTY THAT WAS BROKEN. Position Q-k must be exactly covered by the deadman
    plus what the tranche still rests for. With the placed size the deadman was armed for
    Q-k-f and the sum came to Q-2k — k shares short at every non-zero k."""
    Q, f = 1000.0, 355.0
    le = {"scale_limit_qty": f, "scale_limit_adopted_qty": k}
    position = Q - k
    tranche_covers = opn(le)
    deadman_covers = position - tranche_covers
    assert deadman_covers + tranche_covers == pytest.approx(position), "shares left naked"
    assert deadman_covers == pytest.approx(Q - f) or k >= f


@pytest.mark.parametrize("le", [None, {}, "not a dict", {"scale_limit_qty": None},
                                {"scale_limit_qty": "abc", "scale_limit_adopted_qty": "x"}])
def test_unreadable_state_yields_no_reserve_rather_than_raising(le):
    """It runs inside the protective re-arm; it must degrade, and degrade toward claiming
    LESS coverage, never more."""
    assert opn(le) == 0.0


def test_both_consumers_agree_by_construction():
    """The reserve function's docstring promises it mirrors the head guard. Fixing one and
    not the other is how a certified reserve stops describing real coverage, so they now
    share one implementation."""
    import inspect
    from app.services.trading.momentum_neural import live_runner as LR
    src = inspect.getsource(LR._alpaca_deadman_reserved_tranche_quantity)
    assert "_tranche_open_quantity(le)" in src
    guard = inspect.getsource(LR._ensure_alpaca_deadman_stop)
    assert "_tranche_open_quantity(le)" in guard
    assert "scale_limit_qty" not in src, (
        "the reserve must not read the placed size directly any more"
    )
