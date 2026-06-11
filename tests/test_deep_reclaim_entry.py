"""Deep-retrace RECLAIM entry (the 2026-06-11 EDHL gap): after a retrace too deep
for the flag checks, price reclaims the 9-EMA, holds it, and the system buys the
first break of the recovery swing high — instead of rejecting forever on
window-anchored pullback_too_deep / pullback_below_ema9 while the name rips
(EDHL 13.67->18.21 with Ross in for +$76k, session 657 blocked every 15s)."""

from __future__ import annotations

import inspect

import pandas as pd

from app.services.trading.momentum_neural.entry_gates import (
    TICK_ARMED_WAIT_REASONS,
    pullback_break_confirmation,
)


def _frame(bars: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """bars = [(open, high, low, close, volume)] on a 1m index."""
    idx = pd.date_range("2026-06-11 12:00", periods=len(bars), freq="1min")
    return pd.DataFrame(
        {
            "Open": [b[0] for b in bars],
            "High": [b[1] for b in bars],
            "Low": [b[2] for b in bars],
            "Close": [b[3] for b in bars],
            "Volume": [b[4] for b in bars],
        },
        index=idx,
    )


def _deep_v_bars(
    *, dip_low: float = 13.5, recovery_closes: tuple = (14.0, 14.3, 14.5),
    last_bar: tuple = (14.6, 15.4, 14.55, 15.3, 500_000.0),
) -> list[tuple[float, float, float, float, float]]:
    """20-bar ramp 10->15, 3-bar deep fade to ``dip_low``, recovery bars holding
    the (decayed) EMA-9, then ``last_bar`` as the current bar."""
    bars = []
    px = 10.0
    for _ in range(20):  # impulse: +0.25/bar with honest ranges (drives ATR%)
        o = px
        c = px + 0.25
        bars.append((o, c + 0.15, o - 0.15, c, 100_000.0))
        px = c
    # fade: three red bars down to the dip (deep vs the 3-bar base => too_deep)
    bars.append((15.0, 15.05, 14.2, 14.0, 120_000.0))
    bars.append((14.0, 14.1, dip_low + 0.1, dip_low + 0.2, 120_000.0))
    bars.append((dip_low + 0.2, dip_low + 0.4, dip_low, dip_low + 0.1, 120_000.0))
    # recovery: closes back above the decayed EMA-9, rising highs
    rec_h = (14.2, 14.5, 14.8)
    for h, c in zip(rec_h, recovery_closes):
        bars.append((c - 0.2, h, c - 0.3, c, 150_000.0))
    bars.append(last_bar)
    return bars


_GATES = dict(
    entry_interval="1m",
    require_retest=True,
    volume_spike_multiple=1.5,
    runaway_min_volume_spike=2.5,
    require_sustained_volume=True,
    sustained_rvol_floor=1.0,
)


def test_deep_v_reclaim_break_fires_on_completed_bar() -> None:
    df = _frame(_deep_v_bars())
    ok, reason, dbg = pullback_break_confirmation(df, **_GATES)
    assert ok and reason == "deep_reclaim_ok", (reason, dbg)
    assert dbg["pattern"] == "deep_reclaim"
    assert dbg["deep_reclaim_from"] in ("pullback_too_deep", "pullback_below_ema9")
    # entry level = the RECOVERY swing high (14.8), not the pre-fade HOD (15+)
    assert abs(dbg["pullback_high"] - 14.8) < 1e-9
    # stop = reclaim consolidation low — NEVER the deep dip low
    assert dbg["pullback_low"] > 13.5 + 0.05, dbg["pullback_low"]


def test_reclaim_wait_arms_tick_watch_then_tick_fires() -> None:
    # current bar has NOT broken the recovery high (14.8) yet
    waiting_last = (14.5, 14.7, 14.45, 14.65, 200_000.0)
    df = _frame(_deep_v_bars(last_bar=waiting_last))
    ok, reason, dbg = pullback_break_confirmation(df, **_GATES)
    assert not ok and reason == "waiting_for_reclaim_high", (reason, dbg)
    assert reason in TICK_ARMED_WAIT_REASONS  # the runner will stash the watch level
    assert dbg["pullback_high"] >= 14.8
    # the live WS ask trades through the level -> tick fire, no completed bar needed
    ok2, reason2, dbg2 = pullback_break_confirmation(
        df, live_price=dbg["pullback_high"] + 0.02, **_GATES
    )
    assert ok2 and reason2 == "deep_reclaim_tick_ok", (reason2, dbg2)
    assert dbg2["tick_break"] is True


def test_dead_cat_hold_lost_stays_unarmed() -> None:
    # recovery attempt whose CURRENT close fell back under the EMA band -> the
    # hold streak is broken; nothing armed, no level to fire through
    bars = _deep_v_bars(last_bar=(14.0, 14.05, 13.55, 13.6, 90_000.0))
    df = _frame(bars)
    ok, reason, dbg = pullback_break_confirmation(df, **_GATES)
    assert not ok and reason in ("reclaim_forming", "pullback_too_deep", "pullback_below_ema9"), reason
    if reason == "reclaim_forming":
        assert dbg.get("pullback_high") is None  # un-armed: no tick watch level


def test_collapse_keeps_original_rejection() -> None:
    # 40% breakdown (15 -> 9) is beyond the volatility-relative collapse cap:
    # not a pullback to reclaim-buy — the original rejection must stand
    bars = _deep_v_bars(dip_low=9.0, recovery_closes=(9.4, 9.6, 9.8),
                        last_bar=(9.8, 10.4, 9.7, 10.3, 500_000.0))
    df = _frame(bars)
    ok, reason, dbg = pullback_break_confirmation(df, **_GATES)
    assert not ok and reason in ("pullback_too_deep", "pullback_below_ema9"), (reason, dbg)
    assert dbg.get("pattern") != "deep_reclaim" or dbg.get("pullback_high") is None


def test_disabled_knob_restores_legacy_behavior(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_deep_reclaim_enabled", False, raising=False)
    df = _frame(_deep_v_bars())
    ok, reason, _ = pullback_break_confirmation(df, **_GATES)
    assert not ok and reason in ("pullback_too_deep", "pullback_below_ema9")


def test_tick_armed_tuple_is_shared_across_call_sites() -> None:
    # the EDHL disarm bug came from three hard-coded copies of this tuple — the
    # runner and the replay must consume the entry_gates constant, not a literal
    import app.services.trading.momentum_neural.live_runner as lr
    import app.services.trading.momentum_neural.replay_v2 as rp

    for mod in (lr, rp):
        src = inspect.getsource(mod)
        assert "TICK_ARMED_WAIT_REASONS" in src, mod.__name__
        assert '("waiting_for_break", "waiting_for_reclaim")' not in src, mod.__name__
    assert "waiting_for_reclaim_high" in TICK_ARMED_WAIT_REASONS
