from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.thesis import _best_backtests_by_ticker, build_smart_pick_context_strings


class _FakeQuery:
    def __init__(self, rows: list[object]) -> None:
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
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None
        self.query_args: tuple[object, ...] | None = None

    def query(self, *args: object) -> _FakeQuery:
        self.query_calls += 1
        self.query_args = args
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def _backtest(ticker: str, strategy: str, return_pct: float) -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        strategy_name=strategy,
        return_pct=return_pct,
        win_rate=0.62,
    )


def _pick(ticker: str) -> dict[str, object]:
    return {
        "ticker": ticker,
        "score": 8,
        "signal": "buy",
        "price": 100.0,
        "entry_price": 101.0,
        "stop_loss": 95.0,
        "take_profit": 115.0,
        "risk_level": "medium",
        "signals": ["breakout"],
        "indicators": {"rsi": 55, "macd": 1.2, "adx": 28},
    }


def test_best_backtests_by_ticker_batches_lookup() -> None:
    best_aapl = _backtest("AAPL", "Breakout", 8.5)
    older_aapl = _backtest("AAPL", "Mean Reversion", 2.0)
    best_msft = _backtest("MSFT", "Trend", 5.5)
    db = _FakeSession([best_aapl, older_aapl, best_msft])

    result = _best_backtests_by_ticker(db, {"AAPL", "MSFT"})  # type: ignore[arg-type]

    assert result == {"AAPL": best_aapl, "MSFT": best_msft}
    assert db.query_calls == 1
    assert db.query_args is not None
    assert tuple(getattr(arg, "key", None) for arg in db.query_args) == (
        "ticker",
        "strategy_name",
        "return_pct",
        "win_rate",
    )
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1
    assert db.last_query.order_by_calls == 1


def test_best_backtests_by_ticker_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _best_backtests_by_ticker(db, set()) == {}  # type: ignore[arg-type]
    assert db.query_calls == 0


def test_smart_pick_context_batches_duplicate_ticker_backtests() -> None:
    db = _FakeSession([_backtest("AAPL", "Breakout", 8.5), _backtest("MSFT", "Trend", 5.5)])
    ctx = {
        "top_picks": [_pick("AAPL"), _pick("AAPL"), _pick("MSFT")],
        "total_scanned": 250,
        "stats": {},
    }

    context = build_smart_pick_context_strings(db, ctx)  # type: ignore[arg-type]

    assert db.query_calls == 1
    assert context.count("Best backtest:") == 3
    assert "Breakout" in context
    assert "Trend" in context


def test_smart_pick_context_handles_compact_backtest_tuple_rows() -> None:
    db = _FakeSession([("AAPL", "Breakout", 8.5, 0.62)])
    ctx = {
        "top_picks": [_pick("AAPL")],
        "total_scanned": 250,
        "stats": {},
    }

    context = build_smart_pick_context_strings(db, ctx)  # type: ignore[arg-type]

    assert db.query_calls == 1
    assert "Best backtest: Breakout" in context
    assert "+8.5% return" in context
