"""Actual DB input/output atomicity and warm reducer recovery without new clocks."""
from datetime import timedelta
import json
import os
import re
import subprocess
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa

from tests.test_ordinary_structural_context import source, packet, publish, NOW, counts
from tests.test_structural_context_journal import read
from app.services.trading.momentum_neural import ordinary_structural_context as o
from app.services.trading.momentum_neural import structural_context_journal as j
from app.services.trading.momentum_neural import structural_context_recovery as r
from app.services.trading.momentum_neural.structural_tape_prefix import Limits

CLOCK = int((NOW + timedelta(seconds=2)).timestamp())*10**9
CAP = 2_000_000


@pytest.fixture
def recoverable(source):
    with source.begin() as c:
        j.create_schema(c)
        r.create_schema(c)
    publish(source, packet(source, [('SEED', 1)]))
    service = r.RecoverableStructuralContext.create(source, stream_id='recovery-'+uuid4().hex,
        max_payload_bytes=CAP, max_input_bytes=CAP, limits=Limits(100,100,100),
        max_symbols=5, max_trade_rows=100, clock_ns=lambda:CLOCK)
    try:
        yield source, service
    finally:
        service.close()


def restore(engine, stream, **kwargs):
    return r.RecoverableStructuralContext.restore(engine, stream_id=stream,
        max_payload_bytes=CAP, max_input_bytes=CAP, max_replay_inputs=100, **kwargs)


def wave(engine, service):
    service.owner.update_demand('ranking', revision=1, symbols=['A', 'B'])
    publish(engine, packet(engine, [('A',10), ('A',12), ('A',11), ('A',13),
                                   ('B',20), ('B',19), ('B',21)], start=2))
    return service.owner.advance_one(engine)


def test_exact_restore_retains_noop_demand_revisions_and_continues_same_nested_history(recoverable):
    engine, service = recoverable
    anchor = service.owner.read('audit').source
    reference = o.OrdinaryStructuralContextOwner(anchor=anchor, limits=Limits(100,100,100),
        max_symbols=5, max_trade_rows=100, clock_ns=lambda:CLOCK)
    snap = wave(engine, service)
    reference.update_demand('ranking', revision=1, symbols=['A','B'])
    assert reference.advance_one(engine) == snap
    for target in (service.owner, reference):
        target.update_demand('held', revision=1, symbols=['A'])
        target.update_demand('pending', revision=1, symbols=['B'])
        target.update_demand('ranking', revision=2, symbols=[])
        target.update_demand('inventory', revision=1, symbols=None)
        target.update_demand('inventory', revision=2, symbols=None)  # Same output, new hidden revision.
    before = service.owner.read('exit')
    assert service._input_revision > service.writer.cursor.revision
    stream, cursor = service.writer.cursor.stream_id, service.writer.cursor
    with engine.begin() as c:
        delivery = read(engine, service.writer.anchor)
        j.acknowledge(c, consumer='exit', read=delivery)
    service.close()
    # Historical raw data is no longer a source of recovery truth.
    with engine.begin() as c:
        c.execute(sa.text("UPDATE iqfeed_trade_ticks SET price=999 WHERE symbol='A'"))
    clock_calls=[]
    def next_clock():
        clock_calls.append(True)
        return CLOCK+10**9
    resumed = restore(engine, stream, clock_ns=next_clock)
    try:
        assert clock_calls == [] and resumed.writer.cursor == cursor
        assert resumed.owner.read('exit') == before
        with engine.connect() as c:
            assert j.consumer_cursor(c, stream_id=stream, consumer='exit') == cursor
        with pytest.raises(ValueError, match='stale_demand_revision'):
            resumed.owner.update_demand('inventory', revision=2, symbols=[])
        resumed.owner.update_demand('inventory', revision=3, symbols=[])
        reference.update_demand('inventory', revision=3, symbols=[])
        reference._clock_ns = next_clock
        publish(engine, packet(engine, [('A',12), ('A',14), ('B',20)], start=9))
        assert resumed.owner.advance_one(engine) == reference.advance_one(engine)
        assert len(clock_calls) == 2
        assert counts(resumed.owner.read('selection')) == {'A':6, 'B':4}
    finally:
        resumed.close()


