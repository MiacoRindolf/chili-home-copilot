"""f-coinbase-tick-size-precision-fix (2026-05-10).

Coinbase rejects orders whose price/size carry more decimals than the
product's quote_increment / base_increment. Smoking gun:
ALEPH-USD "Too many decimals in order price" (2026-05-10 19:50 UTC log).
The other 8 stops failed with UNKNOWN_FAILURE_REASON — same root cause
wrapped in a generic Coinbase error.

This module pins the quantize-then-submit contract added to
``CoinbaseSpotAdapter.place_stop_limit_order_gtc`` and to the
module-level ``_quantize_price`` / ``_quantize_size`` helpers.

Mocked SDK throughout — no real Coinbase calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.trading.venue import coinbase_spot
from app.services.trading.venue.coinbase_spot import (
    CoinbaseSpotAdapter,
    _quantize_price,
    _quantize_size,
    reset_product_info_cache_for_tests,
)
from app.services.trading.venue.protocol import (
    NormalizedProduct,
    VenueAdapterError,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Per-test product-info cache reset so cache-hit / TTL tests are
    deterministic."""
    reset_product_info_cache_for_tests()
    yield
    reset_product_info_cache_for_tests()


def _product(
    *,
    pid: str = "ADA-USD",
    base_increment: float = 0.1,
    quote_increment: float = 0.0001,
    min_market_funds: float | None = None,
) -> NormalizedProduct:
    return NormalizedProduct(
        product_id=pid,
        base_currency=pid.split("-")[0],
        quote_currency=pid.split("-")[1] if "-" in pid else "USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_increment=base_increment,
        quote_increment=quote_increment,
        min_market_funds=min_market_funds,
    )


def _bypass_gates(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store.remember",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.try_acquire",
        lambda v: (True, 0),
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.rate_limited_response",
        lambda v, retry, client_order_id=None: {
            "ok": False, "error": "rate_limited", "retry_after_s": retry,
        },
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.cb.clear_cache",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot."
        "order_state_machine.record_transition_standalone",
        lambda **kw: None,
    )


def _adapter_with_product(prod: NormalizedProduct, *, sdk_response=None):
    """Build a CoinbaseSpotAdapter with the SDK + product-info both
    mocked. Returns (adapter, fake_client, get_product_mock)."""
    if sdk_response is None:
        sdk_response = {
            "success": True,
            "success_response": {"order_id": "CB-OK-1", "side": "SELL"},
        }
    adapter = CoinbaseSpotAdapter()
    fake_client = MagicMock()
    fake_client.stop_limit_order_gtc_buy.return_value = sdk_response
    fake_client.stop_limit_order_gtc_sell.return_value = sdk_response
    fake_client.limit_order_gtc_buy.return_value = sdk_response
    fake_client.limit_order_gtc_sell.return_value = sdk_response
    fake_client.market_order_buy.return_value = sdk_response
    fake_client.market_order_sell.return_value = sdk_response
    adapter._client = lambda: fake_client  # type: ignore[assignment]

    from app.services.trading.venue.protocol import FreshnessMeta
    from datetime import datetime, timezone
    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=15.0)
    get_product_mock = MagicMock(return_value=(prod, fresh))
    adapter.get_product = get_product_mock  # type: ignore[assignment]
    return adapter, fake_client, get_product_mock


# ─── 1. Helper unit tests ────────────────────────────────────────────


def test_quantize_price_down_high_decimal_subpenny():
    """ALEPH-USD-class: 8-decimal float quantized to 7-decimal increment."""
    assert _quantize_price(0.00786416, 0.0000001, mode="down") == "0.0078641"


def test_quantize_price_down_low_decimal_btc():
    """BTC-USD-class: high-magnitude price quantized to 0.01."""
    assert _quantize_price(68234.123456, 0.01, mode="down") == "68234.12"


def test_quantize_price_up_buy_stop():
    """BUY stop: rounds UP to keep trigger at-or-above intent."""
    assert _quantize_price(0.00786416, 0.0000001, mode="up") == "0.0078642"


def test_quantize_size_down_high_decimal():
    """Size always rounds DOWN — never order more than intended."""
    assert _quantize_size(10.123456789, 0.01) == "10.12"


def test_quantize_invalid_increment_raises():
    with pytest.raises(ValueError, match="increment must be > 0"):
        _quantize_price(1.0, 0.0, mode="down")
    with pytest.raises(ValueError, match="increment must be > 0"):
        _quantize_price(1.0, -0.01, mode="down")


def test_quantize_non_finite_raises():
    with pytest.raises(ValueError, match="finite"):
        _quantize_price(float("nan"), 0.01, mode="down")
    with pytest.raises(ValueError, match="finite"):
        _quantize_price(float("inf"), 0.01, mode="down")


def test_quantize_invalid_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        _quantize_price(1.0, 0.01, mode="sideways")  # type: ignore[arg-type]


