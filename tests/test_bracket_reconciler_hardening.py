"""P0.5 - hardening tests for bracket reconciler safety.

Covers:

1. Partial-fill sizing — ``qty_drift.delta_payload`` encodes
   ``fill_ratio`` / ``is_partial_fill`` / ``expected_stop_qty`` /
   ``drift_kind`` so a Phase G.2 writer can resize the stop to the
   actually filled quantity.

2. Orphan-stop coverage — the sweep's ``_load_local_view`` now
   includes trades that are no longer open but whose BracketIntent is
   still in non-terminal state. Without this fix a cancelled entry
   with a working broker stop was invisible to the reconciler.

3. Crash-mid-state recovery — every non-agree scan bumps
   ``bracket_intent.last_observed_at`` so the watchdog can distinguish
   "reconciler saw this and it's still broken" (recent observation)
   from "reconciler never ran / crashed" (stale observation).

4. Watchdog — ``run_missing_stop_watchdog`` flags open live trades
   whose most recent reconciliation kind is ``missing_stop`` /
   ``orphan_stop`` and whose observation is older than the stale
   threshold, and fires one alert per hit via the injected dispatcher.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import Trade
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import (
    bump_last_observed,
    upsert_bracket_intent,
)
from app.services.trading.bracket_reconciler import (
    BrokerView,
    LocalView,
    Tolerances,
    classify_discrepancy,
)
from app.services.trading.bracket_reconciliation_service import (
    run_missing_stop_watchdog,
    run_reconciliation_sweep,
)


# ── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _shadow_mode(monkeypatch):
    """All sweep tests run in shadow mode so writer/service gates pass."""
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
    yield


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


def _broker_fn(*views: BrokerView):
    def fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
        return list(views)
    return fn


# ── 1. Partial-fill sizing in qty_drift delta_payload ────────────────


def _local_view_defaults(**over) -> LocalView:
    base = dict(
        trade_id=1, bracket_intent_id=10, ticker="AAPL", direction="long",
        quantity=10.0, intent_state="shadow_logged", stop_price=96.0,
        target_price=106.0, broker_source="robinhood", trade_status="open",
    )
    base.update(over)
    return LocalView(**base)


def _broker_view_defaults(**over) -> BrokerView:
    base = dict(
        available=True, ticker="AAPL", broker_source="robinhood",
        position_quantity=10.0, stop_order_id="s", stop_order_state="open",
        stop_order_price=96.0,
    )
    base.update(over)
    return BrokerView(**base)


class TestPartialFillSizing:
    def test_qty_drift_partial_fill_encoded_as_warn(self):
        """Broker holds less than intended — partial fill.

        Stop should be resizable to 7 shares (broker_qty). Severity warn
        because the position *is* still protected once resized.
        """
        local = _local_view_defaults(quantity=10.0)
        broker = _broker_view_defaults(position_quantity=7.0)
        d = classify_discrepancy(local, broker)
        assert d.kind == "qty_drift"
        assert d.severity == "warn"
        assert d.delta_payload["drift_kind"] == "partial_fill"
        assert d.delta_payload["is_partial_fill"] is True
        assert d.delta_payload["fill_ratio"] == pytest.approx(0.7)
        assert d.delta_payload["expected_stop_qty"] == 7.0
        assert d.delta_payload["local_qty"] == 10.0
        assert d.delta_payload["broker_qty"] == 7.0

    def test_qty_drift_broker_flat_is_error(self):
        """Broker has zero position but local thinks we're long — critical."""
        local = _local_view_defaults(quantity=10.0)
        broker = _broker_view_defaults(position_quantity=0.0)
        d = classify_discrepancy(local, broker)
        assert d.kind == "qty_drift"
        assert d.severity == "error"
        assert d.delta_payload["drift_kind"] == "broker_flat"
        assert d.delta_payload["is_partial_fill"] is False
        assert d.delta_payload["expected_stop_qty"] is None
        assert d.delta_payload["fill_ratio"] == 0.0

    def test_qty_drift_over_fill_is_error(self):
        """Broker holds more than intended — anomalous; should hard-alert."""
        local = _local_view_defaults(quantity=10.0)
        broker = _broker_view_defaults(position_quantity=15.0)
        d = classify_discrepancy(local, broker)
        assert d.kind == "qty_drift"
        assert d.severity == "error"
        assert d.delta_payload["drift_kind"] == "over_fill"
        assert d.delta_payload["is_partial_fill"] is False
        assert d.delta_payload["expected_stop_qty"] == 15.0
        assert d.delta_payload["fill_ratio"] == pytest.approx(1.5)

    def test_qty_drift_within_tolerance_is_agree(self):
        """Rounding-level diff stays agree (no qty_drift)."""
        local = _local_view_defaults(quantity=10.0)
        broker = _broker_view_defaults(position_quantity=10.0)
        d = classify_discrepancy(local, broker)
        assert d.kind == "agree"


