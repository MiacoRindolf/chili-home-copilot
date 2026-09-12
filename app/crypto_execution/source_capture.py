"""Durable continuous REST source for one native shared-context owner.

Every successful observation publishes an accumulated prefix. Revisable mode
re-queries from the acquisition anchor because REST end is not provider finality;
late events rebuild the current view without changing retained old publications.
The explicit resource ceiling stops collection rather than truncating strategy
history. This initial implementation rebuilds the prefix, so long-running source
hosting still needs measured capacity and more efficient revision handling.
Restart verifies the retained chain and replays published observations.
Failed HTTP attempts do not advance the observation boundary. No broker orders,
order-authority claim, background thread, timer or implicit subscription exists.
The host owns scheduling and the exclusive directory lock.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from scripts.crypto_history_pages import HistoryRequest,HistoryWalk,canonical,sha
from .history_source import QuoteHistoryWalk,collect_history_batch,complete_walks
from .tick_context import CryptoTickContext
from app.tick_math.structural_prefix import Limits

def implementation_identity():
    from . import history_source,tick_context
    from app.tick_math import structural_prefix,wave_context,wave_evidence
    from scripts import crypto_trade_frames,crypto_history_pages
    paths=[Path(__file__),*[Path(m.__file__) for m in
        (history_source,tick_context,structural_prefix,wave_context,wave_evidence,crypto_trade_frames,crypto_history_pages)]]
    return {p.name:sha(p.read_bytes()) for p in paths}

def source_metadata(*,assets,location,source_id,anchor_ns,inventory_sha256,page_limit,resources,observation_mode='incremental_unfinalized'):
    return dict(contract='native_rest_source_capture_v1',assets=assets,location=location,source_id=source_id,
        anchor_ns=anchor_ns,inventory_sha256=inventory_sha256,page_limit=page_limit,resources=resources,
        observation_mode=observation_mode,
        implementation_sha256=implementation_identity())

def publication(book):
    return dict(source_root_sha256=book.root,source_sequence=book.sequence,
        symbols={s:dict(asset_id=book.assets[s],print_count=p.count,prefix_sha256=p.prefix_sha256)
                 for s,p in book.prefixes.items()})

class NativeSourceCapture:
    def __init__(self,directory,metadata,*,clock_ns=time.time_ns):
        self.directory=Path(directory);self.clock=clock_ns
        if (not self.directory.is_absolute() or not self.directory.is_dir() or
                metadata.get('contract')!='native_rest_source_capture_v1'):
            raise ValueError('native_source_capture_directory_or_contract_invalid')
        self.metadata=json.loads(canonical(metadata));self.resources=self.metadata['resources']
        self.mode=self.metadata.get('observation_mode','incremental_unfinalized')
        if self.mode not in ('incremental_unfinalized','revisable_prefix'):
            raise ValueError('native_source_observation_mode_invalid')
        self._trades={};self._quotes=set();self._reconstruction_required=False
        if self.metadata.get('implementation_sha256')!=implementation_identity():
            raise ValueError('native_source_capture_implementation_changed')
        required={'max_pages','max_trades','max_quotes','max_page_bytes','max_normalized_bytes',
                  'retained_ticks','frontier_ticks','active_references','max_pending_quotes','max_journal_bytes'}
        if set(self.resources)!=required or any(type(v) is not int or v<=0 for v in self.resources.values()):
            raise ValueError('native_source_capture_resource_bounds_required')
        self._make_book()
        self.revision=0;self.end_ns=self.metadata['anchor_ns'];self.valid=False;self.reason='cold'
        self.record_count=0;self.root=sha(canonical(self.metadata));self.byte_count=0;self._views=()
        self._request(self.end_ns)
        meta_path=self.directory/'metadata.json';records=self.directory/'source.jsonl'
        if meta_path.exists():
            if canonical(json.loads(meta_path.read_bytes()))!=canonical(self.metadata):
                raise ValueError('native_source_capture_metadata_changed')
            self._restore(records)
            if self.valid:self._views=tuple(self.book.view(s) for s in self.book.assets)
        else:
            if records.exists():raise ValueError('native_source_capture_metadata_missing')
            with meta_path.open('xb') as f:f.write(canonical(self.metadata).encode());f.flush();os.fsync(f.fileno())

    def _make_book(self):
        self.book=self._new_book()

    def _new_book(self):
        r=self.resources;m=self.metadata
        return CryptoTickContext(assets=m['assets'],location=m['location'],connection_id=m['source_id'],
            limits=Limits(r['retained_ticks'],r['frontier_ticks'],r['active_references']),
            max_frame_bytes=r['max_normalized_bytes'],max_pending_quotes=r['max_pending_quotes'],source_kind='rest_pages')

    def _candidate(self,batch,published_ns):
        if self.mode!='revisable_prefix':
            self._reconstruction_required=True
            result=self.book.append_rest_batch(batch,published_ns=published_ns)
            return self.book,result['prints'],None,None
        trades={(t.symbol,t.trade_id):(t.event_ns,t.price,t.size,t.reported_taker_side) for t in batch.trades}
        quotes={(q.symbol,q.event_ns,q.bid,q.ask,q.bid_size,q.ask_size) for q in batch.quotes}
        # A later query may add late events. Missing/changed already observed
        # members are an explicit provider revision; never silently erase them.
        if any(trades.get(k)!=v for k,v in self._trades.items()) or not self._quotes<=quotes:
            raise ValueError('native_source_previously_observed_members_missing_or_changed')
        candidate=self._new_book()
        candidate.append_rest_batch(batch,published_ns=published_ns)
        additions={s:0 for s in self.book.assets}
        for symbol,trade_id in trades.keys()-self._trades.keys():additions[symbol]+=1
        return candidate,additions,trades,quotes

    def _accept(self,candidate,trades,quotes):
        self.book=candidate
        if trades is not None:self._trades,self._quotes=trades,quotes
        self._reconstruction_required=False

    def _record(self,body):
        envelope=dict(sequence=self.record_count+1,previous=self.root,body=body)
        root=sha(canonical(envelope));raw=(canonical(dict(envelope,root=root))+'\n').encode()
        if self.byte_count+len(raw)>self.resources['max_journal_bytes']:
            raise ValueError('native_source_journal_capacity')
        with (self.directory/'source.jsonl').open('ab') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        self.record_count+=1;self.root=root;self.byte_count+=len(raw)
        return self.clock()

    def _request(self,end_ns):
        if end_ns<self.end_ns:raise ValueError('native_source_observation_end_regressed')
        start=self.metadata['anchor_ns'] if self.mode=='revisable_prefix' else self.end_ns
        return HistoryRequest(self.metadata['location'],tuple(self.book.assets),start,end_ns,
            self.metadata['page_limit'],self.metadata['inventory_sha256'])

    def observe(self,end_ns,get):
        if self.book.failure is not None or self._reconstruction_required:raise ValueError('native_source_context_reconstruction_required')
        request=self._request(end_ns);observation=str(uuid4())
        self.valid=False;self.reason='collecting'
        self._record(dict(kind='observation_started',observation=observation,request=asdict(request)))
        try:
            def record(value):self._record(dict(kind='transport',observation=observation,value=value))
            r=self.resources
            batch=collect_history_batch(request,get,record=record,max_pages=r['max_pages'],max_trades=r['max_trades'],
                max_quotes=r['max_quotes'],max_page_bytes=r['max_page_bytes'])
            published_ns=self._record(dict(kind='raw_batch_complete',observation=observation,evidence_sha256=batch.evidence_sha256))
            candidate,new_prints,trades,quotes=self._candidate(batch,published_ns)
            view=publication(candidate)
            self._record(dict(kind='context_published',observation=observation,revision=self.revision+1,
                end_ns=end_ns,raw_published_ns=published_ns,evidence_sha256=batch.evidence_sha256,view=view))
            self._accept(candidate,trades,quotes)
            self._views=tuple(self.book.view(s) for s in self.book.assets)
            self.revision+=1;self.end_ns=end_ns;self.valid=True;self.reason='observed_rest_page_prefix'
            return dict(self.status(),new_prints=new_prints)
        except Exception as error:
            self.reason='observation_failed:'+type(error).__name__
            # Even a torn write cannot be turned into current context. The
            # original error propagates; restart verifies every retained byte.
            try:self._record(dict(kind='observation_failed',observation=observation,error_type=type(error).__name__))
            except Exception:pass
            raise

    def _restore(self,path):
        if not path.exists():return
        active=None;trades=quotes=request=None;raw_complete=None
        with path.open('rb') as f:
            while raw:=f.readline(self.resources['max_page_bytes']*2+1048576):
                self.byte_count+=len(raw)
                if self.byte_count>self.resources['max_journal_bytes'] or not raw.endswith(b'\n'):
                    raise ValueError('native_source_retained_record_capacity_or_torn')
                envelope=json.loads(raw);root=envelope.pop('root')
                if (root!=sha(canonical(envelope)) or envelope['sequence']!=self.record_count+1 or envelope['previous']!=self.root):
                    raise ValueError('native_source_retained_chain_changed')
                self.root=root;self.record_count+=1;b=envelope['body'];kind=b['kind']
                if kind=='observation_started':
                    # An incomplete previous attempt has no published context.
                    # New attempts retain the last committed observation boundary.
                    fields=dict(b['request']);fields['symbols']=tuple(fields['symbols'])
                    request=HistoryRequest(**fields)
                    if request!=self._request(request.end_ns):raise ValueError('native_source_replay_request_changed')
                    active=b['observation'];r=self.resources;raw_complete=None
                    trades=HistoryWalk(request,max_pages=r['max_pages'],max_trades=r['max_trades'],max_page_bytes=r['max_page_bytes'])
                    quotes=QuoteHistoryWalk(request,max_pages=r['max_pages'],max_quotes=r['max_quotes'],max_page_bytes=r['max_page_bytes'])
                    self.valid=False;self.reason='retained_attempt_incomplete'
                elif b.get('observation')!=active or active is None:
                    raise ValueError('native_source_replay_observation_identity_changed')
                elif kind=='transport':
                    value=b['value']
                    if value['kind']=='http_read_failed':continue
                    if value['kind']!='http_response':raise ValueError('native_source_replay_transport_kind')
                    path_kind=value['path'].rsplit('/',1)[-1]
                    if path_kind not in ('trades','quotes') or value['path']!='/v1beta3/crypto/'+request.location+'/'+path_kind:
                        raise ValueError('native_source_replay_endpoint_changed')
                    walk=trades if path_kind=='trades' else quotes
                    if value['parameters']!=walk.parameters():raise ValueError('native_source_replay_query_changed')
                    body=bytes.fromhex(value['body_hex'])
                    if sha(body)!=value['body_sha256'] or len(body)>self.resources['max_page_bytes'] or not value['complete']:
                        raise ValueError('native_source_replay_body_changed')
                    if value['status']==200:walk.accept(body,received_ns=value['received_ns'])
                elif kind=='raw_batch_complete':
                    batch=complete_walks(request,trades,quotes)
                    if batch.evidence_sha256!=b['evidence_sha256']:raise ValueError('native_source_replay_batch_changed')
                    raw_complete=batch
                elif kind=='context_published':
                    if (raw_complete is None or b['evidence_sha256']!=raw_complete.evidence_sha256 or
                            b['revision']!=self.revision+1 or b['end_ns']!=request.end_ns):
                        raise ValueError('native_source_replay_publication_binding_changed')
                    candidate,_,trade_members,quote_members=self._candidate(raw_complete,b['raw_published_ns'])
                    if publication(candidate)!=b['view']:raise ValueError('native_source_replay_context_diverged')
                    self._accept(candidate,trade_members,quote_members)
                    self.revision=b['revision'];self.end_ns=b['end_ns'];self.valid=True;self.reason='reconstructed_rest_prefix'
                    active=None
                elif kind=='observation_failed':
                    self.valid=False;self.reason='retained_observation_failure';active=None
                else:raise ValueError('native_source_replay_record_kind_unknown')

    def status(self):
        return dict(source_id=self.metadata['source_id'],source_kind='rest_pages',revision=self.revision,
            observation_mode=self.mode,revision_can_include_late_events=self.mode=='revisable_prefix',
            source_end_ns=self.end_ns,valid=self.valid,reason=self.reason,record_root_sha256=self.root,
            record_count=self.record_count,journal_bytes=self.byte_count,
            requested_symbol_count=len(self.book.assets),print_counts={s:p.count for s,p in self.book.prefixes.items()},
            provider_event_time_finality_certified=False,order_authority=False)

    def current_views(self):
        if not self.valid:raise ValueError('native_source_current_observation_unavailable')
        return self._views
