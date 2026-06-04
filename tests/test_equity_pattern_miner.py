"""Unit tests for the equity-native pattern miner.

Pure-logic tests: timeframe derivation, adaptive exit-config seeding, and the
safe default-off behavior. End-to-end discovery against live data is validated
out-of-band (read-only) since it depends on accumulated trade history.
"""
from app.services.trading import equity_pattern_miner as m


def test_equity_timeframe_for_hold_intraday_vs_swing():
    assert m._equity_timeframe_for_hold(0.5) == "1h"
    assert m._equity_timeframe_for_hold(3.9) == "1h"
    assert m._equity_timeframe_for_hold(4.0) == "1d"
    assert m._equity_timeframe_for_hold(44.0) == "1d"


def test_adaptive_exit_config_derives_max_bars_from_observed_holds():
    # 1d bars: p75 of holds (hours) / 24, floored at 3 bars.
    group = [{"hold_hours": 24.0, "pnl": 1.0},
             {"hold_hours": 48.0, "pnl": 1.0},
             {"hold_hours": 72.0, "pnl": 1.0}]
    cfg = m._adaptive_exit_config(group, timeframe="1d", parent_exit_config={})
    assert cfg["max_bars"] == 3  # ceil(72/24)
    assert cfg["exit_seed_source"] == "equity_miner_adaptive"

    # 1h bars: same holds expressed in 1h units → far more bars.
    cfg_h = m._adaptive_exit_config(group, timeframe="1h", parent_exit_config={})
    assert cfg_h["max_bars"] == 72  # ceil(72/1)


def test_adaptive_exit_config_floor_and_parent_inheritance():
    # Tiny holds still floor at 3 bars (never zero/negative).
    short = [{"hold_hours": 0.1, "pnl": 1.0}]
    floored = m._adaptive_exit_config(short, timeframe="1d", parent_exit_config={})
    assert floored["max_bars"] == 3

    # A proven parent exit policy is inherited, not overwritten.
    parent = {"atr_mult": 2.25, "target_r_multiple": 4.0, "use_bos": True}
    inherited = m._adaptive_exit_config(short, timeframe="1d", parent_exit_config=parent)
    assert inherited["atr_mult"] == 2.25
    assert inherited["target_r_multiple"] == 4.0
    assert inherited["use_bos"] is True

    # No parent → established system fallbacks (not new magic numbers).
    fb = m._adaptive_exit_config(short, timeframe="1d", parent_exit_config={})
    assert fb["atr_mult"] == m._FALLBACK_ATR_MULT
    assert fb["target_r_multiple"] == m._FALLBACK_TARGET_R


def test_miner_is_dormant_by_default(monkeypatch):
    """Ships disabled: returns skipped without touching the session."""
    from app.config import settings

    monkeypatch.setattr(settings, "brain_equity_miner_enabled", False, raising=False)
    # Session is unused on the disabled path, so None is safe.
    result = m.run_equity_pattern_miner(None)
    assert result == {"skipped": True, "reason": "disabled"}
