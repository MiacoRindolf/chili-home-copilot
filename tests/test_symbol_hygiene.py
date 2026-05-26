from __future__ import annotations

from app.services import ticker_universe
from app.services.symbol_hygiene import clean_equity_universe, normalize_equity_symbol
from app.services.trading import market_data
from app.services.trading.prescreen_normalize import normalize_prescreen_ticker

LEGACY_BLOCK_TICKER = "SQ"
ACTIVE_BLOCK_TICKER = "XYZ"
INACTIVE_DISCOVER_TICKER = "DFS"
INACTIVE_HESS_TICKER = "HES"
ACTIVE_CONTROL_TICKER = "AAPL"
COMPACT_PREFERRED_TICKERS = ["BACPM", "COFPJ", "ARESPB"]
COMMON_INTERNAL_P_TICKER = "APPS"


def test_equity_symbol_hygiene_aliases_renames_and_drops_inactive() -> None:
    assert normalize_equity_symbol(LEGACY_BLOCK_TICKER) == ACTIVE_BLOCK_TICKER
    assert normalize_equity_symbol(INACTIVE_DISCOVER_TICKER) == ""
    assert normalize_equity_symbol(INACTIVE_HESS_TICKER) == ""


def test_equity_symbol_hygiene_drops_compact_preferred_symbols() -> None:
    for ticker in COMPACT_PREFERRED_TICKERS:
        assert normalize_equity_symbol(ticker) == ""
    assert normalize_equity_symbol(COMMON_INTERNAL_P_TICKER) == COMMON_INTERNAL_P_TICKER


def test_prescreen_normalization_uses_equity_symbol_hygiene() -> None:
    assert normalize_prescreen_ticker(LEGACY_BLOCK_TICKER) == ACTIVE_BLOCK_TICKER
    assert normalize_prescreen_ticker(INACTIVE_DISCOVER_TICKER) == ""
    assert normalize_prescreen_ticker(INACTIVE_HESS_TICKER) == ""


def test_clean_equity_universe_preserves_order_after_hygiene() -> None:
    assert clean_equity_universe([
        LEGACY_BLOCK_TICKER,
        ACTIVE_BLOCK_TICKER,
        INACTIVE_DISCOVER_TICKER,
        COMPACT_PREFERRED_TICKERS[0],
        ACTIVE_CONTROL_TICKER,
    ]) == [ACTIVE_BLOCK_TICKER, ACTIVE_CONTROL_TICKER]


def test_market_data_default_stock_universe_is_hygienic() -> None:
    defaults = set(market_data.DEFAULT_SCAN_TICKERS)
    assert ACTIVE_BLOCK_TICKER in defaults
    assert LEGACY_BLOCK_TICKER not in defaults
    assert INACTIVE_DISCOVER_TICKER not in defaults
    assert INACTIVE_HESS_TICKER not in defaults


def test_ticker_universe_filters_stale_cache_entries(monkeypatch) -> None:
    ticker_universe._memory_cache.clear()
    monkeypatch.setattr(
        ticker_universe,
        "_load_cache",
        lambda _path: [
            {"ticker": LEGACY_BLOCK_TICKER},
            {"ticker": INACTIVE_DISCOVER_TICKER},
            {"ticker": INACTIVE_HESS_TICKER},
            {"ticker": COMPACT_PREFERRED_TICKERS[1]},
            {"ticker": ACTIVE_CONTROL_TICKER},
        ],
    )

    assert ticker_universe.get_all_us_stock_tickers() == [
        ACTIVE_BLOCK_TICKER,
        ACTIVE_CONTROL_TICKER,
    ]
