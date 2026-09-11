"""A receipt DB wait must not block the loop that keeps subscriptions fresh."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import app.services.trading.momentum_neural.ignition_loop as il
import app.services.trading.momentum_neural.ignition_receipts as ir


def _loop(monkeypatch):
    loop = il.IgnitionScoringLoop()
    monkeypatch.setattr(loop._tracker, "refresh", lambda: set())
    loop._tracker._pending_onsets = [{"symbol": "FIRST"}]
    loop._sessions = SimpleNamespace(refresh=lambda: None, symbols=lambda: set())
    return loop


def _blocked_writer(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def write(onset):
        seen.append(onset["symbol"])
        entered.set()
        assert release.wait(5), "test must release the simulated DB lock"
        return {"recorded": False, "subscribed": False}

    monkeypatch.setattr(ir, "record_snapshot_onset", write)
    return entered, release, seen


def _finish(loop, release):
    release.set()
    loop._running = False
    for name in ("_onset_receipt_worker", "_refresher"):
        worker = getattr(loop, name, None)
        if worker is not None:
            worker.join(2)
            assert not worker.is_alive()
    if loop._pool is not None:
        loop._pool.shutdown(wait=True)


def test_startup_completes_while_receipt_writer_waits(monkeypatch):
    loop = _loop(monkeypatch)
    entered, release, seen = _blocked_writer(monkeypatch)
    synced = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(il.settings, "chili_momentum_ws_ignition_enabled", True)
    monkeypatch.setattr(il, "_post_wake_session_refresh", None)
    monkeypatch.setattr(loop, "_sync_subscriptions", synced.set)
    monkeypatch.setattr(loop, "_refresh_loop", lambda: None)

    def start():
        try:
            loop.start()
        finally:
            finished.set()

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert entered.wait(1)
        assert finished.wait(1), "startup waited for receipt I/O"
        assert synced.is_set()
        assert il._post_wake_session_refresh is not None
        assert seen == ["FIRST"]
        assert not release.is_set()
    finally:
        release.set()
        starter.join(2)
        _finish(loop, release)


def test_blocked_writer_keeps_refresh_heartbeat_and_bounded_backlog_live(monkeypatch, caplog):
    loop = _loop(monkeypatch)
    entered, release, seen = _blocked_writer(monkeypatch)
    clock = {"now": 0.0}
    synced = []
    heartbeat = []
    loop._running = True
    monkeypatch.setattr(il.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(il.time, "sleep", lambda _: clock.update(now=clock["now"] + il._UNIVERSE_REFRESH_S))
    monkeypatch.setattr(loop, "_write_observation_heartbeat", lambda: heartbeat.append(clock["now"]))

    def sync():
        assert entered.wait(1)
        synced.append(clock["now"])
        if len(synced) == 1:
            with loop._tracker._lock:
                loop._tracker._pending_onsets.append({"symbol": "SECOND"})
        else:
            loop._running = False

    monkeypatch.setattr(loop, "_sync_subscriptions", sync)
    runner = threading.Thread(target=loop._refresh_loop, daemon=True)
    runner.start()
    try:
        runner.join(1)
        assert not runner.is_alive(), "refresh waited for receipt I/O"
        assert len(synced) == 2
        assert heartbeat == [2 * il._UNIVERSE_REFRESH_S]
        assert seen == ["FIRST"], "busy publisher spawned a second receipt worker"
        assert loop._tracker.pending_onset_count() == 1
        assert any("writer busy" in record.message for record in caplog.records)
        release.set()
        loop._onset_receipt_worker.join(2)
        assert loop._queue_onset_receipts()
        loop._onset_receipt_worker.join(2)
        assert seen == ["FIRST", "SECOND"]
        assert loop._tracker.pending_onset_count() == 0
    finally:
        release.set()
        loop._running = False
        runner.join(2)
        _finish(loop, release)
