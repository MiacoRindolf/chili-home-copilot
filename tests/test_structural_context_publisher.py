"""Actual DB supervised source startup, warm continuation and fence release."""
from dataclasses import FrozenInstanceError, replace
from threading import Event
from uuid import uuid4

import pytest
import sqlalchemy as sa

from tests.test_ordinary_structural_context import source, packet, publish, counts
from tests.test_structural_context_recovery import CLOCK, CAP
from app.services.trading.momentum_neural import structural_context_journal as journal
from app.services.trading.momentum_neural import structural_context_recovery as recovery
from app.services.trading.momentum_neural.current_structural_context import read_observation
from app.services.trading.momentum_neural.structural_context_publisher import (
    PublisherBudget, StructuralContextPublisher,
)
from app.services.trading.momentum_neural.structural_tape_prefix import Limits


@pytest.fixture
def context_db(source):
    with source.begin() as c:
        journal.create_schema(c)
        recovery.create_schema(c)
    return source


def worker(engine, *, account=None, budget=None):
    return StructuralContextPublisher(engine, expected_account_id=account or str(uuid4()),
        budget=budget or PublisherBudget(Limits(100, 100, 100), 5, 100, CAP, CAP, 100),
        clock_ns=lambda: CLOCK)


def observed(w):
    with w.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        return read_observation(c, stream_id=w.stream_id)


def demand(revision, symbols, *, complete=True):
    return {'watch': dict(revision=revision, symbols=symbols, complete=complete)}


def seed(engine):
    publish(engine, packet(engine, [('SEED', 1)]))


def test_missing_schema_waits_without_creating_it(source):
    w = worker(source)
    assert w.start().state == 'waiting_schema'
    assert w.service is None
    with source.connect() as c:
        assert c.execute(sa.text("SELECT to_regclass('momentum_structural_context_heads')")).scalar_one() is None
    w.close()


def test_source_must_start_before_first_stream_and_wait_is_interruptible(context_db):
    w = worker(context_db)
    try:
        assert w.start().state == 'waiting_source'
        assert w.start().reason == 'print_publication_source_not_started'
        with context_db.connect() as c:
            assert c.execute(sa.text('SELECT count(*) FROM momentum_structural_context_heads')).scalar_one() == 0
        seed(context_db)
        status = w.start()
        assert status.state == 'running' and status.startup_mode == 'cold'
        assert observed(w).status == 'cold'
        assert w.start() is status
    finally:
        w.close()


@pytest.mark.parametrize('missing', ['momentum_structural_context_inputs',
                                    'momentum_structural_context_recovery_heads',
                                    'momentum_structural_context_publications'])
def test_partial_migration_never_leaves_an_orphan_generation(context_db, missing):
    seed(context_db)
    with context_db.begin() as c:
        c.execute(sa.text('DROP TABLE ' + missing))
    w = worker(context_db)
    try:
        assert w.start().state == 'waiting_schema'
        with context_db.begin() as c:
            assert c.execute(sa.text('SELECT count(*) FROM momentum_structural_context_heads')).scalar_one() == 0
            journal.create_schema(c)
            recovery.create_schema(c)
        assert w.start().startup_mode == 'cold'
    finally:
        w.close()


def test_actual_current_selection_reads_worker_output_and_restart_keeps_every_release(context_db):
    account = str(uuid4())
    seed(context_db)
    w = worker(context_db, account=account)
    w.start()
    try:
        w.step(demand(1, ['A', 'B']))
        publish(context_db, packet(context_db, [('A', 10), ('A', 12), ('A', 11)], start=2))
        first = w.step()
        assert counts(observed(w).publication.snapshot) == {'A': 3, 'B': 0}
        w.step(demand(2, None, complete=False))
        checkpoint = w.service.owner.demand_checkpoint()
        assert checkpoint[0].revision == 2 and not checkpoint[0].complete
        with pytest.raises(FrozenInstanceError):
            checkpoint[0].revision = 999
        before = w.service.owner.read('audit')
        cursor = w.service.writer.cursor
    finally:
        w.close()
    assert observed(w).status == 'writer_absent'
    publish(context_db, packet(context_db, [('A', 13), ('B', 20)], start=5))
    publish(context_db, packet(context_db, [('A', 9)], start=7))
    resumed = worker(context_db, account=account)
    try:
        assert resumed.start().startup_mode == 'restore'
        assert resumed.service.writer.cursor == cursor
        assert resumed.service.owner.read('audit') == before
        assert resumed.service.owner.demand_checkpoint() == checkpoint
        assert observed(resumed).status == 'source_gap'
        second = resumed.step(demand(3, ['A', 'B']))
        assert second.source_cursor.revision == first.source_cursor.revision + 1
        assert observed(resumed).status == 'source_gap'
        third = resumed.step()
        assert third.source_cursor.revision == second.source_cursor.revision + 1
        result = observed(resumed)
        assert result.status == 'observed_prefix'
        assert counts(result.publication.snapshot) == {'A': 5, 'B': 1}
        assert not result.publication.snapshot.order_authority
        previous_input = resumed.service._input_revision
        assert resumed.step() == third
        assert resumed.service._input_revision == previous_input  # Idle cannot bloat recovery.
    finally:
        resumed.close()


