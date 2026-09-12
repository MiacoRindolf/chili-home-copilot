"""Literal PAPER native-order transport with raw evidence before interpretation."""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .truth import decimal,identity

PAPER='https://paper-api.alpaca.markets'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('native_paper_redirect_refused')


class NotTransported(RuntimeError):
    """This local invocation failed before entering the HTTP opener."""
    def __init__(self,request_id,reason):
        super().__init__('native_paper_proven_not_transported:'+reason)
        self.request_id=request_id


@dataclass(frozen=True)
class BrokerResponse:
    status: int
    body: bytes

    def json(self):
        return json.loads(self.body,parse_float=Decimal,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('broker_nonfinite_json')))


class PaperCycleHTTP:
    """Transport authority is supplied by the verified running PAPER owner.

    No endpoint override, credential fallback, retry, scheduler or strategy is
    hidden here. Read/response byte limits are explicit engineering inputs.
    """
    def __init__(self,*,paper,account_id,key,secret,require_authority,timeout_seconds,max_response_bytes):
        if paper is not True:raise ValueError('native_execution_requires_paper')
        self.account_id=identity(account_id)
        if (type(key) is not str or not key or type(secret) is not str or not secret or
                not callable(require_authority) or type(max_response_bytes) is not int or max_response_bytes<=0 or
                type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or timeout_seconds<=0):
            raise ValueError('native_paper_transport_inputs_invalid')
        self._key=key;self._secret=secret;self.require_authority=require_authority
        self.timeout=timeout_seconds;self.max_bytes=max_response_bytes
        self._opener=build_opener(NoRedirect())

    def request(self,method,path,payload,*,record,before_transport):
        allowed=(method=='GET' and (path in ('/v2/account','/v2/positions') or
            re.fullmatch(r'/v2/orders\?status=open&limit=500&direction=desc&nested=false(?:&before_order_id=[0-9a-f-]{36})?',path) or
            re.fullmatch(r'/v2/(orders|positions)/[0-9a-f-]{36}',path) or
            re.fullmatch(r'/v2/orders:by_client_order_id\?client_order_id=[A-Za-z0-9_.%~-]+',path)) or
            method=='POST' and path=='/v2/orders' or
            method=='DELETE' and re.fullmatch(r'/v2/orders/[0-9a-f-]{36}',path))
        if not allowed:raise ValueError('native_paper_operation_not_allowed')
        if method!='POST' and payload is not None:raise ValueError('native_paper_unexpected_body')
        if method=='POST':
            if (type(payload) is not dict or set(payload)!=
                    {'symbol','side','type','time_in_force','qty','limit_price','client_order_id'} or
                    type(payload.get('symbol')) is not str or
                    not re.fullmatch('[A-Z0-9]+/[A-Z0-9]+',payload['symbol']) or
                    payload.get('side') not in ('buy','sell') or payload.get('type')!='limit' or
                    payload.get('time_in_force') not in ('gtc','ioc')):
                raise ValueError('native_paper_crypto_limit_instruction_required')
            decimal(payload['qty']);decimal(payload['limit_price'])
            if type(payload['client_order_id']) is not str or not payload['client_order_id']:
                raise ValueError('native_paper_client_order_id_required')
        raw=None if payload is None else json.dumps(payload,separators=(',',':'),allow_nan=False).encode()
        request_id=str(uuid4())
        record(dict(phase='request',request_id=request_id,method=method,path=path,payload=payload))
        try:
            self.require_authority(self.account_id)
            before_transport()
            request=Request(PAPER+path,data=raw,method=method,headers={
                'APCA-API-KEY-ID':self._key,'APCA-API-SECRET-KEY':self._secret,'Content-Type':'application/json'})
        except Exception as error:
            record(dict(phase='not_transported',request_id=request_id,method=method,path=path,
                        error_type=type(error).__name__))
            raise NotTransported(request_id,type(error).__name__) from None
        try:
            try:
                response=self._opener.open(request,timeout=self.timeout)
            except HTTPError as error:
                response=error
            with response:
                status=response.status if hasattr(response,'status') else response.code
                body=response.read(self.max_bytes+1)
        except Exception as error:
            record(dict(phase='transport_unknown',request_id=request_id,method=method,path=path,
                        error_type=type(error).__name__))
            raise RuntimeError('native_paper_transport_unknown_no_resubmit') from None
        if self._key.encode() in body or self._secret.encode() in body:
            record(dict(phase='credential_echo_refused',request_id=request_id,method=method,path=path,status=status))
            raise ValueError('native_paper_credential_echo_refused')
        complete=len(body)<=self.max_bytes
        record(dict(phase='response',request_id=request_id,method=method,path=path,status=status,
                    complete=complete,body_hex=body.hex(),body_sha256=hashlib.sha256(body).hexdigest()))
        if not complete:raise ValueError('native_paper_response_resource_capacity')
        return BrokerResponse(status,body)