def test_input_output_rollback_drops_uncommitted_private_prefix_and_reprocesses_source(recoverable, monkeypatch):
    engine, service = recoverable
    wave(engine, service)
    before, cursor = service.owner.read('exit'), service.writer.cursor
    original = service.writer._c.execute
    def fail(statement, *a, **k):
        if 'UPDATE momentum_structural_context_recovery_heads' in str(statement):
            raise RuntimeError('after-input-insert-before-commit')
        return original(statement, *a, **k)
    monkeypatch.setattr(service.writer._c, 'execute', fail)
    publish(engine, packet(engine, [('A',14)], start=9))
    with pytest.raises(RuntimeError, match='after-input'):
        service.owner.advance_one(engine)
    result=read(engine, cursor)
    assert not result.writer_present and result.publications == ()
    resumed=restore(engine, cursor.stream_id, clock_ns=lambda:CLOCK)
    try:
        assert resumed.owner.read('exit') == before
        assert counts(resumed.owner.advance_one(engine)) == {'A':5,'B':3}
    finally:
        resumed.close()


def test_partial_demand_batch_is_one_durable_output_and_restores_exactly(recoverable):
    engine, service = recoverable
    service.owner.update_demands({'held': dict(revision=1, symbols=['A'], complete=True),
                                 'pending': dict(revision=1, symbols=['B'], complete=True)})
    cursor = service.writer.cursor
    service.owner.update_demands({'held': dict(revision=2, symbols=['C'], complete=False),
                                 'pending': dict(revision=2, symbols=None, complete=False)})
    snap = service.owner.read('exit')
    delivery = read(engine, cursor)
    assert len(delivery.publications) == 1
    assert delivery.publications[0].snapshot == snap
    assert snap.stale_demand_sources == ('held', 'pending')
    service.close()
    resumed = restore(engine, cursor.stream_id, clock_ns=lambda:CLOCK)
    try:
        assert resumed.owner.read('selection') == snap
        with pytest.raises(ValueError, match='stale_demand_revision'):
            resumed.owner.update_demand('held', revision=2, symbols=[])
        resumed.owner.update_demands({'held': dict(revision=3, symbols=['C'], complete=True),
                                     'pending': dict(revision=3, symbols=[], complete=True)})
        after = resumed.owner.read('exit')
        assert after.stale_demand_sources == ()
        assert {v.symbol:v.demand_reasons for v in after.symbols} == {'A':(), 'B':(), 'C':('held',)}
    finally:
        resumed.close()


def test_failed_demand_batch_transaction_recovers_neither_half(recoverable, monkeypatch):
    engine, service = recoverable
    service.owner.update_demand('pending', revision=1, symbols=['A'])
    before, cursor = service.owner.read('exit'), service.writer.cursor
    original = service.writer._c.execute
    def fail(statement, *args, **kwargs):
        if 'UPDATE momentum_structural_context_recovery_heads' in str(statement):
            raise RuntimeError('demand-before-commit')
        return original(statement, *args, **kwargs)
    monkeypatch.setattr(service.writer._c, 'execute', fail)
    with pytest.raises(RuntimeError, match='demand-before-commit'):
        service.owner.update_demands({'pending': dict(revision=2, symbols=[], complete=True),
                                     'held': dict(revision=1, symbols=['A'], complete=True)})
    assert read(engine, cursor).publications == ()
    resumed = restore(engine, cursor.stream_id, clock_ns=lambda:CLOCK)
    try:
        assert resumed.owner.read('exit') == before
        resumed.owner.update_demands({'pending': dict(revision=2, symbols=[], complete=True),
                                     'held': dict(revision=1, symbols=['A'], complete=True)})
        assert resumed.owner.read('exit').symbols[0].demand_reasons == ('held',)
    finally:
        resumed.close()


def test_commit_acknowledgement_loss_recovers_durable_commit_without_repeating_event(recoverable, monkeypatch):
    engine, service = recoverable
    wave(engine, service)
    cursor=service.writer.cursor
    original=service.writer._c._commit_impl
    fired=[]
    def commit_then_fail():
        original()
        if not fired:
            fired.append(True)
            raise RuntimeError('commit-ack-lost')
    monkeypatch.setattr(service.writer._c, '_commit_impl', commit_then_fail)
    publish(engine, packet(engine, [('A',14)], start=9))
    with pytest.raises(RuntimeError, match='commit-ack-lost'):
        service.owner.advance_one(engine)
    result=read(engine, cursor)
    assert len(result.publications)==1 and not result.writer_present
    resumed=restore(engine, cursor.stream_id, clock_ns=lambda:CLOCK)
    try:
        assert resumed.owner.read('exit')==result.publications[0].snapshot
        head=resumed.writer.cursor
        resumed.owner.advance_one(engine)  # No next source release; no repeated event publication.
        assert resumed.writer.cursor==head
    finally:
        resumed.close()


