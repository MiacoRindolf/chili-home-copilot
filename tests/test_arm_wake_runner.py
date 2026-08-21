"""ARM->RUNNER WAKE — ang 23-segundong gap sa pagitan ng arm at unang tick.

Benchmark (Ross 08-20 HUIZ): <10s mula signal hanggang posisyon. Ang armado ay
dating naghihintay sa scheduler batch cadence; ngayon ang bridge mismo ang
gumigising sa runner para sa kaka-armang session.
"""
from __future__ import annotations

import threading
import time

from app.config import settings
from app.services.trading.momentum_neural import ignition_loop as il


def _drain_inflight():
    with il._wake_inflight_lock:
        il._wake_inflight.clear()


def test_spawn_wake_dispatches_immediate_tick(monkeypatch):
    _drain_inflight()
    calls = []
    done = threading.Event()

    def _fake_dispatch(db, sid):
        calls.append(sid)
        done.set()
        return {"ok": True}

    import app.services.trading.momentum_neural.captured_paper_dispatcher as cpd
    monkeypatch.setattr(cpd, "dispatch_live_runner_tick", _fake_dispatch)
    from app.services.trading.momentum_neural import live_runner as lr
    monkeypatch.setattr(lr, "consume_entry_fsm_continuation", lambda sid: False)

    assert il._spawn_arm_wake(14777) is True
    assert done.wait(5.0), "hindi tumakbo ang wake tick"
    time.sleep(0.2)
    assert calls == [14777]
    with il._wake_inflight_lock:
        assert 14777 not in il._wake_inflight, "dapat lumabas sa inflight"


def test_wake_continuation_chains_up_to_max_steps(monkeypatch):
    _drain_inflight()
    calls = []
    done = threading.Event()

    def _fake_dispatch(db, sid):
        calls.append(sid)
        if len(calls) >= 3:
            done.set()
        return {"ok": True}

    import app.services.trading.momentum_neural.captured_paper_dispatcher as cpd
    monkeypatch.setattr(cpd, "dispatch_live_runner_tick", _fake_dispatch)
    from app.services.trading.momentum_neural import live_runner as lr
    seq = iter([True, True, False])
    monkeypatch.setattr(lr, "consume_entry_fsm_continuation", lambda sid: next(seq, False))
    monkeypatch.setattr(
        settings, "chili_momentum_entry_fsm_continuation_max_steps", 3, raising=False
    )

    assert il._spawn_arm_wake(14778) is True
    assert done.wait(5.0)
    time.sleep(0.2)
    assert calls == [14778, 14778, 14778], "3 chained na pass sa iisang wake"


def test_wake_is_single_flight_per_session(monkeypatch):
    _drain_inflight()
    started = threading.Event()
    release = threading.Event()

    def _slow_dispatch(db, sid):
        started.set()
        release.wait(5.0)
        return {"ok": True}

    import app.services.trading.momentum_neural.captured_paper_dispatcher as cpd
    monkeypatch.setattr(cpd, "dispatch_live_runner_tick", _slow_dispatch)
    from app.services.trading.momentum_neural import live_runner as lr
    monkeypatch.setattr(lr, "consume_entry_fsm_continuation", lambda sid: False)

    assert il._spawn_arm_wake(14779) is True
    assert started.wait(5.0)
    # habang tumatakbo pa: ang pangalawang spawn ay tinatanggihan
    assert il._spawn_arm_wake(14779) is False
    release.set()
    time.sleep(0.3)


def test_flag_off_disables_the_wake(monkeypatch):
    _drain_inflight()
    monkeypatch.setattr(
        settings, "chili_momentum_arm_wake_runner_enabled", False, raising=False
    )
    assert il._spawn_arm_wake(14780) is False


def test_bad_session_id_is_rejected():
    _drain_inflight()
    assert il._spawn_arm_wake(None) is False
    assert il._spawn_arm_wake("hindi-numero") is False


def test_bridge_arm_reports_wake(monkeypatch):
    import inspect

    src = inspect.getsource(il.IgnitionScoringLoop._bridge_arm)
    assert "_spawn_arm_wake" in src
    assert "armed+wake" in src
