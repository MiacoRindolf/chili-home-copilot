from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


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

    def query(self, model):
        return _FakeQuery(self._rows)


def test_legacy_price_monitor_skips_option_trades() -> None:
    from app.services.trading.alerts import _check_open_positions

    option_trade = SimpleNamespace(
        id=9101,
        user_id=None,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=1,
        status="open",
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
    )

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("legacy monitor must not fetch underlying spot for options"),
    ):
        out = _check_open_positions(_FakeDb([option_trade]), user_id=None)

    assert out == {"targets_hit": 0, "stops_hit": 0}
