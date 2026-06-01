from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.ticker_scope_autotune import (
    _decide_action,
    _query_per_ticker_stats,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = {}

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _Rows(self.rows)


def test_ticker_autotune_query_uses_contract_aware_returns() -> None:
    sess = _FakeSession(
        [
            SimpleNamespace(
                scan_pattern_id=42,
                ticker="SPY",
                n=2,
                wins=1,
                total_pnl=40.0,
                avg_pnl=20.0,
                total_return_pct=16.0,
                avg_return_pct=8.0,
            )
        ]
    )

    rows = _query_per_ticker_stats(
        sess,
        pattern_ids=[42],
        lookback_days=30,
        min_trades_per_ticker=2,
    )

    assert rows == [
        {
            "scan_pattern_id": 42,
            "ticker": "SPY",
            "n": 2,
            "wins": 1,
            "total_pnl": 40.0,
            "avg_pnl": 20.0,
            "total_return_pct": 16.0,
            "avg_return_pct": 8.0,
        }
    ]
    sql = " ".join(sess.sql.split())
    assert "WITH realized_samples AS" in sql
    assert "FROM trading_trades t" in sql
    assert "total_return_pct" in sql
    assert "avg_return_pct" in sql
    assert "count(realized_return_frac) AS n" in sql
    assert "CASE WHEN realized_return_frac > 0 THEN 1 ELSE 0 END" in sql
    assert "WHERE realized_return_frac IS NOT NULL" in sql
    assert "HAVING count(realized_return_frac) >= :min_per_ticker" in sql
    assert "t.filled_quantity" in sql
    assert "t.partial_taken_qty" in sql
    assert "option_contract_multiplier" in sql
    assert "asset_kind" in sql
    assert "t.entry_price > 0" in sql
    assert "t.quantity > 0" in sql
    assert "count(*)" not in sql
    assert "WHEN pnl > 0" not in sql
    assert "__PATTERN_FILTER__" not in sql
    assert "scan_pattern_id = ANY(:ids)" in sql
    assert sess.params == {
        "lookback_days": 30,
        "min_per_ticker": 2,
        "ids": [42],
    }


def test_ticker_autotune_decision_prefers_expectancy_over_dollar_pnl() -> None:
    action = _decide_action(
        42,
        "option scope rescue",
        [
            {
                "ticker": "HIGH_NOTIONAL_LOSER",
                "total_pnl": 100.0,
                "avg_return_pct": -1.0,
                "total_return_pct": -2.0,
            },
            {
                "ticker": "LOW_NOTIONAL_EDGE",
                "total_pnl": -10.0,
                "avg_return_pct": 3.0,
                "total_return_pct": 6.0,
            },
        ],
    )

    assert action.decision == "narrow_to_explicit"
    assert action.edge_tickers == ("LOW_NOTIONAL_EDGE",)
    assert action.bleed_tickers == ("HIGH_NOTIONAL_LOSER",)
    assert action.net_pnl == 90.0


def test_ticker_autotune_decision_keeps_legacy_total_pnl_fallback() -> None:
    action = _decide_action(
        7,
        "legacy rows",
        [
            {"ticker": "EDGE", "total_pnl": 5.0},
            {"ticker": "BLEED", "total_pnl": -3.0},
        ],
    )

    assert action.decision == "narrow_to_explicit"
    assert action.edge_tickers == ("EDGE",)
    assert action.bleed_tickers == ("BLEED",)
