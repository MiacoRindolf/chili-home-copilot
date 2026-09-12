"""Application-owned PAPER observation workers; no broker order authority.

Native membership is a durable latest checkpoint, not a broker event ledger.
An acknowledged single-slot handoff preserves each observation made by this host
without letting catalog HTTP work block tick publication. Provider notifications
are wakeups only: the contiguous source journal remains the authority.
"""
from dataclasses import asdict
import copy
import hashlib
import logging
import os
import select
import socket
import threading
import time

import psutil
import sqlalchemy as sa

from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .broker_native_demand_store import DurableBrokerNativeDemandService
from .native_iqfeed_mapping import NativeIQFeedMapper
from .native_tick_enrollment import from_mapping
from .paper_context_host_config import load_config, load_provider_catalog
from .paper_native_inventory_reader import PaperNativeInventoryReader
from .structural_context_publisher import StructuralContextPublisher

LOG = logging.getLogger(__name__)


class SourceWake:
    """One worker owns LISTEN; other threads may only signal the local socket."""
    def __init__(self, engine, stop):
        self.stop = stop
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.writer.setblocking(False)
        self.connection = None
        try:
            self.connection = engine.connect().execution_options(isolation_level='AUTOCOMMIT')
            self.connection.execute(sa.text('LISTEN iqfeed_print_publications'))
            self.driver = self.connection.connection.driver_connection
        except BaseException:
            self.close()
            raise

    def signal(self):
        try:
            self.writer.send(b'x')
        except BlockingIOError:
            pass  # A queued byte already wakes the worker.
        except OSError:
            if not self.stop.is_set():
                raise

    def wait(self, seconds):
        if self.stop.is_set():
            return
        ready, _, _ = select.select([self.reader, self.driver], [], [], seconds)
        if self.reader in ready:
            while True:
                try:
                    if not self.reader.recv(65536):
                        break
                except BlockingIOError:
                    break
        if self.driver in ready:
            self.driver.poll()
            self.driver.notifies.clear()

    def close(self):
        try:
            if self.connection is not None:
                # Invalidate, rather than return a LISTEN session to the pool.
                self.connection.invalidate()
                self.connection.close()
        finally:
            self.reader.close()
            self.writer.close()


