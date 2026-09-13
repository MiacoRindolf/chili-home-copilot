"""Physical source-return clocks must not masquerade as tick signal time."""
from types import SimpleNamespace
import pytest
import app.crypto_execution.host as module
from tests.test_native_crypto_host import config


def host(tmp_path):
    h=module.NativePaperHost(None,None,config(tmp_path),'b'*64)
    h.source=SimpleNamespace(status=lambda:dict(revision=7,source_end_ns=90,valid=True))
    return h


def test_return_measurement_follows_source_commit_and_preserves_query_clock(tmp_path,monkeypatch):
    h=host(tmp_path);steps=[]
    walls=iter((100,180));monotonic=iter((1000,1050))
    monkeypatch.setattr(module.time,'time_ns',lambda:next(walls))
    monkeypatch.setattr(module.time,'perf_counter_ns',lambda:next(monotonic))
    def update():steps.append('source_committed');return {'revision':7}
    def record(value):
        assert steps==['source_committed']
        steps.append('timing_retained')
        assert value['requested_through_ns']==90
        assert value['returned_ns']==180 and value['elapsed_ns']==50
        assert not value['broker_fill_latency_measured']
    h._record=record
    assert h._measure_source_update('fast_observation',update)=={'revision':7}
    assert steps==['source_committed','timing_retained']
    assert h.status()['source_timings']['fast_observation']['source_revision']==7


def test_wall_clock_adjustment_does_not_turn_into_negative_compute_duration(tmp_path,monkeypatch):
    h=host(tmp_path);walls=iter((100,80));monotonic=iter((1000,1017));records=[]
    monkeypatch.setattr(module.time,'time_ns',lambda:next(walls))
    monkeypatch.setattr(module.time,'perf_counter_ns',lambda:next(monotonic))
    h._record=records.append
    h._measure_source_update('audit_publication',lambda:True)
    assert records[0]['elapsed_ns']==17
    assert records[0]['started_ns']==100 and records[0]['returned_ns']==80


def test_empty_pending_audit_has_no_phantom_publication_timing(tmp_path):
    h=host(tmp_path);records=[];h._record=records.append
    assert h._measure_source_update('audit_publication',lambda:False) is False
    assert not records and 'source_timings' not in h.status()


def test_source_failure_does_not_report_a_successful_committed_update(tmp_path):
    h=host(tmp_path);records=[];h._record=records.append
    def fail():raise ValueError('native_fixture_source_failure')
    with pytest.raises(ValueError):h._measure_source_update('fast_observation',fail)
    assert not records and 'source_timings' not in h.status()


def test_failed_timing_retention_does_not_announce_readiness(tmp_path):
    h=host(tmp_path);h.current_source=module.CurrentRunSource(h.source)
    h.source.observe=lambda *args:None
    def record(value):raise OSError('fixture journal unavailable')
    h._record=record;h._get_data=lambda **kwargs:None
    h._source_loop()
    assert h.status()['state']=='degraded_source'
    assert not h.current_source.observed.is_set() and not h.published.is_set()
