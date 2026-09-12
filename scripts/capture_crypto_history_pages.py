"""Resumable all-symbol crypto trade history capture; PAPER-pinned GETs only."""
from contextlib import contextmanager
from dataclasses import asdict
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.crypto_history_pages import HistoryRequest, HistoryWalk, canonical, sha


@contextmanager
def capture_lock(directory):
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'owner.lock').open('a+b') as f:
        if f.seek(0,2)==0:
            f.write(b'0'); f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Closing the descriptor releases ownership, including after a crash.
        yield


def durable(path, value, mode='a'):
    with path.open(mode,encoding='utf-8',newline='\n') as f:
        f.write(canonical(value)+'\n'); f.flush(); os.fsync(f.fileno())


class HistoryCapture:
    """Raw HTTP evidence precedes decode; restart verifies the entire chain."""
    def __init__(self,directory,request,*,max_pages,max_trades,max_page_bytes):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.walk=HistoryWalk(request,max_pages=max_pages,max_trades=max_trades,max_page_bytes=max_page_bytes)
        self.max_page_bytes=max_page_bytes
        self.record_count=0
        self.last_received_ns=None
        self.meta=dict(contract='crypto_history_capture_v1',request=asdict(request),
            resources=dict(max_pages=max_pages,max_trades=max_trades,max_page_bytes=max_page_bytes),
            decoder_sha256={p:sha((Path(__file__).resolve().parent/p).read_bytes())
                for p in ('crypto_history_pages.py','crypto_trade_frames.py')})
        meta_path=self.directory/'metadata.json'
        if meta_path.exists():
            if canonical(json.loads(meta_path.read_bytes())) != canonical(self.meta):
                raise ValueError('crypto_history_capture_binding_changed')
        else:
            if (self.directory/'responses.jsonl').exists():
                raise ValueError('crypto_history_capture_metadata_missing')
            durable(meta_path,self.meta,'x')
        self.record_root=sha(canonical(self.meta))
        records=self.directory/'responses.jsonl'
        if records.exists():
            with records.open('rb') as f:
                while raw:=f.readline(max_page_bytes*6+65536):
                    if not raw.endswith(b'\n'):
                        raise ValueError('crypto_history_capture_torn_or_oversized_record')
                    record=json.loads(raw)
                    self._replay(record)

    def _replay(self,record):
        digest=record.get('record_sha256')
        body={k:v for k,v in record.items() if k!='record_sha256'}
        if type(body.get('status')) is not int or not 100<=body['status']<=599:
            raise ValueError('crypto_history_response_status_invalid')
        if (digest!=sha(canonical(body)) or body['previous_record_sha256']!=self.record_root or
                body['sequence']!=self.record_count+1 or body['parameters']!=self.walk.parameters() or
                body['request_sha256']!=self.walk.request.identity or sha(body['raw'])!=body['raw_sha256'] or
                len(body['raw'].encode())>self.max_page_bytes):
            raise ValueError('crypto_history_capture_record_mismatch')
        start,received=body['started_ns'],body['received_ns']
        if (type(start) is not int or type(received) is not int or start<=0 or received<start or
                self.last_received_ns is not None and start<self.last_received_ns):
            raise ValueError('crypto_history_capture_transport_clock_invalid')
        self.record_count+=1
        self.record_root=digest
        self.last_received_ns=received
        if body['status']==200:
            self.walk.accept(body['raw'],received_ns=received)

    def record_response(self,*,raw,status,started_ns,received_ns,headers=None):
        if type(raw) is not str or len(raw.encode())>self.max_page_bytes:
            raise ValueError('crypto_history_response_capacity')
        if type(status) is not int or not 100<=status<=599:
            raise ValueError('crypto_history_response_status_invalid')
        # A transport response, including an error, is retained before decoder
        # inspection. A malformed 200 remains a visible unresolved raw record.
        record=dict(sequence=self.record_count+1,previous_record_sha256=self.record_root,
            request_sha256=self.walk.request.identity,parameters=self.walk.parameters(),
            raw=raw,raw_sha256=sha(raw),status=status,started_ns=started_ns,received_ns=received_ns,
            rate_headers={k:v for k,v in (headers or {}).items() if k.lower().startswith('x-ratelimit')})
        record['record_sha256']=sha(canonical(record))
        durable(self.directory/'responses.jsonl',record)
        self._replay(record)
        return self.receipt()

    def receipt(self):
        return dict(self.walk.receipt(),http_responses=self.record_count,record_root_sha256=self.record_root)


