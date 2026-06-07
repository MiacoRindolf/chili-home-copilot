"""Phase E2: momentum operator readiness is per-venue (robinhood_spot not gated on Coinbase)."""
from __future__ import annotations

import app.services.trading.momentum_neural.operator_readiness as orr


def _patch_common(monkeypatch, *, rh_connected, cb_connected):
    monkeypatch.setattr(
        orr, "get_all_broker_statuses",
        lambda: {"robinhood": {"connected": rh_connected}, "coinbase": {"connected": cb_connected}},
    )
    monkeypatch.setattr(orr, "get_kill_switch_status", lambda: {"active": False})
    monkeypatch.setattr(orr, "mesh_enabled", lambda: True)
    monkeypatch.setattr(orr.settings, "chili_momentum_neural_enabled", True, raising=False)
    monkeypatch.setattr(orr.settings, "chili_momentum_live_runner_enabled", True, raising=False)


def test_robinhood_session_ready_when_rh_connected_even_if_coinbase_down(monkeypatch):
    _patch_common(monkeypatch, rh_connected=True, cb_connected=False)
    monkeypatch.setattr(orr.settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    monkeypatch.setattr(orr.settings, "chili_coinbase_spot_adapter_enabled", False, raising=False)
    rd = orr.build_momentum_operator_readiness(execution_family="robinhood_spot")
    assert rd["execution_family"] == "robinhood_spot"
    assert rd["broker_robinhood_connected"] is True
    assert rd["robinhood_spot_adapter_enabled"] is True
    assert rd["broker_ready_for_live"] is True   # NOT blocked by Coinbase being down
    assert rd["execution_ready"] is True


def test_robinhood_session_blocked_when_rh_adapter_disabled(monkeypatch):
    _patch_common(monkeypatch, rh_connected=True, cb_connected=True)
    monkeypatch.setattr(orr.settings, "chili_robinhood_spot_adapter_enabled", False, raising=False)
    rd = orr.build_momentum_operator_readiness(execution_family="robinhood_spot")
    assert rd["broker_ready_for_live"] is False  # adapter flag off


def test_coinbase_session_still_gated_on_coinbase(monkeypatch):
    _patch_common(monkeypatch, rh_connected=True, cb_connected=False)
    monkeypatch.setattr(orr.settings, "chili_coinbase_spot_adapter_enabled", True, raising=False)
    rd = orr.build_momentum_operator_readiness(execution_family="coinbase_spot")
    # Coinbase down -> not ready, regardless of Robinhood being connected
    assert rd["broker_ready_for_live"] is False


def test_next_action_message_is_venue_aware():
    rd_rh = {"execution_family": "robinhood_spot"}
    msg = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_rh, blocked="broker_not_ready")
    assert "Robinhood" in msg
    rd_cb = {"execution_family": "coinbase_spot"}
    msg2 = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_cb, blocked="broker_not_ready")
    assert "Coinbase" in msg2
