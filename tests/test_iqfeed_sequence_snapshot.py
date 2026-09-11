"""Accepted capture sequence inventory: real lifecycle, forbidden DB access."""
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys

import pytest


REPO=Path(__file__).resolve().parents[1]


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setenv('CHILI_PYTEST','1')
    for name in ('DATABASE_URL','TEST_DATABASE_URL'):
        monkeypatch.setenv(name,'postgresql://chili:chili@localhost:5433/chili_rossbench26_test')
    import psycopg2
    def forbid(*args,**kwargs): raise AssertionError('No DB access in sequence snapshot tests')
    monkeypatch.setattr(psycopg2,'connect',forbid)
    monkeypatch.syspath_prepend(str(REPO))
    spec=importlib.util.spec_from_file_location('lifecycle_for_sequence_snapshot',REPO/'tests/test_replay_capture_producer_lifecycle.py')
    f=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=f
    spec.loader.exec_module(f)
    from app.services.trading.momentum_neural import replay_capture_contract as contract
    from app.services.trading.momentum_neural import structural_tape_prefix as m
    identity,binding=f._identity(),f._resource_binding()
    producer=f._producer(identity,binding,streams=(f.CaptureStream.IQFEED_PRINT,))
    clock=f._ManualClock(f.BASE)
    runtime=f.CaptureProducerLifecycleRuntime(identity=identity,
        ingress=f.BoundedCaptureIngress.from_resource_binding(binding),resource_binding=binding,
        producers=(producer,),heartbeat_timeout_seconds=1,wall_clock=clock)
    opened=runtime.open(opened_at=f.BASE)
    registered=runtime.register(producer.producer_id,recorded_at=clock.set(f.BASE+timedelta(microseconds=1000)))

    def emit(provider_us,price,*,symbol='VEEE',recorded_us=3000):
        received=f.BASE+timedelta(microseconds=1500)
        return runtime.submit_input(producer.producer_id,stream=f.CaptureStream.IQFEED_PRINT,
            provider='iqfeed',symbol=symbol,
            clocks=f.CaptureClocks(provider_event_at=None if provider_us is None else f.BASE+timedelta(microseconds=provider_us),
                received_at=received,available_at=received),
            payload={'schema_version':f.IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,'symbol':symbol,
                'price':price,'size':100,'bid':price-.01,'ask':price,'conditions':['fixture-only']},
            recorded_at=clock.set(f.BASE+timedelta(microseconds=recorded_us)))

    return f,contract,m,runtime,producer,clock,opened,registered,emit


def test_sequence_inventory_includes_late_tick_hidden_by_next_event_interval(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    first=emit(1300,5.0)
    late=emit(1200,4.9)
    now=clock.set(f.BASE+timedelta(microseconds=4000))
    receipt_event,receipt,window=runtime.submit_microstructure_window_receipt(
        decision_id='sequence-snapshot-comparison',operation=c.CaptureMicrostructureOperation.TRADE_FLOW,
        stream=f.CaptureStream.IQFEED_PRINT,provider='iqfeed',symbol='VEEE',requested_at=now,returned_at=now,
        event_start_exclusive=first.clocks.provider_event_at,event_end_inclusive=now,
        parameters={'window_seconds':(now-first.clocks.provider_event_at).total_seconds()})
    assert window==() and receipt.empty_result
    assert receipt.query['source_frontier_sequence']==late.sequence
    snapshot=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=first.sequence)
    assert snapshot.source_events==(late,)
    assert snapshot.through_sequence==receipt_event.sequence
    assert snapshot.available_at==now
    assert snapshot.prefix_root_sha256==runtime._current_prefix_root()
    # The old window is correct for its requested event interval, but cannot
    # serve as the next captured sequence delta.


def test_complete_symbol_delta_root_includes_other_symbols_and_control(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    events=[opened,registered]
    first=emit(1100,5.0);events.append(first)
    other=emit(1150,8.0,symbol='TNON');events.append(other)
    second=emit(1200,4.9);events.append(second)
    before=runtime._sequence
    snapshot=runtime.snapshot_iqfeed_sequence_delta(symbol=' veee ',after_sequence=opened.sequence)
    assert snapshot.source_events==(first,second)
    assert snapshot.through_sequence==second.sequence and snapshot.symbol=='VEEE'
    assert snapshot.prefix_root_sha256==c.capture_prefix_root_sha256(
        tuple(c.CaptureEventRef.from_event(e) for e in events),
        identity_sha256=runtime.identity.identity_sha256,through_sequence=second.sequence)
    assert runtime._sequence==before  # No hidden READ_RECEIPT or other mutation.


def test_same_clock_snapshot_advances_and_prior_tuple_stays_immutable(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    first=emit(1100,5.0)
    a=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=registered.sequence)
    second=emit(1200,4.9)
    b=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=a.through_sequence)
    assert a.source_events==(first,) and b.source_events==(second,)
    assert a.available_at==b.available_at and a.prefix_root_sha256!=b.prefix_root_sha256
    assert a.through_sequence<b.through_sequence


