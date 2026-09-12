"""Exact, whole-page crypto trade history with an explicit exhaustion receipt.

Capture boundaries and page sizes are transport inputs, never strategy windows.
Page exhaustion is NOT provider event-time finality, live delivery completeness,
subscription coverage, executable quote evidence or momentum eligibility.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from scripts.crypto_trade_frames import CryptoTrade, decode_json, timestamp_ns, _number


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def format_ns(value):
    if type(value) is not int or value <= 0:
        raise ValueError('crypto_history_clock_invalid')
    seconds, nanos = divmod(value, 10**9)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')+f'.{nanos:09d}Z'


@dataclass(frozen=True)
class HistoryRequest:
    location: str
    symbols: tuple[str, ...]
    start_ns: int
    end_ns: int
    page_limit: int
    inventory_sha256: str

    def __post_init__(self):
        if self.location not in ('us', 'us-1', 'eu-1'):
            raise ValueError('crypto_history_location_unverified')
        if (type(self.symbols) is not tuple or not self.symbols or
                any(type(s) is not str or not re.fullmatch('[A-Z0-9]+/[A-Z0-9]+',s) for s in self.symbols)
                or tuple(sorted(set(self.symbols))) != self.symbols):
            raise ValueError('crypto_history_symbol_membership_invalid')
        if any(type(v) is not int or v <= 0 for v in (self.start_ns, self.end_ns)) or self.end_ns < self.start_ns:
            raise ValueError('crypto_history_interval_invalid')
        if type(self.page_limit) is not int or not 1 <= self.page_limit <= 10000:
            raise ValueError('crypto_history_documented_page_limit_invalid')
        if type(self.inventory_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',self.inventory_sha256):
            raise ValueError('crypto_history_inventory_digest_invalid')

    @property
    def identity(self): return sha(canonical(asdict(self)))

    @property
    def url(self): return 'https://data.alpaca.markets/v1beta3/crypto/'+self.location+'/trades'

    def parameters(self, page_token=None):
        result=dict(symbols=','.join(self.symbols), start=format_ns(self.start_ns), end=format_ns(self.end_ns),
                    limit=self.page_limit, sort='asc')
        if page_token is not None:
            if type(page_token) is not str or not page_token:
                raise ValueError('crypto_history_page_token_invalid')
            result['page_token']=page_token
        return result


@dataclass(frozen=True)
class HistoryPage:
    request_sha256: str
    request_token: str | None
    next_token: str | None
    raw_sha256: str
    received_ns: int
    trades: tuple[CryptoTrade, ...]


def decode_page(raw, *, request: HistoryRequest, request_token, received_ns, max_bytes):
    if type(request) is not HistoryRequest or type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('crypto_history_decode_inputs_invalid')
    if type(received_ns) is not int or received_ns < request.end_ns:
        raise ValueError('crypto_history_receipt_before_capture_boundary')
    request.parameters(request_token)
    encoded=raw if type(raw) is bytes else raw.encode('utf-8')
    if len(encoded)>max_bytes:
        raise ValueError('crypto_history_page_byte_capacity')
    value=decode_json(encoded.decode('utf-8'))
    if type(value) is not dict or 'trades' not in value or 'next_page_token' not in value or type(value['trades']) is not dict:
        raise ValueError('crypto_history_page_shape_invalid')
    token=value['next_page_token']
    if token is not None and (type(token) is not str or not token):
        raise ValueError('crypto_history_next_token_invalid')
    trades=[]
    for symbol, rows in value['trades'].items():
        if symbol not in request.symbols or type(rows) is not list:
            raise ValueError('crypto_history_unrequested_or_invalid_symbol')
        for row in rows:
            if type(row) is not dict or type(row.get('i')) is not int or row['i']<0:
                raise ValueError('crypto_history_trade_identity_invalid')
            event=timestamp_ns(row.get('t'))
            if not request.start_ns <= event <= request.end_ns:
                raise ValueError('crypto_history_trade_outside_requested_interval')
            side=row.get('tks')
            if side not in (None, '', 'B', 'S'):
                raise ValueError('crypto_history_taker_side_invalid')
            if ('S' in row and row['S'] != symbol) or ('T' in row and row['T'] != 't'):
                raise ValueError('crypto_history_member_identity_conflict')
            trades.append(CryptoTrade(symbol,row['i'],_number(row.get('p'),positive=True),
                _number(row.get('s'),positive=True),event,received_ns,len(trades),side or None,
                {'B':1,'S':-1}.get(side,0),'alpaca_reported_taker' if side else 'unknown'))
    if len(trades)>request.page_limit:
        raise ValueError('crypto_history_page_exceeds_requested_limit')
    return HistoryPage(request.identity,request_token,token,sha(encoded),received_ns,tuple(trades))


class HistoryWalk:
    """Stage a whole page before changing the capture frontier.

    Original raw pages belong in the caller's durable journal. This walker is
    restored by replaying that journal, never from an unauthenticated token alone.
    Identity is location+symbol+trade ID within this explicitly bounded capture;
    no global trade-ID monotonicity is inferred.
    """
    def __init__(self, request, *, max_pages, max_trades, max_page_bytes):
        if type(request) is not HistoryRequest or any(type(v) is not int or v<=0 for v in (max_pages,max_trades,max_page_bytes)):
            raise ValueError('crypto_history_walk_capacity_invalid')
        self.request=request
        self.max_pages,self.max_trades,self.max_page_bytes=max_pages,max_trades,max_page_bytes
        self.page_count=0
        self.next_token=None
        self.complete=False
        self.root=sha(canonical({'contract':'crypto_history_walk_v1','request':asdict(request)}))
        self._tokens=set()
        self._seen={}
        self._last_event={}
        self._trades=[]
        self.last_received_ns=None
        self.duplicate_occurrences=0
        self.equal_timestamp_occurrences=0

    def parameters(self):
        if self.complete: raise ValueError('crypto_history_walk_already_complete')
        if self.page_count>=self.max_pages: raise ValueError('crypto_history_page_capacity')
        return self.request.parameters(self.next_token)

    def accept(self, raw, *, received_ns):
        self.parameters()
        page=decode_page(raw,request=self.request,request_token=self.next_token,
            received_ns=received_ns,max_bytes=self.max_page_bytes)
        if self.last_received_ns is not None and received_ns<self.last_received_ns:
            raise ValueError('crypto_history_receipt_clock_regressed')
        if page.next_token is not None and (page.next_token==self.next_token or page.next_token in self._tokens):
            raise ValueError('crypto_history_pagination_cycle')
        staged_seen, last, additions = {}, dict(self._last_event), []
        duplicates, ties=0,0
        for trade in page.trades:
            key=trade.symbol,trade.trade_id
            identity=(trade.event_ns,trade.price,trade.size,trade.reported_taker_side)
            before=staged_seen.get(key,self._seen.get(key))
            if before is not None:
                if before != identity: raise ValueError('crypto_history_trade_id_conflict')
                duplicates+=1
                continue
            if trade.event_ns<last.get(trade.symbol,trade.event_ns):
                raise ValueError('crypto_history_symbol_time_regressed')
            ties+=last.get(trade.symbol)==trade.event_ns
            staged_seen[key]=identity
            last[trade.symbol]=trade.event_ns
            additions.append(trade)
        if len(self._trades)+len(additions)>self.max_trades:
            raise ValueError('crypto_history_trade_capacity')
        descriptor=dict(request_sha256=page.request_sha256,request_token=page.request_token,
            next_token=page.next_token,raw_sha256=page.raw_sha256,received_ns=received_ns)
        root=sha(canonical({'previous_root':self.root,'page':descriptor}))
        self._seen.update(staged_seen)
        self._last_event=last
        self._trades.extend(additions)
        self._tokens.add(self.next_token)
        self.next_token=page.next_token
        self.page_count+=1
        self.root=root
        self.last_received_ns=received_ns
        self.complete=page.next_token is None
        self.duplicate_occurrences+=duplicates
        self.equal_timestamp_occurrences+=ties
        return page

    def complete_trades(self):
        if not self.complete: raise ValueError('crypto_history_pages_incomplete')
        return tuple(self._trades)

    def receipt(self):
        counts={symbol:0 for symbol in self.request.symbols}
        for trade in self._trades: counts[trade.symbol]+=1
        return dict(request_sha256=self.request.identity,pages=self.page_count,next_token=self.next_token,
            root_sha256=self.root,provider_page_chain_exhausted=self.complete,
            trade_counts=counts,unique_trades=len(self._trades),duplicate_occurrences=self.duplicate_occurrences,
            equal_timestamp_occurrences=self.equal_timestamp_occurrences,known_ns=self.last_received_ns,
            source_location=self.request.location,capture_start_ns=self.request.start_ns,capture_end_ns=self.request.end_ns,
            provider_event_time_finality_certified=False,live_delivery_complete=False,
            quote_coverage_certified=False,source_order_at_equal_timestamp_certified=False,
            momentum_eligibility=False,order_authority=False)
