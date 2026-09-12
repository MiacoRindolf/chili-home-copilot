"""Actual DB changed-symbol delivery and complete revision-bound projections."""
from dataclasses import replace
from uuid import UUID

import pytest
import sqlalchemy as sa

from tests.test_structural_context_journal import durable, read, wave
from tests.test_ordinary_structural_context import source, owner, packet, publish
from tests.test_structural_context_recovery import recoverable, restore, CLOCK
from tests.test_native_tick_enrollment import native_owner, enrollment
from app.services.trading.momentum_neural import structural_context_journal as j
from app.services.trading.momentum_neural import structural_context_delta as delta
from app.services.trading.momentum_neural.current_structural_context import read_observation


def current(engine, stream, names):
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        return read_observation(c, stream_id=stream, symbols=names)


def test_unprinted_symbols_never_reduce_or_hash_empty_frontiers(source, monkeypatch):
    o = owner(source)
    o.update_demand('watch', revision=1, symbols=['A', 'B'])
    publish(source, packet(source, [('A', 10), ('B', 10), ('B', 12), ('B', 11)], start=2))
    first = o.advance_one(source)
    b = o._prefixes['B']
    original_prepare = b.prepare_frontier
    old_receipt, old_hash = b.last_receipt, b.prefix_sha256
    def forbidden(*args, **kwargs):
        raise AssertionError('empty symbol was reduced')
    monkeypatch.setattr(b, 'prepare_frontier', forbidden)
    publish(source, packet(source, [('A', 11)], start=6))
    second = o.advance_one(source)
    assert b.last_receipt == old_receipt and b.prefix_sha256 == old_hash
    assert second.root_sha256 != first.root_sha256 and second.source.revision == first.source.revision+1
    assert not second.symbols[1].events and not second.symbols[1].wave_context.events
    publish(source, packet(source, [('OTHER', 20)], start=7))
    third = o.advance_one(source)
    assert third.symbols[1] is second.symbols[1]
    assert third.source.revision == second.source.revision+1
    monkeypatch.setattr(b, 'prepare_frontier', original_prepare)
    publish(source, packet(source, [('B', 13)], start=8))
    last = o.advance_one(source)
    assert last.symbols[1].print_count == 4
    assert b.last_receipt.previous_source_sequence == old_receipt.source_sequence
    assert b.last_receipt.source_sequence == last.source.revision


def test_empty_release_still_checks_global_observation_clock(source):
    o = owner(source)
    o.update_demand('watch', revision=1, symbols=['A'])
    publish(source, packet(source, [('OTHER', 10)], start=2))
    first = o.advance_one(source)
    o._clock_ns = lambda: 1
    publish(source, packet(source, [('OTHER', 11)], start=3))
    failed = o.advance_one(source)
    assert failed.status == 'unresolved' and failed.reason == 'context_observation_clock_regressed'
    assert failed.source == first.source and failed.symbols[0].print_count == 0
    assert failed.source_observed_ns == first.source_observed_ns


def test_empty_release_observation_clock_is_durable_global_evidence(recoverable):
    engine, service = recoverable
    service.owner.update_demand('watch', revision=1, symbols=['A'])
    publish(engine, packet(engine, [('OTHER', 10)], start=2))
    first = service.owner.advance_one(engine)
    assert first.source_observed_ns == CLOCK
    assert service.owner._prefixes['A'].last_receipt is None
    stream = service.writer.cursor.stream_id
    service.close()
    resumed = restore(engine, stream, clock_ns=lambda: CLOCK-1)
    try:
        assert resumed.owner.read('exit') == first
        publish(engine, packet(engine, [('OTHER', 11)], start=3))
        assert resumed.owner.advance_one(engine).reason == 'context_observation_clock_regressed'
    finally:
        resumed.close()


def test_source_query_sends_actual_selected_release_members_not_entire_inventory(source):
    from scripts.iqfeed_print_publications import read_publications, capture_frontier
    publish(source, packet(source, [('SEED', 1)]))
    with source.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        anchor = capture_frontier(c)
    publish(source, packet(source, [('A', 10), ('OTHER', 11)], start=2))
    sent = []
    def inspect(connection, cursor, statement, parameters, context, executemany):
        if 'symbol=ANY' in statement:
            sent.append(parameters['symbols'])
    sa.event.listen(source, 'before_cursor_execute', inspect)
    try:
        with source.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            result = read_publications(c, symbols=frozenset({'A', *(f'COLD{i}' for i in range(1000))}),
                                       after=anchor, max_publications=1, max_trade_rows=10)
        assert sent == [['A']]
        assert result.publications[0].source_symbols == ('A', 'OTHER')
        assert [row['symbol'] for row in result.publications[0].rows] == ['A']
    finally:
        sa.event.remove(source, 'before_cursor_execute', inspect)


