"""f-crypto-stale-trade-closer (2026-05-08, Phase E).

Pin the crypto-stale sweep:

  Layer 1 — entry-never-filled:
    * window not expired -> no close
    * window expired -> cancelled with `entry_never_filled`

  Layer 2 — broker-zero-qty streak:
    * present -> streak resets to 0
    * absent + streak < N -> increment, no close
    * absent + streak >= N -> cancelled with
      `broker_position_reconciled_to_zero`

  Trade 1810 audit replay:
    * status=open, last_fill_at=NULL, entry_date 7.6 days ago,
      broker reports zero -> cancelled by layer 1 on first sweep.

Phase A extension:
    * Both new exit reasons are excluded from the PDT count
      (`_RECONCILE_ARTIFACT_EXIT_REASONS`).

Run with ``-p no:asyncio`` (workaround for pre-existing pytest-asyncio
plugin collection failure).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.trading.bracket_reconciliation_service import (
    CRYPTO_EXIT_REASON_BROKER_ZERO_QTY,
    CRYPTO_EXIT_REASON_ENTRY_NEVER_FILLED,
    run_crypto_stale_trade_close,
)


# ── Seed helper ──────────────────────────────────────────────────────


def _seed_open_crypto_trade(
    db,
    *,
    trade_id: int,
    ticker: str,
    entry_date,
    last_fill_at=None,
    quantity: float = 1.0,
    streak: int = 0,
) -> None:
    """Seed a crypto trade outside the entry-fill window unless caller
    overrides `entry_date`. user_id=NULL so we dodge the FK."""
    db.execute(text("""
        INSERT INTO trading_trades (
            id, user_id, ticker, status, broker_source, direction,
            quantity, entry_price, entry_date, last_fill_at,
            crypto_broker_zero_qty_streak
        ) VALUES (
            :id, NULL, :ticker, 'open', 'coinbase', 'long',
            :qty, 1.0, :ed, :lfa, :streak
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "qty": quantity,
        "ed": entry_date, "lfa": last_fill_at, "streak": streak,
    })
    db.commit()


def _read_trade(db, trade_id: int):
    return db.execute(text("""
        SELECT id, ticker, status, exit_reason,
               crypto_broker_zero_qty_streak
        FROM trading_trades WHERE id = :id
    """), {"id": trade_id}).fetchone()


# ── Layer 1 — entry-never-filled ─────────────────────────────────────


def test_layer1_window_not_expired_no_close(db):
    """Entry placed 30 minutes ago, window is 2h -> no close."""
    fresh = datetime.utcnow() - timedelta(minutes=30)
    _seed_open_crypto_trade(
        db, trade_id=3001, ticker="BTC-USD",
        entry_date=fresh, last_fill_at=None,
    )

    res = run_crypto_stale_trade_close(db, broker_crypto_tickers=[])
    assert res["layer1_cancelled"] == 0

    row = _read_trade(db, 3001)
    assert row.status == "open"


def test_layer1_window_expired_cancels_with_reason(db):
    """Entry placed 5h ago, window is 2h -> cancel + right reason."""
    long_ago = datetime.utcnow() - timedelta(hours=5)
    _seed_open_crypto_trade(
        db, trade_id=3002, ticker="ETH-USD",
        entry_date=long_ago, last_fill_at=None,
    )

    res = run_crypto_stale_trade_close(db, broker_crypto_tickers=[])
    assert res["layer1_cancelled"] == 1
    assert 3002 in res["trade_ids"]

    row = _read_trade(db, 3002)
    assert row.status == "cancelled"
    assert row.exit_reason == CRYPTO_EXIT_REASON_ENTRY_NEVER_FILLED


def test_layer1_does_not_cancel_filled_orders(db):
    """A trade with last_fill_at set is past layer 1 — even if the
    entry_date is old, the broker did report a fill."""
    long_ago = datetime.utcnow() - timedelta(hours=5)
    _seed_open_crypto_trade(
        db, trade_id=3003, ticker="SOL-USD",
        entry_date=long_ago,
        last_fill_at=long_ago + timedelta(minutes=5),
    )

    res = run_crypto_stale_trade_close(
        db, broker_crypto_tickers=["SOL-USD"],
    )
    assert res["layer1_cancelled"] == 0

    row = _read_trade(db, 3003)
    assert row.status == "open"


# ── Layer 2 — broker-zero-qty streak ─────────────────────────────────


