"""f-equity-reconcile-partial-list-guard (2026-05-08).

Phase B audit returned Case C: 2 post-R32 phantoms in 30 days, both
single-row events with the same fingerprint (one broker_sync cycle
gap between ``last_broker_sync`` and ``exit_date``). R32 catches the
empty-list case; this brief catches the partial-list case via a
per-trade consecutive-missing-streak counter.

Test cases (per brief acceptance criteria):

  1. streak-increments-on-missing (0 → 1).
  2. streak-resets-on-presence (1 → 0).
  3. streak-below-threshold-defers-close (streak=1, N=2, time guard
     expired → no close).
  4. streak-at-threshold-allows-close (streak=2, N=2, time guard
     expired → close fires + Phase B's RECONCILE_CLOSE warning).
  5. fresh-trade-time-guard-still-fires (brand-new trade with
     last_broker_sync=NULL → time guard defers regardless of streak).
  6. JOB / PED replay: missing for 1 cycle → streak=1 → no close.
     Same trade missing in NEXT cycle → streak=2 → close fires.

Run with ``-p no:asyncio`` (workaround for the pre-existing
pytest-asyncio plugin collection failure).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services import broker_service as bs


# ── Seed helper ──────────────────────────────────────────────────────


def _seed_open_robinhood_trade(
    db,
    *,
    trade_id: int,
    ticker: str,
    last_broker_sync,
    streak: int = 0,
    entry_date=None,
) -> None:
    """Seed an open robinhood equity trade (user_id NULL to dodge the
    users FK; R32 uses ``Trade.user_id == user_id`` which becomes
    ``IS NULL`` semantics when both sides are None)."""
    if entry_date is None:
        entry_date = datetime.utcnow() - timedelta(seconds=600)
    db.execute(text("""
        INSERT INTO trading_trades (
            id, user_id, ticker, status, broker_source, direction,
            quantity, entry_price, entry_date, last_broker_sync,
            broker_sync_missing_streak
        ) VALUES (
            :id, NULL, :ticker, 'open', 'robinhood', 'long',
            1.0, 100.0, :ed, :lbs, :streak
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "ed": entry_date,
        "lbs": last_broker_sync, "streak": streak,
    })
    db.commit()


def _read_trade(db, trade_id: int):
    return db.execute(text("""
        SELECT id, ticker, status, exit_reason,
               broker_sync_missing_streak
        FROM trading_trades WHERE id = :id
    """), {"id": trade_id}).fetchone()


def _patch_broker_io(rh_tickers: list[str]):
    """Convert a list of tickers into the [{"ticker":..., "quantity":1, "average_buy_price":100}, ...]
    shape that get_positions returns; return a context-manager stack
    that mocks all broker IO."""
    positions = [
        {"ticker": t, "quantity": 1.0, "average_buy_price": 100.0}
        for t in rh_tickers
    ]
    return (
        patch.object(bs, "is_connected", return_value=True),
        patch.object(bs, "get_positions", return_value=positions),
        patch.object(bs, "get_crypto_positions", return_value=[]),
        patch.object(bs, "acquire_broker_position_sync_lock",
                     return_value=None),
        patch.object(bs, "collapse_open_broker_position_duplicates",
                     return_value={"cancelled": 0}),
    )


# ── Streak increment / reset ────────────────────────────────────────


def test_streak_increments_on_missing(db):
    """Trade missing from a non-empty rh_tickers → streak goes 0 → 1.
    Time guard prevents the close from firing this cycle."""
    long_ago = datetime.utcnow() - timedelta(seconds=600)
    _seed_open_robinhood_trade(
        db, trade_id=2001, ticker="AAPL",
        last_broker_sync=long_ago, streak=0,
    )

    # rh_tickers contains a different ticker so AAPL is "missing".
    p1, p2, p3, p4, p5 = _patch_broker_io(["MSFT"])
    with p1, p2, p3, p4, p5:
        bs.sync_positions_to_db(db, user_id=None)

    row = _read_trade(db, 2001)
    assert row.broker_sync_missing_streak == 1
    # Below threshold → trade still open.
    assert row.status == "open"


def test_streak_resets_on_presence(db):
    """Trade present in rh_tickers → streak resets 1 → 0."""
    long_ago = datetime.utcnow() - timedelta(seconds=600)
    _seed_open_robinhood_trade(
        db, trade_id=2002, ticker="AAPL",
        last_broker_sync=long_ago, streak=1,
    )

    p1, p2, p3, p4, p5 = _patch_broker_io(["AAPL"])
    with p1, p2, p3, p4, p5:
        bs.sync_positions_to_db(db, user_id=None)

    row = _read_trade(db, 2002)
    assert row.broker_sync_missing_streak == 0
    assert row.status == "open"


# ── Gate behaviour ──────────────────────────────────────────────────


