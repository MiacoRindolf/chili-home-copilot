"""Phase A of f-evidence-fidelity-architecture (2026-05-14).

Tests the canonical outcome split:

* ``learning.update_pattern_stats_from_closed_trades`` writes BOTH
  ``corrected_*`` and the legacy ``{trade_count, win_rate,
  avg_return_pct}`` columns.
* ``realized_stats_sync.sync_realized_stats`` writes ONLY
  ``raw_realized_*`` and NEVER touches legacy.
* When both writers fire in sequence (race), the legacy columns retain
  their corrected values -- no last-writer-wins.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.trading import ScanPattern, Trade
from app.services.trading.learning import update_pattern_stats_from_closed_trades
from app.services.trading.realized_stats_sync import sync_realized_stats


# ── helpers ──────────────────────────────────────────────────────────


def _make_pattern(
    db,
    *,
    name: str = "test_canonical_outcome_pattern",
    timeframe: str = "1d",
    max_bars: int = 20,
) -> ScanPattern:
    pat = ScanPattern(
        name=name,
        rules_json={"flavor": "test"},
        origin="builtin",
        asset_class="stock",
        timeframe=timeframe,
        active=True,
        trade_count=0,
        exit_config={"max_bars": max_bars},
        lifecycle_stage="candidate",
    )
    db.add(pat)
    db.flush()
    return pat


def _make_closed_trade(
    db,
    *,
    pattern_id: int,
    entry_price: float,
    exit_price: float,
    entry_offset_days: int,
    exit_offset_days: int,
    ticker: str = "TEST",
) -> Trade:
    entry_dt = datetime.utcnow() - timedelta(days=entry_offset_days)
    exit_dt = datetime.utcnow() - timedelta(days=exit_offset_days)
    pnl = (exit_price - entry_price)
    t = Trade(
        ticker=ticker,
        direction="long",
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        entry_date=entry_dt,
        exit_date=exit_dt,
        status="closed",
        pnl=pnl,
        scan_pattern_id=pattern_id,
    )
    db.add(t)
    db.flush()
    return t


# ── tests ────────────────────────────────────────────────────────────


def test_corrected_writer_writes_legacy_and_corrected(db):
    """``update_pattern_stats_from_closed_trades`` must dual-write
    corrected_* and the legacy columns, and stamp
    ``corrected_stats_updated_at``."""
    pat = _make_pattern(db, name="corrected_writer_dual_write")
    # Two winners, one loser -> wr = 2/3 ≈ 0.6667.
    _make_closed_trade(
        db,
        pattern_id=pat.id,
        entry_price=100.0,
        exit_price=110.0,
        entry_offset_days=10,
        exit_offset_days=9,
    )
    _make_closed_trade(
        db,
        pattern_id=pat.id,
        entry_price=100.0,
        exit_price=105.0,
        entry_offset_days=8,
        exit_offset_days=7,
    )
    _make_closed_trade(
        db,
        pattern_id=pat.id,
        entry_price=100.0,
        exit_price=95.0,
        entry_offset_days=6,
        exit_offset_days=5,
    )
    db.commit()

    before = datetime.utcnow()
    result = update_pattern_stats_from_closed_trades(db, user_id=None)
    db.commit()
    assert result.get("patterns_updated", 0) >= 1

    db.refresh(pat)

    # Legacy columns populated.
    assert pat.trade_count is not None and pat.trade_count > 0
    assert pat.win_rate is not None
    assert pat.avg_return_pct is not None

    # Corrected columns populated AND match legacy exactly (dual-write).
    assert pat.corrected_trade_count == pat.trade_count
    assert pat.corrected_win_rate == pat.win_rate
    assert pat.corrected_avg_return_pct == pat.avg_return_pct

    # Timestamp set.
    assert pat.corrected_stats_updated_at is not None
    assert pat.corrected_stats_updated_at >= before


def test_raw_writer_never_touches_legacy(db):
    """``sync_realized_stats`` must NEVER modify legacy columns. It
    must populate raw_realized_* only.

    Setup: seed corrected values, then fire the raw writer against
    trades whose raw stats DIFFER (no time-decay correction here, so
    raw avg_return_pct equals naïve mean). The legacy columns must
    survive unchanged.
    """
    pat = _make_pattern(db, name="raw_writer_isolated")
    # Pre-seed corrected/legacy with known values that DON'T match the
    # raw recomputation.
    pat.trade_count = 42
    pat.win_rate = 0.95
    pat.avg_return_pct = 12.34
    pat.corrected_trade_count = 42
    pat.corrected_win_rate = 0.95
    pat.corrected_avg_return_pct = 12.34
    pat.corrected_stats_updated_at = datetime.utcnow() - timedelta(days=1)
    db.add(pat)

    # Seed real trades whose recomputed stats will be VERY different.
    # 1 winner, 2 losers -> raw wr = 1/3.
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=101.0,
        entry_offset_days=10, exit_offset_days=9,
    )
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=90.0,
        entry_offset_days=8, exit_offset_days=7,
    )
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=85.0,
        entry_offset_days=6, exit_offset_days=5,
    )
    db.commit()

    result = sync_realized_stats(db, dry_run=False)
    assert result["updated"] >= 1

    db.refresh(pat)

    # Legacy columns UNCHANGED.
    assert pat.trade_count == 42
    assert pat.win_rate == 0.95
    assert pat.avg_return_pct == 12.34
    assert pat.corrected_trade_count == 42
    assert pat.corrected_win_rate == 0.95
    assert pat.corrected_avg_return_pct == 12.34

    # Raw columns populated with their own computation (3 trades, 1 win).
    assert pat.raw_realized_trade_count == 3
    assert pat.raw_realized_win_rate is not None
    assert abs(pat.raw_realized_win_rate - (1.0 / 3.0)) < 1e-6
    assert pat.raw_realized_avg_return_pct is not None
    # Avg of (+1.0%, -10.0%, -15.0%) ≈ -8.0 %.
    assert pat.raw_realized_avg_return_pct < 0.0
    assert pat.raw_realized_stats_updated_at is not None


def test_race_corrected_then_raw_leaves_legacy_corrected(db):
    """End-to-end race: fire corrected writer, snapshot legacy, fire
    raw writer with intentionally diverging trades, assert legacy
    columns equal the corrected snapshot.

    Pre-fix this was the bug pattern 585 hit -- the raw writer wrote
    last and clobbered the corrected legacy columns.
    """
    pat = _make_pattern(db, name="race_corrected_then_raw")
    # First seed of trades: 2 winners, 0 losers (corrected wr = 1.0).
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=110.0,
        entry_offset_days=10, exit_offset_days=9,
    )
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=120.0,
        entry_offset_days=8, exit_offset_days=7,
    )
    db.commit()

    update_pattern_stats_from_closed_trades(db, user_id=None)
    db.commit()
    db.refresh(pat)

    legacy_wr_after_corrected = pat.win_rate
    legacy_n_after_corrected = pat.trade_count
    legacy_ret_after_corrected = pat.avg_return_pct
    corrected_ts_after_corrected = pat.corrected_stats_updated_at

    # Corrected snapshot must be populated.
    assert pat.corrected_win_rate == legacy_wr_after_corrected
    assert pat.corrected_trade_count == legacy_n_after_corrected
    assert pat.corrected_avg_return_pct == legacy_ret_after_corrected

    # Now seed 2 more closed trades that are LOSERS. After the raw
    # writer runs, raw_realized_* will reflect the full 4-trade
    # population (2 W, 2 L) -- diverging from the 2 W corrected snapshot
    # which has not been re-run.
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=80.0,
        entry_offset_days=6, exit_offset_days=5,
    )
    _make_closed_trade(
        db, pattern_id=pat.id, entry_price=100.0, exit_price=70.0,
        entry_offset_days=4, exit_offset_days=3,
    )
    db.commit()

    sync_realized_stats(db, dry_run=False)
    db.refresh(pat)

    # Legacy columns MUST match the corrected snapshot, NOT the raw
    # recomputation. This is the load-bearing assertion.
    assert pat.win_rate == legacy_wr_after_corrected
    assert pat.trade_count == legacy_n_after_corrected
    assert pat.avg_return_pct == legacy_ret_after_corrected

    # Corrected_* untouched, timestamp unchanged.
    assert pat.corrected_win_rate == legacy_wr_after_corrected
    assert pat.corrected_trade_count == legacy_n_after_corrected
    assert pat.corrected_avg_return_pct == legacy_ret_after_corrected
    assert pat.corrected_stats_updated_at == corrected_ts_after_corrected

    # Raw_realized_* populated with the diverging 4-trade recomputation.
    assert pat.raw_realized_trade_count == 4
    assert pat.raw_realized_win_rate is not None
    assert abs(pat.raw_realized_win_rate - 0.5) < 1e-6
    assert pat.raw_realized_stats_updated_at is not None


def test_accessor_prefers_corrected_with_legacy_fallback(db):
    """``pattern_stats_accessor.get_corrected_pattern_stats`` must
    prefer corrected_* and fall back to legacy when corrected_* is
    NULL (the merge-window contract)."""
    from app.services.trading.pattern_stats_accessor import (
        get_corrected_pattern_stats,
    )

    pat = _make_pattern(db, name="accessor_fallback")

    # All NULL → all NULL, source 'missing'. (Note: ScanPattern's
    # default trade_count is 0, so explicitly None it out to exercise
    # the 'missing' branch -- the legitimate not-yet-traded state for
    # win_rate / avg_return_pct in production.)
    pat.trade_count = None
    pat.win_rate = None
    pat.avg_return_pct = None
    stats = get_corrected_pattern_stats(pat)
    assert stats.trade_count is None
    assert stats.win_rate is None
    assert stats.avg_return_pct is None
    assert stats.source_trade_count == "missing"
    assert stats.source_win_rate == "missing"
    assert stats.source_avg_return_pct == "missing"

    # Only legacy populated → legacy returned, source 'legacy'.
    pat.trade_count = 7
    pat.win_rate = 0.6
    pat.avg_return_pct = 1.5
    stats = get_corrected_pattern_stats(pat)
    assert stats.trade_count == 7
    assert abs(stats.win_rate - 0.6) < 1e-9
    assert stats.source_trade_count == "legacy"
    assert stats.source_win_rate == "legacy"

    # Corrected populated → corrected wins.
    pat.corrected_trade_count = 11
    pat.corrected_win_rate = 0.8
    pat.corrected_avg_return_pct = 2.2
    stats = get_corrected_pattern_stats(pat)
    assert stats.trade_count == 11
    assert abs(stats.win_rate - 0.8) < 1e-9
    assert stats.source_trade_count == "corrected"
    assert stats.source_win_rate == "corrected"