def test_layer2_present_resets_streak(db):
    """Broker reports the position present -> streak resets to 0."""
    long_ago = datetime.utcnow() - timedelta(hours=5)
    _seed_open_crypto_trade(
        db, trade_id=3004, ticker="DOGE-USD",
        entry_date=long_ago,
        last_fill_at=long_ago + timedelta(minutes=5),
        streak=2,  # was tracking missing
    )

    res = run_crypto_stale_trade_close(
        db, broker_crypto_tickers=["DOGE-USD"],
    )
    assert res["layer2_streak_reset"] == 1

    row = _read_trade(db, 3004)
    assert row.crypto_broker_zero_qty_streak == 0
    assert row.status == "open"


def test_layer2_absent_below_threshold_increments_no_close(db):
    """Broker reports zero, streak goes 0 -> 1, threshold is 3 ->
    no close."""
    long_ago = datetime.utcnow() - timedelta(hours=5)
    _seed_open_crypto_trade(
        db, trade_id=3005, ticker="AVAX-USD",
        entry_date=long_ago,
        last_fill_at=long_ago + timedelta(minutes=5),
        streak=0,
    )

    res = run_crypto_stale_trade_close(
        db, broker_crypto_tickers=["BTC-USD"],  # AVAX absent
    )
    assert res["layer2_streak_incremented"] == 1
    assert res["layer2_closed"] == 0

    row = _read_trade(db, 3005)
    assert row.crypto_broker_zero_qty_streak == 1
    assert row.status == "open"


def test_layer2_at_threshold_closes_with_reason(db):
    """Streak=2 going to 3 (default threshold) -> close fires with
    the brief\'s reason."""
    long_ago = datetime.utcnow() - timedelta(hours=5)
    _seed_open_crypto_trade(
        db, trade_id=3006, ticker="LINK-USD",
        entry_date=long_ago,
        last_fill_at=long_ago + timedelta(minutes=5),
        streak=2,
    )

    res = run_crypto_stale_trade_close(
        db, broker_crypto_tickers=["BTC-USD"],
    )
    assert res["layer2_closed"] == 1
    assert 3006 in res["trade_ids"]

    row = _read_trade(db, 3006)
    assert row.status == "cancelled"
    assert row.exit_reason == CRYPTO_EXIT_REASON_BROKER_ZERO_QTY


# ── Trade 1810 audit replay ──────────────────────────────────────────


def test_trade_1810_audit_replay_cancels_via_layer1(db):
    """The brief's named scenario: trade 1810 DOT-USD has been
    status=open for 7.6 days, last_fill_at IS NULL, broker reports
    zero. Layer 1 must catch it on the first sweep."""
    seven_six_days_ago = datetime.utcnow() - timedelta(days=7, hours=14)
    _seed_open_crypto_trade(
        db, trade_id=1810, ticker="DOT-USD",
        entry_date=seven_six_days_ago,
        last_fill_at=None,
        quantity=248.0,
    )

    res = run_crypto_stale_trade_close(
        db, broker_crypto_tickers=[],  # broker has no DOT
    )

    assert res["layer1_cancelled"] >= 1
    assert 1810 in res["trade_ids"]

    row = _read_trade(db, 1810)
    assert row.status == "cancelled"
    assert row.exit_reason == CRYPTO_EXIT_REASON_ENTRY_NEVER_FILLED


# ── Phase A integration ──────────────────────────────────────────────


def test_phase_a_excludes_both_new_exit_reasons():
    """The two new exit reasons MUST be in pdt_guard's frozenset so a
    crypto stale-close doesn't pollute the PDT count the way the
    pre-Phase-A equity phantoms did."""
    from app.services.trading.pdt_guard import _RECONCILE_ARTIFACT_EXIT_REASONS

    assert CRYPTO_EXIT_REASON_ENTRY_NEVER_FILLED in _RECONCILE_ARTIFACT_EXIT_REASONS
    assert CRYPTO_EXIT_REASON_BROKER_ZERO_QTY in _RECONCILE_ARTIFACT_EXIT_REASONS


# ── Equity trades not touched ────────────────────────────────────────


def test_equity_trades_unaffected(db):
    """An open equity trade (broker_source='robinhood', ticker='AAPL')
    must not be touched by the crypto sweep — wrong asset class."""
    db.execute(text("""
        INSERT INTO trading_trades (
            id, user_id, ticker, status, broker_source, direction,
            quantity, entry_price, entry_date
        ) VALUES (
            4001, NULL, 'AAPL', 'open', 'robinhood', 'long',
            1.0, 100.0, :ed
        )
        ON CONFLICT (id) DO NOTHING
    """), {"ed": datetime.utcnow() - timedelta(hours=10)})
    db.commit()

    res = run_crypto_stale_trade_close(db, broker_crypto_tickers=[])
    assert 4001 not in res["trade_ids"]

    row = _read_trade(db, 4001)
    assert row.status == "open"
