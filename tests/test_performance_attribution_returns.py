from datetime import datetime
from types import SimpleNamespace

import pandas as pd
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


def test_attribute_trade_option_gross_return_uses_contract_multiplier(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=2,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["gross_return_pct"] == pytest.approx(16.0)
    assert result["alpha_pct"] == pytest.approx(16.0)
    assert result["estimated_cost_pct"] is None
    assert result["net_alpha_pct"] is None


def test_attribute_trade_option_uses_tca_cost_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=4,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=12,
        tca_exit_slippage_bps=18,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["estimated_cost_pct"] == pytest.approx(0.30)
    assert result["net_alpha_pct"] == pytest.approx(15.70)


def test_attribute_trade_option_rejects_ambiguous_underlying_price_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=3,
        ticker="SPY",
        direction="long",
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["error"] == "missing_return_basis"
    assert "gross_return_pct" not in result


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.order_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def _closed_trade(trade_id: int, entry_date: datetime, exit_date: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=trade_id,
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        exit_price=110.0,
        entry_date=entry_date,
        exit_date=exit_date,
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )


def test_fetch_benchmark_returns_fetches_spy_once_for_unique_windows(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []
    frame = pd.DataFrame(
        {"Close": [100.0, 110.0, 121.0, 133.1]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
    )

    def fake_fetch(ticker: str, *, period: str, interval: str):
        calls.append((ticker, period, interval))
        return frame

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        fake_fetch,
    )

    entry_a = datetime(2026, 1, 1)
    exit_a = datetime(2026, 1, 3)
    entry_b = datetime(2026, 1, 2)
    exit_b = datetime(2026, 1, 4)

    result = performance_attribution._fetch_benchmark_returns(
        {(entry_a, exit_a), (entry_b, exit_b)}
    )

    assert calls == [("SPY", "38d", "1d")]
    assert result[(entry_a, exit_a)] == pytest.approx(21.0)
    assert result[(entry_b, exit_b)] == pytest.approx(21.0)


def test_attribute_pattern_trades_uses_batched_benchmark_returns(monkeypatch) -> None:
    entry = datetime(2026, 1, 1)
    exit_ = datetime(2026, 1, 3)
    db = _FakeSession([_closed_trade(1, entry, exit_), _closed_trade(2, entry, exit_)])
    calls: list[set[tuple[datetime | None, datetime | None]]] = []

    def fake_benchmarks(windows):
        calls.append(set(windows))
        return {(entry, exit_): 1.5}

    monkeypatch.setattr(performance_attribution, "_fetch_benchmark_returns", fake_benchmarks)

    result = performance_attribution.attribute_pattern_trades(
        db,  # type: ignore[arg-type]
        pattern_id=42,
    )

    assert calls == [{(entry, exit_)}]
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1
    assert db.last_query.order_by_calls == 1
    assert result["trade_count"] == 2
    assert result["trades_with_attribution"] == 2
    assert result["mean_alpha_pct"] == pytest.approx(8.5)
