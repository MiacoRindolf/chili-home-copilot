"""broker-truth-self-heal (2026-05-04) -- regression tests for the
inverse-reconcile branch in
``app.services.broker_service.sync_positions_to_db``.

Four scenarios:

    A. bookkeeping-only close + matching broker position -> reopen.
    B. real execution-event history + broker still reports position
       -> CONTRADICTION error log, no mutation.
    C. bookkeeping-only close + qty/price MISMATCH -> fall through
       (existing C2 path governs).
    D. no historical Trade row -> existing C2 path governs.

Tests use the chili_test conftest db fixture. Run with
``-p no:asyncio``.

The inverse-reconcile reads broker positions via the existing
``get_positions`` / ``get_crypto_positions`` helpers; we patch those
to feed deterministic snapshots. ``is_connected`` is also patched so
the function doesn't short-circuit on the test host.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text


def _seed_trade(
    db,
    *,
    trade_id: int,
    ticker: str,
    qty: float,
    entry_price: float,
    status: str = "closed",
    exit_reason: str | None = "phantom_after_terminal_reject",
    user_id: int | None = None,
) -> None:
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date,
            exit_reason, exit_date,
            user_id
        ) VALUES (
            :id, :ticker, :status, 'robinhood', 'long', :qty,
            :entry, NOW() - INTERVAL '1 day',
            :exit_reason, CASE WHEN :status='closed' THEN NOW() ELSE NULL END,
            :uid
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "status": status, "qty": qty,
        "entry": entry_price, "exit_reason": exit_reason,
        "uid": user_id,
    })


def _seed_bracket_intent(
    db, *, trade_id: int, intent_id: int, ticker: str, qty: float,
    entry: float, stop: float, intent_state: str = "closed",
) -> None:
    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            id, trade_id, ticker, direction, quantity, entry_price,
            stop_price, intent_state, shadow_mode, broker_source,
            created_at, updated_at, payload_json
        ) VALUES (
            :id, :tid, :ticker, 'long', :qty, :entry,
            :stop, :state, false, 'robinhood',
            NOW(), NOW(), '{}'::jsonb
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
        "entry": entry, "stop": stop, "state": intent_state,
    })


def _seed_execution_event(db, *, trade_id: int, ticker: str) -> None:
    """Insert one minimal execution_events row tied to trade_id. Shape
    mirrors what record_execution_event writes for any fill -- we only
    care that the COUNT(*) returns > 0 for the contradiction branch."""
    db.execute(text("""
        INSERT INTO trading_execution_events (
            trade_id, ticker, venue, execution_family, broker_source,
            event_type, status, event_at, recorded_at, payload_json
        ) VALUES (
            :tid, :ticker, 'rh', 'robinhood_equity', 'robinhood',
            'status', 'filled', NOW(), NOW(), '{}'::jsonb
        )
    """), {"tid": trade_id, "ticker": ticker})


def _patch_broker_positions(positions: list[dict]):
    """Patch the per-call broker accessors so sync_positions_to_db sees
    a deterministic snapshot regardless of the test host's network."""
    return [
        patch("app.services.broker_service.is_connected", return_value=True),
        patch(
            "app.services.broker_service.acquire_broker_position_sync_lock",
            return_value=None,
        ),
        patch(
            "app.services.broker_service.collapse_open_broker_position_duplicates",
            return_value={"cancelled": 0},
        ),
        patch("app.services.broker_service.get_positions", return_value=positions),
        patch("app.services.broker_service.get_crypto_positions", return_value=[]),
        patch(
            "app.services.broker_service._compute_trade_snapshot",
            return_value="{}",
        ),
    ]


# ── Scenarios ─────────────────────────────────────────────────────────


