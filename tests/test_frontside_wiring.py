"""FRONT-SIDE SIZE-TILT WIRING — the dormant 9b03b7e tilt delivered into LIVE entry sizing.

The pure ``front_side_strength_score`` / ``front_side_size_tilt`` helpers (committed +
unit-tested in ``test_frontside_adaptive_strength.py``) were DORMANT — nothing called them
on the live entry path. ``live_runner.tick_live_session`` now wires them in: the strength
score maps to a SIZE-DOWN multiplier that composes into the ``_eff_max_loss`` risk budget
(alongside the existing levers, under the SAME ``_safe_mult`` sanitize + ``base*3`` clamp),
and ``compute_risk_first_quantity`` re-derives integer shares off the tilted budget.

These tests pin the (inputs)->tilted_size contract — the load-bearing mapping the wiring
performs — as PURE functions (no DB / no broker), exactly mirroring the call site:

  (a) flag-ON + STRONG strength  -> mult ~1.0   -> shares ~unchanged vs base;
  (b) flag-ON + WEAK   strength  -> mult < 1.0  -> shares REDUCED (size-DOWN, never up);
  (c) flag-OFF                   -> mult == 1.0 -> shares BYTE-IDENTICAL to base;
  (d) STALE / None strength      -> mult == 1.0 -> shares BYTE-IDENTICAL (fail-OPEN).

Plus the structural invariants: the tilt only SHRINKS the budget (never raises it), and the
shares it yields are always <= the untilted base shares.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.live_runner import _safe_mult
from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity
from app.services.trading.momentum_neural.ross_momentum import (
    FRONTSIDE_SIZE_FLOOR,
    front_side_size_tilt,
    front_side_strength_score,
)

# Representative live-sizing inputs (a mid-priced low-float momentum name).
_ENTRY_PRICE = 10.0
_ATR_PCT = 0.04
_BASE_MAX_LOSS = 50.0
_MAX_NOTIONAL = 5_000.0
_STOP_ATR_MULT = 0.60
_INC = 1.0   # whole shares (equity)
_MN = 1.0


def _base_shares() -> float:
    qty, _ = compute_risk_first_quantity(
        entry_price=_ENTRY_PRICE,
        atr_pct=_ATR_PCT,
        max_loss_usd=_BASE_MAX_LOSS,
        max_notional_ceiling_usd=_MAX_NOTIONAL,
        base_increment=_INC,
        base_min_size=_MN,
        stop_atr_mult=_STOP_ATR_MULT,
    )
    return qty


def _tilted_shares(
    *,
    enabled: bool,
    ofi_level=None,
    ofi_slope=None,
    signed_tape=None,
    stale_tape: bool = False,
    size_floor: float = FRONTSIDE_SIZE_FLOOR,
    defer_below: float = 0.15,
):
    """Replicate the live_runner wiring EXACTLY: strength -> tilt -> _eff_max_loss product
    (sanitized + clamped to base*3) -> compute_risk_first_quantity -> shares."""
    mult = 1.0
    strength = None
    defer = False
    if enabled:
        strength = front_side_strength_score(
            ofi_level=ofi_level,
            ofi_slope=ofi_slope,
            signed_tape=signed_tape,
        )
        mult, defer, _detail = front_side_size_tilt(
            strength,
            size_floor=size_floor,
            defer_below=defer_below,
            stale_tape=stale_tape,
            enabled=True,
        )
    # The SAME composition the runner does at the _eff_max_loss site.
    eff_max_loss = min(
        float(_BASE_MAX_LOSS) * _safe_mult(mult),
        float(_BASE_MAX_LOSS) * 3.0,
    )
    qty, _ = compute_risk_first_quantity(
        entry_price=_ENTRY_PRICE,
        atr_pct=_ATR_PCT,
        max_loss_usd=eff_max_loss,
        max_notional_ceiling_usd=_MAX_NOTIONAL,
        base_increment=_INC,
        base_min_size=_MN,
        stop_atr_mult=_STOP_ATR_MULT,
    )
    return qty, mult, strength, defer


def test_base_shares_are_positive():
    # Sanity: the untilted base sizing yields a real, positive share count.
    assert _base_shares() > 0


def test_strong_strength_keeps_full_size():
    # (a) Strong, agreeing tape (high OFI level + rising slope + buy tape) -> strength high
    # -> mult ~1.0 -> shares ~unchanged vs the untilted base.
    base = _base_shares()
    qty, mult, strength, _defer = _tilted_shares(
        enabled=True, ofi_level=1.0, ofi_slope=1.0, signed_tape=1.0,
    )
    assert strength is not None and strength >= 0.6
    assert mult == pytest.approx(1.0, abs=1e-9)
    assert qty == pytest.approx(base)


def test_weak_strength_reduces_size():
    # (b) Weak, disagreeing tape (negative OFI level + falling slope + sell tape) -> strength
    # low -> mult < 1.0 -> shares strictly REDUCED (but never zero / never below the floor).
    base = _base_shares()
    qty, mult, strength, _defer = _tilted_shares(
        enabled=True, ofi_level=-1.0, ofi_slope=-1.0, signed_tape=-1.0,
    )
    assert strength is not None and strength <= 0.4
    assert FRONTSIDE_SIZE_FLOOR <= mult < 1.0
    assert qty < base
    assert qty > 0  # size-DOWN never zeros the order


def test_flag_off_is_byte_identical():
    # (c) Flag OFF -> the block never runs -> mult 1.0 -> shares byte-identical to base.
    base = _base_shares()
    qty, mult, strength, defer = _tilted_shares(
        enabled=False, ofi_level=-1.0, ofi_slope=-1.0, signed_tape=-1.0,
    )
    assert mult == 1.0
    assert strength is None
    assert defer is False
    assert qty == pytest.approx(base)


def test_stale_tape_fails_open_to_full_size():
    # (d) Stale tape -> stale_tape=True -> tilt fail-OPEN -> mult 1.0 -> byte-identical.
    base = _base_shares()
    qty, mult, _strength, defer = _tilted_shares(
        enabled=True, ofi_level=-1.0, ofi_slope=-1.0, signed_tape=-1.0, stale_tape=True,
    )
    assert mult == 1.0
    assert defer is False
    assert qty == pytest.approx(base)


def test_no_informative_term_fails_open_to_full_size():
    # All micro inputs None (the flow read returned None / no tape) -> strength None ->
    # mult 1.0 -> byte-identical. stale != weak: absence of signal is full size, not a shrink.
    base = _base_shares()
    qty, mult, strength, _defer = _tilted_shares(
        enabled=True, ofi_level=None, ofi_slope=None, signed_tape=None,
    )
    assert strength is None
    assert mult == 1.0
    assert qty == pytest.approx(base)


def test_tilt_only_shrinks_never_raises():
    # Structural invariant across the strength sweep: the tilted shares are ALWAYS <= the
    # untilted base shares (size-DOWN only) and the multiplier never exceeds 1.0.
    base = _base_shares()
    for lvl, slp, tape in [
        (1.0, 1.0, 1.0),
        (0.0, 0.0, 0.0),
        (-0.5, 0.0, 0.2),
        (-1.0, -1.0, -1.0),
        (0.5, -1.0, -0.5),
    ]:
        qty, mult, _s, _d = _tilted_shares(
            enabled=True, ofi_level=lvl, ofi_slope=slp, signed_tape=tape,
        )
        assert mult <= 1.0 + 1e-9
        assert qty <= base + 1e-9


def test_weakest_admitted_still_trades_at_floor():
    # The weakest possible front-side still trades at >= size_floor of the base risk budget
    # (no hard veto): a floored mult yields a positive share count, never zero.
    qty, mult, _s, _d = _tilted_shares(
        enabled=True, ofi_level=-1.0, ofi_slope=-1.0, signed_tape=-1.0,
    )
    assert mult >= FRONTSIDE_SIZE_FLOOR - 1e-9
    assert qty > 0
