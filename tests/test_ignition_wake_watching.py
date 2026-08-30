"""Ignition wake ng nagbabantay — entry-speed fix (2026-08-30).

SINUKAT: ang WATCHING session ay tumitibok sa p50 11.2s (scheduler batch)
habang ang ignition ay 3-segundong spike; candidate→submit p50 ~64s vs Ross
~4s. Ang level-cross wake ay para lamang sa may watch_break_level — ang
velocity/volume/vwap na trigger classes ay walang gumigising. Ang fix: bawat
tick na pumasa sa mismong Ross ignition floor ay gumigising sa mga WATCHING
session ng symbol (walang bagong threshold; spacing 2s + single-flight na
hawak ng _spawn_session_wake).

Runnable: pytest tests/test_ignition_wake_watching.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.momentum_neural.ignition_loop import (
    _SessionCrossTracker,
    STATE_LIVE_ENTERED,
    STATE_WATCHING_LIVE,
)


def _tracker_with(entries_by_symbol):
    t = _SessionCrossTracker.__new__(_SessionCrossTracker)
    import threading

    t._lock = threading.Lock()
    t._by_symbol = entries_by_symbol
    t._hi = {}
    return t


def test_watching_returns_only_watching_sessions():
    t = _tracker_with({
        "XPON": [
            {"session_id": 1, "state": STATE_WATCHING_LIVE},
            {"session_id": 2, "state": STATE_LIVE_ENTERED},
            {"session_id": 3, "state": STATE_WATCHING_LIVE},
        ],
    })
    assert sorted(t.watching("XPON")) == [1, 3]


def test_watching_unknown_symbol_is_empty():
    t = _tracker_with({})
    assert t.watching("WALA") == []


def test_watching_normalizes_symbol_case():
    t = _tracker_with({"XPON": [{"session_id": 7, "state": STATE_WATCHING_LIVE}]})
    assert t.watching("xpon ") == [7]


def test_crossed_watch_break_level_still_works():
    # Ang lumang level-cross na landas ay hindi ginagalaw ng bagong method.
    t = _tracker_with({
        "XPON": [{
            "session_id": 9, "state": STATE_WATCHING_LIVE,
            "watch_break_level": 7.50,
        }],
    })
    q = SimpleNamespace(bid=7.60, mid=7.61)
    assert t.crossed("XPON", q) == [9]
    q2 = SimpleNamespace(bid=7.40, mid=7.41)
    assert t.crossed("XPON", q2) == []
