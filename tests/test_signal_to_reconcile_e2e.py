"""End-to-end integration test: signal → fill → bracket intent → reconcile.

Closes the biggest test-floor gap identified in the Phase 2 audit:
previously each link was unit-tested but no test verified that the chain
holds together. A partial fill at Robinhood, for example, could easily
leave the bracket intent at the wrong size with no single test catching
the mismatch because the reconciler was always tested against hand-rolled
BrokerView fixtures that happened to match the local view.

Each scenario walks the full chain:
  1. BreakoutAlert row inserted (pattern_imminent, scoped to the test user).
  2. ``run_auto_trader_tick`` executes in live-mode, with broker_service
     mocked one layer down so the venue adapter's idempotency + state
     machine still fire.
  3. The AutoTraderRun audit row + Trade row + broker_order_id are
     asserted.
  4. ``upsert_bracket_intent`` writes the intent (this is what
     stop_engine would do after the entry fill on prod).
  5. ``run_reconciliation_sweep`` runs in shadow mode against an
     injectable broker view that we can parametrize per scenario.
  6. The reconciliation log row + intent state are asserted.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app import models
from app.models.trading import AutoTraderRun, BreakoutAlert, Trade
from app.services.trading import auto_trader as at_mod
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent
from app.services.trading.bracket_reconciler import BrokerView
from app.services.trading.bracket_reconciliation_service import (
    run_reconciliation_sweep,
)
from app.services.trading.venue import idempotency_store


@pytest.fixture(autouse=True)
def _reset_idempotency_between_tests(db):
    """The idempotency store's in-memory cache persists across tests in a
    single pytest process, and its DB table (``venue_order_idempotency``)
    isn't in the SQLAlchemy metadata so conftest's TRUNCATE skips it. Each
    test reuses ``alert_id=1`` (TRUNCATE resets sequences) so the
    client_order_id ``atv1-1-buy`` would collide without this reset.
    """
    idempotency_store.reset_for_tests()
    db.execute(text("DELETE FROM venue_order_idempotency"))
    db.commit()
    yield
    idempotency_store.reset_for_tests()
    db.execute(text("DELETE FROM venue_order_idempotency"))
    db.commit()


def _autotrader_cfg(user_id: int) -> SimpleNamespace:
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
        chili_robinhood_spot_adapter_enabled=True,
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


def _insert_alert(db, *, user_id: int, ticker: str) -> BreakoutAlert:
    alert = BreakoutAlert(
        ticker=ticker,
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=47.0,
        target_price=60.0,
        user_id=user_id,
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def _shadow_mode(monkeypatch) -> None:
    """Flip both writer + reconciler into shadow mode so intents + logs land."""
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
        "shadow",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
        "shadow",
        raising=False,
    )


def _run_live_autotrader(db, user_id: int, ticker: str, monkeypatch, broker_result: dict[str, Any]):
    """Fire one autotrader tick in live mode, with broker_service mocked.

    The adapter-level idempotency + state-machine writes are NOT mocked —
    the test exercises them for real, which is the point of the E2E.
    """
    monkeypatch.setattr(at_mod, "settings", _autotrader_cfg(user_id))
    monkeypatch.setattr(at_mod, "effective_autotrader_runtime", lambda _db: _live_runtime())

    # Short-circuit gates that need production infra (market data, LLM,
    # portfolio-risk DB views) — they're covered by their own unit tests.
    monkeypatch.setattr(at_mod, "_current_price", lambda _t: 50.0)
    monkeypatch.setattr(at_mod, "count_autotrader_v1_open", lambda *a, **k: 0)
    monkeypatch.setattr(at_mod, "autotrader_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "autotrader_paper_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "find_open_autotrader_trade", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "find_open_autotrader_paper", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "maybe_scale_in", lambda *a, **k: None)
    monkeypatch.setattr(
        at_mod, "passes_rule_gate",
        lambda *a, **k: (True, "ok", {"projected_profit_pct": 20.0}),
    )
    from app.services.trading import autopilot_scope
    monkeypatch.setattr(
        autopilot_scope, "check_autopilot_entry_gate",
        lambda *a, **k: {"allowed": True, "reason": "test"},
    )
    from app.services.trading.venue import venue_health
    monkeypatch.setattr(venue_health, "is_venue_degraded", lambda *a, **k: False)

    # The adapter calls broker_service.place_buy_order + is_connected.
    # Mocking just those makes the rest of the adapter path (idempotency
    # store, state-machine transitions, venue_health records) run for real.
    with patch("app.services.broker_service.is_connected", return_value=True), \
         patch("app.services.broker_service.place_buy_order", return_value=broker_result):
        return at_mod.run_auto_trader_tick(db)


def _assert_trade_placed(db, alert_id: int, *, expected_broker_oid: str) -> Trade:
    run = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert_id)
        .filter(AutoTraderRun.decision == "placed")
        .first()
    )
    assert run is not None, f"no placed AutoTraderRun for alert={alert_id}"
    assert run.trade_id is not None
    tr = db.query(Trade).filter(Trade.id == run.trade_id).first()
    assert tr is not None
    assert tr.broker_order_id == expected_broker_oid
    assert tr.status == "open"
    return tr


def _upsert_intent_for_trade(db, trade: Trade) -> int:
    """Simulate the stop_engine post-entry call that writes the bracket
    intent. In production this is scheduled separately; the reconciler's
    input contract is the Trade + BracketIntent pair."""
    res = upsert_bracket_intent(
        db,
        trade_id=trade.id,
        user_id=trade.user_id,
        bracket_input=BracketIntentInput(
            ticker=trade.ticker,
            direction=trade.direction,
            entry_price=trade.entry_price,
            quantity=trade.quantity,
            atr=1.0,
            stop_model="atr_swing",
            lifecycle_stage="validated",
            regime="cautious",
        ),
        broker_source=trade.broker_source,
    )
    assert res is not None
    return res.intent_id


def test_e2e_alert_to_reconcile_agree_no_intent(db, monkeypatch):
    """Happy path: alert → live fill → Trade exists with NO bracket intent →
    broker shows matching position → classification is ``agree``.

    This is the common post-fill state before stop_engine has had a chance
    to write the intent; the reconciler must not flag it as missing_stop
    when there's nothing to be missing yet.
    """
    _shadow_mode(monkeypatch)
    u = models.User(name="e2e_agree_u")
    db.add(u)
    db.flush()

    alert = _insert_alert(db, user_id=u.id, ticker="E2EA")
    out = _run_live_autotrader(
        db, u.id, "E2EA", monkeypatch,
        broker_result={"ok": True, "order_id": "rh-e2e-agree-1", "raw": {"average_price": 50.0}},
    )
    assert out["placed"] == 1

    tr = _assert_trade_placed(db, alert.id, expected_broker_oid="rh-e2e-agree-1")

    # Broker sees the position with matching qty; no stop/target (Phase G
    # doesn't wire server-side brackets).
    def broker_fn(rows):
        return [BrokerView(
            available=True,
            ticker=tr.ticker.upper(),
            broker_source="robinhood",
            position_quantity=float(tr.quantity),
        )]

    summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
    assert summary.trades_scanned == 1
    # No bracket intent yet → classifier takes the agree path since qty
    # matches and there's no missing-stop trigger (missing_stop requires
    # has_local_intent).
    assert summary.agree == 1, f"expected agree, got {summary.to_dict()}"


def test_e2e_alert_to_reconcile_missing_stop_with_intent(db, monkeypatch):
    """Alert → fill → bracket intent written → broker shows position but
    no server-side stop → classification is ``missing_stop`` (the expected
    Phase G outcome, since G never places a server-side stop).

    Verifies the intent writer + reconciler cooperate so the watchdog can
    later alert on stale missing_stop rows."""
    _shadow_mode(monkeypatch)
    u = models.User(name="e2e_missing_u")
    db.add(u)
    db.flush()

    alert = _insert_alert(db, user_id=u.id, ticker="E2EMS")
    _run_live_autotrader(
        db, u.id, "E2EMS", monkeypatch,
        broker_result={"ok": True, "order_id": "rh-e2e-ms-1", "raw": {"average_price": 50.0}},
    )
    tr = _assert_trade_placed(db, alert.id, expected_broker_oid="rh-e2e-ms-1")

    intent_id = _upsert_intent_for_trade(db, tr)

    def broker_fn(rows):
        return [BrokerView(
            available=True,
            ticker=tr.ticker.upper(),
            broker_source="robinhood",
            position_quantity=float(tr.quantity),
            # Deliberately NO stop_order_* fields.
        )]

    summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
    assert summary.missing_stop == 1, f"expected missing_stop, got {summary.to_dict()}"

    # The reconciler bumps last_observed_at even on non-agree classifications.
    obs_age = db.execute(text("""
        SELECT last_observed_at IS NOT NULL, last_diff_reason
        FROM trading_bracket_intents WHERE id = :id
    """), {"id": intent_id}).fetchone()
    assert obs_age[0] is True
    assert obs_age[1] and "missing_stop" in obs_age[1]


def test_e2e_partial_fill_produces_qty_drift(db, monkeypatch):
    """Alert → fill → broker reports HALF the intended quantity → the
    reconciler must classify as qty_drift with is_partial_fill=True and
    expected_stop_qty equal to the broker's actual quantity.

    This is the scenario that motivates Phase G.2: a partial fill at the
    broker while the local Trade thinks it got the full qty leaves the
    position under-hedged unless the stop is resized to broker_qty.
    """
    _shadow_mode(monkeypatch)
    u = models.User(name="e2e_partial_u")
    db.add(u)
    db.flush()

    alert = _insert_alert(db, user_id=u.id, ticker="E2EPF")
    _run_live_autotrader(
        db, u.id, "E2EPF", monkeypatch,
        broker_result={"ok": True, "order_id": "rh-e2e-pf-1", "raw": {"average_price": 50.0}},
    )
    tr = _assert_trade_placed(db, alert.id, expected_broker_oid="rh-e2e-pf-1")
    _upsert_intent_for_trade(db, tr)

    partial_qty = float(tr.quantity) / 2.0

    # Broker must report a working stop for classifier to look past the
    # missing_stop branch and reach qty_drift. This is the Phase G.2
    # topology (server-side stop wired); Phase G would collapse to
    # missing_stop regardless of qty because no stop exists at the broker.
    def broker_fn(rows):
        return [BrokerView(
            available=True,
            ticker=tr.ticker.upper(),
            broker_source="robinhood",
            position_quantity=partial_qty,
            stop_order_id="stop-pf-1",
            stop_order_state="open",
            stop_order_price=47.0,
        )]

    summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
    assert summary.qty_drift == 1, f"expected qty_drift, got {summary.to_dict()}"

    # Verify the log row carries the partial-fill payload that Phase G.2
    # will consume to resize the stop.
    row = db.execute(text("""
        SELECT kind, delta_payload
        FROM trading_bracket_reconciliation_log
        WHERE sweep_id = :sid
    """), {"sid": summary.sweep_id}).fetchone()
    assert row is not None
    kind, payload = row[0], row[1]
    assert kind == "qty_drift"
    assert payload["is_partial_fill"] is True
    assert payload["drift_kind"] == "partial_fill"
    assert abs(payload["expected_stop_qty"] - partial_qty) < 1e-6
    assert abs(payload["broker_qty"] - partial_qty) < 1e-6
    assert abs(payload["local_qty"] - float(tr.quantity)) < 1e-6


def test_e2e_broker_rejection_produces_no_trade_and_audit_row(db, monkeypatch):
    """If the broker rejects the order outright, NO Trade row should be
    written and the audit row must capture the broker error. The reconciler
    has nothing to scan. This completes the negative-path coverage."""
    _shadow_mode(monkeypatch)
    u = models.User(name="e2e_reject_u")
    db.add(u)
    db.flush()

    alert = _insert_alert(db, user_id=u.id, ticker="E2ERJ")
    out = _run_live_autotrader(
        db, u.id, "E2ERJ", monkeypatch,
        broker_result={"ok": False, "error": "insufficient_buying_power"},
    )
    assert out["placed"] == 0
    assert out["skipped"] == 1

    # No Trade row.
    assert db.query(Trade).filter(Trade.ticker == "E2ERJ").count() == 0

    # Audit row captures the broker error.
    run = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id)
        .first()
    )
    assert run is not None
    assert run.decision == "blocked"
    assert run.reason and "insufficient_buying_power" in run.reason

    # Reconciler has nothing to scan for this ticker.
    def broker_fn(rows):
        return []

    summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
    # Our alert created no trade; unrelated trades from other tests in this
    # DB might exist but the key assertion is: no trade_id tied to this
    # alert appears in the sweep's decisions.
    sweep_trade_ids = {d.get("trade_id") for d in summary.decisions}
    assert not any(
        db.query(Trade).filter(Trade.id == tid, Trade.ticker == "E2ERJ").first()
        for tid in sweep_trade_ids if tid is not None
    )
