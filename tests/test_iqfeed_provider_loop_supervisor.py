from __future__ import annotations

import inspect
import threading
import time
from types import MethodType

import pytest

from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
)
from scripts import iqfeed_capture_host as host_module
from scripts import iqfeed_depth_bridge as depth_bridge
from scripts import iqfeed_trade_bridge as trade_bridge


def test_trade_writer_never_runs_historical_tick_retention() -> None:
    source = inspect.getsource(trade_bridge.writer)

    assert "DELETE FROM iqfeed_trade_ticks" not in source
    assert "subscribe-hint coordination cleanup" in source


def _run_in_thread(target):
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            target()
        except BaseException as exc:  # asserted by the caller
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    return thread, errors


def _reset_ross_refresh_state(
    monkeypatch,
    *,
    now: float | None = 100.0,
) -> dict[str, float]:
    """Isolate the bridge's process-local subscription-universe cache."""

    monkeypatch.setattr(
        trade_bridge,
        "_ross_universe_cache_lock",
        threading.Lock(),
        raising=False,
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_symbols", (), raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_success_at", None, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_max_age_s", None, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_refresh_due_at", 0.0, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_refresh_inflight", False, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_refresh_thread", None, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_last_error_code", None, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_last_error_detail", None, raising=False
    )
    clock = {"now": float(now if now is not None else time.monotonic())}
    if now is not None:
        monkeypatch.setattr(trade_bridge.time, "monotonic", lambda: clock["now"])
    return clock


def test_ross_universe_refresh_never_blocks_writer_and_singleflights(monkeypatch):
    from app.services.trading.momentum_neural import universe

    _reset_ross_refresh_state(monkeypatch)
    fetch_entered = threading.Event()
    release_fetch = threading.Event()
    fetch_finished = threading.Event()
    fetch_calls: list[object] = []

    def blocking_fetch(profile):
        fetch_calls.append(profile)
        fetch_entered.set()
        assert release_fetch.wait(timeout=2.0)
        fetch_finished.set()
        return [" yxt ", "YXT", "BJDX"]

    monkeypatch.setattr(universe, "build_equity_universe", blocking_fetch)
    result_box = {}
    returned = threading.Event()

    def read_from_writer_thread() -> None:
        result_box["read"] = trade_bridge._ross_universe_symbols_read(2)
        returned.set()

    caller = threading.Thread(target=read_from_writer_thread)
    caller.start()
    try:
        assert returned.wait(timeout=0.5), "Ross refresh blocked the tape writer"
        assert fetch_entered.wait(timeout=0.5)
        first = result_box["read"]
        assert first.ok is False
        assert first.error_code == "ross_refresh_pending"

        second = trade_bridge._ross_universe_symbols_read(2)
        assert second.ok is False
        assert len(fetch_calls) == 1
    finally:
        release_fetch.set()
        caller.join(timeout=1.0)

    assert fetch_finished.wait(timeout=1.0)
    refresh_thread = trade_bridge._ross_universe_refresh_thread
    assert refresh_thread is not None
    refresh_thread.join(timeout=1.0)
    assert not refresh_thread.is_alive()

    cached = trade_bridge._ross_universe_symbols_read(1)
    assert cached.ok is True
    assert cached.symbols == ("YXT",)
    assert fetch_calls == [universe.EQUITY_ROSS_SMALLCAP]


