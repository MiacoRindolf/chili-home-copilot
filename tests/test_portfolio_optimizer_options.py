from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models.trading import PaperTrade


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *result_sets):
        self._result_sets = list(result_sets)
        self._idx = 0

    def query(self, *_args, **_kwargs):
        rows = self._result_sets[min(self._idx, len(self._result_sets) - 1)]
        self._idx += 1
        return _FakeQuery(rows)


def _option_paper_trade() -> PaperTrade:
    return PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            "asset_type": "options",
            "options_path": True,
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
                "limit_price": 1.25,
            },
        },
    )


def test_portfolio_drawdown_uses_option_premium_mark_and_multiplier() -> None:
    from app.services.trading.portfolio_optimizer import check_portfolio_drawdown

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option drawdown must not fetch underlying spot"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ):
        out = check_portfolio_drawdown(
            _FakeDb([_option_paper_trade()], []),
            user_id=None,
            capital=10_000.0,
            max_dd_pct=15.0,
        )

    assert out["unrealized_pnl"] == pytest.approx(40.0)
    assert out["total_pnl"] == pytest.approx(40.0)
    assert out["dd_pct"] == pytest.approx(0.4)
