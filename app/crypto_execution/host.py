"""Application-owned PAPER native source, all-candidate admission and lifecycle.

The three workers have separate responsibilities: HTTP source acquisition,
atomic account allocation, and every already-owned cycle's reconciliation.
Operational waits never define the signal prefix. No executable endpoint,
launcher, live fallback, synthetic setup or singleton position slot exists.
"""
import copy
from contextlib import ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from .admission import LockedNativeAdmissionReader
from .history_source import CryptoDataHTTP,DataHTTPError
from .host_config import load_config,accepted_receipt,read_json
from .lifecycle import canonical
from .owner import NativeCycleOwner
from .paper_http import PaperCycleHTTP
from .selection import assess_all_native,native_opportunities,NativeTickDecisionReader
from .source_capture import NativeSourceCapture,source_metadata
from .store import NativeCycleStore
from .truth import identity,asset_identity
from .window_authority import PaperWindowAuthority
from scripts.capture_crypto_history_pages import capture_lock
from scripts.capture_native_crypto_source import rate_deadline

LOG=logging.getLogger(__name__)


def error_code(exc):
    value=str(exc)
    if value.startswith(('native_','crypto_')) and all(c.isalnum() or c in '_:' for c in value):return value
    return type(exc).__name__


class NativeRuntime:
    """Concrete pipeline also exercised with SQL and a simulated PAPER transport."""
    def __init__(self,*,store,broker,admission,source,assets,fees,record):
        self.store=store;self.source=source;self.assets=tuple(assets);self.fees=fees;self.record=record
        self.by_id={asset_identity(a)[0]:a for a in assets}
        self.decisions=NativeTickDecisionReader(context_reader=self.context,
            asset_reader=lambda aid:self.by_id[aid],fee_reader=lambda:self.fees,
            record=lambda r:store.record_evidence(r['cycle_id'],r))
        self.owner=NativeCycleOwner(store,broker,admission_reader=admission,decision_reader=self.decisions)
        self.admission=admission

    def context(self,symbol):
        try:views=self.source.current_views()
        except ValueError as exc:
            if str(exc)=='native_source_current_observation_unavailable':return None
            raise
        return next((v for v in views if v.symbol==symbol),None)

    def select(self):
        views=self.source.current_views()
        assessments=assess_all_native(views,self.assets,fee_evidence=self.fees)
        opportunities=native_opportunities(assessments,self.assets,account_id=self.store.account_id)
        # Persist all symbols and their actual parent/local/flow/cost reasons.
        # A failed evidence write cannot leave an unreviewable new reservation.
        self.record(dict(kind='selection',assessments=[a.receipt() for a in assessments],
                         opportunities=opportunities))
        result=self.store.reserve_all(opportunities['candidates'],admission_reader=self.admission) if opportunities['candidates'] else None
        if result is not None:self.record(dict(kind='allocation',result=result))
        reasons={}
        for a in assessments:
            for reason in a.receipt()['reasons']:reasons[reason]=reasons.get(reason,0)+1
        return dict(state='assessed',symbols=len(assessments),eligible=len(opportunities['candidates']),
                    exclusions=len(opportunities['excluded']),reasons=reasons,
                    created=0 if result is None else len(result['created']))

    def reconcile(self,stopping=lambda:False):
        ids=self.store.open_cycle_ids();outcomes={};errors={}
        for cycle_id in ids:
            if stopping():break
            try:outcomes[cycle_id]=self.owner.step(cycle_id)
            except Exception as exc:
                # One unresolved symbol cannot prevent another from being visited.
                # Original durable intent survives; owner decides lookup vs POST.
                errors[cycle_id]=error_code(exc)
        return dict(state='reconciled',active_cycles=len(ids),outcomes=outcomes,errors=errors)


class CurrentRunSource:
    """Recovered history cannot authorize entries before a new host observation."""
    def __init__(self,source):self.source=source;self.observed=threading.Event()
    def current_views(self):
        if not self.observed.is_set():raise ValueError('native_source_current_observation_unavailable')
        return self.source.current_views()


