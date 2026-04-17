"""Phase G - DB integration tests for ``bracket_reconciliation_service``."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.models.trading import Trade
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent
from app.services.trading.bracket_reconciler import BrokerView
from app.services.trading.bracket_reconciliation_service import (
    bracket_reconciliation_summary,
    run_reconciliation_sweep,
)


def _make_trade(
    db, *, ticker="AAPL", user_id=None, qty=10.0, entry=100.0,
    status="open", broker_source="robinhood", direction="long",
) -> Trade:
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction=direction,
        entry_price=entry,
        quantity=qty,
        status=status,
        broker_source=broker_source,
        stop_loss=entry - 4.0,
        take_profit=entry + 6.0,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _intent(db, trade: Trade, *, stop=96.0, target=106.0) -> int:
    res = upsert_bracket_intent(
        db,
        trade_id=trade.id,
        user_id=None,
        bracket_input=BracketIntentInput(
            ticker=trade.ticker,
            direction=trade.direction,
            entry_price=trade.entry_price,
            quantity=trade.quantity,
            atr=2.0,
            stop_model="atr_swing",
            lifecycle_stage="validated",
            regime="cautious",
        ),
        broker_source=trade.broker_source,
    )
    assert res is not None
    db.execute(text("""
        UPDATE trading_bracket_intents
        SET stop_price = :sp, target_price = :tp
        WHERE id = :id
    """), {"sp": stop, "tp": target, "id": res.intent_id})
    db.commit()
    return res.intent_id


def _broker_fn_that_returns(*views: BrokerView):
    def fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
        return list(views)
    return fn


class TestModeGates:
    def test_off_mode_short_circuits(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
            "off",
            raising=False,
        )
        summary = run_reconciliation_sweep(db)
        assert summary.mode == "off"
        assert summary.trades_scanned == 0
        assert summary.rows_written == 0

    def test_authoritative_raises(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
            "authoritative",
            raising=False,
        )
        with pytest.raises(RuntimeError):
            run_reconciliation_sweep(db)


class TestShadowSweep:
    def test_shadow_sweep_with_agree(self, db, monkeypatch):
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
        t = _make_trade(db, ticker="AGREE_PHG")
        _intent(db, t)

        broker_fn = _broker_fn_that_returns(
            BrokerView(
                available=True,
                ticker="AGREE_PHG",
                broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="stop-1",
                stop_order_state="open",
                stop_order_price=96.0,
                target_order_id="tgt-1",
                target_order_state="open",
                target_order_price=106.0,
            )
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.mode == "shadow"
        assert summary.trades_scanned == 1
        assert summary.brackets_checked == 1
        assert summary.agree == 1
        assert summary.rows_written == 1
        rows = db.execute(text("""
            SELECT kind, mode FROM trading_bracket_reconciliation_log
            WHERE sweep_id = :sid
        """), {"sid": summary.sweep_id}).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "agree"
        assert rows[0][1] == "shadow"

    def test_shadow_sweep_flags_qty_drift(self, db, monkeypatch):
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
        t = _make_trade(db, ticker="QTY_PHG", qty=10.0)
        _intent(db, t)

        broker_fn = _broker_fn_that_returns(
            BrokerView(
                available=True, ticker="QTY_PHG", broker_source="robinhood",
                position_quantity=9.0,
                stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
            )
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.qty_drift == 1
        assert summary.agree == 0

    def test_shadow_sweep_flags_missing_stop(self, db, monkeypatch):
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
        t = _make_trade(db, ticker="MISS_PHG")
        _intent(db, t)

        broker_fn = _broker_fn_that_returns(
            BrokerView(
                available=True, ticker="MISS_PHG", broker_source="robinhood",
                position_quantity=10.0,
            )
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.missing_stop == 1

    def test_shadow_sweep_flags_broker_down(self, db, monkeypatch):
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
        t = _make_trade(db, ticker="DOWN_PHG")
        _intent(db, t)

        broker_fn = _broker_fn_that_returns(
            BrokerView(available=False, ticker="DOWN_PHG", broker_source="robinhood")
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.broker_down == 1

    def test_paper_trades_are_excluded(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
            "shadow",
            raising=False,
        )
        paper = _make_trade(db, ticker="PAPER_PHG", broker_source=None)
        assert paper.broker_source is None
        summary = run_reconciliation_sweep(db)
        assert summary.trades_scanned == 0


class TestIdempotency:
    def test_two_sweeps_without_changes_same_agree_count(self, db, monkeypatch):
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
        t = _make_trade(db, ticker="IDEM_PHG")
        _intent(db, t)

        broker_fn = _broker_fn_that_returns(
            BrokerView(
                available=True, ticker="IDEM_PHG", broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
                target_order_id="t", target_order_state="open", target_order_price=106.0,
            )
        )
        s1 = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        s2 = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert s1.agree == 1
        assert s2.agree == 1
        intent_count = db.execute(text("""
            SELECT COUNT(*) FROM trading_bracket_intents WHERE trade_id = :tid
        """), {"tid": t.id}).scalar_one()
        assert intent_count == 1
        log_count = db.execute(text("""
            SELECT COUNT(*) FROM trading_bracket_reconciliation_log WHERE trade_id = :tid
        """), {"tid": t.id}).scalar_one()
        assert log_count == 2


class TestDiagnosticsSummary:
    def test_summary_frozen_shape(self, db, monkeypatch):
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
        t = _make_trade(db, ticker="SUM_PHG")
        _intent(db, t)
        broker_fn = _broker_fn_that_returns(
            BrokerView(
                available=True, ticker="SUM_PHG", broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
            )
        )
        run_reconciliation_sweep(db, broker_view_fn=broker_fn)

        summary = bracket_reconciliation_summary(db, lookback_hours=1)
        assert set(summary.keys()) == {
            "mode",
            "lookback_hours",
            "recent_sweeps_requested",
            "rows_total",
            "by_kind",
            "by_severity",
            "last_sweep_id",
            "last_observed_at",
            "sweeps_recent",
        }
        assert summary["mode"] == "shadow"
        assert summary["rows_total"] >= 1
        assert isinstance(summary["sweeps_recent"], list)
