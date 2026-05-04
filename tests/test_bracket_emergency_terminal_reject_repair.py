"""audit-missing-stop-emergency-repair (2026-05-03) — regression tests
for the new branch in
``app.services.trading.bracket_reconciliation_service._invoke_writer_for_decision``
that handles open trade + missing_stop + intent_state=terminal_reject.

Seven scenarios covering all branches:

    1. Phantom branch (broker_qty == 0)              -> trade closed, no broker order.
    2. Real-exposure success (broker_qty == local_qty) -> stop placed at local qty.
    3. Real-exposure capped (broker_qty < local_qty) -> stop placed at broker qty.
    4. Real-exposure rejection-relock                 -> throttle bumped, no progression.
    5. Throttle expiry                                -> after 6h, new attempt fires.
    6. Flag OFF                                       -> state_gated_skip; no broker call.
    7. Broker unavailable                             -> skipped; no audit row, no throttle bump.

Tests stub ``place_missing_stop`` so no real broker is called. The
audit emit (``_g2_event``) is invoked normally so we can assert
``trading_execution_events`` rows. Uses the ``db`` fixture from
conftest (``chili_test`` enforced).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.trading.bracket_reconciler import (
    BrokerView,
    LocalView,
    ReconciliationDecision,
)


# Local minimal stand-in for bracket_writer_g2.WriterAction so the
# test file doesn't import the writer (which has env-time side
# effects). The real WriterAction is a dataclass; this matches the
# attributes we read.
@dataclass
class _StubAction:
    ok: bool
    reason: str
    new_stop_order_id: str | None = None
    new_stop_qty: float | None = None
    new_stop_price: float | None = None


# ── Test seed helpers ──────────────────────────────────────────────────


def _seed_trade_and_intent(db, *, trade_id: int, intent_id: int,
                           ticker: str, qty: float, stop_price: float,
                           intent_state: str = "terminal_reject",
                           trade_status: str = "open",
                           last_repair_attempt_at=None) -> None:
    """Insert one Trade + one BracketIntent row for the test.

    Schema notes:
      - trading_trades has direction (NOT side), entry_date, no
        updated_at. exit_reason exists; closed_reason does not.
      - trading_bracket_intents has direction (long/short), payload_json,
        intent_state, broker_source, and the new
        terminal_reject_repair_last_attempt_at column from migration 222.
    """
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date
        ) VALUES (
            :id, :ticker, :status, 'robinhood', 'long', :qty,
            1.0, NOW()
        )
        ON CONFLICT (id) DO NOTHING
    """), {"id": trade_id, "ticker": ticker, "status": trade_status, "qty": qty})

    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            id, trade_id, ticker, direction, quantity, entry_price,
            stop_price, intent_state, shadow_mode, broker_source,
            terminal_reject_repair_last_attempt_at,
            created_at, updated_at, payload_json
        ) VALUES (
            :id, :tid, :ticker, 'long', :qty, 1.0,
            :stop, :state, false, 'robinhood',
            :last_attempt,
            NOW(), NOW(), '{}'::jsonb
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
        "stop": stop_price, "state": intent_state,
        "last_attempt": last_repair_attempt_at,
    })
    db.commit()


def _local(*, trade_id: int, intent_id: int, ticker: str, qty: float,
           stop_price: float, intent_state: str = "terminal_reject",
           trade_status: str = "open") -> LocalView:
    return LocalView(
        trade_id=trade_id, bracket_intent_id=intent_id, ticker=ticker,
        direction="long", quantity=qty, intent_state=intent_state,
        stop_price=stop_price, target_price=None,
        broker_source="robinhood", trade_status=trade_status,
    )


def _broker(*, position_quantity: float | None, available: bool = True,
            ticker: str = "TEST") -> BrokerView:
    return BrokerView(
        available=available, ticker=ticker, broker_source="robinhood",
        position_quantity=position_quantity,
    )


def _decision_missing_stop() -> ReconciliationDecision:
    return ReconciliationDecision(
        kind="missing_stop", severity="warn", delta_payload={},
    )


def _settings_with_flag(value: bool):
    """Patch the settings.chili_bracket_missing_stop_repair_enabled
    attribute via attribute set on the live settings object — the
    branch reads it via getattr(settings, ..., False)."""
    from app.config import settings
    return patch.object(
        settings, "chili_bracket_missing_stop_repair_enabled", value,
        create=True,
    )


# ── Tests ─────────────────────────────────────────────────────────────


def _invoke(db, *, local, broker, decision, sweep_id: str = "test-sweep"):
    """Call the public invoke entry-point with mode='authoritative'."""
    from app.services.trading.bracket_reconciliation_service import (
        _invoke_writer_for_decision,
    )
    return _invoke_writer_for_decision(
        db, mode="authoritative", sweep_id=sweep_id,
        local=local, broker=broker, decision=decision,
    )


