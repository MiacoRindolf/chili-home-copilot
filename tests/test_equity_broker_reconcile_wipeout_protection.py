"""f-equity-broker-reconcile-wipeout-protection (2026-05-08).

Always-on deliverables (ship regardless of audit case):

  1. R32 empty-broker-positions guard pinned: when ``get_positions()``
     returns an empty list while local trades remain open, the
     reconciler MUST refuse to mass-close. Return shape carries
     ``skipped_reason='empty_broker_positions_with_open_local_trades'``;
     open trades stay open. This is the regression that the original
     incident on 2026-04-30 manufactured 14 phantom rows from.

  2. Wipeout-burst breaker trip cardinality logic:
     ``_record_reconcile_close_burst`` sees three closes in a single
     5-second bucket and trips the drawdown breaker exactly once via
     ``_persist_breaker_state(True, 'wipeout_burst_3_in_5s')``.
     Tested via the ``_now`` and ``_breaker_persister`` injection
     seams so no DB / no real wall clock.

Run with ``-p no:asyncio`` (workaround for pre-existing pytest-asyncio
plugin collection failure; same workaround as in
tests/test_pdt_count_broker_confirmed_only.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.services import broker_service as bs


# ── R32 regression test ──────────────────────────────────────────────


def _seed_open_equity_trade(db, *, trade_id: int, ticker: str) -> None:
    """Seed an open robinhood equity trade outside the confirm window so
    R32's empty-list guard is the SOLE reason for skip behaviour.

    ``user_id`` left NULL to avoid the users-table FK; R32's guard
    matches via ``Trade.user_id == user_id`` which becomes
    ``user_id IS NULL`` semantics in SQLAlchemy when user_id is None,
    so the open-count filter still finds these rows.
    """
    long_ago = datetime.utcnow() - timedelta(seconds=600)
    db.execute(text("""
        INSERT INTO trading_trades (
            id, user_id, ticker, status, broker_source, direction,
            quantity, entry_price, entry_date, last_broker_sync
        ) VALUES (
            :id, NULL, :ticker, 'open', 'robinhood', 'long',
            1.0, 100.0, :ed, :lbs
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "ed": long_ago, "lbs": long_ago,
    })
    db.commit()


def test_r32_empty_broker_positions_guard_skips_mass_close(db):
    """The 2026-04-30 wipeout regression: when get_positions() returns
    [] (broker auth flap, transient API failure, etc.) and local
    trades are still open, the reconciler must refuse to close them.

    Pre-R32 behaviour was a mass-close cascade. Post-R32 the function
    returns early with skipped_reason set.
    """
    _seed_open_equity_trade(db, trade_id=1001, ticker="AAPL")
    _seed_open_equity_trade(db, trade_id=1002, ticker="MSFT")

    with patch.object(bs, "is_connected", return_value=True), \
         patch.object(bs, "get_positions", return_value=[]), \
         patch.object(bs, "get_crypto_positions", return_value=[]), \
         patch.object(bs, "acquire_broker_position_sync_lock", return_value=None), \
         patch.object(
             bs, "collapse_open_broker_position_duplicates",
             return_value={"cancelled": 0},
         ):
        result = bs.sync_positions_to_db(db, user_id=None)

    # Pin the brief's expected return shape.
    assert result.get("skipped_reason") == \
        "empty_broker_positions_with_open_local_trades"
    assert result.get("closed") == 0

    # Open trades stay open — the wipeout did NOT happen.
    rows = db.execute(text("""
        SELECT id, status, exit_reason FROM trading_trades
        WHERE id IN (1001, 1002)
        ORDER BY id
    """)).fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r.status == "open", (
            f"trade {r.id} was closed by R32-bypass; exit_reason={r.exit_reason!r}"
        )


# ── Wipeout-burst cardinality tests ──────────────────────────────────


@pytest.fixture()
def reset_burst_state():
    """Clear the module-level burst counters before each test so we
    don't bleed state across runs."""
    bs._wipeout_burst_buckets.clear()
    bs._wipeout_burst_tripped_buckets.clear()
    bs._RECONCILE_CLOSE_TOTAL = 0
    yield
    bs._wipeout_burst_buckets.clear()
    bs._wipeout_burst_tripped_buckets.clear()


def test_burst_under_threshold_does_not_trip(reset_burst_state):
    """Two closes in one 5-s bucket is normal cleanup, not a wipeout.
    The breaker must NOT trip."""
    persister = MagicMock()
    bs._record_reconcile_close_burst(
        "AAPL", 1, _now=100.0, _breaker_persister=persister,
    )
    bs._record_reconcile_close_burst(
        "MSFT", 2, _now=100.5, _breaker_persister=persister,
    )
    persister.assert_not_called()
    assert bs._RECONCILE_CLOSE_TOTAL == 2


def test_burst_at_threshold_trips_breaker_once(reset_burst_state):
    """Three closes in one bucket trips the breaker exactly once with
    the brief-specified reason string."""
    persister = MagicMock()
    bs._record_reconcile_close_burst(
        "AAPL", 1, _now=100.0, _breaker_persister=persister,
    )
    bs._record_reconcile_close_burst(
        "MSFT", 2, _now=100.5, _breaker_persister=persister,
    )
    bs._record_reconcile_close_burst(
        "NVDA", 3, _now=101.0, _breaker_persister=persister,
    )
    assert persister.call_count == 1
    args = persister.call_args.args
    assert args[0] is True
    assert args[1] == "wipeout_burst_3_in_5s"


def test_burst_above_threshold_does_not_re_trip_same_bucket(
    reset_burst_state,
):
    """Once a bucket trips, additional closes in the SAME bucket must
    not re-call the persister (idempotency on the burst signal —
    operator should see one trip, not five)."""
    persister = MagicMock()
    for i in range(6):
        bs._record_reconcile_close_burst(
            f"T{i}", i, _now=100.0 + i * 0.4,
            _breaker_persister=persister,
        )
    assert persister.call_count == 1


def test_burst_resets_in_new_bucket(reset_burst_state):
    """A burst in bucket N must not block tripping in bucket N+1
    (each bucket is independent — the operator's reset is implicit
    across windows)."""
    persister = MagicMock()
    # Bucket 1 = floor(100.0 / 5) = 20 → 3 closes
    for i, t in enumerate((100.0, 100.5, 101.0)):
        bs._record_reconcile_close_burst(
            f"BUCKET1-{i}", i, _now=t, _breaker_persister=persister,
        )
    # Bucket 2 = floor(110.0 / 5) = 22 → 3 closes (skip bucket 21 at 105–110s)
    for i, t in enumerate((110.0, 110.5, 111.0)):
        bs._record_reconcile_close_burst(
            f"BUCKET2-{i}", i + 100, _now=t,
            _breaker_persister=persister,
        )
    assert persister.call_count == 2


def test_burst_gc_drops_old_buckets(reset_burst_state):
    """The bucket dict is bounded by ``_WIPEOUT_BURST_BUCKET_RETENTION_S``.
    A bucket older than retention must be GC'd so the dict can't grow
    without bound under a long-running broker-sync worker."""
    persister = MagicMock()
    bs._record_reconcile_close_burst(
        "OLD", 1, _now=100.0, _breaker_persister=persister,
    )
    # 900s later — way past the 300s retention.
    bs._record_reconcile_close_burst(
        "NEW", 2, _now=1000.0, _breaker_persister=persister,
    )
    # Only the NEW bucket should remain.
    assert len(bs._wipeout_burst_buckets) == 1
