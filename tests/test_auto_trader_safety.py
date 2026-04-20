"""Phase 1 safety hardening tests for the AutoTrader v1 live path.

Covers:
  * P0.2 — advisory-lock claim prevents a same-tick duplicate submission
    when an alert is observed twice. (Race between two ticks is hard to
    exercise deterministically in one process; we validate the primitive.)
  * P0.5 — kill switch flipped mid-flight (after initial gate, before
    placement) is honoured — no broker call is made and the audit reason
    is recorded.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import models
from app.models.trading import AutoTraderRun, BreakoutAlert
from app.services.trading import auto_trader as at_mod


def _minimal_settings(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=True,
        chili_autotrader_user_id=user_id,
        brain_default_user_id=user_id,
        chili_autotrader_llm_revalidation_enabled=False,
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=12.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_max_concurrent=5,
        chili_autotrader_per_trade_notional_usd=300.0,
        chili_autotrader_synergy_enabled=False,
        chili_autotrader_synergy_scale_notional_usd=150.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_feature_parity_enabled=False,
    )


def _live_runtime() -> dict:
    return {
        "tick_allowed": True,
        "paused": False,
        "live_orders_effective": True,
        "live_orders_env": True,
        "desk_live_override": False,
        "monitor_entries_allowed": True,
        "payload": {},
    }


def test_try_claim_alert_same_session_releases_cleanly(db):
    """The advisory lock is the primitive behind the TOCTOU fix. Acquire,
    release, then re-acquire in the same session to prove it's not sticky."""
    pytest.importorskip("sqlalchemy")
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("advisory locks are Postgres-only")

    alert_id = 987654321
    assert at_mod._try_claim_alert(db, alert_id) is True
    at_mod._release_alert_claim(db, alert_id)

    # After release, another acquire in the same session must still succeed.
    assert at_mod._try_claim_alert(db, alert_id) is True
    at_mod._release_alert_claim(db, alert_id)


def test_try_claim_alert_second_session_blocks_while_held(db):
    """While one session holds the lock, a second session's try-acquire
    must return False. This is what prevents two concurrent AutoTrader
    ticks from both passing the gate for the same alert."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("advisory locks are Postgres-only")

    from sqlalchemy.orm import sessionmaker

    alert_id = 123456789
    assert at_mod._try_claim_alert(db, alert_id) is True
    try:
        # Open a second independent session against the same engine.
        Session2 = sessionmaker(bind=db.bind)
        other = Session2()
        try:
            got_from_other = at_mod._try_claim_alert(other, alert_id)
            assert got_from_other is False, (
                "second session should not be able to claim while the first holds the lock"
            )
        finally:
            other.close()
    finally:
        at_mod._release_alert_claim(db, alert_id)


def test_kill_switch_flipped_mid_flight_blocks_placement(db, monkeypatch):
    """P0.5 — the kill switch is re-checked right before broker submission.
    Setup: kill switch starts off (tick-entry check passes), we patch
    is_kill_switch_active to start returning True only at the moment
    _execute_new_entry is about to call place_market_order."""
    u = models.User(name="kill_midflight_u")
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="KSWT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=48.0,
        target_price=55.0,
        user_id=u.id,
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    monkeypatch.setattr(at_mod, "settings", _minimal_settings(u.id))
    monkeypatch.setattr(at_mod, "effective_autotrader_runtime", lambda _db: _live_runtime())

    from app.services.trading import governance as gov
    # Tick-entry check (in auto_trader.run_auto_trader_tick) uses the module
    # import; _execute_new_entry imports it locally, which means
    # monkeypatching at_mod's reference is not enough — patch the source
    # module directly. Starts False, flips True on the 2nd call.
    calls = {"n": 0}

    def _flipping():
        calls["n"] += 1
        return calls["n"] >= 2  # pass the tick gate, fail the placement re-check

    monkeypatch.setattr(gov, "is_kill_switch_active", _flipping)

    # Stub out everything expensive between gates and placement so the
    # second is_kill_switch_active call IS the placement re-check.
    monkeypatch.setattr(at_mod, "_current_price", lambda _t: 50.0)
    monkeypatch.setattr(at_mod, "count_autotrader_v1_open", lambda *a, **k: 0)
    monkeypatch.setattr(at_mod, "autotrader_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "autotrader_paper_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "find_open_autotrader_trade", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "find_open_autotrader_paper", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "maybe_scale_in", lambda *a, **k: None)
    monkeypatch.setattr(
        at_mod, "passes_rule_gate",
        lambda *a, **k: (True, "ok", {"projected_profit_pct": 10.0}),
    )

    from app.services.trading import autopilot_scope
    monkeypatch.setattr(
        autopilot_scope, "check_autopilot_entry_gate",
        lambda *a, **k: {"allowed": True, "reason": "test"},
    )

    from app.services.trading.venue import venue_health
    monkeypatch.setattr(venue_health, "is_venue_degraded", lambda *a, **k: False)

    # If placement ever got called, the test fails — it means the
    # mid-flight kill switch re-check was bypassed.
    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.side_effect = AssertionError(
        "broker call should have been blocked by mid-flight kill switch"
    )
    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ):
        out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    # The flipping check returned True before placement; expected outcome
    # is that _execute_new_entry wrote a 'blocked' audit row.
    fake_adapter.place_market_order.assert_not_called()

    runs = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).all()
    assert runs, "expected an audit row for this alert"
    reasons = {r.reason for r in runs}
    assert any(
        r and "kill_switch_activated_mid_flight" in r for r in reasons
    ), f"expected mid-flight kill-switch reason, got: {reasons}"
