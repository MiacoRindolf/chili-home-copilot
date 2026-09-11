"""Ordinary PAPER market observation on preadmitted, exclusive RR readers.

No connection/thread starts at import. The tick never creates or health-checks
a physical connection. A cache miss is an explicitly independent observation;
an epoch already begun can never reconnect or switch to caller READ COMMITTED.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import logging
import os
from queue import Empty, SimpleQueue
import threading
import time
from typing import Any, Callable
from uuid import uuid4


LOG = logging.getLogger(__name__)
_ADMISSION: ContextVar[Any] = ContextVar("ordinary_held_reader_admission", default=None)
_EPOCH: ContextVar[Any] = ContextVar("ordinary_held_reader_epoch", default=None)
_MANAGER = None
_MANAGER_LOCK = threading.Lock()
_MANAGER_PID = os.getpid()
_RUNTIME_FALLBACK = "reader_runtime_not_started"
_RUNTIME_BUDGET = None


def _utc():
    return datetime.now(timezone.utc).isoformat()


class HeldReaderUnavailable(RuntimeError):
    """No replay, reconnect or RC retry authority comes from this failure."""


@dataclass
class _Slot:
    identifier: str
    connection: Any = None
    dbapi: Any = None
    state: str = "warming"


class ReaderLease:
    def __init__(self, manager, slot):
        self.manager, self.slot = manager, slot
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.generation = manager.generation
        self.closed = False
        self.unusable = False

    def assert_owner(self):
        if self.closed or self.pid != os.getpid() or self.thread != threading.get_ident():
            raise HeldReaderUnavailable("reader_lease_owner_mismatch")

    def healthy(self):
        self.assert_owner()
        conn = self.slot.connection
        return not self.unusable and not conn.closed and not conn.invalidated

    def release(self):
        self.assert_owner()
        self.closed = True
        # SimpleQueue.put has no capacity wait. Maintenance alone consumes it;
        # the tick never waits for its manager's bookkeeping or health I/O.
        self.manager.returned.put(self)
        self.manager.wake.set()


class ReaderCache:
    """At most capacity physical resources, including unclosed retirement.

    SQLAlchemy Connection access is serialized by exclusive state ownership:
    maintenance -> ready -> one leased thread -> maintenance. Transaction
    objects never cross that handoff. An unexpected fork disables this object
    before touching its inherited locks or parent-owned DBAPI connections.
    """
    def __init__(self, factory: Callable[[], Any], *, capacity: int, budget: dict,
                 health_timeout_ms: int, shutdown_wait_s: float):
        self.factory, self.capacity, self.budget = factory, int(capacity), dict(budget)
        self.health_timeout_ms = int(health_timeout_ms)
        self.shutdown_wait_s = float(shutdown_wait_s)
        self.pid = os.getpid()
        self.generation = str(uuid4())
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.ready = deque()
        self.returned = SimpleQueue()
        self.slots = {}
        self.running = False
        self.thread = None
        self.last_reason = "reader_runtime_not_started"

    def start(self):
        if self.pid != os.getpid():
            return False
        with self.lock:
            if self.running:
                return True
            if self.slots or (self.thread is not None and self.thread.is_alive()):
                self.last_reason = "reader_prior_generation_retiring"
                return False
            if self.capacity <= 0:
                self.last_reason = self.budget.get("reason", "reader_budget_unavailable")
                return False
            self.generation = str(uuid4())
            self.running = True
            self.last_reason = "reader_warming"
            self.thread = threading.Thread(target=self._maintain, name="held-reader-maintenance", daemon=True)
            self.thread.start()
            self.wake.set()
            return True

    def try_lease(self):
        if self.pid != os.getpid():
            return None, "reader_process_generation_mismatch"
        if not self.lock.acquire(blocking=False):
            return None, "reader_cache_contended"
        try:
            if not self.running:
                return None, self.last_reason
            if self.ready:
                slot = self.ready.popleft()
                assert slot.state == "ready"
                slot.state = "leased"
                return ReaderLease(self, slot), None
            reason = self.last_reason if self.last_reason not in {"reader_ready", "reader_warming"} else "reader_cache_empty"
            self.wake.set()
            return None, reason
        finally:
            self.lock.release()

    def stop(self):
        if self.pid != os.getpid():
            return False
        with self.lock:
            self.running = False
            self.last_reason = "reader_generation_stopping"
            thread = self.thread
        self.wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.shutdown_wait_s)
        return thread is None or not thread.is_alive()

    def _retire(self, slot):
        """Only maintenance owns this slot; failed physical close keeps its charge."""
        conn = slot.connection
        if conn is None:
            closed = True
        else:
            try:
                if not conn.closed:
                    conn.invalidate()
            except Exception:
                LOG.warning("held reader invalidation failed; attempting close")
            try:
                conn.close()
            except Exception:
                LOG.warning("held reader physical close failed; capacity remains charged")
            closed = bool(getattr(slot.dbapi, "closed", False))
        with self.lock:
            if closed:
                self.slots.pop(slot.identifier, None)
            else:
                slot.state = "quarantined"
                self.last_reason = "reader_physical_retirement_unproven"

    def _prepare(self, slot):
        from sqlalchemy import text
        try:
            if slot.connection is None:
                slot.connection = self.factory()
                slot.dbapi = slot.connection.connection.dbapi_connection
                # Ownership has transferred to this charged slot. A failure
                # from here onward cannot lose the physical resource's charge.
                slot.connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True,
                )
            conn = slot.connection
            if conn.closed or conn.invalidated or conn.in_transaction():
                raise HeldReaderUnavailable("reader_not_transaction_free_for_health")
            # Administrative health I/O happens only here. Rollback restores its
            # timeout and ends its probe snapshot before READY is published.
            with conn.begin() as transaction:
                conn.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                             {"value": str(self.health_timeout_ms) + "ms"})
                observed = conn.execute(text(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only')"
                )).one()
                if tuple(observed) != ("repeatable read", "on"):
                    raise HeldReaderUnavailable("reader_isolation_not_preconfigured")
                transaction.rollback()
            if conn.in_transaction():
                raise HeldReaderUnavailable("reader_health_transaction_remains")
            with self.lock:
                if self.running:
                    slot.state = "ready"
                    self.ready.append(slot)
                    self.last_reason = "reader_ready"
                    return
        except Exception:
            with self.lock:
                self.last_reason = "reader_health_or_connect_failed"
        self._retire(slot)

    def _maintain(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            # Each signal permits at most capacity preparations. A failed
            # connect/health attempt never creates an unbounded retry loop.
            jobs = []
            while True:
                try:
                    lease = self.returned.get_nowait()
                except Empty:
                    break
                slot = lease.slot
                with self.lock:
                    assert slot.state == "leased"
                    slot.state = "maintenance"
                if lease.unusable:
                    self._retire(slot)
                else:
                    jobs.append(slot)
            with self.lock:
                running = self.running
                if not running:
                    jobs.extend(self.ready)
                    self.ready.clear()
                else:
                    for _ in range(max(0, self.capacity - len(self.slots))):
                        slot = _Slot(str(uuid4()))
                        self.slots[slot.identifier] = slot
                        jobs.append(slot)
            for slot in jobs:
                with self.lock:
                    still_running = self.running
                if still_running:
                    self._prepare(slot)
                else:
                    self._retire(slot)
            with self.lock:
                # Leased stragglers keep this generation alive and charged;
                # their eventual return wakes this same worker for retirement.
                if not self.running and not any(s.state == "leased" for s in self.slots.values()):
                    return


def start_reader_cache(*, shutdown_wait_s: float):
    """Called by an actual ordinary driver after process initialization."""
    global _MANAGER, _MANAGER_PID, _MANAGER_LOCK, _RUNTIME_FALLBACK, _RUNTIME_BUDGET
    if _MANAGER_PID != os.getpid():
        if _MANAGER is not None:
            # Keep inherited objects referenced; do not trigger a parent
            # connection's finalizer, dispose it, or touch its inherited mutex.
            return False
        # A module imported before a prefork launch has no reader resources.
        # Its actual child startup may create a fresh process-owned manager.
        _MANAGER_PID = os.getpid()
        _MANAGER_LOCK = threading.Lock()
    from app import db as database
    budget = database.held_reader_budget
    _RUNTIME_BUDGET = budget.receipt()
    if budget.reader_capacity <= 0:
        _RUNTIME_FALLBACK = budget.reason
        return False
    try:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = ReaderCache(
                    database.make_held_reader_connection, capacity=budget.reader_capacity,
                    budget=budget.receipt(),
                    health_timeout_ms=max(1, int(database._pool_timeout * 1000)),
                    shutdown_wait_s=shutdown_wait_s,
                )
            return _MANAGER.start()
    except Exception:
        _RUNTIME_FALLBACK = "reader_runtime_initialization_failed"
        LOG.warning("held reader runtime unavailable; independent observations remain active")
        return False


def stop_reader_cache():
    if _MANAGER_PID != os.getpid() or _MANAGER is None:
        return False
    return _MANAGER.stop()


@contextmanager
def preadmitted_scope(*, cache=None):
    if _ADMISSION.get() is not None:
        yield
        return
    manager = cache if cache is not None else _MANAGER
    if _MANAGER_PID != os.getpid() and cache is None:
        lease, reason = None, "reader_process_generation_mismatch"
    elif manager is None:
        lease, reason = None, _RUNTIME_FALLBACK
    else:
        lease, reason = manager.try_lease()
    admission = {"lease": lease, "reason": reason, "ordinary_session_id": None,
                 "budget": dict(manager.budget) if manager is not None else _RUNTIME_BUDGET}
    token = _ADMISSION.set(admission)
    try:
        yield
    finally:
        _ADMISSION.reset(token)
        if lease is not None:
            try:
                lease.release()
            except Exception:
                # Capacity stays charged if an unexpected ownership violation
                # prevents handback; it cannot turn a completed tick into rollback.
                LOG.warning("held reader lease return failed; resource remains charged")


def preadmitted_tick(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with preadmitted_scope():
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def ordinary_route(session_id):
    admission = _ADMISSION.get()
    if admission is None:
        yield
        return
    previous = admission["ordinary_session_id"]
    admission["ordinary_session_id"] = int(session_id)
    try:
        yield
    finally:
        admission["ordinary_session_id"] = previous


class _FetchedResult:
    def __init__(self, result, epoch):
        self.result, self.epoch = result, epoch

    def fetchall(self):
        rows = self.result.fetchall()
        self.epoch.successful_reads += 1
        return rows


class _ReadPort:
    def __init__(self, epoch):
        self.epoch = epoch

    def begin_nested(self):
        self.epoch.begin()
        return self.epoch.connection.begin_nested()

    def execute(self, statement, params=None):
        self.epoch.begin()
        result = self.epoch.connection.execute(statement, dict(params or {}))
        # Only SELECTs have materialized market rows; SET LOCAL has no fetch.
        if str(statement).lstrip().upper().startswith("SELECT"):
            return _FetchedResult(result, self.epoch)
        return result


class _Epoch:
    def __init__(self, session_id, as_of, *, execution_family=None):
        self.lease = None
        self.connection = None
        self.transaction = None
        self.identifier = None
        self.started_at = None
        self.as_of = as_of
        self.successful_reads = 0
        self.cleanup = "not_started"
        self.cleanup_elapsed_ms = None
        self.reason = "reader_not_preadmitted"
        self.budget = None
        admission = _ADMISSION.get()
        if admission is not None:
            self.budget = admission["budget"]
            if str(execution_family or "").strip().lower() != "alpaca_spot":
                self.reason = "reader_not_ordinary_paper_equity"
            elif admission["ordinary_session_id"] != session_id:
                self.reason = "reader_ordinary_route_not_activated"
            elif admission["lease"] is None:
                self.reason = admission["reason"] or "reader_not_preadmitted"
            else:
                lease = admission["lease"]
                try:
                    if lease.healthy():
                        self.lease, self.connection = lease, lease.slot.connection
                        self.reason = None
                    else:
                        self.reason = "reader_unhealthy_before_epoch"
                except Exception:
                    self.reason = "reader_unhealthy_before_epoch"

    def begin(self):
        if self.connection is None or not self.lease.healthy():
            raise HeldReaderUnavailable("reader_epoch_connection_unavailable")
        # Access only an already-checked-out physical connection. Testing
        # invalidated first prevents SQLAlchemy's automatic revalidation path.
        if self.connection.connection.dbapi_connection is not self.lease.slot.dbapi:
            self.lease.unusable = True
            raise HeldReaderUnavailable("reader_epoch_backend_changed")
        if self.transaction is None:
            self.identifier = str(uuid4())
            self.started_at = _utc()
            self.transaction = self.connection.begin()
            self.cleanup = "open"
        elif not self.transaction.is_active:
            raise HeldReaderUnavailable("reader_epoch_transaction_ended")

    def finish(self):
        if self.transaction is None:
            return
        started = time.monotonic()
        try:
            self.transaction.rollback()
            if self.connection.in_transaction():
                raise HeldReaderUnavailable("reader_cleanup_transaction_remains")
            self.cleanup = "transaction_ended"
        except Exception:
            self.lease.unusable = True
            try:
                self.connection.invalidate()
            except Exception:
                pass
            self.cleanup = ("connection_invalidated_physical_closed"
                            if bool(getattr(self.lease.slot.dbapi, "closed", False))
                            else "cleanup_unproven_resource_quarantined")
            LOG.warning("held reader cleanup failed (%s); decided protection retained", self.cleanup)
        finally:
            self.cleanup_elapsed_ms = (time.monotonic() - started) * 1000

    def receipt(self):
        no_query = self.connection is not None and self.identifier is None
        return {
            "mode": ("ordinary_readonly_repeatable_read" if self.identifier else
                     "not_started" if no_query else "independent_recorded_publication_reads"),
            "common_snapshot": self.identifier is not None and self.successful_reads > 0,
            "epoch_id": self.identifier,
            "reason": self.reason or ("reader_no_role_query_reached" if no_query else
                                      "reader_no_successful_role_read" if not self.successful_reads else None),
            "epoch_requested_at_utc": self.started_at,
            "epoch_clock_meaning": "client_before_begin_not_exact_snapshot_time",
            "snapshot_identity_kind": "application_id_for_one_guarded_connection_transaction",
            "database_snapshot_metadata": None,
            "successful_role_reads": self.successful_reads,
            "cleanup": self.cleanup, "cleanup_elapsed_ms": self.cleanup_elapsed_ms,
            "reader_generation": self.lease.generation if self.lease is not None else None,
            "reader_process_id": self.lease.pid if self.lease is not None else None,
            "resource_budget": self.budget,
            "membership_relationship": "distinct_role_predicates_in_one_visibility_epoch_only_when_common_snapshot_true",
            "historical_state_is_in_current_epoch": False,
            "captured_prefix": False, "provider_completeness": "unknown",
        }


def current_epoch_receipt():
    epoch = _EPOCH.get()
    return epoch.receipt() if epoch is not None else None


def query_port(caller_db):
    epoch = _EPOCH.get()
    if epoch is not None and epoch.connection is not None and epoch.identifier is None:
        try:
            same_database = caller_db.get_bind().url == epoch.connection.engine.url
        except Exception:
            same_database = False
        if not same_database:
            epoch.connection = None
            epoch.reason = "reader_caller_database_identity_unproven"
    return _ReadPort(epoch) if epoch is not None and epoch.connection is not None else caller_db


def observe_exit_epoch(function):
    @wraps(function)
    def wrapped(db, sess, le, **kwargs):
        epoch = _Epoch(getattr(sess, "id", None), kwargs.get("as_of"),
                       execution_family=getattr(sess, "execution_family", None))
        token = _EPOCH.set(epoch)
        try:
            return function(db, sess, le, **kwargs)
        finally:
            try:
                epoch.finish()
                audit = function.__globals__.get("_held_eval_audit")
                if audit is not None:
                    audit.note("market_snapshot", epoch.receipt())
            finally:
                _EPOCH.reset(token)
    return wrapped
