from contextlib import ExitStack
from types import SimpleNamespace
import json
import time
import pytest
from app.crypto_execution.grouped_host_source import GroupedHostSource
from app.crypto_execution.host import NativePaperHost,CurrentRunSource
from app.crypto_execution.host_config import load_config
from scripts.crypto_history_pages import canonical,sha
from scripts.crypto_trade_frames import timestamp_ns
from tests.test_native_crypto_source_capture import getter,metadata
from tests.test_native_crypto_host import config,settings
from tests.test_native_crypto_cycle_store import store
from tests.test_native_crypto_lifecycle import ASSET,ACCOUNT
from app.crypto_execution.source_capture import NativeSourceCapture
from tests.test_native_crypto_history_source import BASE,t,q


def transition(tmp_path):
    legacy=tmp_path/'source';legacy.mkdir();m=metadata();m['observation_mode']='revisable_prefix'
    m['assets']=[dict(ASSET,id=a['id'],symbol=a['symbol']) for a in m['assets']]
    original=NativeSourceCapture(legacy,m,clock_ns=lambda:BASE+100000)
    get,_=getter(trade_rows=[t(90)],quote_rows=[q(80)]);original.observe(BASE+100,get)
    get,_=getter(trade_rows=[t(85,tid=2),t(90),t(150,tid=3)],quote_rows=[q(80),q(140)])
    original.observe(BASE+200,get)
    seal=dict(metadata_sha256=sha((legacy/'metadata.json').read_bytes()),record_root_sha256=original.root,
        record_count=original.record_count,journal_bytes=original.byte_count,revision=original.revision)
    manifest=dict(contract='native_grouped_source_transition_v1',legacy_directory=str(legacy),
        grouped_directory=str(tmp_path/'source-grouped-v1'),seed_origin=seal)
    path=tmp_path/'transition.json';path.write_text(canonical(manifest))
    c=config(tmp_path);c.update(directory=str(tmp_path),grouped_source_manifest_path=str(path),grouped_source_manifest_sha256=sha(path.read_bytes()))
    return original,c,path


def test_grouped_host_recovery_preserves_legacy_and_requires_current_run_observation(tmp_path):
    original,c,path=transition(tmp_path);before=(tmp_path/'source/source.jsonl').read_bytes()
    with ExitStack() as stack:
        source=GroupedHostSource(tmp_path,c,original.metadata,stack);current=CurrentRunSource(source)
        with pytest.raises(ValueError,match='unavailable'):current.current_views()
        get,_=getter(trade_rows=[t(85,tid=2),t(90),t(150,tid=3),t(250,'12',tid=4)],quote_rows=[q(80),q(140)])
        source.observe(BASE+300,get);current.observed.set()
        assert current.current_views()[0].print_count==4
        value=source.status();assert value['legacy_seed_revision']==2 and value['grouped_revision']==1
        assert value['revision']==3 and value['requested_symbol_count']==2
        views=source.current_views()
    assert (tmp_path/'source/source.jsonl').read_bytes()==before
    with ExitStack() as stack:
        source=GroupedHostSource(tmp_path,c,original.metadata,stack)
        assert source.current_views()==views
        with pytest.raises(ValueError,match='unavailable'):CurrentRunSource(source).current_views()


@pytest.mark.parametrize('fault',['hash','outside','unsealed'])
def test_host_transition_rejects_changed_origin_or_unsealed_newer_history(tmp_path,fault):
    original,c,path=transition(tmp_path)
    if fault=='hash':c['grouped_source_manifest_sha256']='f'*64
    elif fault=='outside':
        m=json.loads(path.read_bytes());m['grouped_directory']=str(tmp_path.parent/'outside')
        path.write_text(canonical(m));c['grouped_source_manifest_sha256']=sha(path.read_bytes())
    else:
        with (tmp_path/'source/source.jsonl').open('ab') as f:f.write(b'unsealed newer evidence')
    with ExitStack() as stack:
        with pytest.raises(ValueError):GroupedHostSource(tmp_path,c,original.metadata,stack)