def test_ross_universe_refresh_failure_never_erases_last_success(monkeypatch):
    from app.services.trading.momentum_neural import universe

    clock = _reset_ross_refresh_state(monkeypatch)
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_symbols", ("CACHED",), raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_success_at", 99.0, raising=False
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_max_age_s", 300.0, raising=False
    )
    release_fetch = threading.Event()
    fetch_calls = 0

    def empty_fetch(_profile):
        nonlocal fetch_calls
        fetch_calls += 1
        assert release_fetch.wait(timeout=2.0)
        return []

    monkeypatch.setattr(universe, "build_equity_universe", empty_fetch)
    pending = trade_bridge._ross_universe_symbols_read(10)
    assert pending.ok is True
    assert pending.symbols == ("CACHED",)

    release_fetch.set()
    refresh_thread = trade_bridge._ross_universe_refresh_thread
    assert refresh_thread is not None
    refresh_thread.join(timeout=1.0)
    assert not refresh_thread.is_alive()

    failed = trade_bridge._ross_universe_symbols_read(10)
    assert failed.ok is False
    assert failed.error_code == "ross_universe_empty_or_unavailable"
    assert trade_bridge._ross_universe_cache_symbols == ("CACHED",)

    # The builder's 30-second timeout may leave its own provider daemon alive.
    # Do not create another provider attempt just REFRESH_S later; the profile's
    # semantic snapshot TTL is the conservative failure retry boundary.
    first_refresh_thread = trade_bridge._ross_universe_refresh_thread
    clock["now"] = 121.0
    still_failed = trade_bridge._ross_universe_symbols_read(10)
    assert still_failed.ok is False
    assert trade_bridge._ross_universe_refresh_thread is first_refresh_thread
    assert trade_bridge._ross_universe_refresh_due_at == 400.0
    assert fetch_calls == 1


def test_trade_writer_drains_while_ross_refresh_is_blocked(monkeypatch):
    _reset_ross_refresh_state(monkeypatch, now=None)
    now = time.monotonic()
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_symbols", ("CACHED",), raising=False
    )
    monkeypatch.setattr(
        trade_bridge,
        "_ross_universe_cache_success_at",
        now - 301.0,
        raising=False,
    )
    monkeypatch.setattr(
        trade_bridge, "_ross_universe_cache_max_age_s", 300.0, raising=False
    )
    fetch_entered = threading.Event()
    release_fetch = threading.Event()
    writer_progressed = threading.Event()
    stop = threading.Event()
    loader_calls = 0
    drain_calls = 0
    sent_commands: list[str] = []

    def blocking_loader():
        nonlocal loader_calls
        loader_calls += 1
        fetch_entered.set()
        assert release_fetch.wait(timeout=2.0)
        return ("NEW",), 300.0

    def drain_batch(*, max_events, hot_symbols):
        del max_events, hot_symbols
        nonlocal drain_calls
        drain_calls += 1
        writer_progressed.set()
        if drain_calls >= 2:
            stop.set()
        return [], [], False

    governor = trade_bridge.WatchCapacityGovernor.start(
        cap=10,
        floor=1,
        hard_max=10,
        quiet_interval_seconds=60.0,
        now=now,
    )
    monkeypatch.setattr(trade_bridge, "_watch_capacity", governor)
    monkeypatch.setattr(
        trade_bridge,
        "_start_watch_capacity_connection",
        lambda **_kwargs: (governor, governor, False),
    )
    monkeypatch.setattr(
        trade_bridge,
        "_advance_watch_capacity",
        lambda **_kwargs: (governor, governor, False),
    )
    monkeypatch.setattr(trade_bridge, "_load_ross_universe_symbols", blocking_loader)
    monkeypatch.setattr(
        trade_bridge,
        "_live_symbols_read",
        lambda: trade_bridge.SourceRead.success(trade_bridge.TargetCause.ACTIVE, ()),
    )
    monkeypatch.setattr(
        trade_bridge,
        "_eligible_symbols_read",
        lambda _limit: trade_bridge.SourceRead.success(
            trade_bridge.TargetCause.ELIGIBLE, ()
        ),
    )
    monkeypatch.setattr(trade_bridge, "SUBSCRIBE_ON_ALERT", False)
    monkeypatch.setattr(trade_bridge, "STICKY_RESUBSCRIBE", False)
    monkeypatch.setattr(trade_bridge, "STALE_NBBO_RECONNECT_S", 0.0)
    monkeypatch.setattr(trade_bridge, "FLUSH_INTERVAL_S", 0.0)
    monkeypatch.setattr(trade_bridge, "REFRESH_S", 0.0)
    monkeypatch.setattr(trade_bridge, "_drain_pending_write_batch", drain_batch)
    monkeypatch.setattr(
        trade_bridge,
        "_send",
        lambda _socket, command: sent_commands.append(command),
    )

    old_watched = set(trade_bridge.watched)
    trade_bridge.watched.clear()
    trade_bridge.watched.add("CACHED")
    generation = 901
    trade_bridge._activate_connection_generation(generation)
    writer_thread, errors = _run_in_thread(
        lambda: trade_bridge.writer(set(), None, object(), stop, generation)
    )
    try:
        assert fetch_entered.wait(timeout=0.5)
        assert writer_progressed.wait(timeout=0.5), "tape drain waited on Ross refresh"
        writer_thread.join(timeout=1.0)
        assert not writer_thread.is_alive()
        assert errors == []
        assert drain_calls >= 2
        assert loader_calls == 1
        assert "CACHED" in trade_bridge.watched
        assert "rCACHED" not in sent_commands
    finally:
        stop.set()
        release_fetch.set()
        writer_thread.join(timeout=1.0)
        refresh_thread = trade_bridge._ross_universe_refresh_thread
        if refresh_thread is not None:
            refresh_thread.join(timeout=1.0)
        trade_bridge._retire_connection_generation(generation)
        trade_bridge.watched.clear()
        trade_bridge.watched.update(old_watched)


