"""Real DB journal, owner fence, consumer restart and atomic publication tests."""
from dataclasses import asdict, replace
from fractions import Fraction
import json
import os
import re
import subprocess
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa

from tests.test_ordinary_structural_context import source, owner, packet, publish, counts
from app.services.trading.momentum_neural import structural_context_journal as j


@pytest.fixture
def durable(source):
    with source.begin() as c:
        j.create_schema(c)
    o = owner(source)
    w = j.ContextJournalWriter.create(source, stream_id='fixture-'+uuid4().hex,
        source_anchor=o.read('audit').source, max_payload_bytes=2_000_000)
    try:
        o.bind_publication_sink(w.publish)
        yield source, o, w
    finally:
        w.close()


def read(engine, cursor, *, publications=100, byte_capacity=2_000_000):
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        return j.read_context(c, after=cursor, max_publications=publications,
                              max_payload_bytes=byte_capacity)


def wave(engine, o):
    o.update_demand('ranking', revision=1, symbols=['A'])
    publish(engine, packet(engine, [('A', 10), ('A', 12), ('A', 11), ('A', 13)],
                           start=2, invalid={'sz': 0.125}))
    return o.advance_one(engine)


def test_slow_consumers_receive_every_publication_and_source_events_only_once(durable):
    engine, o, w = durable
    observed = wave(engine, o)
    o.update_demand('held', revision=1, symbols=['A'])
    o.update_demand('ranking', revision=2, symbols=[])
    first = read(engine, w.anchor, publications=2)
    assert not first.caught_up and first.writer_present
    rest = read(engine, first.consumed)
    assert rest.caught_up and len(rest.publications) == 3
    source_pub = rest.publications[0]
    assert source_pub.snapshot == observed and source_pub.source_advanced
    assert len(source_pub.new_events[0][1]) == 3
    assert source_pub.snapshot.symbols[0].scopes[0].whole_mass[0] == Fraction(1,8)
    assert all(p.new_events == () for p in rest.publications[1:])
    assert all(p.snapshot.symbols[0].events == observed.symbols[0].events for p in rest.publications)
    for consumer in ('selection', 'entry', 'exit'):
        with engine.connect() as c:
            assert j.consumer_cursor(c, stream_id=w.anchor.stream_id, consumer=consumer) == w.anchor
        assert read(engine, w.anchor).publications[-1].snapshot == o.read(consumer)


def test_another_process_reads_the_same_immutable_result_without_reducing(durable):
    engine, o, w = durable
    observed = wave(engine, o)
    with engine.connect() as c:
        schema = c.execute(sa.text('SELECT current_schema()')).scalar_one()
    assert re.fullmatch(r'test_print_publications_[a-f0-9]+', schema)
    code = '''
import os,json,sqlalchemy as sa
from app.services.trading.momentum_neural import structural_context_journal as j
from app.services.trading.momentum_neural.structural_tape_prefix import Prefix
def forbidden(*a,**k): raise AssertionError('consumer tried to reduce tape')
Prefix.append_frontier=Prefix.prepare_frontier=forbidden
engine=sa.create_engine(os.environ['TEST_DATABASE_URL'],connect_args={'options':'-c search_path='+os.environ['CONTEXT_TEST_SCHEMA']})
with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
 result=j.read_context(c,after=j.JournalCursor(**json.loads(os.environ['CONTEXT_TEST_CURSOR'])),max_publications=100,max_payload_bytes=2000000)
 last=result.publications[-1]
 print(json.dumps({'root':last.snapshot.root_sha256,'counts':{s.symbol:s.print_count for s in last.snapshot.symbols},'snapshot':j.encode_snapshot(last.snapshot)}))
engine.dispose()
'''
    env = {**os.environ, 'CONTEXT_TEST_SCHEMA': schema,
           'CONTEXT_TEST_CURSOR': json.dumps(asdict(w.anchor)), 'PYTHONIOENCODING': 'utf-8'}
    r = subprocess.run([sys.executable, '-B', '-c', code], env=env, capture_output=True,
                       text=True, encoding='utf-8', timeout=60)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert result['root'] == observed.root_sha256 and result['counts'] == {'A': 4}
    assert j.decode_snapshot(result['snapshot']) == observed


def test_only_one_writer_and_existing_stream_cannot_silently_cold_restart(durable):
    engine, o, w = durable
    kwargs = dict(stream_id=w.anchor.stream_id, source_anchor=o.read('audit').source, max_payload_bytes=10000)
    with pytest.raises(ValueError, match='writer_already_present'):
        j.ContextJournalWriter.create(engine, **kwargs)
    assert read(engine, w.anchor).writer_present
    w.close()
    assert not read(engine, w.anchor).writer_present
    with pytest.raises(ValueError, match='restore_required'):
        j.ContextJournalWriter.create(engine, **kwargs)
    assert not read(engine, w.anchor).writer_present


