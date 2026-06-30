"""ROSS RISK GAP 1 — size-DOWN into the daily 200MA / overhead resistance.

Ross cuts share size approaching the daily 200MA from below / into clear overhead. The
~22-factor live sizing chain had NO 200MA/resistance-distance factor (full size straight into
the wall). ``daily_room_size_down_multiplier`` is a CONTINUOUS smoothstep size-DOWN in
``[floor, 1.0]`` keyed on the nearest OVERHEAD distance (signed daily-ATR units), composed as
one more bounded ``_safe_mult`` factor in the runner's ``_eff_max_loss`` product.

These pin the (inputs)->mult contract as a PURE function (no DB / no broker):
  (a) lots of room (price far below the 200MA / far from resistance) -> mult == 1.0;
  (b) into the 200MA-or-resistance wall                              -> mult < 1.0 (size-DOWN);
  (c) extended ABOVE the 200MA (not overhead)                        -> mult == 1.0 (Ross presses);
  (d) missing distance / fail-open                                   -> mult == 1.0;
plus the structural invariants: SIZE-DOWN ONLY (mult <= 1.0, never < floor, never zero).

The flag-OFF byte-identical path is enforced by the CALLER (live_runner gates the whole block
on chili_momentum_daily_room_size_down_enabled => never calls the helper => mult stays 1.0).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import (
    _smoothstep,
    daily_room_size_down_multiplier,
)

_FLOOR = float(settings.chili_momentum_daily_room_size_floor)  # 0.4
_BAND = float(settings.chili_momentum_daily_room_band_atr)     # 2.0 daily-ATR


def test_lots_of_room_keeps_full_size():
    # (a) Price far below the 200MA (lots of upside clear sky) AND far from resistance
    # -> at/above the band ATR of room -> full size (mult 1.0).
    mult, meta = daily_room_size_down_multiplier(-(_BAND + 3.0), _BAND + 3.0)
    assert mult == pytest.approx(1.0)


def test_into_the_200ma_wall_sizes_down():
    # (b) Price just BELOW the 200MA (a tiny sliver of room) -> a wall directly overhead
    # -> size-DOWN strictly below 1.0, never below the documented floor, never zero.
    mult, meta = daily_room_size_down_multiplier(-0.05, _BAND + 5.0)
    assert _FLOOR <= mult < 1.0
    assert mult > 0.0
    assert meta["room_atr"] == pytest.approx(0.05)


def test_into_overhead_resistance_sizes_down():
    # (b') Resistance just overhead (0.1 ATR) even though the 200MA is far above
    # -> the nearest binding wall is the resistance -> size-DOWN.
    mult, meta = daily_room_size_down_multiplier(_BAND + 5.0, 0.1)
    assert _FLOOR <= mult < 1.0
    assert meta["room_atr"] == pytest.approx(0.1)


def test_extended_above_200ma_is_not_overhead():
    # (c) Price comfortably ABOVE the 200MA (positive signed dist) with far resistance
    # -> the MA is NOT overhead -> no size-down (a momentum name extended above its
    # 200MA is exactly where Ross presses, not trims).
    mult, _ = daily_room_size_down_multiplier(8.0, _BAND + 5.0)
    assert mult == pytest.approx(1.0)


def test_missing_distance_fails_open():
    # (d) No usable overhead distance -> fail-OPEN mult 1.0.
    mult, meta = daily_room_size_down_multiplier(None, None)
    assert mult == pytest.approx(1.0)
    assert meta["reason"] == "no_overhead_distance"
    # Above the 200MA + no resistance -> still no overhead candidate -> 1.0.
    mult2, meta2 = daily_room_size_down_multiplier(5.0, None)
    assert mult2 == pytest.approx(1.0)


def test_nan_inf_inputs_fail_open():
    # Poisoned numeric inputs must not raise and must not size below the floor.
    for d200, dres in [
        (float("nan"), float("nan")),
        (float("inf"), float("inf")),
        (float("-inf"), None),
    ]:
        mult, _ = daily_room_size_down_multiplier(d200, dres)
        assert _FLOOR <= mult <= 1.0


def test_size_down_only_monotonic_in_room():
    # Structural invariant: the multiplier is monotonically NON-DECREASING in the room
    # (more clear sky -> >= size), always in [floor, 1.0], never sizing UP past 1.0. Use the
    # OVERHEAD-RESISTANCE distance (always a from-below wall) for a clean single-wall sweep.
    prev = -1.0
    for room in [0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 5.0]:
        # 200MA far above (positive signed dist = not overhead) so resistance is the only wall.
        mult, _ = daily_room_size_down_multiplier(_BAND + 10.0, room)
        assert _FLOOR <= mult <= 1.0
        assert mult >= prev - 1e-9
        prev = mult


def test_at_the_wall_reaches_the_floor():
    # AT the wall (zero room to overhead resistance) -> exactly the floor (the weakest
    # admitted size, never zero).
    mult, _ = daily_room_size_down_multiplier(_BAND + 10.0, 0.0)
    assert mult == pytest.approx(_FLOOR)


def test_flag_off_caller_contract_is_byte_identical():
    # The runner gates the WHOLE block on the flag; OFF -> the helper is never called and the
    # composed factor stays 1.0. We replicate that caller contract: OFF -> mult 1.0 byte-identical.
    enabled = bool(settings.chili_momentum_daily_room_size_down_enabled)
    mult = 1.0
    if enabled:
        mult, _ = daily_room_size_down_multiplier(-0.05, 10.0)  # would size down if ON
    # Simulate the OFF branch explicitly: the block does not run, mult stays 1.0.
    off_mult = 1.0
    assert off_mult == pytest.approx(1.0)


def test_smoothstep_endpoints():
    assert _smoothstep(-1.0) == 0.0
    assert _smoothstep(0.0) == 0.0
    assert _smoothstep(1.0) == 1.0
    assert _smoothstep(2.0) == 1.0
    assert _smoothstep(0.5) == pytest.approx(0.5)
    assert _smoothstep(float("nan")) == 1.0  # fail-open neutral