def test_competing_worker_cannot_replace_or_disconnect_current_writer(context_db):
    seed(context_db)
    account = str(uuid4())
    a, b = worker(context_db, account=account), worker(context_db, account=account)
    try:
        a.start()
        with pytest.raises(ValueError, match='context_writer_already_present'):
            b.start()
        assert b.status.state == 'failed' and observed(a).writer_present
        assert a.step(demand(1, ['A'])).state == 'running'
        with pytest.raises(ValueError, match='publisher_terminal'):
            b.start()
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize('fault', ['inputs_missing', 'configuration', 'code'])
def test_recovery_failures_never_silently_create_new_generation(context_db, monkeypatch, fault):
    seed(context_db)
    account = str(uuid4())
    a = worker(context_db, account=account)
    a.start()
    generation = a.service.writer.cursor.generation
    a.close()
    if fault == 'inputs_missing':
        with context_db.begin() as c:
            c.execute(sa.text('DELETE FROM momentum_structural_context_recovery_heads'))
    elif fault == 'code':
        monkeypatch.setattr(recovery, 'code_identity', lambda: '0'*64)
    budget = replace(a.budget, max_symbols=6) if fault == 'configuration' else a.budget
    b = worker(context_db, account=account, budget=budget)
    with pytest.raises(ValueError, match='recovery_|recovered_configuration'):
        b.start()
    assert b.status.state == 'failed'
    with context_db.connect() as c:
        assert c.execute(sa.text('SELECT generation FROM momentum_structural_context_heads')).scalar_one() == generation
    assert not observed(a).writer_present


def test_invalid_source_release_publishes_gap_and_releases_writer_without_skipping(context_db):
    seed(context_db)
    w = worker(context_db)
    w.start()
    try:
        w.step(demand(1, ['A']))
        before = w.status.source_cursor
        publish(context_db, packet(context_db, [('A', 10)], start=2,
                                   invalid={'provider_delay_minutes': 15}))
        status = w.step()
        assert status.state == 'failed'
        assert status.reason == 'ordinary_provider_delay_unresolved'
        assert status.source_cursor == before
        assert not observed(w).writer_present
        with pytest.raises(ValueError, match='publisher_not_running'):
            w.step()
    finally:
        w.close()


def test_supervisor_catches_up_without_waiting_between_releases_then_stops_cleanly(context_db):
    seed(context_db)
    w = worker(context_db)
    w.start()
    w.step(demand(1, ['A']))
    base = w.status.source_cursor.revision
    for i, price in enumerate((10, 12, 11), start=2):
        publish(context_db, packet(context_db, [('A', price)], start=i))
    statuses, checkpoints, waits = [], [], []
    class Stop:
        value = False
        def is_set(self): return self.value
        def wait(self, seconds):
            waits.append(seconds)
            self.value = True
    stop = Stop()
    def updates(checkpoint):
        checkpoints.append(checkpoint)
        return None
    w.run_supervised(stop_event=stop, demand_updates=updates, on_status=statuses.append,
                     idle_wait_seconds=.25)
    assert [s.source_cursor.revision for s in statuses] == [base+1, base+2, base+3]
    assert len(checkpoints) == 4 and all(c[0].revision == 1 for c in checkpoints)
    assert waits == [.25] and w.status.state == 'stopped'
    assert not observed(w).writer_present


def test_supervisor_stop_before_start_and_during_demand_read_does_not_publish(context_db):
    stop = Event()
    stop.set()
    w = worker(context_db)
    w.run_supervised(stop_event=stop, demand_updates=lambda _: pytest.fail('must not read'),
                     on_status=lambda _: pytest.fail('must not start'), idle_wait_seconds=.25)
    assert w.service is None and w.status.state == 'stopped'
    seed(context_db)
    stop.clear()
    w = worker(context_db)
    def updates(_):
        stop.set()
        return demand(1, ['A'])
    w.run_supervised(stop_event=stop, demand_updates=updates,
                     on_status=lambda _: None, idle_wait_seconds=.25)
    assert w.service.owner.demand_checkpoint() == ()
    assert not observed(w).writer_present


def test_demand_reader_failure_releases_fence_and_preserves_prior_held_demand(context_db):
    seed(context_db)
    w = worker(context_db)
    w.start()
    w.step({'held': dict(revision=1, symbols=['A'], complete=True)})
    def fail(_): raise RuntimeError('read unavailable')
    with pytest.raises(RuntimeError, match='read unavailable'):
        w.run_supervised(stop_event=Event(), demand_updates=fail, on_status=lambda _: None,
                         idle_wait_seconds=.25)
    assert w.status.state == 'failed' and not observed(w).writer_present
    assert w.service.owner.demand_checkpoint()[0].symbols == ('A',)


def test_waiting_source_does_not_invoke_demand_reader(context_db):
    stop, statuses = Event(), []
    w = worker(context_db)
    def status(value):
        statuses.append(value)
        stop.set()
    w.run_supervised(stop_event=stop, demand_updates=lambda _: pytest.fail('source not started'),
                     on_status=status, idle_wait_seconds=.25)
    assert [s.state for s in statuses] == ['waiting_source']
    assert w.status.state == 'stopped' and w.service is None
