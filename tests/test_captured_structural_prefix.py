"""Exact bridge provenance through actual coordinator reads to structural state."""
from dataclasses import replace
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys

import pytest


REPO=Path(__file__).resolve().parents[1]


@pytest.fixture
def wire(tmp_path,monkeypatch):
    monkeypatch.setenv('CHILI_PYTEST','1')
    for name in ('DATABASE_URL','TEST_DATABASE_URL'):
        monkeypatch.setenv(name,'postgresql://chili:chili@localhost:5433/chili_rossbench26_test')
    import psycopg2
    def forbid(*args,**kwargs): raise AssertionError('Adapter tests forbid DB connections')
    monkeypatch.setattr(psycopg2,'connect',forbid)
    monkeypatch.syspath_prepend(str(REPO))
    spec=importlib.util.spec_from_file_location('live_capture_for_structural_adapter',REPO/'tests/test_live_replay_capture.py')
    f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
    from app.services.trading.momentum_neural import captured_structural_prefix as a
    coordinator,_startup,clock=f._coordinator(tmp_path/'capture',extra_streams=(f.CaptureStream.IQFEED_PRINT,))
    consumer=a.CapturedStructuralPrefix(identity=coordinator.identity,symbol='VEEE',segment='test',
        limits=a.Limits(100,100,100),anchor_sequence=coordinator._prefix_rows,
        anchor_root_sha256=coordinator._current_prefix_root())
    run_id=str(f.uuid.uuid4())
    emitted=0;reads=0

    def emit(price,*,provenance_changes=None,missing_provenance=False,provider_at=None):
        nonlocal emitted
        emitted+=1
        available=f.BASE+timedelta(seconds=3)
        clock.set(available)
        clocks,payload=f._iqfeed_exact_print_observation(binding=coordinator.resource_binding,
            bridge_run_id=run_id,generation=1,frame_sequence=emitted,available_at=available)
        payload.update(price=price,bid=price-.01,ask=price+.01)
        provenance=payload[f.IQFEED_L1_SOURCE_PROVENANCE_FIELD]
        if provider_at is not None:
            clocks=replace(clocks,provider_event_at=provider_at)
            provenance['provider_event_at']=provider_at.isoformat().replace('+00:00','Z')
        provenance.update(provenance_changes or {})
        if missing_provenance: del payload[f.IQFEED_L1_SOURCE_PROVENANCE_FIELD]
        result=coordinator.submit_exact_input(stream=f.CaptureStream.IQFEED_PRINT,provider='iqfeed',
            symbol='VEEE',clocks=clocks,payload=payload)
        assert result.accepted
        return result.event

    def read(*,after=None):
        nonlocal reads
        reads+=1
        decision=f'adapter-decision-{reads}'
        captured=coordinator.capture_iqfeed_sequence_read(decision_id=decision,symbol='VEEE',
            after_sequence=consumer.source_sequence if after is None else after,
            requested_at=clock.now,returned_at=clock.now)
        return captured,decision

    return f,a,coordinator,consumer,emit,read


def state(consumer):
    p=consumer.prefix
    return (p.count,p.prefix_sha256,p.last_receipt,p.active_references(),consumer.source_sequence,
        consumer.source_root_sha256,consumer.last_read_event_sha256,
        frozenset(consumer._provider_keys),tuple(consumer._witnesses))


def test_actual_coordinator_reads_preserve_plateau_ids_and_source_witnesses(wire):
    f,a,coordinator,consumer,emit,read=wire
    events=[]
    for price in [5.0,4.9,4.9,5.1]:
        events.append(emit(price))
        captured,decision=read()
        assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    p=consumer.prefix
    assert p.count==4
    ref=p.active_references()[0]
    assert (ref.kind,ref.plateau_first_id,ref.origin_id,ref.confirmation_id)==(
        'valley',events[1].sequence,events[2].sequence,events[3].sequence)
    for i,event in enumerate(events):
        witness=consumer.witness(i)
        assert witness.event_sha256==event.event_sha256 and witness.payload_sha256==event.payload_sha256
        assert p.tick(i).frame_sequence==i+1
        assert p.tick(i).published_ns==a.ns(event.clocks.available_at)
        assert p.tick(i).event_ns==a.ns(event.clocks.provider_event_at)


