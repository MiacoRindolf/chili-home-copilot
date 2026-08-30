"""End-to-end latency ng ignition wake — patunay NGAYON, hindi Lunes.

Sinusukat: mula sa pagdating ng igniting tick sa ``_on_tick`` hanggang tumakbo
ang runner tick ng WATCHING session. Ang lumang landas ay scheduler batch
(sinukat p50 11.2s); ang bagong landas (#1247) ay dapat MILLISECOND-scale.
Ang tunay na market tape ay Lunes pa, pero ang wiring at ang makina-latency
ay napapatunayan dito nang buo.

Runnable: pytest tests/test_ignition_wake_latency.py -v -s
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural.ignition_loop import (
    IgnitionScoringLoop,
    STATE_WATCHING_LIVE,
    _SessionCrossTracker,
)


@pytest.fixture(autouse=True)
def _owning_process_role(monkeypatch):
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "momentum_exec_only")


class _StubTracker:
    def velocity_for(self, sym):
        return 12.5

    def rvol_for(self, sym):
        return 8.0

    def baseline_for(self, sym):
        return 5.00


def _bare_loop(seeded_sessions):
    loop = IgnitionScoringLoop.__new__(IgnitionScoringLoop)
    loop._running = True
    loop._tracker = _StubTracker()
    t = _SessionCrossTracker.__new__(_SessionCrossTracker)
    t._lock = threading.Lock()
    t._by_symbol = seeded_sessions
    t._hi = {}
    loop._sessions = t
    loop._last_score = {}
    loop._inflight = set()
    loop._inflight_lock = threading.Lock()
    loop._pool = None
    return loop


def test_igniting_tick_wakes_watching_session_in_milliseconds(monkeypatch):
    # linisin ang module-global spacing/inflight state
    with il._session_wake_lock:
        il._session_wake_last.clear()
    with il._wake_inflight_lock:
        il._wake_inflight.clear()

    ticked = threading.Event()
    seen = {}

    def _fake_two_phase(SL, sid):
        seen["sid"] = sid
        seen["t1"] = time.perf_counter()
        ticked.set()
        return {"ok": True}

    import app.services.trading.momentum_neural.captured_paper_dispatcher as cpd
    monkeypatch.setattr(cpd, "run_live_runner_tick_two_phase", _fake_two_phase)
    from app.services.trading.momentum_neural import live_runner as lr
    monkeypatch.setattr(lr, "consume_entry_fsm_continuation", lambda sid: False)

    # ang Ross floor ay pinapasa ng tunay na axes (velocity 12.5 / rvol 8 /
    # +25% move) — pero i-pin natin para deterministic ang floor mismo
    from app.services.trading.momentum_neural import nbbo_tape as nt
    monkeypatch.setattr(nt, "_ross_threshold_crossed", lambda *a, **k: True)

    loop = _bare_loop({
        "XPON": [{"session_id": 4242, "state": STATE_WATCHING_LIVE}],
    })
    quote = SimpleNamespace(bid=7.55, mid=7.56, price=7.56, change_pct=25.0)

    t0 = time.perf_counter()
    loop._on_tick("XPON", quote)
    assert ticked.wait(2.0), "hindi tumakbo ang runner tick mula sa ignition wake"
    elapsed_ms = (seen["t1"] - t0) * 1000.0
    print(f"\n[ignition-wake latency] tick->runner = {elapsed_ms:.1f} ms")
    assert seen["sid"] == 4242
    assert elapsed_ms < 500.0, f"masyadong mabagal: {elapsed_ms:.1f} ms"


def test_flag_off_falls_back_to_batch_only(monkeypatch):
    with il._session_wake_lock:
        il._session_wake_last.clear()
    with il._wake_inflight_lock:
        il._wake_inflight.clear()
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_momentum_ignition_wake_watching_enabled", False,
        raising=False,
    )
    called = threading.Event()
    import app.services.trading.momentum_neural.captured_paper_dispatcher as cpd
    monkeypatch.setattr(
        cpd, "run_live_runner_tick_two_phase",
        lambda SL, sid: called.set() or {"ok": True},
    )
    from app.services.trading.momentum_neural import nbbo_tape as nt
    monkeypatch.setattr(nt, "_ross_threshold_crossed", lambda *a, **k: True)

    loop = _bare_loop({
        "XPON": [{"session_id": 4243, "state": STATE_WATCHING_LIVE}],
    })
    quote = SimpleNamespace(bid=7.55, mid=7.56, price=7.56, change_pct=25.0)
    loop._on_tick("XPON", quote)
    assert not called.wait(0.5), "naka-OFF ang flag pero gumising pa rin"


def test_spacing_bounds_hot_tape(monkeypatch):
    # Dalawang sunod-sunod na igniting tick sa loob ng 2s spacing: ISANG wake.
    with il._session_wake_lock:
        il._session_wake_last.clear()
    with il._wake_inflight_lock:
        il._wake_inflight.clear()
    count = {"n": 0}
    done = threading.Event()
    import app.services.trading.momentum_neural.captured_paper_dispatcher as cpd

    def _rec(SL, sid):
        count["n"] += 1
        done.set()
        return {"ok": True}

    monkeypatch.setattr(cpd, "run_live_runner_tick_two_phase", _rec)
    from app.services.trading.momentum_neural import live_runner as lr
    monkeypatch.setattr(lr, "consume_entry_fsm_continuation", lambda sid: False)
    from app.services.trading.momentum_neural import nbbo_tape as nt
    monkeypatch.setattr(nt, "_ross_threshold_crossed", lambda *a, **k: True)

    loop = _bare_loop({
        "XPON": [{"session_id": 4244, "state": STATE_WATCHING_LIVE}],
    })
    quote = SimpleNamespace(bid=7.55, mid=7.56, price=7.56, change_pct=25.0)
    loop._on_tick("XPON", quote)
    loop._on_tick("XPON", quote)
    assert done.wait(2.0)
    time.sleep(0.3)
    assert count["n"] == 1, f"dapat isang wake lang sa spacing window, nakita {count['n']}"
