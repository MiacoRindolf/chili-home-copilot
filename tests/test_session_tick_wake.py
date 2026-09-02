"""Tick-speed session wake — instant open/close dispatch (2026-08-23).

Sa batch/scheduler window ang bawat entry/exit state ay umuusad lang sa
scheduler cadence (nominal 10s); ang loop-mode tick bridge ay retired mula sa
08-17 cutover. Ang session-cross tracker sa ignition_loop ang batch-mode mirror:
tick na tumawid sa stop/target/watch-break => agarang dispatch wake. Kasama rito
ang batch-safe stop-confirm redispatch sa live_runner (ang loop timer ay tahimik
na False sa batch mode => dating +1 buong cadence bawat software stop exit).

Runnable: pytest tests/test_session_tick_wake.py -v
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_WATCHING_LIVE,
)


@pytest.fixture(autouse=True)
def _owning_process_role(monkeypatch):
    """Ang wake ay may ROLE GATE mula 2026-08-24 (tingnan ang `wake_ownership`).

    Ang `tests/conftest.py` ay nagtatakda ng `CHILI_SCHEDULER_ROLE="none"` --
    ang role ng web container, na tahasang WALANG APScheduler at kaya hindi
    nagmamay-ari ng momentum execution. `_schedule_dispatch_wake` ay tahimik na
    False sa isang hindi-may-ari, kaya ang bawat stop-confirm assertion dito ay
    dating bumabagsak nang WALANG sinusubok. Ang suite na ito ay sumusubok sa
    MEKANISMO ng wake, hindi sa gate, kaya ito ay tumatakbo bilang may-ari.
    Ang gate mismo ay sinasaklaw ng `tests/test_wake_role_ownership.py`.
    """
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "momentum_exec_only")


def _tracker_with(symbol: str, entries: list[dict]) -> il._SessionCrossTracker:
    t = il._SessionCrossTracker()
    t._by_symbol = {symbol: entries}
    return t


def _quote(bid=None, mid=None, last=None):
    return SimpleNamespace(bid=bid, mid=mid, last=last)


# ── _SessionCrossTracker.crossed ─────────────────────────────────────────────

def test_stop_cross_hits_on_bid():
    t = _tracker_with("SDOT", [
        {"session_id": 7, "state": STATE_LIVE_ENTERED, "stop_px": 16.76, "target_px": 20.0},
    ])
    assert t.crossed("SDOT", _quote(bid=16.75, mid=16.80)) == [7]
    assert t.crossed("SDOT", _quote(bid=16.77, mid=16.80)) == []


def test_target_cross_uses_0995_band():
    t = _tracker_with("HUIZ", [
        {"session_id": 9, "state": STATE_LIVE_ENTERED, "stop_px": 1.0, "target_px": 10.0},
    ])
    assert t.crossed("HUIZ", _quote(bid=9.96)) == [9]
    assert t.crossed("HUIZ", _quote(bid=9.90)) == []


def test_watch_break_cross_uses_mid():
    t = _tracker_with("COIW", [
        {"session_id": 3, "state": STATE_WATCHING_LIVE, "watch_break_level": 5.50},
    ])
    assert t.crossed("COIW", _quote(bid=5.40, mid=5.51)) == [3]
    assert t.crossed("COIW", _quote(bid=5.40, mid=5.49)) == []


def test_pending_entry_dispatches_every_tick():
    t = _tracker_with("BCCQ", [
        {"session_id": 4, "state": STATE_LIVE_PENDING_ENTRY},
    ])
    assert t.crossed("BCCQ", _quote(bid=1.0)) == [4]


def test_untracked_symbol_is_cheap_empty():
    t = il._SessionCrossTracker()
    assert t.crossed("ZZZZ", _quote(bid=1.0)) == []


def test_bad_quote_returns_empty():
    t = _tracker_with("SDOT", [
        {"session_id": 7, "state": STATE_LIVE_ENTERED, "stop_px": 16.76},
    ])
    assert t.crossed("SDOT", _quote()) == []
    assert t.crossed("SDOT", SimpleNamespace(bid="junk", mid=None, last=None)) == []


# ── bagong-high wake (trail/ladder gap) ─────────────────────────────────────

def test_new_high_first_observation_is_seed_only():
    t = _tracker_with("HUIZ", [
        {"session_id": 11, "state": STATE_LIVE_ENTERED, "stop_px": 5.0, "target_px": 100.0},
    ])
    # unang tick: seed lang, walang wake (stop/target di tumawid)
    assert t.crossed("HUIZ", _quote(bid=9.00, mid=9.02)) == []
    # pag-akyat: bagong high => wake (para tumakbo ang ratchet/ladder ngayon)
    assert t.crossed("HUIZ", _quote(bid=9.10, mid=9.12)) == [11]
    # flat/pababa: walang wake
    assert t.crossed("HUIZ", _quote(bid=9.05, mid=9.07)) == []
    # lampas sa dating high: wake ulit
    assert t.crossed("HUIZ", _quote(bid=9.20, mid=9.22)) == [11]


def test_stop_cross_takes_priority_over_new_high():
    t = _tracker_with("SDOT", [
        {"session_id": 12, "state": STATE_LIVE_ENTERED, "stop_px": 16.76, "target_px": 0.0},
    ])
    t._hi[12] = 17.0
    # breach: iisang hit lang (hindi dinodoble ng new-high branch)
    assert t.crossed("SDOT", _quote(bid=16.70, mid=16.72)) == [12]


def test_refresh_prunes_high_of_departed_sessions():
    t = il._SessionCrossTracker()
    t._hi[99] = 5.0
    with patch.object(il, "SessionLocal") as mock_sl:
        mock_sl.return_value.query.return_value.filter.return_value.all.return_value = []
        t.refresh()
    assert 99 not in t._hi


def test_session_refresh_faster_than_universe():
    """Ang session inventory ay hindi dapat maging bulag nang 20s — hiwalay na
    ~5s cadence; ang universe ay nananatili sa ~20s ritmo."""
    assert il._SESSION_REFRESH_S < il._UNIVERSE_REFRESH_S
    assert il._SESSION_REFRESH_S <= 5.0


def test_post_wake_refresh_hook_called():
    calls: list[str] = []
    old = il._post_wake_session_refresh
    il._post_wake_session_refresh = lambda: calls.append("refreshed")
    try:
        with patch(
            "app.services.trading.momentum_neural.captured_paper_dispatcher."
            "dispatch_live_runner_tick",
            side_effect=lambda db, sid: None,
        ):
            il._wake_runner_tick(77)
        assert calls == ["refreshed"]
    finally:
        il._post_wake_session_refresh = old


# ── _spawn_session_wake spacing + kill switch ───────────────────────────────

def test_session_wake_spacing_dedups():
    il._session_wake_last.clear()
    started: list[int] = []
    with patch.object(il.threading, "Thread") as mock_thread:
        mock_thread.return_value = SimpleNamespace(start=lambda: started.append(1))
        assert il._spawn_session_wake(101) is True
        # agad na pangalawa: sa loob ng min spacing => tanggi
        assert il._spawn_session_wake(101) is False
    il._wake_inflight.discard(101)
    il._session_wake_last.clear()


def test_session_wake_kill_switch(monkeypatch):
    il._session_wake_last.clear()
    from app.config import settings as real_settings

    # kill switch ang unang tsek — walang thread, walang spacing record
    monkeypatch.setattr(
        real_settings, "chili_momentum_session_tick_wake_enabled", False, raising=False
    )
    assert il._spawn_session_wake(102) is False
    assert 102 not in il._session_wake_last


def test_session_wake_single_flight_vs_inflight():
    il._session_wake_last.clear()
    with il._wake_inflight_lock:
        il._wake_inflight.add(103)
    try:
        assert il._spawn_session_wake(103) is False
    finally:
        with il._wake_inflight_lock:
            il._wake_inflight.discard(103)
        il._session_wake_last.clear()


# ── on-tick integration: crossing fires kahit sub-floor ang ignition ────────

def test_on_tick_wakes_session_below_ignition_floor():
    loop = il.IgnitionScoringLoop()
    loop._running = True
    loop._sessions = _tracker_with("SDOT", [
        {"session_id": 7, "state": STATE_LIVE_ENTERED, "stop_px": 16.76, "target_px": 0.0},
    ])
    woken: list[int] = []
    with patch.object(il, "_spawn_session_wake", side_effect=lambda sid: woken.append(sid)):
        # walang baseline sa universe tracker => ang ignition scoring ay
        # mag-e-early-return, pero ang session wake ay DAPAT pumutok pa rin
        loop._on_tick("SDOT", _quote(bid=16.70, mid=16.72))
    assert woken == [7]


# ── batch-safe stop-confirm redispatch ──────────────────────────────────────

def test_stop_confirm_prefers_loop_path():
    with patch(
        "app.services.trading.momentum_neural.live_runner_loop."
        "schedule_live_runner_stop_confirmation",
        return_value=True,
    ) as loop_sched:
        assert lr._schedule_stop_confirm_dispatch(55) is True
        loop_sched.assert_called_once_with(55)
    assert 55 not in lr._stop_confirm_wake_inflight


def test_stop_confirm_fallback_blocked_under_pytest_env():
    """Sa ilalim ng CHILI_PYTEST=1 (itong test run mismo) ang wall-clock Timer
    ay HINDI dapat ma-armahan — determinism ng replay/pytest ang dahilan."""
    with patch(
        "app.services.trading.momentum_neural.live_runner_loop."
        "schedule_live_runner_stop_confirmation",
        return_value=False,
    ):
        assert lr._schedule_stop_confirm_dispatch(56) is False
    assert 56 not in lr._stop_confirm_wake_inflight


def test_stop_confirm_fallback_arms_timer_outside_test_env(monkeypatch):
    monkeypatch.delenv("CHILI_PYTEST", raising=False)
    monkeypatch.delenv("CHILI_DIAGNOSTIC_REPLAY_ISOLATED", raising=False)
    armed: dict = {}

    class _FakeTimer:
        def __init__(self, delay, fn, args=()):
            armed["delay"] = delay
            armed["fn"] = fn
            armed["args"] = args

        def start(self):
            armed["started"] = True

    with patch(
        "app.services.trading.momentum_neural.live_runner_loop."
        "schedule_live_runner_stop_confirmation",
        return_value=False,
    ), patch.object(lr.threading, "Timer", _FakeTimer):
        assert lr._schedule_stop_confirm_dispatch(57) is True
    try:
        assert armed["started"] is True
        assert armed["delay"] == lr._STOP_CONFIRM_WAKE_DELAY_S
        assert armed["delay"] > 1.0  # lampas sa 1.0s flicker guard
        assert armed["args"] == (57,)
        # in-flight hanggang tumakbo ang wake tick
        assert 57 in lr._stop_confirm_wake_inflight
        # dedup habang in-flight
        with patch(
            "app.services.trading.momentum_neural.live_runner_loop."
            "schedule_live_runner_stop_confirmation",
            return_value=False,
        ), patch.object(lr.threading, "Timer", _FakeTimer):
            assert lr._schedule_stop_confirm_dispatch(57) is False
    finally:
        with lr._stop_confirm_wake_lock:
            lr._stop_confirm_wake_inflight.discard(57)


def test_stop_confirm_wake_tick_dispatches_and_clears_inflight():
    calls: list[int] = []
    with lr._stop_confirm_wake_lock:
        lr._stop_confirm_wake_inflight.add(58)
    with patch(
        "app.services.trading.momentum_neural.captured_paper_dispatcher."
        "dispatch_live_runner_tick",
        side_effect=lambda db, sid: calls.append(sid),
    ):
        lr._stop_confirm_wake_tick(58)
    assert calls == [58]
    assert 58 not in lr._stop_confirm_wake_inflight


def test_breach_call_sites_use_batch_safe_wrapper():
    """Ang bawat breach-confirm call site ay dapat dumaan sa wrapper — hindi na
    sa direktang loop-only scheduler na tahimik na False sa batch mode.

    Tatlong site (2026-08-27): ang dalawang orihinal mula #1109 (stop-breach
    pending-confirm at ang L2 chop-hold redispatch sa parehong stop path) at
    ang bailout dwell-confirm pending-confirm mula #1207
    (`_bailout_dwell_confirm_holds`, `bailout_breach_pending_confirm`). Kung
    magdagdag ng panibagong site, itaas ang bilang DITO at ilista ito sa itaas.
    """
    import inspect

    src = inspect.getsource(lr)
    assert src.count("_schedule_stop_confirm_dispatch(int(sess.id))") == 3
    # Walang call site ang lumalampas sa wrapper patungo sa loop-only scheduler.
    assert "schedule_live_runner_stop_confirmation(int(sess.id))" not in src
