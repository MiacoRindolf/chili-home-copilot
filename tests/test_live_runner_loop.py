"""LiveRunnerLoop (Stage 2 websocket rail) — dispatch semantics, no DB/broker.

The loop's job is narrow: turn a price-bus tick into an IMMEDIATE
tick_live_session dispatch when (and only when) a tracked threshold is
breached — debounced, bounded, never on the WS thread.
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.captured_paper_dispatcher as dispatch_mod
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


_IQFEED_PIN = "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"
_IQFEED_RUN_ID = "12553525-2da8-4b22-a69f-d3034871e90c"
_CAPTURED_PAPER_ACCOUNT_ID = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
_CAPTURED_PAPER_GENERATION = "f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3"


def _captured_paper_owner_marker(
    *,
    session_id: int,
    symbol: str,
    account_id: str = _CAPTURED_PAPER_ACCOUNT_ID,
    generation: str = _CAPTURED_PAPER_GENERATION,
) -> dict:
    request = dispatch_mod.CapturedPaperDispatchRequest(
        session_id=session_id,
        symbol=symbol,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=account_id,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=generation,
        first_dip_policy_mode="candidate",
    )
    return dispatch_mod.captured_paper_session_owner_marker(request)


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


def test_entry_continuation_runs_post_inflight_without_scheduler_spacing():
    loop = LiveRunnerLoop()
    loop._running = True
    ran: list[int] = []

    def _mechanical_three_state_tick(sid):
        ran.append(sid)
        if len(ran) < 3:
            assert loop.schedule_entry_continuation(sid) is True

    class _SyncPool:
        def submit(self, fn, *args):
            fn(*args)

    loop._tick_session = _mechanical_three_state_tick  # type: ignore
    loop._pool = _SyncPool()  # type: ignore
    loop._last_event_tick[99] = time.monotonic()

    # candidate -> pending -> pre-submit runs as three committed invocations,
    # but neither transition waits for the ordinary two-second debounce or a
    # minute-scale scheduler pulse.
    assert loop._dispatch(99, guarantee_after_inflight=True) is True
    assert ran == [99, 99, 99]
    assert loop._stop_confirm_redispatch == {}


def test_entry_continuation_is_noop_without_active_owner():
    loop = LiveRunnerLoop()
    assert loop.schedule_entry_continuation(99) is False


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


def test_captured_paper_inventory_validator_accepts_exact_owner_marker():
    marker = _captured_paper_owner_marker(session_id=41, symbol="ACTU")
    row = SimpleNamespace(
        id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_owner": deepcopy(marker),
        },
    )

    verified = dispatch_mod.validate_captured_paper_session_owner_inventory(
        row,
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        expected_runtime_generation=_CAPTURED_PAPER_GENERATION,
        expected_execution_family="alpaca_spot",
    )

    assert verified == marker
    assert verified is not marker


def test_captured_paper_inventory_validator_rejects_missing_owner_marker():
    row = SimpleNamespace(
        id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
        },
    )

    with pytest.raises(
        dispatch_mod.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_session_owner_missing",
    ):
        dispatch_mod.validate_captured_paper_session_owner_inventory(
            row,
            expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
            expected_runtime_generation=_CAPTURED_PAPER_GENERATION,
        )


def test_captured_paper_inventory_validator_rejects_owner_hash_tamper():
    marker = _captured_paper_owner_marker(session_id=41, symbol="ACTU")
    marker["config_sha256"] = "9" * 64
    row = SimpleNamespace(
        id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_owner": marker,
        },
    )

    with pytest.raises(
        dispatch_mod.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_session_owner_marker_hash_mismatch",
    ):
        dispatch_mod.validate_captured_paper_session_owner_inventory(
            row,
            expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
            expected_runtime_generation=_CAPTURED_PAPER_GENERATION,
        )


def test_captured_paper_inventory_validator_rejects_empty_owner_generation():
    marker = _captured_paper_owner_marker(session_id=41, symbol="ACTU")
    marker["runtime_generation"] = ""
    row = SimpleNamespace(
        id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_owner": marker,
        },
    )

    with pytest.raises(
        dispatch_mod.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_session_owner_marker_invalid",
    ):
        dispatch_mod.validate_captured_paper_session_owner_inventory(
            row,
            expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
            expected_runtime_generation=_CAPTURED_PAPER_GENERATION,
        )


@pytest.mark.parametrize(
    ("row_overrides", "scope_overrides", "reason"),
    [
        (
            {"id": 42},
            {},
            "captured_paper_session_owner_route_mismatch",
        ),
        (
            {"symbol": "OTHER"},
            {},
            "captured_paper_session_owner_route_mismatch",
        ),
        (
            {"execution_family": "coinbase_spot"},
            {},
            "captured_paper_session_owner_route_mismatch",
        ),
        (
            {},
            {"expected_account_id": "3929f568-40fb-4c1d-a263-4c3d0ee38ae8"},
            "captured_paper_session_owner_inventory_scope_mismatch",
        ),
        (
            {},
            {
                "expected_runtime_generation": (
                    "be1694d0-76c9-477a-a355-8fb110276302"
                )
            },
            "captured_paper_session_owner_inventory_scope_mismatch",
        ),
        (
            {},
            {"expected_execution_family": "coinbase_spot"},
            "captured_paper_session_owner_inventory_scope_invalid",
        ),
    ],
)
def test_captured_paper_inventory_validator_rejects_route_or_scope_drift(
    row_overrides,
    scope_overrides,
    reason,
):
    row_values = {
        "id": 41,
        "symbol": "ACTU",
        "execution_family": "alpaca_spot",
    }
    row_values.update(row_overrides)
    row = SimpleNamespace(
        **row_values,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_owner": _captured_paper_owner_marker(
                session_id=41,
                symbol="ACTU",
            ),
        },
    )
    scope = {
        "expected_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
        "expected_runtime_generation": _CAPTURED_PAPER_GENERATION,
        "expected_execution_family": "alpaca_spot",
    }
    scope.update(scope_overrides)

    with pytest.raises(
        dispatch_mod.CapturedPaperRuntimeUnavailableError,
        match=reason,
    ):
        dispatch_mod.validate_captured_paper_session_owner_inventory(
            row,
            **scope,
        )


def test_captured_paper_tracker_rejects_entire_foreign_runnable_inventory(
    monkeypatch,
):
    account_id = _CAPTURED_PAPER_ACCOUNT_ID
    generation = _CAPTURED_PAPER_GENERATION
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=account_id,
        runtime_generation=generation,
    )
    tracker = loop_mod._LiveSessionTracker(scope)
    exact = SimpleNamespace(
        id=1,
        symbol="ACTU",
        state="live_trailing",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": account_id,
            "captured_paper_runtime_generation": generation,
            "captured_paper_session_owner": _captured_paper_owner_marker(
                session_id=1,
                symbol="ACTU",
            ),
        },
    )
    foreign = SimpleNamespace(
        id=2,
        symbol="BTC-USD",
        state="live_trailing",
        execution_family="coinbase_spot",
        risk_snapshot_json={},
    )

    class _Query:
        def filter(self, *_args):
            return self

        def all(self):
            return [exact, foreign]

    class _Db:
        def query(self, _model):
            return _Query()

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(loop_mod, "SessionLocal", _Db)
    tracker.set_owner_generation(1, clear=True)

    assert tracker.refresh(expected_generation=1) is False
    assert tracker.count() == 0
    assert tracker.scope_is_healthy() is False


def test_captured_paper_tracker_ignores_distinct_preowner_state(monkeypatch):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    tracker = loop_mod._LiveSessionTracker(scope)
    preowner = SimpleNamespace(
        id=40,
        symbol="PREP",
        state="captured_paper_preowner",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "schema_version": "chili.captured-paper-preowner-risk-snapshot.v1",
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_preowner": {
                "schema_version": "chili.captured-paper-session-preowner.v1",
            },
        },
    )
    owned = SimpleNamespace(
        id=41,
        symbol="ACTU",
        state="live_trailing",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_owner": _captured_paper_owner_marker(
                session_id=41,
                symbol="ACTU",
            ),
        },
    )

    class _Query:
        def filter(self, *_args):
            return self

        def all(self):
            # Defense-in-depth: a real SQL query excludes PREOWNER because it
            # is outside LIVE_RUNNER_RUNNABLE_STATES.  Even if a query/mock
            # returns it, the dedicated tracker never aliases it to OWNER.
            return [preowner, owned]

    class _Db:
        def query(self, _model):
            return _Query()

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(loop_mod, "SessionLocal", _Db)
    tracker.set_owner_generation(1, clear=True)

    assert tracker.refresh(expected_generation=1) is True
    assert tracker.scope_is_healthy() is True
    assert tracker.get_sessions_for_symbol("PREP") == []
    assert [
        row["session_id"] for row in tracker.get_sessions_for_symbol("ACTU")
    ] == [41]


def test_captured_paper_tracker_rejects_runnable_preowner_without_final_owner(
    monkeypatch,
):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    tracker = loop_mod._LiveSessionTracker(scope)
    invalid = SimpleNamespace(
        id=40,
        symbol="PREP",
        state="live_pending_entry",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "schema_version": "chili.captured-paper-preowner-risk-snapshot.v1",
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _CAPTURED_PAPER_ACCOUNT_ID,
            "captured_paper_session_preowner": {
                "schema_version": "chili.captured-paper-session-preowner.v1",
            },
        },
    )

    class _Query:
        def filter(self, *_args):
            return self

        def all(self):
            return [invalid]

    class _Db:
        def query(self, _model):
            return _Query()

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(loop_mod, "SessionLocal", _Db)
    tracker.set_owner_generation(1, clear=True)

    assert tracker.refresh(expected_generation=1) is False
    assert tracker.count() == 0
    assert tracker.scope_is_healthy() is False


def test_captured_paper_loop_tick_uses_strict_dispatch_only(monkeypatch):
    account_id = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
    generation = "f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3"
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=account_id,
        runtime_generation=generation,
    )
    loop = LiveRunnerLoop(captured_paper_scope=scope)
    lifecycle = SimpleNamespace(
        commits=0,
        rollbacks=0,
        closes=0,
        transaction_active=False,
    )

    def begin():
        lifecycle.transaction_active = True

    def commit():
        lifecycle.commits += 1
        lifecycle.transaction_active = False

    def rollback():
        lifecycle.rollbacks += 1
        lifecycle.transaction_active = False

    db = SimpleNamespace(
        begin=begin,
        in_transaction=lambda: lifecycle.transaction_active,
        commit=commit,
        rollback=rollback,
        close=lambda: setattr(lifecycle, "closes", lifecycle.closes + 1),
    )
    calls = []
    monkeypatch.setattr(loop_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        loop_mod,
        "dispatch_live_runner_tick",
        lambda *_args, **_kwargs: pytest.fail("ordinary dispatcher is forbidden"),
    )
    monkeypatch.setattr(
        loop_mod,
        "dispatch_captured_paper_live_runner_tick",
        lambda owned_db, session_id, **kwargs: calls.append(
            (owned_db, session_id, kwargs)
        ),
    )

    loop._tick_session(41)

    assert calls == [
        (
            db,
            41,
            {
                "expected_account_id": account_id,
                "expected_runtime_generation": generation,
                "expected_execution_family": "alpaca_spot",
            },
        )
    ]
    assert lifecycle.commits == 1
    assert lifecycle.closes == 1


def test_expired_initial_release_refreshes_tracker_after_commit(monkeypatch):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    loop = LiveRunnerLoop(captured_paper_scope=scope)
    loop._running = True
    loop._generation = 7
    loop._stop_event = loop_mod.threading.Event()
    lifecycle = SimpleNamespace(commits=0, rollbacks=0, closes=0)
    db = SimpleNamespace(
        commit=lambda: setattr(lifecycle, "commits", lifecycle.commits + 1),
        rollback=lambda: setattr(lifecycle, "rollbacks", lifecycle.rollbacks + 1),
        close=lambda: setattr(lifecycle, "closes", lifecycle.closes + 1),
    )
    refreshes = []
    monkeypatch.setattr(loop_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        loop_mod,
        "dispatch_captured_paper_live_runner_tick",
        lambda *_args, **_kwargs: {
            "ok": True,
            "reason": "captured_paper_initial_authority_expired_released",
            "refresh_session_inventory": True,
            "opportunity_consumed": False,
            "risk_reserved": False,
            "outbox_created": False,
            "order_posted": False,
            "broker_order_post_calls": 0,
        },
    )
    monkeypatch.setattr(
        loop._tracker,
        "refresh",
        lambda **kwargs: refreshes.append(kwargs) or True,
    )

    loop._tick_session(41)

    assert lifecycle.commits == 1
    assert lifecycle.closes == 1
    assert refreshes == [{"expected_generation": 7}]


def test_captured_paper_iqfeed_event_refuses_legacy_symbol_admission(monkeypatch):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id="d7cc580c-2b8f-432f-b771-1cecfb3fe87a",
        runtime_generation="f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3",
    )
    loop = LiveRunnerLoop(captured_paper_scope=scope)
    monkeypatch.setattr(
        loop_mod,
        "SessionLocal",
        lambda: pytest.fail("legacy captured PAPER admission opened the database"),
    )

    result = loop._admit_iqfeed_symbol(
        "ACTU",
        {"source": "iqfeed_l1"},
        expected_generation=1,
    )

    assert result == {
        "ok": False,
        "admitted": False,
        "skipped": "captured_paper_sealed_symbol_admission_unavailable",
        "symbol": "ACTU",
        "opportunity_consumed": False,
        "risk_reserved": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }


@pytest.mark.parametrize("admitter", [None, object()])
def test_captured_paper_start_rejects_missing_or_noncallable_admitter(
    monkeypatch,
    admitter,
):
    monkeypatch.setattr(loop_mod, "_loop", None)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_autopilot_price_bus_enabled",
        True,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_enabled",
        True,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_enabled",
        True,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_scheduler_enabled",
        False,
    )

    assert (
        loop_mod.start_captured_paper_live_runner_loop(
            expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
            runtime_generation=_CAPTURED_PAPER_GENERATION,
            captured_paper_symbol_admitter=admitter,
        )
        is False
    )
    assert loop_mod._loop is None


@pytest.mark.parametrize(
    "case",
    [
        "listener_disabled",
        "bridge_env_missing",
        "bridge_notify_disabled",
        "channel_mismatch",
        "uppercase_channel",
        "bad_build",
        "stale_v2_build",
    ],
)
def test_captured_paper_start_refuses_invalid_iqfeed_admission_contract(
    monkeypatch,
    case,
):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    loop = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
        case != "listener_disabled",
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_channel",
        (
            "consumer_channel"
            if case == "channel_mismatch"
            else ("Momentum_Iqfeed_L1" if case == "uppercase_channel" else "momentum_iqfeed_l1")
        ),
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        (
            "unreviewed"
            if case == "bad_build"
            else (
                "iqfeed-l1-quote-provenance-v2+sha256:0123456789abcdef"
                if case == "stale_v2_build"
                else _IQFEED_PIN
            )
        ),
        raising=False,
    )
    if case == "bridge_env_missing":
        monkeypatch.delenv("IQFEED_NOTIFY_ENABLED", raising=False)
        monkeypatch.delenv("IQFEED_NOTIFY_CHANNEL", raising=False)
    else:
        monkeypatch.setenv(
            "IQFEED_NOTIFY_ENABLED",
            "0" if case == "bridge_notify_disabled" else "1",
        )
        monkeypatch.setenv(
            "IQFEED_NOTIFY_CHANNEL",
            "Momentum_Iqfeed_L1" if case == "uppercase_channel" else "momentum_iqfeed_l1",
        )
    fence_calls = []
    monkeypatch.setattr(
        loop,
        "_acquire_owner_fence",
        lambda: fence_calls.append("fence") or True,
    )

    assert loop.start() is False
    assert fence_calls == []
    assert loop._running is False
    assert loop._generation == 0


def _install_captured_paper_start_fakes(monkeypatch, loop):
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
        True,
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
    monkeypatch.setenv("IQFEED_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("IQFEED_NOTIFY_CHANNEL", "momentum_iqfeed_l1")

    def _acquire():
        loop._owner_fence_connection = object()
        return True

    def _release():
        loop._owner_fence_connection = None
        loop._owner_fence_generation = None

    monkeypatch.setattr(loop, "_acquire_owner_fence", _acquire)
    monkeypatch.setattr(loop, "_owner_fence_is_held", lambda: True)
    monkeypatch.setattr(loop, "_release_owner_fence", _release)
    monkeypatch.setattr(loop._tracker, "refresh", lambda **_kwargs: True)
    monkeypatch.setattr(loop._tracker, "get_all_symbols", lambda: set())
    monkeypatch.setattr(loop._tracker, "count", lambda: 0)
    monkeypatch.setattr(loop, "_subscribe_active_symbols", lambda **_kwargs: True)
    monkeypatch.setattr(
        loop,
        "_record_lane_health_heartbeat",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        loop,
        "_refresh_loop",
        lambda _generation, stop_event: stop_event.wait(),
    )


def test_captured_paper_start_waits_for_generation_owned_notify_listener(
    monkeypatch,
):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    loop = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=lambda **_kwargs: {"ok": True},
    )
    _install_captured_paper_start_fakes(monkeypatch, loop)
    release_listener = loop_mod.threading.Event()

    def _notify(generation, stop_event):
        loop._mark_iqfeed_notify_listener_ready(generation)
        while loop._generation_active(generation, stop_event):
            if release_listener.wait(0.01):
                return

    monkeypatch.setattr(loop, "_iqfeed_notify_loop", _notify)

    try:
        assert loop.start() is True
        assert loop.admission_owner_ready() is True
        assert loop._notify_thread_generation == loop._generation
        release_listener.set()
        loop._notify_thread.join(timeout=2.0)
        assert not loop._notify_thread.is_alive()
        assert loop.admission_owner_ready() is False
    finally:
        loop.stop()


def test_captured_paper_listener_startup_failure_rolls_back_owner(monkeypatch):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    loop = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=lambda **_kwargs: {"ok": True},
    )
    _install_captured_paper_start_fakes(monkeypatch, loop)

    def _notify(generation, _stop_event):
        loop._mark_iqfeed_notify_listener_failed(generation)

    monkeypatch.setattr(loop, "_iqfeed_notify_loop", _notify)

    with pytest.raises(
        RuntimeError,
        match="IQFeed notify listener failed startup",
    ):
        loop.start()

    assert loop._running is False
    assert loop._owner_fence_connection is None
    assert loop._notify_thread is None
    assert loop._notify_thread_generation is None
    assert loop.admission_owner_ready() is False


def test_captured_paper_admission_runs_under_exact_generation_and_refreshes(
    monkeypatch,
):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    calls = []
    loop = None

    def admitter(*, symbol, payload):
        calls.append(
            {
                "symbol": symbol,
                "payload": payload,
                "inflight": list(loop._iqfeed_admission_inflight.values()),
            }
        )
        return {
            "ok": True,
            "admitted": True,
            "session_id": 41,
            "symbol": symbol,
        }

    loop = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=admitter,
    )
    loop._running = True
    loop._generation = 7
    refreshes = []
    monkeypatch.setattr(
        loop._tracker,
        "refresh",
        lambda **kwargs: refreshes.append(kwargs) or True,
    )
    payload = {"source": "iqfeed_l1", "message_type": "Q"}

    assert (
        loop._admit_iqfeed_symbol(
            "ACTU",
            payload,
            expected_generation=6,
        )
        is None
    )
    assert calls == []

    result = loop._admit_iqfeed_symbol(
        "ACTU",
        payload,
        expected_generation=7,
    )

    assert result == {
        "ok": True,
        "admitted": True,
        "session_id": 41,
        "symbol": "ACTU",
    }
    assert calls == [
        {
            "symbol": "ACTU",
            "payload": payload,
            "inflight": [(7, "ACTU")],
        }
    ]
    assert refreshes == [{"expected_generation": 7}]
    assert loop._iqfeed_admission_inflight == {}


def test_ordinary_loop_cannot_install_captured_paper_admitter():
    with pytest.raises(
        ValueError,
        match="ordinary live loop cannot install captured PAPER admission",
    ):
        LiveRunnerLoop(captured_paper_symbol_admitter=lambda **_kwargs: {})


def test_captured_paper_singleton_retry_requires_exact_admitter_identity(
    monkeypatch,
):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    admitter = lambda **_kwargs: {"ok": True}
    selected = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=admitter,
    )
    starts = []
    monkeypatch.setattr(selected, "start", lambda: starts.append("start") or True)
    monkeypatch.setattr(loop_mod, "_loop", selected)
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_autopilot_price_bus_enabled",
        True,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_enabled",
        True,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_enabled",
        True,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_scheduler_enabled",
        False,
    )

    assert (
        loop_mod.start_captured_paper_live_runner_loop(
            expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
            runtime_generation=_CAPTURED_PAPER_GENERATION,
            captured_paper_symbol_admitter=admitter,
        )
        is True
    )
    assert (
        loop_mod.start_captured_paper_live_runner_loop(
            expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
            runtime_generation=_CAPTURED_PAPER_GENERATION,
            captured_paper_symbol_admitter=(
                lambda **_kwargs: {"ok": True}
            ),
        )
        is False
    )
    assert starts == ["start"]


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

    # The ordinary loop registers the authority channel AND the ignition
    # nomination channel (live by default) on the same connection.
    assert listened == [
        "LISTEN momentum_iqfeed_l1;",
        "LISTEN momentum_iqfeed_ignition;",
    ]
    # LISTEN registration alone proves neither tracker refresh nor price-bus exit
    # ownership, so it cannot keep the owner-health signal green.
    assert heartbeats == []


def test_captured_paper_listener_is_not_ready_during_reconnect_gap(monkeypatch):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    loop = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=lambda **_kwargs: {"ok": True},
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
    monkeypatch.setenv("IQFEED_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("IQFEED_NOTIFY_CHANNEL", "momentum_iqfeed_l1")

    class _Cursor:
        def execute(self, _sql):
            return None

    class _Connection:
        notifies = []

        def set_session(self, **_kwargs):
            return None

        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    connections = iter((_Connection(), _Connection()))
    monkeypatch.setitem(
        __import__("sys").modules,
        "psycopg2",
        SimpleNamespace(connect=lambda _url: next(connections)),
    )

    observations = []

    class _StopEvent:
        def is_set(self):
            return False

        def wait(self, _timeout=None):
            observations.append(
                (
                    "reconnect_gap",
                    loop._iqfeed_notify_listener_alive_for_generation(1, self),
                )
            )
            return False

    stop_event = _StopEvent()
    loop._running = True
    loop._generation = 1
    loop._stop_event = stop_event
    loop._notify_thread_generation = 1
    loop._notify_startup_event = loop_mod.threading.Event()
    loop._notify_thread = loop_mod.threading.current_thread()
    select_calls = {"count": 0}

    def _disconnect_then_quiesce(_read, _write, _errors, _timeout):
        select_calls["count"] += 1
        observations.append(
            (
                "before_disconnect" if select_calls["count"] == 1 else "reconnected",
                loop._iqfeed_notify_listener_alive_for_generation(1, stop_event),
            )
        )
        if select_calls["count"] == 1:
            raise RuntimeError("simulated LISTEN disconnect")
        loop._running = False
        return [], [], []

    monkeypatch.setattr(loop_mod.select, "select", _disconnect_then_quiesce)

    loop._iqfeed_notify_loop(1, stop_event)

    assert observations == [
        ("before_disconnect", True),
        ("reconnect_gap", False),
        ("reconnected", True),
    ]
    assert loop._notify_ready_generation is None
    assert loop._notify_failed_generation == 1


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


# ── IGNITION nomination channel (tick-based early-mover; 2026-07-17) ──────────


def _ignition_payload(now: datetime, **overrides) -> str:
    payload = {
        "schema": "chili.iqfeed-ignition-nominate.v1",
        "symbol": "PLSM",
        "source": "ignition_tick",
        "fired_at": (now - timedelta(seconds=1)).isoformat(),
        "last_price": 4.98,
        "pct_change_60s": 0.084,
        "dollar_vol_60s": 516361.0,
        "prints_10s": 103,
        "bridge_run_id": _IQFEED_RUN_ID,
        "connection_generation": 2,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _ignition_loop(monkeypatch, sessions=()):
    loop = LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    loop._tracker.get_sessions_for_symbol = lambda sym: list(sessions)  # type: ignore
    admissions = []

    def _admit(symbol, payload, *, expected_generation):
        admissions.append((symbol, payload.get("source"), expected_generation))
        return {"ok": True, "admitted": True, "symbol": symbol}

    monkeypatch.setattr(loop, "_admit_iqfeed_symbol", _admit)
    dispatches = []
    monkeypatch.setattr(
        loop,
        "_dispatch",
        lambda sid, expected_generation=None: dispatches.append(sid) or True,
    )
    return loop, admissions, dispatches


def test_ignition_payload_admits_with_ignition_tick_source(monkeypatch):
    loop, admissions, _dispatches = _ignition_loop(monkeypatch)
    now = datetime.now(timezone.utc)

    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=1
    ) is True
    assert admissions == [("PLSM", "ignition_tick", 1)]


def test_ignition_dispatches_existing_sessions_without_admission(monkeypatch):
    loop, admissions, dispatches = _ignition_loop(
        monkeypatch,
        sessions=[{"session_id": 42, "symbol": "PLSM"}],
    )
    now = datetime.now(timezone.utc)

    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=1
    ) is True
    assert admissions == []
    assert dispatches == [42]


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "chili.iqfeed-ignition-nominate.v0"},
        {"source": "iqfeed_l1"},
        {"symbol": "plsm"},
        {"symbol": "PLSM."},
        {"symbol": None},
        {"fired_at": "not-a-time"},
        {"last_price": 0},
        {"last_price": -1.5},
        {"last_price": True},
    ],
)
def test_ignition_rejects_malformed_payloads(monkeypatch, overrides):
    loop, admissions, _dispatches = _ignition_loop(monkeypatch)
    now = datetime.now(timezone.utc)

    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now, **overrides), generation=1
    ) is False
    assert admissions == []


def test_ignition_rejects_stale_and_future_fired_at(monkeypatch):
    loop, admissions, _dispatches = _ignition_loop(monkeypatch)
    now = datetime.now(timezone.utc)

    stale = _ignition_payload(
        now, fired_at=(now - timedelta(seconds=45)).isoformat()
    )
    future = _ignition_payload(
        now, fired_at=(now + timedelta(seconds=5)).isoformat()
    )
    assert loop._handle_iqfeed_ignition_payload(stale, generation=1) is False
    assert loop._handle_iqfeed_ignition_payload(future, generation=1) is False
    assert admissions == []


def test_ignition_per_symbol_dedup_ttl_blocks_repeat(monkeypatch):
    loop, admissions, _dispatches = _ignition_loop(monkeypatch)
    now = datetime.now(timezone.utc)

    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=1
    ) is True
    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=1
    ) is False
    assert len(admissions) == 1


def test_ignition_admits_per_minute_hard_cap(monkeypatch):
    loop, admissions, _dispatches = _ignition_loop(monkeypatch)
    now = datetime.now(timezone.utc)

    for idx in range(loop_mod._IGNITION_ADMITS_PER_MINUTE + 3):
        loop._handle_iqfeed_ignition_payload(
            _ignition_payload(now, symbol=f"CAP{idx}"), generation=1
        )
    assert len(admissions) == loop_mod._IGNITION_ADMITS_PER_MINUTE


def test_ignition_refused_for_captured_paper_scope(monkeypatch):
    scope = loop_mod.CapturedPaperLiveRunnerScope(
        expected_account_id=_CAPTURED_PAPER_ACCOUNT_ID,
        runtime_generation=_CAPTURED_PAPER_GENERATION,
    )
    loop = LiveRunnerLoop(
        captured_paper_scope=scope,
        captured_paper_symbol_admitter=lambda **_kwargs: {"ok": True},
    )
    loop._running = True
    loop._generation = 1
    admissions = []
    monkeypatch.setattr(
        loop,
        "_admit_iqfeed_symbol",
        lambda *a, **k: admissions.append(a) or {"admitted": True},
    )
    now = datetime.now(timezone.utc)

    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=1
    ) is False
    assert admissions == []
    # The captured-paper loop never LISTENs to the ignition channel either.
    assert loop._ignition_listen_channel("momentum_iqfeed_l1") is None


def test_ignition_listen_channel_validation(monkeypatch):
    loop = LiveRunnerLoop()
    assert (
        loop._ignition_listen_channel("momentum_iqfeed_l1")
        == "momentum_iqfeed_ignition"
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_ignition_channel",
        "momentum_iqfeed_l1",
        raising=False,
    )
    # Colliding with the authority channel is refused.
    assert loop._ignition_listen_channel("momentum_iqfeed_l1") is None
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_ignition_channel",
        "bad;channel",
        raising=False,
    )
    assert loop._ignition_listen_channel("momentum_iqfeed_l1") is None
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_ignition_channel",
        "momentum_iqfeed_ignition",
        raising=False,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_live_runner_loop_iqfeed_ignition_enabled",
        False,
        raising=False,
    )
    assert loop._ignition_listen_channel("momentum_iqfeed_l1") is None


def test_ignition_inactive_generation_is_refused(monkeypatch):
    loop, admissions, _dispatches = _ignition_loop(monkeypatch)
    now = datetime.now(timezone.utc)

    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=7
    ) is False
    loop._running = False
    assert loop._handle_iqfeed_ignition_payload(
        _ignition_payload(now), generation=1
    ) is False
    assert admissions == []