@pytest.mark.parametrize(
    ("bridge", "terminal_error"),
    (
        (trade_bridge, trade_bridge._ReaderQuiescenceError),
        (depth_bridge, depth_bridge._DepthReaderQuiescenceError),
    ),
)
def test_run_supervised_reconnect_wait_is_interruptible(
    monkeypatch,
    bridge,
    terminal_error,
):
    del terminal_error
    attempted = threading.Event()
    stop = threading.Event()
    connected = threading.Event()
    ready = threading.Event()

    monkeypatch.setattr(
        bridge,
        "_require_supervised_capture_posture",
        lambda: None,
    )
    verify_name = (
        "_verify_bridge_schema"
        if bridge is trade_bridge
        else "_verify_depth_schema"
    )
    monkeypatch.setattr(bridge, verify_name, lambda: None)

    def fail_connection(*_args, **_kwargs):
        attempted.set()
        raise ConnectionError("fixture provider unavailable")

    monkeypatch.setattr(bridge, "_run_connection", fail_connection)
    thread, errors = _run_in_thread(
        lambda: bridge.run_supervised(
            stop_event=stop,
            connected_event=connected,
            ready_event=ready,
            reconnect_wait_seconds=60.0,
        )
    )
    assert attempted.wait(timeout=1.0)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert time.monotonic() - started < 1.0
    assert errors == []
    assert not connected.is_set()
    assert not ready.is_set()


@pytest.mark.parametrize(
    ("bridge", "terminal_error"),
    (
        (trade_bridge, trade_bridge._ReaderQuiescenceError),
        (depth_bridge, depth_bridge._DepthReaderQuiescenceError),
    ),
)
def test_run_supervised_reports_terminal_reader_failure(
    monkeypatch,
    bridge,
    terminal_error,
):
    monkeypatch.setattr(
        bridge,
        "_require_supervised_capture_posture",
        lambda: None,
    )
    verify_name = (
        "_verify_bridge_schema"
        if bridge is trade_bridge
        else "_verify_depth_schema"
    )
    monkeypatch.setattr(bridge, verify_name, lambda: None)

    def terminal(*_args, **_kwargs):
        raise terminal_error("fixture reader did not quiesce")

    monkeypatch.setattr(bridge, "_run_connection", terminal)
    with pytest.raises(terminal_error, match="did not quiesce"):
        bridge.run_supervised(
            stop_event=threading.Event(),
            reconnect_wait_seconds=0.01,
        )


