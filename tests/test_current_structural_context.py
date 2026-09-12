from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from tests.test_structural_context_journal import durable, wave
from tests.test_ordinary_structural_context import source, packet, publish
from app.services.trading.momentum_neural import current_structural_context as current
from app.services.trading.momentum_neural import structural_context_journal as journal


def observe(engine, stream, **kwargs):
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        return current.read_observation(c, stream_id=stream, **kwargs)


def test_current_read_binds_exact_latest_wave_and_never_acknowledges_events(durable):
    engine, owner, writer = durable
    snapshot = wave(engine, owner)
    stream = writer.anchor.stream_id
    before = observe(engine, stream)
    receipt = before.receipt(['A', 'MISSING'])
    assert before.publication.snapshot == snapshot
    assert receipt['status'] == 'observed_prefix'
    assert receipt['symbols']['A']['flow_references']
    assert receipt['symbols']['MISSING']['status'] == 'not_enrolled'
    assert not receipt['order_authority'] and not receipt['event_history_replayed']
    assert not receipt['event_offset_advanced']
    with engine.connect() as c:
        assert journal.consumer_cursor(c, stream_id=stream, consumer='selection') == writer.anchor
    with pytest.raises(FrozenInstanceError):
        before.status = 'ready'


def test_new_source_release_is_gap_until_owner_commits_it(durable):
    engine, owner, writer = durable
    wave(engine, owner)
    before = observe(engine, writer.anchor.stream_id)
    publish(engine, packet(engine, [('A', 9)], start=10))
    behind = observe(engine, writer.anchor.stream_id)
    assert behind.status == 'source_gap'
    assert behind.receipt(['A'])['symbols']['A']['status'] == 'source_gap'
    assert behind.publication == before.publication
    owner.advance_one(engine)
    after = observe(engine, writer.anchor.stream_id)
    assert after.status == 'observed_prefix'
    assert after.publication.cursor.revision > before.publication.cursor.revision
    assert before.receipt(['A'])['status'] == 'observed_prefix'  # immutable observation of its own read


def test_closed_writer_does_not_present_retained_geometry_as_current_owner(durable):
    engine, owner, writer = durable
    wave(engine, owner)
    writer.close()
    result = observe(engine, writer.anchor.stream_id)
    assert result.status == 'writer_absent' and not result.writer_present
    assert result.publication is not None
    assert result.receipt(['A'])['symbols']['A']['status'] == 'writer_absent'


def test_current_reader_refuses_bad_or_oversized_head_instead_of_returning_older_view(durable):
    engine, owner, writer = durable
    wave(engine, owner)
    with pytest.raises(ValueError, match='byte_capacity'):
        observe(engine, writer.anchor.stream_id, max_payload_bytes=1)
    with engine.begin() as c:
        c.execute(sa.text('''UPDATE momentum_structural_context_publications SET payload=payload||' '
            WHERE stream_id=:s AND revision=(SELECT max(revision) FROM momentum_structural_context_publications WHERE stream_id=:s)'''),
            {'s': writer.anchor.stream_id})
    with pytest.raises(ValueError, match='digest_mismatch'):
        observe(engine, writer.anchor.stream_id)


def test_empty_stream_and_cold_symbol_remain_distinct(durable):
    engine, owner, writer = durable
    # Binding the publication sink publishes the empty cold owner once.
    before = observe(engine, writer.anchor.stream_id)
    assert before.status == 'cold'
    owner.update_demand('inventory', revision=1, symbols=['A'])
    value = observe(engine, writer.anchor.stream_id).receipt(['A', 'B'])
    assert value['symbols']['A']['status'] == 'cold'
    assert value['symbols']['B']['status'] == 'not_enrolled'


def test_selection_uses_separate_read_only_connection_and_preserves_caller_transaction(durable, monkeypatch):
    engine, owner, writer = durable
    wave(engine, owner)
    monkeypatch.setattr(current, 'ordinary_paper_context_stream', lambda _: writer.anchor.stream_id)
    database = SimpleNamespace(get_bind=lambda: engine)
    receipt = current.observe_selection(database, symbols=['A'], expected_account_id='opaque-fixture')
    assert receipt['status'] == 'observed_prefix'
    with engine.connect() as c:
        assert c.execute(sa.text('SELECT 1')).scalar_one() == 1


def test_unavailable_observation_sanitizes_exception_and_never_reuses_prior_view():
    def fail():
        raise RuntimeError('secret credentials must not appear')
    db = SimpleNamespace(get_bind=fail)
    result = current.observe_selection(db, symbols=['A'],
        expected_account_id='aaaaaaaa-2222-4333-8444-555555555555')
    assert result['status'] == 'unavailable' and result['reason'] == 'RuntimeError'
    assert result['context_cursor'] is None
    assert 'secret' not in repr(result)


def test_explicit_decision_instant_never_reads_later_current_context():
    from datetime import datetime, timezone
    calls = []
    db = SimpleNamespace(get_bind=lambda: calls.append(True))
    result = current.observe_selection(db, symbols=['A'], expected_account_id=None,
        decision_at=datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert result['reason'] == 'explicit_decision_frontier_requires_bound_context'
    assert result['context_cursor'] is None and calls == []
