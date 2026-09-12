"""Complete paginated native trade/quote observations for every requested asset.

Request boundaries are transport watermarks, never strategy windows. The caller
retains all prior tick state. Exhausting both page chains proves the response
walk, not provider event-time finality or ordering of equal-timestamp events.
"""
from dataclasses import asdict,dataclass
from decimal import Decimal
import hashlib
import json
import math
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,build_opener

from scripts.crypto_history_pages import HistoryRequest,HistoryWalk,canonical,format_ns,sha
from scripts.crypto_trade_frames import CryptoQuote,decode_json,timestamp_ns,_number
from .paper_http import NoRedirect

@dataclass(frozen=True)
class CompleteHistoryBatch:
    request: HistoryRequest
    trades: tuple
    quotes: tuple
    trade_receipt: dict
    quote_receipt: dict
    received_ns: int
    evidence_sha256: str
    content_sha256: str

class QuoteHistoryWalk:
    def __init__(self,request,*,max_pages,max_quotes,max_page_bytes):
        if type(request) is not HistoryRequest or any(type(v) is not int or v<=0 for v in (max_pages,max_quotes,max_page_bytes)):
            raise ValueError('crypto_quote_history_resources_invalid')
        self.request=request;self.max_pages=max_pages;self.max_quotes=max_quotes;self.max_bytes=max_page_bytes
        self.page_count=0;self.next_token=None;self.complete=False;self.quotes=[];self._tokens=set();self._last={}
        self.root=sha(canonical(dict(contract='crypto_quote_history_v1',request=asdict(request))))
        self.last_received_ns=None

    def parameters(self):
        if self.complete or self.page_count>=self.max_pages:raise ValueError('crypto_quote_history_page_capacity_or_complete')
        return self.request.parameters(self.next_token)

    def accept(self,raw,*,received_ns):
        self.parameters()
        encoded=raw.encode() if type(raw) is str else raw
        if (type(encoded) is not bytes or len(encoded)>self.max_bytes or type(received_ns) is not int or
                received_ns<self.request.end_ns or self.last_received_ns is not None and received_ns<self.last_received_ns):
            raise ValueError('crypto_quote_history_response_bounds_invalid')
        value=decode_json(encoded.decode())
        if type(value) is not dict or type(value.get('quotes')) is not dict or 'next_page_token' not in value:
            raise ValueError('crypto_quote_history_response_shape_invalid')
        token=value['next_page_token']
        if token is not None and (type(token) is not str or not token or token==self.next_token or token in self._tokens):
            raise ValueError('crypto_quote_history_pagination_cycle_or_token')
        additions=[];last=dict(self._last)
        for symbol,rows in value['quotes'].items():
            if symbol not in self.request.symbols or type(rows) is not list:
                raise ValueError('crypto_quote_history_unrequested_symbol')
            for row in rows:
                if type(row) is not dict:raise ValueError('crypto_quote_history_member_invalid')
                event=timestamp_ns(row.get('t'))
                if (not self.request.start_ns<=event<=self.request.end_ns or event<last.get(symbol,event) or
                        'S' in row and row['S']!=symbol or 'T' in row and row['T']!='q'):
                    raise ValueError('crypto_quote_history_member_binding_invalid')
                last[symbol]=event
                additions.append(CryptoQuote(symbol,_number(row.get('bp'),positive=False),_number(row.get('ap'),positive=False),
                    _number(row.get('bs'),positive=False),_number(row.get('as'),positive=False),event,received_ns,len(additions)))
        if len(additions)>self.request.page_limit or len(self.quotes)+len(additions)>self.max_quotes:
            raise ValueError('crypto_quote_history_member_capacity')
        root=sha(canonical(dict(previous=self.root,request_token=self.next_token,next_token=token,
            raw_sha256=sha(encoded),received_ns=received_ns)))
        self.quotes.extend(additions);self._last=last;self.root=root;self._tokens.add(self.next_token)
        self.next_token=token;self.page_count+=1;self.complete=token is None;self.last_received_ns=received_ns

    def receipt(self):
        counts={s:0 for s in self.request.symbols}
        for q in self.quotes:counts[q.symbol]+=1
        return dict(contract='crypto_quote_history_v1',request_sha256=self.request.identity,
            pages=self.page_count,root_sha256=self.root,quote_counts=counts,
            provider_page_chain_exhausted=self.complete,provider_event_time_finality_certified=False,
            equal_timestamp_source_order_certified=False,known_ns=self.last_received_ns)

class DataHTTPError(RuntimeError):
    def __init__(self,status,headers):
        super().__init__('crypto_data_http_'+str(status));self.status=status;self.headers=headers

