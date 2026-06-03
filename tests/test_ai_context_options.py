from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_open_trade_context_option_uses_contract_premium_domain() -> None:
    from app.services.trading.ai_context import _format_open_trade_context_line

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        quantity=2,
        entry_price=1.25,
        entry_date=datetime(2026, 5, 26),
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
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ):
        line = _format_open_trade_context_line(trade)

    assert "[OPTIONS]" in line
    assert "2 contract(s)" in line
    assert "@ $1.2500 premium" in line
    assert "current premium $1.4500" in line
    assert "P&L +$40.00 (+16.0%)" in line
    assert "contract_multiplier=100" in line
    assert "quote_source=robinhood_options" in line
    assert "SPY 2026-06-19 strike=729.0 call" in line
    assert "shares" not in line.lower()


def test_open_trade_context_stock_preserves_existing_shape() -> None:
    from app.services.trading.ai_context import _format_open_trade_context_line

    trade = SimpleNamespace(
        ticker="AAPL",
        direction="long",
        quantity=3,
        entry_price=195.0,
        entry_date=datetime(2026, 5, 26),
        indicator_snapshot={},
    )

    line = _format_open_trade_context_line(trade)

    assert line == "  - LONG 3x @ $195.0 (entered 2026-05-26)"


def test_brain_prediction_pattern_loader_detaches_before_rollback(monkeypatch) -> None:
    from app import db as app_db
    from app.services.trading import pattern_engine
    from app.services.trading.ai_context import _load_brain_prediction_patterns

    pattern = SimpleNamespace(name="Detached pattern")

    class _PatternSession:
        def __init__(self) -> None:
            self.events: list[str] = []

        def expunge(self, obj) -> None:
            self.events.append(f"expunge:{obj.name}")

        def rollback(self) -> None:
            self.events.append("rollback")

        def close(self) -> None:
            self.events.append("close")

    session = _PatternSession()

    def _get_active_patterns(db):
        assert db is session
        session.events.append("query")
        return [pattern]

    monkeypatch.setattr(app_db, "SessionLocal", lambda: session)
    monkeypatch.setattr(pattern_engine, "get_active_patterns", _get_active_patterns)

    assert _load_brain_prediction_patterns() == [pattern]
    assert session.events == ["query", "expunge:Detached pattern", "rollback", "close"]


@pytest.mark.parametrize("bad_price", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_open_trade_context_option_rejects_bad_premium_quote(bad_price) -> None:
    from app.services.trading.ai_context import _format_open_trade_context_line

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        quantity=2,
        entry_price=1.25,
        entry_date=datetime(2026, 5, 26),
        indicator_snapshot={"asset_kind": "option"},
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": bad_price, "source": "robinhood_options"},
    ):
        line = _format_open_trade_context_line(trade)

    assert "current premium unavailable" in line
    assert "P&L unavailable" in line
    assert "quote_source=robinhood_options" in line


@pytest.mark.parametrize("bad_entry", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_open_trade_context_option_rejects_bad_entry_price(bad_entry) -> None:
    from app.services.trading.ai_context import _format_open_trade_context_line

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        quantity=2,
        entry_price=bad_entry,
        entry_date=datetime(2026, 5, 26),
        indicator_snapshot={"asset_kind": "option"},
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ):
        line = _format_open_trade_context_line(trade)

    assert "@ $? premium" in line
    assert "current premium $1.4500" in line
    assert "P&L unavailable" in line
