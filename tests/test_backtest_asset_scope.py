"""Backtest ticker pool respects crypto vs stock asset universes."""
from __future__ import annotations

from app.services.trading.backtest_engine import (
    SECTOR_TICKERS,
    _extract_context,
    _select_tickers,
    effective_backtest_asset_universe,
)

_MARKET_STRUCTURE_TOKENS = {"BOS", "FVG", "HTF", "LTF", "MSS"}
_REAL_STOCK_MENTION = "AAPL"
_REAL_CRYPTO_MENTION = "BTC-USD"
_TEST_STOCK_TARGET_COUNT = 30


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


def test_extract_context_ignores_market_structure_tokens_as_tickers():
    ctx = _extract_context(
        "Stock pullback uses FVG with BOS confirmation across HTF/LTF plus MSS; "
        f"validate on {_REAL_STOCK_MENTION} and {_REAL_CRYPTO_MENTION}."
    )

    assert _MARKET_STRUCTURE_TOKENS.isdisjoint(ctx["mentioned_tickers"])
    assert _REAL_STOCK_MENTION in ctx["mentioned_tickers"]
    assert _REAL_CRYPTO_MENTION in ctx["mentioned_tickers"]


def test_select_tickers_defensively_ignores_context_structure_tokens():
    ctx = {
        "mentioned_tickers": [
            *_MARKET_STRUCTURE_TOKENS,
            _REAL_STOCK_MENTION,
        ],
        "wants_crypto": False,
        "crypto_only": False,
        "stock_only": False,
    }

    pool = _select_tickers(
        ctx,
        db=None,
        insight_id=None,
        target_count=_TEST_STOCK_TARGET_COUNT,
        asset_class="stocks",
    )

    assert _MARKET_STRUCTURE_TOKENS.isdisjoint(pool)
    assert _REAL_STOCK_MENTION in pool
