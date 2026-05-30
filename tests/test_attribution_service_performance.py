import inspect

from app.services.trading import attribution_service
from app.services.trading.attribution_service import (
    _top_high_slippage_trades,
    _top_pattern_deltas,
)


def test_top_high_slippage_trades_uses_bounded_ranking_with_stable_ties() -> None:
    rows = [
        {"ticker": "AAA", "total_slippage_bps": 10.0},
        {"ticker": "BBB", "total_slippage_bps": 90.0},
        {"ticker": "CCC", "total_slippage_bps": 90.0},
        {"ticker": "DDD", "total_slippage_bps": 40.0},
    ]

    assert _top_high_slippage_trades(rows, 3) == [
        {"ticker": "BBB", "total_slippage_bps": 90.0},
        {"ticker": "CCC", "total_slippage_bps": 90.0},
        {"ticker": "DDD", "total_slippage_bps": 40.0},
    ]


def test_top_pattern_deltas_handles_outperformers_and_underperformers() -> None:
    rows = [
        {"pattern": "flat", "delta_pct": 0.0},
        {"pattern": "winner_a", "delta_pct": 14.0},
        {"pattern": "winner_b", "delta_pct": 14.0},
        {"pattern": "laggard", "delta_pct": -20.0},
    ]

    assert _top_pattern_deltas(rows, 2, reverse=True) == [
        {"pattern": "winner_a", "delta_pct": 14.0},
        {"pattern": "winner_b", "delta_pct": 14.0},
    ]
    assert _top_pattern_deltas(rows, 2, reverse=False) == [
        {"pattern": "laggard", "delta_pct": -20.0},
        {"pattern": "flat", "delta_pct": 0.0},
    ]


def test_attribution_top_helpers_empty_for_non_positive_limits() -> None:
    assert _top_high_slippage_trades([{"total_slippage_bps": 1.0}], 0) == []
    assert _top_pattern_deltas([{"delta_pct": 1.0}], 0, reverse=True) == []


def test_closed_pattern_live_stats_reads_management_envelopes_contract() -> None:
    source = inspect.getsource(attribution_service._closed_pattern_live_stats)

    assert "MANAGEMENT_ENVELOPES_RELATION" in source
    assert "FROM trading_trades" not in source
