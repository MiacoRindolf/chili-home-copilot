from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.trading import performance_attribution


def test_attribute_trade_short_gross_return_is_direction_aware(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=1,
        ticker="SPY",
        direction="short",
        entry_price=100.0,
        exit_price=80.0,
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["gross_return_pct"] == pytest.approx(20.0)
    assert result["alpha_pct"] == pytest.approx(20.0)
