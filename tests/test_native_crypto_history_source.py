from dataclasses import replace
from decimal import Decimal
import json
from uuid import uuid4
import pytest

from app.crypto_execution.history_source import (QuoteHistoryWalk,CryptoDataHTTP,DataHTTPError,
    collect_history_batch,batch_messages)
from app.crypto_execution.tick_context import CryptoTickContext
from app.tick_math.structural_prefix import Limits
from scripts.crypto_history_pages import HistoryRequest
from scripts.crypto_trade_frames import timestamp_ns

BASE=timestamp_ns('2026-09-12T00:00:00Z')
SYMBOLS=('BTC/USD','ETH/USD')
def request(start=0,end=100):return HistoryRequest('us',SYMBOLS,BASE+start,BASE+end,100,'a'*64)
def q(i,bid='9',ask='11'):return dict(t=f'2026-09-12T00:00:00.{i:09d}Z',bp=bid,ap=ask,bs='1',**{'as':'1'})
def t(i,price='10',tid=1):return dict(t=f'2026-09-12T00:00:00.{i:09d}Z',p=price,s='0.000000001',i=tid,tks='B')
def make_batch(req=None,*,trades=(),quotes=(),quote_pages=None,record=None):
    req=req or request();seen=[]
    payloads={'trades':[dict(trades={'BTC/USD':list(trades)},next_page_token=None)],
              'quotes':quote_pages or [dict(quotes={'BTC/USD':list(quotes)},next_page_token=None)]}
    def get(**args):
        kind=args['kind'];seen.append((kind,args['parameters']))
        raw=json.dumps(payloads[kind].pop(0)).encode();received=BASE+1000+len(seen)
        args['record'](dict(kind='response',data=raw.hex()))
        return raw,received
    batch=collect_history_batch(req,get,record=record or (lambda _:None),max_pages=10,max_trades=100,max_quotes=100,max_page_bytes=65536)
    return batch,seen
def context():
    return CryptoTickContext(assets=[dict(id=str(uuid4()),symbol=s,**{'class':'crypto'},status='active',tradable=True) for s in SYMBOLS],
        location='us',connection_id='rest-epoch',limits=Limits(100,100,100),max_frame_bytes=65536,max_pending_quotes=100,source_kind='rest_pages')

def test_all_symbols_survive_short_and_empty_pages_until_token_exhaustion():
    pages=[dict(quotes={},next_page_token='next'),dict(quotes={'ETH/USD':[q(1)]},next_page_token=None)]
    batch,seen=make_batch(trades=[t(2)],quote_pages=pages)
    assert [kind for kind,_ in seen]==['trades','quotes','quotes']
    assert seen[-1][1]['page_token']=='next'
    assert all(params['symbols']=='BTC/USD,ETH/USD' for _,params in seen)
    assert batch.quote_receipt['quote_counts']=={'BTC/USD':0,'ETH/USD':1}
    assert batch.trade_receipt['trade_counts']=={'BTC/USD':1,'ETH/USD':0}
    assert len(batch_messages(batch))==2

def test_rest_batch_does_not_invent_a_websocket_subscription_or_equal_time_quote_order():
    batch,_=make_batch(trades=[t(2),t(3,tid=2)],quotes=[q(1),q(2,'19','21')])
    book=context();result=book.append_rest_batch(batch,published_ns=BASE+2000)
    view=book.view('BTC/USD');p=book.prefixes['BTC/USD']
    assert result['subscribed'] is None and view.source_kind=='rest_pages'
    assert view.coverage=='observed_rest_page_prefix'
    assert p.tick(0).bid==9 and p.tick(1).bid==19
    assert view.last_binding.quote_basis=='prior_event_quote_in_completed_rest_observation'
    with pytest.raises(ValueError,match='websocket_source_required'):
        book.append('[]',frame_sequence=2,received_ns=BASE+3000,published_ns=BASE+3001)

