from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.auto_trader import (
    _annotate_broker_reject,
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