def test_same_read_is_idempotent_only_after_actual_source_validation(wire):
    f,a,coordinator,consumer,emit,read=wire
    emit(5.0);captured,decision=read()
    assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    before=state(consumer)
    assert consumer.apply_read(captured,decision_id=decision).status=='already_applied'
    assert state(consumer)==before
    altered=replace(captured,source_events=())
    assert consumer.apply_read(altered,decision_id=decision).status=='unresolved'
    assert state(consumer)==before


def test_wrong_decision_or_cursor_preserves_all_state(wire):
    f,a,coordinator,consumer,emit,read=wire
    anchor=consumer.source_sequence
    emit(5.0);captured,decision=read()
    before=state(consumer)
    assert consumer.apply_read(captured,decision_id='wrong-decision').reason=='captured_read_evidence_invalid'
    assert state(consumer)==before
    assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    emit(4.9);stale,next_decision=read(after=anchor)
    before=state(consumer)
    assert consumer.apply_read(stale,decision_id=next_decision).reason=='capture_source_cursor_mismatch'
    assert state(consumer)==before


def test_empty_source_boundaries_do_not_invent_wave_or_mass(wire):
    f,a,coordinator,consumer,emit,read=wire
    first,decision=read()
    result=consumer.apply_read(first,decision_id=decision)
    assert result.status=='already_applied' and result.reason=='source_prefix_unchanged'
    second,decision=read()
    assert consumer.apply_read(second,decision_id=decision).status=='applied'
    assert consumer.prefix.count==0 and consumer.prefix.active_references()==()


@pytest.mark.parametrize('bad_kind,reason',[
    ('missing','exact_print_provenance_required'),
    ('generation','epoch_change_requires_new_segment'),
    ('frame','source_frame_order_requires_reconstruction'),
    ('provider_key','repeated_provider_identity_requires_reconstruction'),
    ('late','late_or_duplicate_tick'),
    ('future','source_clock_from_future'),
])
def test_bad_source_leaves_prefix_cursor_and_witnesses_unmodified(wire,bad_kind,reason):
    f,a,coordinator,consumer,emit,read=wire
    first=emit(5.0);captured,decision=read()
    assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    kwargs={}
    if bad_kind=='missing': kwargs['missing_provenance']=True
    if bad_kind=='generation': kwargs['provenance_changes']={'connection_generation':2}
    if bad_kind=='frame': kwargs['provenance_changes']={'source_frame_sequence':1}
    if bad_kind=='provider_key': kwargs['provenance_changes']={'provider_tick_id':'10001'}
    if bad_kind=='late': kwargs['provider_at']=first.clocks.provider_event_at-timedelta(microseconds=1)
    if bad_kind=='future': kwargs['provider_at']=first.clocks.available_at+timedelta(microseconds=1)
    emit(4.9,**kwargs);captured,decision=read()
    before=state(consumer)
    assert consumer.apply_read(captured,decision_id=decision).reason==reason
    assert state(consumer)==before


def test_entire_batch_rejects_mixed_epoch_without_partial_first_row(wire):
    f,a,coordinator,consumer,emit,read=wire
    emit(5.0);emit(4.9,provenance_changes={'connection_generation':2})
    captured,decision=read();before=state(consumer)
    assert consumer.apply_read(captured,decision_id=decision).reason=='epoch_change_requires_new_segment'
    assert state(consumer)==before


def test_reference_capacity_failure_can_retry_without_losing_provider_keys(wire):
    f,a,coordinator,consumer,emit,read=wire
    consumer.prefix.limits=a.Limits(100,100,1)
    for price in [5.0,5.1,5.0,5.1]: emit(price)
    captured,decision=read();before=state(consumer)
    assert consumer.apply_read(captured,decision_id=decision).reason=='resource_capacity_unresolved'
    assert state(consumer)==before
    consumer.prefix.limits=a.Limits(100,100,100)
    assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    assert consumer.prefix.count==4 and len(consumer._provider_keys)==4


def test_provider_identity_matches_bridge_date_time_id_market_key(wire):
    f,a,coordinator,consumer,emit,read=wire
    emit(5.0);captured,decision=read()
    assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    emit(4.9,provenance_changes={'provider_tick_id':'10001','trade_market_center':'P'})
    captured,decision=read()
    assert consumer.apply_read(captured,decision_id=decision).status=='applied'
    assert consumer.witness(0).provider_identity!=consumer.witness(1).provider_identity
