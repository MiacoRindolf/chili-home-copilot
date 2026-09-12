"""Bounded PAPER-pinned, all-native-symbol market-data capture; no orders.

Duration, polling, HTTP pages and storage limits are transport/resource budgets.
They never reset the accumulated tick prefix or select/rank trading symbols.
The same directory resumes its verified committed watermark under an OS lock.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.request import Request,build_opener
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.crypto_execution.history_source import CryptoDataHTTP,DataHTTPError
from app.crypto_execution.paper_http import NoRedirect
from app.crypto_execution.source_capture import NativeSourceCapture,source_metadata
from scripts.capture_crypto_history_pages import capture_lock
from scripts.crypto_history_pages import canonical,sha


def durable(path,value,mode='a'):
    with path.open(mode,encoding='utf-8',newline='\n') as f:
        f.write(canonical(value)+'\n');f.flush();os.fsync(f.fileno())


def rate_deadline(headers,now):
    """Use provider reset evidence only; absent evidence cannot invent a retry."""
    h={k.lower():v for k,v in headers.items()}
    deadlines=[]
    if 'retry-after' in h:
        try:deadlines.append(now+float(h['retry-after']))
        except ValueError:pass
    if 'x-ratelimit-reset' in h:
        try:deadlines.append(float(h['x-ratelimit-reset']))
        except ValueError:pass
    return max((x for x in deadlines if math.isfinite(x) and x>now),default=None)


def run(args):
    from dotenv import dotenv_values
    directory=Path(args.directory).resolve();env=dotenv_values(args.env_file)
    if str(env.get('CHILI_ALPACA_PAPER','')).lower() not in ('true','1'):
        raise ValueError('native_capture_paper_configuration_required')
    key,secret,expected=(env.get(k) for k in
        ('CHILI_ALPACA_API_KEY','CHILI_ALPACA_API_SECRET','CHILI_ALPACA_EXPECTED_ACCOUNT_ID'))
    if not all((key,secret,expected)):raise ValueError('native_capture_account_configuration_missing')
    resources=json.loads(Path(args.resources).read_bytes())
    opener=build_opener(NoRedirect());headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret}
    with capture_lock(directory):
        def broker_get(path):
            start=time.time_ns()
            with opener.open(Request('https://paper-api.alpaca.markets/v2/'+path,headers=headers,method='GET'),timeout=20) as r:
                raw=r.read(resources['max_page_bytes']+1);status=r.status
            if key.encode() in raw or secret.encode() in raw:raise ValueError('native_capture_credential_echo')
            if status!=200 or len(raw)>resources['max_page_bytes']:raise ValueError('native_capture_broker_response')
            durable(directory/'broker_reads.jsonl',dict(path=path,status=status,started_ns=start,received_ns=time.time_ns(),
                body_hex=raw.hex(),body_sha256=sha(raw)))
            return json.loads(raw),sha(raw)
        account,_=broker_get('account')
        if str(account.get('id'))!=expected:raise ValueError('native_capture_paper_account_changed')
        assets,inventory_sha=broker_get('assets?status=active&asset_class=crypto')
        if type(assets) is not list:raise ValueError('native_capture_inventory_shape')
        assets=[a for a in assets if a.get('class')=='crypto' and a.get('status')=='active' and a.get('tradable') is True]
        metadata_path=directory/'metadata.json'
        if metadata_path.exists():
            metadata=json.loads(metadata_path.read_bytes())
            old={a['symbol']:a for a in metadata['assets']};new={a['symbol']:a for a in assets}
            if old!=new or metadata['location']!=args.location or metadata['resources']!=resources:
                raise ValueError('native_capture_membership_or_resources_changed')
        else:
            metadata=source_metadata(assets=assets,location=args.location,source_id=str(uuid4()),anchor_ns=time.time_ns(),
                inventory_sha256=inventory_sha,page_limit=10000,resources=resources)
        capture=NativeSourceCapture(directory,metadata)
        durable(directory/'runs.jsonl',dict(started_ns=time.time_ns(),pid=os.getpid(),
            account_identity_sha256=sha(expected),runner_sha256=sha(Path(__file__).read_bytes()),
            duration_seconds=args.duration_seconds,poll_seconds=args.poll_seconds,
            duration_basis='operator_bounded_data_experiment',poll_basis='HTTP_transport_budget_only',order_authority=False))
        client=CryptoDataHTTP(paper=True,key=key,secret=secret,timeout_seconds=20,max_response_bytes=resources['max_page_bytes'])
        stop=time.monotonic()+args.duration_seconds;next_allowed=0.0
        def wait_transport(deadline):
            while time.time()<deadline:
                if time.monotonic()>=stop:raise TimeoutError('native_capture_duration_exhausted')
                time.sleep(min(1,deadline-time.time()))
        def get(**kwargs):
            nonlocal next_allowed
            wait_transport(next_allowed)
            record=kwargs.pop('record')
            def retained(value):
                nonlocal next_allowed
                record(value)
                h={k.lower():v for k,v in value.get('rate_headers',{}).items()}
                if h.get('x-ratelimit-remaining')=='0':
                    deadline=rate_deadline(h,time.time())
                    if deadline is None:raise ValueError('native_capture_rate_reset_missing')
                    next_allowed=deadline
            return client.get(**kwargs,record=retained)
        error=None
        try:
            while time.monotonic()<stop:
                try:
                    result=capture.observe(time.time_ns(),get)
                except DataHTTPError as exc:
                    retry=rate_deadline(exc.headers,time.time())
                    if exc.status!=429 or retry is None:raise
                    next_allowed=retry;wait_transport(retry);continue
                durable(directory/'observations.jsonl',dict(at_ns=time.time_ns(),**result))
                print(canonical(dict(revision=result['revision'],symbols=result['requested_symbol_count'],
                    prints=sum(result['print_counts'].values()),new_prints=result['new_prints'],valid=result['valid'])),flush=True)
                time.sleep(min(args.poll_seconds,max(0,stop-time.monotonic())))
        except Exception as exc:
            error=dict(type=type(exc).__name__)
            # Emit only our closed-form local codes; never arbitrary HTTP text.
            code=str(exc)
            if code.startswith(('native_','crypto_')) and all(c.isalnum() or c in '_:' for c in code):error['code']=code
            raise
        finally:
            durable(directory/'runs.jsonl',dict(ended_ns=time.time_ns(),error=error,status=capture.status()))
            print(canonical(dict(ended=True,error=error,status=capture.status())),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('directory','env-file','resources'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--location',choices=('us','us-1','eu-1'),required=True)
    parser.add_argument('--duration-seconds',type=float,required=True)
    parser.add_argument('--poll-seconds',type=float,required=True)
    args=parser.parse_args()
    if any(not math.isfinite(v) or v<=0 for v in (args.duration_seconds,args.poll_seconds)):
        parser.error('positive finite transport budgets required')
    try:run(args)
    except Exception as exc:
        print(canonical(dict(capture_failed=type(exc).__name__)),flush=True)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
