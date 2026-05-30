from __future__ import annotations

from app.services.trading.market_data import _percentile_rank_percent


def test_percentile_rank_percent_does_not_require_sorted_input() -> None:
    assert _percentile_rank_percent([30.0, 10.0, 20.0, 40.0], 25.0) == 50.0
    assert _percentile_rank_percent([], 25.0) is None
