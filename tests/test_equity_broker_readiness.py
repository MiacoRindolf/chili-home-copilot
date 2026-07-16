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


def test_alpaca_session_ready_via_alpaca_not_coinbase(monkeypatch):
    # Alpaca readiness must use the ALPACA adapter, NOT fall through to the Coinbase
    # branch (which gated alpaca_spot on an unrelated Coinbase status -> always False).
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)  # both brokers down
    monkeypatch.setattr(orr.settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(orr.settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(orr.settings, "chili_coinbase_spot_adapter_enabled", False, raising=False)
    import app.services.trading.venue.alpaca_spot as alp
    monkeypatch.setattr(alp.AlpacaSpotAdapter, "is_enabled", lambda self: True)
    rd = orr.build_momentum_operator_readiness(execution_family="alpaca_spot")
    assert rd["execution_family"] == "alpaca_spot"
    assert rd["alpaca_spot_adapter_enabled"] is True
    assert rd["broker_alpaca_ready"] is True
    assert rd["broker_ready_for_live"] is True   # NOT gated on Coinbase being down
    assert rd["execution_ready"] is True


def test_alpaca_session_blocked_when_adapter_disabled(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=True)
    monkeypatch.setattr(orr.settings, "chili_alpaca_enabled", False, raising=False)
    rd = orr.build_momentum_operator_readiness(execution_family="alpaca_spot")
    assert rd["broker_ready_for_live"] is False  # adapter flag off


def test_alpaca_live_posture_is_quarantined_without_adapter_probe(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(orr.settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(orr.settings, "chili_alpaca_paper", False, raising=False)
    import app.services.trading.venue.alpaca_spot as alp

    probes = []
    monkeypatch.setattr(
        alp.AlpacaSpotAdapter,
        "is_enabled",
        lambda self: probes.append("adapter") or True,
    )
    rd = orr.build_momentum_operator_readiness(
        execution_family="alpaca_spot",
        symbol="ACTU",
    )
    assert probes == []
    assert rd["execution_quarantine_reason"] == "alpaca_live_posture_not_certified"
    assert rd["broker_ready_for_live"] is False
    assert rd["execution_ready"] is False
    assert rd["runnable_live_now"] is False
    assert (
        orr.blocked_reason_for_session(
            mode="live",
            readiness=rd,
            canonical_state="queued_live",
        )
        == "alpaca_live_posture_not_certified"
    )


def test_alpaca_short_and_crypto_readiness_are_quarantined(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(orr.settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(orr.settings, "chili_alpaca_paper", True, raising=False)

    short = orr.build_momentum_operator_readiness(
        execution_family="alpaca_short",
        symbol="ACTU",
    )
    crypto = orr.build_momentum_operator_readiness(
        execution_family="alpaca_spot",
        symbol="BTC-USD",
    )
    slash_crypto = orr.build_momentum_operator_readiness(
        execution_family="alpaca_spot",
        symbol="BTC/USD",
    )
    explicit_crypto = orr.build_momentum_operator_readiness(
        execution_family="alpaca_spot",
        symbol="ACTU",
        asset_class="crypto",
    )
    assert short["execution_quarantine_reason"] == "alpaca_short_execution_not_certified"
    assert crypto["execution_quarantine_reason"] == "alpaca_crypto_execution_not_certified"
    assert slash_crypto["execution_quarantine_reason"] == "alpaca_crypto_execution_not_certified"
    assert explicit_crypto["execution_quarantine_reason"] == "alpaca_crypto_execution_not_certified"
    assert short["broker_ready_for_live"] is False
    assert crypto["broker_ready_for_live"] is False
    assert slash_crypto["broker_ready_for_live"] is False
    assert explicit_crypto["broker_ready_for_live"] is False


def test_next_action_message_is_venue_aware():
    rd_rh = {"execution_family": "robinhood_spot"}
    msg = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_rh, blocked="broker_not_ready")
    assert "Robinhood" in msg
    rd_cb = {"execution_family": "coinbase_spot"}
    msg2 = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_cb, blocked="broker_not_ready")
    assert "Coinbase" in msg2
    rd_alp = {"execution_family": "alpaca_spot"}
    msg3 = orr.next_action_required(mode="live", state="x", canonical_state="queued_live", readiness=rd_alp, blocked="broker_not_ready")
    assert "Alpaca" in msg3


def test_external_event_loop_config_is_valid_but_runtime_health_is_unverified(
    monkeypatch,
):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_scheduler_enabled", False,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_loop_enabled", True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_autopilot_price_bus_enabled", True, raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_scheduler_runs_externally", True, raising=False,
    )
    monkeypatch.setattr(orr.settings, "chili_scheduler_role", "none", raising=False)

    rd = orr.build_momentum_operator_readiness(execution_family="coinbase_spot")
    rd["broker_ready_for_live"] = True
    blocked = orr.blocked_reason_for_session(
        mode="live",
        readiness=rd,
        canonical_state="queued_live",
    )
    cta = orr.next_action_required(
        mode="live",
        state="queued_live",
        canonical_state="queued_live",
        readiness=rd,
        blocked=blocked,
    )

    assert rd["live_driver_mode"] == "event_loop"
    assert rd["live_driver_config_valid"] is True
    assert rd["live_driver_runtime_state"] == "unknown_external"
    assert rd["live_driver_would_run"] is False
    assert rd["live_scheduler_would_run"] is False
    assert rd["runnable_live_now"] is False
    assert blocked == "live_event_loop_health_unverified"
    assert "heartbeat/owner fence" in cta.lower()
    assert "legacy live batch disabled" in cta.lower()


def test_external_scheduler_process_requires_its_actual_local_loop(monkeypatch):
    from app.services.trading.momentum_neural import live_runner_loop

    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(
        orr.settings,
        "chili_momentum_live_runner_scheduler_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings,
        "chili_momentum_live_runner_loop_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings,
        "chili_autopilot_price_bus_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings,
        "chili_scheduler_runs_externally",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings,
        "chili_scheduler_role",
        "momentum_exec_only",
        raising=False,
    )
    monkeypatch.setattr(
        live_runner_loop,
        "is_live_runner_loop_running",
        lambda: True,
    )

    rd = orr.build_momentum_operator_readiness(execution_family="coinbase_spot")

    assert rd["live_event_loop_process_signal_available"] is True
    assert rd["live_event_loop_running"] is True
    assert rd["live_driver_runtime_state"] == "running"
    assert rd["live_driver_would_run"] is True


def test_invalid_dual_live_driver_is_blocked_without_batch_enable_cta(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_scheduler_enabled", True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_loop_enabled", True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_autopilot_price_bus_enabled", True, raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_scheduler_runs_externally", True, raising=False,
    )
    rd = orr.build_momentum_operator_readiness(execution_family="coinbase_spot")
    rd["broker_ready_for_live"] = True

    blocked = orr.blocked_reason_for_session(
        mode="live", readiness=rd, canonical_state="queued_live"
    )
    cta = orr.next_action_required(
        mode="live",
        state="queued_live",
        canonical_state="queued_live",
        readiness=rd,
        blocked=blocked,
    )

    assert blocked == "live_driver_misconfigured"
    assert "exactly one" in cta.lower()
    assert "never both" in cta.lower()


def test_loop_without_price_bus_cta_keeps_legacy_batch_disabled(monkeypatch):
    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_scheduler_enabled", False,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_loop_enabled", True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_autopilot_price_bus_enabled", False, raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_scheduler_runs_externally", True, raising=False,
    )
    rd = orr.build_momentum_operator_readiness(execution_family="coinbase_spot")
    rd["broker_ready_for_live"] = True
    blocked = orr.blocked_reason_for_session(
        mode="live", readiness=rd, canonical_state="queued_live"
    )

    cta = orr.next_action_required(
        mode="live",
        state="queued_live",
        canonical_state="queued_live",
        readiness=rd,
        blocked=blocked,
    )

    assert blocked == "live_driver_misconfigured"
    assert "price bus" in cta.lower()
    assert "legacy live batch disabled" in cta.lower()


def test_local_event_loop_refusal_is_not_reported_as_scheduler_batch_gap(
    monkeypatch,
):
    from app.services.trading.momentum_neural import live_runner_loop

    _patch_common(monkeypatch, rh_connected=False, cb_connected=False)
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_scheduler_enabled", False,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_momentum_live_runner_loop_enabled", True,
        raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_autopilot_price_bus_enabled", True, raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_scheduler_runs_externally", False, raising=False,
    )
    monkeypatch.setattr(
        orr.settings, "chili_scheduler_role", "momentum_exec_only", raising=False,
    )
    monkeypatch.setattr(live_runner_loop, "is_live_runner_loop_running", lambda: False)
    rd = orr.build_momentum_operator_readiness(execution_family="coinbase_spot")
    rd["broker_ready_for_live"] = True

    blocked = orr.blocked_reason_for_session(
        mode="live", readiness=rd, canonical_state="queued_live"
    )
    cta = orr.next_action_required(
        mode="live",
        state="queued_live",
        canonical_state="queued_live",
        readiness=rd,
        blocked=blocked,
    )

    assert blocked == "live_event_loop_not_running"
    assert "event-loop owner" in cta.lower()
    assert "legacy live batch disabled" in cta.lower()