def test_phantom_branch_closes_trade_no_broker_order(db):
    """Scenario 1: broker_qty == 0 across N consecutive sweeps -> trade
    closed, no broker order placed, audit row written, throttle column set.

    bracket-emergency-repair-flap-guard (2026-05-04): the close now
    requires N (default 3) consecutive zero-qty sweeps. We invoke
    threshold-many times so the assertion contract holds under the
    flap guard.
    """
    from app.services.trading.bracket_reconciliation_service import (
        EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS as _N,
    )
    _seed_trade_and_intent(
        db, trade_id=9001, intent_id=99001, ticker="PHTM",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9001, intent_id=99001, ticker="PHTM",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=0.0, ticker="PHTM")

    with _settings_with_flag(True):
        result = None
        for _ in range(_N):
            result = _invoke(db, local=local, broker=broker,
                             decision=_decision_missing_stop())

    assert result is not None
    assert result["writer"] == "emergency_terminal_reject_repair"
    assert result["ok"] is True
    assert result["reason"] == "phantom_closed"
    assert result["new_stop_order_id"] is None

    # Trade should be marked closed via exit_reason (the schema
    # convention; closed_reason does not exist on trading_trades).
    row = db.execute(text(
        "SELECT status, exit_reason FROM trading_trades WHERE id=9001"
    )).first()
    assert row[0] == "closed"
    assert row[1] == "phantom_after_terminal_reject"

    # Intent should be marked closed and throttle set.
    row = db.execute(text(
        "SELECT intent_state, terminal_reject_repair_last_attempt_at "
        "FROM trading_bracket_intents WHERE id=99001"
    )).first()
    assert row[0] == "closed"
    assert row[1] is not None  # throttle set


def test_real_exposure_success_places_stop_at_local_qty(db):
    """Scenario 2: broker_qty == local_qty == 10, FIX-51 writer mock
    succeeds → stop placed at qty=10, intent state advances per
    place_missing_stop, throttle set."""
    _seed_trade_and_intent(
        db, trade_id=9002, intent_id=99002, ticker="REAL",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9002, intent_id=99002, ticker="REAL",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=10.0, ticker="REAL")

    captured_qty: list[float] = []

    def _stub_place(db_, *, trade_id, bracket_intent_id, ticker,
                    broker_source, decision, local_quantity, stop_price,
                    **kw):
        captured_qty.append(float(local_quantity))
        return _StubAction(
            ok=True, reason="placed",
            new_stop_order_id="rh-stop-123",
            new_stop_qty=float(local_quantity),
            new_stop_price=float(stop_price),
        )

    with _settings_with_flag(True), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        result = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())

    assert result is not None
    assert result["writer"] == "emergency_terminal_reject_repair"
    assert result["ok"] is True
    assert result["new_stop_order_id"] == "rh-stop-123"
    assert result["qty"] == 10.0
    assert captured_qty == [10.0]  # min(local=10, broker=10)

    # Throttle bumped.
    row = db.execute(text(
        "SELECT terminal_reject_repair_last_attempt_at "
        "FROM trading_bracket_intents WHERE id=99002"
    )).first()
    assert row[0] is not None


def test_real_exposure_capped_when_broker_qty_less(db):
    """Scenario 3: local_qty=20, broker_qty=10 → place at 10."""
    _seed_trade_and_intent(
        db, trade_id=9003, intent_id=99003, ticker="CAP",
        qty=20.0, stop_price=5.0,
    )
    local = _local(trade_id=9003, intent_id=99003, ticker="CAP",
                   qty=20.0, stop_price=5.0)
    broker = _broker(position_quantity=10.0, ticker="CAP")

    captured_qty: list[float] = []

    def _stub_place(db_, *, local_quantity, **kw):
        captured_qty.append(float(local_quantity))
        return _StubAction(
            ok=True, reason="placed", new_stop_order_id="rh-stop-456",
            new_stop_qty=float(local_quantity), new_stop_price=5.0,
        )

    with _settings_with_flag(True), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        result = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())

    assert result["ok"] is True
    assert captured_qty == [10.0]  # min(20, 10)
    assert result["qty"] == 10.0


