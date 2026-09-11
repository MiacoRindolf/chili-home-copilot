"""Resource ownership tests without DB fixtures, app startup or broker adapters."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.held_reader_budget import resolve_held_reader_budget


ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


snapshot = _load("_held_reader_resource_under_test", "app/services/trading/momentum_neural/held_market_snapshot.py")
reads = _load("_held_reader_optional_under_test", "app/services/trading/momentum_neural/optional_db_read.py")


def settings(**changes):
    values = dict(chili_scheduler_role="momentum_exec_only", chili_alpaca_paper=True,
                  chili_momentum_legacy_alpaca_dispatch_enabled=True,
                  chili_momentum_live_runner_enabled=True,
                  chili_momentum_live_runner_loop_enabled=True,
                  chili_momentum_live_runner_scheduler_enabled=False,
                  chili_momentum_live_runner_batch_workers=0,
                  chili_momentum_risk_max_concurrent_live_sessions=5)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("retained,overflow,event,expected", [
    (1, 1, True, 0), (1, 1, False, 0), (2, 1, True, 0),
    (2, 2, True, 1), (1, 3, True, 1), (2, 3, False, 3),
    (6, 74, True, 3), (8, 72, False, 5),
])
def test_partition_preserves_resident_fence_and_two_caller_slots(retained, overflow, event, expected):
    result = resolve_held_reader_budget(settings(chili_momentum_live_runner_loop_enabled=event,
                                                chili_momentum_live_runner_scheduler_enabled=not event),
                                        pool_size=retained, max_overflow=overflow)
    assert result.reader_capacity == expected
    assert result.retained == retained
    assert result.caller_overflow + result.reader_capacity == overflow
    assert result.retained + result.caller_overflow >= 2 + int(event) or expected == 0
    assert result.receipt()["budget_scope"] == "partitioned_sqlalchemy_peak_excludes_direct_libpq_sockets"


@pytest.mark.parametrize("retained,overflow", [(0, 8), (2, -1), (True, 5), (2, None), (2, float("inf")), (2, "5")])
def test_unbounded_invalid_budget_never_reserved_or_silently_repaired(retained, overflow):
    result = resolve_held_reader_budget(settings(), pool_size=retained, max_overflow=overflow)
    assert result.reader_capacity == 0
    assert result.caller_overflow is overflow
    assert result.reason == "reader_pool_budget_invalid_or_unbounded"
    assert result.receipt()["original_peak"] is None


@pytest.mark.parametrize("kwargs", [{"pytest_process": True}, {"mp_child": True}])
def test_tests_and_multiprocess_children_have_no_default_physical_cache(kwargs):
    result = resolve_held_reader_budget(settings(), pool_size=25, max_overflow=55, **kwargs)
    assert result.reader_capacity == 0 and result.caller_overflow == 55


@pytest.mark.parametrize("change", [
    {"chili_scheduler_role": "none"}, {"chili_scheduler_role": "rnd_only"},
    {"chili_alpaca_paper": False}, {"chili_momentum_legacy_alpaca_dispatch_enabled": False},
    {"chili_momentum_live_runner_enabled": False},
])
def test_nonordinary_processes_do_not_lose_caller_overflow(change):
    result = resolve_held_reader_budget(settings(**change), pool_size=8, max_overflow=72)
    assert result.reader_capacity == 0 and result.caller_overflow == 72


def test_overlapping_driver_width_is_sizing_input_and_invalid_width_is_named():
    result = resolve_held_reader_budget(settings(chili_momentum_live_runner_scheduler_enabled=True,
                                                chili_momentum_live_runner_batch_workers=8),
                                        pool_size=4, max_overflow=10)
    assert result.driver_capacity == result.reader_capacity == 8
    assert result.resident_caller_slots == 1
    invalid = resolve_held_reader_budget(settings(chili_momentum_live_runner_scheduler_enabled=True,
                                                 chili_momentum_live_runner_batch_workers=float("nan")),
                                         pool_size=4, max_overflow=10)
    assert invalid.reader_capacity == 0 and invalid.reason == "reader_driver_capacity_invalid"


class Transaction:
    def __init__(self, connection, nested=False):
        self.connection, self.nested = connection, nested
        self.is_active = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.rollback()

    def rollback(self):
        if not self.is_active:
            return
        if not self.nested and self.connection.fail_rollback:
            raise RuntimeError("injected rollback failure")
        self.is_active = False
        if not self.nested:
            self.connection.transaction = None


class Result:
    def one(self):
        return ("repeatable read", "on")

    def fetchall(self):
        return [(42,)]


class Connection:
    def __init__(self):
        self.closed = self.invalidated = self.fail_rollback = False
        self.transaction = None
        self.dbapi = SimpleNamespace(closed=False)
        self.connection = SimpleNamespace(dbapi_connection=self.dbapi)
        self.engine = SimpleNamespace(url="same-test-database")
        self.calls = []

    def in_transaction(self):
        return self.transaction is not None

    def execution_options(self, **kwargs):
        return self

    def begin(self):
        assert not self.in_transaction()
        self.transaction = Transaction(self)
        return self.transaction

    def begin_nested(self):
        assert self.in_transaction()
        return Transaction(self, nested=True)

    def execute(self, statement, params=None):
        self.calls.append((threading.get_ident(), str(statement)))
        return Result()

    def invalidate(self):
        self.invalidated = self.dbapi.closed = True
        self.transaction = None

    def close(self):
        self.closed = self.dbapi.closed = True
        self.transaction = None


def cache(factory, capacity=1):
    return snapshot.ReaderCache(factory, capacity=capacity, budget={"reader_capacity": capacity},
                                health_timeout_ms=500, shutdown_wait_s=.05)


def ready_lease(manager):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        lease, _ = manager.try_lease()
        if lease is not None:
            return lease
        time.sleep(.001)
    raise AssertionError("Fake maintenance did not publish a ready lease")


def test_warming_and_contention_fall_back_without_factory_call_on_tick_thread():
    entered, release = threading.Event(), threading.Event()
    creator_threads = []
    connection = Connection()

    def factory():
        creator_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return connection

    manager = cache(factory)
    manager.start()
    assert entered.wait(2)
    try:
        assert manager.try_lease()[0] is None
        with manager.lock:
            assert manager.try_lease() == (None, "reader_cache_contended")
        assert len(creator_threads) == 1 and creator_threads[0] != threading.get_ident()
        assert connection.calls == []
        release.set()
        lease = ready_lease(manager)
        assert not connection.in_transaction()
        count = len(connection.calls)
        assert manager.try_lease()[0] is None  # leased resource still consumes C
        assert len(connection.calls) == count  # no lease-time ping/reset
        assert all(thread != threading.get_ident() for thread, _ in connection.calls)
        lease.release()
    finally:
        release.set()
        manager.stop()


def test_stop_during_warm_never_publishes_old_generation_or_overlaps_replacement():
    entered, release = threading.Event(), threading.Event()
    created = []

    def factory():
        entered.set()
        assert release.wait(2)
        created.append(Connection())
        return created[-1]

    manager = cache(factory)
    manager.start()
    assert entered.wait(2)
    assert manager.stop() is False  # bounded shutdown does not wait for blocked connect
    assert manager.start() is False
    assert manager.try_lease()[0] is None
    release.set()
    manager.thread.join(2)
    assert not manager.thread.is_alive()
    assert len(created) == 1 and created[0].dbapi.closed
    assert not manager.ready and not manager.slots


def test_fork_pid_guard_precedes_inherited_lock_or_connection_access(monkeypatch):
    manager = cache(lambda: (_ for _ in ()).throw(AssertionError("must not connect")))
    manager.lock = SimpleNamespace(acquire=lambda **k: (_ for _ in ()).throw(AssertionError("inherited lock")))
    monkeypatch.setattr(snapshot, "os", SimpleNamespace(getpid=lambda: manager.pid + 1))
    assert manager.try_lease() == (None, "reader_process_generation_mismatch")
    assert manager.stop() is False and manager.start() is False


def _exercise_epoch(lease, *, fail_cleanup=False, no_query=False):
    notes = {}
    # The wrapper records into the source evaluator's audit globals, as the real
    # live evaluator does; these tests do not import the trading package.
    globals()["_held_eval_audit"] = SimpleNamespace(note=lambda key, value: notes.__setitem__(key, value))
    db = SimpleNamespace(get_bind=lambda: lease.slot.connection.engine)
    sess = SimpleNamespace(id=7, execution_family="alpaca_spot")

    @snapshot.observe_exit_epoch
    def evaluate(db, sess, le, **kwargs):
        if not no_query:
            port = snapshot.query_port(db)
            assert reads.optional_fetchall(port, text("SELECT 42")) == [(42,)]
        lease.slot.connection.fail_rollback = fail_cleanup
        return {"action": "tick_deadman", "quantity": 10}

    admission = {"lease": lease, "reason": None, "ordinary_session_id": 7, "budget": {"reader_capacity": 1}}
    token = snapshot._ADMISSION.set(admission)
    try:
        result = evaluate(db, sess, {}, as_of="existing-fixed-decision-clock")
        # This is the external transport seam: normal and handled cleanup fault
        # must finish/invalidate the reader before returning the decided action.
        assert not lease.slot.connection.in_transaction()
        assert result == {"action": "tick_deadman", "quantity": 10}
        return notes["market_snapshot"]
    finally:
        snapshot._ADMISSION.reset(token)


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_exclusive_epoch_ends_before_protective_result_and_cleanup_failure_keeps_result(fail_cleanup):
    manager = cache(Connection)
    manager.start()
    lease = ready_lease(manager)
    try:
        receipt = _exercise_epoch(lease, fail_cleanup=fail_cleanup)
        assert receipt["common_snapshot"] is True and receipt["successful_role_reads"] == 1
        assert receipt["cleanup"] == ("connection_invalidated_physical_closed" if fail_cleanup else "transaction_ended")
        assert receipt["cleanup_elapsed_ms"] >= 0
        if not fail_cleanup:
            second = _exercise_epoch(lease)
            assert second["epoch_id"] != receipt["epoch_id"]
            assert second["reader_generation"] == receipt["reader_generation"]
        wrong_thread = []
        worker = threading.Thread(target=lambda: wrong_thread.append(pytest.raises(snapshot.HeldReaderUnavailable, lease.assert_owner)))
        worker.start()
        worker.join(2)
        assert len(wrong_thread) == 1
    finally:
        lease.release()
        manager.stop()


def test_unused_admission_has_no_snapshot_and_no_strategy_query():
    manager = cache(Connection)
    manager.start()
    lease = ready_lease(manager)
    try:
        count = len(lease.slot.connection.calls)
        receipt = _exercise_epoch(lease, no_query=True)
        assert receipt["mode"] == "not_started" and receipt["common_snapshot"] is False
        assert receipt["reason"] == "reader_no_role_query_reached"
        assert receipt["epoch_id"] is None and receipt["cleanup"] == "not_started"
        assert len(lease.slot.connection.calls) == count
    finally:
        lease.release()
        manager.stop()


@pytest.mark.parametrize("invalidate_fails,close_fails", [(False, False), (True, False), (False, True), (True, True)])
def test_actual_factory_configuration_failure_keeps_physical_ownership(invalidate_fails, close_fails):
    import ast
    from pathlib import Path
    # Use the production factory, with only its engine/socket replaced. This
    # catches configuration accidentally moving back before slot ownership.
    path = Path(__file__).parents[1] / "app/db.py"
    factory = next(n for n in ast.parse(path.read_text(encoding="utf-8")).body
                   if isinstance(n, ast.FunctionDef) and n.name == "make_held_reader_connection")
    resources, finished = [], threading.Event()

    class BrokenConfiguration(Connection):
        def __init__(self):
            super().__init__()
            self.invalidate_calls = self.close_calls = 0

        def execution_options(self, **kwargs):
            raise RuntimeError("post-connect RR configuration failed")

        def invalidate(self):
            self.invalidate_calls += 1
            if invalidate_fails:
                raise RuntimeError("physical invalidate failed")
            super().invalidate()

        def close(self):
            self.close_calls += 1
            try:
                if close_fails:
                    raise RuntimeError("physical close failed")
                super().close()
            finally:
                finished.set()

    def connect():
        resources.append(BrokenConfiguration())
        return resources[-1]

    engine = SimpleNamespace(connect=connect, dialect=SimpleNamespace(driver="psycopg2"))
    namespace = {"os": os, "held_reader_budget": SimpleNamespace(reader_capacity=1),
                 "_pool_timeout": 1, "_held_reader_factory_engine": engine,
                 "_held_reader_factory_pid": os.getpid()}
    exec(compile(ast.Module(body=[factory], type_ignores=[]), str(path), "exec"), namespace)
    manager = cache(namespace["make_held_reader_connection"])
    manager.start()
    try:
        assert finished.wait(2)
        # The maintenance worker completes retirement before handling another
        # signal. With both cleanup failures the original slot stays charged.
        manager.stop()
        assert resources and all(c.invalidate_calls == c.close_calls == 1 for c in resources)
        if invalidate_fails and close_fails:
            assert len(resources) == len(manager.slots) == 1
            assert next(iter(manager.slots.values())).state == "quarantined"
            assert not resources[0].dbapi.closed
            assert manager.start() is False
            assert manager.try_lease()[0] is None
        else:
            assert all(c.dbapi.closed for c in resources)
            assert not manager.slots
    finally:
        manager.stop()
        # Only fake resources are retired here after proving failed cleanup.
        for slot in list(manager.slots.values()):
            Connection.invalidate(slot.connection)
            manager._retire(slot)
