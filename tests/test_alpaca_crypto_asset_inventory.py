"""Crypto data preparation uses asset truth before execution is certified."""
from types import SimpleNamespace

import pytest

from app.services.trading.venue import alpaca_spot as ap


def asset(**values):
    return SimpleNamespace(**dict({
        "symbol": "BTC/USD", "asset_class": "crypto", "status": "active",
        "tradable": True, "fractionable": True, "marginable": False,
        "shortable": False, "min_order_size": "0.0001",
        "min_trade_increment": "0.000000001", "price_increment": "0.01",
    }, **values))


def adapter(monkeypatch, rows):
    calls = []
    class Client:
        def get_asset(self, symbol):
            calls.append(symbol)
            return rows[0]
        def get_all_assets(self, request):
            calls.append(request)
            return rows
    result = ap.AlpacaSpotAdapter()
    monkeypatch.setattr(result, "_account_client", lambda: Client())
    return result, calls


def test_crypto_product_probe_preserves_fractional_constraints(monkeypatch):
    a, calls = adapter(monkeypatch, [asset()])
    product, _, error = a.get_product_probe("BTC-USD")
    assert error is None and calls == ["BTC/USD"]
    assert (product.base_currency, product.quote_currency) == ("BTC", "USD")
    assert product.base_min_size == 0.0001 and product.base_increment == 1e-9
    assert product.raw["exact_constraints"] == {
        "min_order_size": "0.0001", "min_trade_increment": "1E-9", "price_increment": "0.01"}
    assert product.raw["order_authority_granted"] is False


@pytest.mark.parametrize("field,value", [
    ("min_order_size", None), ("min_order_size", "0"),
    ("min_trade_increment", "NaN"), ("price_increment", "-0.01"),
    ("price_increment", "Infinity"), ("price_increment", "1e-1000"),
])
def test_bad_crypto_constraints_never_fall_back_to_one_coin_or_one_cent(monkeypatch, field, value):
    a, _ = adapter(monkeypatch, [asset(**{field: value})])
    product, _, error = a.get_product_probe("BTC-USD")
    assert product is None and error == "crypto_asset_constraint_unavailable:" + field


def test_inventory_uses_broker_crypto_class_and_preserves_all_quote_currencies(monkeypatch):
    a, calls = adapter(monkeypatch, [asset(), asset(symbol="ETH/BTC"),
        asset(symbol="DOGE/USD", tradable=False), asset(symbol="USDC/USD")])
    products, _, error = a.get_crypto_products_probe()
    assert error is None
    assert calls[0].asset_class.value == "crypto" and calls[0].status.value == "active"
    assert [p.product_id for p in products] == ["BTC-USD", "ETH-BTC", "USDC-USD"]
    assert products[1].quote_currency == "BTC"
    assert all(not p.raw["order_authority_granted"] for p in products)


@pytest.mark.parametrize("rows", [None, [asset(), asset()], [asset(price_increment=None)]])
def test_incomplete_or_contradictory_catalog_is_not_a_successful_empty_universe(monkeypatch, rows):
    a, _ = adapter(monkeypatch, rows)
    products, _, error = a.get_crypto_products_probe()
    assert products == [] and error is not None


def test_equity_probe_retains_whole_share_stop_compatible_constraints(monkeypatch):
    a, _ = adapter(monkeypatch, [asset(symbol="AAPL", asset_class="us_equity")])
    product, _, error = a.get_product_probe("AAPL")
    assert error is None and product.product_type == "equity"
    assert product.base_increment == product.base_min_size == 1


def test_crypto_symbol_mismatch_is_not_listing_authority(monkeypatch):
    a, _ = adapter(monkeypatch, [asset(symbol="ETH/USD")])
    product, _, error = a.get_product_probe("BTC-USD")
    assert product is None and error == "crypto_asset_symbol_mismatch"


def test_actual_broker_catalog_constraints_round_trip_without_a_majors_list(monkeypatch):
    import json
    from decimal import Decimal
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / "fixtures" /
        "alpaca_crypto_assets_20260912.json").read_text(encoding="utf-8"))
    raw_assets = fixture["assets"]
    assets = [SimpleNamespace(**{**row, "asset_class": row["class"]}) for row in raw_assets]
    a, _ = adapter(monkeypatch, assets)
    products, _, error = a.get_crypto_products_probe()
    assert error is None
    expected = {row["symbol"]: row for row in raw_assets
                if row["tradable"] is True and row["status"] == "active"}
    assert {p.raw["broker_symbol"] for p in products} == set(expected)
    for product in products:
        row = expected[product.raw["broker_symbol"]]
        for field, value in product.raw["exact_constraints"].items():
            assert Decimal(value) == Decimal(str(row[field]))
        assert (product.base_currency, product.quote_currency) == tuple(row["symbol"].split("/"))