def test_restore_is_fenced_for_its_entire_reduction_and_checks_original_code(recoverable, monkeypatch):
    engine, service = recoverable
    wave(engine, service)
    stream=service.writer.cursor.stream_id
    with pytest.raises(ValueError, match='writer_already_present'):
        restore(engine, stream)
    service.close()
    original=o.Prefix.prepare_frontier
    checked=[]
    def check_lock(self, *a, **k):
        with pytest.raises(ValueError, match='writer_already_present'):
            restore(engine, stream)
        checked.append(True)
        return original(self,*a,**k)
    monkeypatch.setattr(o.Prefix, 'prepare_frontier', check_lock)
    resumed=restore(engine, stream)
    resumed.close()
    assert checked
    monkeypatch.setattr(r, 'code_identity', lambda:'f'*64)
    with pytest.raises(ValueError, match='code_or_contract_changed'):
        restore(engine, stream)
    assert not read(engine, service.writer.anchor).writer_present


@pytest.mark.parametrize('damage', ['delete', 'payload', 'head', 'output'])
def test_missing_or_changed_retained_evidence_never_yields_warm_owner(recoverable, damage):
    engine, service=recoverable
    wave(engine, service)
    stream=service.writer.cursor.stream_id
    service.close()
    with engine.begin() as c:
        statements={
            'delete': 'DELETE FROM momentum_structural_context_inputs WHERE revision=2',
            'payload': "UPDATE momentum_structural_context_inputs SET input_payload=input_payload||' ' WHERE revision=2",
            'head': "UPDATE momentum_structural_context_recovery_heads SET root_sha256=repeat('f',64)",
            'output': "UPDATE momentum_structural_context_publications SET payload=payload||' ' WHERE revision=2",
        }
        c.execute(sa.text(statements[damage]))
    with pytest.raises(ValueError):
        restore(engine, stream)
    # read_context itself rejects damaged output, so inspect only fence presence.
    with engine.connect() as c:
        assert not j._lock_present(c, stream)


def test_recomputation_detects_changed_clock_even_after_input_chain_is_rehashed(recoverable):
    engine, service=recoverable
    wave(engine, service)
    stream=service.writer.cursor.stream_id
    service.close()
    with engine.begin() as c:
        row=c.execute(sa.text('SELECT * FROM momentum_structural_context_inputs ORDER BY revision DESC LIMIT 1')).mappings().one()
        envelope=r.decode_input(row['input_payload'])
        envelope['input']['known_ns']+=1
        payload=r.encode_input(envelope)
        digest=j._sha(payload)
        out=j.JournalCursor(stream, row['generation'], row['output_revision'], row['output_root_sha256'])
        root=r._root(out,row['revision'],row['previous_sha256'],digest,out,row['output_payload_sha256'])
        c.execute(sa.text('UPDATE momentum_structural_context_inputs SET input_payload=:p,input_sha256=:sha,root_sha256=:root WHERE revision=:r'),
                  {'p':payload,'sha':digest,'root':root,'r':row['revision']})
        c.execute(sa.text('UPDATE momentum_structural_context_recovery_heads SET root_sha256=:root'),{'root':root})
    with pytest.raises(ValueError, match='recomputed_output_mismatch'):
        restore(engine, stream)


def test_unresolved_source_and_external_read_failure_are_replayed_without_new_io(recoverable, monkeypatch):
    engine, service=recoverable
    service.owner.update_demand('held', revision=1, symbols=['A'])
    bad=packet(engine, [('A',10)], start=2, invalid={'provider_delay_minutes':1})
    publish(engine,bad)
    assert service.owner.advance_one(engine).reason=='ordinary_provider_delay_unresolved'
    original=o.read_publications
    def unavailable(*a,**k): raise ValueError('fixture-source-unavailable')
    monkeypatch.setattr(o,'read_publications',unavailable)
    expected=service.owner.advance_one(engine)
    assert expected.reason=='fixture-source-unavailable'
    stream=service.writer.cursor.stream_id
    service.close()
    resumed=restore(engine,stream,clock_ns=lambda:(_ for _ in ()).throw(AssertionError('new recovery clock')))
    try:
        assert resumed.owner.read('exit')==expected
        assert resumed._input_revision==service._input_revision
    finally:
        resumed.close()
    monkeypatch.setattr(o,'read_publications',original)


def test_legacy_stream_and_insufficient_replay_capacity_are_explicit(recoverable):
    engine,service=recoverable
    wave(engine,service)
    stream=service.writer.cursor.stream_id
    service.close()
    with pytest.raises(ValueError,match='recovery_work_capacity'):
        r.RecoverableStructuralContext.restore(engine,stream_id=stream,max_payload_bytes=CAP,
            max_input_bytes=CAP,max_replay_inputs=1)
    with pytest.raises(ValueError,match='recovery_input_byte_capacity'):
        r.RecoverableStructuralContext.restore(engine,stream_id=stream,max_payload_bytes=CAP,
            max_input_bytes=1,max_replay_inputs=100)
    legacy=j.ContextJournalWriter.create(engine,stream_id='legacy-'+uuid4().hex,
        source_anchor=service.owner.read('audit').source,max_payload_bytes=CAP)
    legacy.close()
    with pytest.raises(ValueError,match='recovery_inputs_missing'):
        restore(engine,legacy.cursor.stream_id)


