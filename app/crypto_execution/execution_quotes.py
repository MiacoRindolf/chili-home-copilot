"""Separately retained Alpaca US quote inputs for already-armed PAPER exits.

Reference-feed structure decides whether to exit. This reader only prices an
existing full-sale intent; a returned quote does not certify a future fill or
this account's simulator venue. No entry, order endpoint, or historical-window
fallback is provided.
"""
from dataclasses import asdict,dataclass
from decimal import Decimal
from fractions import Fraction
import hashlib,json,re,time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,build_opener

from .paper_http import NoRedirect
from .truth import decimal,asset_identity
from .lifecycle import decimal_text
from scripts.crypto_trade_frames import timestamp_ns


@dataclass(frozen=True)
class ExitPriceQuote:
    symbol:str
    bid:str
    ask:str
    bid_size:str
    ask_size:str
    event_ns:int
    request_started_ns:int
    received_ns:int
    body_sha256:str
    location:str='us'

    def __post_init__(self):
        if (any(type(v) is not int or v<=0 for v in (self.event_ns,self.request_started_ns,self.received_ns))
                or self.request_started_ns>self.received_ns or self.event_ns>self.received_ns
                or type(self.body_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',self.body_sha256)):
            raise ValueError('native_exit_quote_provenance_invalid')

    def price(self,asset):
        _,symbol=asset_identity(asset)
        if symbol!=self.symbol or self.location!='us':raise ValueError('native_exit_quote_asset_or_location_changed')
        bid,ask=Fraction(decimal(self.bid)),Fraction(decimal(self.ask))
        step=Fraction(decimal(asset['price_increment']))
        if not 0<bid<=ask or decimal(self.bid_size)<=0 or decimal(self.ask_size)<=0:
            raise ValueError('native_exit_quote_invalid_book')
        price=(bid//step)*step
        if price<=0:raise ValueError('native_exit_quote_below_price_increment')
        return decimal_text(price)

    def receipt(self):
        return dict(asdict(self),basis='alpaca_us_latest_quote_for_armed_paper_limit_exit',
            account_execution_venue_certified=False,fill_certified=False,order_authority=False)


class ExitQuoteHTTP:
    def __init__(self,*,paper,key,secret,before_read,observe_rate,timeout_seconds,max_response_bytes):
        if paper is not True or not all(type(v) is str and v for v in (key,secret)):
            raise ValueError('native_exit_quote_paper_credentials_required')
        if not callable(before_read) or not callable(observe_rate):raise ValueError('native_exit_quote_callbacks_required')
        if type(timeout_seconds) not in (int,float) or not 0<timeout_seconds< float('inf') or type(max_response_bytes) is not int or max_response_bytes<=0:
            raise ValueError('native_exit_quote_transport_bounds_required')
        self.key,self.secret=key,secret;self.before_read=before_read;self.observe_rate=observe_rate
        self.timeout,self.max_bytes=timeout_seconds,max_response_bytes;self.opener=build_opener(NoRedirect())

    def __call__(self,asset,record):
        _,symbol=asset_identity(asset)
        self.before_read()
        path='/v1beta3/crypto/us/latest/quotes'
        request=Request('https://data.alpaca.markets'+path+'?'+urlencode({'symbols':symbol}),method='GET',
            headers={'APCA-API-KEY-ID':self.key,'APCA-API-SECRET-KEY':self.secret})
        started=time.time_ns()
        try:
            try:response=self.opener.open(request,timeout=self.timeout)
            except HTTPError as exc:response=exc
            with response:
                raw=response.read(self.max_bytes+1);status=response.status
                headers={k.lower():v for k,v in response.headers.items() if k.lower().startswith('x-ratelimit') or k.lower()=='retry-after'}
        except Exception as exc:
            record(dict(phase='exit_quote_read_failed',path=path,symbol=symbol,error=type(exc).__name__,started_ns=started))
            raise ValueError('native_exit_quote_transport_failed') from None
        received=time.time_ns()
        if self.key.encode() in raw or self.secret.encode() in raw:raise ValueError('native_exit_quote_credential_echo')
        digest=hashlib.sha256(raw).hexdigest()
        record(dict(phase='exit_quote_response',path=path,symbol=symbol,location='us',status=status,
            started_ns=started,received_ns=received,body_sha256=digest,body_hex=raw.hex(),complete=len(raw)<=self.max_bytes,
            rate_headers=headers,account_execution_venue_certified=False))
        self.observe_rate(status,headers)
        if len(raw)>self.max_bytes:raise ValueError('native_exit_quote_response_capacity')
        if status!=200:raise ValueError('native_exit_quote_http_'+str(status))
        try:
            value=json.loads(raw,parse_float=Decimal,parse_int=Decimal);q=value['quotes'][symbol]
            if 'S' in q and q['S']!=symbol:raise ValueError('wrong_symbol')
            quote=ExitPriceQuote(symbol,*[decimal_text(Fraction(decimal(q[k]))) for k in ('bp','ap','bs','as')],
                timestamp_ns(q['t']),started,received,digest)
            quote.price(asset)
        except (KeyError,TypeError,ValueError):raise ValueError('native_exit_quote_payload_invalid') from None
        return quote