def test_empty_observation_advances_source_without_fabricating_prints():
    batch,_=make_batch();book=context();old=book.root
    result=book.append_rest_batch(batch,published_ns=BASE+2000)
    assert book.root!=old and book.sequence==1
    assert result['prints']=={'BTC/USD':0,'ETH/USD':0}
    assert all(book.view(s).print_count==0 for s in SYMBOLS)

def test_transport_batch_boundaries_do_not_reset_structural_history():
    first,_=make_batch(request(0,100),trades=[t(90)],quotes=[q(80)])
    second,_=make_batch(request(100,200),trades=[t(150,'12',tid=2)],quotes=[q(140,'11','13')])
    book=context();book.append_rest_batch(first,published_ns=BASE+2000)
    book.append_rest_batch(second,published_ns=BASE+4000)
    assert book.view('BTC/USD').print_count==2
    assert book.prefixes['BTC/USD'].tick(0).price==10

@pytest.mark.parametrize('fault',['gap','membership','content'])
def test_bad_batch_cannot_advance_current_context(fault):
    batch,_=make_batch();book=context();book.append_rest_batch(batch,published_ns=BASE+2000);root=book.root
    next_batch,_=make_batch(request(101 if fault=='gap' else 100,200))
    if fault=='membership':next_batch=replace(next_batch,request=replace(next_batch.request,symbols=('BTC/USD',)))
    if fault=='content':next_batch=replace(next_batch,content_sha256='f'*64)
    with pytest.raises(ValueError):book.append_rest_batch(next_batch,published_ns=BASE+4000)
    assert book.root==root and book.view('BTC/USD').coverage.startswith('unavailable:')

@pytest.mark.parametrize('fault',['cycle','unordered','outside','unrequested','overflow'])
def test_quote_walk_refuses_bad_pages_without_partial_progress(fault):
    walk=QuoteHistoryWalk(request(),max_pages=10,max_quotes=1 if fault=='overflow' else 100,max_page_bytes=65536)
    walk.accept(json.dumps(dict(quotes={},next_page_token='next')),received_ns=BASE+1000)
    before=(walk.root,walk.page_count)
    quotes={'BTC/USD':[q(2)]};token=None
    if fault=='cycle':token='next'
    if fault=='unordered':quotes={'BTC/USD':[q(2),q(1)]}
    if fault=='outside':quotes={'BTC/USD':[q(101)]}
    if fault=='unrequested':quotes={'DOGE/USD':[q(2)]}
    if fault=='overflow':quotes={'BTC/USD':[q(1),q(2)]}
    with pytest.raises(ValueError):walk.accept(json.dumps(dict(quotes=quotes,next_page_token=token)),received_ns=BASE+2000)
    assert before==(walk.root,walk.page_count)

def test_failed_durable_recording_cannot_produce_a_complete_batch():
    def fail(_):raise OSError('fixture fsync failure')
    with pytest.raises(OSError):make_batch(trades=[t(2)],record=fail)

def test_data_client_pins_host_get_and_records_error_before_raising():
    class Response:
        status=429;headers={'Retry-After':'1','X-RateLimit-Remaining':'0'}
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,_):return b'{"message":"rate limit"}'
    class Opener:
        def open(self,request,**_):
            assert request.get_method()=='GET'
            assert request.full_url.startswith('https://data.alpaca.markets/v1beta3/crypto/us/quotes?')
            return Response()
    client=CryptoDataHTTP(paper=True,key='fixture-key-123',secret='fixture-secret-123',timeout_seconds=1,max_response_bytes=1000)
    client._opener=Opener();records=[]
    with pytest.raises(DataHTTPError) as error:
        client.get(location='us',kind='quotes',parameters=request().parameters(),record=records.append)
    assert error.value.status==429 and records[-1]['status']==429
    assert records[-1]['body_hex']==b'{"message":"rate limit"}'.hex()
    with pytest.raises(ValueError):client.get(location='us',kind='orders',parameters={},record=records.append)