class HostBroker:
    """Keep account-wide native transport pacing outside the strategy rules."""
    def __init__(self,client,observe_rate):
        self.client=client;self.account_id=client.account_id;self.observe_rate=observe_rate
    def request(self,method,path,payload,*,record,before_transport):
        def retained(value):
            record(value)
            if value.get('phase')=='response':self.observe_rate(value['status'],value.get('rate_headers',{}))
        return self.client.request(method,path,payload,record=retained,before_transport=before_transport)


class NativePaperHost:
    def __init__(self,engine,settings,config,config_sha256):
        self.engine=engine;self.settings=settings;self.c=config;self.config_sha=config_sha256
        self.stop=threading.Event();self.published=threading.Event();self.lock=threading.Lock()
        self.journal_lock=threading.Lock();self.broker_lock=threading.Lock()
        self.threads=[];self.runtime=None;self.source=None;self.next_broker=0.;self.next_data=0.
        self.broker_rate_unavailable=False
        self.state=dict(state='starting',config_sha256=config_sha256,paper_only=True,
            order_authority=False,source=None,selection=None,reconciliation=None,
            simultaneous_fills_verified=False,continuous_capacity_verified=False)

    def _set(self,key,value):
        with self.lock:self.state[key]=value

    def status(self):
        with self.lock:
            return dict(copy.deepcopy(self.state),workers_alive=[t.name for t in self.threads if t.is_alive()])

    def start(self):
        t=threading.Thread(target=self._run,name='chili-native-paper-host',daemon=True)
        self.threads.append(t);t.start()

    def close(self):
        self.stop.set();self.published.set()
        # Joining coordinator also waits for its children and releases file lock.
        for t in tuple(self.threads):
            if t is not threading.current_thread():t.join(timeout=self.c['http_timeout_seconds']+1)
        return self.status()

    def _record(self,value):
        raw=(canonical(dict(at_ns=time.time_ns(),**value))+'\n').encode()
        with self.journal_lock:
            if self.journal_bytes+len(raw)>self.c['max_host_journal_bytes']:
                raise ValueError('native_host_journal_capacity')
            with self.journal_path.open('ab') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            self.journal_bytes+=len(raw)

    def _authority(self,account):
        if self.stop.is_set():raise ValueError('native_host_stopping')
        self.authority(account)

    def _pace_broker(self):
        with self.broker_lock:
            if self.broker_rate_unavailable:raise ValueError('native_host_broker_rate_reset_missing')
            if self.stop.wait(max(0,self.next_broker-time.monotonic())):
                raise ValueError('native_host_stopping')
            self.next_broker=time.monotonic()+self.c['broker_min_interval_seconds']

    def _broker_rate(self,status,headers):
        if status!=429 and headers.get('x-ratelimit-remaining')!='0':return
        deadline=rate_deadline(headers,time.time())
        with self.broker_lock:
            if deadline is None:self.broker_rate_unavailable=True
            else:self.next_broker=max(self.next_broker,time.monotonic()+max(0,deadline-time.time()))
        self._set('broker_rate',dict(state='reset_unavailable' if deadline is None else 'provider_backoff',
                                    reset_at=deadline,http_status=status))

    def _broker_read(self,path):
        response=self.broker.request('GET',path,None,record=self._record,before_transport=lambda:None)
        if response.status!=200:raise ValueError('native_host_broker_inventory_unavailable')
        return response.json(),hashlib.sha256(response.body).hexdigest()

    def _run(self):
        try:
            with ExitStack() as stack:
                root=Path(self.c['directory'])
                stack.enter_context(capture_lock(root))
                for name in ('source','admission'): (root/name).mkdir(exist_ok=True)
                self.journal_path=root/'host.jsonl'
                self.journal_bytes=self.journal_path.stat().st_size if self.journal_path.exists() else 0
                receipt,sha=accepted_receipt(self.c)
                account=identity(self.settings.chili_alpaca_expected_account_id)
                self.authority=PaperWindowAuthority(self.engine,account_id=account,receipt_path=receipt,
                    receipt_sha256=sha,supervisor_path=self.c['supervisor_path'],env_path=self.c['env_path'],
                    supervisor_env_path=self.c['supervisor_env_path'],env_sha256=self.c['env_sha256'],
                    code_root=Path(__file__).resolve().parents[2],max_receipt_bytes=self.c['max_receipt_bytes'])
                def require(account_id):
                    self._pace_broker();self._authority(account_id)
                client=PaperCycleHTTP(paper=True,account_id=account,key=self.settings.chili_alpaca_api_key,
                    secret=self.settings.chili_alpaca_api_secret,require_authority=require,
                    timeout_seconds=self.c['http_timeout_seconds'],max_response_bytes=self.c['max_http_bytes'])
                self.broker=HostBroker(client,self._broker_rate)
                observed,_=self._broker_read('/v2/account')
                if identity(observed.get('id'))!=account:raise ValueError('native_host_account_changed')
                rows,inventory_sha=self._broker_read('/v2/assets?status=active&asset_class=crypto')
                if type(rows) is not list:raise ValueError('native_host_inventory_shape')
                assets=[a for a in rows if a.get('class')=='crypto' and a.get('status')=='active' and a.get('tradable') is True]
                # Provider numeric strings remain exact. Reject any lossy metadata.
                for a in assets:asset_identity(a)
                if not assets:raise ValueError('native_host_inventory_empty')
                fees,fee_sha=read_json(self.c['fee_evidence_path'],self.c['max_event_bytes'])
                if fee_sha!=self.c['fee_evidence_sha256']:raise ValueError('native_host_fee_evidence_changed')
                fees=dict(fees,sha256=fee_sha)
                meta_path=root/'source'/'metadata.json'
                if meta_path.exists():
                    metadata,_=read_json(str(meta_path),self.c['max_event_bytes'])
                    old={a['symbol']:a for a in metadata['assets']};new={a['symbol']:a for a in assets}
                    if old!=new or metadata['resources']!=self.c['source_resources'] or metadata['location']!=self.c['location']:
                        raise ValueError('native_host_source_membership_or_resources_changed')
                    if metadata['observation_mode']!='revisable_prefix':raise ValueError('native_host_revisable_source_required')
                else:
                    metadata=source_metadata(assets=assets,location=self.c['location'],source_id=str(uuid4()),
                        anchor_ns=time.time_ns(),inventory_sha256=inventory_sha,page_limit=10000,
                        resources=self.c['source_resources'],observation_mode='revisable_prefix')
                self.source=NativeSourceCapture(root/'source',metadata)
                self.current_source=CurrentRunSource(self.source)
                store=NativeCycleStore(self.engine,account_id=account,max_event_bytes=self.c['max_event_bytes'],
                    lock_timeout_ms=self.c['lock_timeout_ms'])
                store.open_cycle_ids()  # Verify migrated ledger before announcing execution readiness.
                admission=LockedNativeAdmissionReader(self.broker,
                    account_risk_fraction=str(self.settings.chili_momentum_risk_loss_fraction_of_equity),
                    journal_root=root/'admission',max_journal_bytes=self.c['max_admission_journal_bytes'],
                    max_order_pages=self.c['max_order_pages'])
                self.runtime=NativeRuntime(store=store,broker=self.broker,admission=admission,
                    source=self.current_source,assets=assets,fees=fees,record=self._record)
                self.data=CryptoDataHTTP(paper=True,key=self.settings.chili_alpaca_api_key,
                    secret=self.settings.chili_alpaca_api_secret,timeout_seconds=self.c['http_timeout_seconds'],
                    max_response_bytes=self.c['source_resources']['max_page_bytes'])
                self._record(dict(kind='host_bound',config_sha256=self.config_sha,receipt_sha256=sha,
                    account_identity_sha256=hashlib.sha256(account.encode()).hexdigest(),
                    fee_evidence_sha256=fee_sha,asset_count=len(assets),
                    paper_only=True,signal_basis='retained_print_prefix',transport_clocks_are_signal_windows=False))
                self._set('order_authority',True);self._set('state','running')
                children=[]
                try:
                    for name,target in (('source',self._source_loop),('selection',self._selection_loop),('cycles',self._cycles_loop)):
                        t=threading.Thread(target=target,name='chili-native-'+name,daemon=True)
                        children.append(t);self.threads.append(t);t.start()
                    self.stop.wait()
                finally:
                    self.stop.set();self.published.set()
                    for t in children:t.join()
        except Exception as exc:
            self._set('state','failed');self._set('error',error_code(exc))
            LOG.warning('Native PAPER host unavailable (%s)',error_code(exc))
        finally:
            self._set('order_authority',False)
            if self.stop.is_set():self._set('state','stopped')

    def _get_data(self,**kwargs):
        self._authority(self.broker.account_id)
        if self.stop.wait(max(0,self.next_data-time.time())):raise ValueError('native_host_stopping')
        self._authority(self.broker.account_id)
        retained=kwargs.pop('record')
        def record(value):
            retained(value)
            headers={k.lower():v for k,v in value.get('rate_headers',{}).items()}
            if headers.get('x-ratelimit-remaining')=='0':
                deadline=rate_deadline(headers,time.time())
                if deadline is None:raise ValueError('native_host_rate_reset_missing')
                self.next_data=deadline
        return self.data.get(**kwargs,record=record)

    def _source_loop(self):
        while not self.stop.is_set():
            try:
                self.source.observe(time.time_ns(),self._get_data)
                self.current_source.observed.set()
                self._set('source',self.source.status());self.published.set()
            except Exception as exc:
                self._set('source',dict(self.source.status(),error=error_code(exc)))
                if isinstance(exc,DataHTTPError) and exc.status==429:
                    deadline=rate_deadline(exc.headers,time.time())
                    if deadline is not None:
                        self.next_data=deadline
                        if not self.stop.wait(max(0,deadline-time.time())):continue
                # Held-cycle reconciliation stays alive. No invented recovery
                # from torn journal, resource exhaustion or provider revisions.
                self._set('state','degraded_source')
                return
            if self.stop.wait(self.c['poll_seconds']):return

    def _selection_loop(self):
        while not self.stop.is_set():
            self.published.wait();self.published.clear()
            if self.stop.is_set():return
            try:self._set('selection',self.runtime.select())
            except Exception as exc:self._set('selection',dict(state='unavailable',error=error_code(exc)))

    def _cycles_loop(self):
        while not self.stop.is_set():
            try:self._set('reconciliation',self.runtime.reconcile(self.stop.is_set))
            except Exception as exc:self._set('reconciliation',dict(state='unavailable',error=error_code(exc)))
            if self.stop.wait(self.c['reconcile_seconds']):return


