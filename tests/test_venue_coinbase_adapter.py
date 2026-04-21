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


def test_duplicate_client_order_guard(db):
    # Phase B tech-debt: this test latently depended on
    # ``venue_order_idempotency`` being empty for "same-id". The table is
    # shared across runs and was not TRUNCATEd for pure-mock tests, so
    # an accumulated row silently flipped r1 to ok=False. Purge the
    # specific CID before and after to make the test self-contained.
    from sqlalchemy import text as _sql_text

    cid = "same-id"
    db.execute(
        _sql_text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": cid},
    )
    db.commit()
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

    r1 = ad.place_market_order(product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid)
    assert r1["ok"] is True
    r2 = ad.place_market_order(product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid)
    assert r2["ok"] is False
    assert "duplicate" in (r2.get("error") or "").lower()
    db.execute(
        _sql_text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": cid},
    )
    db.commit()
    reset_duplicate_client_order_guard_for_tests()


def test_place_market_order_request_shaping(db):
    # Phase B: purge any leftover DB idempotency row for "cid-xyz" so the
    # test is self-contained against accumulated state. See
    # test_duplicate_client_order_guard above for context.
    from sqlalchemy import text as _sql_text

    cid = "cid-xyz"
    db.execute(
        _sql_text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": cid},
    )
    db.commit()
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
        client_order_id=cid,
    )
    assert out["ok"] is True
    mock.market_order_sell.assert_called_once()
    kw = mock.market_order_sell.call_args.kwargs
    assert kw["product_id"] == "ZK-USD"
    assert kw["base_size"] == "1.5"
    assert kw["client_order_id"] == cid
    db.execute(
        _sql_text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": cid},
    )
    db.commit()


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
    # Status was renamed "stub" → "disabled" when the seam was scoped down
    # to the no-op implementation that ships today. Accept either so this
    # test stays green through a future rename.
    assert d["status"] in {"stub", "disabled"}


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


# ── Fault-injection (Phase B) ───────────────────────────────────────────
#
# The four scenarios below pin the CB adapter's behavior under upstream
# failure modes that previously only surfaced in prod. Unlike the RH
# adapter, CB wraps the broker call in try/except — so a raised
# TimeoutError is translated into a structured ok=False response.


def _purge_idempotency_cid(db, cid: str) -> None:
    """Delete any leftover ``venue_order_idempotency`` rows for this CID.

    See the matching helper in the RH adapter test for rationale. The
    idempotency table is not truncated by the default pytest fixtures
    for pure-mock tests — rows accumulate across runs.
    """
    from sqlalchemy import text as _sql_text
    db.execute(
        _sql_text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": cid},
    )
    db.commit()


def test_place_market_order_timeout_caught_and_returned(db):
    """Unlike RH, the CB adapter wraps the SDK call in try/except; a
    TimeoutError from the client comes back as ok=False with the error
    stringified. This pins that contract so upstream retry logic can
    dispatch on the structured response rather than on an exception.
    """
    cid = "cid-cb-timeout"
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()
    mock = MagicMock()
    mock.market_order_buy.side_effect = TimeoutError("CB SDK timeout")
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    out = ad.place_market_order(
        product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid,
    )
    assert out["ok"] is False
    assert "timeout" in out["error"].lower()
    assert out["client_order_id"] == cid
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()


def test_place_market_order_broker_returns_429(db):
    """Coinbase returns TOO_MANY_REQUESTS inside success=False + error_response.
    Adapter surfaces the message via the ok/error contract; no idempotency
    row is stored (caller can retry after backoff).
    """
    cid = "cid-cb-429"
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()
    mock = MagicMock()
    mock.market_order_buy.return_value = {
        "success": False,
        "error_response": {
            "message": "rate limit exceeded (please wait and retry)",
            "error": "TOO_MANY_REQUESTS",
        },
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    out = ad.place_market_order(
        product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid,
    )
    assert out["ok"] is False
    assert "rate limit" in out["error"].lower()
    assert out["client_order_id"] == cid
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()


def test_get_fills_partial_fill_size_and_price():
    """A partially-filled CB order is reported as a NormalizedFill with the
    true (partial) size and fill price. Sizing logic downstream must see
    the realized exposure, not the order's total requested size.
    """
    mock = MagicMock()
    mock.get_fills.return_value = {
        "fills": [
            {
                "entry_id": "fill-partial-1",
                "order_id": "ord-partial-1",
                "product_id": "BTC-USD",
                "side": "BUY",
                "size": "0.0035",  # 0.0035 of the 0.01 originally requested
                "price": "50100.25",
                "commission": "0.01",
            }
        ]
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    fills, _fresh = ad.get_fills(limit=10)
    assert len(fills) == 1
    assert fills[0].order_id == "ord-partial-1"
    assert fills[0].size == pytest.approx(0.0035)
    assert fills[0].price == pytest.approx(50100.25)
    assert fills[0].side == "buy"


def test_place_market_order_duplicate_cid_intra_process(db):
    """CB in-process dup-CID guard: a second place-order call with the
    same client_order_id short-circuits without hitting the SDK. Parity
    check with the RH fast-path test; also complements
    test_duplicate_client_order_guard which covers back-to-back calls
    but through a slightly different mock shape.
    """
    cid = "intra-cb-cid-1"
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()
    mock = MagicMock()
    mock.market_order_buy.return_value = {
        "success": True,
        "success_response": {"order_id": "ord-intra-cb-1"},
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    first = ad.place_market_order(
        product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid,
    )
    assert first["ok"] is True
    assert mock.market_order_buy.call_count == 1

    second = ad.place_market_order(
        product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid,
    )
    assert second["ok"] is False
    assert "duplicate" in (second.get("error") or "").lower()
    assert mock.market_order_buy.call_count == 1, "CB SDK hit twice on dup CID"
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()
