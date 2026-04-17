"""Phase B unit tests: pure ExitEvaluator contract.

These tests pin down the frozen priority
(stop -> target -> BOS -> time_decay -> trail -> partial) and the
monotonic-trail invariant. They do not touch the DB, the adapters, or the
shadow hooks; those are covered by the parity tests.
"""

from __future__ import annotations

import pytest

from app.services.trading import exit_evaluator as ev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state_long(
    entry: float = 100.0,
    stop: float | None = 97.0,
    target: float | None = 106.0,
    bars_held: int = 0,
    highest: float | None = None,
    trail: float | None = None,
    partial_taken: bool = False,
) -> ev.PositionState:
    return ev.PositionState(
        direction="long",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        bars_held=bars_held,
        highest_since_entry=highest if highest is not None else entry,
        lowest_since_entry=entry,
        trailing_stop=trail,
        partial_taken=partial_taken,
    )


def _bar(close: float, high: float | None = None, low: float | None = None,
         atr: float | None = 1.0, swing_low: float | None = None,
         swing_high: float | None = None) -> ev.BarContext:
    return ev.BarContext(
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        atr=atr,
        swing_low=swing_low,
        swing_high=swing_high,
    )


# ---------------------------------------------------------------------------
# ExitConfig / hash
# ---------------------------------------------------------------------------

def test_exit_config_hash_stable_and_sensitive():
    a = ev.ExitConfig(trail_atr_mult=2.0, max_bars=20, use_bos=True)
    b = ev.ExitConfig(trail_atr_mult=2.0, max_bars=20, use_bos=True)
    c = ev.ExitConfig(trail_atr_mult=1.5, max_bars=20, use_bos=True)
    assert a.config_hash() == b.config_hash()
    assert a.config_hash() != c.config_hash()
    assert len(a.config_hash()) == 16


def test_build_config_live_defaults_map_legacy():
    cfg = ev.build_config_live(None)
    assert cfg.hard_stop_enabled is True
    assert cfg.hard_target_enabled is True
    # Legacy ``compute_live_exit_levels`` computes a trail value for reporting
    # but never closes on it; live flavor deliberately disables the trail
    # close rule for parity.
    assert cfg.trail_atr_mult is None
    assert cfg.max_bars == 20
    assert cfg.use_bos is True
    # Legacy stores percent (0.5) — evaluator wants a fraction (0.005).
    assert cfg.bos_buffer_frac == pytest.approx(0.005)
    assert cfg.bos_grace_bars == 0
    assert cfg.trail_monotonic is True


def test_build_config_backtest_trail_is_non_monotonic_for_legacy_parity():
    cfg = ev.build_config_backtest()
    assert cfg.trail_monotonic is False


def test_build_config_backtest_defaults_map_dynamic_pattern_strategy():
    cfg = ev.build_config_backtest()
    assert cfg.hard_stop_enabled is False
    assert cfg.hard_target_enabled is False
    assert cfg.trail_atr_mult == pytest.approx(2.0)
    assert cfg.max_bars == 20
    assert cfg.use_bos is True
    assert cfg.bos_buffer_frac == pytest.approx(0.003)
    assert cfg.bos_grace_bars == 3


# ---------------------------------------------------------------------------
# Rule 1: hard stop (live only)
# ---------------------------------------------------------------------------

