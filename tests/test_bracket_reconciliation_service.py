"""Phase G - DB integration tests for ``bracket_reconciliation_service``."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.models.trading import Trade
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent
from app.services.trading.bracket_reconciler import BrokerView, LocalView, Tolerances
from app.services.trading.bracket_reconciliation_service import (
    SweepBatch,
    _stage_classify_all,
    _stage_fetch_broker,
    _stage_load_local,
    _stage_log_all,
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


# ── Staged-sweep refactor: focused stage tests + flag-parity ──────────


def _local(**over) -> LocalView:
    defaults = dict(
        trade_id=1,
        bracket_intent_id=10,
        ticker="AAA",
        direction="long",
        quantity=10.0,
        intent_state="shadow_logged",
        stop_price=96.0,
        target_price=106.0,
        broker_source="robinhood",
        trade_status="open",
    )
    defaults.update(over)
    return LocalView(**defaults)


def _bv(**over) -> BrokerView:
    defaults = dict(
        available=True,
        ticker="AAA",
        broker_source="robinhood",
        position_quantity=10.0,
        stop_order_id="s",
        stop_order_state="open",
        stop_order_price=96.0,
        target_order_id="t",
        target_order_state="open",
        target_order_price=106.0,
    )
    defaults.update(over)
    return BrokerView(**defaults)


class TestStagedClassifyStage:
    """``_stage_classify_all`` is pure — must run without any DB / broker."""

    def test_classifies_each_pair_without_db(self):
        batch = SweepBatch(
            sweep_id="sweep-test",
            mode="shadow",
            tolerances=Tolerances(),
            local_views=[
                _local(trade_id=1, ticker="AAA"),
                _local(trade_id=2, ticker="BBB"),
                _local(trade_id=3, ticker="CCC", quantity=10.0),
            ],
            broker_views=[
                _bv(ticker="AAA"),
                _bv(ticker="BBB", available=False),
                _bv(ticker="CCC", position_quantity=9.0),
            ],
        )
        decisions = _stage_classify_all(batch)
        assert [d.kind for d in decisions] == ["agree", "broker_down", "qty_drift"]
        assert batch.decisions is decisions

    def test_empty_batch_returns_empty_decisions(self):
        batch = SweepBatch(sweep_id="x", mode="shadow", tolerances=Tolerances())
        assert _stage_classify_all(batch) == []
        assert batch.decisions == []


class TestStagedFetchBrokerStage:
    """``_stage_fetch_broker`` must align broker views parallel to locals and
    backfill ``available=False`` when the broker function omits a ticker."""

    def test_aligns_views_and_backfills_missing_as_unavailable(self):
        batch = SweepBatch(
            sweep_id="x", mode="shadow", tolerances=Tolerances(),
            local_views=[
                _local(trade_id=1, ticker="AAA"),
                _local(trade_id=2, ticker="BBB"),
            ],
        )

        def broker_fn(local_rows):
            # Intentionally only return one of the two tickers.
            return [_bv(ticker="AAA")]

        _stage_fetch_broker(batch, broker_fn)
        assert len(batch.broker_views) == 2
        assert batch.broker_views[0].ticker == "AAA"
        assert batch.broker_views[0].available is True
        assert batch.broker_views[1].ticker == "BBB"
        assert batch.broker_views[1].available is False


class TestStagedLoadLocalStage:
    """``_stage_load_local`` populates ``local_views`` via the scope query."""

    def test_loads_open_live_trade_with_intent(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow", raising=False,
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
            "shadow", raising=False,
        )
        t = _make_trade(db, ticker="LOAD_STAGE")
        _intent(db, t)
        batch = SweepBatch(sweep_id="x", mode="shadow", tolerances=Tolerances())
        _stage_load_local(db, batch)
        tickers = [lv.ticker for lv in batch.local_views]
        assert "LOAD_STAGE" in tickers


class TestStagedLogAllStage:
    """``_stage_log_all`` writes one row per decision + bumps intents."""

    def test_writes_rows_and_returns_count(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow", raising=False,
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
            "shadow", raising=False,
        )
        t = _make_trade(db, ticker="LOG_STAGE")
        intent_id = _intent(db, t)
        batch = SweepBatch(sweep_id="x", mode="shadow", tolerances=Tolerances())
        _stage_load_local(db, batch)
        _stage_fetch_broker(
            batch,
            _broker_fn_that_returns(
                BrokerView(
                    available=True, ticker="LOG_STAGE", broker_source="robinhood",
                    position_quantity=10.0,
                    stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
                    target_order_id="t", target_order_state="open", target_order_price=106.0,
                )
            ),
        )
        _stage_classify_all(batch)
        rows_written = _stage_log_all(db, batch)
        db.commit()
        assert rows_written == len(batch.local_views)
        assert rows_written >= 1
        log_count = db.execute(text("""
            SELECT COUNT(*) FROM trading_bracket_reconciliation_log
            WHERE sweep_id = :sid
        """), {"sid": "x"}).scalar_one()
        assert log_count == rows_written
        # intent_id exists to ensure the fixture wiring is real
        assert intent_id is not None


class TestStagedVsLegacyParity:
    """Flag-on and flag-off must produce byte-identical ``SweepSummary``
    fields (modulo sweep_id + took_ms). This is the exit-criteria parity
    gate before ``brain_live_brackets_staged_sweep_enabled`` is flipped.
    """

    def _shadow(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
            "shadow", raising=False,
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
            "shadow", raising=False,
        )

    def _flag(self, monkeypatch, value: bool):
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service."
            "settings.brain_live_brackets_staged_sweep_enabled",
            value, raising=False,
        )

    def _seed_mixed_trades(self, db):
        t_agree = _make_trade(db, ticker="AGREE_PAR")
        _intent(db, t_agree)
        t_qty = _make_trade(db, ticker="QTY_PAR", qty=10.0)
        _intent(db, t_qty)
        t_miss = _make_trade(db, ticker="MISS_PAR")
        _intent(db, t_miss)
        t_down = _make_trade(db, ticker="DOWN_PAR")
        _intent(db, t_down)

    def _broker_fn(self):
        return _broker_fn_that_returns(
            BrokerView(
                available=True, ticker="AGREE_PAR", broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="s1", stop_order_state="open", stop_order_price=96.0,
                target_order_id="t1", target_order_state="open", target_order_price=106.0,
            ),
            BrokerView(
                available=True, ticker="QTY_PAR", broker_source="robinhood",
                position_quantity=9.0,
                stop_order_id="s2", stop_order_state="open", stop_order_price=96.0,
            ),
            BrokerView(
                available=True, ticker="MISS_PAR", broker_source="robinhood",
                position_quantity=10.0,
            ),
            BrokerView(available=False, ticker="DOWN_PAR", broker_source="robinhood"),
        )

    def test_staged_matches_legacy_summary(self, db, monkeypatch):
        self._shadow(monkeypatch)
        self._seed_mixed_trades(db)

        self._flag(monkeypatch, False)
        s_legacy = run_reconciliation_sweep(db, broker_view_fn=self._broker_fn())

        self._flag(monkeypatch, True)
        s_staged = run_reconciliation_sweep(db, broker_view_fn=self._broker_fn())

        parity_fields = (
            "mode", "trades_scanned", "brackets_checked",
            "agree", "orphan_stop", "missing_stop",
            "qty_drift", "state_drift", "price_drift",
            "broker_down", "unreconciled", "rows_written",
        )
        for f in parity_fields:
            assert getattr(s_legacy, f) == getattr(s_staged, f), (
                f"SweepSummary.{f} diverged: legacy={getattr(s_legacy, f)!r}, "
                f"staged={getattr(s_staged, f)!r}"
            )
        assert s_legacy.agree == 1
        assert s_legacy.qty_drift == 1
        assert s_legacy.missing_stop == 1
        assert s_legacy.broker_down == 1

    def test_staged_flag_defaults_to_true(self):
        from app.config import settings
        assert settings.brain_live_brackets_staged_sweep_enabled is True