@pytest.mark.parametrize("bridge", (trade_bridge, depth_bridge))
def test_supervised_posture_never_uses_uncaptured_cli_escape(monkeypatch, bridge):
    monkeypatch.setattr(bridge, "_capture_handoff", None)
    monkeypatch.setattr(
        bridge.sys,
        "argv",
        ["bridge.py", bridge.UNCAPTURED_DIAGNOSTIC_FLAG],
    )
    with pytest.raises(RuntimeError, match="requires a bound capture handoff"):
        bridge._require_supervised_capture_posture()


@pytest.mark.parametrize("bridge", (trade_bridge, depth_bridge))
def test_connection_generation_observes_external_stop_and_clears_health(
    monkeypatch,
    bridge,
):
    class Socket:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def settimeout(self, _timeout) -> None:
            return None

        def sendall(self, _payload) -> None:
            return None

        def shutdown(self, _how) -> None:
            self.closed.set()

        def close(self) -> None:
            self.closed.set()

    connection = Socket()
    supervisor_stop = threading.Event()
    connected = threading.Event()
    ready = threading.Event()

    monkeypatch.setattr(
        bridge.socket,
        "create_connection",
        lambda _address, timeout: connection,
    )
    monkeypatch.setattr(
        bridge,
        "_record_capture_connection_boundary",
        lambda **_kwargs: None,
    )
    if bridge is trade_bridge:
        monkeypatch.setattr(
            bridge,
            "_wait_for_protocol_ack",
            lambda *_args, **_kwargs: True,
        )
        monkeypatch.setattr(
            bridge,
            "_wait_for_selected_fields_ack",
            lambda *_args, **_kwargs: True,
        )

        def reader(_socket, local_stop, _generation) -> None:
            local_stop.wait(timeout=2.0)

        def writer(_forced, _deadline, _socket, local_stop, _generation) -> None:
            local_stop.wait(timeout=2.0)

    else:
        def reader(_socket, local_stop, _generation) -> None:
            local_stop.wait(timeout=2.0)

        def writer(_forced, _deadline, local_stop) -> None:
            assert local_stop is not supervisor_stop
            connection.closed.wait(timeout=2.0)

    monkeypatch.setattr(bridge, "reader", reader)
    monkeypatch.setattr(bridge, "writer", writer)
    thread, errors = _run_in_thread(
        lambda: bridge._run_connection(
            set(),
            None,
            supervisor_stop_event=supervisor_stop,
            connected_event=connected,
            ready_event=ready,
        )
    )
    assert connected.wait(timeout=1.0)
    assert ready.wait(timeout=1.0)
    supervisor_stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert errors == []
    assert connection.closed.is_set()
    assert connected.is_set() is False
    assert ready.is_set() is False


def test_depth_connection_stop_interrupts_before_initial_source_refresh(
    monkeypatch,
) -> None:
    class Socket:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def settimeout(self, _timeout) -> None:
            return None

        def sendall(self, _payload) -> None:
            return None

        def shutdown(self, _how) -> None:
            self.closed.set()

        def close(self) -> None:
            self.closed.set()

    connection = Socket()
    supervisor_stop = threading.Event()
    connected = threading.Event()
    ready = threading.Event()
    source_entered = threading.Event()
    boundaries: list[bool] = []

    monkeypatch.setattr(depth_bridge, "SNAP_INTERVAL_S", 0.2)
    monkeypatch.setattr(depth_bridge, "SUBSCRIBE_ON_ALERT", False)
    monkeypatch.setattr(
        depth_bridge.socket,
        "create_connection",
        lambda _address, timeout: connection,
    )
    monkeypatch.setattr(
        depth_bridge,
        "_record_capture_connection_boundary",
        lambda *, active, **_kwargs: boundaries.append(active),
    )

    def forbidden_source_read():
        source_entered.set()
        raise AssertionError("initial source refresh ran after connection stop")

    monkeypatch.setattr(depth_bridge, "_live_symbols_read", forbidden_source_read)

    def reader(_socket, local_stop, _generation) -> None:
        local_stop.wait(timeout=2.0)

    monkeypatch.setattr(depth_bridge, "reader", reader)
    thread, errors = _run_in_thread(
        lambda: depth_bridge._run_connection(
            set(),
            None,
            supervisor_stop_event=supervisor_stop,
            connected_event=connected,
            ready_event=ready,
        )
    )
    assert connected.wait(timeout=1.0)
    assert ready.wait(timeout=1.0)
    time.sleep(0.02)
    started = time.monotonic()
    supervisor_stop.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert time.monotonic() - started < 1.0
    assert errors == []
    assert source_entered.is_set() is False
    assert connection.closed.is_set()
    assert boundaries == [True, False]
    assert connected.is_set() is False
    assert ready.is_set() is False


