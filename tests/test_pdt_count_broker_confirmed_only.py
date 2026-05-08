"""f-pdt-count-broker-confirmed-only (2026-05-08).

Pin the SQL filter so `_count_day_trades_5d` counts ONLY broker-
confirmed day-trades:

  * `broker_order_id IS NOT NULL`
  * `last_fill_at IS NOT NULL`
  * `exit_reason NOT IN`
    `_RECONCILE_ARTIFACT_EXIT_REASONS`

Operator audit 2026-05-08: 14 PDT-counted trades were chili
synthesizing closes when the equity reconciler couldn't find
positions at the broker (broker_order_id NULL, last_fill_at NULL,
exit_reason='broker_reconcile_position_gone'). R31/R32 (commits
539e1c2 + 7af3d49, 2026-04-30) fixed this for crypto; this brief
filters them out of the equity PDT count so the operator's account
stops self-locking.

Tests use the chili_test conftest db fixture. Run with
``-p no:asyncio`` (workaround for pre-existing pytest-asyncio plugin
collection failure; same workaround as in
tests/test_bracket_writer_cover_policy_clarify.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.services.trading.pdt_guard import (
    _RECONCILE_ARTIFACT_EXIT_REASONS,
    _count_day_trades_5d,
)


# ── Seed helpers ──────────────────────────────────────────────────────


_UNSET = object()


def _seed_trade(
    db,
    *,
    trade_id: int,
    ticker: str = "AAPL",
    status: str = "closed",
    direction: str = "long",
    entry_price: float = 100.0,
    quantity: float = 1.0,
    entry_at: datetime | None = None,
    exit_at: datetime | None = None,
    broker_order_id: str | None = "BO-1",
    last_fill_at=_UNSET,
    exit_reason: str | None = "stop_filled",
) -> None:
    """Seed a single trading_trades row.

    Defaults paint a realistic broker-confirmed same-day round-trip:
    entry + exit on the same day, broker_order_id present, last_fill_at
    populated, ordinary exit_reason. Override any field per scenario.

    ``last_fill_at`` uses a sentinel so callers can explicitly pass
    ``None`` to seed a phantom row (broker_order_id present but no
    fill recorded) without the helper auto-filling it.
    """
    if entry_at is None:
        entry_at = datetime.utcnow() - timedelta(hours=4)
    if exit_at is None:
        # Same calendar day as entry_at.
        exit_at = entry_at + timedelta(hours=2)
    if last_fill_at is _UNSET:
        # Default: mirror what a real broker fill produces (last_fill_at
        # ≈ exit_at) iff broker_order_id is set; otherwise stay None.
        last_fill_at = exit_at if broker_order_id is not None else None

    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, direction, quantity,
            entry_price, entry_date, exit_date,
            broker_order_id, last_fill_at, exit_reason
        ) VALUES (
            :id, :ticker, :status, :direction, :quantity,
            :entry_price, :entry_at, :exit_at,
            :boid, :lfa, :ereason
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "status": status,
        "direction": direction, "quantity": quantity,
        "entry_price": entry_price,
        "entry_at": entry_at, "exit_at": exit_at,
        "boid": broker_order_id, "lfa": last_fill_at,
        "ereason": exit_reason,
    })
    db.commit()


# ── Module-level constant pinned ──────────────────────────────────────


def test_reconcile_artifact_exit_reasons_constant_shape():
    """The brief locks the two reasons that mark reconcile artifacts.
    Adding more is fine; removing either silently masks the operator's
    audit findings -- catch it here."""
    assert "broker_reconcile_position_gone" in _RECONCILE_ARTIFACT_EXIT_REASONS
    assert "forced_unwind_reconcile" in _RECONCILE_ARTIFACT_EXIT_REASONS
    # Frozenset, not a list/tuple — immutability is part of the contract.
    assert isinstance(_RECONCILE_ARTIFACT_EXIT_REASONS, frozenset)


# ── Real broker-confirmed same-day round-trip → counted ───────────────


def test_real_broker_confirmed_round_trip_is_counted(db):
    _seed_trade(db, trade_id=1, ticker="AAPL")  # all defaults = real
    n = _count_day_trades_5d(db)
    assert n == 1


# ── broker_order_id IS NULL → not counted ─────────────────────────────


def test_broker_order_id_null_is_not_counted(db):
    _seed_trade(
        db, trade_id=2, ticker="MSFT",
        broker_order_id=None,
        # last_fill_at default would be None too if boid is None; force
        # it to non-null so we isolate the broker_order_id filter.
        last_fill_at=datetime.utcnow() - timedelta(hours=1),
    )
    n = _count_day_trades_5d(db)
    assert n == 0


# ── last_fill_at IS NULL → not counted ────────────────────────────────


def test_last_fill_at_null_is_not_counted(db):
    _seed_trade(
        db, trade_id=3, ticker="TSLA",
        broker_order_id="BO-T",
        last_fill_at=None,
    )
    n = _count_day_trades_5d(db)
    assert n == 0


# ── exit_reason in reconcile-artifact set → not counted ───────────────


def test_broker_reconcile_position_gone_is_not_counted(db):
    _seed_trade(
        db, trade_id=4, ticker="NVDA",
        broker_order_id="BO-N",
        last_fill_at=datetime.utcnow() - timedelta(hours=1),
        exit_reason="broker_reconcile_position_gone",
    )
    n = _count_day_trades_5d(db)
    assert n == 0


def test_forced_unwind_reconcile_is_not_counted(db):
    _seed_trade(
        db, trade_id=5, ticker="AMD",
        broker_order_id="BO-A",
        last_fill_at=datetime.utcnow() - timedelta(hours=1),
        exit_reason="forced_unwind_reconcile",
    )
    n = _count_day_trades_5d(db)
    assert n == 0


# ── Mixed: real + 4 artifacts → only real counted ─────────────────────


def test_mixed_real_and_artifacts_counts_only_real(db):
    """Models the 2026-05-08 audit shape: 14 phantom rows + a few real
    day-trades. The filter must keep only the broker-confirmed ones.
    """
    # 1 real
    _seed_trade(db, trade_id=10, ticker="AAPL")
    # 4 artifacts of various flavors
    _seed_trade(
        db, trade_id=11, ticker="MSFT",
        broker_order_id=None,
        last_fill_at=datetime.utcnow() - timedelta(hours=1),
    )
    _seed_trade(
        db, trade_id=12, ticker="TSLA",
        broker_order_id="BO-T", last_fill_at=None,
    )
    _seed_trade(
        db, trade_id=13, ticker="NVDA",
        broker_order_id="BO-N",
        last_fill_at=datetime.utcnow() - timedelta(hours=1),
        exit_reason="broker_reconcile_position_gone",
    )
    _seed_trade(
        db, trade_id=14, ticker="AMD",
        broker_order_id="BO-A",
        last_fill_at=datetime.utcnow() - timedelta(hours=1),
        exit_reason="forced_unwind_reconcile",
    )

    n = _count_day_trades_5d(db)
    assert n == 1


# ── Crypto bypass still works (R35) ───────────────────────────────────


def test_crypto_ticker_still_excluded(db):
    """The pre-existing R35 crypto bypass (ticker NOT LIKE '%-USD')
    must remain — this brief is purely additive."""
    _seed_trade(db, trade_id=20, ticker="BTC-USD")  # broker-confirmed
    n = _count_day_trades_5d(db)
    assert n == 0


# ── Cutoff window still respected ─────────────────────────────────────


def test_old_round_trip_outside_window_not_counted(db):
    """A real, broker-confirmed round-trip from 30 days ago is outside
    the 9-calendar-day lookback window and must not be counted."""
    long_ago = datetime.utcnow() - timedelta(days=30)
    _seed_trade(
        db, trade_id=30, ticker="AAPL",
        entry_at=long_ago, exit_at=long_ago + timedelta(hours=2),
        last_fill_at=long_ago + timedelta(hours=2),
    )
    n = _count_day_trades_5d(db)
    assert n == 0


# ── Multi-day-trade aggregation ───────────────────────────────────────


def test_three_real_round_trips_counted_as_three(db):
    """Three real broker-confirmed same-day round-trips on different
    days within the window should aggregate to 3 — the SEC threshold
    is 4-in-5-business-days, so 3 still allows entries."""
    base = datetime.utcnow() - timedelta(days=2, hours=5)
    for i, sym in enumerate(("AAPL", "MSFT", "NVDA"), start=40):
        entry = base + timedelta(days=(i - 40))
        _seed_trade(
            db, trade_id=i, ticker=sym,
            entry_at=entry, exit_at=entry + timedelta(hours=1),
            last_fill_at=entry + timedelta(hours=1),
            broker_order_id=f"BO-{sym}",
        )
    n = _count_day_trades_5d(db)
    assert n == 3