class PaperContextHost:
    def __init__(self, engine, settings, config):
        if settings.chili_alpaca_paper is not True:
            raise ValueError('shared_tick_host_requires_paper')
        self.account = settings.chili_alpaca_expected_account_id
        self.account_sha = alpaca_paper_account_identity_sha256(self.account)
        self.engine, self.settings, self.config = engine, settings, config
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self._pending = None
        self._wake = None
        self._threads = []
        self._status = dict(state='not_started', native={'state': 'not_started'},
            publisher={'state': 'not_started'}, config_sha256=config.config_sha256,
            account_identity_sha256=self.account_sha, order_authority=False,
            full_broker_universe_tick_observed=False, subscriptions_dispatched=False,
            membership_delivery='durable_latest_checkpoint_acknowledged_handoff',
            broker_event_history_complete=False)

    def status(self):
        with self.condition:
            result = copy.deepcopy(self._status)
            result['workers_alive'] = [t.name for t in self._threads if t.is_alive()]
            if self.stop.is_set() and result['state'] != 'failed':
                result['state'] = 'stopping' if result['workers_alive'] else 'stopped'
            return result

    def _set(self, key, value):
        with self.condition:
            previous = self._status.get(key)
            if key == 'state' and previous == 'failed':
                return  # A sibling finishing its last step cannot erase failure.
            self._status[key] = value
        # Cursors remain available from status without flooding logs per tick.
        before = (previous.get('state'), previous.get('reason')) if isinstance(previous, dict) else previous
        after = (value.get('state'), value.get('reason')) if isinstance(value, dict) else value
        if before != after:
            LOG.info('[shared_tick_host] %s=%s', key, after)

    def _resource_check(self):
        if psutil.Process().memory_info().rss > self.config.process_rss_limit_bytes:
            raise ValueError('shared_tick_host_process_rss_capacity')

    def request_stop(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
            if self._wake is not None:
                self._wake.signal()

    def close(self):
        self.request_stop()
        deadline = time.monotonic() + self.config.shutdown_wait_seconds
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(max(0, deadline-time.monotonic()))
        return self.status()  # Never label still-running workers stopped.

    def _guard(self, name, work):
        try:
            work()
        except Exception as exc:
            if not self.stop.is_set():
                # Arbitrary exception text may contain HTTP/SQL credentials.
                reason = type(exc).__name__
                if isinstance(exc, ValueError) and str(exc).startswith(('shared_tick_host_', 'context_host_')):
                    reason = str(exc)
                self._set(name, dict(state='failed', reason=reason))
                self._set('state', 'failed')
                self.request_stop()

    def start(self):
        with self.condition:
            if self.stop.is_set() or self._threads:
                return
            self._status['state'] = 'starting'
            self._threads = [threading.Thread(target=self._guard, args=(name, work),
                name='chili-shared-tick-'+name, daemon=True)
                for name, work in [('native', self._native), ('publisher', self._publisher)]]
            try:
                for thread in self._threads:
                    thread.start()
            except Exception:
                self._status['state'] = 'failed'
                self._status['reason'] = 'shared_tick_host_thread_start_failed'
                self.request_stop()
                raise

    def _deliver(self, enrollment):
        with self.condition:
            while self._pending is not None and not self.stop.is_set():
                self.condition.wait()
            if self.stop.is_set():
                return
            self._pending = enrollment
            if self._wake is not None:
                self._wake.signal()
            while self._pending is enrollment and not self.stop.is_set():
                self.condition.wait()

    def _native(self):
        cfg = self.config
        # Stable account-scoped session lock owns broker observation traffic.
        key = int.from_bytes(hashlib.sha256(('paper_native_host:'+self.account_sha).encode()).digest()[:8], 'big', signed=True)
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as fence:
            if not fence.execute(sa.text('SELECT pg_try_advisory_lock(:key)'), {'key': key}).scalar_one():
                raise ValueError('shared_tick_host_native_owner_exists')
            reader = None
            try:
                self._resource_check()
                self._set('native', dict(state='loading_catalog'))
                mapper = NativeIQFeedMapper(load_provider_catalog(cfg.catalog, self.stop))
                self._resource_check()
                service = None
                while service is None and not self.stop.is_set():
                    try:
                        service = DurableBrokerNativeDemandService.open(self.engine,
                            expected_account_id=self.account, max_payload_bytes=cfg.native_checkpoint_bytes)
                    except sa.exc.DBAPIError as exc:
                        code = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
                        if code != '42P01':
                            raise
                        self._set('native', dict(state='waiting_schema', reason='native_checkpoint_schema_not_installed'))
                        self.stop.wait(cfg.native_refresh_wait_seconds)
                if self.stop.is_set():
                    return
                reader = PaperNativeInventoryReader(expected_account_id=self.account,
                    api_key=self.settings.chili_alpaca_api_key, api_secret=self.settings.chili_alpaca_api_secret,
                    paper=self.settings.chili_alpaca_paper, timeout_seconds=cfg.http_timeout_seconds,
                    max_response_bytes=cfg.http_response_bytes, stop_event=self.stop)
                snapshot = service.read()
                while not self.stop.is_set():
                    self._resource_check()
                    if snapshot is None:
                        inventory = reader.get_asset_inventory_probe()
                        exposure = reader.get_coverage_inventory_probe()
                        if self.stop.is_set():
                            break
                        snapshot = service.apply(inventory, exposure)
                    enrollment = from_mapping(mapper.bind(snapshot))
                    self._resource_check()
                    self._set('native', dict(state='observed', revision=snapshot.revision,
                        mapped_equities=len(enrollment.bindings), mapping_gaps=len(enrollment.gaps),
                        incomplete_reasons=enrollment.incomplete_reasons,
                        broker_stale_reasons=snapshot.stale_reasons,
                        inventory_read_complete=snapshot.inventory.error is None,
                        exposure_unavailable_reasons=snapshot.coverage.unavailable_reasons,
                        inventory_last_success_completed_ns=(snapshot.inventory.last_success.completed_ns
                            if snapshot.inventory.last_success else None),
                        exposure_probe_completed_ns=snapshot.coverage.probe.completed_ns,
                        catalog_observed_ns=enrollment.reference.catalog_observed_ns,
                        provider_catalog_effective_freshness_certified=False))
                    self._deliver(enrollment)
                    if self.stop.wait(cfg.native_refresh_wait_seconds):
                        break
                    # Verify that the session fence has not been lost before more HTTP.
                    fence.execute(sa.text('SELECT 1'))
                    snapshot = None
            finally:
                if reader is not None:
                    reader.close()
                # Do not put a session with an advisory lock back in the pool.
                fence.invalidate()
                with self.condition:
                    self._status['native'] = dict(self._status['native'], state='stopped')

    def _publisher(self):
        publisher = StructuralContextPublisher(self.engine, expected_account_id=self.account,
                                              budget=self.config.publisher)
        wake = SourceWake(self.engine, self.stop)
        with self.condition:
            self._wake = wake
        try:
            while not self.stop.is_set():
                self._resource_check()
                status = publisher.start()
                before = status.source_cursor
                if status.state == 'running':
                    with self.condition:
                        pending = self._pending
                    if self.stop.is_set():
                        break
                    status = publisher.step(pending)
                    if status.state == 'running' and pending is not None:
                        with self.condition:
                            if self._pending is pending:
                                self._status['native_context_reference'] = asdict(pending.reference)
                                self._pending = None
                                self.condition.notify_all()
                self._set('publisher', asdict(status))
                if status.state == 'failed':
                    raise ValueError('shared_tick_host_publisher_failed')
                self._set('state', 'observing' if status.state == 'running' else 'waiting_dependencies')
                if status.source_cursor == before:
                    wake.wait(self.config.publisher_idle_wait_seconds)
        finally:
            publisher.close()
            self._set('publisher', asdict(publisher.status))
            with self.condition:
                self._wake = None
                wake.close()


class HostLifecycle:
    """Shutdown before deferred startup wins; no worker resurrects afterwards."""
    def __init__(self):
        self.lock = threading.Lock()
        self.stopped = False
        self.host = None
        self.state = {'state': 'not_started', 'order_authority': False}

    def start(self, engine, settings):
        with self.lock:
            if self.stopped or self.host is not None:
                return
            if (os.environ.get('CHILI_PYTEST', '').lower() in ('1', 'true', 'yes') or
                    settings.chili_scheduler_role != 'momentum_exec_only' or not (
                        settings.chili_momentum_equity_execution_via_alpaca_paper or
                        settings.chili_momentum_crypto_execution_via_alpaca_paper)):
                self.state = {'state': 'not_applicable', 'order_authority': False}
                return
            try:
                config = load_config(settings.chili_momentum_shared_tick_host_config_path)
                self.host = PaperContextHost(engine, settings, config)
                self.host.start()
            except Exception as exc:
                reason = type(exc).__name__
                if isinstance(exc, ValueError) and str(exc).startswith(('shared_tick_host_', 'context_host_')):
                    reason = str(exc)
                self.state = {'state': 'waiting_configuration', 'reason': reason, 'order_authority': False}
                LOG.warning('[shared_tick_host] resource/catalog configuration unavailable (%s)', reason)

    def close(self):
        with self.lock:
            self.stopped = True
            host = self.host
        if host is not None:
            return host.close()
        self.state = {'state': 'stopped', 'order_authority': False}
        return self.state

    def status(self):
        with self.lock:
            return self.host.status() if self.host else dict(self.state)


lifecycle = HostLifecycle()
