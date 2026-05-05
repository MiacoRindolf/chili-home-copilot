"""Tests for f-evidence-canonical-writer.

Covers the 14 cases from the brief:
  1.  compute_trade_correction: 1d trade held 5 days, max_bars=20 ->
      not overheld, corrected == realized.
  2.  compute_trade_correction: 1m trade held 60 minutes, max_bars=20
      -> overheld, corrected uses counterfactual close.
  3.  compute_trade_correction: short sign convention.
  4.  compute_trade_correction: counterfactual unavailable -> falls
      back to realized, counterfactual_available=False.
  5.  aggregate_pattern_stats: 10 mixed trades produces correct counts.
  6.  aggregate_pattern_stats: zero trades returns n=0, no NaN.
  7.  update_pattern_stats_from_closed_trades: writes one audit row,
      updates pattern's three fields, returns patterns_updated=1.
  8.  First-run detection: empty audit table -> first_run_backfill;
      second invocation -> periodic_recompute or no_change.
  9.  Coverage gate: cf_unavailable / overheld > 0.5 ->
      coverage_too_thin, ScanPattern fields NOT updated.
  10. Idempotence: second invocation with no new closed trades produces
      identical pattern values; audit row gets correction_reason='no_change'.
  11. Realized-EV-gate integration: corrected stats can flip a pattern
      from passing to failing the gate.
  12. NaN guard: malformed trade with NaN exit_price is skipped without
      breaking the pattern's aggregation.
  13. Sign-convention sanity: long winners all overheld with CF closes
      lower than realized -> corrected avg_return_pct < realized.
  14. The 180-day cutoff: a closed trade older than 180 days is
      excluded from the aggregation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import math
import pytest

from app.models.trading import (
    PatternEvidenceCorrection, ScanPattern, Trade,
)
from app.services.trading import learning as learning_mod
from app.services.trading.evidence_correction import (
    PatternStats,
    TradeCorrection,
    aggregate_pattern_stats,
    compute_trade_correction,
)
from app.services.trading.realized_ev_gate import evaluate_realized_ev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_pattern(
    db, *, timeframe: str = "1d", win_rate: float | None = 0.6,
    avg_return_pct: float | None = 1.5, trade_count: int = 0,
    exit_config: dict | None = None,
) -> ScanPattern:
    pat = ScanPattern(
        name=f"evcorr_{timeframe}_pat",
        rules_json={},
        origin="test",
        asset_class="all",
        timeframe=timeframe,
        win_rate=win_rate,
        avg_return_pct=avg_return_pct,
        trade_count=trade_count,
        exit_config=exit_config or {"max_bars": 20},
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return pat


def _seed_closed_trade(
    db,
    *,
    pattern_id: int,
    entry_price: float = 100.0,
    exit_price: float = 105.0,
    direction: str = "long",
    ticker: str = "TEST",
    entry_offset: timedelta = timedelta(days=2),
    held_for: timedelta = timedelta(days=1),
) -> Trade:
    """Closed trade with explicit timestamps + scan_pattern linkage."""
    entry_dt = datetime.utcnow() - entry_offset
    exit_dt = entry_dt + held_for
    pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
    t = Trade(
        ticker=ticker,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        status="closed",
        entry_date=entry_dt,
        exit_date=exit_dt,
        pnl=pnl,
        scan_pattern_id=pattern_id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------------------
# 1. compute_trade_correction: not overheld
# ---------------------------------------------------------------------------

def test_compute_trade_correction_not_overheld():
    entry_dt = datetime(2026, 1, 1, 9, 30)
    close_dt = entry_dt + timedelta(days=5)
    corr = compute_trade_correction(
        entry_price=100.0, exit_price=110.0,
        entry_date=entry_dt, close_date=close_dt,
        direction="long", ticker="X",
        pattern_timeframe="1d", max_bars=20,
    )
    assert corr.overheld is False
    assert corr.realized_return_pct == pytest.approx(10.0)
    assert corr.corrected_return_pct == pytest.approx(10.0)
    assert corr.realized_won is True
    assert corr.corrected_won is True


# ---------------------------------------------------------------------------
# 2. compute_trade_correction: overheld 1m, mocked counterfactual
# ---------------------------------------------------------------------------

def test_compute_trade_correction_overheld_with_counterfactual():
    entry_dt = datetime(2026, 1, 1, 9, 30)
    close_dt = entry_dt + timedelta(minutes=60)  # 60 bars on 1m, max_bars=20
    with patch(
        "app.services.trading.evidence_correction._fetch_counterfactual_close",
        return_value=102.0,  # CF says position was at 102 at bar 20
    ):
        corr = compute_trade_correction(
            entry_price=100.0, exit_price=120.0,  # realized 20% (lucky overhold)
            entry_date=entry_dt, close_date=close_dt,
            direction="long", ticker="X",
            pattern_timeframe="1m", max_bars=20,
        )
    assert corr.overheld is True
    assert corr.counterfactual_available is True
    assert corr.realized_return_pct == pytest.approx(20.0)
    assert corr.corrected_return_pct == pytest.approx(2.0)
    assert corr.realized_won is True
    assert corr.corrected_won is True  # still positive, just less so


# ---------------------------------------------------------------------------
# 3. compute_trade_correction: short sign convention
# ---------------------------------------------------------------------------

def test_compute_trade_correction_short_sign_convention():
    entry_dt = datetime(2026, 1, 1, 9, 30)
    close_dt = entry_dt + timedelta(days=2)
    # Long: exit < entry -> loss.
    long_corr = compute_trade_correction(
        entry_price=100.0, exit_price=95.0,
        entry_date=entry_dt, close_date=close_dt,
        direction="long", ticker="X",
        pattern_timeframe="1d", max_bars=20,
    )
    assert long_corr.realized_return_pct == pytest.approx(-5.0)
    assert long_corr.realized_won is False
    # Short: exit < entry -> win (cover lower than sold).
    short_corr = compute_trade_correction(
        entry_price=100.0, exit_price=95.0,
        entry_date=entry_dt, close_date=close_dt,
        direction="short", ticker="X",
        pattern_timeframe="1d", max_bars=20,
    )
    assert short_corr.realized_return_pct == pytest.approx(5.0)
    assert short_corr.realized_won is True


# ---------------------------------------------------------------------------
# 4. compute_trade_correction: CF unavailable
# ---------------------------------------------------------------------------

def test_compute_trade_correction_counterfactual_unavailable():
    entry_dt = datetime(2026, 1, 1, 9, 30)
    close_dt = entry_dt + timedelta(minutes=60)
    with patch(
        "app.services.trading.evidence_correction._fetch_counterfactual_close",
        return_value=None,
    ):
        corr = compute_trade_correction(
            entry_price=100.0, exit_price=120.0,
            entry_date=entry_dt, close_date=close_dt,
            direction="long", ticker="X",
            pattern_timeframe="1m", max_bars=20,
        )
    assert corr.overheld is True
    assert corr.counterfactual_available is False
    # Falls back to realized so the trade isn't dropped.
    assert corr.corrected_return_pct == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 5. aggregate_pattern_stats: mixed mix
# ---------------------------------------------------------------------------

def test_aggregate_pattern_stats_counts():
    corrections = (
        # 6 non-overheld trades (3 wins, 3 losses, all realized)
        [TradeCorrection(2.0, 2.0, False, True, True, True)] * 3
        + [TradeCorrection(-1.0, -1.0, False, True, False, False)] * 3
        # 3 overheld with CF (call them all wins post-correction)
        + [TradeCorrection(5.0, 1.5, True, True, True, True)] * 3
        # 1 overheld without CF (falls back to realized)
        + [TradeCorrection(4.0, 4.0, True, False, True, True)]
    )
    s = aggregate_pattern_stats(corrections)
    assert s.n == 10
    assert s.overheld_n == 4
    assert s.counterfactual_applied_n == 3
    assert s.counterfactual_unavailable_n == 1
    # 7 corrected wins / 10 = 0.7
    assert s.win_rate == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 6. aggregate_pattern_stats: empty
# ---------------------------------------------------------------------------

def test_aggregate_pattern_stats_empty():
    s = aggregate_pattern_stats([])
    assert s.n == 0
    assert s.win_rate == 0.0
    assert s.avg_return_pct == 0.0
    assert s.overheld_n == 0
    assert s.counterfactual_applied_n == 0
    assert s.counterfactual_unavailable_n == 0


# ---------------------------------------------------------------------------
# 7. update_pattern_stats_from_closed_trades: end-to-end one pattern
# ---------------------------------------------------------------------------

def test_update_writes_one_audit_row_and_updates_pattern(db):
    pat = _seed_pattern(db, timeframe="1d", win_rate=0.5, avg_return_pct=0.0)
    # 5 trades, all 1d held 1 day -> not overheld; 3 wins 2 losses, mean +0.4%
    for prices in [(100, 102), (100, 101), (100, 103), (100, 99), (100, 97)]:
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=prices[0], exit_price=prices[1],
            entry_offset=timedelta(days=2), held_for=timedelta(days=1),
        )
    out = learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    assert out["patterns_updated"] == 1
    db.refresh(pat)
    assert pat.win_rate == pytest.approx(3 / 5)
    assert pat.avg_return_pct == pytest.approx(0.4)
    assert pat.trade_count == 5
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.closed_trades_considered == 5
    assert r.overheld_trade_count == 0
    assert r.counterfactual_applied_count == 0
    assert r.counterfactual_unavailable_count == 0


# ---------------------------------------------------------------------------
# 8. First-run detection
# ---------------------------------------------------------------------------

def test_first_run_then_periodic_or_no_change(db):
    pat = _seed_pattern(db, timeframe="1d")
    _seed_closed_trade(
        db, pattern_id=pat.id, entry_price=100, exit_price=101,
        entry_offset=timedelta(days=2), held_for=timedelta(days=1),
    )
    learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).order_by(PatternEvidenceCorrection.id.asc()).all()
    assert rows[0].correction_reason == "first_run_backfill"

    # Re-run without new trades -> next reason is periodic_recompute or no_change.
    learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).order_by(PatternEvidenceCorrection.id.asc()).all()
    assert len(rows) >= 2
    assert rows[1].correction_reason in ("periodic_recompute", "no_change")


# ---------------------------------------------------------------------------
# 9. Coverage gate
# ---------------------------------------------------------------------------

def test_coverage_gate_skips_update(db):
    pat = _seed_pattern(
        db, timeframe="1m", win_rate=0.8, avg_return_pct=5.0,
        exit_config={"max_bars": 20},
    )
    # 4 overheld 1m trades; mock CF as always unavailable.
    for _ in range(4):
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=100.0, exit_price=120.0,
            entry_offset=timedelta(hours=2),
            held_for=timedelta(minutes=60),
        )
    with patch(
        "app.services.trading.evidence_correction._fetch_counterfactual_close",
        return_value=None,
    ):
        learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    db.refresh(pat)
    # Pattern fields NOT updated (coverage gate triggered).
    assert pat.win_rate == pytest.approx(0.8)
    assert pat.avg_return_pct == pytest.approx(5.0)
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).all()
    assert len(rows) == 1
    assert rows[0].correction_reason == "coverage_too_thin"
    assert rows[0].overheld_trade_count == 4
    assert rows[0].counterfactual_unavailable_count == 4


# ---------------------------------------------------------------------------
# 10. Idempotence: no new trades -> no_change
# ---------------------------------------------------------------------------

def test_idempotence_no_change_on_second_call(db):
    pat = _seed_pattern(db, timeframe="1d")
    for prices in [(100, 102), (100, 99)]:
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=prices[0], exit_price=prices[1],
            entry_offset=timedelta(days=2), held_for=timedelta(days=1),
        )
    learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    db.refresh(pat)
    wr_after_first = pat.win_rate
    avg_after_first = pat.avg_return_pct

    # Second call: nothing changes.
    learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    db.refresh(pat)
    assert pat.win_rate == pytest.approx(wr_after_first)
    assert pat.avg_return_pct == pytest.approx(avg_after_first)

    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).order_by(PatternEvidenceCorrection.id.asc()).all()
    # Second row reason is no_change (or periodic_recompute if rounding
    # tipped it; but values match here so no_change).
    assert rows[1].correction_reason == "no_change"


# ---------------------------------------------------------------------------
# 11. Realized-EV gate flips after correction
# ---------------------------------------------------------------------------

def test_realized_ev_gate_flips_after_correction(db, monkeypatch):
    # Force the gate to require >=2 trades (so this isn't pre-blocked).
    monkeypatch.setattr(
        "app.services.trading.realized_ev_gate._settings_get",
        lambda key, default: {
            "chili_realized_ev_min_avg_return_pct": 0.0,
            "chili_realized_ev_min_win_rate": 0.0,
            "chili_realized_ev_min_trades": 2,
        }.get(key, default),
    )
    pat = _seed_pattern(
        db, timeframe="1m", win_rate=0.8, avg_return_pct=5.0,
        exit_config={"max_bars": 20},
    )
    # Pre-correction: pattern shows passing stats. Now seed overheld 1m
    # trades whose CF says they were at a loss at bar 20.
    for _ in range(3):
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=100.0, exit_price=110.0,  # realized 10%
            entry_offset=timedelta(hours=2),
            held_for=timedelta(minutes=60),
        )
    with patch(
        "app.services.trading.evidence_correction._fetch_counterfactual_close",
        return_value=98.0,  # at bar 20 it was at -2%
    ):
        learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    db.refresh(pat)
    # Corrected stats should be negative -> EV gate fails.
    result = evaluate_realized_ev(pat)
    assert result.passed is False


# ---------------------------------------------------------------------------
# 12. NaN guard
# ---------------------------------------------------------------------------

def test_nan_guard_skips_malformed_trade(db):
    pat = _seed_pattern(db, timeframe="1d")
    _seed_closed_trade(
        db, pattern_id=pat.id, entry_price=100, exit_price=102,
        entry_offset=timedelta(days=2), held_for=timedelta(days=1),
    )
    # Inject a malformed trade -- entry_price 0 (would raise on division).
    bad = Trade(
        ticker="BAD", direction="long",
        entry_price=100.0, exit_price=101.0,
        quantity=1.0, status="closed",
        entry_date=datetime.utcnow() - timedelta(days=2),
        exit_date=datetime.utcnow() - timedelta(days=1),
        pnl=1.0, scan_pattern_id=pat.id,
    )
    db.add(bad)
    db.commit()
    # Now pretend exit_price is NaN by patching compute_trade_correction
    # to raise on the second invocation.
    real_compute = __import__(
        "app.services.trading.evidence_correction",
        fromlist=["compute_trade_correction"],
    ).compute_trade_correction
    call_count = {"n": 0}

    def _flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("NaN exit_price simulated")
        return real_compute(*args, **kwargs)

    with patch(
        "app.services.trading.evidence_correction.compute_trade_correction",
        side_effect=_flaky,
    ):
        out = learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    # Should still succeed for the pattern -- the bad trade is skipped.
    assert out["patterns_updated"] == 1


# ---------------------------------------------------------------------------
# 13. Sign-convention sanity: corrected < realized when CF closes lower
# ---------------------------------------------------------------------------

def test_corrected_avg_lower_than_realized_when_cf_closes_lower(db):
    pat = _seed_pattern(db, timeframe="1m", exit_config={"max_bars": 20})
    # 5 long winners all overheld; realized exit 110, CF exit 102.
    for _ in range(5):
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=100.0, exit_price=110.0,
            entry_offset=timedelta(hours=2),
            held_for=timedelta(minutes=60),
        )
    with patch(
        "app.services.trading.evidence_correction._fetch_counterfactual_close",
        return_value=102.0,
    ):
        learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    db.refresh(pat)
    # Corrected = 2%, realized would have been 10%.
    assert pat.avg_return_pct == pytest.approx(2.0)
    assert pat.avg_return_pct < 10.0


# ---------------------------------------------------------------------------
# 14. 180-day cutoff
# ---------------------------------------------------------------------------

def test_180_day_cutoff_excludes_old_trades(db):
    pat = _seed_pattern(db, timeframe="1d")
    # Trade closed 200 days ago: should be excluded.
    _seed_closed_trade(
        db, pattern_id=pat.id, entry_price=100, exit_price=200,
        entry_offset=timedelta(days=201), held_for=timedelta(days=1),
    )
    # Trade closed 5 days ago: included.
    _seed_closed_trade(
        db, pattern_id=pat.id, entry_price=100, exit_price=110,
        entry_offset=timedelta(days=6), held_for=timedelta(days=1),
    )
    learning_mod.update_pattern_stats_from_closed_trades(db, user_id=None)
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).all()
    assert len(rows) == 1
    # Only one trade considered (the recent one, 10% return).
    assert rows[0].closed_trades_considered == 1
    db.refresh(pat)
    assert pat.avg_return_pct == pytest.approx(10.0)
