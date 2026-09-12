"""Supervised ordinary PAPER context publication, without broker order authority.

The deployment supplies provider-bound demand updates. Native broker inventory
is not an IQFeed symbol mapping. This worker never invents that binding, creates
schemas, starts bridges, or replaces a damaged stream with a new cold anchor.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

import sqlalchemy as sa

from scripts.iqfeed_print_publications import capture_frontier
from .current_structural_context import ordinary_paper_context_stream
from .structural_context_recovery import RecoverableStructuralContext
from .structural_tape_prefix import Limits
from .native_tick_enrollment import NativeTickEnrollment


@dataclass(frozen=True)
class PublisherBudget:
    """Explicit process capacity, never a price horizon or opportunity ranking."""
    limits: Limits
    max_symbols: int
    max_trade_rows: int
    max_payload_bytes: int
    max_input_bytes: int
    max_replay_inputs: int

    def __post_init__(self):
        if type(self.limits) is not Limits or any(type(v) is not int or v <= 0 for v in (
                self.max_symbols, self.max_trade_rows, self.max_payload_bytes,
                self.max_input_bytes, self.max_replay_inputs)):
            raise ValueError('context_publisher_capacity_invalid')

    def owner_configuration(self):
        return dict(limits=self.limits, max_symbols=self.max_symbols,
                    max_trade_rows=self.max_trade_rows)


@dataclass(frozen=True)
class PublisherStatus:
    state: str
    reason: str | None
    stream_id: str
    startup_mode: str | None = None
    context_cursor: object | None = None
    source_cursor: object | None = None
    enrolled_symbols: int = 0
    order_authority: bool = False


class StructuralContextPublisher:
    """One supervisor-owned worker; failed/closed instances cannot reacquire.

    Missing schema or a never-started source can be retried before ownership.
    Once a stream exists, only exact fenced recovery is allowed. Recovery gaps,
    code drift, changed owner capacity, and competing writers are terminal.
    """
    def __init__(self, engine, *, expected_account_id, budget: PublisherBudget,
                 clock_ns=time.time_ns):
        if type(budget) is not PublisherBudget:
            raise ValueError('context_publisher_budget_required')
        self.engine, self.budget, self.clock_ns = engine, budget, clock_ns
        self.stream_id = ordinary_paper_context_stream(expected_account_id)
        self.service = None
        self.status = PublisherStatus('not_started', None, self.stream_id)
        self._terminal = False

    def _preflight(self):
        with self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            c.execute(sa.text('SET TRANSACTION READ ONLY'))
            c.execute(sa.text("SET LOCAL statement_timeout='20s'"))
            # Check the whole migration chain before create() can commit a head.
            # Otherwise a missing input table could leave an unrecoverable empty
            # generation after its initial output transaction fails.
            for table in ('iqfeed_print_publication_head', 'iqfeed_print_publications',
                          'momentum_structural_context_heads',
                          'momentum_structural_context_publications',
                          'momentum_structural_context_consumers',
                          'momentum_structural_context_recovery_heads',
                          'momentum_structural_context_inputs'):
                c.execute(sa.text('SELECT 1 FROM ' + table + ' LIMIT 0'))
            exists = c.execute(sa.text('SELECT 1 FROM momentum_structural_context_heads '
                                      'WHERE stream_id=:s'), {'s': self.stream_id}).first()
            if exists:
                return 'restore'
            capture_frontier(c)  # Absence is not an empty, usable tick history.
            return 'cold'

    def start(self):
        if self._terminal:
            raise ValueError('context_publisher_terminal')
        if self.service is not None:
            return self.status
        try:
            mode = self._preflight()
        except ValueError as exc:
            if str(exc) != 'print_publication_source_not_started':
                raise
            self.status = PublisherStatus('waiting_source', str(exc), self.stream_id)
            return self.status
        except sa.exc.DBAPIError as exc:
            code = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
            if code != '42P01':
                raise
            self.status = PublisherStatus('waiting_schema', 'shared_context_schema_not_installed',
                                          self.stream_id)
            return self.status
        try:
            options = dict(stream_id=self.stream_id, max_payload_bytes=self.budget.max_payload_bytes,
                           max_input_bytes=self.budget.max_input_bytes)
            if mode == 'restore':
                self.service = RecoverableStructuralContext.restore(self.engine, **options,
                    max_replay_inputs=self.budget.max_replay_inputs, clock_ns=self.clock_ns)
                if dict(self.service.owner.configuration()) != self.budget.owner_configuration():
                    raise ValueError('context_publisher_recovered_configuration_changed')
            else:
                self.service = RecoverableStructuralContext.create(self.engine, **options,
                    **self.budget.owner_configuration(), clock_ns=self.clock_ns)
            self.status = PublisherStatus('running', None, self.stream_id, mode,
                self.service.writer.cursor, self.service.owner.read('audit').source,
                len(self.service.owner.read('audit').symbols))
            return self.status
        except BaseException:
            self.close(reason='context_publisher_start_failed')
            raise

    def step(self, updates=None):
        """Commit one complete source release; never skip to the newest release.

        Updates are provider-bound observation demands, with independent durable
        source revisions. None means no new observation. A failed observation
        must explicitly say complete=False; it cannot retire retained members.
        """
        if self._terminal or self.service is None:
            raise ValueError('context_publisher_not_running')
        try:
            if updates is not None:
                if type(updates) is NativeTickEnrollment:
                    self.service.update_native_enrollment(updates)
                else:
                    self.service.owner.update_demands(updates)
            snapshot = self.service.owner.advance_one(self.engine)
            cursor = self.service.writer.cursor
            if snapshot.status == 'unresolved':
                self.close(reason='context_source_unresolved')
                self.status = PublisherStatus('failed', snapshot.reason, self.stream_id,
                    self.status.startup_mode, cursor, snapshot.source, len(snapshot.symbols))
            else:
                self.status = PublisherStatus('running', None, self.stream_id,
                    self.status.startup_mode, cursor, snapshot.source, len(snapshot.symbols))
            return self.status
        except BaseException:
            self.close(reason='context_publisher_step_failed')
            raise

    def close(self, *, reason=None):
        if self._terminal and reason is None:
            return
        self._terminal = True
        try:
            if self.service is not None:
                self.service.close()
        finally:
            old = self.status
            self.status = PublisherStatus('failed' if reason else 'stopped', reason,
                self.stream_id, old.startup_mode, old.context_cursor, old.source_cursor,
                old.enrolled_symbols)

    def run_supervised(self, *, stop_event, demand_updates, on_status, idle_wait_seconds):
        """Catch up without a clock window; an idle wait only schedules DB I/O.

        demand_updates receives immutable restored checkpoints. It returns only
        newer source observations, or None. Read failures must either be encoded
        as incomplete demands or escape; no exception becomes a complete empty
        membership. The hosting process owns threads and stop-event lifetime.
        """
        if (not callable(demand_updates) or not callable(on_status)
                or not callable(getattr(stop_event, 'is_set', None))
                or not callable(getattr(stop_event, 'wait', None))
                or type(idle_wait_seconds) not in (int, float)
                or not math.isfinite(idle_wait_seconds) or idle_wait_seconds <= 0):
            raise ValueError('context_publisher_supervision_invalid')
        previous_status = None
        try:
            while not stop_event.is_set():
                status = self.start()
                before = status.source_cursor
                if self.service is not None:
                    updates = demand_updates(self.service.owner.demand_checkpoint())
                    if stop_event.is_set():
                        break
                    status = self.step(updates)
                if status != previous_status:
                    on_status(status)
                    previous_status = status
                if self._terminal:
                    break
                if status.source_cursor == before:
                    stop_event.wait(idle_wait_seconds)
        except BaseException:
            self.close(reason='context_publisher_supervision_failed')
            raise
        finally:
            if not self._terminal:
                self.close()
