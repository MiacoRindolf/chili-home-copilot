"""Tests for the leaked / stale pending-exit reconciliation.

Closes the class of bug where a Robinhood exit order rests as ``queued`` and is
never re-evaluated: ``has_active_pending_exit`` makes the AutoTrader monitor
``continue`` past the trade, and ``sync_pending_exit_order`` only acts on fills
/ terminal states. A gfd market sell placed at Friday's close that queues across
the weekend (real incident: CORB trade 2287) would route at the uncontrolled
Monday-open print with the stop silently unmanaged.

These are pure-logic tests — the heavy collaborators (broker market-hours,
cancel, re-submit, position-truth reconcile) are patched — so they run without
a live broker or database. The end-to-end wiring through ``sync_orders_to_db``
is covered in ``test_broker_sync.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.trading import robinhood_exit_execution as rxe

_UTC = timezone.utc

# Monday 2026-06-08 14:00 UTC = 10:00 ET — inside the regular session.
# The 09:30 ET open that day is 13:30 UTC.
MON_IN_SESSION = datetime(2026, 6, 8, 14, 0, 0, tzinfo=_UTC)
# The incident order: requested Friday 2026-06-05 19:59:51 (naive UTC, as the
# column stores it), before Monday's open -> it spanned a session boundary.
FRI_CLOSE_REQUESTED = datetime(2026, 6, 5, 19, 59, 51)

REG_WINDOW = {"session": "regular_hours", "can_submit_now": True, "market_hours": "regular_hours"}
WEEKEND_WINDOW = {"session": "closed_weekend", "can_submit_now": False, "market_hours": None}


class _FakeDB:
    def add(self, *_a, **_k):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


def _trade(**overrides):
    base = dict(
        id=2287,
        user_id=None,
        ticker="CORB",
        direction="long",
        quantity=1.332337,
        entry_price=29.3169,
        status="open",
        broker_source="robinhood",
        pending_exit_order_id="6a232acc-d365-40ae-975c-05b4d36e9c88",
        pending_exit_status="queued",
        pending_exit_reason="stop",
        pending_exit_requested_at=FRI_CLOSE_REQUESTED,
        pending_exit_limit_price=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _queued_order(**overrides):
    base = {"id": "6a232acc", "state": "queued", "side": "sell",
            "type": "market", "cumulative_quantity": "0"}
    base.update(overrides)
    return base


# ── _is_urgent_exit_reason ────────────────────────────────────────────


class TestIsUrgentExitReason:
    @pytest.mark.parametrize(
        "reason",
        ["stop", "STOP", "stop_loss_hit", "pattern_exit_now", "desk_close_now",
         "emergency_drawdown", "emergency_kill_switch"],
    )
    def test_urgent_reasons(self, reason):
        assert rxe._is_urgent_exit_reason(reason) is True

    @pytest.mark.parametrize("reason", ["target", "take_profit", "trailing_take", "", None, "scale_out"])
    def test_non_urgent_reasons(self, reason):
        assert rxe._is_urgent_exit_reason(reason) is False


# ── _pending_exit_is_stale_resting ────────────────────────────────────


class TestPendingExitIsStaleResting:
    def _call(self, trade, *, window=None, order=None, now=MON_IN_SESSION):
        return rxe._pending_exit_is_stale_resting(
            trade,
            window=window or REG_WINDOW,
            broker_order=order or _queued_order(),
            now_utc=now,
        )

    def test_stale_when_queued_across_session_open(self):
        # Requested Friday close, still queued Monday mid-session -> stale.
        assert self._call(_trade()) is True

    def test_confirmed_state_also_stale(self):
        assert self._call(_trade(), order=_queued_order(state="confirmed")) is True

    def test_fresh_same_session_not_stale(self):
        # Requested after today's 09:30 ET open -> normal latency, not stale.
        fresh = _trade(pending_exit_requested_at=datetime(2026, 6, 8, 13, 45, 0))
        assert self._call(fresh) is False

    def test_not_stale_off_hours(self):
        # Even an ancient order is left alone when no tradeable session is open.
        assert self._call(_trade(), window=WEEKEND_WINDOW) is False

    def test_not_stale_when_cannot_submit(self):
        win = {"session": "regular_hours", "can_submit_now": False, "market_hours": "regular_hours"}
        assert self._call(_trade(), window=win) is False

    def test_not_stale_with_partial_fill(self):
        # Any progress -> the partial/fill reconcile path owns it, not this one.
        assert self._call(_trade(), order=_queued_order(cumulative_quantity="0.5")) is False

    @pytest.mark.parametrize("state", ["filled", "cancelled", "rejected", "expired"])
    def test_terminal_states_not_stale(self, state):
        assert self._call(_trade(), order=_queued_order(state=state)) is False

    def test_missing_requested_at_not_stale(self):
        assert self._call(_trade(pending_exit_requested_at=None)) is False


# ── reconcile_pending_exit_liveness ───────────────────────────────────


class TestReconcilePendingExitLiveness:
    def _patch_window(self, monkeypatch, window):
        monkeypatch.setattr(
            rxe, "describe_robinhood_equity_execution_window",
            lambda *a, **k: window,
        )

    def test_requeues_stale_stop_under_price_protection(self, monkeypatch):
        self._patch_window(monkeypatch, REG_WINDOW)
        cancel_calls, submit_calls = [], []
        monkeypatch.setattr(
            rxe, "cancel_pending_exit_order",
            lambda db, trade, **k: (cancel_calls.append(k) or {"ok": True, "state": "cancelled"}),
        )
        monkeypatch.setattr(
            rxe, "submit_robinhood_trade_exit",
            lambda db, trade, **k: (submit_calls.append(k) or {"ok": True, "state": "working"}),
        )

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), _trade(), broker_order=_queued_order(), now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "requeued_price_protected"
        assert len(cancel_calls) == 1
        assert len(submit_calls) == 1
        assert submit_calls[0]["price_protected"] is True
        assert submit_calls[0]["exit_reason"] == "stop"

    def test_noop_for_non_urgent_target_exit(self, monkeypatch):
        # A resting target/limit must NOT be force-converted to a marketable
        # exit — that would dump the position below target.
        self._patch_window(monkeypatch, REG_WINDOW)
        submit_calls = []
        monkeypatch.setattr(
            rxe, "submit_robinhood_trade_exit",
            lambda *a, **k: (submit_calls.append(k) or {"ok": True, "state": "working"}),
        )

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), _trade(pending_exit_reason="target"),
            broker_order=_queued_order(), now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "none"
        assert submit_calls == []

    def test_noop_for_fresh_order(self, monkeypatch):
        self._patch_window(monkeypatch, REG_WINDOW)
        submit_calls = []
        monkeypatch.setattr(
            rxe, "submit_robinhood_trade_exit",
            lambda *a, **k: (submit_calls.append(k) or {"ok": True}),
        )
        fresh = _trade(pending_exit_requested_at=datetime(2026, 6, 8, 13, 45, 0))

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), fresh, broker_order=_queued_order(), now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "none"
        assert submit_calls == []

    def test_noop_off_hours(self, monkeypatch):
        self._patch_window(monkeypatch, WEEKEND_WINDOW)
        submit_calls = []
        monkeypatch.setattr(
            rxe, "submit_robinhood_trade_exit",
            lambda *a, **k: (submit_calls.append(k) or {"ok": True}),
        )

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), _trade(), broker_order=_queued_order(), now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "none"
        assert submit_calls == []

    def test_cancel_failure_leaves_order_and_does_not_resubmit(self, monkeypatch):
        self._patch_window(monkeypatch, REG_WINDOW)
        submit_calls = []
        monkeypatch.setattr(
            rxe, "cancel_pending_exit_order",
            lambda *a, **k: {"ok": False, "error": "rate_limited"},
        )
        monkeypatch.setattr(
            rxe, "submit_robinhood_trade_exit",
            lambda *a, **k: (submit_calls.append(k) or {"ok": True}),
        )

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), _trade(), broker_order=_queued_order(), now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "cancel_failed"
        assert out["error"] == "rate_limited"
        assert submit_calls == []

    def test_missing_order_closes_when_position_gone(self, monkeypatch):
        from app.services.trading import broker_position_truth as bpt

        monkeypatch.setattr(
            bpt, "reconcile_stale_robinhood_open_trade",
            lambda db, trade, **k: {"status": "closed"},
        )

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), _trade(), broker_order=None, now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "closed"

    def test_missing_order_noop_when_position_live(self, monkeypatch):
        from app.services.trading import broker_position_truth as bpt

        monkeypatch.setattr(
            bpt, "reconcile_stale_robinhood_open_trade",
            lambda db, trade, **k: None,
        )

        out = rxe.reconcile_pending_exit_liveness(
            _FakeDB(), _trade(), broker_order=None, now_utc=MON_IN_SESSION,
        )

        assert out["action"] == "none"
        assert out["reason"] == "order_missing_position_live_or_grace"
