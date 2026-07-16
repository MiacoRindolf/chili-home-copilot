from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.services.trading.momentum_neural.entry_gates import (
    TICK_ARMED_WAIT_REASONS,
    ross_breakout_starter_confirmation,
)


def _starter_df() -> pd.DataFrame:
    rows = [
        ("2026-07-01 11:58:00", 10.20, 10.34, 10.14, 10.30, 120_000),
        ("2026-07-01 11:58:15", 10.30, 10.48, 10.28, 10.43, 135_000),
        ("2026-07-01 11:58:30", 10.43, 10.61, 10.39, 10.57, 150_000),
        ("2026-07-01 11:58:45", 10.57, 10.73, 10.52, 10.68, 165_000),
        ("2026-07-01 11:59:00", 10.68, 10.82, 10.62, 10.77, 180_000),
        ("2026-07-01 11:59:15", 10.77, 10.87, 10.72, 10.79, 190_000),
        ("2026-07-01 11:59:30", 10.79, 10.84, 10.74, 10.80, 175_000),
        ("2026-07-01 11:59:45", 10.80, 10.86, 10.78, 10.82, 185_000),
        ("2026-07-01 12:00:00", 10.82, 10.85, 10.80, 10.84, 200_000),
    ]
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")
    return df


def _backside_lower_high_df() -> pd.DataFrame:
    rows = [
        ("2026-07-01 11:55:00", 11.20, 12.00, 11.10, 11.80, 300_000),
        ("2026-07-01 11:55:15", 11.80, 11.90, 11.20, 11.35, 260_000),
        ("2026-07-01 11:55:30", 11.35, 11.45, 10.80, 10.95, 220_000),
        ("2026-07-01 11:55:45", 10.95, 11.05, 10.40, 10.50, 210_000),
        ("2026-07-01 11:56:00", 10.50, 10.60, 10.00, 10.20, 190_000),
        ("2026-07-01 11:56:15", 10.20, 10.30, 9.80, 9.95, 175_000),
        ("2026-07-01 11:56:30", 9.95, 10.00, 9.70, 9.82, 165_000),
        ("2026-07-01 11:56:45", 9.82, 9.90, 9.60, 9.70, 150_000),
        ("2026-07-01 11:57:00", 9.70, 9.78, 9.50, 9.62, 140_000),
        ("2026-07-01 11:57:15", 9.62, 9.72, 9.45, 9.58, 135_000),
        ("2026-07-01 11:57:30", 9.58, 9.82, 9.50, 9.76, 150_000),
        ("2026-07-01 11:57:45", 9.76, 9.95, 9.70, 9.92, 170_000),
        ("2026-07-01 11:58:00", 9.92, 10.00, 9.86, 9.96, 185_000),
        ("2026-07-01 11:58:15", 9.96, 9.98, 9.90, 9.97, 180_000),
        ("2026-07-01 11:58:30", 9.97, 10.00, 9.94, 9.99, 190_000),
    ]
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")


def test_ross_breakout_starter_fires_under_premarket_high_without_pullback() -> None:
    ok, reason, debug = ross_breakout_starter_confirmation(
        _starter_df(),
        entry_interval="15s",
        live_price=10.85,
        symbol="JEM",
    )

    assert ok is True
    assert reason == "ross_breakout_starter_tick"
    assert debug["breakout_level"] == 10.87
    assert debug["pullback_high"] == 10.87
    assert debug["pullback_low"] < 10.85
    assert "pullback_depth_bps" not in debug


def test_ross_breakout_starter_waits_when_price_is_not_at_level() -> None:
    ok, reason, debug = ross_breakout_starter_confirmation(
        _starter_df(),
        entry_interval="15s",
        live_price=10.81,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "ross_breakout_starter_waiting_for_level"
    assert reason in TICK_ARMED_WAIT_REASONS
    assert debug["session_high"] == 10.87
    assert debug["pullback_high"] == 10.87


def test_ross_breakout_starter_does_not_turn_backside_into_pullback_veto(monkeypatch) -> None:
    from app.services.trading.momentum_neural import ross_momentum

    monkeypatch.setattr(
        ross_momentum,
        "front_side_state",
        lambda *_args, **_kwargs: SimpleNamespace(is_backside=True, reason="backside_fade"),
    )

    ok, reason, debug = ross_breakout_starter_confirmation(
        _starter_df(),
        entry_interval="15s",
        live_price=10.85,
        symbol="JEM",
    )

    assert ok is True
    assert reason == "ross_breakout_starter_tick"
    assert debug["front_side_state_advisory"] == "backside_fade"


def test_ross_breakout_starter_requires_push_even_on_tick_break() -> None:
    df = _starter_df().copy()
    df.iloc[-2, df.columns.get_loc("Close")] = 10.87

    ok, reason, debug = ross_breakout_starter_confirmation(
        df,
        entry_interval="15s",
        live_price=10.87,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "ross_breakout_starter_waiting_for_push"
    assert debug["velocity_bps"] == 0.0


def test_ross_breakout_starter_blocks_backside_bounce_below_session_high(monkeypatch) -> None:
    from app.services.trading.momentum_neural import ross_momentum

    monkeypatch.setattr(
        ross_momentum,
        "front_side_state",
        lambda *_args, **_kwargs: SimpleNamespace(is_backside=True, reason="backside_fade"),
    )

    ok, reason, debug = ross_breakout_starter_confirmation(
        _backside_lower_high_df(),
        entry_interval="15s",
        live_price=10.0,
        symbol="TC",
    )

    assert ok is False
    assert reason == "ross_breakout_starter_backside_below_session_high"
    assert debug["front_side_state_advisory"] == "backside_fade"
    assert debug["room_to_session_high_bps"] > debug["max_above_level_bps"]