class _FakeSupervisedBridge:
    def __init__(self, lane: str, events: list[str]) -> None:
        self.lane = lane
        self.events = events
        self.release_failure = threading.Event()
        self.stopped = threading.Event()

    def run_supervised(
        self,
        *,
        stop_event,
        schema_ready_event,
        connected_event,
        ready_event,
        forced_symbols,
        reconnect_wait_seconds,
    ) -> None:
        assert reconnect_wait_seconds > 0
        assert isinstance(forced_symbols, tuple)
        self.events.append(f"{self.lane}_lane_started")
        schema_ready_event.set()
        connected_event.set()
        ready_event.set()
        while not stop_event.wait(0.005):
            if self.release_failure.is_set():
                raise RuntimeError(f"{self.lane} fixture failure")
        ready_event.clear()
        connected_event.clear()
        self.events.append(f"{self.lane}_lane_stopped")
        self.stopped.set()


def test_supervisor_uses_non_daemon_lanes_and_one_failure_stops_peer():
    events: list[str] = []
    trade = _FakeSupervisedBridge("trade", events)
    depth = _FakeSupervisedBridge("depth", events)
    supervisor = host_module.IqfeedProviderLoopSupervisor(
        trade_bridge=trade,
        depth_bridge=depth,
    )

    started = supervisor.start(
        readiness_timeout_seconds=1.0,
        join_timeout_seconds=1.0,
        reconnect_wait_seconds=0.01,
    )
    assert started["state"] == "running"
    assert started["all_ready"] is True
    assert started["provider_sockets_started"] is True
    assert all(
        lane["thread_daemon"] is False
        for lane in started["lanes"].values()
    )

    trade.release_failure.set()
    deadline = time.monotonic() + 1.0
    while supervisor.health()["state"] != "failed" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert supervisor.health()["state"] == "failed"
    assert depth.stopped.wait(timeout=1.0)

    closed = supervisor.close(join_timeout_seconds=1.0)
    assert closed["state"] == "failed"
    assert closed["stop_requested"] is True
    assert not any(lane["thread_alive"] for lane in closed["lanes"].values())
    assert "trade" in closed["failures"]


def test_supervisor_readiness_timeout_stops_and_joins_both_lanes():
    class NeverReady(_FakeSupervisedBridge):
        def run_supervised(self, **kwargs) -> None:
            kwargs["connected_event"].set()
            self.events.append(f"{self.lane}_lane_started")
            kwargs["stop_event"].wait()
            kwargs["connected_event"].clear()
            self.stopped.set()

    events: list[str] = []
    trade = _FakeSupervisedBridge("trade", events)
    depth = NeverReady("depth", events)
    supervisor = host_module.IqfeedProviderLoopSupervisor(
        trade_bridge=trade,
        depth_bridge=depth,
    )

    with pytest.raises(CaptureContractError, match="startup failed closed"):
        supervisor.start(
            readiness_timeout_seconds=0.05,
            join_timeout_seconds=1.0,
            reconnect_wait_seconds=0.01,
        )

    health = supervisor.health()
    assert health["state"] == "failed"
    assert "readiness" in health["failures"]
    assert not any(lane["thread_alive"] for lane in health["lanes"].values())