# ─── 2. Cache behavior ───────────────────────────────────────────────


def test_product_info_cache_hit_avoids_refetch(monkeypatch):
    """Two placements for same product → get_product called once."""
    _bypass_gates(monkeypatch)
    prod = _product()
    adapter, sdk, get_product_mock = _adapter_with_product(prod)
    for _ in range(2):
        adapter.place_stop_limit_order_gtc(
            product_id="ADA-USD", side="sell",
            base_size="100.0", stop_price="0.4500", limit_price="0.4400",
        )
    # Second call hit the cache, not get_product.
    assert get_product_mock.call_count == 1
    assert sdk.stop_limit_order_gtc_sell.call_count == 2


def test_product_info_cache_ttl_refetches(monkeypatch):
    """After TTL expiry (forced via patched time.time), fetch again."""
    _bypass_gates(monkeypatch)
    prod = _product()
    adapter, sdk, get_product_mock = _adapter_with_product(prod)

    fake_clock = {"now": 1_000_000.0}
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.time.time",
        lambda: fake_clock["now"],
    )

    adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.4500", limit_price="0.4400",
    )
    # Jump past TTL (3600s + 1).
    fake_clock["now"] += 3601.0
    adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.4500", limit_price="0.4400",
    )
    assert get_product_mock.call_count == 2


def test_product_info_fetch_failure_raises_no_fallback(monkeypatch):
    """When get_product returns (None, fresh): refuse to place — no
    magic-fallback tick_size guess. Returns ok=False with explicit code."""
    _bypass_gates(monkeypatch)
    adapter, sdk, _ = _adapter_with_product(_product())
    from app.services.trading.venue.protocol import FreshnessMeta
    from datetime import datetime, timezone
    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=15.0)
    adapter.get_product = lambda pid: (None, fresh)  # type: ignore[assignment]

    res = adapter.place_stop_limit_order_gtc(
        product_id="ALEPH-USD", side="sell",
        base_size="100.0", stop_price="0.00786416", limit_price="0.00786416",
    )
    assert res["ok"] is False
    assert res.get("code") == "product_info_unavailable"
    assert "magic-fallback" in res["error"]
    sdk.stop_limit_order_gtc_sell.assert_not_called()


def test_product_info_invalid_increment_raises(monkeypatch):
    """quote_increment=None → ok=False with code product_info_invalid."""
    _bypass_gates(monkeypatch)
    bad_prod = NormalizedProduct(
        product_id="BROKEN-USD", base_currency="BROKEN", quote_currency="USD",
        status="online", trading_disabled=False, cancel_only=False,
        limit_only=False, post_only=False, auction_mode=False,
        base_increment=0.1, quote_increment=None, min_market_funds=None,
    )
    adapter, sdk, _ = _adapter_with_product(bad_prod)
    res = adapter.place_stop_limit_order_gtc(
        product_id="BROKEN-USD", side="sell",
        base_size="100.0", stop_price="1.0", limit_price="0.99",
    )
    assert res["ok"] is False
    assert res.get("code") == "product_info_invalid"
    sdk.stop_limit_order_gtc_sell.assert_not_called()


# ─── 3. End-to-end integration through place_stop_limit_order_gtc ────


def test_aleph_usd_repro_quantizes_to_quote_increment(monkeypatch):
    """The smoking-gun reproducer. Sub-penny product (quote_increment=
    0.0000001) with an 8-decimal raw stop_price; SDK must receive the
    quantized 7-decimal string, not the raw float string."""
    _bypass_gates(monkeypatch)
    prod = _product(
        pid="ALEPH-USD",
        base_increment=1.0,
        quote_increment=0.0000001,
    )
    adapter, sdk, _ = _adapter_with_product(prod)

    # Realistic: bracket writer puts limit slightly below stop so the
    # SELL stop accepts a fill when triggered.
    res = adapter.place_stop_limit_order_gtc(
        product_id="ALEPH-USD", side="sell",
        base_size="237.0",
        stop_price="0.00786416",
        limit_price="0.00785416",
    )
    assert res["ok"] is True
    call_kwargs = sdk.stop_limit_order_gtc_sell.call_args.kwargs
    # 8-decimal raw input was the production-rejected shape. Both fields
    # must now be ≤ 7 decimals (quote_increment=0.0000001).
    assert call_kwargs["stop_price"] == "0.0078641"
    assert call_kwargs["limit_price"] == "0.0078541"
    assert "." in call_kwargs["stop_price"]
    assert len(call_kwargs["stop_price"].split(".")[1]) <= 7


def test_sell_stop_limit_below_stop_after_quantize(monkeypatch):
    """When buffer collapses (stop=0.10, limit=0.0999, increment=0.01),
    submitted limit_price must be < stop_price (one tick below)."""
    _bypass_gates(monkeypatch)
    prod = _product(quote_increment=0.01, base_increment=1.0)
    adapter, sdk, _ = _adapter_with_product(prod)
    adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100",
        stop_price="0.10",
        limit_price="0.0999",
    )
    call_kwargs = sdk.stop_limit_order_gtc_sell.call_args.kwargs
    from decimal import Decimal
    assert Decimal(call_kwargs["limit_price"]) < Decimal(call_kwargs["stop_price"])


