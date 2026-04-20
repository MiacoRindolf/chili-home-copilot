"""Tests for the Phase G.2 bracket writer (SCAFFOLDED, DEFAULT OFF).

These tests prove the writer's behavior BEFORE the flag flips in
production. Rollout plan (in the module docstring) calls for flipping
``chili_bracket_writer_g2_enabled`` after reviewing this suite.

Coverage:

  * Default (both feature flags off) → every action returns ok=False,
    reason='disabled', and NEVER calls the adapter.
  * Each enable flag gates only its own action.
  * Unsupported venue (e.g. coinbase) → unsupported_venue.
  * Invalid decision (wrong kind / missing payload fields) → invalid_decision.
  * resize_stop_for_partial_fill with a partial_fill decision → cancel
    happens BEFORE place; if cancel fails, place is NOT attempted.
  * If cancel succeeds but place fails → CRITICAL log, position is flagged
    unprotected, action returns place_failed.
  * place_missing_stop with a valid decision → single place call, returns ok.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.trading import bracket_writer_g2 as g2
from app.services.trading.bracket_reconciler import ReconciliationDecision


def _on_cfg(**overrides) -> SimpleNamespace:
    """Return a settings object with the writer flags ON, plus overrides."""
    base = dict(
        chili_bracket_writer_g2_enabled=True,
        chili_bracket_writer_g2_partial_fill_resize=True,
        chili_bracket_writer_g2_place_missing_stop=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _off_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        chili_bracket_writer_g2_enabled=False,
        chili_bracket_writer_g2_partial_fill_resize=False,
        chili_bracket_writer_g2_place_missing_stop=False,
    )


def _partial_fill_decision(expected_qty: float) -> ReconciliationDecision:
    return ReconciliationDecision(
        kind="qty_drift",
        severity="warn",
        delta_payload={
            "drift_kind": "partial_fill",
            "is_partial_fill": True,
            "expected_stop_qty": expected_qty,
            "local_qty": expected_qty * 2,
            "broker_qty": expected_qty,
            "abs_diff": expected_qty,
            "fill_ratio": 0.5,
        },
    )


def _missing_stop_decision() -> ReconciliationDecision:
    return ReconciliationDecision(
        kind="missing_stop",
        severity="warn",
        delta_payload={"intent_state": "shadow_logged"},
    )


# ── Default-off assertions (these are the rollout safety net) ─────────


def test_default_config_blocks_resize_never_calls_adapter(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _off_cfg())
    factory = MagicMock()

    result = g2.resize_stop_for_partial_fill(
        db,
        trade_id=1, bracket_intent_id=1, ticker="OFF1",
        broker_source="robinhood",
        decision=_partial_fill_decision(5.0),
        prior_stop_order_id="stop-1", stop_price=95.0,
        adapter_factory=factory,
    )
    assert result.ok is False
    assert result.reason == "disabled"
    factory.assert_not_called()


def test_default_config_blocks_place_never_calls_adapter(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _off_cfg())
    factory = MagicMock()

    result = g2.place_missing_stop(
        db,
        trade_id=1, bracket_intent_id=1, ticker="OFF2",
        broker_source="robinhood",
        decision=_missing_stop_decision(),
        local_quantity=5.0, stop_price=95.0,
        adapter_factory=factory,
    )
    assert result.ok is False
    assert result.reason == "disabled"
    factory.assert_not_called()


def test_top_level_on_but_per_action_off_still_blocked(db, monkeypatch):
    """Top-level flag alone is not enough — the per-action flag must also be on."""
    cfg = SimpleNamespace(
        chili_bracket_writer_g2_enabled=True,
        chili_bracket_writer_g2_partial_fill_resize=False,
        chili_bracket_writer_g2_place_missing_stop=False,
    )
    monkeypatch.setattr(g2, "settings", cfg)
    factory = MagicMock()

    r1 = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="T",
        broker_source="robinhood",
        decision=_partial_fill_decision(5.0),
        prior_stop_order_id="s", stop_price=95.0,
        adapter_factory=factory,
    )
    r2 = g2.place_missing_stop(
        db, trade_id=1, bracket_intent_id=1, ticker="T",
        broker_source="robinhood",
        decision=_missing_stop_decision(),
        local_quantity=5.0, stop_price=95.0,
        adapter_factory=factory,
    )
    assert r1.reason == "disabled"
    assert r2.reason == "disabled"
    factory.assert_not_called()


# ── Validation ────────────────────────────────────────────────────────


def test_resize_rejects_non_partial_fill_decision(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _on_cfg())
    factory = MagicMock()

    bad_decision = ReconciliationDecision(
        kind="qty_drift", severity="error",
        delta_payload={"drift_kind": "broker_flat", "expected_stop_qty": None},
    )
    result = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="T",
        broker_source="robinhood", decision=bad_decision,
        prior_stop_order_id="stop-1", stop_price=95.0,
        adapter_factory=factory,
    )
    assert result.reason == "invalid_decision"
    factory.assert_not_called()


def test_resize_rejects_missing_prior_stop(db, monkeypatch):
    """If the broker has no stop to cancel, resize must refuse — routing
    belongs to place_missing_stop instead."""
    monkeypatch.setattr(g2, "settings", _on_cfg())
    factory = MagicMock()

    result = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="T",
        broker_source="robinhood",
        decision=_partial_fill_decision(5.0),
        prior_stop_order_id=None, stop_price=95.0,
        adapter_factory=factory,
    )
    assert result.reason == "invalid_decision"
    factory.assert_not_called()


def test_resize_rejects_unsupported_venue(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _on_cfg())
    factory = MagicMock()

    result = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="BTC-USD",
        broker_source="coinbase",
        decision=_partial_fill_decision(0.1),
        prior_stop_order_id="stop-1", stop_price=50_000.0,
        adapter_factory=factory,
    )
    assert result.reason == "unsupported_venue"
    factory.assert_not_called()


def test_place_rejects_wrong_kind(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _on_cfg())
    factory = MagicMock()

    wrong = ReconciliationDecision(kind="agree", severity="info")
    result = g2.place_missing_stop(
        db, trade_id=1, bracket_intent_id=1, ticker="T",
        broker_source="robinhood", decision=wrong,
        local_quantity=5.0, stop_price=95.0,
        adapter_factory=factory,
    )
    assert result.reason == "invalid_decision"
    factory.assert_not_called()


# ── Happy path ─────────────────────────────────────────────────────────


def test_resize_cancels_then_places_when_enabled(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _on_cfg())

    adapter = MagicMock()
    adapter.cancel_order.return_value = {"ok": True, "raw": {}}
    adapter.place_limit_order_gtc.return_value = {
        "ok": True, "order_id": "new-stop-1", "raw": {}
    }
    factory = MagicMock(return_value=adapter)

    result = g2.resize_stop_for_partial_fill(
        db, trade_id=42, bracket_intent_id=7, ticker="RSZ",
        broker_source="robinhood",
        decision=_partial_fill_decision(5.0),
        prior_stop_order_id="old-stop-7", stop_price=92.5,
        adapter_factory=factory,
    )

    assert result.ok is True
    assert result.reason == "ok"
    assert result.prior_stop_order_id == "old-stop-7"
    assert result.new_stop_order_id == "new-stop-1"
    assert result.new_stop_qty == 5.0
    assert result.new_stop_price == 92.5

    # Cancel MUST be called before place.
    calls = adapter.method_calls
    assert calls[0][0] == "cancel_order"
    assert calls[0][1] == ("old-stop-7",)
    assert calls[1][0] == "place_limit_order_gtc"


def test_place_missing_stop_happy_path(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _on_cfg())

    adapter = MagicMock()
    adapter.place_limit_order_gtc.return_value = {
        "ok": True, "order_id": "new-stop-m", "raw": {}
    }
    factory = MagicMock(return_value=adapter)

    result = g2.place_missing_stop(
        db, trade_id=55, bracket_intent_id=9, ticker="MSP",
        broker_source="robinhood", decision=_missing_stop_decision(),
        local_quantity=10.0, stop_price=91.0,
        adapter_factory=factory,
    )
    assert result.ok is True
    assert result.new_stop_order_id == "new-stop-m"
    assert result.new_stop_qty == 10.0
    assert result.new_stop_price == 91.0
    adapter.cancel_order.assert_not_called()
    adapter.place_limit_order_gtc.assert_called_once()


# ── Failure modes ──────────────────────────────────────────────────────


def test_resize_cancel_failure_does_not_place(db, monkeypatch):
    """If the cancel of the old stop fails, we must NOT place a new one —
    the position would end up with two working stops."""
    monkeypatch.setattr(g2, "settings", _on_cfg())

    adapter = MagicMock()
    adapter.cancel_order.return_value = {"ok": False, "error": "not_cancellable"}
    factory = MagicMock(return_value=adapter)

    result = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="CFX",
        broker_source="robinhood",
        decision=_partial_fill_decision(5.0),
        prior_stop_order_id="stop-x", stop_price=92.5,
        adapter_factory=factory,
    )
    assert result.ok is False
    assert result.reason == "cancel_failed"
    adapter.place_limit_order_gtc.assert_not_called()


def test_resize_place_failure_after_successful_cancel_logs_critical(
    db, monkeypatch, caplog
):
    """Cancel succeeded, place failed — we're in the dangerous window where
    the position has no stop. This must log CRITICAL for the operator."""
    import logging
    caplog.set_level(logging.CRITICAL, logger="app.services.trading.bracket_writer_g2")
    monkeypatch.setattr(g2, "settings", _on_cfg())

    adapter = MagicMock()
    adapter.cancel_order.return_value = {"ok": True, "raw": {}}
    adapter.place_limit_order_gtc.return_value = {
        "ok": False, "error": "market_closed"
    }
    factory = MagicMock(return_value=adapter)

    result = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="DANGER",
        broker_source="robinhood",
        decision=_partial_fill_decision(5.0),
        prior_stop_order_id="stop-danger", stop_price=92.5,
        adapter_factory=factory,
    )
    assert result.ok is False
    assert result.reason == "place_failed"

    critical_lines = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert any(
        "PRIOR STOP CANCELLED BUT REPLACEMENT FAILED" in r.getMessage()
        for r in critical_lines
    ), f"expected CRITICAL log, got: {[r.getMessage() for r in critical_lines]}"


def test_place_missing_stop_broker_error_returns_place_failed(db, monkeypatch):
    monkeypatch.setattr(g2, "settings", _on_cfg())

    adapter = MagicMock()
    adapter.place_limit_order_gtc.return_value = {
        "ok": False, "error": "insufficient_buying_power"
    }
    factory = MagicMock(return_value=adapter)

    result = g2.place_missing_stop(
        db, trade_id=1, bracket_intent_id=1, ticker="FAIL",
        broker_source="robinhood", decision=_missing_stop_decision(),
        local_quantity=5.0, stop_price=95.0,
        adapter_factory=factory,
    )
    assert result.ok is False
    assert result.reason == "place_failed"