def test_failed_db_publication_rolls_back_and_no_uncommitted_view_is_exposed(durable, monkeypatch):
    engine, o, w = durable
    o.update_demand('held', revision=1, symbols=['A'])
    before, cursor = o.read('exit'), w.cursor
    original = w._c.execute
    def fail_after_insert(statement, *args, **kwargs):
        if 'UPDATE momentum_structural_context_heads' in str(statement):
            raise RuntimeError('injected-after-publication-insert')
        return original(statement, *args, **kwargs)
    monkeypatch.setattr(w._c, 'execute', fail_after_insert)
    publish(engine, packet(engine, [('A', 10)], start=2))
    with pytest.raises(RuntimeError, match='injected-after'):
        o.advance_one(engine)
    assert counts(o.read('exit')) == counts(before) == {'A': 0}
    assert o.read('exit').status == 'unresolved'
    result = read(engine, cursor)
    assert not result.publications and not result.writer_present
    with pytest.raises(RuntimeError, match='reconstruction_required'):
        o.update_demand('watch', revision=1, symbols=['B'])


@pytest.mark.parametrize('corruption', ['payload', 'missing', 'source_flag', 'head'])
def test_missing_or_changed_retained_publications_fail_explicitly(durable, corruption):
    engine, o, w = durable
    wave(engine, o)
    with engine.begin() as c:
        params = {'s': w.anchor.stream_id}
        if corruption == 'payload':
            c.execute(sa.text("UPDATE momentum_structural_context_publications SET payload=payload||' ' WHERE stream_id=:s AND revision=2"), params)
        elif corruption == 'missing':
            c.execute(sa.text('DELETE FROM momentum_structural_context_publications WHERE stream_id=:s AND revision=2'), params)
        elif corruption == 'source_flag':
            c.execute(sa.text('UPDATE momentum_structural_context_publications SET source_advanced=NOT source_advanced WHERE stream_id=:s AND revision=3'), params)
        else:
            c.execute(sa.text("UPDATE momentum_structural_context_heads SET root_sha256=repeat('a',64) WHERE stream_id=:s"), params)
    with pytest.raises(ValueError, match='gap|digest|head'):
        read(engine, w.anchor)


def test_consumer_offset_commits_with_processing_and_survives_reader_restart(durable):
    engine, o, w = durable
    wave(engine, o)
    delivery = read(engine, w.anchor)
    with pytest.raises(RuntimeError, match='rollback'):
        with engine.begin() as c:
            j.acknowledge(c, consumer='entry', read=delivery)
            raise RuntimeError('rollback consumer processing')
    with engine.begin() as c:
        assert j.consumer_cursor(c, stream_id=w.anchor.stream_id, consumer='entry') == w.anchor
        j.acknowledge(c, consumer='entry', read=delivery)
    with engine.connect() as c:
        cursor = j.consumer_cursor(c, stream_id=w.anchor.stream_id, consumer='entry')
        assert j.consumer_cursor(c, stream_id=w.anchor.stream_id, consumer='exit') == w.anchor
    assert cursor == delivery.consumed and not read(engine, cursor).publications
    with engine.begin() as c:
        with pytest.raises(ValueError, match='offset_changed'):
            j.acknowledge(c, consumer='entry', read=delivery)


def test_ack_cannot_skip_or_change_delivered_records(durable):
    engine, o, w = durable
    wave(engine, o)
    delivery = read(engine, w.anchor)
    changed = replace(delivery, publications=delivery.publications[1:])
    with engine.begin() as c:
        with pytest.raises(ValueError, match='publication_gap'):
            j.acknowledge(c, consumer='exit', read=changed)
    final = delivery.publications[-1]
    changed = replace(delivery, publications=delivery.publications[:-1]+(
        replace(final, snapshot=replace(final.snapshot, root_sha256='a'*64)),))
    with engine.begin() as c:
        with pytest.raises(ValueError, match='evidence_missing_or_changed'):
            j.acknowledge(c, consumer='exit', read=changed)
        assert j.consumer_cursor(c, stream_id=w.anchor.stream_id, consumer='exit') == w.anchor