class CryptoDataHTTP:
    """Read-only data.alpaca.markets client; never contains an order endpoint."""
    def __init__(self,*,paper,key,secret,timeout_seconds,max_response_bytes):
        if paper is not True or not all(type(v) is str and v for v in (key,secret)):
            raise ValueError('crypto_data_paper_credentials_required')
        if (type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or timeout_seconds<=0 or
                type(max_response_bytes) is not int or max_response_bytes<=0):
            raise ValueError('crypto_data_transport_resources_required')
        self._key,self._secret=key,secret;self.timeout=timeout_seconds;self.max_bytes=max_response_bytes
        self._opener=build_opener(NoRedirect())

    def get(self,*,location,kind,parameters,record):
        if location not in ('us','us-1','eu-1') or kind not in ('trades','quotes'):
            raise ValueError('crypto_data_endpoint_not_allowed')
        path='/v1beta3/crypto/'+location+'/'+kind
        started=time.time_ns()
        request=Request('https://data.alpaca.markets'+path+'?'+urlencode(parameters),method='GET',
            headers={'APCA-API-KEY-ID':self._key,'APCA-API-SECRET-KEY':self._secret})
        try:
            try:r=self._opener.open(request,timeout=self.timeout)
            except HTTPError as error:r=error
            with r:
                status=r.status;raw=r.read(self.max_bytes+1)
                headers={k:v for k,v in r.headers.items() if k.lower().startswith('x-ratelimit') or k.lower()=='retry-after'}
        except Exception as error:
            record(dict(kind='http_read_failed',path=path,parameters=parameters,started_ns=started,error_type=type(error).__name__))
            raise RuntimeError('crypto_data_read_failed') from None
        received=time.time_ns()
        if self._key.encode() in raw or self._secret.encode() in raw:raise ValueError('crypto_data_credential_echo_refused')
        complete=len(raw)<=self.max_bytes
        # Caller must fsync this raw response before parsing can change context.
        record(dict(kind='http_response',path=path,parameters=parameters,started_ns=started,received_ns=received,
            status=status,complete=complete,body_hex=raw.hex(),body_sha256=sha(raw),rate_headers=headers))
        if not complete:raise ValueError('crypto_data_response_capacity')
        if status!=200:raise DataHTTPError(status,headers)
        return raw,received

def collect_history_batch(request,get,*,record,max_pages,max_trades,max_quotes,max_page_bytes):
    trades=HistoryWalk(request,max_pages=max_pages,max_trades=max_trades,max_page_bytes=max_page_bytes)
    quotes=QuoteHistoryWalk(request,max_pages=max_pages,max_quotes=max_quotes,max_page_bytes=max_page_bytes)
    for kind,walk in (('trades',trades),('quotes',quotes)):
        while not walk.complete:
            raw,received=get(location=request.location,kind=kind,parameters=walk.parameters(),record=record)
            walk.accept(raw,received_ns=received)
    return complete_walks(request,trades,quotes)

def complete_walks(request,trades,quotes):
    if not trades.complete or not quotes.complete:raise ValueError('crypto_history_batch_incomplete')
    tr,qr=trades.receipt(),quotes.receipt()
    values=trades.complete_trades();quoted=tuple(quotes.quotes)
    content=sha(canonical(_event_messages(values,quoted)))
    evidence=sha(canonical(dict(contract='complete_crypto_history_batch_v1',request=asdict(request),trades=tr,quotes=qr,content_sha256=content)))
    return CompleteHistoryBatch(request,values,quoted,tr,qr,
        max(trades.last_received_ns,quotes.last_received_ns),evidence,content)

def batch_messages(batch):
    """Normalize a complete observation; equal-time quote/trade order is unknown.

    Source consumers MUST use strict event-before-trade quote linkage for this
    REST contract. The sorted tie order here is deterministic serialization only.
    All members become available together; no intermediate entry is emitted.
    """
    if type(batch) is not CompleteHistoryBatch:raise ValueError('crypto_complete_batch_required')
    messages=_event_messages(batch.trades,batch.quotes)
    content=sha(canonical(messages))
    if (content!=batch.content_sha256 or batch.evidence_sha256!=sha(canonical(dict(
            contract='complete_crypto_history_batch_v1',request=asdict(batch.request),trades=batch.trade_receipt,
            quotes=batch.quote_receipt,content_sha256=content))) or
            batch.received_ns!=max(batch.trade_receipt['known_ns'],batch.quote_receipt['known_ns']) or
            batch.trade_receipt.get('provider_page_chain_exhausted') is not True or
            batch.quote_receipt.get('provider_page_chain_exhausted') is not True):
        raise ValueError('crypto_complete_batch_binding_changed')
    return messages

def _event_messages(trades,quotes):
    events=[]
    for i,q in enumerate(quotes):
        events.append((q.event_ns,q.symbol,0,i,dict(T='q',S=q.symbol,t=format_ns(q.event_ns),
            bp=str(q.bid),ap=str(q.ask),bs=str(q.bid_size),**{'as':str(q.ask_size)})))
    for i,t in enumerate(trades):
        events.append((t.event_ns,t.symbol,1,i,dict(T='t',S=t.symbol,t=format_ns(t.event_ns),
            i=t.trade_id,p=str(t.price),s=str(t.size),tks=t.reported_taker_side)))
    return tuple(row[-1] for row in sorted(events,key=lambda row:row[:4]))
