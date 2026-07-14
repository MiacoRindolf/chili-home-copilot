"""LiveRunnerLoop (Stage 2 websocket rail) — dispatch semantics, no DB/broker.

The loop's job is narrow: turn a price-bus tick into an IMMEDIATE
tick_live_session dispatch when (and only when) a tracked threshold is
breached — debounced, bounded, never on the WS thread.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.live_runner_loop as loop_mod
from app.services.trading.momentum_neural.live_runner_loop import (
    _EVENT_TICK_MIN_SPACING_S,
    _STOP_CONFIRM_DELAY_S,
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


_IQFEED_PIN = "iqfeed-l1-quote-provenance-v2+sha256:0123456789abcdef"
_IQFEED_RUN_ID = "12553525-2da8-4b22-a69f-d3034871e90c"


def _iqfeed_payload(now: datetime, **overrides) -> str:
    reference = now - timedelta(milliseconds=200)
    received = now - timedelta(milliseconds=100)
    payload = {
        "symbol": "ACTU",
        "observed_at": reference.isoformat(),
        "bid": 1.47,
        "ask": 1.48,
        "received_at": received.isoformat(),
        "provider_event_at": None,
        "provider_trade_reference_at": reference.isoformat(),
        "timestamp_basis": "iqfeed_q_receive_trade_reference_fenced",
        "source": "iqfeed_l1",
        "bridge_version": _IQFEED_PIN,
        "message_type": "Q",
        "bridge_run_id": _IQFEED_RUN_ID,
        "connection_generation": 2,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


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


def test_stop_confirmation_timer_survives_inflight_tick(monkeypatch):
    loop = LiveRunnerLoop()
    loop._running = True
    calls: list[int] = []
    timers = []
    first_started = loop_mod.threading.Event()
    release_first = loop_mod.threading.Event()
    second_ran = loop_mod.threading.Event()

    def _blocking_tick(sid):
        calls.append(sid)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2.0)
        else:
            second_ran.set()

    loop._tick_session = _blocking_tick  # type: ignore
    loop._pool = loop_mod.ThreadPoolExecutor(max_workers=1)  # type: ignore

    class _Timer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.daemon = False
            self.name = ""
            timers.append(self)

        def start(self):
            return None

    monkeypatch.setattr(loop_mod.threading, "Timer", _Timer)

    try:
        loop._dispatch(77)
        assert first_started.wait(timeout=2.0)
        assert loop.schedule_stop_confirmation(77) is True
        assert timers[0].interval == _STOP_CONFIRM_DELAY_S
        assert 1.0 < timers[0].interval < _EVENT_TICK_MIN_SPACING_S
        assert timers[0].daemon is True

        # Fire the backstop while the first tick is still in flight. Previously
        # `_dispatch` dropped this callback permanently.
        loop._last_event_tick[77] = time.monotonic() - (
            _EVENT_TICK_MIN_SPACING_S + 0.1
        )
        timers[0].callback()
        with loop._inflight_lock:
            assert 77 in loop._stop_confirm_redispatch

        release_first.set()
        assert second_ran.wait(timeout=2.0)
        assert calls == [77, 77]
    finally:
        release_first.set()
        loop._running = False
        loop._pool.shutdown(wait=True)


def test_stop_confirmation_bypasses_ordinary_event_debounce():
    loop = LiveRunnerLoop()
    loop._running = True
    ran = []

    def _fake_tick(sid):
        ran.append(sid)

    class _SyncPool:
        def submit(self, fn, *args):
            fn(*args)

    loop._tick_session = _fake_tick  # type: ignore
    loop._pool = _SyncPool()  # type: ignore
    loop._last_event_tick[88] = time.monotonic()

    loop._dispatch(88, guarantee_after_inflight=True)

    assert ran == [88]


def test_fresh_exact_build_q_notify_dispatches_once(monkeypatch):
    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    dispatched = []
    loop._tracker.get_sessions_for_symbol = lambda _sym: [{"session_id": 77}]  # type: ignore
    loop._dispatch = lambda sid, **_kwargs: (dispatched.append(sid) or True)  # type: ignore
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )
    payload = _iqfeed_payload(now)

    assert loop._handle_iqfeed_notify_payload(payload, generation=1) is True
    assert loop._handle_iqfeed_notify_payload(payload, generation=1) is False
    assert dispatched == [77]


@pytest.mark.parametrize(
    "case",
    [
        "invalid_json",
        "raw_symbol",
        "wrong_source",
        "lowercase_symbol",
        "crypto_symbol",
        "wrong_basis",
        "wrong_build",
        "summary_p",
        "invalid_run",
        "zero_generation",
        "naive_receive",
        "non_utc_receive",
        "stale_reference",
        "future_receive",
        "bad_spread",
    ],
)
def test_notify_rejects_any_uncertified_field(monkeypatch, case):
    now = datetime.now(timezone.utc)
    reference = now - timedelta(milliseconds=200)
    overrides = {}
    if case == "invalid_json":
        payload = "{broken"
    elif case == "raw_symbol":
        payload = "ACTU"
    else:
        if case == "wrong_source":
            overrides["source"] = "iqfeed_depth"
        elif case == "lowercase_symbol":
            overrides["symbol"] = "actu"
        elif case == "crypto_symbol":
            overrides["symbol"] = "BTC-USD"
        elif case == "wrong_basis":
            overrides["timestamp_basis"] = "bridge_received_at"
        elif case == "wrong_build":
            overrides["bridge_version"] = (
                "iqfeed-l1-quote-provenance-v2+sha256:ffffffffffffffff"
            )
        elif case == "summary_p":
            overrides["message_type"] = "P"
        elif case == "invalid_run":
            overrides["bridge_run_id"] = "not-a-uuid"
        elif case == "zero_generation":
            overrides["connection_generation"] = 0
        elif case == "naive_receive":
            overrides["received_at"] = now.replace(tzinfo=None).isoformat()
        elif case == "non_utc_receive":
            overrides["received_at"] = now.astimezone(
                timezone(timedelta(hours=-4))
            ).isoformat()
        elif case == "stale_reference":
            stale = now - timedelta(seconds=3)
            overrides.update(
                observed_at=stale.isoformat(),
                provider_trade_reference_at=stale.isoformat(),
            )
        elif case == "future_receive":
            overrides["received_at"] = (now + timedelta(seconds=1.01)).isoformat()
        elif case == "bad_spread":
            overrides.update(bid=1.49, ask=1.48)
        payload = _iqfeed_payload(now, **overrides)

    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    loop._tracker.get_sessions_for_symbol = lambda _sym: [{"session_id": 77}]  # type: ignore
    loop._dispatch = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore
        AssertionError("uncertified notify dispatched")
    )
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )

    assert loop._handle_iqfeed_notify_payload(payload, generation=1) is False


def test_notify_generation_never_rolls_back_within_bridge_run(monkeypatch):
    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    dispatched = []
    loop._tracker.get_sessions_for_symbol = lambda _sym: [{"session_id": 77}]  # type: ignore
    loop._dispatch = lambda sid, **_kwargs: (dispatched.append(sid) or True)  # type: ignore
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )

    assert loop._handle_iqfeed_notify_payload(
        _iqfeed_payload(now, connection_generation=3), generation=1
    ) is True
    assert loop._handle_iqfeed_notify_payload(
        _iqfeed_payload(
            now,
            connection_generation=2,
            received_at=(now - timedelta(milliseconds=50)).isoformat(),
        ),
        generation=1,
    ) is False
    assert dispatched == [77]


def test_notify_watermark_waits_for_successful_dispatch(monkeypatch):
    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    outcomes = iter([False, True])
    calls = []
    loop._tracker.get_sessions_for_symbol = lambda _sym: [{"session_id": 77}]  # type: ignore

    def _dispatch(sid, **_kwargs):
        calls.append(sid)
        return next(outcomes)

    loop._dispatch = _dispatch  # type: ignore
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )
    payload = _iqfeed_payload(now)

    assert loop._handle_iqfeed_notify_payload(payload, generation=1) is False
    assert loop._iqfeed_certified_watermarks == {}
    assert loop._handle_iqfeed_notify_payload(payload, generation=1) is True
    assert len(loop._iqfeed_certified_watermarks) == 1
    assert calls == [77, 77]


def test_unhandled_higher_generation_does_not_poison_later_event(monkeypatch):
    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    outcomes = iter([False, True])
    loop._tracker.get_sessions_for_symbol = lambda _sym: [{"session_id": 77}]  # type: ignore
    loop._dispatch = lambda *_args, **_kwargs: next(outcomes)  # type: ignore
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )

    assert loop._handle_iqfeed_notify_payload(
        _iqfeed_payload(now, connection_generation=3), generation=1
    ) is False
    assert loop._iqfeed_generation_watermarks == {}
    assert loop._iqfeed_inflight_certified == set()

    assert loop._handle_iqfeed_notify_payload(
        _iqfeed_payload(
            now,
            connection_generation=2,
            received_at=(now - timedelta(milliseconds=50)).isoformat(),
        ),
        generation=1,
    ) is True
    assert loop._iqfeed_generation_watermarks[_IQFEED_RUN_ID] == 2


def test_concurrent_duplicate_notify_has_one_inflight_owner(monkeypatch):
    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    dispatch_started = loop_mod.threading.Event()
    release_dispatch = loop_mod.threading.Event()
    calls = []
    results = []
    loop._tracker.get_sessions_for_symbol = lambda _sym: [{"session_id": 77}]  # type: ignore

    def _dispatch(sid, **_kwargs):
        calls.append(sid)
        dispatch_started.set()
        assert release_dispatch.wait(timeout=2.0)
        return True

    loop._dispatch = _dispatch  # type: ignore
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )
    payload = _iqfeed_payload(now)
    first = loop_mod.threading.Thread(
        target=lambda: results.append(
            loop._handle_iqfeed_notify_payload(payload, generation=1)
        )
    )
    first.start()
    try:
        assert dispatch_started.wait(timeout=2.0)
        # The full tuple is reserved, but not yet accepted/watermarked.
        assert loop._handle_iqfeed_notify_payload(payload, generation=1) is False
        assert loop._iqfeed_certified_watermarks == {}
    finally:
        release_dispatch.set()
        first.join(timeout=2.0)

    assert not first.is_alive()
    assert results == [True]
    assert calls == [77]
    assert loop._iqfeed_inflight_certified == set()
    assert len(loop._iqfeed_certified_watermarks) == 1


@pytest.mark.parametrize(
    "admission_result",
    [
        {"admitted": True, "skipped": None, "session_id": 77},
        {
            "admitted": False,
            "skipped": "already_active",
            "session_id": 77,
        },
        {
            "admitted": False,
            "skipped": "already_active",
            "session_id": 77,
            "begin": {"deduped": True},
        },
    ],
)
def test_iqfeed_admission_commits_before_existing_dispatch_for_all_session_paths(
    monkeypatch,
    admission_result,
):
    from app.services.trading.momentum_neural import ross_event_admission

    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    loop._tracker.set_owner_generation(1, clear=True)
    sessions = []
    trace = []
    loop._tracker.get_sessions_for_symbol = lambda _sym: list(sessions)  # type: ignore

    def _refresh(**kwargs):
        trace.append(("refresh", kwargs))
        sessions[:] = [{"session_id": 77}]

    def _dispatch(session_id, **kwargs):
        trace.append(("dispatch", {"session_id": session_id, **kwargs}))
        return True

    loop._tracker.refresh = _refresh  # type: ignore
    loop._dispatch = _dispatch  # type: ignore

    class _Db:
        def commit(self):
            trace.append(("commit", {}))

        def rollback(self):
            trace.append(("rollback", {}))

        def close(self):
            trace.append(("close", {}))

    def _admit(*_args, **kwargs):
        assert kwargs["defer_live_ticks_until_commit"] is True
        trace.append(("admit_no_tick", {}))
        return dict(admission_result)

    monkeypatch.setattr(loop_mod, "SessionLocal", _Db)
    monkeypatch.setattr(ross_event_admission, "admit_ross_event", _admit)
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )

    assert loop._handle_iqfeed_notify_payload(
        _iqfeed_payload(now),
        generation=1,
    ) is True

    names = [name for name, _payload in trace]
    assert names.index("commit") < names.index("refresh") < names.index("dispatch")
    assert names.count("dispatch") == 1


def test_iqfeed_admission_blocked_across_stop_rolls_back_and_blocks_restart(
    monkeypatch,
):
    from app.services.trading.momentum_neural import ross_event_admission

    now = datetime.now(timezone.utc)
    loop = LiveRunnerLoop()
    monkeypatch.setattr(loop, "_acquire_owner_fence", lambda: True)
    monkeypatch.setattr(loop, "_owner_fence_is_held", lambda: True)
    loop._running = True
    loop._generation = 1
    loop._tracker.set_owner_generation(1, clear=True)
    loop._tracker.get_sessions_for_symbol = lambda _sym: []  # type: ignore
    refresh_calls = []
    loop._tracker.refresh = lambda **kwargs: refresh_calls.append(kwargs)  # type: ignore
    entered = loop_mod.threading.Event()
    release = loop_mod.threading.Event()
    handler_results = []
    db_calls = {"commit": 0, "rollback": 0, "close": 0}

    class _Db:
        def commit(self):
            db_calls["commit"] += 1

        def rollback(self):
            db_calls["rollback"] += 1

        def close(self):
            db_calls["close"] += 1

    def _blocking_admit(*_args, **kwargs):
        assert kwargs["defer_live_ticks_until_commit"] is True
        entered.set()
        assert release.wait(timeout=2.0)
        return {"admitted": True, "skipped": None}

    monkeypatch.setattr(loop_mod, "SessionLocal", _Db)
    monkeypatch.setattr(ross_event_admission, "admit_ross_event", _blocking_admit)
    monkeypatch.setattr(loop_mod, "_utcnow", lambda: now)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )
    payload = _iqfeed_payload(now)
    handler = loop_mod.threading.Thread(
        target=lambda: handler_results.append(
            loop._handle_iqfeed_notify_payload(payload, generation=1)
        )
    )
    handler.start()
    try:
        assert entered.wait(timeout=2.0)
        assert loop.stop() is True
        # The old generation is stopped, but its DB work has not quiesced yet.
        assert loop.start() is False
        assert loop._iqfeed_admission_inflight
    finally:
        release.set()
        handler.join(timeout=2.0)

    assert not handler.is_alive()
    assert handler_results == [False]
    assert db_calls["commit"] == 0
    assert db_calls["rollback"] >= 1
    assert db_calls["close"] == 1
    assert refresh_calls == []
    assert loop._iqfeed_admission_inflight == {}

    # Once the old admission has rolled back and retired, restart is allowed.
    loop._tracker.refresh = lambda **_kwargs: True  # type: ignore
    loop._tracker.get_all_symbols = lambda: set()  # type: ignore
    loop._tracker.count = lambda: 0  # type: ignore
    loop._subscribe_active_symbols = lambda **_kwargs: True  # type: ignore
    loop._record_lane_health_heartbeat = lambda **_kwargs: True  # type: ignore
    loop._start_iqfeed_notify_listener = lambda *_args: None  # type: ignore
    try:
        assert loop.start() is True
    finally:
        loop.stop()


def test_old_generation_tracker_refresh_cannot_overwrite_new_snapshot(monkeypatch):
    tracker = loop_mod._LiveSessionTracker()
    old_entered = loop_mod.threading.Event()
    release_old = loop_mod.threading.Event()
    sessions = iter(
        [
            (
                SimpleNamespace(
                    id=1,
                    symbol="OLD",
                    state="live_trailing",
                    risk_snapshot_json={},
                ),
                old_entered,
                release_old,
            ),
            (
                SimpleNamespace(
                    id=2,
                    symbol="NEW",
                    state="live_trailing",
                    risk_snapshot_json={},
                ),
                None,
                None,
            ),
        ]
    )
    session_lock = loop_mod.threading.Lock()

    class _Query:
        def __init__(self, row, entered, release):
            self.row = row
            self.entered = entered
            self.release = release

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            if self.entered is not None:
                self.entered.set()
                assert self.release.wait(timeout=2.0)
            return [self.row]

    class _Db:
        def __init__(self, row, entered, release):
            self.query_result = _Query(row, entered, release)

        def query(self, _model):
            return self.query_result

        def rollback(self):
            return None

        def close(self):
            return None

    def _session_local():
        with session_lock:
            row, entered, release = next(sessions)
        return _Db(row, entered, release)

    monkeypatch.setattr(loop_mod, "SessionLocal", _session_local)
    tracker.set_owner_generation(1, clear=True)
    old_results = []
    old_refresh = loop_mod.threading.Thread(
        target=lambda: old_results.append(
            tracker.refresh(expected_generation=1)
        )
    )
    old_refresh.start()
    try:
        assert old_entered.wait(timeout=2.0)
        tracker.set_owner_generation(2, clear=True)
        assert tracker.refresh(expected_generation=2) is True
    finally:
        release_old.set()
        old_refresh.join(timeout=2.0)

    assert not old_refresh.is_alive()
    assert old_results == [False]
    assert tracker.get_sessions_for_symbol("OLD") == []
    assert [row["session_id"] for row in tracker.get_sessions_for_symbol("NEW")] == [2]


def test_iqfeed_listener_registration_cannot_spoof_lane_health(monkeypatch):
    loop = LiveRunnerLoop()
    loop._running = True
    heartbeats = []
    listened = []

    class _Cursor:
        def execute(self, sql):
            listened.append(sql)

    class _Connection:
        notifies = []

        def set_session(self, **_kwargs):
            return None

        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    fake_psycopg2 = SimpleNamespace(connect=lambda _url: _Connection())
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_psycopg2)
    monkeypatch.setattr(
        loop,
        "_record_lane_health_heartbeat",
        lambda **_kwargs: heartbeats.append(time.monotonic()),
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_channel",
        "momentum_iqfeed_l1",
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )

    select_calls = {"count": 0}

    def _one_quiet_select(_read, _write, _errors, _timeout):
        select_calls["count"] += 1
        if select_calls["count"] >= 2:
            loop._running = False
        return [], [], []

    monkeypatch.setattr(loop_mod.select, "select", _one_quiet_select)

    loop._iqfeed_notify_loop()

    assert listened == ["LISTEN momentum_iqfeed_l1;"]
    # LISTEN registration alone proves neither tracker refresh nor price-bus exit
    # ownership, so it cannot keep the owner-health signal green.
    assert heartbeats == []


def test_successful_generation_owned_refresh_records_lane_health(monkeypatch):
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 7
    heartbeats = []
    refreshes = []
    subscriptions = []

    monkeypatch.setattr(
        loop,
        "_record_lane_health_heartbeat",
        lambda *, generation, force=False: heartbeats.append(
            (generation, force)
        ) or True,
    )
    monkeypatch.setattr(
        loop._tracker,
        "refresh",
        lambda *, expected_generation: refreshes.append(expected_generation) or True,
    )
    monkeypatch.setattr(
        loop,
        "_subscribe_active_symbols",
        lambda *, generation: subscriptions.append(generation) or True,
    )

    class _OneRefresh:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return False

        def wait(self, _timeout):
            self.waits += 1
            return self.waits > 1

    loop._refresh_loop(7, _OneRefresh())

    assert refreshes == [7]
    assert subscriptions == [7]
    assert heartbeats == [(7, False)]


def test_price_bus_refresh_failure_cannot_record_lane_health(monkeypatch):
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 7
    heartbeats = []
    monkeypatch.setattr(
        loop._tracker,
        "refresh",
        lambda *, expected_generation: expected_generation == 7,
    )
    monkeypatch.setattr(
        loop,
        "_subscribe_active_symbols",
        lambda *, generation: False,
    )
    monkeypatch.setattr(
        loop,
        "_record_lane_health_heartbeat",
        lambda **kwargs: heartbeats.append(kwargs) or True,
    )

    class _OneRefresh:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return False

        def wait(self, _timeout):
            self.waits += 1
            return self.waits > 1

    loop._refresh_loop(7, _OneRefresh())

    assert heartbeats == []


def test_durable_lane_health_heartbeat_is_completed_throttled_and_generation_owned(
    monkeypatch,
):
    from app.services.trading.momentum_neural import lane_health as lane_health_mod

    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 4
    loop._generation_started_at_utc = datetime.now(timezone.utc) - timedelta(
        seconds=5
    )
    staged = []
    sessions = []

    class _Db:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closes = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closes += 1

    def _session_local():
        db = _Db()
        sessions.append(db)
        return db

    monkeypatch.setattr(loop_mod, "SessionLocal", _session_local)
    monkeypatch.setattr(
        lane_health_mod,
        "record_live_runner_loop_run",
        lambda db, **kwargs: staged.append((db, kwargs)) or "job-id",
    )

    assert loop._record_lane_health_heartbeat(generation=4, force=True) is True
    assert loop._record_lane_health_heartbeat(generation=4) is True
    assert len(sessions) == 1
    assert staged == [
        (
            sessions[0],
            {
                "owner_instance_id": loop._owner_instance_id,
                "generation": 4,
                "generation_started_at": loop._generation_started_at_utc,
            },
        )
    ]
    assert sessions[0].commits == 1
    assert sessions[0].closes == 1

    loop._running = False
    assert loop._record_lane_health_heartbeat(generation=4, force=True) is False
    assert len(sessions) == 1


def test_module_start_self_guards_single_driver_configuration(monkeypatch):
    starts = []

    class _Owner:
        def start(self):
            starts.append("start")
            return True

    monkeypatch.setattr(loop_mod, "get_live_runner_loop", lambda: _Owner())
    monkeypatch.setattr(
        loop_mod.settings, "chili_autopilot_price_bus_enabled", True
    )
    monkeypatch.setattr(
        loop_mod.settings, "chili_momentum_live_runner_enabled", True
    )

    monkeypatch.setattr(
        loop_mod.settings, "chili_momentum_live_runner_loop_enabled", False
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_scheduler_enabled",
        False,
    )
    assert loop_mod.start_live_runner_loop() is False

    monkeypatch.setattr(
        loop_mod.settings, "chili_momentum_live_runner_loop_enabled", True
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_scheduler_enabled",
        True,
    )
    assert loop_mod.start_live_runner_loop() is False

    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_scheduler_enabled",
        False,
    )
    assert loop_mod.start_live_runner_loop() is True
    assert starts == ["start"]


def test_stop_then_quick_restart_has_one_listener_and_dispatch_owner(monkeypatch):
    from app.services.trading import price_bus as price_bus_mod

    class _FakeBus:
        def __init__(self):
            self.callbacks = {}
            self.provider_subscribe_calls = []

        def subscribe_symbol(self, symbol):
            self.provider_subscribe_calls.append(symbol)

        def register_tick_listener(self, symbol, callback):
            self.callbacks.setdefault(symbol, []).append(callback)

        def unregister_tick_listener(self, symbol, callback):
            callbacks = self.callbacks.get(symbol, [])
            try:
                callbacks.remove(callback)
            except ValueError:
                pass
            if not callbacks:
                self.callbacks.pop(symbol, None)

        def fire(self, symbol, quote):
            for callback in list(self.callbacks.get(symbol, [])):
                callback(symbol, quote)

    bus = _FakeBus()
    monkeypatch.setattr(price_bus_mod, "get_price_bus", lambda: bus)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )

    loop = LiveRunnerLoop()
    monkeypatch.setattr(loop, "_acquire_owner_fence", lambda: True)
    monkeypatch.setattr(loop, "_owner_fence_is_held", lambda: True)
    session = {
        "session_id": 77,
        "symbol": "ACTU",
        "state": "live_trailing",
        "stop_px": 1.45,
        "target_px": 2.00,
    }
    loop._tracker.refresh = lambda **_kwargs: True  # type: ignore
    loop._tracker.get_all_symbols = lambda: {"ACTU"}  # type: ignore
    loop._tracker.get_sessions_for_symbol = lambda _sym: [session]  # type: ignore
    loop._tracker.count = lambda: 1  # type: ignore
    loop._record_lane_health_heartbeat = lambda **_kwargs: True  # type: ignore

    notify_started = loop_mod.threading.Event()
    active_lock = loop_mod.threading.Lock()
    active = {"count": 0, "max": 0, "starts": 0, "exits": 0}

    def _fake_notify(generation, stop_event):
        with active_lock:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            active["starts"] += 1
        notify_started.set()
        try:
            while loop._generation_active(generation, stop_event):
                if stop_event.wait(0.01):
                    break
        finally:
            with active_lock:
                active["count"] -= 1
                active["exits"] += 1

    loop._iqfeed_notify_loop = _fake_notify  # type: ignore
    dispatched = []
    loop._dispatch = lambda sid, **_kwargs: dispatched.append(sid)  # type: ignore

    try:
        assert loop.start() is True
        assert notify_started.wait(timeout=2.0)
        assert len(bus.callbacks.get("ACTU", [])) == 1
        assert bus.provider_subscribe_calls == ["ACTU"]

        assert loop.stop() is True
        assert bus.callbacks.get("ACTU", []) == []
        assert active["count"] == 0

        notify_started.clear()
        assert loop.start() is True
        assert notify_started.wait(timeout=2.0)
        assert len(bus.callbacks.get("ACTU", [])) == 1
        # Restart reattaches this owner's callback without duplicating the
        # process-lifetime Massive/Coinbase provider subscription.
        assert bus.provider_subscribe_calls == ["ACTU"]
        assert active["max"] == 1

        bus.fire("ACTU", _q(bid=1.40))
        assert dispatched == [77]
    finally:
        loop.stop()

    assert bus.callbacks.get("ACTU", []) == []
    assert active["starts"] == 2
    assert active["exits"] == 2
    assert active["count"] == 0


def test_postgres_owner_fence_allows_only_one_process_generation():
    first = LiveRunnerLoop()
    contender = LiveRunnerLoop()
    try:
        assert first._acquire_owner_fence() is True
        assert first._owner_fence_is_held() is True
        assert contender._acquire_owner_fence() is False

        first._release_owner_fence()
        assert contender._acquire_owner_fence() is True
        assert contender._owner_fence_is_held() is True
    finally:
        first._release_owner_fence()
        contender._release_owner_fence()


def test_failed_owner_fence_unlock_invalidates_backend():
    loop = LiveRunnerLoop()

    class _BrokenFenceConnection:
        def __init__(self):
            self.invalidated = False
            self.closed = False

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("connection lost before unlock")

        def invalidate(self):
            self.invalidated = True

        def close(self):
            self.closed = True

    conn = _BrokenFenceConnection()
    loop._owner_fence_connection = conn
    loop._owner_fence_generation = 4

    loop._release_owner_fence()

    assert conn.invalidated is True
    assert conn.closed is True
    assert loop._owner_fence_connection is None
    assert loop._owner_fence_generation is None


def test_refresh_retires_generation_immediately_when_owner_fence_is_lost(
    monkeypatch,
):
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 9
    loop._generation_started_at_utc = datetime.now(timezone.utc)
    loop._owner_fence_generation = 9
    retired = []
    heartbeats = []
    monkeypatch.setattr(loop, "_owner_fence_is_held", lambda: False)
    monkeypatch.setattr(
        loop,
        "stop",
        lambda: retired.append(9) or setattr(loop, "_running", False) or True,
    )
    monkeypatch.setattr(
        loop,
        "_record_lane_health_heartbeat",
        lambda **kwargs: heartbeats.append(kwargs) or True,
    )

    class _OneRefresh:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return False

        def wait(self, _timeout):
            self.waits += 1
            return self.waits > 1

    loop._refresh_loop(9, _OneRefresh())

    assert retired == [9]
    assert heartbeats == []


def test_restart_refuses_while_prior_generation_tick_is_inflight():
    loop = LiveRunnerLoop()
    loop._running = False
    loop._generation = 8
    with loop._inflight_lock:
        loop._inflight.add(77)

    assert loop.start() is False
    assert loop._running is False
    assert loop._generation == 8

    with loop._inflight_lock:
        loop._inflight.clear()


def test_scheduler_shutdown_stops_out_of_band_live_loop(monkeypatch):
    from app.services import trading_scheduler
    from app.services import trading_service

    stopped = []
    signaled = []
    monkeypatch.setattr(
        loop_mod,
        "stop_live_runner_loop",
        lambda: stopped.append("loop"),
    )
    monkeypatch.setattr(
        trading_service,
        "signal_shutdown",
        lambda: signaled.append("scheduler"),
    )
    monkeypatch.setattr(trading_scheduler, "_scheduler", None)

    trading_scheduler.stop_scheduler()

    assert stopped == ["loop"]
    assert signaled == ["scheduler"]
