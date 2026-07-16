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


def test_agentic_session_ready_from_agentic_adapter_not_coinbase_or_legacy_rh(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(
        orr,
        "_agentic_mcp_adapter_status",
        lambda: {"enabled": True, "reason": "agentic_adapter_enabled"},
    )

    rd = orr.build_momentum_operator_readiness(execution_family="robinhood_agentic_mcp")

    assert rd["execution_family"] == "robinhood_agentic_mcp"
    assert rd["robinhood_agentic_mcp_adapter_enabled"] is True
    assert rd["robinhood_agentic_mcp_adapter_reason"] == "agentic_adapter_enabled"
    assert rd["broker_robinhood_agentic_connected"] is True
    assert rd["broker_ready_for_live"] is True
    assert rd["execution_ready"] is True


def test_default_readiness_uses_configured_equity_rail_not_coinbase(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(orr.settings, "chili_equity_execution_rail", "robinhood_agentic_mcp", raising=False)
    monkeypatch.setattr(
        orr,
        "_agentic_mcp_adapter_status",
        lambda: {"enabled": True, "reason": "agentic_adapter_enabled"},
    )

    rd = orr.build_momentum_operator_readiness()

    assert rd["execution_family"] == "robinhood_agentic_mcp"
    assert rd["broker_ready_for_live"] is True
    assert rd["execution_ready"] is True


def test_default_readiness_keeps_crypto_symbols_on_coinbase(monkeypatch):
    _patch_common(monkeypatch, rh_connected=True, cb_connected=False)
    monkeypatch.setattr(orr.settings, "chili_equity_execution_rail", "robinhood_agentic_mcp", raising=False)
    monkeypatch.setattr(orr.settings, "chili_coinbase_spot_adapter_enabled", True, raising=False)

    rd = orr.build_momentum_operator_readiness(symbol="BTC-USD")

    assert rd["execution_family"] == "coinbase_spot"
    assert rd["broker_ready_for_live"] is False


def test_agentic_session_blocked_when_agentic_auth_disabled(monkeypatch):
    _patch_common(monkeypatch, rh_connected=True, cb_connected=True)
    monkeypatch.setattr(
        orr,
        "_agentic_mcp_adapter_status",
        lambda: {
            "enabled": False,
            "reason": "execution_auth:needs_reauth",
            "token_present": True,
            "token_bundle_present": True,
            "token_bundle_routable": False,
        },
    )

    rd = orr.build_momentum_operator_readiness(execution_family="robinhood_agentic_mcp")

    assert rd["broker_ready_for_live"] is False
    assert rd["execution_ready"] is False
    assert rd["robinhood_agentic_mcp_adapter_reason"] == "execution_auth:needs_reauth"
    assert rd["robinhood_agentic_mcp_token_present"] is True
    assert rd["robinhood_agentic_mcp_token_bundle_present"] is True
    assert rd["robinhood_agentic_mcp_token_bundle_routable"] is False


def test_agentic_adapter_status_reports_no_token_without_secret_detail(monkeypatch):
    class FakeClient:
        def has_token(self):
            return False

    class FakeAdapter:
        def _get_client(self):
            return FakeClient()

        def is_enabled(self):
            return False

    monkeypatch.setattr(
        orr,
        "resolve_live_spot_adapter_factory",
        lambda execution_family: lambda: FakeAdapter(),
    )
    monkeypatch.setattr(
        orr,
        "_agentic_mcp_token_bundle_status",
        lambda: {
            "token_present": False,
            "token_bundle_present": False,
            "token_bundle_routable": False,
        },
    )

    status = orr._agentic_mcp_adapter_status()

    assert status["enabled"] is False
    assert status["reason"] == "no_token"
    assert "token" not in str(status).lower() or "secret" not in str(status).lower()


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
    rd_agentic = {"execution_family": "robinhood_agentic_mcp"}
    msg_agentic = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_agentic, blocked="broker_not_ready")
    assert "Agentic" in msg_agentic
    rd_cb = {"execution_family": "coinbase_spot"}
    msg2 = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_cb, blocked="broker_not_ready")
    assert "Coinbase" in msg2
