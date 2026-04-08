"""Coinbase spot venue adapter — normalization and plumbing (mocked SDK; no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.venue import (
    CoinbaseSpotAdapter,
    CoinbaseWebSocketSeam,
    FreshnessMeta,
    NormalizedProduct,
    NormalizedTicker,
    VenueAdapterError,
    execution_readiness_dict_from_normalized,
    is_fresh_enough,
    require_fresh_or_raise,
    reset_duplicate_client_order_guard_for_tests,
)
from app.services.trading.venue.coinbase_spot import _normalize_bbo, _normalize_product


def test_normalize_product_maps_increments_and_flags():
    raw = {
        "product_id": "BTC-USD",
        "base_currency_id": "BTC",
        "quote_currency_id": "USD",
        "status": "online",
        "trading_disabled": False,
        "cancel_only": False,
        "limit_only": False,
        "post_only": False,
        "auction_mode": False,
        "base_min_size": "0.00001",
        "base_increment": "0.00000001",
        "quote_increment": "0.0000001",
        "price_increment": "0.01",
    }
    p = _normalize_product(raw)
    assert p.product_id == "BTC-USD"
    assert p.base_min_size == pytest.approx(1e-5)
    assert p.price_increment == pytest.approx(0.01)
    assert p.tradable_for_spot_momentum() is True


def test_normalize_bbo_spread_bps():
    fr = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=15.0)
    resp = {
        "pricebooks": [
            {
                "product_id": "ETH-USD",
                "bids": [{"price": "100", "size": "1"}],
                "asks": [{"price": "101", "size": "1"}],
            }
        ]
    }
    t = _normalize_bbo(resp, "ETH-USD", fr)
    assert t is not None
    assert t.bid == 100.0
    assert t.ask == 101.0
    assert t.mid == 100.5
    assert t.spread_abs == 1.0
    assert t.spread_bps == pytest.approx((1.0 / 100.5) * 10000.0)


def test_freshness_helpers():
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    m = FreshnessMeta(retrieved_at_utc=old, max_age_seconds=30.0)
    assert is_fresh_enough(m) is False
    with pytest.raises(VenueAdapterError):
        require_fresh_or_raise(m, strict=True)
    require_fresh_or_raise(m, strict=False)


def test_duplicate_client_order_guard():
    reset_duplicate_client_order_guard_for_tests()
    mock = MagicMock()
    mock.market_order_buy.return_value = {
        "success": True,
        "success_response": {"order_id": "oid1"},
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    # is_enabled may be False without credentials — force client path via monkeypatch
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    r1 = ad.place_market_order(product_id="BTC-USD", side="buy", base_size="0.001", client_order_id="same-id")
    assert r1["ok"] is True
    r2 = ad.place_market_order(product_id="BTC-USD", side="buy", base_size="0.001", client_order_id="same-id")
    assert r2["ok"] is False
    assert "duplicate" in (r2.get("error") or "").lower()
    reset_duplicate_client_order_guard_for_tests()


def test_place_market_order_request_shaping():
    reset_duplicate_client_order_guard_for_tests()
    mock = MagicMock()
    mock.market_order_sell.return_value = {
        "success": True,
        "success_response": {"order_id": "abc"},
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    out = ad.place_market_order(
        product_id="ZK",
        side="sell",
        base_size="1.5",
        client_order_id="cid-xyz",
    )
    assert out["ok"] is True
    mock.market_order_sell.assert_called_once()
    kw = mock.market_order_sell.call_args.kwargs
    assert kw["product_id"] == "ZK-USD"
    assert kw["base_size"] == "1.5"
    assert kw["client_order_id"] == "cid-xyz"


def test_cancel_order_calls_sdk():
    mock = MagicMock()
    mock.cancel_orders.return_value = {"results": []}
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]
    r = ad.cancel_order("ord-1")
    assert r["ok"] is True
    mock.cancel_orders.assert_called_once_with(order_ids=["ord-1"])


def test_readiness_bridge_and_features():
    p = NormalizedProduct(
        product_id="BTC-USD",
        base_currency="BTC",
        quote_currency="USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_min_size=0.0001,
        base_increment=1e-8,
        price_increment=0.01,
    )
    t = NormalizedTicker(
        product_id="BTC-USD",
        bid=100.0,
        ask=100.2,
        mid=100.1,
        spread_abs=0.2,
        spread_bps=19.98,
    )
    d = execution_readiness_dict_from_normalized(p, t)
    assert d["product_tradable"] is True
    assert d["spread_bps"] == pytest.approx(19.98, rel=1e-2)
    feats = ExecutionReadinessFeatures.from_coinbase_normalized(product=p, ticker=t)
    assert feats.spread_bps == pytest.approx(19.98, rel=1e-2)
    assert feats.slippage_estimate_bps == pytest.approx(19.98 * 0.5, rel=1e-2)
    assert feats.product_tradable is True


def test_websocket_seam_stub():
    seam = CoinbaseWebSocketSeam()
    d = seam.describe()
    assert d["status"] == "stub"


def test_list_open_orders_normalized():
    mock = MagicMock()
    mock.list_orders.return_value = {
        "orders": [
            {
                "order_id": "o1",
                "product_id": "BTC-USD",
                "side": "BUY",
                "status": "OPEN",
                "order_type": "LIMIT",
                "filled_size": "0",
                "average_filled_price": None,
            }
        ]
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]
    orders, _fr = ad.list_open_orders(limit=5)
    assert len(orders) == 1
    assert orders[0].order_id == "o1"
    assert orders[0].side == "buy"


def test_get_fills_normalized():
    mock = MagicMock()
    mock.get_fills.return_value = {
        "fills": [
            {
                "entry_id": "f1",
                "order_id": "o1",
                "product_id": "BTC-USD",
                "side": "BUY",
                "size": "0.01",
                "price": "50000",
                "commission": "0.1",
            }
        ]
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]
    fills, _fr = ad.get_fills(limit=10)
    assert len(fills) == 1
    assert fills[0].price == 50000.0
    assert fills[0].size == pytest.approx(0.01)


def test_preview_market_order_returns_raw():
    mock = MagicMock()
    mock.preview_market_order.return_value = {"preview": True, "warnings": []}
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]
    r = ad.preview_market_order(product_id="BTC-USD", side="buy", base_size="0.001")
    assert r["ok"] is True
    assert r["raw"]["preview"] is True
