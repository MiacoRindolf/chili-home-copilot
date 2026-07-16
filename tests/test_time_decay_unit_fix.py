"""Tests for f-time-decay-unit-fix.

Covers the 10 cases from the brief:
  1-3. ``timeframe_to_seconds`` happy + invalid.
  4-6. ``_compute_bars_held`` at 1d / 1m / 1h timeframes.
  7.   ``_compute_bars_held`` orphan trade (no scan_pattern_id) -> 1d fallback.
  8.   ``_compute_bars_held`` unknown timeframe -> 1d fallback + WARNING.
  9.   Integration: 1m position fires ``exit_time_decay`` after 21 minutes.
  10.  Regression: result key is ``bars_held``, not the legacy ``days_held``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from app.models.trading import PaperTrade, ScanPattern
from app.services.trading import live_exit_engine as lee
from app.services.trading.timeframe_utils import (
    canonical_interval_for_seconds,
    known_timeframes,
    timeframe_to_seconds,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_pattern(db, *, timeframe: str = "1d") -> ScanPattern:
    # win_rate is bounded [0.0, 1.0] by chk_scan_patterns_win_rate_range
    # (it stores a fraction, not a percentage). avg_return_pct is unbounded.
    pat = ScanPattern(
        name=f"tf_{timeframe}_pattern",
        rules_json={},
        origin="test",
        asset_class="all",
        timeframe=timeframe,
        win_rate=0.5,
        avg_return_pct=1.0,
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return pat


def _seed_paper_trade(
    db,
    *,
    scan_pattern_id: int | None,
    entry_offset: timedelta,
    entry_price: float = 100.0,
    stop_price: float = 95.0,
) -> PaperTrade:
    pt = PaperTrade(
        ticker="TEST",
        direction="long",
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=entry_price * 1.10,
        quantity=10.0,
        status="open",
        entry_date=datetime.utcnow() - entry_offset,
        scan_pattern_id=scan_pattern_id,
    )
    db.add(pt)
    db.commit()
    db.refresh(pt)
    return pt


def _stub_external_market_data(monkeypatch, atr_value: float = 1.0) -> None:
    """Replace fetch_ohlcv_df + compute_atr so compute_live_exit_levels runs offline."""
    import pandas as pd

    def _fake_fetch_ohlcv_df(ticker, period=None, interval=None, start=None, end=None):
        idx = pd.date_range("2026-01-01", periods=30, freq="D")
        return pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1_000_000},
            index=idx,
        )

    def _fake_compute_atr(highs, lows, closes, period=14):
        import numpy as np
        return np.array([atr_value] * len(closes))

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        _fake_fetch_ohlcv_df,
    )
    monkeypatch.setattr(
        "app.services.trading.indicator_core.compute_atr",
        _fake_compute_atr,
    )


# ---------------------------------------------------------------------------
# 1-3. timeframe_to_seconds
# ---------------------------------------------------------------------------

def test_timeframe_to_seconds_1d():
    assert timeframe_to_seconds("1d") == 86400


def test_timeframe_to_seconds_1m():
    assert timeframe_to_seconds("1m") == 60


def test_timeframe_to_seconds_unknown_raises():
    with pytest.raises(ValueError, match="Unknown timeframe"):
        timeframe_to_seconds("13h")


def test_canonical_interval_uses_production_minute_spelling():
    assert canonical_interval_for_seconds(60) == "1m"
    assert canonical_interval_for_seconds(15) == "15s"


def test_known_timeframes_covers_production_survey():
    """Survey at fix-time saw 1m/5m/15m/1h/4h/1d in production. All must
    be in the allowed list so the CHECK constraint doesn't reject any.
    """
    allowed = set(known_timeframes())
    for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
        assert tf in allowed, f"Production timeframe {tf!r} missing from allowed list"


# ---------------------------------------------------------------------------
# 4-6. _compute_bars_held at different timeframes
# ---------------------------------------------------------------------------

def test_compute_bars_held_1d_5_days(db):
    pat = _seed_pattern(db, timeframe="1d")
    pt = _seed_paper_trade(db, scan_pattern_id=pat.id, entry_offset=timedelta(days=5))
    assert lee._compute_bars_held(db, pt) == 5


def test_compute_bars_held_1m_100_minutes(db):
    pat = _seed_pattern(db, timeframe="1m")
    pt = _seed_paper_trade(db, scan_pattern_id=pat.id, entry_offset=timedelta(minutes=100))
    assert lee._compute_bars_held(db, pt) == 100


def test_compute_bars_held_1h_2_hours(db):
    pat = _seed_pattern(db, timeframe="1h")
    pt = _seed_paper_trade(db, scan_pattern_id=pat.id, entry_offset=timedelta(seconds=7200))
    assert lee._compute_bars_held(db, pt) == 2


# ---------------------------------------------------------------------------
# 7. Orphan trade (no scan_pattern_id) -> 1d fallback
# ---------------------------------------------------------------------------

def test_compute_bars_held_orphan_falls_back_to_1d(db):
    pt = _seed_paper_trade(
        db, scan_pattern_id=None, entry_offset=timedelta(days=3),
    )
    # 3 days at the 1d fallback -> 3 bars.
    assert lee._compute_bars_held(db, pt) == 3


# ---------------------------------------------------------------------------
# 8. Unknown timeframe -> 1d fallback + WARNING
# ---------------------------------------------------------------------------

def test_compute_bars_held_unknown_timeframe_warns_and_falls_back(db, caplog, monkeypatch):
    """When a timeframe value reaches the helper but isn't known to
    ``timeframe_utils``, the helper logs WARNING and falls back to 1d.

    This path matters even though migration 227 adds a CHECK constraint
    on ``scan_patterns.timeframe``: the helper runs in pre-migration
    environments, in tests, and as a defensive shield if a future
    timeframe value gets allowed at the SQL layer before the helper's
    map is updated. Mutating the row directly would now hit the CHECK,
    so we monkeypatch the converter to simulate an unknown value.
    """
    pat = _seed_pattern(db, timeframe="1d")
    pt = _seed_paper_trade(db, scan_pattern_id=pat.id, entry_offset=timedelta(days=2))

    def _raise_for_anything(_tf):
        raise ValueError("simulated unknown timeframe")

    monkeypatch.setattr(
        "app.services.trading.timeframe_utils.timeframe_to_seconds",
        _raise_for_anything,
    )
    with caplog.at_level(logging.WARNING, logger="app.services.trading.live_exit_engine"):
        bars = lee._compute_bars_held(db, pt)
    # Falls back to 1d -> 2 bars after 2 days.
    assert bars == 2
    assert any("Unknown timeframe" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 9. Integration: 1m position fires exit_time_decay after 21 minutes
# ---------------------------------------------------------------------------

def test_compute_live_exit_levels_time_decay_fires_on_1m_after_21_min(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    pat = _seed_pattern(db, timeframe="1m")
    pt = _seed_paper_trade(
        db, scan_pattern_id=pat.id, entry_offset=timedelta(minutes=21),
    )
    # Ensure no terminal exit fires from stop / target / BOS by widening
    # the targets and disabling BOS.
    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["max_bars"] = 20
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)
    # Price between stop and target so no terminal hits.
    result = lee.compute_live_exit_levels(db, pt, current_price=100.0)
    assert result["action"] == "exit_time_decay", result
    assert result["bars_held"] == 21


def test_compute_live_exit_levels_time_decay_does_not_fire_on_1d_after_21_min(
    db, monkeypatch,
):
    """Same elapsed wall-clock but 1d timeframe -> bars_held == 0 -> hold."""
    _stub_external_market_data(monkeypatch)
    pat = _seed_pattern(db, timeframe="1d")
    pt = _seed_paper_trade(
        db, scan_pattern_id=pat.id, entry_offset=timedelta(minutes=21),
    )
    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["max_bars"] = 20
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)
    result = lee.compute_live_exit_levels(db, pt, current_price=100.0)
    assert result["action"] == "hold"


# ---------------------------------------------------------------------------
# 10. Regression: result["bars_held"] (not legacy days_held)
# ---------------------------------------------------------------------------

def test_result_uses_bars_held_key_not_days_held(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    pat = _seed_pattern(db, timeframe="1d")
    pt = _seed_paper_trade(
        db, scan_pattern_id=pat.id, entry_offset=timedelta(days=25),
    )
    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["max_bars"] = 20
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)
    result = lee.compute_live_exit_levels(db, pt, current_price=100.0)
    assert result["action"] == "exit_time_decay"
    assert "bars_held" in result
    assert "days_held" not in result
    assert result["bars_held"] == 25