def test_buy_stop_limit_above_stop_after_quantize(monkeypatch):
    """Symmetric for BUY: limit_price > stop_price after quantization."""
    _bypass_gates(monkeypatch)
    prod = _product(quote_increment=0.01, base_increment=1.0)
    adapter, sdk, _ = _adapter_with_product(prod)
    adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="buy",
        base_size="100",
        stop_price="0.10",
        limit_price="0.1001",
    )
    call_kwargs = sdk.stop_limit_order_gtc_buy.call_args.kwargs
    from decimal import Decimal
    assert Decimal(call_kwargs["limit_price"]) > Decimal(call_kwargs["stop_price"])


def test_min_market_funds_rejected_with_explicit_error(monkeypatch):
    """Notional below min_market_funds → ok=False with explicit error
    naming min_market_funds; SDK is NOT called."""
    _bypass_gates(monkeypatch)
    prod = _product(
        base_increment=0.0001,
        quote_increment=0.01,
        min_market_funds=10.0,
    )
    adapter, sdk, _ = _adapter_with_product(prod)
    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="0.5",
        stop_price="1.00",
        limit_price="0.99",
    )
    assert res["ok"] is False
    assert "min_market_funds" in res["error"]
    sdk.stop_limit_order_gtc_sell.assert_not_called()


def test_above_min_market_funds_passes(monkeypatch):
    """Notional above min_market_funds → SDK is invoked."""
    _bypass_gates(monkeypatch)
    prod = _product(
        base_increment=0.0001,
        quote_increment=0.01,
        min_market_funds=1.0,
    )
    adapter, sdk, _ = _adapter_with_product(prod)
    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="50.0",
        stop_price="1.00",
        limit_price="0.99",
    )
    assert res["ok"] is True
    sdk.stop_limit_order_gtc_sell.assert_called_once()


def test_size_quantized_down_to_base_increment_in_sdk_call(monkeypatch):
    """base_size='10.123456789' with base_increment=0.01 → SDK kwargs
    show base_size='10.12'."""
    _bypass_gates(monkeypatch)
    prod = _product(base_increment=0.01, quote_increment=0.01)
    adapter, sdk, _ = _adapter_with_product(prod)
    adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="10.123456789",
        stop_price="1.00",
        limit_price="0.99",
    )
    call_kwargs = sdk.stop_limit_order_gtc_sell.call_args.kwargs
    assert call_kwargs["base_size"] == "10.12"


def test_limit_order_quantizes_size_and_limit_price_in_sdk_call(monkeypatch):
    """Regular limit entries use the venue product increments too."""
    _bypass_gates(monkeypatch)
    prod = _product(pid="THQ-USD", base_increment=0.1, quote_increment=0.00001)
    adapter, sdk, _ = _adapter_with_product(prod)

    res = adapter.place_limit_order_gtc(
        product_id="THQ-USD",
        side="sell",
        base_size="5950.36752793",
        limit_price="0.020481",
        client_order_id="limit-quant",
    )

    assert res["ok"] is True
    call_kwargs = sdk.limit_order_gtc_sell.call_args.kwargs
    assert call_kwargs["base_size"] == "5950.3"
    assert call_kwargs["limit_price"] == "0.02049"
    assert res["base_size"] == "5950.3"
    assert res["limit_price"] == "0.02049"


def test_market_order_quantizes_size_in_sdk_call(monkeypatch):
    """Market entries should not send more size precision than Coinbase allows."""
    _bypass_gates(monkeypatch)
    prod = _product(pid="THQ-USD", base_increment=0.1, quote_increment=0.00001)
    adapter, sdk, _ = _adapter_with_product(prod)

    res = adapter.place_market_order(
        product_id="THQ-USD",
        side="sell",
        base_size="5950.36752793",
        client_order_id="market-quant",
    )

    assert res["ok"] is True
    call_kwargs = sdk.market_order_sell.call_args.kwargs
    assert call_kwargs["base_size"] == "5950.3"
    assert res["base_size"] == "5950.3"


def test_existing_safety_gates_intact_duplicate_check_short_circuits(monkeypatch):
    """Quantization must be additive — duplicate client_order_id still
    short-circuits BEFORE the product-info fetch happens. Regression
    guard for ordering of the new block."""
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: True,
    )
    prod = _product()
    adapter, sdk, get_product_mock = _adapter_with_product(prod)

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.4500", limit_price="0.4400",
        client_order_id="dupe-key",
    )
    assert res["ok"] is False
    assert "duplicate" in res["error"].lower()
    sdk.stop_limit_order_gtc_sell.assert_not_called()
    get_product_mock.assert_not_called()