def test_only_changed_symbol_values_are_published_and_persisted(durable):
    engine, o, w = durable
    o.update_demand('watch', revision=1, symbols=['A', 'B', 'C'])
    cursor = w.cursor
    publish(engine, packet(engine, [('A', 10)], start=2))
    result = o.advance_one(engine)
    with engine.connect() as c:
        raw = c.execute(sa.text('SELECT payload FROM momentum_structural_context_publications WHERE revision=:r'),
                        {'r': w.cursor.revision}).scalar_one()
        _, header, changed = delta.decode_wire(raw)
        assert [v.symbol for v in changed] == ['A'] and header.symbols == ()
        names = c.execute(sa.text('SELECT symbol FROM momentum_structural_context_versions WHERE revision=:r'),
                          {'r': w.cursor.revision}).scalars().all()
        assert names == ['A']
    event_read = read(engine, cursor)
    assert event_read.publications[0].snapshot == result
    projection = current(engine, w.cursor.stream_id, ['A', 'MISSING'])
    assert [v.symbol for v in projection.publication.snapshot.symbols] == ['A']
    assert projection.publication.state_sha256 == event_read.publications[0].state_sha256
    assert projection.receipt(['MISSING'])['symbols']['MISSING']['status'] == 'not_enrolled'
    assert projection.publication.projected


@pytest.mark.parametrize('damage', ['missing_member', 'changed_descriptor', 'future_member', 'missing_object', 'changed_object'])
def test_projection_never_substitutes_old_or_partial_state_when_index_or_value_is_bad(durable, damage):
    engine, o, w = durable
    o.update_demand('watch', revision=1, symbols=['A', 'B'])
    wave(engine, o)
    with engine.begin() as c:
        queries = {
            'missing_member': "DELETE FROM momentum_structural_context_members WHERE symbol='B'",
            'changed_descriptor': "UPDATE momentum_structural_context_members SET descriptor=replace(descriptor,'B','WRONG') WHERE symbol='B'",
            'future_member': "UPDATE momentum_structural_context_members SET revision=revision+999 WHERE symbol='B'",
            'missing_object': "DELETE FROM momentum_structural_context_objects WHERE payload_sha256=(SELECT payload_sha256 FROM momentum_structural_context_members WHERE symbol='A')",
            'changed_object': "UPDATE momentum_structural_context_objects SET payload=payload||' ' WHERE payload_sha256=(SELECT payload_sha256 FROM momentum_structural_context_members WHERE symbol='A')",
        }
        c.execute(sa.text(queries[damage]))
    with pytest.raises(ValueError, match='context_materialized'):
        current(engine, w.cursor.stream_id, ['A'])


def test_partial_projection_is_never_a_complete_event_delivery_or_acknowledgment(durable):
    engine, o, w = durable
    wave(engine, o)
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        value = j.read_current_context(c, stream_id=w.cursor.stream_id,
                                       max_payload_bytes=2_000_000, selected_symbols=['A'])
    with pytest.raises(ValueError, match='no_event_delivery'):
        value.publications[0].new_wave_events
    with engine.begin() as c:
        # Pin offset to the delivery's predecessor to isolate the projection rule.
        c.execute(sa.text('''INSERT INTO momentum_structural_context_consumers
            (stream_id,generation,consumer,revision,root_sha256) VALUES(:s,:g,'entry',:r,:root)'''),
            {'s': value.after.stream_id, 'g': value.after.generation, 'r': value.after.revision, 'root': value.after.root_sha256})
        with pytest.raises(ValueError, match='projection_cannot_acknowledge'):
            j.acknowledge(c, consumer='entry', read=value)


def test_native_projection_uses_verified_alias_or_uuid_without_provider_spelling_fallback(native_owner):
    engine, service = native_owner
    service.update_native_enrollment(enrollment())
    publish(engine, packet(engine, [('ABR-D', 10)], start=2))
    service.owner.advance_one(engine)
    stream, asset = service.writer.cursor.stream_id, str(UUID(int=1))
    by_alias = current(engine, stream, ['ABR.PRD']).receipt(['ABR.PRD'])
    by_id = current(engine, stream, [asset]).receipt([], native_asset_ids=[asset])
    assert by_alias['symbols']['ABR.PRD'] == by_id['native_assets'][asset]
    assert current(engine, stream, ['ABR-D']).publication.snapshot.symbols == ()


def test_persistence_rollback_cannot_advance_materialized_membership(durable, monkeypatch):
    engine, o, w = durable
    wave(engine, o)
    before = current(engine, w.cursor.stream_id, ['A'])
    execute = w._c.execute
    def fail(statement, *args, **kwargs):
        if 'INSERT INTO momentum_structural_context_members' in str(statement):
            raise RuntimeError('after-version-before-current-member')
        return execute(statement, *args, **kwargs)
    monkeypatch.setattr(w._c, 'execute', fail)
    publish(engine, packet(engine, [('A', 14)], start=6))
    with pytest.raises(RuntimeError):
        o.advance_one(engine)
    after = current(engine, w.cursor.stream_id, ['A'])
    assert after.publication == before.publication
    assert not after.writer_present


def test_recovery_refuses_corrupt_derived_materialization_even_when_event_inputs_are_intact(recoverable):
    engine, service = recoverable
    wave(engine, service.owner)
    stream = service.writer.cursor.stream_id
    service.close()
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM momentum_structural_context_members WHERE symbol='A'"))
    with pytest.raises(ValueError, match='materialized_membership_digest_mismatch'):
        restore(engine, stream, clock_ns=lambda: CLOCK)


def test_delta_requires_exact_base_and_cannot_repeat_unchanged_objects(durable):
    engine, o, w = durable
    wave(engine, o)
    state = w._state
    with pytest.raises(ValueError, match='base_state_mismatch'):
        delta.apply(state.payload)
    raw, _, _ = delta.decode_wire(state.payload)
    raw['changed'] = j._encode(())
    with pytest.raises(ValueError):
        delta.apply(j._json(raw))
