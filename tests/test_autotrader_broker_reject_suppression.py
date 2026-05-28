from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.auto_trader import (
    _annotate_broker_reject,
    _annotate_missing_order_id_broker_reject,
    _broker_reject_action_fingerprint,
)


def test_broker_reject_fingerprint_is_stable_and_action_sensitive():
    alert = SimpleNamespace(ticker="ready", asset_type="stock", scan_pattern_id=42)
    snap = {"options_path": False}

    first = _broker_reject_action_fingerprint(
        alert,
        venue="robinhood",
        side="buy",
        qty=1.25,
        snap=snap,
        order_hint="market",
    )
    second = _broker_reject_action_fingerprint(
        alert,
        venue="Robinhood",
        side="BUY",
        qty=1.25,
        snap=snap,
        order_hint="market",
    )
    different_qty = _broker_reject_action_fingerprint(
        alert,
        venue="robinhood",
        side="buy",
        qty=1.50,
        snap=snap,
        order_hint="market",
    )

    assert first == second
    assert first != different_qty


def test_broker_reject_annotation_preserves_audit_fingerprint():
    snap = {}

    _annotate_broker_reject(
        snap,
        fingerprint="abc123",
        venue="robinhood",
        error="quantity precision",
    )

    assert snap["broker_reject_fingerprint"] == "abc123"
    assert snap["broker_reject_venue"] == "robinhood"
    assert snap["broker_reject_error"] == "quantity precision"


def test_missing_order_id_annotation_fingerprints_post_call_broker_failure():
    alert = SimpleNamespace(ticker="ACMR", asset_type="stock", scan_pattern_id=1246)
    snap = {"options_path": False}
    res = {"ok": True, "base_size": 1.25, "client_order_id": "atv1-99-buy"}

    fp = _annotate_missing_order_id_broker_reject(
        alert,
        qty=1.25,
        snap=snap,
        res=res,
    )

    expected = _broker_reject_action_fingerprint(
        alert,
        venue="robinhood",
        side="buy",
        qty=1.25,
        snap=snap,
        order_hint="market",
    )
    assert fp == expected
    assert snap["broker_reject_fingerprint"] == expected
    assert snap["broker_reject_venue"] == "robinhood"
    assert snap["broker_reject_error"] == "place_no_order_id"
    assert snap["broker_reject_missing_order_id"] is True
    assert snap["broker_reject_client_order_id"] == "atv1-99-buy"


def test_missing_order_id_annotation_preserves_coinbase_maker_shape():
    alert = SimpleNamespace(ticker="BTC-USD", asset_type="crypto", scan_pattern_id=585)
    snap = {"options_path": False}
    res = {
        "ok": True,
        "base_size": "0.01",
        "client_order_id": "atv1-101-buy",
        "_chili_broker_source": "coinbase",
        "_chili_maker_only": True,
    }

    fp = _annotate_missing_order_id_broker_reject(
        alert,
        qty=0.01,
        snap=snap,
        res=res,
    )

    expected = _broker_reject_action_fingerprint(
        alert,
        venue="coinbase",
        side="buy",
        qty=0.01,
        snap=snap,
        order_hint="limit_post_only",
    )
    assert fp == expected
    assert snap["broker_reject_venue"] == "coinbase"
    assert snap["broker_reject_order_hint"] == "limit_post_only"
