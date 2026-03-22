"""Backtest ticker pool respects crypto vs stock asset universes."""
from __future__ import annotations

from app.services.trading.backtest_engine import (
    SECTOR_TICKERS,
    _select_tickers,
    effective_backtest_asset_universe,
)


def test_effective_universe_pattern_crypto_wins_over_context():
    ctx = {"crypto_only": False, "stock_only": True}
    assert effective_backtest_asset_universe("crypto", ctx) == "crypto"


def test_effective_universe_uses_context_when_pattern_all():
    ctx = {"crypto_only": True, "stock_only": False}
    assert effective_backtest_asset_universe("all", ctx) == "crypto"
    ctx2 = {"crypto_only": False, "stock_only": True}
    assert effective_backtest_asset_universe(None, ctx2) == "stocks"


def test_select_tickers_crypto_asset_class_no_stocks():
    ctx = {
        "mentioned_tickers": [],
        "wants_crypto": True,
        "crypto_only": False,
        "stock_only": False,
    }
    pool = _select_tickers(
        ctx,
        db=None,
        insight_id=None,
        target_count=30,
        ticker_scope="universal",
        scope_tickers=None,
        asset_class="crypto",
    )
    assert len(pool) >= 10
    stock_set = set()
    for _sect, tickers in SECTOR_TICKERS.items():
        if _sect != "crypto":
            stock_set.update(tickers)
    for t in pool:
        assert t.endswith("-USD"), f"unexpected stock in crypto pool: {t}"
        assert t not in stock_set


def test_select_tickers_stocks_asset_class_no_crypto():
    ctx = {
        "mentioned_tickers": [],
        "wants_crypto": False,
        "crypto_only": False,
        "stock_only": False,
    }
    pool = _select_tickers(
        ctx,
        db=None,
        insight_id=None,
        target_count=30,
        ticker_scope="universal",
        scope_tickers=None,
        asset_class="stocks",
    )
    assert len(pool) >= 10
    for t in pool:
        assert not t.endswith("-USD"), f"unexpected crypto in stock pool: {t}"


def test_select_tickers_universal_includes_both_asset_types():
    ctx = {
        "mentioned_tickers": [],
        "wants_crypto": False,
        "crypto_only": False,
        "stock_only": False,
    }
    pool = _select_tickers(
        ctx,
        db=None,
        insight_id=None,
        target_count=40,
        asset_class="all",
    )
    has_crypto = any(t.endswith("-USD") for t in pool)
    has_stock = any(not t.endswith("-USD") for t in pool)
    assert has_crypto and has_stock
