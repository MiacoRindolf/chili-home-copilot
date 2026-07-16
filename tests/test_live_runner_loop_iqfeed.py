from __future__ import annotations


def test_iqfeed_poll_is_recent_window_bounded() -> None:
    import inspect

    from app.services.trading.momentum_neural import live_runner_loop

    src = inspect.getsource(live_runner_loop.LiveRunnerLoop._tick_from_iqfeed_tape)

    assert "observed_at >= (now() - make_interval(secs => :recent_s))" in src
    assert "GROUP BY symbol" in src
    assert "SELECT DISTINCT ON (symbol)" not in src


def test_iqfeed_notify_admission_refreshes_viability(monkeypatch) -> None:
    from app.services.trading.momentum_neural import live_runner_loop

    calls: list[dict] = []

    class _Loop(live_runner_loop.LiveRunnerLoop):
        def __init__(self) -> None:
            pass

    def admit(db, **kwargs):
        calls.append(kwargs)
        return {"admitted": False, "skipped": "no_fresh_live_eligible_candidate"}

    class _DB:
        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(live_runner_loop, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.ross_event_admission.admit_ross_event",
        admit,
    )

    out = _Loop()._admit_iqfeed_symbol("LHAI", {"source": "iqfeed_l1"})

    assert out["skipped"] == "no_fresh_live_eligible_candidate"
    assert calls[0]["refresh_viability"] is True


def test_iqfeed_notify_submits_newly_admitted_session_without_sync_tick(monkeypatch) -> None:
    from app.services.trading.momentum_neural import live_runner_loop

    submitted: list[tuple[int, str]] = []

    class _Tracker:
        def __init__(self) -> None:
            self.refreshes = 0

        def get_sessions_for_symbol(self, symbol):
            return [] if self.refreshes == 0 else [{"session_id": 991, "symbol": symbol}]

        def refresh(self) -> None:
            self.refreshes += 1

    class _Loop(live_runner_loop.LiveRunnerLoop):
        def __init__(self) -> None:
            self._tracker = _Tracker()
            self._last_iqfeed_observed_at = {}

        def _maybe_refresh(self) -> None:
            self._tracker.refresh()

        def _submit_session(self, session_id: int, *, cause: str) -> None:
            submitted.append((session_id, cause))

    monkeypatch.setattr(
        _Loop,
        "_admit_iqfeed_symbol",
        lambda self, symbol, payload: {"admitted": True, "session_id": 991, "ticked": 0},
    )

    _Loop()._handle_iqfeed_notify_payload(
        '{"symbol":"DXTS","observed_at":"2026-07-01T11:05:00Z","source":"iqfeed_l1"}'
    )

    assert submitted == [(991, "iqfeed_notify")]


def test_start_warms_active_execution_adapter_before_threads(monkeypatch) -> None:
    from app.services.trading.momentum_neural import live_runner_loop

    calls: list[str] = []

    class _Adapter:
        def is_enabled(self) -> bool:
            calls.append("is_enabled")
            return True

    class _Tracker:
        def refresh(self) -> None:
            calls.append("refresh")

        def get_all_execution_families(self):
            return {"robinhood_agentic_mcp"}

        def get_all_session_ids(self):
            return []

    class _Loop(live_runner_loop.LiveRunnerLoop):
        def __init__(self) -> None:
            self._tracker = _Tracker()
            self._running = False
            self._thread = None
            self._notify_thread = None
            self._subscribed_symbols = set()
            self._last_refresh = 0.0

        def _subscribe_active_symbols(self) -> None:
            calls.append("subscribe")

        def _start_iqfeed_notify_listener(self) -> None:
            calls.append("notify")

        @property
        def _min_tick_interval(self) -> float:
            return 0.25

    class _Thread:
        def __init__(self, *, target, daemon, name) -> None:
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self) -> None:
            calls.append("thread_start")

    monkeypatch.setattr(
        live_runner_loop,
        "threading",
        type("T", (), {"Thread": _Thread, "Lock": lambda: None}),
    )
    monkeypatch.setattr(
        "app.services.trading.execution_family_registry.resolve_live_spot_adapter_factory",
        lambda execution_family: (lambda: _Adapter()),
    )

    _Loop().start()

    assert calls[:2] == ["refresh", "is_enabled"]
    assert "thread_start" in calls


def test_tick_session_drains_entry_handoff_until_order_submitted(monkeypatch) -> None:
    from app.services.trading.momentum_neural import live_runner_loop

    states = ["live_entry_candidate", "live_pending_entry", "live_pending_entry"]
    calls: list[int] = []

    class _Session:
        state = "watching_live"
        risk_snapshot_json = {"momentum_live_execution": {}}

    sess = _Session()

    class _DB:
        def get(self, model, session_id):
            return sess

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    def tick_live_session(db, session_id):
        calls.append(session_id)
        sess.state = states[min(len(calls) - 1, len(states) - 1)]
        if len(calls) >= 3:
            sess.risk_snapshot_json = {
                "momentum_live_execution": {
                    "entry_submitted": True,
                    "entry_order_id": "ord-1",
                }
            }
        return {"ok": True, "state": sess.state}

    class _Loop(live_runner_loop.LiveRunnerLoop):
        def __init__(self) -> None:
            self._lock = type("_L", (), {
                "__enter__": lambda self: None,
                "__exit__": lambda self, *args: None,
            })()
            self._inflight = set()

    monkeypatch.setattr(live_runner_loop, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.tick_live_session",
        tick_live_session,
    )

    _Loop()._tick_session(10390, "iqfeed_notify")

    assert calls == [10390, 10390, 10390]