def test_supervisor_refuses_unbind_after_nonquiescent_reader_failure():
    class ReaderStillAlive(RuntimeError):
        provider_reader_may_be_alive = True

    class UnsafeBridge(_FakeSupervisedBridge):
        def run_supervised(self, **kwargs) -> None:
            kwargs["connected_event"].set()
            raise ReaderStillAlive("fixture reader survived socket close")

    events: list[str] = []
    trade = UnsafeBridge("trade", events)
    depth = _FakeSupervisedBridge("depth", events)
    supervisor = host_module.IqfeedProviderLoopSupervisor(
        trade_bridge=trade,
        depth_bridge=depth,
    )

    with pytest.raises(CaptureContractError, match="startup failed closed"):
        supervisor.start(
            readiness_timeout_seconds=1.0,
            join_timeout_seconds=1.0,
            reconnect_wait_seconds=0.01,
        )
    assert supervisor.health()["safe_to_unbind"] is False
    with pytest.raises(CaptureContractError, match="refusing unbind"):
        supervisor.close(join_timeout_seconds=1.0)


def test_host_binds_before_lanes_and_stops_joins_unbinds_then_drains(monkeypatch):
    events: list[str] = []
    trade = _FakeSupervisedBridge("trade", events)
    depth = _FakeSupervisedBridge("depth", events)

    def bind_handoff(_handoff) -> None:
        events.append("handoff_bound")

    def trade_unbind(_handoff) -> None:
        assert trade.stopped.is_set()
        assert depth.stopped.is_set()
        events.append("trade_unbound")

    def depth_unbind(_handoff) -> None:
        assert trade.stopped.is_set()
        assert depth.stopped.is_set()
        events.append("depth_unbound")

    trade.bind_capture_handoff = bind_handoff
    trade.unbind_capture_handoff = trade_unbind
    depth.bind_capture_handoff = bind_handoff
    depth.unbind_capture_handoff = depth_unbind

    class Service:
        @staticmethod
        def health():
            return {"pending_symbols": (), "running_symbols": ()}

    class Composition:
        service = Service()
        l1_handoff = object()
        l2_handoff = object()

        @staticmethod
        def quiesce_ingress_for_shutdown():
            assert "depth_unbound" in events
            assert "trade_unbound" in events
            events.append("composition_drained")
            return {"state": "ingress_quiescent"}

        @staticmethod
        def close():
            assert "composition_drained" in events
            return {"state": "closed"}

        @staticmethod
        def health():
            return {"state": "fixture"}

    class Receipt:
        receipt_sha256 = "a" * 64

        @staticmethod
        def to_dict():
            return {"schema_version": "fixture"}

    host = object.__new__(host_module.IqfeedCaptureHost)
    host.composition = Composition()
    host.trade_bridge = trade
    host.depth_bridge = depth
    host._state = host_module.IqfeedCaptureHostState.PREPARED
    host._receipt = None
    host._trade_bound = False
    host._depth_bound = False
    host._captured_paper_runner_symbols = set()
    host._captured_paper_admission_symbols = set()
    host._macro_feature_caches = {}
    host._provider_supervisor = None
    host._provider_join_timeout_seconds = 20.0
    host._shutdown_capture_aborts = ()
    host._lock = threading.RLock()

    def bind(self):
        events.append("host_bind")
        self.trade_bridge.bind_capture_handoff(self.composition.l1_handoff)
        self._trade_bound = True
        self.depth_bridge.bind_capture_handoff(self.composition.l2_handoff)
        self._depth_bound = True
        self._receipt = Receipt()
        self._state = host_module.IqfeedCaptureHostState.BOUND
        return self._receipt

    host.bind = MethodType(bind, host)
    started = host.start_provider_loops(
        readiness_timeout_seconds=1.0,
        join_timeout_seconds=1.0,
        reconnect_wait_seconds=0.01,
    )

    assert events[0] == "host_bind"
    assert events.index("host_bind") < events.index("trade_lane_started")
    assert events.index("host_bind") < events.index("depth_lane_started")
    assert started["provider_loop_supervisor"]["all_ready"] is True
    health = host.health()
    assert health["provider_sockets_started"] is True
    assert health["database_started"] is True
    assert health["broker_started"] is False
    assert health["paper_live_execution_enabled"] is False
    assert health["activation_authorized"] is False
    assert health["provider_loop_activation_requested"] is True
    assert health["provider_loop_cli_wired"] is False

    closed = host.close()
    assert closed["state"] == "closed"
    assert closed["provider_sockets_started"] is False
    assert events[-1] == "composition_drained"


