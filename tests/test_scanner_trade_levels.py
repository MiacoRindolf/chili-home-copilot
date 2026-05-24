from __future__ import annotations

import pytest

from app.services.trading.scanner import _long_atr_trade_levels


def test_long_atr_trade_levels_rejects_negative_stop_geometry():
    assert (
        _long_atr_trade_levels(
            0.6949,
            1.5373,
            stop_mult=2.5,
            target_mult=3.0,
            crypto=False,
        )
        is None
    )


def test_long_atr_trade_levels_returns_sane_geometry():
    entry, stop, target = _long_atr_trade_levels(
        100.0,
        2.0,
        stop_mult=2.0,
        target_mult=3.0,
        crypto=False,
    )

    assert entry == pytest.approx(100.0)
    assert stop == pytest.approx(96.0)
    assert target == pytest.approx(106.0)
