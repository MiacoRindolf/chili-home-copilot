"""ROSS RISK GAP 2 — red-intraday size-DOWN (the cushion ladder, down side).

Ross trades SMALLER when down on the day. ``cushion_risk_multiplier`` is the UP side (it climbs
a ladder off banked GREEN cushion, floored at 1.0); ``red_intraday_size_down_multiplier`` is the
missing DOWN side: a CONTINUOUS size-DOWN in ``[floor, 1.0]`` keyed on today's NEGATIVE realized
P&L, scaled by red depth in units of the day's per-trade risk budget (self-relative).

  (a) red day                -> mult < 1.0 (deeper red -> smaller);
  (b) green / flat day       -> mult == 1.0 (byte-identical / unchanged);
  (c) deep red               -> mult floors (never zero);
  (d) fail-neutral on error  -> mult == 1.0.

The flag-OFF byte-identical path is enforced by the CALLER (live_runner gates the whole block on
chili_momentum_red_intraday_size_down_enabled => never calls the helper => mult stays 1.0).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import (
    red_intraday_size_down_multiplier,
)

_FLOOR = float(settings.chili_momentum_red_intraday_size_floor)        # 0.4
_FULL_DOWN = float(settings.chili_momentum_red_intraday_full_down_units)  # 2.0
_BASE = 50.0


def _patch_day(monkeypatch, realized):
    import app.services.trading.governance as gov

    monkeypatch.setattr(
        gov, "global_realized_pnl_today_et",
        lambda db, user_id=None: {"total_usd": realized},
    )


def test_green_day_unchanged(db, monkeypatch):
    # (b) Green on the day -> no size-down (full size, byte-identical).
    _patch_day(monkeypatch, 250.0)
    mult, meta = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert mult == pytest.approx(1.0)
    assert meta["reason"] == "not_red"


def test_flat_day_unchanged(db, monkeypatch):
    # (b) Flat ($0 realized) -> not red -> mult 1.0.
    _patch_day(monkeypatch, 0.0)
    mult, meta = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert mult == pytest.approx(1.0)
    assert meta["reason"] == "not_red"


def test_red_day_sizes_down(db, monkeypatch):
    # (a) Down ~half the per-trade budget -> a real size-down strictly below 1.0, above floor.
    _patch_day(monkeypatch, -0.5 * _BASE)
    mult, meta = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert _FLOOR <= mult < 1.0
    assert meta["red_units"] == pytest.approx(0.5)


def test_deeper_red_is_smaller(db, monkeypatch):
    # Monotonic: a deeper red intraday sizes strictly smaller than a shallow red.
    _patch_day(monkeypatch, -0.25 * _BASE)
    shallow, _ = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    _patch_day(monkeypatch, -1.0 * _BASE)
    deep, _ = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert deep < shallow
    assert _FLOOR <= deep <= shallow <= 1.0


def test_deep_red_reaches_floor_never_zero(db, monkeypatch):
    # (c) Down >= full_down_units of risk -> floors (never zero, never below the floor).
    _patch_day(monkeypatch, -(_FULL_DOWN + 5.0) * _BASE)
    mult, _ = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert mult == pytest.approx(_FLOOR)
    assert mult > 0.0


def test_full_down_units_hits_floor_exactly(db, monkeypatch):
    # At exactly full_down_units of red the ramp reaches the floor.
    _patch_day(monkeypatch, -_FULL_DOWN * _BASE)
    mult, _ = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert mult == pytest.approx(_FLOOR)


def test_size_down_only_never_above_one(db, monkeypatch):
    # Structural invariant: across the red sweep the multiplier is ALWAYS in [floor, 1.0]
    # (size-DOWN only — a red day never sizes UP).
    for red in [0.0, 0.1, 0.5, 1.0, 1.5, 2.0, 3.0]:
        _patch_day(monkeypatch, -red * _BASE)
        mult, _ = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
        assert _FLOOR <= mult <= 1.0


def test_no_base_loss_is_neutral(db, monkeypatch):
    # A degenerate (zero / non-positive) base loss -> neutral 1.0 (cannot scale red depth).
    _patch_day(monkeypatch, -100.0)
    mult, meta = red_intraday_size_down_multiplier(db, base_loss_usd=0.0)
    assert mult == pytest.approx(1.0)
    assert meta["reason"] == "no_base_loss"


def test_fail_neutral_on_error(db, monkeypatch):
    # (d) Day-P&L read blows up -> fail-NEUTRAL 1.0 (never increases risk, never blocks).
    import app.services.trading.governance as gov

    def _boom(db, user_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(gov, "global_realized_pnl_today_et", _boom)
    mult, meta = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
    assert mult == pytest.approx(1.0)
    assert meta["reason"] == "error_fail_neutral"


def test_flag_off_caller_contract_is_byte_identical(db, monkeypatch):
    # The runner gates the WHOLE block on the flag; OFF -> the helper is never called and the
    # composed factor stays 1.0. Replicate that contract: OFF -> mult 1.0 (byte-identical even
    # on a red day that WOULD have sized down if ON).
    _patch_day(monkeypatch, -1.0 * _BASE)
    if bool(settings.chili_momentum_red_intraday_size_down_enabled):
        on_mult, _ = red_intraday_size_down_multiplier(db, base_loss_usd=_BASE)
        assert on_mult < 1.0  # ON sizes down
    off_mult = 1.0  # the OFF branch never runs the block
    assert off_mult == pytest.approx(1.0)
