"""Phase B parity tests: canonical ExitEvaluator vs legacy exit math.

These tests do NOT import backtrader or spin up a real backtest. Instead
they re-express the legacy formulas from
``backtest_service.DynamicPatternStrategy.next`` and
``live_exit_engine.compute_live_exit_levels`` as plain Python, then feed
the same synthetic bar stream through both the legacy formula and the
canonical evaluator. The invariant we assert is:

    On every bar, legacy_close == canonical_close.

``close`` means ``any exit action``. Label mismatches (e.g. legacy said
``exit_trail`` but canonical said ``exit_time_decay`` on a bar that would
fire both) are tolerated — PnL impact of any close is identical under the
priority contract.

Coverage:
* 50 trend paths (drifting up then drifting down)
* 50 chop paths (mean-reverting)
* 50 crash paths (gap-down)
* Live-flavor hard stop / hard target edge cases (explicit BASE-USD crypto)
* Monotonic trail: canonical trail is >= legacy trail at every bar
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.trading import exit_evaluator as ev


def _atr_simple(highs, lows, closes, period: int = 14) -> list[float | None]:
    """Minimal ATR clone for tests; uses simple mean of true range."""
    tr: list[float] = []
    n = len(closes)
    for i in range(n):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    out: list[float | None] = [None] * n
    for i in range(n):
        if i + 1 < period:
            out[i] = None
        else:
            out[i] = float(np.mean(tr[i - period + 1 : i + 1]))
    return out


def _confirmed_swing_low(lows: list[float], lookback: int = 10) -> list[float | None]:
    """Mirror backtest ``_compute_swing_lows`` semantics in a test-local form."""
    n = len(lows)
    out: list[float | None] = [None] * n
    last: float | None = None
    for confirm_bar in range(2 * lookback, n):
        center = confirm_bar - lookback
        window = lows[max(0, center - lookback) : center + lookback + 1]
        if lows[center] == min(window):
            last = float(lows[center])
        out[confirm_bar] = last
    return out


def _make_path(seed: int, mode: str, n: int = 60) -> tuple[list, list, list]:
    """Return (opens, highs, lows, closes) — already split as H/L/C for brevity."""
    rng = np.random.default_rng(seed)
    if mode == "trend":
        drift_up = np.linspace(0, 10, n // 2)
        drift_down = np.linspace(10, 4, n - n // 2)
        trend = np.concatenate([drift_up, drift_down])
        close = 100.0 + trend + rng.normal(0, 0.5, n)
    elif mode == "chop":
        close = 100.0 + np.cumsum(rng.normal(0, 0.3, n))
    elif mode == "crash":
        close = np.concatenate([100 + np.linspace(0, 3, n - 5), 100 + np.linspace(3, -12, 5)])
        close = close + rng.normal(0, 0.2, n)
    else:
        raise ValueError(mode)
    highs = close + np.abs(rng.normal(0, 0.4, n))
    lows = close - np.abs(rng.normal(0, 0.4, n))
    highs = np.maximum(highs, close)
    lows = np.minimum(lows, close)
    return highs.tolist(), lows.tolist(), close.tolist()


# ---------------------------------------------------------------------------
# Backtest parity: canonical evaluator vs legacy formula from DynamicPatternStrategy
# ---------------------------------------------------------------------------

def _simulate_legacy_backtest_close(
    *,
    highs,
    lows,
    closes,
    entry_idx: int,
    atr_arr,
    swing_low_arr,
    exit_atr_mult: float,
    exit_max_bars: int,
    bos_buffer_pct: float,
    bos_grace: int,
):
    """Legacy DynamicPatternStrategy.next() exit branch in a straight function.

    Returns (close_bar_idx, reason, exit_price, trailing_stops) or
    (None, None, None, trailing_stops) if the position is never closed in
    the window. Entry happens at bar ``entry_idx`` at that bar's close.
    """
    highest = float(closes[entry_idx])
    bars_in_trade = 0
    trailing_stops = []
    for i in range(entry_idx + 1, len(closes)):
        bars_in_trade += 1
        price = float(closes[i])
        highest = max(highest, price)
        atr_val = 0.0
        if i < len(atr_arr) and atr_arr[i] is not None:
            atr_val = atr_arr[i]
        trailing_stop = highest - exit_atr_mult * atr_val
        trailing_stops.append(trailing_stop)

        bos_triggered = False
        if bars_in_trade >= bos_grace and i < len(swing_low_arr):
            swing_low = swing_low_arr[i]
            if swing_low is not None and swing_low > 0:
                bos_threshold = swing_low * (1 - bos_buffer_pct)
                if price < bos_threshold:
                    bos_triggered = True

        if price < trailing_stop:
            return i, "exit_trail", price, trailing_stops
        if bos_triggered:
            return i, "exit_bos", price, trailing_stops
        if bars_in_trade >= exit_max_bars:
            return i, "exit_time_decay", price, trailing_stops
    return None, None, None, trailing_stops


def _simulate_canonical_backtest_close(
    *,
    highs,
    lows,
    closes,
    entry_idx: int,
    atr_arr,
    swing_low_arr,
    exit_atr_mult: float,
    exit_max_bars: int,
    bos_buffer_frac: float,
    bos_grace: int,
):
    """Canonical evaluator equivalent driven bar-by-bar."""
    cfg = ev.build_config_backtest(
        exit_atr_mult=exit_atr_mult,
        exit_max_bars=exit_max_bars,
        use_bos=True,
        bos_buffer_frac=bos_buffer_frac,
        bos_grace_bars=bos_grace,
    )
    entry_price = float(closes[entry_idx])
    state = ev.PositionState(
        direction="long",
        entry_price=entry_price,
        stop_price=None,
        target_price=None,
        bars_held=0,
        highest_since_entry=entry_price,
        lowest_since_entry=entry_price,
        trailing_stop=None,
        partial_taken=False,
    )
    trailing_stops = []
    for i in range(entry_idx + 1, len(closes)):
        bar = ev.BarContext(
            open=float(closes[i]),
            high=float(highs[i]),
            low=float(lows[i]),
            close=float(closes[i]),
            atr=atr_arr[i] if i < len(atr_arr) and atr_arr[i] is not None else None,
            swing_low=swing_low_arr[i] if i < len(swing_low_arr) else None,
            swing_high=None,
            bar_idx=i,
        )
        decision = ev.evaluate_bar(cfg, state, bar)
        trailing_stops.append(decision.trailing_stop)
        if decision.action != ev.EXIT_ACTION_HOLD:
            return i, decision.action, decision.exit_price, trailing_stops
        state = decision.updated_state
    return None, None, None, trailing_stops


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("mode", ["trend", "chop", "crash"])
def test_backtest_parity_close_bar_matches(seed: int, mode: str):
    """Core invariant: legacy and canonical exit on the same bar."""
    highs, lows, closes = _make_path(seed=seed, mode=mode, n=60)
    atr_arr = _atr_simple(highs, lows, closes, period=14)
    swing_low_arr = _confirmed_swing_low(lows, lookback=5)
    entry_idx = 15

    legacy_bar, legacy_reason, legacy_price, legacy_trails = _simulate_legacy_backtest_close(
        highs=highs, lows=lows, closes=closes, entry_idx=entry_idx,
        atr_arr=atr_arr, swing_low_arr=swing_low_arr,
        exit_atr_mult=2.0, exit_max_bars=20,
        bos_buffer_pct=0.003, bos_grace=3,
    )
    canonical_bar, canonical_action, canonical_price, canonical_trails = _simulate_canonical_backtest_close(
        highs=highs, lows=lows, closes=closes, entry_idx=entry_idx,
        atr_arr=atr_arr, swing_low_arr=swing_low_arr,
        exit_atr_mult=2.0, exit_max_bars=20,
        bos_buffer_frac=0.003, bos_grace=3,
    )
    assert legacy_bar == canonical_bar, (
        f"seed={seed} mode={mode} legacy_bar={legacy_bar} canonical_bar={canonical_bar} "
        f"legacy_reason={legacy_reason} canonical_action={canonical_action}"
    )
    if legacy_bar is not None:
        # Price identical (both use close on the close-bar).
        assert legacy_price == pytest.approx(canonical_price)


@pytest.mark.parametrize("seed", range(10))
def test_backtest_parity_trailing_stop_matches_bar_by_bar(seed: int):
    """Trailing stop values should match at each evaluated bar."""
    highs, lows, closes = _make_path(seed=seed, mode="trend", n=60)
    atr_arr = _atr_simple(highs, lows, closes, period=14)
    swing_low_arr = _confirmed_swing_low(lows, lookback=5)
    entry_idx = 15

    _, _, _, legacy_trails = _simulate_legacy_backtest_close(
        highs=highs, lows=lows, closes=closes, entry_idx=entry_idx,
        atr_arr=atr_arr, swing_low_arr=swing_low_arr,
        exit_atr_mult=2.0, exit_max_bars=200,  # force no time-decay stop
        bos_buffer_pct=0.003, bos_grace=100,   # force no BOS stop
    )
    # Canonical without BOS / time_decay stops, so trails run the same length.
    cfg = ev.build_config_backtest(
        exit_atr_mult=2.0, exit_max_bars=200, use_bos=False,
        bos_buffer_frac=0.003, bos_grace_bars=100,
    )
    entry_price = float(closes[entry_idx])
    state = ev.PositionState(
        direction="long",
        entry_price=entry_price,
        stop_price=None,
        target_price=None,
        bars_held=0,
        highest_since_entry=entry_price,
        lowest_since_entry=entry_price,
        trailing_stop=None,
        partial_taken=False,
    )
    canonical_trails = []
    for i in range(entry_idx + 1, len(closes)):
        bar = ev.BarContext(
            open=float(closes[i]), high=float(highs[i]), low=float(lows[i]),
            close=float(closes[i]),
            atr=atr_arr[i] if atr_arr[i] is not None else None,
            swing_low=None, swing_high=None, bar_idx=i,
        )
        decision = ev.evaluate_bar(cfg, state, bar)
        canonical_trails.append(decision.trailing_stop)
        if decision.action != ev.EXIT_ACTION_HOLD:
            break
        state = decision.updated_state

    # Legacy trail is a raw candidate (can loosen); canonical trail is
    # monotonic. So canonical must be >= legacy at each bar.
    for bi, (l_trail, c_trail) in enumerate(zip(legacy_trails, canonical_trails)):
        if c_trail is None:
            continue
        assert c_trail >= l_trail - 1e-9, (
            f"seed={seed} bar={bi} canonical_trail={c_trail} legacy_trail={l_trail}"
        )


# ---------------------------------------------------------------------------
# Live parity: canonical evaluator vs legacy compute_live_exit_levels math
# ---------------------------------------------------------------------------

def _simulate_legacy_live(
    *,
    current_price: float,
    entry: float,
    stop: float,
    target: float | None,
    is_long: bool,
    atr: float | None,
    swing_low: float | None,
    exit_cfg: dict,
    days_held: int,
):
    """Re-express compute_live_exit_levels decision tree without DB/HTTP."""
    action = "hold"
    exit_price = None
    if atr and exit_cfg.get("trailing_enabled", True):
        pass  # trailing stop only computed, not used by legacy for close decisions
    if is_long and current_price <= stop:
        action = "exit_stop"
        exit_price = stop
    elif not is_long and current_price >= stop:
        action = "exit_stop"
        exit_price = stop
    if target:
        if is_long and current_price >= target:
            action = "exit_target"
            exit_price = target
        elif not is_long and current_price <= target:
            action = "exit_target"
            exit_price = target
    max_bars = exit_cfg.get("max_bars", 20)
    if max_bars and days_held >= max_bars and action == "hold":
        action = "exit_time_decay"
        exit_price = current_price
    if atr and exit_cfg.get("use_bos", True) and swing_low:
        bos_buffer = exit_cfg.get("bos_buffer_pct", 0.5) / 100
        bos_level = swing_low * (1 - bos_buffer) if is_long else swing_low * (1 + bos_buffer)
        if is_long and current_price < bos_level:
            action = "exit_bos"
            exit_price = current_price
    return action, exit_price


def _simulate_canonical_live(
    *,
    current_price: float,
    entry: float,
    stop: float,
    target: float | None,
    is_long: bool,
    atr: float | None,
    swing_low: float | None,
    exit_cfg: dict,
    days_held: int,
):
    cfg = ev.build_config_live(exit_cfg)
    state = ev.PositionState(
        direction="long" if is_long else "short",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        bars_held=max(0, days_held - 1),
        highest_since_entry=max(entry, current_price) if is_long else entry,
        lowest_since_entry=min(entry, current_price) if is_long else entry,
        trailing_stop=None,
        partial_taken=False,
    )
    bar = ev.BarContext(
        open=current_price, high=current_price, low=current_price, close=current_price,
        atr=atr, swing_low=swing_low, swing_high=None, bar_idx=days_held,
    )
    decision = ev.evaluate_bar(cfg, state, bar)
    return decision.action, decision.exit_price


@pytest.mark.parametrize("current_price,expected_action", [
    (96.0, "exit_stop"),       # breaches stop at 97
    (106.5, "exit_target"),    # breaches target at 106
    (100.0, "hold"),
])
def test_live_parity_simple_triggers_equities(current_price, expected_action):
    cfg_dict = {"trailing_enabled": True, "trailing_atr_mult": 1.5, "max_bars": 20,
                "use_bos": False, "bos_buffer_pct": 0.5}
    legacy, legacy_px = _simulate_legacy_live(
        current_price=current_price, entry=100.0, stop=97.0, target=106.0,
        is_long=True, atr=1.0, swing_low=None, exit_cfg=cfg_dict, days_held=2,
    )
    canonical, canonical_px = _simulate_canonical_live(
        current_price=current_price, entry=100.0, stop=97.0, target=106.0,
        is_long=True, atr=1.0, swing_low=None, exit_cfg=cfg_dict, days_held=2,
    )
    assert legacy == canonical == expected_action
    if expected_action != "hold":
        assert legacy_px == pytest.approx(canonical_px)


def test_live_parity_time_decay_only_when_no_other_rule():
    cfg_dict = {"trailing_enabled": True, "trailing_atr_mult": 1.5, "max_bars": 3,
                "use_bos": False, "bos_buffer_pct": 0.5}
    legacy, _ = _simulate_legacy_live(
        current_price=100.5, entry=100.0, stop=97.0, target=110.0,
        is_long=True, atr=1.0, swing_low=None, exit_cfg=cfg_dict, days_held=4,
    )
    canonical, _ = _simulate_canonical_live(
        current_price=100.5, entry=100.0, stop=97.0, target=110.0,
        is_long=True, atr=1.0, swing_low=None, exit_cfg=cfg_dict, days_held=4,
    )
    assert legacy == canonical == "exit_time_decay"


def test_live_parity_bos_fires_when_close_below_buffered_swing():
    cfg_dict = {"trailing_enabled": True, "trailing_atr_mult": 1.5, "max_bars": 20,
                "use_bos": True, "bos_buffer_pct": 0.5}
    # Legacy BOS: swing_low=99, buffer=0.005 -> level=98.505. Close 98.0 below -> BOS.
    # Stop is at 97.0 — current_price=98.0 is ABOVE stop so stop does not fire.
    legacy, _ = _simulate_legacy_live(
        current_price=98.0, entry=100.0, stop=97.0, target=110.0,
        is_long=True, atr=1.0, swing_low=99.0, exit_cfg=cfg_dict, days_held=2,
    )
    canonical, _ = _simulate_canonical_live(
        current_price=98.0, entry=100.0, stop=97.0, target=110.0,
        is_long=True, atr=1.0, swing_low=99.0, exit_cfg=cfg_dict, days_held=2,
    )
    assert legacy == canonical == "exit_bos"


def test_live_parity_crypto_bare_concat_ticker():
    """Crypto inputs must produce identical decisions for the non-BOS stop case.

    The evaluator is symbol-agnostic; this test exercises the crypto price
    range and confirms no rounding drift. BOS is disabled (swing_low=None)
    to isolate the stop path — the BOS-overwrite divergence between legacy
    live and the canonical priority contract is covered separately by the
    ``_known_bos_overwrite_divergence`` test below.
    """
    cfg_dict = {"trailing_enabled": True, "trailing_atr_mult": 1.5, "max_bars": 48,
                "use_bos": False, "bos_buffer_pct": 0.5}
    legacy, legacy_px = _simulate_legacy_live(
        current_price=48_400.0, entry=50_000.0, stop=48_500.0, target=53_000.0,
        is_long=True, atr=250.0, swing_low=None, exit_cfg=cfg_dict, days_held=3,
    )
    canonical, canonical_px = _simulate_canonical_live(
        current_price=48_400.0, entry=50_000.0, stop=48_500.0, target=53_000.0,
        is_long=True, atr=250.0, swing_low=None, exit_cfg=cfg_dict, days_held=3,
    )
    assert legacy == canonical == "exit_stop"
    assert legacy_px == pytest.approx(canonical_px)


def test_known_divergence_live_bos_overwrites_stop_is_legacy_bug():
    """Document a KNOWN divergence: legacy BOS overwrites earlier stop/target.

    ``compute_live_exit_levels`` sets ``action`` then checks BOS last without
    guarding on ``action == 'hold'``, so BOS wins whenever it fires — even
    when the bar already tripped the hard stop. Canonical priority is
    ``stop > target > BOS`` which is mathematically safer (caps loss at the
    known stop price). Phase B records this as a disagreement in shadow; a
    later cutover phase will adopt canonical behavior.
    """
    cfg_dict = {"trailing_enabled": True, "trailing_atr_mult": 1.5, "max_bars": 48,
                "use_bos": True, "bos_buffer_pct": 0.5}
    legacy, legacy_px = _simulate_legacy_live(
        current_price=48_400.0, entry=50_000.0, stop=48_500.0, target=53_000.0,
        is_long=True, atr=250.0, swing_low=49_000.0, exit_cfg=cfg_dict, days_held=3,
    )
    canonical, canonical_px = _simulate_canonical_live(
        current_price=48_400.0, entry=50_000.0, stop=48_500.0, target=53_000.0,
        is_long=True, atr=250.0, swing_low=49_000.0, exit_cfg=cfg_dict, days_held=3,
    )
    assert legacy == "exit_bos"
    assert legacy_px == pytest.approx(48_400.0)
    assert canonical == "exit_stop"
    assert canonical_px == pytest.approx(48_500.0)
    # Canonical is $100 better on this bar; legacy executes below stop.
    assert canonical_px > legacy_px


# ---------------------------------------------------------------------------
# Stress test: 150 synthetic live paths (50 each regime)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(50))
@pytest.mark.parametrize("mode", ["trend", "chop", "crash"])
def test_live_parity_stress(seed: int, mode: str):
    """Drive synthetic bars through both legacy live math and canonical evaluator.

    For each bar in the path we pretend ``current_price == close`` and
    evaluate both paths. The invariant: the FIRST-TO-CLOSE bar is the same
    under both paths (label can differ when two rules fire on the same
    bar; PnL impact is identical).
    """
    highs, lows, closes = _make_path(seed=seed, mode=mode, n=60)
    atr_arr = _atr_simple(highs, lows, closes, period=14)
    entry_idx = 15
    entry = float(closes[entry_idx])
    stop = entry * 0.97
    target = entry * 1.06
    cfg_dict = {"trailing_enabled": True, "trailing_atr_mult": 1.5, "max_bars": 20,
                "use_bos": False, "bos_buffer_pct": 0.5}

    legacy_first = None
    canonical_first = None
    for i in range(entry_idx + 1, len(closes)):
        days_held = i - entry_idx
        atr_val = atr_arr[i]
        current_price = float(closes[i])
        if legacy_first is None:
            la, _ = _simulate_legacy_live(
                current_price=current_price, entry=entry, stop=stop, target=target,
                is_long=True, atr=atr_val, swing_low=None,
                exit_cfg=cfg_dict, days_held=days_held,
            )
            if la != "hold":
                legacy_first = i
        if canonical_first is None:
            ca, _ = _simulate_canonical_live(
                current_price=current_price, entry=entry, stop=stop, target=target,
                is_long=True, atr=atr_val, swing_low=None,
                exit_cfg=cfg_dict, days_held=days_held,
            )
            if ca != "hold":
                canonical_first = i
        if legacy_first is not None and canonical_first is not None:
            break

    assert legacy_first == canonical_first, (
        f"seed={seed} mode={mode} legacy_first={legacy_first} "
        f"canonical_first={canonical_first}"
    )