def test_size_bounds_never_split_a_context_and_failed_writer_closes_fence(durable):
    engine, o, w = durable
    wave(engine, o)
    with engine.connect() as c:
        first_size = c.execute(sa.text('SELECT octet_length(payload) FROM momentum_structural_context_publications WHERE stream_id=:s AND revision=1'), {'s': w.anchor.stream_id}).scalar_one()
    one = read(engine, w.anchor, byte_capacity=first_size)
    assert len(one.publications) == 1 and not one.caught_up
    with pytest.raises(ValueError, match='atomic_context_exceeds'):
        read(engine, one.consumed, byte_capacity=first_size)
    cursor = w.cursor
    w._max_bytes = 1  # Operational storage failure, never a strategy lookback.
    with pytest.raises(ValueError, match='byte_capacity'):
        o.update_demand('held', revision=1, symbols=['A'])
    assert not read(engine, cursor).writer_present
    assert o.read('exit').status == 'unresolved'


def test_lost_session_lock_cannot_be_automatically_reacquired(durable):
    engine, o, w = durable
    w._c.execute(sa.text('SELECT pg_advisory_unlock(hashtext(:ns),hashtext(:s))'),
                 {'ns': j.LOCK_NAMESPACE, 's': w.anchor.stream_id})
    w._c.commit()
    cursor = w.cursor
    with pytest.raises(ValueError, match='fence_lost'):
        o.update_demand('held', revision=1, symbols=['A'])
    assert not read(engine, cursor).publications


def test_identical_observation_is_not_republished(durable):
    engine, o, w = durable
    snap = wave(engine, o)
    cursor = w.cursor
    w.publish(snap)
    assert w.cursor == cursor and not read(engine, cursor).publications


def test_codec_refuses_authority_flags_and_unknown_record_types(durable):
    engine, o, w = durable
    snap = wave(engine, o)
    with pytest.raises(ValueError, match='observation_context_required'):
        j.encode_snapshot(replace(snap, order_authority=True))
    raw = j.encode_snapshot(snap)
    assert j.decode_snapshot(raw) == snap
    with pytest.raises(ValueError, match='record_shape'):
        j.decode_snapshot(raw.replace('"type":"ContextSnapshot"', '"type":"ExecutableOrder"'))
    with pytest.raises(ValueError, match='duplicate_json_key'):
        j.decode_snapshot('{"tuple":[],"tuple":[]}')


def test_commit_coupled_wake_has_cursor_only_and_reader_needs_no_notification(durable, monkeypatch):
    import select
    engine, o, w = durable
    raw = engine.raw_connection()
    listener = raw.driver_connection
    listener.autocommit = True
    original = w._c.execute
    checked_before_commit = []
    try:
        with listener.cursor() as c:
            c.execute('LISTEN '+j.CHANNEL)
        def observe(statement, *args, **kwargs):
            result = original(statement, *args, **kwargs)
            if 'pg_notify' in str(statement):
                listener.poll()
                assert listener.notifies == []
                checked_before_commit.append(True)
            return result
        monkeypatch.setattr(w._c, 'execute', observe)
        o.update_demand('held', revision=1, symbols=['A'])
        assert checked_before_commit == [True]
        assert select.select([listener], [], [], 5)[0]
        listener.poll()
        messages = list(listener.notifies)
        assert [(m.channel, json.loads(m.payload)) for m in messages] == [
            (j.CHANNEL, [w.cursor.stream_id, w.cursor.generation, w.cursor.revision])]
        listener.notifies.clear()  # Losing the wake cannot lose durable evidence.
        assert read(engine, w.anchor).consumed == w.cursor
    finally:
        with listener.cursor() as c:
            c.execute('UNLISTEN '+j.CHANNEL)
        listener.autocommit = False
        raw.close()


def test_schema_creation_is_idempotent_and_consumer_cas_rejects_competing_workers(durable):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    engine, o, w = durable
    with engine.begin() as c:
        j.create_schema(c)
    wave(engine, o)
    delivery = read(engine, w.anchor)
    ready = Barrier(2)
    def worker():
        ready.wait(timeout=10)
        try:
            with engine.begin() as c:
                j.acknowledge(c, consumer='entry', read=delivery)
            return 'committed'
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(results) == ['committed', 'context_consumer_offset_changed']
    with engine.connect() as c:
        assert j.consumer_cursor(c, stream_id=w.anchor.stream_id, consumer='entry') == delivery.consumed


def test_codec_refuses_inconsistent_mass_partition(durable):
    engine, o, w = durable
    snap = wave(engine, o)
    view = snap.symbols[0]
    scope = view.scopes[0]
    wrong_mass = (scope.whole_mass[0]+1,) + scope.whole_mass[1:]
    bad = replace(snap, symbols=(replace(view, scopes=(replace(scope, whole_mass=wrong_mass),)),))
    with pytest.raises(ValueError, match='scope_arithmetic_invalid'):
        j.encode_snapshot(bad)