# ── 2. Orphan-stop coverage: cancelled trade with live intent ────────


class TestOrphanStopCoverage:
    def test_cancelled_trade_with_working_broker_stop_is_scanned(self, db):
        """Headline guarantee — before P0.5 this case was invisible.

        Cancelled Trade + BracketIntent still in non-terminal state +
        broker still has a working stop → sweep must scan this row and
        classify it as ``orphan_stop`` (severity=error).
        """
        t = _make_trade(db, ticker="ORPHAN_CANC", status="cancelled")
        _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="ORPHAN_CANC", broker_source="robinhood",
                position_quantity=0.0,
                stop_order_id="stale-stop", stop_order_state="open",
                stop_order_price=96.0,
            )
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.trades_scanned == 1, "cancelled-trade scope gap: row not scanned"
        assert summary.orphan_stop == 1
        rows = db.execute(text("""
            SELECT kind, severity FROM trading_bracket_reconciliation_log
            WHERE ticker = 'ORPHAN_CANC'
        """)).fetchall()
        assert rows and rows[0][0] == "orphan_stop"
        assert rows[0][1] == "error"

    def test_reconciled_intent_on_cancelled_trade_is_not_rescanned(self, db):
        """Once the intent is in ``reconciled`` state, the orphan-scope
        rule must stop pulling the row — avoids rescanning terminal rows.
        """
        t = _make_trade(db, ticker="ORPHAN_DONE", status="cancelled")
        intent_id = _intent(db, t)
        db.execute(text("""
            UPDATE trading_bracket_intents
            SET intent_state = 'reconciled' WHERE id = :id
        """), {"id": intent_id})
        db.commit()
        summary = run_reconciliation_sweep(db, broker_view_fn=_broker_fn())
        assert summary.trades_scanned == 0

    def test_open_trade_scope_still_works(self, db):
        """Regression: the new OR clause must not drop open-trade coverage."""
        t = _make_trade(db, ticker="OPEN_CTRL")
        _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="OPEN_CTRL", broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
                target_order_id="t", target_order_state="open", target_order_price=106.0,
            )
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.trades_scanned == 1
        assert summary.agree == 1


# ── 3. Crash-mid-state recovery: bump_last_observed ──────────────────


class TestLastObservedBump:
    def test_non_agree_scan_bumps_last_observed_at(self, db):
        """Every non-agree row stays in non-terminal state; we still want
        ``last_observed_at`` to move forward so the watchdog knows the
        reconciler actually visited this intent.
        """
        t = _make_trade(db, ticker="BUMP_MISS")
        intent_id = _intent(db, t)
        before = db.execute(text("""
            SELECT last_observed_at FROM trading_bracket_intents WHERE id = :id
        """), {"id": intent_id}).scalar_one()
        assert before is None  # writer doesn't stamp this

        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="BUMP_MISS", broker_source="robinhood",
                position_quantity=10.0,
                # No working stop → missing_stop branch.
            )
        )
        summary = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        assert summary.missing_stop == 1

        row = db.execute(text("""
            SELECT last_observed_at, last_diff_reason, intent_state
            FROM trading_bracket_intents WHERE id = :id
        """), {"id": intent_id}).fetchone()
        assert row[0] is not None, (
            "crash-recovery signal: last_observed_at must be bumped on non-agree scans"
        )
        assert row[1] and row[1].startswith("missing_stop"), (
            "diff_reason should carry the kind:severity from the decision"
        )
        # State must NOT be 'reconciled' — that's mark_reconciled's job only.
        assert row[2] != "reconciled"

    def test_bump_helper_does_not_change_intent_state(self, db):
        """``bump_last_observed`` is a pure signal helper — it must not
        flip any intent into reconciled/terminal state.
        """
        t = _make_trade(db, ticker="BUMP_PURE")
        intent_id = _intent(db, t)
        state_before = db.execute(text("""
            SELECT intent_state FROM trading_bracket_intents WHERE id = :id
        """), {"id": intent_id}).scalar_one()
        bump_last_observed(db, intent_id, diff_reason="qty_drift:warn")
        db.commit()
        state_after = db.execute(text("""
            SELECT intent_state, last_diff_reason FROM trading_bracket_intents WHERE id = :id
        """), {"id": intent_id}).fetchone()
        assert state_after[0] == state_before
        assert state_after[1] == "qty_drift:warn"

    def test_agree_still_uses_mark_reconciled(self, db):
        """Agree rows should still advance to ``reconciled`` — bump is a
        distinct, lower-privilege signal. Regression guard against collapsing
        the two paths.
        """
        t = _make_trade(db, ticker="AGREE_BUMP")
        intent_id = _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="AGREE_BUMP", broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
                target_order_id="t", target_order_state="open", target_order_price=106.0,
            )
        )
        run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        state = db.execute(text("""
            SELECT intent_state FROM trading_bracket_intents WHERE id = :id
        """), {"id": intent_id}).scalar_one()
        assert state == "reconciled"


