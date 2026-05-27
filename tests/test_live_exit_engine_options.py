from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from app.models.trading import PaperTrade


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._rows)


def _option_paper_trade() -> PaperTrade:
    return PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.65,
        target_price=2.50,
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


def test_run_exit_engine_skips_option_paper_trades_without_underlying_quote() -> None:
    from app.services.trading.live_exit_engine import run_exit_engine

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option paper exits must not fetch underlying spot"),
    ), patch(
        "app.services.trading.live_exit_engine.compute_live_exit_levels",
        side_effect=AssertionError("option paper exits must not use stock exit engine"),
    ):
        out = run_exit_engine(_FakeDb([_option_paper_trade()]))

    assert out["evaluated"] == 0
    assert out["actions"] == []
    assert out["partial_actions"] == []
    assert out["all"] == []
    assert out["skipped_options"] == 1