def test_rejection_relock_throttles_intent(db):
    """Scenario 4: writer mock returns ok=False → throttle set, intent
    NOT advanced, immediate re-call within window returns None
    (caller falls through to state_gated_skip)."""
    _seed_trade_and_intent(
        db, trade_id=9004, intent_id=99004, ticker="REJ",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9004, intent_id=99004, ticker="REJ",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=10.0, ticker="REJ")

    def _stub_place(db_, **kw):
        return _StubAction(
            ok=False, reason="broker_rejected_again",
            new_stop_order_id=None, new_stop_qty=None, new_stop_price=None,
        )

    with _settings_with_flag(True), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        # First call attempts; bumps throttle; returns ok=False outcome.
        first = _invoke(db, local=local, broker=broker,
                        decision=_decision_missing_stop())
        assert first is not None
        assert first["ok"] is False
        assert first["reason"] == "broker_rejected_again"

        # Second call within throttle window: branch returns None →
        # caller falls through to state_gated_skip (None signals that).
        second = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())
    # Caller behavior: when this helper returns None, the outer
    # _invoke_writer_for_decision falls through and returns the
    # state_gated_skip dict.
    assert second is not None
    assert second["writer"] == "state_gated_skip"
    assert second["reason"] == "state_terminal_reject"


def test_throttle_expiry_allows_new_attempt(db):
    """Scenario 5: Throttle column set 7h ago → new attempt fires."""
    seven_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=7)
    _seed_trade_and_intent(
        db, trade_id=9005, intent_id=99005, ticker="EXP",
        qty=10.0, stop_price=5.0,
        last_repair_attempt_at=seven_hours_ago,
    )
    local = _local(trade_id=9005, intent_id=99005, ticker="EXP",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=10.0, ticker="EXP")

    def _stub_place(db_, **kw):
        return _StubAction(
            ok=True, reason="placed", new_stop_order_id="rh-stop-789",
            new_stop_qty=10.0, new_stop_price=5.0,
        )

    with _settings_with_flag(True), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        result = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())
    assert result is not None
    assert result["ok"] is True
    assert result["new_stop_order_id"] == "rh-stop-789"


def test_flag_off_falls_through_to_state_gated_skip(db):
    """Scenario 6: Flag False → emergency-repair branch never enters;
    outer falls through to state_gated_skip; no broker call, no audit
    row written by emergency-repair path."""
    _seed_trade_and_intent(
        db, trade_id=9006, intent_id=99006, ticker="OFF",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9006, intent_id=99006, ticker="OFF",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=10.0, ticker="OFF")

    place_called: list[bool] = []

    def _stub_place(db_, **kw):
        place_called.append(True)
        return _StubAction(ok=True, reason="placed")

    with _settings_with_flag(False), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        result = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())

    assert result is not None
    assert result["writer"] == "state_gated_skip"
    assert result["reason"] == "state_terminal_reject"
    assert place_called == []  # never called

    # Throttle column unchanged.
    row = db.execute(text(
        "SELECT terminal_reject_repair_last_attempt_at "
        "FROM trading_bracket_intents WHERE id=99006"
    )).first()
    assert row[0] is None


def test_broker_unavailable_skips_silently(db):
    """Scenario 7: broker.available == False → branch returns None
    (silent skip), no throttle bump, no broker call. Outer falls
    through to state_gated_skip."""
    _seed_trade_and_intent(
        db, trade_id=9007, intent_id=99007, ticker="DOWN",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9007, intent_id=99007, ticker="DOWN",
                   qty=10.0, stop_price=5.0)
    broker = BrokerView(
        available=False, ticker="DOWN", broker_source="robinhood",
        position_quantity=None,
    )

    place_called: list[bool] = []

    def _stub_place(db_, **kw):
        place_called.append(True)
        return _StubAction(ok=True, reason="placed")

    with _settings_with_flag(True), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        result = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())

    assert result is not None
    assert result["writer"] == "state_gated_skip"
    assert place_called == []

    # Throttle column unchanged (broker_unavailable doesn't bump).
    row = db.execute(text(
        "SELECT terminal_reject_repair_last_attempt_at "
        "FROM trading_bracket_intents WHERE id=99007"
    )).first()
    assert row[0] is None


# ── bracket-emergency-repair-flap-guard (2026-05-04) -- new scenarios ──


def test_single_sweep_zero_qty_does_not_phantom_close(db):
    """Scenario 8: a SINGLE sweep with broker_qty == 0 must NOT phantom-
    close. Counter increments to 1, audit row is phantom_close_deferred,
    trade stays open, intent stays terminal_reject. Closes the regression
    where a single auth-flap sweep manufactured a phantom close."""
    _seed_trade_and_intent(
        db, trade_id=9008, intent_id=99008, ticker="FLAP",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9008, intent_id=99008, ticker="FLAP",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=0.0, ticker="FLAP")

    with _settings_with_flag(True):
        result = _invoke(db, local=local, broker=broker,
                         decision=_decision_missing_stop())

    # The new branch returns None, falling through to state_gated_skip.
    # _invoke_writer_for_decision wraps that into the state_gated_skip
    # writer dict.
    assert result is not None
    assert result["writer"] == "state_gated_skip"

    row = db.execute(text(
        "SELECT status, exit_reason FROM trading_trades WHERE id=9008"
    )).first()
    assert row[0] == "open"
    assert row[1] is None  # not phantom-closed

    row = db.execute(text(
        "SELECT intent_state, phantom_close_consecutive_zero_qty_sweeps "
        "FROM trading_bracket_intents WHERE id=99008"
    )).first()
    assert row[0] == "terminal_reject"
    assert row[1] == 1


