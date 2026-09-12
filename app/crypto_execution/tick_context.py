"""Native crypto frames into the shared exact local/parent print reducer.

The source owner must durably retain raw frames before calling append. This
single-writer reducer neither authenticates its caller nor submits orders.
Every symbol is represented; cold/missing data never becomes a rank exclusion.
Connection loss requires explicit reconstruction or a new segment, not a timer
that silently makes old context current again.
"""
from bisect import bisect_right
from dataclasses import dataclass,replace
from fractions import Fraction
import hashlib
import json
from types import MappingProxyType

from scripts.crypto_trade_frames import CryptoFrame,decode_frame,decode_json
from app.tick_math.structural_prefix import (
    Tick,Limits,Prefix,ConsumerFrontierReceipt,Result,rows_sha256)
from app.tick_math.wave_evidence import wave_evidence
from .truth import asset_identity

ZERO=(Fraction(0),)*3

def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

@dataclass(frozen=True)
class PrintBinding:
    source_sequence: int
    provider_trade_id: int
    trade_event_ns: int
    quote_source_sequence: int | None
    quote_event_ns: int | None
    quote_basis: str
    reported_taker_side: str | None

@dataclass(frozen=True)
class ReportedFlow:
    basis: str
    kind: str
    order: int
    origin_id: int
    confirmation_id: int
    end_id: int
    formation: tuple[Fraction,Fraction,Fraction]
    follow_through: tuple[Fraction,Fraction,Fraction]
    # Each tuple is (reported buy volume, reported sell volume, unknown volume).
    side_basis: str = 'alpaca_reported_taker'

    @property
    def follow_net_bounds(self):
        b,s,u=self.follow_through
        return b-s-u,b-s+u

@dataclass(frozen=True)
class NativeSymbolContext:
    asset_id: str
    symbol: str
    source_kind: str
    source_identity_sha256: str
    source_sequence: int
    source_root_sha256: str
    print_count: int
    prefix_sha256: str
    last_print: Tick | None
    last_binding: PrintBinding | None
    current_quote: object | None
    current_quote_source_sequence: int | None
    wave: object | None
    quote_inferred_evidence: tuple
    reported_flow: tuple[ReportedFlow,...]
    coverage: str
    history_before_connection: str = 'unknown'
    order_authority: bool = False

