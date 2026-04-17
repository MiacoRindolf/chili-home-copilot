"""Phase G - DB integration tests for ``bracket_intent_writer``."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.trading import Trade
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import (
    bracket_intent_summary,
    mark_reconciled,
    mode_is_active,
    upsert_bracket_intent,
)


def _make_trade(db, *, ticker="AAPL", user_id=None, qty=10.0, entry=100.0, direction="long") -> Trade:
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction=direction,
        entry_price=entry,
        quantity=qty,
        status="open",
        broker_source="robinhood",
        stop_loss=entry - 4.0,
        take_profit=entry + 6.0,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _input_for(trade: Trade, **over) -> BracketIntentInput:
    defaults = dict(
        ticker=trade.ticker,
        direction=trade.direction,
        entry_price=trade.entry_price,
        quantity=trade.quantity,
        atr=2.0,
        stop_model="atr_swing",
        lifecycle_stage="validated",
        regime="cautious",
    )
    defaults.update(over)
    return BracketIntentInput(**defaults)


class TestModeGate:
    def test_off_mode_returns_none(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "off",
            raising=False,
        )
        assert mode_is_active() is False
        t = _make_trade(db, ticker="OFF_PHG")
        res = upsert_bracket_intent(
            db,
            trade_id=t.id,
            user_id=None,
            bracket_input=_input_for(t),
        )
        assert res is None
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_bracket_intents WHERE trade_id = :tid"
        ), {"tid": t.id}).scalar_one()
        assert count == 0


class TestShadowUpsert:
    def test_shadow_writes_one_row(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        t = _make_trade(db, ticker="SHAD1_PHG")
        res = upsert_bracket_intent(
            db,
            trade_id=t.id,
            user_id=None,
            bracket_input=_input_for(t),
            broker_source="robinhood",
        )
        assert res is not None
        assert res.created is True
        assert res.state == "shadow_logged"
        row = db.execute(text(
            "SELECT intent_state, shadow_mode, broker_source, stop_price "
            "FROM trading_bracket_intents WHERE trade_id = :tid"
        ), {"tid": t.id}).fetchone()
        assert row is not None
        assert row[0] == "shadow_logged"
        assert row[1] is True
        assert row[2] == "robinhood"
        assert row[3] is not None

    def test_shadow_upsert_is_idempotent(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        t = _make_trade(db, ticker="SHAD2_PHG")
        first = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t),
        )
        second = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t),
        )
        assert first is not None and second is not None
        assert first.intent_id == second.intent_id
        assert first.created is True
        assert second.created is False
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_bracket_intents WHERE trade_id = :tid"
        ), {"tid": t.id}).scalar_one()
        assert count == 1

    def test_shadow_upsert_updates_stop_on_reinput(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        t = _make_trade(db, ticker="SHAD3_PHG")
        first = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t, atr=2.0),
        )
        second = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t, atr=4.0),
        )
        assert first.stop_price != second.stop_price


class TestAuthoritativeProtection:
    def test_authoritative_state_is_preserved(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        t = _make_trade(db, ticker="AUTH_PHG")
        first = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t),
        )
        assert first is not None
        db.execute(text(
            "UPDATE trading_bracket_intents SET intent_state = 'authoritative_submitted' "
            "WHERE id = :id"
        ), {"id": first.intent_id})
        db.commit()

        second = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t, atr=4.0),
        )
        assert second is not None
        assert second.created is False
        row = db.execute(text(
            "SELECT intent_state FROM trading_bracket_intents WHERE id = :id"
        ), {"id": first.intent_id}).fetchone()
        assert row[0] == "authoritative_submitted"


class TestMarkReconciled:
    def test_mark_reconciled_transitions(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        t = _make_trade(db, ticker="REC_PHG")
        first = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t),
        )
        assert first is not None
        updated = mark_reconciled(db, first.intent_id, reason="agree")
        assert updated is True
        row = db.execute(text(
            "SELECT intent_state, last_diff_reason FROM trading_bracket_intents WHERE id = :id"
        ), {"id": first.intent_id}).fetchone()
        assert row[0] == "reconciled"
        assert row[1] == "agree"

    def test_mark_reconciled_skips_authoritative(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        t = _make_trade(db, ticker="RECA_PHG")
        first = upsert_bracket_intent(
            db, trade_id=t.id, user_id=None, bracket_input=_input_for(t),
        )
        assert first is not None
        db.execute(text(
            "UPDATE trading_bracket_intents SET intent_state = 'authoritative_submitted' "
            "WHERE id = :id"
        ), {"id": first.intent_id})
        db.commit()
        updated = mark_reconciled(db, first.intent_id, reason="agree")
        assert updated is False


class TestSummary:
    def test_summary_counts_by_state(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        for tk in ("S1_PHG", "S2_PHG", "S3_PHG"):
            t = _make_trade(db, ticker=tk)
            upsert_bracket_intent(
                db, trade_id=t.id, user_id=None, bracket_input=_input_for(t),
                broker_source="robinhood",
            )
        summary = bracket_intent_summary(db, lookback_hours=1)
        assert summary["mode"] == "shadow"
        assert summary["intents_total"] >= 3
        assert summary["by_state"].get("shadow_logged", 0) >= 3
        assert summary["by_broker_source"].get("robinhood", 0) >= 3
        assert summary["latest_intent"] is not None
