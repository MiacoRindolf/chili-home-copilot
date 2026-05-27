from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.asset_class import (
    PATTERN_ASSET_CLASS_ALL,
    PATTERN_ASSET_CLASS_CRYPTO,
    PATTERN_ASSET_CLASS_STOCKS,
    normalize_pattern_asset_class,
)
from app.services.trading.pattern_engine import get_active_patterns


def test_pattern_asset_class_normalizes_stock_aliases() -> None:
    assert normalize_pattern_asset_class("stock") == PATTERN_ASSET_CLASS_STOCKS
    assert normalize_pattern_asset_class("stocks") == PATTERN_ASSET_CLASS_STOCKS
    assert normalize_pattern_asset_class("equity") == PATTERN_ASSET_CLASS_STOCKS
    assert normalize_pattern_asset_class("equities") == PATTERN_ASSET_CLASS_STOCKS
    assert normalize_pattern_asset_class("crypto") == PATTERN_ASSET_CLASS_CRYPTO
    assert normalize_pattern_asset_class("") == PATTERN_ASSET_CLASS_ALL


class _PatternQuery:
    def __init__(self, patterns):
        self.patterns = patterns

    def filter_by(self, **kwargs):
        active = kwargs.get("active")
        return _PatternQuery([p for p in self.patterns if p.active is active])

    def all(self):
        return list(self.patterns)


class _PatternDb:
    def __init__(self, patterns):
        self.patterns = patterns

    def query(self, _model):
        return _PatternQuery(self.patterns)


def test_get_active_patterns_treats_stock_aliases_as_same_asset() -> None:
    all_pat = SimpleNamespace(id=1, asset_class="all", active=True)
    stock_pat = SimpleNamespace(id=2, asset_class="stock", active=True)
    stocks_pat = SimpleNamespace(id=3, asset_class="stocks", active=True)
    equity_pat = SimpleNamespace(id=4, asset_class="equity", active=True)
    crypto_pat = SimpleNamespace(id=5, asset_class="crypto", active=True)
    inactive_pat = SimpleNamespace(id=6, asset_class="stock", active=False)
    db = _PatternDb([
        all_pat,
        stock_pat,
        stocks_pat,
        equity_pat,
        crypto_pat,
        inactive_pat,
    ])

    stock_ids = {p.id for p in get_active_patterns(db, asset_class="stocks")}
    stock_alias_ids = {p.id for p in get_active_patterns(db, asset_class="stock")}
    all_ids = {p.id for p in get_active_patterns(db, asset_class="all")}

    assert stock_ids == {all_pat.id, stock_pat.id, stocks_pat.id, equity_pat.id}
    assert stock_alias_ids == stock_ids
    assert crypto_pat.id not in stock_ids
    assert all_ids == {all_pat.id}