def test_three_consecutive_zeros_phantom_close(db):
    """Scenario 9: three consecutive sweeps with broker_qty == 0 DO
    phantom-close on the third. Mirrors scenario 1's end-state
    assertion under the new flap-guard contract."""
    from app.services.trading.bracket_reconciliation_service import (
        EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS as _N,
    )
    _seed_trade_and_intent(
        db, trade_id=9009, intent_id=99009, ticker="THRC",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9009, intent_id=99009, ticker="THRC",
                   qty=10.0, stop_price=5.0)
    broker = _broker(position_quantity=0.0, ticker="THRC")

    results: list = []
    with _settings_with_flag(True):
        for _ in range(_N):
            results.append(
                _invoke(db, local=local, broker=broker,
                        decision=_decision_missing_stop())
            )

    # First N-1 are deferred (state_gated_skip wrapper).
    for r in results[:-1]:
        assert r is not None
        assert r["writer"] == "state_gated_skip"

    # Last one phantom-closes.
    last = results[-1]
    assert last is not None
    assert last["writer"] == "emergency_terminal_reject_repair"
    assert last["ok"] is True
    assert last["reason"] == "phantom_closed"

    row = db.execute(text(
        "SELECT status, exit_reason FROM trading_trades WHERE id=9009"
    )).first()
    assert row[0] == "closed"
    assert row[1] == "phantom_after_terminal_reject"

    row = db.execute(text(
        "SELECT intent_state, phantom_close_consecutive_zero_qty_sweeps "
        "FROM trading_bracket_intents WHERE id=99009"
    )).first()
    assert row[0] == "closed"
    assert row[1] == _N  # counter NOT reset on phantom-close (sub-branch 3 owns reset)


def test_counter_resets_on_positive_broker_qty(db):
    """Scenario 10: two zero-qty sweeps then a positive-qty sweep then
    another zero-qty sweep. The positive observation in sub-branch 3
    must reset the counter to 0; the subsequent zero observation lands
    at counter == 1, NOT counter == 3 (would have phantom-closed)."""
    _seed_trade_and_intent(
        db, trade_id=9010, intent_id=99010, ticker="RSET",
        qty=10.0, stop_price=5.0,
    )
    local = _local(trade_id=9010, intent_id=99010, ticker="RSET",
                   qty=10.0, stop_price=5.0)
    broker_zero = _broker(position_quantity=0.0, ticker="RSET")
    broker_pos = _broker(position_quantity=10.0, ticker="RSET")

    # Stub place_missing_stop so sub-branch 3 reaches its UPDATE
    # (which resets the counter via _bump_repair_attempt).
    def _stub_place(db_, **kw):
        return _StubAction(ok=True, reason="placed")

    with _settings_with_flag(True), \
         patch("app.services.trading.bracket_writer_g2.place_missing_stop",
               side_effect=_stub_place):
        # Two consecutive zero-qty observations -> counter 1, then 2.
        _invoke(db, local=local, broker=broker_zero,
                decision=_decision_missing_stop())
        _invoke(db, local=local, broker=broker_zero,
                decision=_decision_missing_stop())
        cur = db.execute(text(
            "SELECT phantom_close_consecutive_zero_qty_sweeps "
            "FROM trading_bracket_intents WHERE id=99010"
        )).scalar()
        assert cur == 2

        # Positive observation -> sub-branch 3 fires, place_missing_stop
        # stub succeeds, _bump_repair_attempt resets counter to 0.
        _invoke(db, local=local, broker=broker_pos,
                decision=_decision_missing_stop())
        cur = db.execute(text(
            "SELECT phantom_close_consecutive_zero_qty_sweeps "
            "FROM trading_bracket_intents WHERE id=99010"
        )).scalar()
        assert cur == 0

        # Next zero observation lands at 1, NOT 3.
        _invoke(db, local=local, broker=broker_zero,
                decision=_decision_missing_stop())
        cur = db.execute(text(
            "SELECT phantom_close_consecutive_zero_qty_sweeps "
            "FROM trading_bracket_intents WHERE id=99010"
        )).scalar()
        assert cur == 1

    # Trade still open; never phantom-closed.
    row = db.execute(text(
        "SELECT status, exit_reason FROM trading_trades WHERE id=9010"
    )).first()
    assert row[0] == "open"
    assert row[1] is None
