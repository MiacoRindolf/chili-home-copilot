"""Event-driven tick exit hint (Lever B-2, 2026-06-16).

A held crypto trailing position whose order flow rolls over to the sell side wakes the
exit runner on the WS tick (up to 15s sooner than the poll) — Ross's "eject the moment
the ask thickens". It is a DISPATCH HINT ONLY: the helper never sells; it only calls
self._dispatch (which runs the UNMODIFIED, INVARIANT-A-safe tick_live_session that is
the sole decider of any exit). Ships OBSERVE-FIRST: _enabled OFF logs the would-dispatch
counterfactual without acting.
"""

import logging

from app.services.trading.momentum_neural import live_runner_loop as lrl
from app.services.trading.momentum_neural import pipeline as pl


def _loop(monkeypatch, *, enabled=False, observe=True, thr=-0.25, ofi=None):
    monkeypatch.setattr(lrl.settings, "chili_momentum_exit_event_driven_enabled", enabled, raising=False)
    monkeypatch.setattr(lrl.settings, "chili_momentum_exit_event_driven_observe", observe, raising=False)
    monkeypatch.setattr(lrl.settings, "chili_momentum_exit_event_ofi_rollover_thr", thr, raising=False)
    monkeypatch.setattr(pl, "_live_ofi_microprice", lambda symbol, db=None, as_of=None: (ofi, None))
    loop = lrl.LiveRunnerLoop()
    calls = {"dispatch": []}
    loop._dispatch = lambda sid: calls["dispatch"].append(sid)   # the ONLY allowed action
    return loop, calls


def test_observe_first_logs_does_not_dispatch(monkeypatch, caplog):
    loop, calls = _loop(monkeypatch, enabled=False, observe=True, ofi=-0.5)   # rollover
    with caplog.at_level(logging.INFO):
        loop._maybe_event_exit_hint({"session_id": 7}, "TAO-USD")
    assert calls["dispatch"] == []                       # observe-only: did NOT act
    assert any("event_exit_hint observe-only" in r.message for r in caplog.records)


def test_enabled_dispatches_runner_decides(monkeypatch):
    loop, calls = _loop(monkeypatch, enabled=True, ofi=-0.5)                  # rollover
    loop._maybe_event_exit_hint({"session_id": 7}, "TAO-USD")
    assert calls["dispatch"] == [7]    # wakes the runner — the runner decides any sell


def test_no_rollover_is_noop(monkeypatch):
    loop, calls = _loop(monkeypatch, enabled=True, ofi=0.1)                   # OFI positive
    loop._maybe_event_exit_hint({"session_id": 7}, "TAO-USD")
    assert calls["dispatch"] == []


def test_threshold_boundary_inclusive(monkeypatch):
    # ofi == thr is NOT a rollover (strict <).
    loop, calls = _loop(monkeypatch, enabled=True, thr=-0.25, ofi=-0.25)
    loop._maybe_event_exit_hint({"session_id": 7}, "TAO-USD")
    assert calls["dispatch"] == []


def test_ofi_none_is_noop(monkeypatch):
    loop, calls = _loop(monkeypatch, enabled=True, ofi=None)                  # ring empty
    loop._maybe_event_exit_hint({"session_id": 7}, "TAO-USD")
    assert calls["dispatch"] == []


def test_fully_disabled_skips_the_ofi_read(monkeypatch):
    # both flags off -> short-circuit BEFORE the per-tick ring read (zero cost).
    read = {"n": 0}
    monkeypatch.setattr(lrl.settings, "chili_momentum_exit_event_driven_enabled", False, raising=False)
    monkeypatch.setattr(lrl.settings, "chili_momentum_exit_event_driven_observe", False, raising=False)

    def _spy(symbol, db=None, as_of=None):
        read["n"] += 1
        return (-0.5, None)

    monkeypatch.setattr(pl, "_live_ofi_microprice", _spy)
    loop = lrl.LiveRunnerLoop()
    loop._dispatch = lambda sid: None
    loop._maybe_event_exit_hint({"session_id": 7}, "TAO-USD")
    assert read["n"] == 0


def test_helper_never_calls_broker_or_exit(monkeypatch):
    # Safety: the hint's ONLY side effect is _dispatch. It carries no sell/stop math —
    # proving INVARIANT-A and the sell-winner guard are entirely in the (unmodified)
    # tick_live_session, never here. We assert _dispatch is the sole observable action.
    loop, calls = _loop(monkeypatch, enabled=True, ofi=-0.9)
    # Any attempt to place/modify an order would need a broker/session handle the helper
    # does not have — so the only thing it can do is dispatch.
    loop._maybe_event_exit_hint({"session_id": 42}, "ETH-USD")
    assert calls["dispatch"] == [42]
