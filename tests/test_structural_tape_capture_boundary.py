"""Actual capture lifecycle integration; no database or active lane access."""
from dataclasses import replace
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_actual_capture_equal_clock_prefixes_preserve_sequence_and_wave(monkeypatch):
    # Runtime imports require a configured URL; any attempted connection fails.
    # This test never creates a schema or touches a configured database.
    monkeypatch.setenv('CHILI_PYTEST', '1')
    for name in ('DATABASE_URL', 'TEST_DATABASE_URL'):
        monkeypatch.setenv(name, 'postgresql://chili:chili@localhost:5433/chili_rossbench26_test')
    import psycopg2

    def forbid_connection(*args, **kwargs):
        raise AssertionError('Capture boundary integration forbids DB connections')

    monkeypatch.setattr(psycopg2, 'connect', forbid_connection)
    monkeypatch.syspath_prepend(str(REPO))
    f = load('capture_fixture_for_structural_boundary', REPO/'tests/test_replay_capture_producer_lifecycle.py')
    m = load('structural_prefix_for_capture_boundary', REPO/'app/services/trading/momentum_neural/structural_tape_prefix.py')
    from app.services.trading.momentum_neural.replay_capture_contract import CaptureIqfeedPrint

    identity = f._identity()
    binding = f._resource_binding()
    producer = f._producer(identity, binding, streams=(f.CaptureStream.IQFEED_PRINT,))
    clock = f._ManualClock(f.BASE)
    runtime = f.CaptureProducerLifecycleRuntime(identity=identity,
        ingress=f.BoundedCaptureIngress.from_resource_binding(binding), resource_binding=binding,
        producers=(producer,), heartbeat_timeout_seconds=1, wall_clock=clock)
    runtime.open(opened_at=f.BASE)
    runtime.register(producer.producer_id, recorded_at=clock.set(f.BASE+timedelta(milliseconds=1)))
    # Inspect the real lifecycle root for this mechanics test. This is not an
    # external authenticated reader or permission to construct live order proof.
    anchor_sequence, anchor_root = runtime._sequence, runtime._current_prefix_root()
    returned = clock.set(f.BASE+timedelta(milliseconds=2))
    original_available = f.BASE+timedelta(microseconds=1500)
    observations = []
    for i, price in enumerate((5.0, 4.9, 5.1)):
        event = runtime.submit_input(producer.producer_id, stream=f.CaptureStream.IQFEED_PRINT,
            provider='iqfeed', symbol='VEEE', clocks=f.CaptureClocks(
                provider_event_at=f.BASE+timedelta(microseconds=1100+i*100),
                received_at=original_available, available_at=original_available),
            payload={'schema_version':f.IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION, 'symbol':'VEEE',
                'price':price, 'size':100, 'bid':price-.01, 'ask':price, 'conditions':['fixture-only']},
            recorded_at=returned)
        observations.append((event, runtime._current_prefix_root()))

    def ns(value):
        delta = value-f.BASE
        return (delta.days*86400+delta.seconds)*10**9+delta.microseconds*1000

    consumer = m.Prefix('VEEE', 'capture-test', m.Limits(10,10,10))
    recorded = m.Prefix('VEEE', 'capture-test', m.Limits(10,10,10))
    for index, (event, root) in enumerate(observations):
        value = CaptureIqfeedPrint.from_event(event)
        row = m.Tick(event.sequence, value.price, value.size, value.bid, value.ask,
            ns(event.clocks.provider_event_at), ns(event.clocks.received_at),
            ns(event.clocks.available_at), (identity.identity_sha256,))
        receipt = m.ConsumerFrontierReceipt(row.known_ns, 1, m.rows_sha256([row]),
            consumer.prefix_sha256, identity.identity_sha256, event.sequence, root,
            anchor_sequence, anchor_root)
        assert consumer.append_frontier([row], receipt).status == 'applied'
        assert consumer.last_receipt.source_root_sha256 == root
        anchor_sequence, anchor_root = event.sequence, root
        # Ordinary recorded timestamps still cannot identify separate reads.
        ordinary = m.FrontierReceipt(row.known_ns, 1, m.rows_sha256([row]), recorded.prefix_sha256)
        result = recorded.append_frontier([row], ordinary)
        assert result.status == ('applied' if index == 0 else 'unresolved')
        if index:
            assert result.reason == 'late_or_conflicting_frontier'
        assert event.clocks.available_at == returned
        assert event.payload['_capture_release']['original_available_at'].endswith('00.001500Z')
        assert row.published_ns == ns(returned)

    assert consumer.count == 3 and recorded.count == 1
    refs = consumer.active_references()
    assert [(r.kind, r.origin_id, r.confirmation_id) for r in refs] == [
        ('valley', observations[1][0].sequence, observations[2][0].sequence)]
    assert consumer.last_receipt.source_root_sha256 == runtime._current_prefix_root()
    before = consumer.prefix_sha256
    bad = replace(consumer.last_receipt, previous_prefix_sha256=before,
        previous_source_sequence=observations[0][0].sequence,
        previous_source_root_sha256=observations[0][1])
    assert consumer.append_frontier([row], bad).reason == 'previous_source_prefix_mismatch'
    assert consumer.prefix_sha256 == before
