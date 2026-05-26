from __future__ import annotations

import sys
import types

from app.services import broker_service


def _comp_pair_info() -> dict:
    return {
        "symbol": "COMP-USD",
        "min_order_quantity_increment": "0.000010000000000000",
        "min_order_size": "0.000100000000000000",
        "max_order_size": "6083.0000000000000000",
        "min_order_price_increment": "0.010000000000000000",
    }


def test_robinhood_crypto_quantity_uses_pair_increment(monkeypatch):
    monkeypatch.setattr(
        broker_service,
        "_get_robinhood_crypto_pair_info",
        lambda base: _comp_pair_info(),
    )

    qty, meta = broker_service._normalize_robinhood_crypto_quantity(
        "COMP",
        "0.22494563",
    )

    assert qty == "0.22494"
    assert meta["adjusted"] is True
    assert meta["min_order_quantity_increment"] == "0.00001"


def test_robinhood_crypto_error_extracts_field_messages():
    msg = broker_service._extract_robinhood_error_message(
        {
            "quantity": [
                "Your order quantity has too much precision. Please round the quantity.",
            ],
        },
        "fallback",
    )

    assert msg.startswith("quantity: Your order quantity has too much precision")


def test_robinhood_crypto_buy_submits_aligned_quantity(monkeypatch):
    captured = {}

    def fake_order_buy_crypto_by_quantity(*, symbol, quantity, jsonify):
        captured.update(symbol=symbol, quantity=quantity, jsonify=jsonify)
        return _FakeResponse(201, {"id": "rh-crypto-1", "state": "queued"})

    fake_rh = types.ModuleType("robin_stocks.robinhood")
    fake_rh.orders = types.SimpleNamespace(
        order_buy_crypto_by_quantity=fake_order_buy_crypto_by_quantity,
    )
    fake_rh.crypto = types.SimpleNamespace(get_crypto_info=lambda symbol: _comp_pair_info())
    fake_pkg = types.ModuleType("robin_stocks")
    fake_pkg.robinhood = fake_rh
    monkeypatch.setitem(sys.modules, "robin_stocks", fake_pkg)
    monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", fake_rh)

    monkeypatch.setattr(broker_service, "_rh_available", True)
    monkeypatch.setattr(broker_service, "is_connected", lambda: True)
    monkeypatch.setattr(broker_service, "_is_crypto_supported_on_robinhood", lambda base: True)
    monkeypatch.setattr(broker_service, "_retry_api_call", lambda fn, label=None: fn())

    res = broker_service.place_crypto_buy_order("COMP-USD", 0.22494563)

    assert res["ok"] is True
    assert captured == {
        "symbol": "COMP",
        "quantity": 0.22494,
        "jsonify": False,
    }


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_robinhood_crypto_response_preserves_422_body():
    body = {
        "quantity": [
            "Your order quantity has too much precision. Please round the quantity.",
        ],
    }

    parsed, error, raw = broker_service._coerce_robinhood_crypto_order_response(
        _FakeResponse(422, body),
        fallback="fallback",
    )

    assert parsed is None
    assert raw == body
    assert error.startswith("Robinhood crypto HTTP 422: quantity: Your order quantity")
