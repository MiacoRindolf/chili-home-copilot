from dataclasses import asdict
import json
from uuid import uuid4
import pytest

from app.crypto_execution.source_capture import NativeSourceCapture,source_metadata
from app.crypto_execution.history_source import DataHTTPError
from scripts.crypto_history_pages import canonical,sha
from tests.test_native_crypto_history_source import BASE,SYMBOLS,q,t

def metadata():
    return source_metadata(assets=[dict(id=str(uuid4()),symbol=s,**{'class':'crypto'},status='active',tradable=True) for s in SYMBOLS],
        location='us',source_id=str(uuid4()),anchor_ns=BASE,inventory_sha256='a'*64,page_limit=100,
        resources=dict(max_pages=10,max_trades=100,max_quotes=100,max_page_bytes=65536,max_normalized_bytes=65536,
            retained_ticks=100,frontier_ticks=100,active_references=100,max_pending_quotes=100,max_journal_bytes=1048576))

def getter(*,trade_rows,quote_rows,fail_kind=None):
    calls=[]
    def get(*,location,kind,parameters,record):
        calls.append((kind,parameters))
        status=429 if kind==fail_kind else 200
        body=(dict(message='limit') if status!=200 else
              {kind:{'BTC/USD':trade_rows if kind=='trades' else quote_rows},'next_page_token':None})
        raw=canonical(body).encode();received=BASE+10000+len(calls)
        record(dict(kind='http_response',path='/v1beta3/crypto/'+location+'/'+kind,parameters=parameters,
            started_ns=received-1,received_ns=received,status=status,complete=True,body_hex=raw.hex(),body_sha256=sha(raw),rate_headers={}))
        if status!=200:raise DataHTTPError(status,{})
        return raw,received
    return get,calls

def capture(tmp_path,meta=None):
    return NativeSourceCapture(tmp_path,meta or metadata(),clock_ns=lambda:BASE+100000)

def test_continuous_watermark_and_exact_context_recover_from_actual_retained_responses(tmp_path):
    m=metadata();c=capture(tmp_path,m)
    first,calls=getter(trade_rows=[t(90)],quote_rows=[q(80)])
    c.observe(BASE+100,first)
    old=c.current_views()
    second,seen=getter(trade_rows=[t(150,'12',tid=2)],quote_rows=[q(140,'11','13')])
    c.observe(BASE+200,second)
    assert seen[0][1]['start'].endswith('.000000100Z')
    assert c.status()['print_counts']=={'BTC/USD':2,'ETH/USD':0}
    assert old[0].print_count==1 and old[1].print_count==0
    restored=capture(tmp_path,m)
    assert restored.current_views()==c.current_views()
    assert restored.root==c.root and restored.end_ns==BASE+200 and restored.revision==2
    assert not restored.status()['order_authority']

def test_failed_page_does_not_advance_watermark_or_allow_stale_current_view(tmp_path):
    m=metadata();c=capture(tmp_path,m)
    first,_=getter(trade_rows=[t(90)],quote_rows=[q(80)]);c.observe(BASE+100,first)
    failed,_=getter(trade_rows=[t(150,'12',tid=2)],quote_rows=[],fail_kind='quotes')
    with pytest.raises(DataHTTPError):c.observe(BASE+200,failed)
    assert c.end_ns==BASE+100 and c.revision==1
    with pytest.raises(ValueError,match='current_observation_unavailable'):c.current_views()
    restored=capture(tmp_path,m)
    assert not restored.valid and restored.end_ns==BASE+100
    retry,calls=getter(trade_rows=[t(150,'12',tid=2),t(250,'13',tid=3)],quote_rows=[q(140,'11','13'),q(240,'12','14')])
    restored.observe(BASE+300,retry)
    assert calls[0][1]['start'].endswith('.000000100Z')
    assert restored.current_views()[0].print_count==3

def test_input_only_crash_is_not_a_published_observation_and_can_be_recollected(tmp_path):
    m=metadata();c=capture(tmp_path,m)
    get,_=getter(trade_rows=[t(90)],quote_rows=[q(80)]);c.observe(BASE+100,get)
    path=tmp_path/'source.jsonl';lines=path.read_bytes().splitlines(keepends=True)
    assert json.loads(lines[-1])['body']['kind']=='context_published'
    path.write_bytes(b''.join(lines[:-1]))
    restored=capture(tmp_path,m)
    assert restored.revision==0 and restored.end_ns==BASE and not restored.valid
    again,_=getter(trade_rows=[t(90)],quote_rows=[q(80)]);restored.observe(BASE+100,again)
    assert restored.current_views()[0].print_count==1

@pytest.mark.parametrize('fault',['raw','view','torn','implementation'])
def test_recovery_rejects_changed_or_incomplete_evidence(tmp_path,fault):
    m=metadata();c=capture(tmp_path,m)
    get,_=getter(trade_rows=[t(90)],quote_rows=[q(80)]);c.observe(BASE+100,get)
    path=tmp_path/'source.jsonl';lines=path.read_bytes().splitlines()
    if fault=='implementation':
        m['implementation_sha256']['tick_context.py']='0'*64
    elif fault=='torn':path.write_bytes(b'\n'.join(lines)+b'\n{"partial":')
    elif fault=='raw':path.write_bytes(path.read_bytes().replace(b'http_response',b'http_responsX',1))
    else:
        records=[json.loads(v) for v in lines]
        records[-1]['body']['view']['symbols']['BTC/USD']['print_count']=999
        prev=sha(canonical(m))
        for r in records:
            r.pop('root');r['previous']=prev;r['root']=sha(canonical(r));prev=r['root']
        path.write_bytes(b''.join((canonical(r)+'\n').encode() for r in records))
    with pytest.raises(ValueError):capture(tmp_path,m)

def test_failed_context_publication_never_exposes_new_view(tmp_path,monkeypatch):
    c=capture(tmp_path);real=c._record
    def fail(body):
        if body['kind']=='context_published':raise OSError('fsync fixture')
        return real(body)
    monkeypatch.setattr(c,'_record',fail)
    get,_=getter(trade_rows=[t(90)],quote_rows=[q(80)])
    with pytest.raises(OSError):c.observe(BASE+100,get)
    assert c.revision==0 and not c.valid
    with pytest.raises(ValueError):c.current_views()