def test_host_startup_failure_joins_before_unbind_and_drain():
    events: list[str] = []

    class FailingTrade(_FakeSupervisedBridge):
        def run_supervised(self, **kwargs) -> None:
            self.events.append("trade_lane_started")
            kwargs["connected_event"].set()
            try:
                raise RuntimeError("fixture terminal trade failure")
            finally:
                kwargs["connected_event"].clear()
                self.stopped.set()

    trade = FailingTrade("trade", events)
    depth = _FakeSupervisedBridge("depth", events)

    def bind_handoff(_handoff) -> None:
        return None

    def unbind_handoff(_handoff) -> None:
        assert trade.stopped.is_set()
        assert depth.stopped.is_set()
        events.append("handoff_unbound")

    for bridge in (trade, depth):
        bridge.bind_capture_handoff = bind_handoff
        bridge.unbind_capture_handoff = unbind_handoff

    class Service:
        @staticmethod
        def health():
            return {"pending_symbols": (), "running_symbols": ()}

    class Composition:
        service = Service()
        l1_handoff = object()
        l2_handoff = object()

        @staticmethod
        def close():
            assert events.count("handoff_unbound") == 2
            events.append("composition_drained")
            return {"state": "closed"}

    class Receipt:
        receipt_sha256 = "b" * 64

        @staticmethod
        def to_dict():
            return {"schema_version": "fixture"}

    host = object.__new__(host_module.IqfeedCaptureHost)
    host.composition = Composition()
    host.trade_bridge = trade
    host.depth_bridge = depth
    host._state = host_module.IqfeedCaptureHostState.PREPARED
    host._receipt = None
    host._trade_bound = False
    host._depth_bound = False
    host._captured_paper_runner_symbols = set()
    host._captured_paper_admission_symbols = set()
    host._macro_feature_caches = {}
    host._provider_supervisor = None
    host._provider_join_timeout_seconds = 20.0
    host._shutdown_capture_aborts = ()
    host._lock = threading.RLock()

    def bind(self):
        events.append("host_bind")
        self.trade_bridge.bind_capture_handoff(self.composition.l1_handoff)
        self._trade_bound = True
        self.depth_bridge.bind_capture_handoff(self.composition.l2_handoff)
        self._depth_bound = True
        self._receipt = Receipt()
        self._state = host_module.IqfeedCaptureHostState.BOUND
        return self._receipt

    host.bind = MethodType(bind, host)
    with pytest.raises(CaptureContractError, match="startup failed closed"):
        host.start_provider_loops(
            readiness_timeout_seconds=1.0,
            join_timeout_seconds=1.0,
            reconnect_wait_seconds=0.01,
        )

    assert host.state is host_module.IqfeedCaptureHostState.FAILED
    assert host._trade_bound is False
    assert host._depth_bound is False
    assert events[-1] == "composition_drained"


def test_capture_host_cli_remains_validate_only():
    with pytest.raises(SystemExit) as exited:
        host_module._parser().parse_args([])
    assert exited.value.code == 2
    assert "start_provider_loops" not in host_module._parser().format_help()