class HostLifecycle:
    def __init__(self):
        self.lock=threading.Lock();self.stopped=False;self.host=None
        self.state=dict(state='not_started',order_authority=False)

    def start(self,engine,settings):
        with self.lock:
            if self.stopped or self.host is not None:return
            path=getattr(settings,'chili_momentum_native_crypto_host_config_path','')
            if os.environ.get('CHILI_PYTEST','').lower() in ('1','true','yes') or not path:
                self.state=dict(state='not_configured',order_authority=False);return
            try:
                if (settings.chili_scheduler_role!='momentum_exec_only' or settings.chili_alpaca_paper is not True or
                    settings.chili_alpaca_enabled is not True or settings.chili_momentum_crypto_execution_via_alpaca_paper is not True):
                    raise ValueError('native_host_paper_execution_scope_required')
                c,sha=load_config(path)
                self.host=NativePaperHost(engine,settings,c,sha);self.host.start()
            except Exception as exc:self.state=dict(state='waiting_configuration',error=error_code(exc),order_authority=False)

    def close(self):
        with self.lock:self.stopped=True;host=self.host
        if host:return host.close()
        with self.lock:self.state=dict(state='stopped',order_authority=False)
        return self.status()

    def status(self):
        with self.lock:return self.host.status() if self.host else dict(self.state)


lifecycle=HostLifecycle()