def test_inverse_reconcile_reopens_bookkeeping_close(db):
    """Scenario A: closed trade, no execution_events, broker reports
    matching qty/avg_price -> trade re-opens."""
    _seed_trade(
        db, trade_id=4001, ticker="ALPHA", qty=10.0, entry_price=5.0,
        exit_reason="phantom_after_terminal_reject",
    )
    _seed_bracket_intent(
        db, trade_id=4001, intent_id=44001, ticker="ALPHA",
        qty=10.0, entry=5.0, stop=4.5, intent_state="closed",
    )
    db.commit()

    from app.services import broker_service

    patches = _patch_broker_positions([
        {"ticker": "ALPHA", "quantity": 10.0, "average_buy_price": 5.0},
    ])
    for p in patches:
        p.start()
    try:
        result = broker_service.sync_positions_to_db(db, user_id=None)
    finally:
        for p in patches:
            p.stop()

    assert result.get("reopened", 0) >= 1

    row = db.execute(text(
        "SELECT status, exit_reason, exit_date, exit_price "
        "FROM trading_trades WHERE id=4001"
    )).first()
    assert row[0] == "open"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None

    row = db.execute(text(
        "SELECT intent_state, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=44001"
    )).first()
    assert row[0] == "intent"
    assert row[1] == "inverse_reconcile_reopen"


def test_inverse_reconcile_blocks_on_execution_history(db, caplog):
    """Scenario B: closed trade has an execution_event row (real broker
    activity) AND broker still reports the position. NOT auto-
    reconciled. Error log emitted with CONTRADICTION."""
    import logging

    _seed_trade(
        db, trade_id=4002, ticker="BETA", qty=20.0, entry_price=4.0,
        exit_reason="target",
    )
    _seed_execution_event(db, trade_id=4002, ticker="BETA")
    db.commit()

    from app.services import broker_service

    patches = _patch_broker_positions([
        {"ticker": "BETA", "quantity": 20.0, "average_buy_price": 4.0},
    ])
    for p in patches:
        p.start()
    try:
        with caplog.at_level(
            logging.ERROR, logger="app.services.broker_service",
        ):
            result = broker_service.sync_positions_to_db(db, user_id=None)
    finally:
        for p in patches:
            p.stop()

    assert result.get("reopened", 0) == 0

    row = db.execute(text(
        "SELECT status, exit_reason FROM trading_trades WHERE id=4002"
    )).first()
    assert row[0] == "closed", "trade with execution history must NOT be reopened"
    assert row[1] == "target"

    matching = [
        r for r in caplog.records
        if "CONTRADICTION" in r.getMessage() and "trade_id=4002" in r.getMessage()
    ]
    assert matching, "expected CONTRADICTION error log; got none"


def test_inverse_reconcile_skips_on_qty_mismatch(db):
    """Scenario C: closed trade, no execution_events, broker reports a
    DIFFERENT qty. Inverse-reconcile does not re-open; falls through
    to the existing GGG/C2 path. (We don't assert that path's
    behavior here -- only that the closed trade row is unchanged.)"""
    _seed_trade(
        db, trade_id=4003, ticker="GAMMA", qty=10.0, entry_price=3.0,
        exit_reason="phantom_after_terminal_reject",
    )
    db.commit()

    from app.services import broker_service

    patches = _patch_broker_positions([
        {"ticker": "GAMMA", "quantity": 25.0, "average_buy_price": 3.0},  # qty mismatch
    ])
    for p in patches:
        p.start()
    try:
        result = broker_service.sync_positions_to_db(db, user_id=None)
    finally:
        for p in patches:
            p.stop()

    assert result.get("reopened", 0) == 0

    row = db.execute(text(
        "SELECT status, exit_reason FROM trading_trades WHERE id=4003"
    )).first()
    assert row[0] == "closed", "qty-mismatch must NOT route to reopen"


def test_inverse_reconcile_no_history_falls_to_c2(db):
    """Scenario D: broker reports a ticker with no Trade row in DB at
    all. Inverse-reconcile finds no most_recent row -> existing C2
    phantom-guard path governs. We only assert that no row was
    incorrectly created with status=open via the inverse-reconcile
    path."""
    from app.services import broker_service

    patches = _patch_broker_positions([
        {"ticker": "DELTA", "quantity": 50.0, "average_buy_price": 2.0},
    ])
    for p in patches:
        p.start()
    try:
        result = broker_service.sync_positions_to_db(db, user_id=None)
    finally:
        for p in patches:
            p.stop()

    assert result.get("reopened", 0) == 0

    # No DELTA Trade row should exist (C2's phantom guard refuses to
    # create without a matching buy fill in recent history; on the test
    # host there is no broker history so it falls through).
    cnt = db.execute(text(
        "SELECT COUNT(*) FROM trading_trades WHERE ticker='DELTA'"
    )).scalar()
    assert cnt == 0