class CryptoTickContext:
    def __init__(self,*,assets,location,connection_id,limits,max_frame_bytes,max_pending_quotes,source_kind='websocket'):
        if type(limits) is not Limits or any(type(v) is not int or v<=0 for v in (max_frame_bytes,max_pending_quotes)):
            raise ValueError('native_context_resource_bounds_required')
        if location not in ('us','us-1','eu-1') or type(connection_id) is not str or not connection_id:
            raise ValueError('native_context_source_identity_invalid')
        if source_kind not in ('websocket','rest_pages'):raise ValueError('native_context_source_kind_invalid')
        if type(assets) not in (list,tuple) or not assets:
            raise ValueError('native_context_assets_required')
        bindings={}
        for asset in assets:
            aid,symbol=asset_identity(asset)
            if asset.get('status')!='active' or asset.get('tradable') is not True or symbol in bindings or aid in bindings.values():
                raise ValueError('native_context_asset_membership_invalid')
            bindings[symbol]=aid
        self.assets=MappingProxyType(dict(sorted(bindings.items())))
        self.location,self.connection_id=location,connection_id
        self.source_kind=source_kind;self._rest_end=None
        identity=dict(contract='native_crypto_shared_tick_context_v1',location=location,
            connection_id=connection_id,assets=dict(self.assets))
        if source_kind!='websocket':identity['source_kind']=source_kind
        self.identity=sha(identity)
        self.prefixes={s:Prefix(s,self.identity,limits) for s in self.assets}
        self.max_bytes,self.max_quotes=max_frame_bytes,max_pending_quotes
        self.sequence=0;self.frame_sequence=0;self.received_ns=0;self.published_ns=0
        self.root=self.identity;self.acknowledged=False;self.failure=None
        self._quotes={s:[] for s in self.assets};self._trade_ids={s:{} for s in self.assets}
        self._bindings={s:[] for s in self.assets};self._mass={s:[ZERO] for s in self.assets}

    def invalidate(self,reason):
        if type(reason) is not str or not reason:raise ValueError('native_context_invalidation_reason_required')
        self.failure=reason

    def append(self,raw,*,frame_sequence,received_ns,published_ns):
        """Consume a whole durably recorded frame in member order, or quarantine.

        Publication/receipt clocks are provenance only. No seconds horizon,
        print-count strategy window, assumed sequential provider trade ID, or
        hindsight insertion into an earlier signal is used.
        """
        if self.failure is not None:raise ValueError('native_context_segment_unavailable')
        if self.source_kind!='websocket':raise ValueError('native_context_websocket_source_required')
        try:return self._append(raw,frame_sequence,received_ns,published_ns)
        except Exception:
            self.failure='frame_rejected_reconstruction_required'
            raise

    def append_rest_batch(self,batch,*,published_ns):
        from .history_source import CompleteHistoryBatch,batch_messages
        if self.failure is not None:raise ValueError('native_context_segment_unavailable')
        if self.source_kind!='rest_pages':raise ValueError('native_context_rest_source_required')
        try:
            if (type(batch) is not CompleteHistoryBatch or batch.request.location!=self.location or
                    tuple(self.assets)!=batch.request.symbols or
                    self._rest_end is not None and batch.request.start_ns!=self._rest_end):
                raise ValueError('native_context_rest_frontier_or_membership_changed')
            messages=batch_messages(batch)
            raw=json.dumps(messages,sort_keys=True,separators=(',',':'))
            result=self._append(raw,self.frame_sequence+1,batch.received_ns,published_ns,rest_evidence=batch.evidence_sha256)
            self._rest_end=batch.request.end_ns
            return result
        except Exception:
            self.failure='rest_batch_rejected_reconstruction_required';raise

    def _append(self,raw,frame_sequence,received_ns,published_ns,rest_evidence=None):
        if type(raw) not in (str,bytes):raise ValueError('native_context_raw_frame_required')
        encoded=raw.encode() if type(raw) is str else raw
        if len(encoded)>self.max_bytes:raise ValueError('native_context_frame_capacity')
        if (type(frame_sequence) is not int or frame_sequence!=self.frame_sequence+1 or
                type(received_ns) is not int or received_ns<self.received_ns or
                type(published_ns) is not int or published_ns<received_ns or published_ns<self.published_ns):
            raise ValueError('native_context_frame_frontier_invalid')
        frame=(CryptoFrame(self.location,self.connection_id,frame_sequence,hashlib.sha256(encoded).hexdigest(),received_ns,(),(),(),0)
            if rest_evidence is not None and encoded==b'[]' else
            decode_frame(encoded,feed_location=self.location,connection_id=self.connection_id,
                frame_sequence=frame_sequence,received_ns=received_ns,subscribed_symbols=frozenset(self.assets)))
        payload=decode_json(encoded.decode())
        if rest_evidence is not None:
            if frame.control_types:raise ValueError('native_context_rest_contains_stream_control')
            # A REST publication marker advances an empty observation too. It
            # is not a print, invented WebSocket frame or subscription ACK.
            payload=[dict(T='rest_observation',evidence_sha256=rest_evidence),*payload]
            frame=CryptoFrame(frame.feed_location,frame.connection_id,frame.frame_sequence,frame.raw_sha256,
                frame.received_ns,tuple(replace(t,member_index=t.member_index+1) for t in frame.trades),
                tuple(replace(q,member_index=q.member_index+1) for q in frame.quotes),(),len(payload))
        trades={t.member_index:t for t in frame.trades};quotes={q.member_index:q for q in frame.quotes}
        staged_quotes={s:list(v) for s,v in self._quotes.items()}
        additions={s:[] for s in self.assets};new_ids={s:{} for s in self.assets}
        new_bindings={s:[] for s in self.assets};new_mass={s:[] for s in self.assets}
        acknowledged=self.acknowledged or rest_evidence is not None;duplicates=[]
        for i,item in enumerate(payload):
            source_id=self.sequence+i+1
            if item['T']=='subscription':
                if (set(item.get('trades',[]))!=set(self.assets) or
                        set(item.get('quotes',[]))!=set(self.assets)):
                    raise ValueError('native_context_subscription_membership_changed')
                acknowledged=True
            if item['T'] not in ('t','q'):continue
            if not acknowledged:raise ValueError('native_context_data_before_subscription_ack')
            if i in quotes:
                q=quotes[i];history=staged_quotes[q.symbol]
                # Ordering by event time is only for the causal as-of join.
                # Receipt/member order still controls what was actually known.
                key=(q.event_ns,source_id,q)
                at=bisect_right([(t,s) for t,s,_ in history],(q.event_ns,source_id))
                history.insert(at,key)
                if len(history)>self.max_quotes:raise ValueError('native_context_pending_quote_capacity')
                continue
            t=trades[i];symbol=t.symbol
            fingerprint=(t.event_ns,t.price,t.size,t.reported_taker_side)
            previous=new_ids[symbol].get(t.trade_id,self._trade_ids[symbol].get(t.trade_id))
            if previous is not None:
                if previous!=fingerprint:raise ValueError('native_context_conflicting_trade_identity')
                duplicates.append((symbol,t.trade_id,source_id));continue
            history=staged_quotes[symbol]
            quote_time=t.event_ns-1 if rest_evidence is not None else t.event_ns
            at=bisect_right([(event,sid) for event,sid,_ in history],(quote_time,source_id))-1
            bound=history[at] if at>=0 else None
            quote=bound[2] if bound else None
            valid=quote is not None and quote.two_sided_uncrossed
            # An invalid latest eligible quote is retained as invalid; never
            # substitute an older valid quote to invent an executable spread.
            tick=Tick(source_id,t.price,t.size,quote.bid if valid else None,quote.ask if valid else None,
                t.event_ns,received_ns,published_ns,(self.location,self.connection_id),frame_sequence,frame.raw_sha256)
            additions[symbol].append(tick);new_ids[symbol][t.trade_id]=fingerprint
            new_bindings[symbol].append(PrintBinding(source_id,t.trade_id,t.event_ns,
                bound[1] if bound else None,quote.event_ns if quote else None,
                ('prior_event_quote_in_completed_rest_observation' if rest_evidence is not None else 'prior_received_asof_quote')
                    if valid else 'invalid_asof_quote' if quote else 'asof_quote_missing',
                t.reported_taker_side))
            mass=list(new_mass[symbol][-1] if new_mass[symbol] else self._mass[symbol][-1])
            mass[{'B':0,'S':1}.get(t.reported_taker_side,2)]+=Fraction(t.size)
            new_mass[symbol].append(tuple(mass))
            # Trades cannot go backward in the shared prefix. For subsequent
            # trades, quotes older than this as-of winner are dominated. The
            # bound quote is already retained in the immutable print itself.
            if at>0:del history[:at]
        sequence=self.sequence+len(payload)
        descriptor=dict(previous=self.root,frame_sequence=frame_sequence,raw_sha256=frame.raw_sha256,
            received_ns=received_ns,published_ns=published_ns,source_sequence=sequence)
        if rest_evidence is not None:descriptor.update(source_kind='rest_pages',rest_evidence_sha256=rest_evidence)
        root=sha(descriptor)
        prepared={}
        for symbol,prefix in self.prefixes.items():
            rows=additions[symbol]
            known=max([published_ns,*[r.known_ns for r in rows],
                prefix.last_receipt.known_ns if prefix.last_receipt else 0])
            receipt=ConsumerFrontierReceipt(known,len(rows),rows_sha256(rows),prefix.prefix_sha256,
                self.identity,sequence,root,self.sequence,self.root)
            value=prefix.prepare_frontier(rows,receipt)
            if isinstance(value,Result):raise ValueError('native_context_'+str(value.reason))
            prepared[symbol]=value
        # A single owner commits only after every symbol has passed validation.
        # Persistence/restart and process-allocation failure remain host concerns.
        for symbol,prefix in self.prefixes.items():
            if prefix.commit_frontier(prepared[symbol]).status!='applied':
                raise ValueError('native_context_single_writer_frontier_changed')
            self._trade_ids[symbol].update(new_ids[symbol]);self._bindings[symbol].extend(new_bindings[symbol])
            self._mass[symbol].extend(new_mass[symbol])
        self._quotes=staged_quotes;self.acknowledged=acknowledged
        self.sequence,self.frame_sequence,self.root=sequence,frame_sequence,root
        self.received_ns,self.published_ns=received_ns,published_ns
        return dict(frame_sequence=frame_sequence,source_sequence=sequence,root_sha256=root,
            subscribed=acknowledged if self.source_kind=='websocket' else None,
            source_kind=self.source_kind,prints={s:len(v) for s,v in additions.items()},duplicates=tuple(duplicates))

    def view(self,symbol):
        prefix=self.prefixes[symbol];wave=prefix.wave_context;flows=[]
        if wave is not None:
            refs=set(t for t in (wave.local_peak,wave.local_valley) if t is not None)
            for parent in wave.minimal_parents:
                for pair in parent.aliases:refs.update((pair.peak,pair.valley))
            def mass(start,end):
                # The shared geometry defines (start,end], excluding the origin.
                return tuple(b-a for a,b in zip(self._mass[symbol][start+1],self._mass[symbol][end+1]))
            for r in sorted(refs,key=lambda r:(r.basis,r.order,r.kind,r.origin_index,r.confirmation_index)):
                flows.append(ReportedFlow(r.basis,r.kind,r.order,r.origin_id,r.confirmation_id,wave.end_id,
                    mass(r.origin_index,r.confirmation_index),mass(r.confirmation_index,wave.end_index)))
        coverage=('unavailable:'+self.failure if self.failure else
                  ('awaiting_rest_observation' if self.source_kind=='rest_pages' else 'awaiting_subscription') if not self.acknowledged
                  else ('observed_rest_page_prefix' if self.source_kind=='rest_pages' else 'observed_connection_prefix') if prefix.count
                  else 'cold_no_observed_prints')
        return NativeSymbolContext(self.assets[symbol],symbol,self.source_kind,self.identity,self.sequence,self.root,prefix.count,prefix.prefix_sha256,
            prefix.tick(prefix.count-1) if prefix.count else None,
            self._bindings[symbol][-1] if prefix.count else None,
            self._quotes[symbol][-1][2] if self._quotes[symbol] else None,
            self._quotes[symbol][-1][1] if self._quotes[symbol] else None,
            wave,wave_evidence(prefix),tuple(flows),coverage)
