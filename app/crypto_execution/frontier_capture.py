"""Durable grouped observations and exact committed-publication recovery.

The caller must verify the supplied legacy seed and origin seal. This component
binds them immutably; it does not itself certify a V1 history or authorize orders.
One directory lock covers the entire lifetime. Raw HTTP records are fsynced
before decoding, publications before exposure. Recovery seeds once and applies
committed deltas using the same collector and reducer as prospective operation.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time
import threading

from scripts.capture_crypto_history_pages import capture_lock
from scripts.crypto_history_pages import HistoryRequest,canonical,sha
from .frontier_source import ChannelGroup,FrontierPlan,collect_frontiers,plan_frontiers,validate_plan
from .frontier_reducer import FrontierReducer
from .source_capture import publication


def implementation_identity():
    from . import frontier_source,frontier_members,frontier_reducer,member_sequence,member_index
    from .source_capture import implementation_identity as legacy_identity
    return dict(legacy=legacy_identity(),grouped={Path(m.__file__).name:sha(Path(m.__file__).read_bytes())
        for m in (frontier_source,frontier_members,frontier_reducer,member_sequence,member_index)},capture=sha(Path(__file__).read_bytes()))


def read_plan(body):
    groups=[]
    for g in body['groups']:
        fields=dict(g['request']);fields['symbols']=tuple(fields['symbols'])
        groups.append(ChannelGroup(g['channel'],HistoryRequest(**fields),g['known_overlap_rows'],g['estimated_pages']))
    p=FrontierPlan(tuple(groups),tuple(body['symbols']),body['prior_observation_sha256'],
        body['observed_through_ns'],body['anchor_ns'],body['end_ns'],body['mode'])
    validate_plan(p)
    if canonical(p.receipt())!=canonical(body):raise ValueError('native_frontier_retained_plan_changed')
    return p


class FrontierCapture:
    def __init__(self,directory,*,seed,seed_origin,seed_published_ns,book_factory,resources,clock_ns=time.time_ns):
        self.directory=Path(directory);self.clock=clock_ns;self.closed=False;self.closing=False;self.failed_write=False
        self.journal_lock=threading.RLock();self.owner_lock=threading.Lock();self.audit_lock=threading.Lock()
        self.pending_audit=None;self.audit_collecting=False;self.failed_modes=set();self.has_publication=False
        required={'max_pages','max_trades','max_quotes','max_page_bytes','max_journal_bytes'}
        if (not self.directory.is_absolute() or set(resources)!=required or
                any(type(n) is not int or n<=0 for n in resources.values())):
            raise ValueError('native_frontier_capture_resources_required')
        # The origin is an explicit external seal, never an implicit cold reset.
        if (type(seed_origin) is not dict or set(seed_origin)!=
                {'metadata_sha256','record_root_sha256','record_count','journal_bytes','revision'} or
                any(type(seed_origin[k]) is not int or seed_origin[k]<=0
                    for k in ('record_count','journal_bytes','revision')) or
                any(type(seed_origin[k]) is not str or len(seed_origin[k])!=64 or
                    any(c not in '0123456789abcdef' for c in seed_origin[k])
                    for k in ('metadata_sha256','record_root_sha256'))):
            raise ValueError('native_frontier_explicit_seed_origin_required')
        self.resources=dict(resources);self.stack=ExitStack()
        try:
            self.stack.enter_context(capture_lock(self.directory))
            self.reducer=FrontierReducer(seed,book_factory=book_factory,published_ns=seed_published_ns,
                max_trades=resources['max_trades'],max_quotes=resources['max_quotes'])
            self.metadata=json.loads(canonical(dict(contract='native_frontier_capture_v1',seed_origin=seed_origin,
                seed_evidence_sha256=seed.evidence_sha256,seed_published_ns=seed_published_ns,
                seed_view=publication(self.reducer.book),resources=resources,
                implementation_sha256=implementation_identity())))
            meta=self.directory/'metadata.json';self.path=self.directory/'frontier.jsonl'
            self.root=sha(canonical(self.metadata));self.record_count=0;self.byte_count=0
            self.valid=False;self.reason='seed_requires_current_observation'
            if meta.exists():
                with meta.open('rb') as f:raw=f.read(1048577)
                if len(raw)>1048576 or canonical(json.loads(raw))!=canonical(self.metadata):
                    raise ValueError('native_frontier_capture_binding_changed')
                self._restore()
            else:
                if self.path.exists():raise ValueError('native_frontier_capture_metadata_missing')
                with meta.open('xb') as f:
                    f.write(canonical(self.metadata).encode());f.flush();os.fsync(f.fileno())
        except Exception:
            self.stack.close();raise

    def close(self):
        with self.journal_lock:self.closing=True
        # Do not release filesystem ownership while a collector can still write.
        with self.audit_lock,self.owner_lock,self.journal_lock:
            self.closed=True;self.valid=False;self.stack.close()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    def _record(self,body):
        with self.journal_lock:return self._append_record(body)

    def _append_record(self,body):
        if self.closed or self.failed_write:raise ValueError('native_frontier_capture_recovery_required')
        envelope=dict(sequence=self.record_count+1,previous=self.root,body=body)
        root=sha(canonical(envelope));raw=(canonical(dict(envelope,root=root))+'\n').encode()
        if len(raw)>self._line_bound() or self.byte_count+len(raw)>self.resources['max_journal_bytes']:
            raise ValueError('native_frontier_journal_capacity')
        try:
            with self.path.open('ab') as f:
                if f.write(raw)!=len(raw):raise OSError('short journal write')
                f.flush();os.fsync(f.fileno())
        except Exception:
            self.failed_write=True;self.valid=False;raise
        self.root=root;self.record_count+=1;self.byte_count+=len(raw)

    def _line_bound(self):return self.resources['max_page_bytes']*2+1048576

    def _collect(self,plan,get,record):
        return collect_frontiers(plan,get,record=record,
            **{k:self.resources[k] for k in ('max_pages','max_trades','max_quotes','max_page_bytes')})

    def _plan(self,end_ns,mode,page_limit):
        if self.closed or self.closing or self.failed_write or not self.reducer.valid:
            raise ValueError('native_frontier_capture_recovery_required')
        m=self.reducer.members
        return plan_frontiers(histories=m.histories(),location=m.location,symbols=m.symbols,
            anchor_ns=m.anchor_ns,observed_through_ns=m.through_ns,end_ns=end_ns,
            page_limit=page_limit,inventory_sha256=m.inventory_sha256,prior_observation_sha256=m.identity,mode=mode)

    def _failed(self,plan,exc):
        with self.journal_lock:
            self.failed_modes.add(plan.mode);self.valid=False;self.reason='observation_failed:'+type(exc).__name__
            if not self.failed_write:
                try:self._record(dict(kind='frontier_observation_failed',plan_sha256=plan.identity,error_type=type(exc).__name__))
                except Exception:pass

    def _publish(self,observation):
        self.reducer.apply(observation,published_ns=self.clock(),record=self._record)
        with self.journal_lock:
            self.failed_modes.discard(observation.plan.mode);self.has_publication=True
            self.valid=not self.failed_modes and not self.failed_write
            self.reason='observed_grouped_rest_prefix' if self.valid else 'other_observation_unresolved'

    def observe(self,end_ns,get,*,mode='observed_frontiers',page_limit):
        # Only this owner (also publish_audit) may mutate reducer membership.
        with ExitStack() as stack:
            if mode=='full_anchor_audit':
                stack.enter_context(self.audit_lock)
                if self.pending_audit is not None:raise ValueError('native_frontier_audit_pending_publication')
            stack.enter_context(self.owner_lock)
            plan=self._plan(end_ns,mode,page_limit)
            try:
                observation=self._collect(plan,get,self._record)
                self._publish(observation)
                return self.status()
            except Exception as exc:
                self._failed(plan,exc);raise

    def collect_audit(self,end_ns,get,*,page_limit):
        """One background collector retains raw evidence, never publishes math."""
        if not self.audit_lock.acquire(blocking=False):raise ValueError('native_frontier_audit_already_collecting')
        try:
            with self.journal_lock:
                if self.pending_audit is not None:raise ValueError('native_frontier_audit_pending_publication')
                self.audit_collecting=True
            with self.owner_lock:
                plan=self._plan(self.clock() if end_ns is None else end_ns,'full_anchor_audit',page_limit)
            try:
                observation=self._collect(plan,get,self._record)
                with self.journal_lock:self.pending_audit=observation
                return dict(plan_sha256=plan.identity,observation_sha256=observation.identity,published=False)
            except Exception as exc:
                self._failed(plan,exc);raise
        finally:
            with self.journal_lock:self.audit_collecting=False
            self.audit_lock.release()

    def publish_audit(self):
        """Main source owner calls only between fast observations."""
        with self.owner_lock:
            with self.journal_lock:observation=self.pending_audit
            if observation is None:return False
            try:self._publish(observation)
            except Exception as exc:
                self._failed(observation.plan,exc);raise
            finally:
                with self.journal_lock:self.pending_audit=None
            return True

    def _restore(self):
        if not self.path.exists():return
        active={}
        with self.path.open('rb') as f:
            while True:
                offset=f.tell();raw=f.readline(self._line_bound()+1)
                if not raw:break
                self.byte_count+=len(raw)
                if (not raw.endswith(b'\n') or len(raw)>self._line_bound() or
                        self.byte_count>self.resources['max_journal_bytes']):
                    raise ValueError('native_frontier_retained_capacity_or_torn')
                envelope=json.loads(raw);root=envelope.pop('root')
                if (root!=sha(canonical(envelope)) or envelope['sequence']!=self.record_count+1 or envelope['previous']!=self.root):
                    raise ValueError('native_frontier_retained_chain_changed')
                self.record_count+=1;self.root=root;b=envelope['body'];kind=b['kind']
                if kind=='frontier_observation_started':
                    plan=read_plan(b['plan'])
                    current=self.reducer.members
                    if (plan.identity!=b['plan_sha256'] or
                            plan.prior_observation_sha256!=current.identity and
                            (plan.mode!='full_anchor_audit' or plan.prior_observation_sha256 not in current.ancestors)):
                        raise ValueError('native_frontier_retained_prior_changed')
                    # A restarted attempt can supersede an uncommitted attempt
                    # of the same collector. Its raw evidence remains retained.
                    active={k:v for k,v in active.items() if v['plan'].mode!=plan.mode}
                    active[plan.identity]=dict(plan=plan,transports=[],observation=None)
                    self.failed_modes.add(plan.mode);self.valid=False;self.reason='retained_attempt_incomplete'
                    continue
                if kind=='frontier_context_published':
                    found=[(key,v) for key,v in active.items() if v['observation'] is not None and
                           v['observation'].identity==b['observation_sha256']]
                    if len(found)!=1:raise ValueError('native_frontier_retained_raw_incomplete')
                    key,stage=found[0];observation=stage['observation']
                    def compare(value):
                        if canonical(value)!=canonical(b):raise ValueError('native_frontier_retained_math_diverged')
                    self.reducer.apply(observation,published_ns=b['published_ns'],record=compare)
                    self.failed_modes.discard(stage['plan'].mode);self.has_publication=True
                    self.valid=not self.failed_modes;self.reason='reconstructed_grouped_rest_prefix'
                    del active[key];continue
                key=b.get('plan_sha256') if kind!='frontier_observation_complete' else b['receipt'].get('plan_sha256')
                stage=active.get(key)
                if stage is None:raise ValueError('native_frontier_retained_observation_missing')
                plan=stage['plan']
                if kind=='frontier_transport':
                    if stage['observation'] is not None:
                        raise ValueError('native_frontier_retained_transport_order')
                    stage['transports'].append(offset)
                    if len(stage['transports'])>self.resources['max_pages']:
                        raise ValueError('native_frontier_retained_page_capacity')
                elif kind=='frontier_observation_complete':
                    if stage['observation'] is not None:raise ValueError('native_frontier_retained_duplicate_complete')
                    stage['observation']=self._recollect(plan,stage['transports'],b)
                elif kind=='frontier_observation_failed':
                    self.failed_modes.add(plan.mode);self.valid=False;self.reason='retained_observation_failed'
                    del active[key]
                else:raise ValueError('native_frontier_retained_record_kind')
        for stage in active.values():
            if stage['plan'].mode=='full_anchor_audit' and stage['observation'] is not None:
                self.pending_audit=stage['observation']

    def _recollect(self,plan,offsets,complete):
        # Retain offsets, not another whole raw observation in memory.
        cursor=0;expected_transport=None
        with self.path.open('rb') as f:
            def get(*,location,kind,parameters,record):
                nonlocal cursor,expected_transport
                if cursor>=len(offsets):raise ValueError('native_frontier_retained_response_missing')
                f.seek(offsets[cursor]);cursor+=1
                expected_transport=json.loads(f.readline(self._line_bound()+1))['body']
                v=expected_transport['transport'];record(v)
                return bytes.fromhex(v['body_hex']),v['received_ns']
            def check(value):
                expected=(dict(kind='frontier_observation_started',plan_sha256=plan.identity,plan=plan.receipt())
                    if value['kind']=='frontier_observation_started' else
                    complete if value['kind']=='frontier_observation_complete' else expected_transport)
                if canonical(value)!=canonical(expected):raise ValueError('native_frontier_retained_collection_diverged')
            result=self._collect(plan,get,check)
            if cursor!=len(offsets):raise ValueError('native_frontier_retained_extra_responses')
            return result

    def current_views(self):
        if not self.valid or self.closed or self.closing:raise ValueError('native_source_current_observation_unavailable')
        return self.reducer.current_views()

    def status(self):
        return dict(valid=self.valid and not self.closed and not self.closing,reason=self.reason,revision=self.reducer.revision,
            source_end_ns=self.reducer.members.through_ns,record_root_sha256=self.root,record_count=self.record_count,
            journal_bytes=self.byte_count,requested_symbol_count=len(self.reducer.members.symbols),
            audit_collecting=self.audit_collecting,audit_pending=self.pending_audit is not None,
            unresolved_modes=sorted(self.failed_modes),
            provider_event_time_finality_certified=False,order_authority=False)
