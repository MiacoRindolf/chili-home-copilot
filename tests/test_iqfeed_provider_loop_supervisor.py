from __future__ import annotations

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

        def writer(_forced, _deadline) -> None:
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
        def close():
            assert "depth_unbound" in events
            assert "trade_unbound" in events
            events.append("composition_drained")
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
    host._macro_feature_caches = {}
    host._provider_supervisor = None
    host._provider_join_timeout_seconds = 20.0
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
    host._macro_feature_caches = {}
    host._provider_supervisor = None
    host._provider_join_timeout_seconds = 20.0
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