def test_host_config_requires_both_manifest_path_and_digest(tmp_path):
    original,c,path=transition(tmp_path);cfg=tmp_path/'config.json';cfg.write_text(canonical(c))
    assert load_config(str(cfg))[0]==c
    del c['grouped_source_manifest_sha256'];cfg.write_text(canonical(c))
    with pytest.raises(ValueError,match='contract_invalid'):load_config(str(cfg))


def test_rate_reset_survives_collectors_exhaustion_exception(tmp_path,monkeypatch):
    host=NativePaperHost(None,None,config(tmp_path),'b'*64)
    monkeypatch.setattr(host,'_before_data_read',lambda:None)
    def get(**kwargs):
        kwargs['record'](dict(status=200,rate_headers={'x-ratelimit-remaining':'0','retry-after':'20'}))
    host.data=SimpleNamespace(get=get)
    def refuse(_):raise ValueError('native_frontier_provider_rate_exhausted')
    with pytest.raises(ValueError):host._get_data(record=refuse)
    assert host.next_data>time.time() and not host.data_rate_unavailable
    waits=[];host.stop=SimpleNamespace(wait=lambda value:waits.append(value) or False)
    assert host._wait_data_rate(ValueError('native_frontier_provider_rate_exhausted'))
    assert 0<waits[0]<=20
    assert not host._wait_data_rate(ValueError('native_frontier_retained_chain_changed'))


def test_missing_reset_still_retains_the_actual_http_evidence(tmp_path,monkeypatch):
    host=NativePaperHost(None,None,config(tmp_path),'b'*64)
    monkeypatch.setattr(host,'_before_data_read',lambda:None);retained=[]
    value=dict(status=429,rate_headers={})
    host.data=SimpleNamespace(get=lambda **kwargs:kwargs['record'](value))
    with pytest.raises(ValueError,match='rate_reset_missing'):host._get_data(record=retained.append)
    assert retained==[value] and host.data_rate_unavailable


def test_already_elapsed_verified_backoff_can_resume_without_new_invented_delay(tmp_path):
    host=NativePaperHost(None,None,config(tmp_path),'b'*64);host.next_data=time.time()-1
    waits=[];host.stop=SimpleNamespace(wait=lambda value:waits.append(value) or False)
    assert host._wait_data_rate(ValueError('native_frontier_provider_rate_exhausted'))
    assert waits==[0]


def test_actual_host_assembly_starts_independent_audit_worker_and_closes_source(store,tmp_path,monkeypatch):
    import threading
    import app.crypto_execution.host as module
    from tests.test_native_crypto_owner import Response
    original,c,path=transition(tmp_path);calls=[]
    class Inventory:
        def open(self,request,timeout):
            calls.append(request.full_url)
            value=dict(id=ACCOUNT) if request.full_url.endswith('/account') else original.metadata['assets']
            return Response(200,json.dumps(value).encode())
    real_client=module.PaperCycleHTTP
    def client(**kwargs):
        result=real_client(**kwargs);result._opener=Inventory();return result
    monkeypatch.setattr(module,'PaperCycleHTTP',client)
    monkeypatch.setattr(module,'accepted_receipt',lambda _:(tmp_path/'accepted.json','a'*64))
    monkeypatch.setattr(module,'PaperWindowAuthority',lambda *a,**kw:lambda account:None)
    monkeypatch.setattr(module,'NativeCycleStore',lambda *a,**kw:store)
    host=NativePaperHost(store.engine,settings(),c,'b'*64);barrier=threading.Barrier(4);visited=[]
    def worker():
        visited.append(threading.current_thread().name)
        barrier.wait(timeout=10);host.stop.set()
    for method in ('_source_loop','_selection_loop','_cycles_loop','_audit_loop'):monkeypatch.setattr(host,method,worker)
    host.start();host.threads[0].join(15)
    assert not host.threads[0].is_alive()
    assert set(visited)=={'chili-native-source','chili-native-selection','chili-native-cycles','chili-native-source-audit'}
    assert isinstance(host.source,GroupedHostSource) and host.source.capture.closed
    assert host.status()['state']=='stopped' and not host.status()['order_authority']
    assert len(calls)==2 and all(url.startswith('https://paper-api.alpaca.markets/v2/') for url in calls)
