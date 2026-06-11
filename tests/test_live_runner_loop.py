"""LiveRunnerLoop (Stage 2 websocket rail) — dispatch semantics, no DB/broker.

The loop's job is narrow: turn a price-bus tick into an IMMEDIATE
tick_live_session dispatch when (and only when) a tracked threshold is
breached — debounced, bounded, never on the WS thread.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app.services.trading.momentum_neural.live_runner_loop import (
    _EVENT_TICK_MIN_SPACING_S,
    LiveRunnerLoop,
)


def _loop_with(sessions):
    """A loop whose tracker returns the given session dicts and whose dispatch
    records instead of running DB ticks."""
    loop = LiveRunnerLoop()
    loop._running = True
    loop._tracker.get_sessions_for_symbol = lambda sym: sessions  # type: ignore
    calls = []
    loop._dispatch = lambda sid: calls.append(sid)  # type: ignore
    return loop, calls


def _q(bid=None, mid=None):
    return SimpleNamespace(bid=bid, mid=mid, price=mid)


def test_stop_breach_dispatches():
    loop, calls = _loop_with([
        {"session_id": 7, "symbol": "BATL", "state": "live_trailing", "stop_px": 2.10, "target_px": 2.90},
    ])
    loop._on_tick("BATL", _q(bid=2.09))
    assert calls == [7]


def test_target_zone_dispatches():
    loop, calls = _loop_with([
        {"session_id": 7, "symbol": "BATL", "state": "live_entered", "stop_px": 1.50, "target_px": 2.00},
    ])
    loop._on_tick("BATL", _q(bid=1.991))  # >= target*0.995
    assert calls == [7]


def test_inside_band_does_not_dispatch():
    loop, calls = _loop_with([
        {"session_id": 7, "symbol": "BATL", "state": "live_trailing", "stop_px": 2.10, "target_px": 2.90},
    ])
    loop._on_tick("BATL", _q(bid=2.40))
    assert calls == []


def test_pending_entry_always_dispatches():
    loop, calls = _loop_with([
        {"session_id": 9, "symbol": "DSY", "state": "live_pending_entry"},
    ])
    loop._on_tick("DSY", _q(bid=5.0))
    assert calls == [9]


def test_watching_state_never_event_ticks():
    # entries are Stage 3 — watching sessions stay on the scheduled batch
    loop, calls = _loop_with([
        {"session_id": 4, "symbol": "DSY", "state": "watching_live", "stop_px": 0, "target_px": 0},
    ])
    loop._on_tick("DSY", _q(bid=5.0))
    assert calls == []


def test_no_usable_price_is_safe():
    loop, calls = _loop_with([
        {"session_id": 7, "symbol": "BATL", "state": "live_trailing", "stop_px": 2.10, "target_px": 2.90},
    ])
    loop._on_tick("BATL", _q(bid=None, mid=None))
    assert calls == []


def test_debounce_blocks_rapid_repeat_dispatch():
    loop = LiveRunnerLoop()
    loop._running = True
    ran = []

    def _fake_tick(sid):  # mimic the real _tick_session's finally: clear inflight
        ran.append(sid)
        with loop._inflight_lock:
            loop._inflight.discard(sid)

    loop._tick_session = _fake_tick  # type: ignore

    class _SyncPool:  # run inline so the test is deterministic
        def submit(self, fn, *a):
            fn(*a)

    loop._pool = _SyncPool()  # type: ignore
    loop._dispatch(42)
    loop._dispatch(42)  # within the spacing window -> dropped
    assert ran == [42]
    loop._last_event_tick[42] = time.monotonic() - (_EVENT_TICK_MIN_SPACING_S + 0.1)
    loop._dispatch(42)
    assert ran == [42, 42]


def test_not_running_ignores_ticks():
    loop, calls = _loop_with([
        {"session_id": 7, "symbol": "BATL", "state": "live_trailing", "stop_px": 2.10, "target_px": 2.90},
    ])
    loop._running = False
    loop._on_tick("BATL", _q(bid=1.0))
    assert calls == []