# ── 4. Watchdog — run_missing_stop_watchdog ───────────────────────────


class _SpyDispatcher:
    """Captures dispatch_alert calls without touching SMS / DB alert rows."""

    def __init__(self, return_value: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.return_value = return_value

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return self.return_value


class TestMissingStopWatchdog:
    def test_disabled_returns_empty(self, db, monkeypatch):
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", False, raising=False)
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(db, alert_dispatcher=spy)
        assert summary.enabled is False
        assert summary.hits == []
        assert spy.calls == []

    def test_fresh_missing_stop_is_not_yet_stale(self, db, monkeypatch):
        """A missing_stop observation just written is fresh → no alert."""
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="FRESH_MISS")
        _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="FRESH_MISS", broker_source="robinhood",
                position_quantity=10.0,  # no stop
            )
        )
        run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=spy, stale_after_sec=3600,
        )
        assert summary.enabled is True
        assert summary.open_trades_scanned == 1
        assert summary.hits == []
        assert spy.calls == []

    def test_stale_missing_stop_fires_one_alert(self, db, monkeypatch):
        """Back-date the reconciliation row so it crosses the threshold.

        Headline guarantee — the watchdog fires an alert via the dispatcher
        and reports a ``missing_stop`` hit with age > threshold.
        """
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="STALE_MISS")
        _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="STALE_MISS", broker_source="robinhood",
                position_quantity=10.0,
            )
        )
        run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        # Back-date the reconciliation row 10 minutes.
        db.execute(text("""
            UPDATE trading_bracket_reconciliation_log
            SET observed_at = NOW() - INTERVAL '10 minutes'
            WHERE ticker = 'STALE_MISS'
        """))
        db.commit()
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=spy, stale_after_sec=60,
        )
        assert summary.enabled is True
        assert len(summary.hits) == 1
        hit = summary.hits[0]
        assert hit.kind == "missing_stop"
        assert hit.age_seconds >= 60
        assert hit.alert_sent is True
        assert len(spy.calls) == 1
        assert spy.calls[0]["alert_type"] == "bracket_watchdog_missing_stop"
        assert spy.calls[0]["ticker"] == "STALE_MISS"

    def test_stale_orphan_stop_also_fires(self, db, monkeypatch):
        """Orphan stops on cancelled entries are also operator-critical —
        the watchdog must alert for this kind too.

        Instead of running a full sweep to write the log row, we insert
        one directly; the watchdog queries the log regardless of how it
        was produced, and this keeps the test fast + DB-cheap.
        """
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="STALE_ORPHAN")
        intent_id = _intent(db, t)
        # Back-date the intent so its age > threshold too, so
        # `never_observed` would fire if our log row weren't found.
        db.execute(text("""
            UPDATE trading_bracket_intents
            SET created_at = NOW() - INTERVAL '10 minutes'
            WHERE id = :id
        """), {"id": intent_id})
        # Insert an orphan_stop log row for this trade with a stale observed_at.
        db.execute(text("""
            INSERT INTO trading_bracket_reconciliation_log (
                sweep_id, trade_id, bracket_intent_id, ticker, broker_source,
                kind, severity, local_payload, broker_payload, delta_payload,
                mode, observed_at
            ) VALUES (
                'test-sweep-orphan', :tid, :iid, 'STALE_ORPHAN', 'robinhood',
                'orphan_stop', 'error',
                CAST('{}' AS JSONB), CAST('{}' AS JSONB), CAST('{}' AS JSONB),
                'shadow', NOW() - INTERVAL '10 minutes'
            )
        """), {"tid": t.id, "iid": intent_id})
        db.commit()
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=spy, stale_after_sec=60,
        )
        assert len(summary.hits) == 1
        assert summary.hits[0].kind == "orphan_stop"
        assert spy.calls[0]["alert_type"] == "bracket_watchdog_orphan_stop"

    def test_agree_observations_never_fire(self, db, monkeypatch):
        """Watchdog must ignore agree kinds no matter how old."""
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="AGREE_OLD")
        _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="AGREE_OLD", broker_source="robinhood",
                position_quantity=10.0,
                stop_order_id="s", stop_order_state="open", stop_order_price=96.0,
                target_order_id="t", target_order_state="open", target_order_price=106.0,
            )
        )
        run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        db.execute(text("""
            UPDATE trading_bracket_reconciliation_log
            SET observed_at = NOW() - INTERVAL '1 hour'
            WHERE ticker = 'AGREE_OLD'
        """))
        db.commit()
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=spy, stale_after_sec=60,
        )
        assert summary.hits == []
        assert spy.calls == []

    def test_never_observed_intent_fires_after_threshold(self, db, monkeypatch):
        """An open trade with a BracketIntent but no reconciliation row
        yet should eventually trip ``never_observed`` — this is the
        crash-recovery signal for the case where the sweep hasn't run
        at all (or crashed before touching this intent).
        """
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="NEVER_SEEN")
        _intent(db, t)
        # Back-date the bracket intent creation so its age is > threshold.
        db.execute(text("""
            UPDATE trading_bracket_intents
            SET created_at = NOW() - INTERVAL '10 minutes'
            WHERE trade_id = :tid
        """), {"tid": t.id})
        db.commit()

        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=spy, stale_after_sec=60,
        )
        assert summary.open_trades_scanned == 1
        assert len(summary.hits) == 1
        assert summary.hits[0].kind == "never_observed"
        assert summary.hits[0].severity == "error"
        assert spy.calls[0]["alert_type"] == "bracket_watchdog_never_observed"

    def test_reconciled_intent_is_skipped(self, db, monkeypatch):
        """An intent flipped to ``reconciled`` must not show up even if
        old — otherwise any closed position would fire the watchdog.
        """
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="RECON_DONE")
        intent_id = _intent(db, t)
        db.execute(text("""
            UPDATE trading_bracket_intents
            SET intent_state = 'reconciled',
                created_at = NOW() - INTERVAL '10 minutes'
            WHERE id = :id
        """), {"id": intent_id})
        db.commit()
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=spy, stale_after_sec=60,
        )
        assert summary.hits == []

    def test_paper_trades_excluded(self, db, monkeypatch):
        """Paper positions don't need a broker stop — skip."""
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="PAPER_SKIP", broker_source=None)
        assert t.broker_source is None
        # No intent required — _make_trade with broker_source=None simulates
        # paper; the watchdog query excludes these.
        spy = _SpyDispatcher()
        summary = run_missing_stop_watchdog(db, alert_dispatcher=spy, stale_after_sec=60)
        assert summary.hits == []

    def test_dispatcher_failure_does_not_crash_watchdog(self, db, monkeypatch):
        """Dispatcher raising must be caught; other hits still reported."""
        monkeypatch.setattr(settings, "chili_bracket_watchdog_enabled", True, raising=False)
        t = _make_trade(db, ticker="BOOM_DISP")
        _intent(db, t)
        broker_fn = _broker_fn(
            BrokerView(
                available=True, ticker="BOOM_DISP", broker_source="robinhood",
                position_quantity=10.0,
            )
        )
        run_reconciliation_sweep(db, broker_view_fn=broker_fn)
        db.execute(text("""
            UPDATE trading_bracket_reconciliation_log
            SET observed_at = NOW() - INTERVAL '10 minutes'
            WHERE ticker = 'BOOM_DISP'
        """))
        db.commit()

        def bad_dispatcher(**kwargs):
            raise RuntimeError("simulated sms failure")

        summary = run_missing_stop_watchdog(
            db, alert_dispatcher=bad_dispatcher, stale_after_sec=60,
        )
        assert len(summary.hits) == 1
        hit = summary.hits[0]
        assert hit.alert_sent is False
        assert hit.alert_skip_reason and hit.alert_skip_reason.startswith("dispatch_error:")
