"""PAPER host assembly, durable SQL pipeline and lifecycle shutdown boundaries."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.crypto_execution.host import NativeRuntime,HostLifecycle,NativePaperHost,CurrentRunSource
from app.crypto_execution.host_config import accepted_receipt,load_config
from app.crypto_execution.paper_http import PaperCycleHTTP
from app.crypto_execution.store import NativeCycleStore
from tests.test_native_crypto_cycle_store import store,admission,reserve
from tests.test_native_crypto_owner import Broker,Response
from tests.test_native_crypto_selection import context,FEES,add
from tests.test_native_crypto_lifecycle import ACCOUNT,CYCLE,ASSET


class Source:
    def __init__(self,views):self.views=views;self.valid=True
    def current_views(self):
        if not self.valid:raise ValueError('native_source_current_observation_unavailable')
        return self.views


def test_restart_history_is_unavailable_until_current_host_observation():
    retained=Source(('retained',));source=CurrentRunSource(retained)
    with pytest.raises(ValueError,match='current_observation_unavailable'):source.current_views()
    source.observed.set()
    assert source.current_views()==('retained',)
    retained.valid=False
    with pytest.raises(ValueError,match='current_observation_unavailable'):source.current_views()


def runtime(store,source,assets,record=lambda _:None,external=None):
    broker=PaperCycleHTTP(paper=True,account_id=ACCOUNT,key='fixture-key-never-real',secret='fixture-secret-never-real',
        require_authority=lambda _:None,timeout_seconds=1,max_response_bytes=65536)
    broker._opener=external or Broker()
    return NativeRuntime(store=store,broker=broker,admission=admission,source=source,assets=assets,fees=FEES,record=record)


def test_all_eligible_assets_are_reserved_together_and_republication_is_idempotent(store):
    b,assets=context();v=b.view('BTC/USD')
    views=(v,replace(v,symbol=assets[1]['symbol'],asset_id=assets[1]['id']))
    evidence=[];r=runtime(store,Source(views),assets,record=evidence.append)
    result=r.select()
    assert result['eligible']==result['created']==2
    assert len(store.open_cycle_ids())==2
    assert r.select()['created']==0
    assert evidence[0]['kind']=='selection' and len(evidence[0]['assessments'])==2


def test_failed_selection_evidence_write_never_reserves(store):
    b,assets=context()
    def fail(_):raise OSError('fixture disk failure')
    r=runtime(store,Source(tuple(b.view(s) for s in b.assets)),assets,record=fail)
    with pytest.raises(OSError):r.select()
    assert store.open_cycle_ids()==[]


def test_runtime_sells_whole_balance_and_reconciles_when_source_is_down(store):
    b,assets=context();source=Source(tuple(b.view(s) for s in b.assets));external=Broker()
    r=runtime(store,source,assets,external=external)
    assert r.select()['created']==1
    ids=store.open_cycle_ids();assert len(ids)==1
    assert r.reconcile()['outcomes'][ids[0]]['outcome']=='entry_observed'
    source.valid=False
    assert r.reconcile()['outcomes'][ids[0]]['outcome']=='position_reconciled'
    assert external.balance>0
    add(b,70);source.views=tuple(b.view(s) for s in b.assets);source.valid=True
    for _ in range(4):r.reconcile()
    assert store.open_cycle_ids()==[]
    assert external.sells==['0.0000399','0.0000199']


def test_one_cycle_error_does_not_skip_other_owned_cycles(store):
    b,assets=context();v=b.view('BTC/USD')
    r=runtime(store,Source((v,replace(v,symbol=assets[1]['symbol'],asset_id=assets[1]['id']))),assets)
    r.select();ids=store.open_cycle_ids();visited=[]
    def step(cid):
        visited.append(cid)
        if cid==ids[0]:raise TimeoutError('fixture provider failure')
        return dict(outcome='visited')
    r.owner.step=step
    status=r.reconcile()
    assert visited==ids and status['errors']=={ids[0]:'TimeoutError'} and ids[1] in status['outcomes']


def test_open_cycle_inventory_recovers_preexisting_reservation(store):
    reserve(store)
    recovered=NativeCycleStore(store.engine,account_id=ACCOUNT,schema=store.cycles.split('.')[0],
        max_event_bytes=store.max_bytes,lock_timeout_ms=store.lock_timeout_ms)
    assert recovered.open_cycle_ids()==[CYCLE]


def config(tmp_path):
    from tests.test_native_crypto_source_capture import metadata
    fee=tmp_path/'fees.json';fee.write_text(json.dumps(FEES))
    return dict(contract='native_paper_host_v1',directory=str(tmp_path/'host'),receipt_log=str(tmp_path/'launcher.log'),
        receipt_directory=str(tmp_path),supervisor_path=str(tmp_path/'supervisor.py'),env_path=str(tmp_path/'.env'),
        fee_evidence_path=str(fee),fee_evidence_sha256=hashlib.sha256(fee.read_bytes()).hexdigest(),
        location='us',source_resources=metadata()['resources'],poll_seconds=.01,reconcile_seconds=.01,
        broker_min_interval_seconds=.001,http_timeout_seconds=1,max_http_bytes=65536,
        max_receipt_bytes=65536,max_log_bytes=65536,max_event_bytes=131072,lock_timeout_ms=1000,
        max_admission_journal_bytes=1048576,max_order_pages=5,max_host_journal_bytes=1048576)


def settings(path=''):
    return SimpleNamespace(chili_scheduler_role='momentum_exec_only',chili_alpaca_paper=True,
        chili_alpaca_enabled=True,chili_momentum_crypto_execution_via_alpaca_paper=True,
        chili_momentum_native_crypto_host_config_path=path,chili_alpaca_expected_account_id=ACCOUNT,
        chili_alpaca_api_key='fixture-key-never-real',chili_alpaca_api_secret='fixture-secret-never-real',
        chili_momentum_risk_loss_fraction_of_equity=.03)


def test_config_and_receipt_require_exact_schema_and_bound_directory(tmp_path):
    c=config(tmp_path);path=tmp_path/'config.json';path.write_text(json.dumps(c))
    assert load_config(str(path))[0]==c
    receipt=tmp_path/'receipt.json';receipt.write_text('{}')
    Path(c['receipt_log']).write_text('ACCEPTED receipt: '+str(receipt)+' sha='+'a'*64+' clean=True\n')
    assert accepted_receipt(c)==(receipt.resolve(),'a'*64)
    c['receipt_directory']=str(tmp_path/'elsewhere');Path(c['receipt_directory']).mkdir()
    with pytest.raises(ValueError,match='directory_changed'):accepted_receipt(c)
    c['unexpected_enable']=True;path.write_text(json.dumps(c))
    with pytest.raises(ValueError,match='contract_invalid'):load_config(str(path))


def test_shutdown_before_deferred_startup_prevents_host_creation(monkeypatch):
    monkeypatch.delenv('CHILI_PYTEST',raising=False)
    life=HostLifecycle();life.close();life.start(None,settings('does-not-exist'))
    assert life.host is None and life.status()['state']=='stopped'


def test_pytest_and_nonpaper_scope_never_start_transport(monkeypatch):
    life=HostLifecycle();life.start(None,settings('nonexistent'))
    assert life.host is None
    monkeypatch.delenv('CHILI_PYTEST',raising=False)
    s=settings('nonexistent');s.chili_alpaca_paper=False
    life=HostLifecycle();life.start(None,s)
    assert life.host is None and life.status()['error']=='native_host_paper_execution_scope_required'


def test_production_bootstrap_assembles_real_store_source_admission_owner_without_import_side_effects(store,tmp_path,monkeypatch):
    import app.crypto_execution.host as module
    c=config(tmp_path);calls=[]
    class InventoryBroker:
        def open(self,request,timeout):
            calls.append(request.full_url)
            body=dict(id=ACCOUNT) if request.full_url.endswith('/account') else [ASSET]
            return Response(200,json.dumps(body).encode())
    original=PaperCycleHTTP
    def client(**kwargs):
        b=original(**kwargs);b._opener=InventoryBroker();return b
    monkeypatch.setattr(module,'PaperCycleHTTP',client)
    monkeypatch.setattr(module,'accepted_receipt',lambda _: (tmp_path/'receipt.json','a'*64))
    # Only the external process/lease boundary is simulated. Runtime components
    # below use real PAPER HTTP validation, SQL schema and durable source files.
    monkeypatch.setattr(module,'PaperWindowAuthority',lambda *a,**k:lambda account:None)
    monkeypatch.setattr(module,'NativeCycleStore',lambda *a,**k:store)
    host=NativePaperHost(store.engine,settings(),c,'b'*64)
    seen=[]
    def source_loop():
        seen.append(host.runtime)
        host.stop.set()
    monkeypatch.setattr(host,'_source_loop',source_loop)
    host.start();host.threads[0].join(5)
    assert not host.threads[0].is_alive()
    assert len(seen)==1 and isinstance(seen[0],NativeRuntime)
    assert host.source.metadata['observation_mode']=='revisable_prefix'
    assert host.source.metadata['assets']==[ASSET]
    assert calls==['https://paper-api.alpaca.markets/v2/account',
                   'https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=crypto']
    assert host.status()['state']=='stopped' and not host.status()['order_authority']


def test_native_host_configuration_excludes_legacy_crypto_producer(monkeypatch):
    from app.config import settings as configured
    from app.services.trading.execution_family_registry import resolve_execution_family_for_symbol,ExecutionFamilyRoutingError
    monkeypatch.setattr(configured,'chili_momentum_native_crypto_host_config_path','fixture-config')
    with pytest.raises(ExecutionFamilyRoutingError,match='native_paper_crypto_host_owns_admission'):
        resolve_execution_family_for_symbol('BTC/USD',mode='live')


def test_broker_rate_headers_are_retained_before_shared_host_backoff(tmp_path):
    from app.crypto_execution.host import HostBroker
    c=config(tmp_path);host=NativePaperHost(None,settings(),c,'b'*64)
    events=[]
    class Limited:
        def open(self,request,timeout):
            response=Response(429,b'{"message":"rate limited"}')
            response.headers={'Retry-After':'20','X-RateLimit-Remaining':'0','Unrelated-Header':'omitted'}
            return response
    client=PaperCycleHTTP(paper=True,account_id=ACCOUNT,key='fixture-key-never-real',secret='fixture-secret-never-real',
        require_authority=lambda _:None,timeout_seconds=1,max_response_bytes=65536)
    client._opener=Limited()
    broker=HostBroker(client,host._broker_rate)
    response=broker.request('GET','/v2/account',None,record=events.append,before_transport=lambda:None)
    assert response.status==429 and response.rate_headers=={'retry-after':'20','x-ratelimit-remaining':'0'}
    assert events[-1]['rate_headers']==response.rate_headers
    assert host.status()['broker_rate']['state']=='provider_backoff' and not host.broker_rate_unavailable
    host._broker_rate(429,{})
    with pytest.raises(ValueError,match='rate_reset_missing'):host._pace_broker()