def test_separate_process_restores_all_outputs_without_source_reads_or_observation_clock(recoverable):
    engine,service=recoverable
    expected=wave(engine,service)
    stream=service.writer.cursor.stream_id
    with engine.connect() as c: schema=c.execute(sa.text('SELECT current_schema()')).scalar_one()
    assert re.fullmatch(r'test_print_publications_[a-f0-9]+',schema)
    service.close()
    code="""
import os,json,sqlalchemy as sa
from app.services.trading.momentum_neural import structural_context_recovery as r
from app.services.trading.momentum_neural import ordinary_structural_context as o
from app.services.trading.momentum_neural import structural_context_journal as j
def forbidden(*a,**k): raise AssertionError('recovery read changed input or clock')
o.read_publications=forbidden
engine=sa.create_engine(os.environ['TEST_DATABASE_URL'],connect_args={'options':'-c search_path='+os.environ['RECOVERY_TEST_SCHEMA']})
service=r.RecoverableStructuralContext.restore(engine,stream_id=os.environ['RECOVERY_TEST_STREAM'],max_payload_bytes=2000000,max_input_bytes=2000000,max_replay_inputs=100,clock_ns=forbidden)
try: print(json.dumps({'snapshot':j.encode_snapshot(service.owner.read('exit')),'inputs':service._input_revision}))
finally: service.close();engine.dispose()
"""
    result=subprocess.run([sys.executable,'-B','-c',code],env={**os.environ,
        'RECOVERY_TEST_SCHEMA':schema,'RECOVERY_TEST_STREAM':stream,'PYTHONIOENCODING':'utf-8'},
        capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stderr
    result=json.loads(result.stdout)
    assert j.decode_snapshot(result['snapshot'])==expected
    assert result['inputs']==service._input_revision


def test_input_capacity_failure_is_atomic_and_releases_writer(recoverable):
    engine,service=recoverable
    before=service.writer.cursor
    service._max_input_bytes=1
    with pytest.raises(ValueError,match='recovery_input_byte_capacity'):
        service.owner.update_demand('held',revision=1,symbols=['A'])
    result=read(engine,before)
    assert not result.publications and not result.writer_present
    resumed=restore(engine,before.stream_id)
    try:
        assert resumed.owner.read('audit').symbols==()
    finally:
        resumed.close()


def test_backlog_frontier_and_late_symbol_enrollment_survive_recovery(recoverable):
    engine,service=recoverable
    service.owner.update_demand('ranking',revision=1,symbols=['A'])
    publish(engine,packet(engine,[('A',10),('B',20)],start=2))
    publish(engine,packet(engine,[('A',12),('B',21)],start=4))
    publish(engine,packet(engine,[('A',11),('B',19)],start=6))
    first=service.owner.advance_one(engine)
    assert first.observed_frontier.revision==first.source.revision+2
    service.owner.update_demand('pending',revision=1,symbols=['B'])
    second=service.owner.advance_one(engine)
    assert counts(second)=={'A':2,'B':1}
    stream=service.writer.cursor.stream_id
    service.close()
    resumed=restore(engine,stream,clock_ns=lambda:CLOCK)
    try:
        assert resumed.owner.read('entry')==second
        assert counts(resumed.owner.advance_one(engine))=={'A':3,'B':2}
    finally:
        resumed.close()


def test_failure_reading_code_identity_releases_recovery_fence(recoverable,monkeypatch):
    engine,service=recoverable
    wave(engine,service)
    stream=service.writer.cursor.stream_id
    service.close()
    def unavailable(): raise OSError('fixture-code-file-unavailable')
    monkeypatch.setattr(r,'code_identity',unavailable)
    with pytest.raises(OSError,match='code-file-unavailable'):
        restore(engine,stream)
    with engine.connect() as c:
        assert not j._lock_present(c,stream)


def test_unserializable_input_after_read_refuses_owner_and_closes_fence(recoverable):
    engine,service=recoverable
    service.owner.update_demand('held',revision=1,symbols=['A'])
    cursor=service.writer.cursor
    publish(engine,packet(engine,[('A',10)],start=2))
    service.owner._clock_ns=lambda:float('nan')
    with pytest.raises(ValueError,match='recovery_input_type_invalid'):
        service.owner.advance_one(engine)
    result=read(engine,cursor)
    assert result.publications==() and not result.writer_present
    with pytest.raises(RuntimeError,match='reconstruction_required'):
        service.owner.advance_one(engine)