def test_streak_below_threshold_defers_close(db):
    """Streak=1 (after this cycle) with N=2: even though the time
    guard has expired, the close must NOT fire."""
    long_ago = datetime.utcnow() - timedelta(seconds=600)  # past confirm window
    _seed_open_robinhood_trade(
        db, trade_id=2003, ticker="AAPL",
        last_broker_sync=long_ago, streak=0,
    )

    p1, p2, p3, p4, p5 = _patch_broker_io(["MSFT"])
    with p1, p2, p3, p4, p5, \
         patch.object(bs, "_RECONCILE_PARTIAL_LIST_STREAK_MIN", 2):
        bs.sync_positions_to_db(db, user_id=None)

    row = _read_trade(db, 2003)
    assert row.broker_sync_missing_streak == 1
    assert row.status == "open"
    assert row.exit_reason is None


def test_streak_at_threshold_allows_close(db, caplog):
    """Streak=2 with N=2 and the time guard expired: close fires and
    Phase B's RECONCILE_CLOSE warning is emitted."""
    long_ago = datetime.utcnow() - timedelta(seconds=600)
    # Pre-set streak=1 so the cycle's increment → 2 trips the gate.
    _seed_open_robinhood_trade(
        db, trade_id=2004, ticker="AAPL",
        last_broker_sync=long_ago, streak=1,
    )

    p1, p2, p3, p4, p5 = _patch_broker_io(["MSFT"])
    with p1, p2, p3, p4, p5, \
         patch.object(bs, "_RECONCILE_PARTIAL_LIST_STREAK_MIN", 2), \
         caplog.at_level("WARNING", logger="app.services.broker_service"):
        bs.sync_positions_to_db(db, user_id=None)

    row = _read_trade(db, 2004)
    assert row.status == "closed"
    assert row.exit_reason in (
        "broker_reconcile_position_gone",
        "broker_reconcile_no_exit_price",
    )
    # Phase B's structured warning fires regardless of which exit_reason
    # branch we land in.
    assert any(
        "RECONCILE_CLOSE" in rec.getMessage() and "AAPL" in rec.getMessage()
        for rec in caplog.records
    )


def test_fresh_trade_time_guard_still_fires(db):
    """Brand-new trade with last_broker_sync=NULL but recent
    entry_date: even if streak somehow reaches threshold, the time
    guard (using entry_date as fallback) defers the close."""
    fresh_entry = datetime.utcnow() - timedelta(seconds=10)
    _seed_open_robinhood_trade(
        db, trade_id=2005, ticker="AAPL",
        last_broker_sync=None,
        streak=5,  # streak alone would say "close it"
        entry_date=fresh_entry,
    )

    p1, p2, p3, p4, p5 = _patch_broker_io(["MSFT"])
    with p1, p2, p3, p4, p5, \
         patch.object(bs, "_RECONCILE_PARTIAL_LIST_STREAK_MIN", 2):
        bs.sync_positions_to_db(db, user_id=None)

    row = _read_trade(db, 2005)
    # Time guard kicks in -> trade stays open.
    assert row.status == "open"


# ── JOB / PED replay ────────────────────────────────────────────────


def test_job_ped_replay_first_cycle_defers_second_cycle_closes(db, caplog):
    """Replay of the post-R32 phantom fingerprint: position missing for
    1 cycle (streak=1) → no close; missing again next cycle (streak=2)
    → close fires."""
    long_ago = datetime.utcnow() - timedelta(seconds=600)
    _seed_open_robinhood_trade(
        db, trade_id=2006, ticker="JOB",
        last_broker_sync=long_ago, streak=0,
    )

    p1, p2, p3, p4, p5 = _patch_broker_io(["AAPL"])  # JOB missing
    # Cycle 1 -- streak goes 0 → 1; close deferred.
    with p1, p2, p3, p4, p5, \
         patch.object(bs, "_RECONCILE_PARTIAL_LIST_STREAK_MIN", 2):
        bs.sync_positions_to_db(db, user_id=None)
    row = _read_trade(db, 2006)
    assert row.broker_sync_missing_streak == 1
    assert row.status == "open"

    # Cycle 2 -- streak goes 1 → 2; gate opens; time guard still expired
    # because last_broker_sync was set long ago and the cycle 1 sync did
    # not advance it for THIS trade (the broker layer only stamps
    # last_broker_sync on present trades).
    p1, p2, p3, p4, p5 = _patch_broker_io(["AAPL"])  # JOB still missing
    with p1, p2, p3, p4, p5, \
         patch.object(bs, "_RECONCILE_PARTIAL_LIST_STREAK_MIN", 2), \
         caplog.at_level("WARNING", logger="app.services.broker_service"):
        bs.sync_positions_to_db(db, user_id=None)

    row = _read_trade(db, 2006)
    assert row.status == "closed"
    # Phase B observability fires.
    assert any(
        "RECONCILE_CLOSE" in rec.getMessage() and "JOB" in rec.getMessage()
        for rec in caplog.records
    )