def collect(capture,get_response,*,max_wall_seconds,clock_ns=time.time_ns,monotonic=time.monotonic):
    """One collection attempt; HTTP failure leaves the same token resumable."""
    if type(max_wall_seconds) not in (int,float) or not 0<max_wall_seconds<float('inf'):
        raise ValueError('crypto_history_collection_budget_invalid')
    deadline=monotonic()+max_wall_seconds
    while not capture.walk.complete:
        if monotonic()>=deadline:
            return dict(capture.receipt(),reason='capture_wall_resource_limit')
        params=capture.walk.parameters()
        started=clock_ns()
        status,raw,headers=get_response(capture.walk.request.url,params)
        capture.record_response(raw=raw,status=status,headers=headers,started_ns=started,received_ns=clock_ns())
        if status!=200:
            return dict(capture.receipt(),reason='http_'+str(status))
    return dict(capture.receipt(),reason='provider_page_chain_exhausted')


def main():
    from dotenv import dotenv_values
    import requests
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-file',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--max-pages',type=int,required=True)
    parser.add_argument('--max-trades',type=int,required=True)
    parser.add_argument('--max-page-bytes',type=int,required=True)
    parser.add_argument('--max-wall-seconds',type=float,required=True)
    parser.add_argument('--http-timeout-seconds',type=float,required=True)
    args=parser.parse_args()
    if not math.isfinite(args.http_timeout_seconds) or args.http_timeout_seconds<=0:
        raise ValueError('crypto_history_http_budget_invalid')
    data=json.loads(args.request_file.read_bytes()); data['symbols']=tuple(data['symbols'])
    request=HistoryRequest(**data)
    env=dotenv_values(args.env_file)
    if str(env.get('CHILI_ALPACA_PAPER','')).lower() not in ('1','true'):
        raise ValueError('crypto_history_requires_paper_configuration')
    expected=env.get('CHILI_ALPACA_EXPECTED_ACCOUNT_ID')
    key,secret=env.get('CHILI_ALPACA_API_KEY'),env.get('CHILI_ALPACA_API_SECRET')
    if not expected or not key or not secret: raise ValueError('crypto_history_paper_credentials_missing')
    headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret}
    with requests.Session() as session:
        def get(url,params):
            if url not in (request.url,'https://paper-api.alpaca.markets/v2/account'):
                raise ValueError('crypto_history_endpoint_not_allowed')
            with session.get(url,params=params,headers=headers,timeout=args.http_timeout_seconds,
                             allow_redirects=False,stream=True) as r:
                chunks=[]; size=0
                for chunk in r.iter_content(chunk_size=65536):
                    size+=len(chunk)
                    if size>args.max_page_bytes: raise ValueError('crypto_history_response_capacity')
                    chunks.append(chunk)
                raw=b''.join(chunks).decode('utf-8')
                if key in raw or secret in raw: raise ValueError('crypto_history_credential_echo_refused')
                return r.status_code,raw,dict(r.headers)
        def account():
            status,raw,_=get('https://paper-api.alpaca.markets/v2/account',None)
            if status!=200 or json.loads(raw).get('id')!=expected:
                raise ValueError('crypto_history_paper_account_mismatch')
        with capture_lock(args.output_dir):
            account()
            capture=HistoryCapture(args.output_dir,request,max_pages=args.max_pages,
                max_trades=args.max_trades,max_page_bytes=args.max_page_bytes)
            result=collect(capture,get,max_wall_seconds=args.max_wall_seconds)
            account()
            result.update(account_identity_verified=True,orders_submitted=False)
            durable(args.output_dir/'observations.jsonl',result)
            print(canonical(result))


if __name__=='__main__':
    main()
