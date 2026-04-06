"""Unit tests for backtest win-rate scale helpers."""
from __future__ import annotations

import pytest

from app.services.trading.backtest_metrics import (
    backtest_win_rate_db_to_display_pct,
    backtest_win_rate_display_pct_for_compare,
    json_win_rate_to_display_pct,
    normalize_win_rate_for_db,
)


@pytest.mark.parametrize(
    "inp, expected",
    [
        (None, None),
        (0.55, 0.55),
        (55.0, 0.55),
        (100.0, 1.0),
        (150.0, 1.0),
        (-1.0, 0.0),
    ],
)
def test_normalize_win_rate_for_db(inp, expected):
    out = normalize_win_rate_for_db(inp)
    if expected is None:
        assert out is None
    else:
        assert out == pytest.approx(expected)


@pytest.mark.parametrize(
    "inp, expected",
    [
        (None, None),
        (0.55, 55.0),
        (1.0, 100.0),
        (55.0, 55.0),
        (0.0, 0.0),
    ],
)
def test_backtest_win_rate_db_to_display_pct(inp, expected):
    out = backtest_win_rate_db_to_display_pct(inp)
    if expected is None:
        assert out is None
    else:
        assert out == pytest.approx(expected)


def test_backtest_win_rate_display_pct_for_compare_none():
    assert backtest_win_rate_display_pct_for_compare(None) == 0.0


@pytest.mark.parametrize(
    "inp, expected",
    [
        (None, None),
        (0.6, 60.0),
        (60.0, 60.0),
        ("55.5", 55.5),
    ],
)
def test_json_win_rate_to_display_pct(inp, expected):
    out = json_win_rate_to_display_pct(inp)
    if expected is None:
        assert out is None
    else:
        assert out == pytest.approx(expected)
