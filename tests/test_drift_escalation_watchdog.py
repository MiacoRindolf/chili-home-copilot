"""Tests for the P0.8 drift escalation watchdog.

Pre-populates ``trading_bracket_reconciliation_log`` with consecutive-row
scenarios and verifies the watchdog detects streaks correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text

from app import models
from app.models.trading import Trade
from app.services.trading import drift_escalation_watchdog as dew
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent


def _make_trade_and_intent(db, *, user_id, ticker):
    t = Trade(
        user_id=user_id, ticker=ticker, direction="long", entry_price=100.0,
        quantity=5.0, status="open", broker_source="robinhood",
        broker_order_id=f"dew-{ticker}",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    res = upsert_bracket_intent(
        db, trade_id=t.id, user_id=user_id,
        bracket_input=BracketIntentInput(
            ticker=ticker, direction="long", entry_price=100.0, quantity=5.0,
            atr=2.0, stop_model="atr_swing", lifecycle_stage="validated",
            regime="cautious",
        ),
        broker_source="robinhood", mode_override="shadow",
    )
    assert res is not None
    return t, res.intent_id


def _write_log_row(db, *, sweep_id, trade_id, intent_id, ticker, kind, observed_at):
    db.execute(
        text(
            """
            INSERT INTO trading_bracket_reconciliation_log
                (sweep_id, trade_id, bracket_intent_id, ticker, broker_source,
                 kind, severity, local_payload, broker_payload, delta_payload,
                 mode, observed_at)
            VALUES
                (:sweep, :tid, :iid, :ticker, 'robinhood',
                 :kind, 'warn',
                 CAST('{}' AS JSONB), CAST('{}' AS JSONB), CAST('{}' AS JSONB),
                 'shadow', :obs)
            """
        ),
        {
            "sweep": sweep_id, "tid": trade_id, "iid": intent_id,
            "ticker": ticker, "kind": kind, "obs": observed_at,
        },
    )


def test_disabled_flag_returns_empty(db, monkeypatch):
    cfg = SimpleNamespace(chili_drift_escalation_enabled=False)
    monkeypatch.setattr(dew, "settings", cfg)
    summary = dew.run_drift_escalation_watchdog(db)
    assert summary.enabled is False
    assert summary.hits == []


def test_streak_below_min_count_no_hit(db):
    """3 consecutive qty_drift rows with a min_count of 5 — no alert."""
    u = models.User(name="dew_below_u")
    db.add(u); db.flush()
    _, iid = _make_trade_and_intent(db, user_id=u.id, ticker="DEWBELOW")

    now = datetime.utcnow()
    for i in range(3):
        _write_log_row(
            db, sweep_id=f"sw-{i}", trade_id=_.id if hasattr(_, "id") else None,
            intent_id=iid, ticker="DEWBELOW", kind="qty_drift",
            observed_at=now - timedelta(minutes=3 - i),
        )
    db.commit()

    dispatched = []
    def _dispatch(**kw):
        dispatched.append(kw)
        return True

    summary = dew.run_drift_escalation_watchdog(
        db,
        enabled_override=True,
        min_count_override=5,
        lookback_minutes_override=60,
        alert_dispatcher=_dispatch,
    )
    assert summary.hits == []
    assert dispatched == []


def test_streak_at_threshold_fires_alert(db):
    """5 consecutive qty_drift rows with min_count=5 — alert fires once."""
    u = models.User(name="dew_at_u")
    db.add(u); db.flush()
    t, iid = _make_trade_and_intent(db, user_id=u.id, ticker="DEWATTHR")

    now = datetime.utcnow()
    for i in range(5):
        _write_log_row(
            db, sweep_id=f"sw-at-{i}", trade_id=t.id, intent_id=iid,
            ticker="DEWATTHR", kind="qty_drift",
            observed_at=now - timedelta(minutes=5 - i),
        )
    db.commit()

    dispatched = []
    def _dispatch(**kw):
        dispatched.append(kw)
        return True

    summary = dew.run_drift_escalation_watchdog(
        db, enabled_override=True, min_count_override=5,
        lookback_minutes_override=60, alert_dispatcher=_dispatch,
    )

    assert len(summary.hits) == 1
    hit = summary.hits[0]
    assert hit.bracket_intent_id == iid
    assert hit.kind == "qty_drift"
    assert hit.consecutive_count == 5
    assert hit.alert_sent is True
    assert len(dispatched) == 1
    assert "drift_escalation_qty_drift" in dispatched[0]["alert_type"]


def test_broken_streak_does_not_escalate(db):
    """4 qty_drift, then 1 agree, then 4 more qty_drift — the leading
    streak is only 4 (breaks at 'agree'), so no alert fires."""
    u = models.User(name="dew_broken_u")
    db.add(u); db.flush()
    t, iid = _make_trade_and_intent(db, user_id=u.id, ticker="DEWBRK")

    now = datetime.utcnow()
    kinds = ["qty_drift"] * 4 + ["agree"] + ["qty_drift"] * 4
    for i, kind in enumerate(kinds):
        _write_log_row(
            db, sweep_id=f"sw-br-{i}", trade_id=t.id, intent_id=iid,
            ticker="DEWBRK", kind=kind,
            observed_at=now - timedelta(minutes=len(kinds) - i),
        )
    db.commit()

    dispatched = []
    def _dispatch(**kw):
        dispatched.append(kw)
        return True

    summary = dew.run_drift_escalation_watchdog(
        db, enabled_override=True, min_count_override=5,
        lookback_minutes_override=60, alert_dispatcher=_dispatch,
    )
    # Leading streak (newest first): qty_drift, qty_drift, qty_drift,
    # qty_drift — then 'agree' breaks it at length 4. Below min_count=5.
    assert summary.hits == []
    assert dispatched == []


def test_only_escalatable_kinds_trigger_alerts(db):
    """A streak of 'agree' rows must never escalate, no matter how long."""
    u = models.User(name="dew_agree_u")
    db.add(u); db.flush()
    t, iid = _make_trade_and_intent(db, user_id=u.id, ticker="DEWAGREE")

    now = datetime.utcnow()
    for i in range(10):
        _write_log_row(
            db, sweep_id=f"sw-ag-{i}", trade_id=t.id, intent_id=iid,
            ticker="DEWAGREE", kind="agree",
            observed_at=now - timedelta(minutes=10 - i),
        )
    db.commit()

    dispatched = []
    def _dispatch(**kw):
        dispatched.append(kw)
        return True

    summary = dew.run_drift_escalation_watchdog(
        db, enabled_override=True, min_count_override=5,
        lookback_minutes_override=60, alert_dispatcher=_dispatch,
    )
    assert summary.hits == []
    assert dispatched == []


def test_two_intents_independent_streaks(db):
    """Two different intents — one escalates, one doesn't. The watchdog
    must handle them independently."""
    u = models.User(name="dew_two_u")
    db.add(u); db.flush()
    t_hit, iid_hit = _make_trade_and_intent(db, user_id=u.id, ticker="DEWHITX")
    t_miss, iid_miss = _make_trade_and_intent(db, user_id=u.id, ticker="DEWMISX")

    now = datetime.utcnow()
    # 5x qty_drift on HITX; 2x missing_stop on MISX.
    for i in range(5):
        _write_log_row(
            db, sweep_id=f"sw-hit-{i}", trade_id=t_hit.id, intent_id=iid_hit,
            ticker="DEWHITX", kind="qty_drift",
            observed_at=now - timedelta(minutes=5 - i),
        )
    for i in range(2):
        _write_log_row(
            db, sweep_id=f"sw-mis-{i}", trade_id=t_miss.id, intent_id=iid_miss,
            ticker="DEWMISX", kind="missing_stop",
            observed_at=now - timedelta(minutes=2 - i),
        )
    db.commit()

    dispatched = []
    def _dispatch(**kw):
        dispatched.append(kw)
        return True

    summary = dew.run_drift_escalation_watchdog(
        db, enabled_override=True, min_count_override=5,
        lookback_minutes_override=60, alert_dispatcher=_dispatch,
    )

    hit_ids = {h.bracket_intent_id for h in summary.hits}
    assert iid_hit in hit_ids
    assert iid_miss not in hit_ids
    assert len(dispatched) == 1
