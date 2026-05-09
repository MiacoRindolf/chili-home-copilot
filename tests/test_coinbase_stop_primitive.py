"""f-coinbase-autotrader-enablement-phase-4-bracket-writer-path (2026-05-09).

Pin `CoinbaseSpotAdapter.place_stop_limit_order_gtc`:

  * Returns the same envelope shape as `place_limit_order_gtc`:
    `{ok: True, order_id, client_order_id, raw}` on success;
    `{ok: False, error, client_order_id}` on failure.
  * Default `stop_direction` is STOP_DIRECTION_STOP_DOWN for sell;
    STOP_DIRECTION_STOP_UP for buy.
  * Invalid side returns ok=False without calling the SDK.
  * SDK exception caught and packaged as ok=False (the bracket
    writer's code-bug detector then arms its cooldown).

Helper-level mocked SDK; no real Coinbase call.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter


def _make_adapter(*, sdk_response):
    """Build an adapter with the SDK methods mocked to return
    `sdk_response`. Bypass the gating helpers (rate-limiter,
    idempotency-store, state-machine) that don't matter for the
    primitive's contract."""
    adapter = CoinbaseSpotAdapter()
    fake_client = MagicMock()
    fake_client.stop_limit_order_gtc_buy.return_value = sdk_response
    fake_client.stop_limit_order_gtc_sell.return_value = sdk_response
    adapter._client = lambda: fake_client  # type: ignore[assignment]
    return adapter, fake_client


def _success_resp(order_id: str = "CB-STOP-1"):
    return {
        "success": True,
        "success_response": {"order_id": order_id, "side": "SELL"},
        "order_configuration": {"stop_limit_stop_limit_gtc": {}},
    }


def _failure_resp(message: str = "Insufficient base balance"):
    return {
        "success": False,
        "error_response": {"message": message, "error": message},
    }


# ── Sell stop-loss happy path ────────────────────────────────────────


def test_sell_stop_loss_success_envelope_shape(monkeypatch):
    """Standard SELL stop-loss returns ok=True + order_id + raw."""
    adapter, sdk = _make_adapter(sdk_response=_success_resp("CB-1"))

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
        "app.services.trading.venue.coinbase_spot.cb.clear_cache",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot."
        "order_state_machine.record_transition_standalone",
        lambda **kw: None,
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD",
        side="sell",
        base_size="100.0",
        stop_price="0.4500",
        limit_price="0.4400",
        client_order_id="cli-test-1",
    )
    assert res["ok"] is True
    assert res["order_id"] == "CB-1"
    assert res["client_order_id"] == "cli-test-1"
    assert "raw" in res

    # Verify the SDK call args carry the right shape.
    sdk.stop_limit_order_gtc_sell.assert_called_once()
    call_kwargs = sdk.stop_limit_order_gtc_sell.call_args.kwargs
    assert call_kwargs["product_id"] == "ADA-USD"
    assert call_kwargs["base_size"] == "100.0"
    assert call_kwargs["stop_price"] == "0.4500"
    assert call_kwargs["limit_price"] == "0.4400"
    assert call_kwargs["stop_direction"] == "STOP_DIRECTION_STOP_DOWN"


# ── Buy stop default direction ───────────────────────────────────────


def test_buy_stop_default_direction_is_up(monkeypatch):
    adapter, sdk = _make_adapter(sdk_response=_success_resp())
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
        "app.services.trading.venue.coinbase_spot.cb.clear_cache",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot."
        "order_state_machine.record_transition_standalone",
        lambda **kw: None,
    )

    adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="buy",
        base_size="100.0", stop_price="0.5000", limit_price="0.5050",
    )
    call_kwargs = sdk.stop_limit_order_gtc_buy.call_args.kwargs
    assert call_kwargs["stop_direction"] == "STOP_DIRECTION_STOP_UP"


# ── Failure path ─────────────────────────────────────────────────────


def test_failure_response_packaged_as_ok_false(monkeypatch):
    adapter, sdk = _make_adapter(
        sdk_response=_failure_resp("Insufficient base balance"),
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.try_acquire",
        lambda v: (True, 0),
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot."
        "order_state_machine.record_transition_standalone",
        lambda **kw: None,
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.45", limit_price="0.44",
    )
    assert res["ok"] is False
    assert "Insufficient base balance" in res["error"]
    assert "client_order_id" in res


def test_sdk_raises_caught_and_packaged_as_ok_false(monkeypatch):
    """A raised SDK exception MUST be caught and returned as ok=False
    so the bracket writer's code-bug detector can arm its cooldown."""
    adapter = CoinbaseSpotAdapter()
    fake_client = MagicMock()
    fake_client.stop_limit_order_gtc_sell.side_effect = IndexError(
        "list index out of range"
    )
    adapter._client = lambda: fake_client  # type: ignore[assignment]

    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.try_acquire",
        lambda v: (True, 0),
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot."
        "order_state_machine.record_transition_standalone",
        lambda **kw: None,
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.45", limit_price="0.44",
    )
    assert res["ok"] is False
    assert "list index out of range" in res["error"]
    assert "client_order_id" in res


# ── Invalid side ─────────────────────────────────────────────────────


def test_invalid_side_rejected_without_sdk_call(monkeypatch):
    adapter, sdk = _make_adapter(sdk_response=_success_resp())
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.try_acquire",
        lambda v: (True, 0),
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="hold",
        base_size="100.0", stop_price="0.45", limit_price="0.44",
    )
    assert res["ok"] is False
    assert "invalid side" in res["error"].lower()
    sdk.stop_limit_order_gtc_buy.assert_not_called()
    sdk.stop_limit_order_gtc_sell.assert_not_called()


# ── Idempotency dedupe ───────────────────────────────────────────────


def test_duplicate_client_order_id_short_circuits(monkeypatch):
    adapter, sdk = _make_adapter(sdk_response=_success_resp())
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: True,
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.45", limit_price="0.44",
        client_order_id="dupe-key",
    )
    assert res["ok"] is False
    assert "duplicate" in res["error"].lower()
    sdk.stop_limit_order_gtc_sell.assert_not_called()


# ── Adapter disabled ─────────────────────────────────────────────────


def test_disabled_adapter_short_circuits(monkeypatch):
    adapter, sdk = _make_adapter(sdk_response=_success_resp())
    monkeypatch.setattr(
        "app.config.settings.chili_coinbase_spot_adapter_enabled",
        False, raising=False,
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.45", limit_price="0.44",
    )
    assert res["ok"] is False
    assert "disabled" in res["error"].lower()
    sdk.stop_limit_order_gtc_sell.assert_not_called()


# ── Rate-limiter gate ───────────────────────────────────────────────


def test_rate_limited_returns_canonical_response(monkeypatch):
    adapter, sdk = _make_adapter(sdk_response=_success_resp())
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.idempotency_store"
        ".is_duplicate", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.try_acquire",
        lambda v: (False, 12.5),
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.rate_limiter.rate_limited_response",
        lambda v, retry, client_order_id=None: {
            "ok": False, "error": "rate_limited", "retry_after_s": retry,
        },
    )
    monkeypatch.setattr(
        "app.services.trading.venue.coinbase_spot.venue_health.record_rate_limit_event",
        lambda **kw: None,
    )

    res = adapter.place_stop_limit_order_gtc(
        product_id="ADA-USD", side="sell",
        base_size="100.0", stop_price="0.45", limit_price="0.44",
    )
    assert res["ok"] is False
    assert res.get("error") == "rate_limited"
    sdk.stop_limit_order_gtc_sell.assert_not_called()
