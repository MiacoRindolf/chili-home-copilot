from __future__ import annotations

from types import SimpleNamespace

from app.config import BRAIN_QUEUE_MARKET_HOURS_STOCK_LANE_DEFAULT
from app.services.trading.learning import (
    _apply_market_hours_stock_lane,
    _queue_exploration_cap,
)

_QUEUE_PATTERN_IDS = [11, 12, 13, 14, 15]
_STOCK_ONLY_PATTERN_IDS = {12, 13, 15}
_NON_STOCK_PATTERN_IDS = [11, 14]
_STOCK_LANE_LIMIT = 2
_EXPECTED_BOUNDED_FILTER = [11, 12, 13, 14]
_EXPECTED_DEFERRED_AFTER_BOUNDED_LANE = (
    len(_STOCK_ONLY_PATTERN_IDS) - _STOCK_LANE_LIMIT
)
_ENV_STOCK_LANE_LIMIT = BRAIN_QUEUE_MARKET_HOURS_STOCK_LANE_DEFAULT + 2
_EXPLORATION_MAX = 40
_MARKET_HOURS_EXPLORATION_MAX = 3


def test_market_hours_stock_lane_keeps_bounded_stock_ids_in_queue_order():
    filtered, kept_stock, deferred_stock = _apply_market_hours_stock_lane(
        _QUEUE_PATTERN_IDS,
        _STOCK_ONLY_PATTERN_IDS,
        SimpleNamespace(
            chili_brain_queue_market_hours_stock_lane_max_patterns=_STOCK_LANE_LIMIT
        ),
    )

    assert filtered == _EXPECTED_BOUNDED_FILTER
    assert kept_stock == _STOCK_LANE_LIMIT
    assert deferred_stock == _EXPECTED_DEFERRED_AFTER_BOUNDED_LANE


def test_market_hours_stock_lane_zero_preserves_full_pause():
    filtered, kept_stock, deferred_stock = _apply_market_hours_stock_lane(
        _QUEUE_PATTERN_IDS,
        _STOCK_ONLY_PATTERN_IDS,
        SimpleNamespace(chili_brain_queue_market_hours_stock_lane_max_patterns=0),
    )

    assert filtered == _NON_STOCK_PATTERN_IDS
    assert kept_stock == 0
    assert deferred_stock == len(_STOCK_ONLY_PATTERN_IDS)


def test_settings_accepts_market_hours_stock_lane_env(monkeypatch):
    monkeypatch.setenv(
        "CHILI_BRAIN_QUEUE_MARKET_HOURS_STOCK_LANE_MAX_PATTERNS",
        str(_ENV_STOCK_LANE_LIMIT),
    )

    from app.config import Settings

    settings = Settings()

    assert (
        settings.chili_brain_queue_market_hours_stock_lane_max_patterns
        == _ENV_STOCK_LANE_LIMIT
    )


def test_market_hours_exploration_cap_defaults_to_zero():
    cfg = SimpleNamespace(brain_queue_exploration_max=_EXPLORATION_MAX)

    assert _queue_exploration_cap(cfg, market_hours_active=True) == 0
    assert _queue_exploration_cap(cfg, market_hours_active=False) == _EXPLORATION_MAX


def test_market_hours_exploration_cap_is_configurable():
    cfg = SimpleNamespace(
        brain_queue_exploration_max=_EXPLORATION_MAX,
        chili_brain_queue_market_hours_exploration_max=_MARKET_HOURS_EXPLORATION_MAX,
    )

    assert (
        _queue_exploration_cap(cfg, market_hours_active=True)
        == _MARKET_HOURS_EXPLORATION_MAX
    )


def test_settings_accepts_market_hours_exploration_env(monkeypatch):
    monkeypatch.setenv(
        "CHILI_BRAIN_QUEUE_MARKET_HOURS_EXPLORATION_MAX",
        str(_MARKET_HOURS_EXPLORATION_MAX),
    )

    from app.config import Settings

    settings = Settings()

    assert (
        settings.chili_brain_queue_market_hours_exploration_max
        == _MARKET_HOURS_EXPLORATION_MAX
    )
