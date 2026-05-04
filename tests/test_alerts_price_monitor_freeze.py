"""broker-truth-self-heal (2026-05-04) -- regression test for the
alerts.run_price_monitor freeze path that replaced the prior
auto-liquidate behavior.

Scenario E: when check_emergency_conditions returns
``recommended_action='emergency_close_all'``, run_price_monitor must
NOT call emergency_close_all. It must instead activate the kill switch
and return ``emergency_action='freeze'``.

Run with ``-p no:asyncio``.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def test_price_monitor_emergency_freezes_does_not_liquidate(db):
    """Scenario E: emergency condition triggers freeze, not liquidation.
    """
    from app.services.trading import alerts as alerts_mod
    from app.services.trading import governance as governance_mod

    # Reset kill switch so we can observe whether it was activated.
    governance_mod.deactivate_kill_switch()

    fake_emergency = {
        "ok": True,
        "drawdown_pct": -25.0,
        "critical_threshold": 20.0,
        "disconnected": True,
        "recommended_action": "emergency_close_all",
        "open_positions": 5,
    }

    with (
        patch(
            "app.services.trading.emergency_liquidation.check_emergency_conditions",
            return_value=fake_emergency,
        ),
        patch(
            "app.services.trading.emergency_liquidation.emergency_close_all",
            side_effect=AssertionError(
                "emergency_close_all must NOT be called from run_price_monitor"
            ),
        ),
    ):
        result = alerts_mod.run_price_monitor(db, user_id=None)

    # Freeze, not liquidate.
    assert result.get("emergency_action") == "freeze"
    assert result.get("emergency_freeze_reason") is not None

    # Kill switch is now active with the freeze reason.
    assert governance_mod.is_kill_switch_active() is True

    # Cleanup: deactivate so subsequent tests aren't poisoned.
    governance_mod.deactivate_kill_switch()


def test_kill_switch_idempotent_on_same_reason(caplog):
    """Bug 3: activate_kill_switch should no-op when called twice with
    the same reason. Same reason -> single CRITICAL log line, not two."""
    import logging
    from app.services.trading import governance as governance_mod

    governance_mod.deactivate_kill_switch()

    with caplog.at_level(logging.CRITICAL, logger="app.services.trading.governance"):
        governance_mod.activate_kill_switch(reason="test_reason")
        governance_mod.activate_kill_switch(reason="test_reason")  # idempotent

    activated = [
        r for r in caplog.records
        if "KILL SWITCH ACTIVATED" in r.getMessage()
    ]
    assert len(activated) == 1, (
        f"expected exactly one CRITICAL log line on same-reason re-arm; got {len(activated)}"
    )

    # A different reason still re-arms.
    caplog.clear()
    with caplog.at_level(logging.CRITICAL, logger="app.services.trading.governance"):
        governance_mod.activate_kill_switch(reason="different_reason")

    activated = [
        r for r in caplog.records
        if "KILL SWITCH ACTIVATED" in r.getMessage()
    ]
    assert len(activated) == 1, (
        "different reason should still emit one CRITICAL line"
    )

    governance_mod.deactivate_kill_switch()