def test_empty_symbol_delta_still_names_global_boundary(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    other=emit(1100,5.0,symbol='TNON')
    snapshot=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=registered.sequence)
    assert snapshot.source_events==() and snapshot.through_sequence==other.sequence
    repeated=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=other.sequence)
    assert repeated.source_events==() and repeated.prefix_root_sha256==snapshot.prefix_root_sha256


def test_evicted_late_provider_tick_cannot_disappear_from_sequence_delta(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    # Exercise the real source-index eviction path at a test resource capacity.
    runtime._recent_source_event_limit=1
    first=emit(1300,5.0)
    late=emit(1200,4.9)
    last=emit(1400,5.1)
    with pytest.raises(c.CaptureContractError,match='iqfeed_sequence_source_index_eviction'):
        runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=first.sequence)
    assert runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=late.sequence).source_events==(last,)


def test_other_symbol_eviction_does_not_claim_target_data_was_lost(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    runtime._recent_source_event_limit=1
    emit(1100,5.0,symbol='TNON')
    target=emit(1200,4.9)
    assert runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=registered.sequence).source_events==(target,)


@pytest.mark.parametrize('anchor',[0,-1,True,1.5,999])
def test_invalid_or_future_anchor_is_rejected(capture,anchor):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    with pytest.raises(c.CaptureContractError,match='iqfeed_sequence_'):
        runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=anchor)


@pytest.mark.parametrize('global_gap',[False,True])
def test_reported_gap_prevents_claiming_complete_target_data(capture,global_gap):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    first=emit(1100,5.0)
    now=clock.set(f.BASE+timedelta(microseconds=4000))
    runtime.report_gap(producer.producer_id,
        stream=f.CaptureStream.COVERAGE_GAP if global_gap else f.CaptureStream.IQFEED_PRINT,
        symbol=None if global_gap else 'VEEE',reason='fixture_missing_input',first_available_at=now,last_available_at=now,lost_count=1,
        recorded_at=now)
    with pytest.raises(c.CaptureContractError,match='iqfeed_sequence_reported_coverage_gap'):
        runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=first.sequence)


def test_latched_failure_prevents_snapshot_and_does_not_mutate_sequence(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    before=runtime._sequence
    runtime._latch_failure('fixture_ingress_failure')
    with pytest.raises(c.CaptureContractError,match='iqfeed_sequence_capture_submission_failed'):
        runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=registered.sequence)
    assert runtime._sequence==before


def test_snapshot_feeds_consumer_and_late_tick_fails_without_silent_filter(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    anchor_root=runtime._current_prefix_root()
    prefix=m.Prefix('VEEE','sequence-adapter-test',m.Limits(20,20,20))
    after=registered.sequence

    def apply():
        nonlocal after,anchor_root
        snapshot=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=after)
        def ns(value):
            delta=value-f.BASE
            return (delta.days*86400+delta.seconds)*10**9+delta.microseconds*1000
        rows=[]
        for event in snapshot.source_events:
            value=c.CaptureIqfeedPrint.from_event(event)
            rows.append(m.Tick(event.sequence,value.price,value.size,value.bid,value.ask,
                ns(event.clocks.provider_event_at),ns(event.clocks.received_at),ns(event.clocks.available_at),
                (snapshot.identity_sha256,)))
        receipt=m.ConsumerFrontierReceipt(ns(snapshot.available_at),len(rows),m.rows_sha256(rows),
            prefix.prefix_sha256,snapshot.identity_sha256,snapshot.through_sequence,snapshot.prefix_root_sha256,
            after,anchor_root)
        result=prefix.append_frontier(rows,receipt)
        if result.status=='applied':
            after,anchor_root=snapshot.through_sequence,snapshot.prefix_root_sha256
        return result

    for provider_us,price in [(1100,5),(1200,4.9),(1300,5.1)]:
        emit(provider_us,price)
        assert apply().status=='applied'
    assert prefix.count==3 and prefix.active_references()[0].kind=='valley'
    before=(prefix.prefix_sha256,prefix.last_receipt,after,anchor_root)
    emit(1250,5.0)
    assert apply().reason=='late_or_duplicate_tick'
    assert (prefix.prefix_sha256,prefix.last_receipt,after,anchor_root)==before


@pytest.mark.parametrize('provider_us',[None,5000])
def test_unknown_or_future_provider_clock_is_visible_not_filtered(capture,provider_us):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    event=emit(provider_us,5.0)
    snapshot=runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=registered.sequence)
    assert snapshot.source_events==(event,)
    assert event.clocks.provider_event_at is None or event.clocks.provider_event_at>snapshot.available_at


def test_another_symbol_gap_is_not_target_completeness_evidence(capture):
    f,c,m,runtime,producer,clock,opened,registered,emit=capture
    event=emit(1100,5.0)
    now=clock.set(f.BASE+timedelta(microseconds=4000))
    runtime.report_gap(producer.producer_id,stream=f.CaptureStream.IQFEED_PRINT,
        symbol='TNON',reason='fixture_other_symbol_gap',first_available_at=now,last_available_at=now,
        lost_count=1,recorded_at=now)
    assert runtime.snapshot_iqfeed_sequence_delta(symbol='VEEE',after_sequence=registered.sequence).source_events==(event,)
