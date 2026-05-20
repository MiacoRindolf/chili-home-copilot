"""f-coinbase-bracket-coverage-fix (2026-05-10).

Pin the three structural fixes that closed the bug where 9 open
Coinbase trades sat unprotected at the venue:

  * Bug A — stop_engine emits bracket_intent on every sweep, not
    just on alert events. (entry-time coverage)
  * Bug B — reconciler backfills missing intents at sweep load
    time. (safety net for any code path that bypasses stop_engine)
  * Bug C — writer-invocation gate accepts both Robinhood and
    Coinbase via _SUPPORTED_VENUES; previously a hardcoded
    `!= "robinhood"` silently rejected every Coinbase missing_stop.

Tests run against the pytest `_test`-suffixed Postgres DB. Heavy
plumbing (broker_view_fn, place_missing_stop) is mocked at the seam
so these tests are fast and don't reach the network.
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.models.trading import Trade
from app.services.trading import bracket_writer_g2 as bw_g2
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent
from app.services.trading.bracket_reconciler import (
    BrokerView,
    LocalView,
    ReconciliationDecision,
    Tolerances,
)
from app.services.trading.bracket_reconciliation_service import (
    SweepBatch,
    _invoke_writer_for_decision,
    _stage_backfill_missing_intents,
    _stage_load_local,
    run_reconciliation_sweep,
)
from app.services.trading.stop_engine import _maybe_emit_bracket_intent


# ─── Helpers ────────────────────────────────────────────────────────


def _make_trade(
    db,
    *,
    ticker: str,
    broker_source: str | None,
    stop_loss: float | None,
    take_profit: float | None = None,
    qty: float = 10.0,
    entry: float = 100.0,
    direction: str = "long",
    status: str = "open",
) -> Trade:
    """Insert a Trade row directly. The `broker_source` parameter
    distinguishes paper (None) from live (robinhood / coinbase)."""
    t = Trade(
        ticker=ticker,
        direction=direction,
        entry_price=entry,
        quantity=qty,
        status=status,
        broker_source=broker_source,
        stop_loss=stop_loss,
        take_profit=(
            take_profit
            if take_profit is not None
            else ((entry + 5.0) if stop_loss is not None else None)
        ),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _intent_count_for(db, trade_id: int) -> int:
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM trading_bracket_intents WHERE trade_id=:t"),
            {"t": trade_id},
        ).scalar_one()
    )


def _intent_row(db, trade_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            "SELECT id, intent_state, broker_source, stop_price, target_price, updated_at "
            "FROM trading_bracket_intents WHERE trade_id=:t ORDER BY id DESC LIMIT 1"
        ),
        {"t": trade_id},
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "intent_state": row[1],
        "broker_source": row[2],
        "stop_price": float(row[3]) if row[3] is not None else None,
        "target_price": float(row[4]) if row[4] is not None else None,
        "updated_at": row[5],
    }


def _set_bracket_mode(monkeypatch, mode: str) -> None:
    """The mode is read from `settings.brain_live_brackets_mode` from
    several modules; patch each to the same value."""
    for target in (
        "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
        "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
    ):
        monkeypatch.setattr(target, mode, raising=False)
    # stop_engine reads via local `from ...config import settings as _cfg`
    # inside the function each call, so patching the live settings module
    # covers it.
    from app.config import settings as _cfg
    monkeypatch.setattr(_cfg, "brain_live_brackets_mode", mode, raising=False)


def _enable_writer_flag(monkeypatch) -> None:
    from app.config import settings as _cfg
    monkeypatch.setattr(
        _cfg, "chili_bracket_sweep_writer_enabled", True, raising=False,
    )


# ─── Bug A: stop_engine entry-time emission ────────────────────────


class TestBugAEntryTimeEmission:
    def test_emits_intent_for_coinbase_trade_without_alert(
        self, db, monkeypatch,
    ):
        """A fresh Coinbase trade with stop_loss>0 produces an intent
        row even when the stop_engine produces no alert event. This
        is the entry-time coverage gap that left 9 trades naked."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="ADA-USD", broker_source="coinbase",
            stop_loss=95.0, entry=100.0, qty=5.0,
        )
        assert _intent_count_for(db, t.id) == 0

        # Simulate the post-Bug-A call site in stop_engine.evaluate_all:
        # invoke the emitter unconditionally (no alert event needed).
        _maybe_emit_bracket_intent(db, t, brain=None)

        assert _intent_count_for(db, t.id) == 1
        row = _intent_row(db, t.id)
        assert row is not None
        assert row["broker_source"] == "coinbase"
        assert row["stop_price"] is not None and row["stop_price"] > 0

    def test_no_emit_for_paper_trade(self, db, monkeypatch):
        """Paper trades (broker_source unset) must remain excluded —
        regression guard for the broker_source short-circuit inside
        the emitter."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="PAPER_X", broker_source=None,
            stop_loss=95.0,
        )
        _maybe_emit_bracket_intent(db, t, brain=None)
        assert _intent_count_for(db, t.id) == 0

    def test_no_emit_when_mode_is_off(self, db, monkeypatch):
        _set_bracket_mode(monkeypatch, "off")
        t = _make_trade(
            db, ticker="OFFMODE-USD", broker_source="coinbase",
            stop_loss=95.0,
        )
        _maybe_emit_bracket_intent(db, t, brain=None)
        assert _intent_count_for(db, t.id) == 0

    def test_emit_idempotent_on_repeat(self, db, monkeypatch):
        """Calling the emitter twice on the same trade must keep
        exactly one intent row (the upsert's existing-row branch)."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="IDEMP-USD", broker_source="coinbase",
            stop_loss=99.0,
        )
        _maybe_emit_bracket_intent(db, t, brain=None)
        first = _intent_row(db, t.id)
        assert first is not None

        _maybe_emit_bracket_intent(db, t, brain=None)

        assert _intent_count_for(db, t.id) == 1
        second = _intent_row(db, t.id)
        assert second is not None
        assert second["id"] == first["id"]

    def test_emit_uses_trade_current_stop_and_target(self, db, monkeypatch):
        """Recurring emits must not roll monitor-managed levels backward."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db,
            ticker="CURR-USD",
            broker_source="coinbase",
            stop_loss=88.0,
            take_profit=140.0,
            entry=100.0,
        )

        _maybe_emit_bracket_intent(db, t, brain=None)
        first = _intent_row(db, t.id)
        assert first is not None
        assert first["stop_price"] == pytest.approx(88.0)
        assert first["target_price"] == pytest.approx(140.0)

        t.stop_loss = 92.0
        t.take_profit = 150.0
        db.add(t)
        db.commit()

        _maybe_emit_bracket_intent(db, t, brain=None)
        second = _intent_row(db, t.id)
        assert second is not None
        assert second["id"] == first["id"]
        assert second["stop_price"] == pytest.approx(92.0)
        assert second["target_price"] == pytest.approx(150.0)


# ─── Bug B: reconciler backfills missing intents ──────────────────


class TestBugBReconcilerBackfill:
    def test_backfills_open_coinbase_trade_with_no_intent(
        self, db, monkeypatch,
    ):
        """An open live trade with stop_loss>0 and no intent row must
        get an intent created by _stage_backfill_missing_intents."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="BACKFILL-USD", broker_source="coinbase",
            stop_loss=92.0,
        )
        rows = [
            {
                "trade_id": t.id,
                "bracket_intent_id": None,
                "broker_source": "coinbase",
                "trade_status": "open",
            }
        ]
        n = _stage_backfill_missing_intents(db, rows, mode="shadow")
        assert n == 1
        assert _intent_count_for(db, t.id) == 1

    def test_backfill_skips_trade_with_existing_intent(
        self, db, monkeypatch,
    ):
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="EXISTS-USD", broker_source="coinbase",
            stop_loss=92.0,
        )
        upsert_bracket_intent(
            db,
            trade_id=t.id,
            user_id=None,
            bracket_input=BracketIntentInput(
                ticker=t.ticker,
                direction="long",
                entry_price=t.entry_price,
                quantity=t.quantity,
                regime="cautious",
            ),
            broker_source="coinbase",
        )
        before = _intent_count_for(db, t.id)
        rows = [
            {
                "trade_id": t.id,
                "bracket_intent_id": _intent_row(db, t.id)["id"],
                "broker_source": "coinbase",
                "trade_status": "open",
            }
        ]
        n = _stage_backfill_missing_intents(db, rows, mode="shadow")
        assert n == 0
        assert _intent_count_for(db, t.id) == before

    def test_backfill_skips_paper_trade(self, db, monkeypatch):
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="PAPER-BACK", broker_source=None,
            stop_loss=92.0,
        )
        rows = [
            {
                "trade_id": t.id,
                "bracket_intent_id": None,
                "broker_source": None,
                "trade_status": "open",
            }
        ]
        n = _stage_backfill_missing_intents(db, rows, mode="shadow")
        assert n == 0
        assert _intent_count_for(db, t.id) == 0

    def test_backfill_skips_trade_without_stop_loss(self, db, monkeypatch):
        """No-magic-fallback rule: a trade with stop_loss=None has no
        information for the brain to write, so we do nothing."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="NOSTOP-USD", broker_source="coinbase",
            stop_loss=None,
        )
        rows = [
            {
                "trade_id": t.id,
                "bracket_intent_id": None,
                "broker_source": "coinbase",
                "trade_status": "open",
            }
        ]
        n = _stage_backfill_missing_intents(db, rows, mode="shadow")
        assert n == 0
        assert _intent_count_for(db, t.id) == 0

    def test_backfill_noop_when_mode_off(self, db, monkeypatch):
        _set_bracket_mode(monkeypatch, "off")
        t = _make_trade(
            db, ticker="OFFBACK-USD", broker_source="coinbase",
            stop_loss=92.0,
        )
        rows = [
            {
                "trade_id": t.id,
                "bracket_intent_id": None,
                "broker_source": "coinbase",
                "trade_status": "open",
            }
        ]
        n = _stage_backfill_missing_intents(db, rows, mode="off")
        assert n == 0
        assert _intent_count_for(db, t.id) == 0

    def test_full_sweep_backfills_then_classifies(self, db, monkeypatch):
        """End-to-end: an open Coinbase trade with no intent enters
        the sweep, gets backfilled, and the classifier sees it."""
        _set_bracket_mode(monkeypatch, "shadow")
        t = _make_trade(
            db, ticker="FULLSWEEP-USD", broker_source="coinbase",
            stop_loss=92.0,
        )
        assert _intent_count_for(db, t.id) == 0

        def broker_view_fn(local_rows):
            return [
                BrokerView(
                    available=True,
                    ticker=r["ticker"],
                    broker_source=r["broker_source"],
                    position_quantity=10.0,
                )
                for r in local_rows
            ]

        summary = run_reconciliation_sweep(db, broker_view_fn=broker_view_fn)
        assert summary.trades_scanned >= 1
        assert _intent_count_for(db, t.id) == 1


# ─── Bug C: writer-invocation gate accepts Coinbase ───────────────


class TestBugCWriterInvocation:
    def _make_local_and_broker(
        self, *, broker_source: str, with_intent_id: int | None = 999,
    ) -> tuple[LocalView, BrokerView, ReconciliationDecision]:
        local = LocalView(
            trade_id=42,
            bracket_intent_id=with_intent_id,
            ticker="ADA-USD" if broker_source == "coinbase" else "AAPL",
            direction="long",
            quantity=10.0,
            intent_state="intent",
            stop_price=92.0,
            target_price=110.0,
            broker_source=broker_source,
            trade_status="open",
        )
        broker = BrokerView(
            available=True,
            ticker=local.ticker,
            broker_source=broker_source,
            position_quantity=10.0,
        )
        decision = ReconciliationDecision(
            kind="missing_stop", severity="warn", delta_payload={},
        )
        return local, broker, decision

    def test_coinbase_missing_stop_reaches_writer(
        self, db, monkeypatch, caplog,
    ):
        """The smoking gun fix: Coinbase missing_stop must reach
        place_missing_stop. Patch the writer to a MagicMock and assert
        it was called."""
        _set_bracket_mode(monkeypatch, "authoritative")
        _enable_writer_flag(monkeypatch)

        local, broker, decision = self._make_local_and_broker(
            broker_source="coinbase",
        )
        mock_writer = MagicMock(
            return_value=bw_g2.WriterAction(
                action="place_missing_stop", ok=True, reason="ok",
                broker_source="coinbase", ticker=local.ticker,
                new_stop_order_id="cb-1",
            )
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_writer_g2.place_missing_stop",
            mock_writer,
        )
        # Block the secondary throttle/decision-resolution helpers; they
        # are not under test here.
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service."
            "_resolve_pending_bracket_decision",
            lambda *a, **kw: None,
        )

        result = _invoke_writer_for_decision(
            db, mode="authoritative", sweep_id="test-sweep",
            local=local, broker=broker, decision=decision,
        )
        assert result is not None
        assert mock_writer.called
        kwargs = mock_writer.call_args.kwargs
        assert kwargs["broker_source"] == "coinbase"
        assert kwargs["ticker"] == "ADA-USD"

    def test_robinhood_missing_stop_still_reaches_writer(
        self, db, monkeypatch,
    ):
        """Regression guard: Robinhood byte-identical contract is
        intact — RH still reaches place_missing_stop."""
        _set_bracket_mode(monkeypatch, "authoritative")
        _enable_writer_flag(monkeypatch)
        local, broker, decision = self._make_local_and_broker(
            broker_source="robinhood",
        )
        mock_writer = MagicMock(
            return_value=bw_g2.WriterAction(
                action="place_missing_stop", ok=True, reason="ok",
                broker_source="robinhood", ticker="AAPL",
                new_stop_order_id="rh-1",
            )
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_writer_g2.place_missing_stop",
            mock_writer,
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service."
            "_resolve_pending_bracket_decision",
            lambda *a, **kw: None,
        )

        result = _invoke_writer_for_decision(
            db, mode="authoritative", sweep_id="test-sweep",
            local=local, broker=broker, decision=decision,
        )
        assert result is not None
        assert mock_writer.called
        assert mock_writer.call_args.kwargs["broker_source"] == "robinhood"

    def test_unsupported_venue_logs_and_skips(
        self, db, monkeypatch, caplog,
    ):
        """A trade with an unsupported broker_source must NOT reach
        the writer and must produce a visible info log line."""
        _set_bracket_mode(monkeypatch, "authoritative")
        _enable_writer_flag(monkeypatch)
        local, broker, decision = self._make_local_and_broker(
            broker_source="alpaca",
        )
        mock_writer = MagicMock()
        monkeypatch.setattr(
            "app.services.trading.bracket_writer_g2.place_missing_stop",
            mock_writer,
        )

        with caplog.at_level(logging.INFO):
            result = _invoke_writer_for_decision(
                db, mode="authoritative", sweep_id="test-sweep",
                local=local, broker=broker, decision=decision,
            )
        assert result is None
        assert not mock_writer.called
        assert any(
            "venue-unsupported" in rec.getMessage()
            for rec in caplog.records
        ), "expected info log line containing 'venue-unsupported'"

    def test_missing_intent_id_logs_and_skips(
        self, db, monkeypatch, caplog,
    ):
        """A LocalView with bracket_intent_id=None must NOT silently
        skip — it logs an info line so the operator notices when the
        backfill stage missed a row."""
        _set_bracket_mode(monkeypatch, "authoritative")
        _enable_writer_flag(monkeypatch)
        local, broker, decision = self._make_local_and_broker(
            broker_source="coinbase", with_intent_id=None,
        )
        mock_writer = MagicMock()
        monkeypatch.setattr(
            "app.services.trading.bracket_writer_g2.place_missing_stop",
            mock_writer,
        )

        with caplog.at_level(logging.INFO):
            result = _invoke_writer_for_decision(
                db, mode="authoritative", sweep_id="test-sweep",
                local=local, broker=broker, decision=decision,
            )
        assert result is None
        assert not mock_writer.called
        assert any(
            "no-intent-row" in rec.getMessage()
            for rec in caplog.records
        ), "expected info log line containing 'no-intent-row'"

    def test_coinbase_qty_drift_resize_explicitly_skipped(
        self, db, monkeypatch, caplog,
    ):
        """Coinbase has no resize peer for place_stop_limit_order_gtc
        yet — the reconciler must explicitly skip with an info log
        rather than crashing inside the writer."""
        _set_bracket_mode(monkeypatch, "authoritative")
        _enable_writer_flag(monkeypatch)
        local = LocalView(
            trade_id=44,
            bracket_intent_id=1001,
            ticker="ADA-USD",
            direction="long",
            quantity=10.0,
            intent_state="confirmed_at_broker",
            stop_price=92.0,
            target_price=110.0,
            broker_source="coinbase",
            trade_status="open",
        )
        broker = BrokerView(
            available=True, ticker="ADA-USD", broker_source="coinbase",
            position_quantity=8.0, stop_order_id="cb-existing",
            stop_order_state="open", stop_order_price=92.0,
        )
        decision = ReconciliationDecision(
            kind="qty_drift", severity="warn",
            delta_payload={"drift_kind": "partial_fill",
                           "expected_stop_qty": 8.0},
        )
        mock_resize = MagicMock()
        monkeypatch.setattr(
            "app.services.trading.bracket_writer_g2."
            "resize_stop_for_partial_fill",
            mock_resize,
        )
        monkeypatch.setattr(
            "app.services.trading.bracket_reconciliation_service."
            "_resolve_pending_bracket_decision",
            lambda *a, **kw: None,
        )

        with caplog.at_level(logging.INFO):
            result = _invoke_writer_for_decision(
                db, mode="authoritative", sweep_id="test-sweep",
                local=local, broker=broker, decision=decision,
            )
        assert result is not None
        assert result["writer"] == "resize_stop_for_partial_fill"
        assert result["reason"] == "resize_not_yet_wired_for_coinbase"
        assert not mock_resize.called
        assert any(
            "resize_not_yet_wired_for_coinbase" in rec.getMessage()
            for rec in caplog.records
        )