def test_hard_stop_fires_on_low_breach_long():
    cfg = ev.build_config_live(None)
    state = _state_long(entry=100.0, stop=97.0, target=110.0)
    bar = _bar(close=98.0, low=96.5, high=99.0, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_STOP
    assert out.exit_price == pytest.approx(97.0)
    assert out.reason_code == "hard_stop"
    assert out.r_multiple == pytest.approx(-1.0)


def test_hard_stop_does_not_fire_in_backtest_config():
    cfg = ev.build_config_backtest(exit_atr_mult=2.0, exit_max_bars=20, use_bos=False)
    state = _state_long(entry=100.0, stop=97.0, target=110.0)
    bar = _bar(close=96.5, low=96.5, high=97.2, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action != ev.EXIT_ACTION_EXIT_STOP


# ---------------------------------------------------------------------------
# Rule 2: hard target (live only)
# ---------------------------------------------------------------------------

def test_hard_target_fires_on_high_touch_long():
    cfg = ev.build_config_live(None)
    state = _state_long(entry=100.0, stop=97.0, target=106.0)
    bar = _bar(close=105.5, low=104.0, high=106.2, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_TARGET
    assert out.exit_price == pytest.approx(106.0)
    assert out.reason_code == "hard_target"
    assert out.r_multiple == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Rule 3: BOS + grace + buffer
# ---------------------------------------------------------------------------

def test_bos_fires_long_below_swing_with_buffer():
    cfg = ev.build_config_backtest(exit_atr_mult=5.0, exit_max_bars=100,
                                    use_bos=True, bos_buffer_frac=0.003,
                                    bos_grace_bars=3)
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=5, highest=102.0, trail=None)
    # swing_low=99. BOS level = 99 * (1 - 0.003) = 98.703. Close < that -> BOS.
    bar = _bar(close=98.5, low=98.3, high=99.1, atr=0.5, swing_low=99.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_BOS
    assert out.reason_code == "bos_long"
    assert out.exit_price == pytest.approx(98.5)


def test_bos_suppressed_inside_grace_period():
    cfg = ev.build_config_backtest(exit_atr_mult=5.0, exit_max_bars=100,
                                    use_bos=True, bos_buffer_frac=0.003,
                                    bos_grace_bars=6)
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=3, highest=101.0, trail=None)
    bar = _bar(close=98.5, low=98.5, high=99.0, atr=0.5, swing_low=99.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action != ev.EXIT_ACTION_EXIT_BOS


def test_bos_suppressed_when_close_above_level():
    cfg = ev.build_config_backtest(exit_atr_mult=5.0, exit_max_bars=100,
                                    use_bos=True, bos_buffer_frac=0.003,
                                    bos_grace_bars=3)
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=5, highest=102.0, trail=None)
    # bos_level = 99 * 0.997 = 98.703. Close at 98.9 is ABOVE level -> no BOS.
    bar = _bar(close=98.9, low=98.9, high=100.0, atr=0.5, swing_low=99.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action != ev.EXIT_ACTION_EXIT_BOS


# ---------------------------------------------------------------------------
# Rule 4: time decay / max_bars
# ---------------------------------------------------------------------------

def test_time_decay_fires_at_max_bars():
    cfg = ev.build_config_backtest(exit_atr_mult=5.0, exit_max_bars=10,
                                    use_bos=False)
    # bars_held will become 10 after evaluate_bar increments.
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=9, highest=101.0, trail=None)
    bar = _bar(close=100.5, atr=0.2)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_TIME_DECAY
    assert out.reason_code == "max_bars"
    assert out.exit_price == pytest.approx(100.5)


# ---------------------------------------------------------------------------
# Rule 5: trailing stop + monotonicity
# ---------------------------------------------------------------------------

def test_trail_fires_when_close_below_trailing_stop_long():
    cfg = ev.build_config_backtest(exit_atr_mult=2.0, exit_max_bars=100,
                                    use_bos=False)
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=5, highest=110.0, trail=None)
    # Recomputed trail = 110 - 2*1 = 108. Close 107.5 < 108 -> trail exit.
    bar = _bar(close=107.5, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_TRAIL
    assert out.reason_code == "trail_long"
    assert out.trailing_stop == pytest.approx(108.0)


def test_trail_never_loosens_long():
    """Close dips then rises; trailing stop must not decrease (monotonic flavor)."""
    cfg = ev.ExitConfig(
        trail_atr_mult=2.0,
        hard_stop_enabled=False,
        hard_target_enabled=False,
        max_bars=100,
        use_bos=False,
        trail_monotonic=True,
    )
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=1, highest=110.0, trail=108.0)
    # Bar 1: ATR drops to 0.5 -> raw candidate = 110 - 2*0.5 = 109.0.
    # trailing_stop max(108, 109) = 109.
    bar1 = _bar(close=109.5, atr=0.5)
    out1 = ev.evaluate_bar(cfg, state, bar1)
    assert out1.trailing_stop == pytest.approx(109.0)

    # Bar 2: price falls but not through trail; highest still 110; ATR rises
    # to 2.0 -> raw candidate = 110 - 2*2 = 106; trailing_stop must stay 109,
    # never loosen.
    bar2 = _bar(close=109.2, atr=2.0)
    out2 = ev.evaluate_bar(cfg, out1.updated_state, bar2)
    assert out2.trailing_stop == pytest.approx(109.0)
    assert out2.action == ev.EXIT_ACTION_HOLD


# ---------------------------------------------------------------------------
# Rule 6: partial at 1R
# ---------------------------------------------------------------------------

def test_trail_can_loosen_when_monotonic_false():
    """Backtest-flavor trail is deliberately non-monotonic (legacy parity)."""
    cfg = ev.ExitConfig(
        trail_atr_mult=2.0, hard_stop_enabled=False, hard_target_enabled=False,
        max_bars=100, use_bos=False, trail_monotonic=False,
    )
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=1, highest=110.0, trail=108.0)
    # ATR grows so raw candidate = 110 - 2*2 = 106 < 108; canonical returns 106
    # (loosens) because this flavor mirrors legacy.
    bar = _bar(close=109.5, atr=2.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.trailing_stop == pytest.approx(106.0)
    assert out.action == ev.EXIT_ACTION_HOLD


def test_partial_at_1r_fires_once():
    cfg = ev.ExitConfig(trail_atr_mult=None, max_bars=100, use_bos=False,
                        partial_at_1r=True)
    state = _state_long(entry=100.0, stop=97.0, target=110.0, bars_held=2)
    bar = _bar(close=103.1, atr=1.0)  # +1R move
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_PARTIAL
    assert out.reason_code == "partial_at_1r"
    assert out.updated_state.partial_taken is True

    # Second 1R bar should NOT re-emit partial.
    out2 = ev.evaluate_bar(cfg, out.updated_state, _bar(close=103.5, atr=1.0))
    assert out2.action != ev.EXIT_ACTION_PARTIAL


# ---------------------------------------------------------------------------
# Priority tie-breaks
# ---------------------------------------------------------------------------

def test_stop_wins_over_target_on_same_bar():
    """If a single bar straddles both levels, hard_stop is reported first."""
    cfg = ev.build_config_live(None)
    state = _state_long(entry=100.0, stop=98.0, target=102.0)
    bar = _bar(close=100.0, low=97.5, high=102.5, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_STOP


def test_target_wins_over_bos_on_same_bar():
    cfg = ev.ExitConfig(
        trail_atr_mult=None, hard_stop_enabled=True, hard_target_enabled=True,
        max_bars=100, use_bos=True, bos_buffer_frac=0.003, bos_grace_bars=0,
    )
    state = _state_long(entry=100.0, stop=98.0, target=102.0, bars_held=5)
    # Bar high tags target (>=102) and close breaches BOS level (99*0.997=98.703).
    # Low stays above hard_stop (98.0) so stop does not fire — target must win.
    bar = _bar(close=98.5, low=98.1, high=102.5, atr=1.0, swing_low=99.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_TARGET


def test_time_decay_wins_over_trail_on_same_bar():
    cfg = ev.build_config_backtest(exit_atr_mult=2.0, exit_max_bars=5,
                                    use_bos=False)
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=4, highest=110.0, trail=None)
    bar = _bar(close=107.5, atr=1.0)  # trail would fire (108>107.5) AND max_bars=5
    out = ev.evaluate_bar(cfg, state, bar)
    # Per frozen priority time_decay > trail.
    assert out.action == ev.EXIT_ACTION_EXIT_TIME_DECAY


# ---------------------------------------------------------------------------
# Crypto-safe: ticker normalization is caller's job, evaluator is symbol-agnostic
# ---------------------------------------------------------------------------

def test_evaluator_symbol_agnostic_for_crypto_semantics():
    """Same math regardless of whether the caller passed BASE-USD or BASEUSD."""
    cfg = ev.build_config_live(None)
    state = _state_long(entry=50_000.0, stop=48_500.0, target=53_000.0)
    bar = _bar(close=49_900.0, low=48_400.0, high=50_100.0, atr=200.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_STOP
    assert out.exit_price == pytest.approx(48_500.0)
    # R-multiple = (48500 - 50000) / |50000 - 48500| = -1.0
    assert out.r_multiple == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Hold path still updates trail + bars_held monotonically
# ---------------------------------------------------------------------------

def test_hold_still_increments_bars_and_updates_highest():
    cfg = ev.build_config_backtest(exit_atr_mult=2.0, exit_max_bars=100,
                                    use_bos=False)
    state = _state_long(entry=100.0, stop=None, target=None,
                        bars_held=3, highest=102.0, trail=None)
    bar = _bar(close=104.0, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_HOLD
    assert out.updated_state.bars_held == 4
    assert out.updated_state.highest_since_entry == pytest.approx(104.0)
    assert out.updated_state.trailing_stop == pytest.approx(102.0)  # 104 - 2*1


# ---------------------------------------------------------------------------
# Short direction smoke
# ---------------------------------------------------------------------------

def test_short_hard_stop_fires_on_high_breach():
    cfg = ev.build_config_live(None)
    state = ev.PositionState(
        direction="short",
        entry_price=100.0,
        stop_price=103.0,
        target_price=94.0,
        bars_held=0,
        highest_since_entry=100.0,
        lowest_since_entry=100.0,
        trailing_stop=None,
        partial_taken=False,
    )
    bar = _bar(close=102.5, low=101.0, high=103.2, atr=1.0)
    out = ev.evaluate_bar(cfg, state, bar)
    assert out.action == ev.EXIT_ACTION_EXIT_STOP
    assert out.exit_price == pytest.approx(103.0)
    assert out.r_multiple == pytest.approx(-1.0)
