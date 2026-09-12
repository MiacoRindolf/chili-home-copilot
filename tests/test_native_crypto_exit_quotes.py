"""Fresh PAPER sale pricing cannot create a signal or bypass durable ownership."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.crypto_execution.execution_quotes import ExitPriceQuote,ExitQuoteHTTP
from app.crypto_execution.selection import NativeTickDecisionReader
from tests.test_native_crypto_selection import context,add,FEES,BASE
from tests.test_native_crypto_lifecycle import ASSET,CYCLE,INSTRUCTION
from tests.test_native_crypto_owner import store,reserve,Broker,Response,owner
from tests.test_native_crypto_host import NativePaperHost,config,settings


def quote():
    return ExitPriceQuote('BTC/USD','60001.239','60002.5','2','3',BASE,BASE+1,BASE+2,'a'*64)


def reader(b,records,quote_reader,source=None,asset=ASSET):
    return NativeTickDecisionReader(context_reader=source or (lambda _:b.view('BTC/USD')),
        asset_reader=lambda _:asset,fee_reader=lambda:FEES,record=records.append,
        exit_quote_reader=quote_reader)


def test_unarmed_entry_and_new_tick_exit_never_require_quote_transport():
    b,_=context(shift=59921);records=[]
    def forbidden(*args):raise AssertionError('unarmed quote read')
    r=reader(b,records,forbidden)
    state=dict(cycle_id=CYCLE,asset=ASSET,instruction=INSTRUCTION,exit_requested=False)
    assert r(state).entry_allowed
    assert not r(dict(state,instruction=dict(INSTRUCTION,limit_price='60001'))).entry_allowed
    add(b,59998)
    assert r(state).exit_requested
    r.context_reader=lambda _:None
    assert r(state) is None


def test_owner_arms_before_quote_failure_then_exits_without_source_or_new_signal(store):
    b,_=context(shift=59921);reserve(store);external=Broker();events=[];fail={'on':True}
    def latest(asset,record):
        assert store.read(CYCLE)['exit_requested'], 'trigger was not durable before quote I/O'
        assert store.read(CYCLE)['position_known']
        record(dict(phase='fixture_quote_response',body_sha256=quote().body_sha256))
        if fail['on']:raise ValueError('fixture_quote_unavailable')
        return quote()
    r=reader(b,events,latest)
    r.record=lambda value:store.record_evidence(CYCLE,value)
    driver=owner(store,external,decision=r)
    driver.step(CYCLE);driver.step(CYCLE)
    add(b,59998)
    with pytest.raises(ValueError,match='fixture_quote_unavailable'):driver.step(CYCLE)
    state=store.audit(CYCLE,max_events=150)
    trigger=state['exit_context_sha256']
    assert state['exit_requested'] and not external.sells
    # Even an unavailable source must not erase the retained full-sale intent.
    r.context_reader=lambda _: (_ for _ in ()).throw(AssertionError('armed exit revisited source'))
    fail['on']=False
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert driver.step(CYCLE)['closed']
    sells=[v for method,_,v in external.calls if method=='POST' and v['side']=='sell']
    assert [v['qty'] for v in sells]==['0.0000399','0.0000199']
    assert all(v['limit_price']=='60001.2' for v in sells)
    assert len({v['client_order_id'] for v in sells})==2
    state=store.audit(CYCLE,max_events=200)
    assert state['exit_context_sha256']==trigger and state['closed']


def test_armed_order_reconciliation_does_not_wait_for_quote_provider(store):
    b,_=context(shift=59921);reserve(store);external=Broker();signal={'exit':False}
    from app.crypto_execution.owner import TickDecision
    driver=owner(store,external,decision=lambda _:TickDecision('b'*64,True,signal['exit'],'60001'))
    driver.step(CYCLE);driver.step(CYCLE);signal['exit']=True
    external.raise_after_accept=True
    with pytest.raises(RuntimeError,match='unknown_no_resubmit'):driver.step(CYCLE)
    def forbidden(*args):raise AssertionError('quote must not block unknown-POST reconciliation')
    r=reader(b,[],forbidden,source=forbidden)
    r.record=lambda value:store.record_evidence(CYCLE,value)
    assert owner(store,external,decision=r).step(CYCLE)['outcome']=='exit_reconciled'
    assert len(external.sells)==1


def test_receipt_failure_prevents_sale_and_preserves_intent(store):
    b,_=context(shift=59921);reserve(store);external=Broker()
    r=reader(b,[],lambda asset,record:quote());driver=owner(store,external,decision=r)
    driver.step(CYCLE);driver.step(CYCLE);add(b,59998)
    def record(value):
        if 'exit_execution_quote' in value.get('selection',{}):raise OSError('receipt unavailable')
        store.record_evidence(CYCLE,value)
    r.record=record
    with pytest.raises(OSError):driver.step(CYCLE)
    assert store.read(CYCLE)['exit_requested'] and not external.sells


def test_armed_asset_uuid_mismatch_is_rejected_before_source_or_quote():
    b,_=context();r=reader(b,[],lambda *args:quote(),asset=dict(ASSET,id=str(uuid4())))
    state=dict(cycle_id=CYCLE,asset=ASSET,instruction=INSTRUCTION,exit_requested=True)
    with pytest.raises(ValueError,match='cycle_asset_changed'):r(state)


def transport(raw,status=200,headers=None,before=None,max_bytes=65536):
    events=[];order=[]
    client=ExitQuoteHTTP(paper=True,key='fixture-public-key-12345',secret='fixture-private-key-12345',
        before_read=before or (lambda:order.append('authority')),
        observe_rate=lambda s,h:order.append(('rate',s,h)),timeout_seconds=1,max_response_bytes=max_bytes)
    class Opener:
        def open(self,request,timeout):
            assert request.get_method()=='GET'
            assert request.full_url=='https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD'
            order.append('http')
            response=Response(status,raw);response.headers=headers or {};return response
    client.opener=Opener()
    def record(value):events.append(value);order.append('retained')
    return client,record,events,order


def payload(**changes):
    q=dict(bp='60001.239',ap='60002.5',bs=2,**{'as':3},t='2026-09-12T00:00:00Z')
    q.update(changes)
    return json.dumps(dict(quotes={'BTC/USD':q})).encode()


def test_transport_retains_exact_raw_before_decode_and_rate_observation():
    raw=payload();client,record,events,order=transport(raw,headers={'X-RateLimit-Remaining':'10'})
    q=client(ASSET,record)
    assert q.price(ASSET)=='60001.2' and q.bid_size=='2'
    assert bytes.fromhex(events[0]['body_hex'])==raw
    assert order==['authority','http','retained',('rate',200,{'x-ratelimit-remaining':'10'})]
    assert not q.receipt()['fill_certified'] and not q.receipt()['account_execution_venue_certified']


@pytest.mark.parametrize('raw',[b'[]',b'{"quotes":{}}',b'{"quotes":{"BTC/USD":[]}}',b'{bad',
    payload(bp='60003'),payload(bs='0'),payload(S='ETH/USD'),payload(t='2999-01-01T00:00:00Z'),
    payload(bp='NaN'),payload(bp=True)])
def test_invalid_payload_is_retained_and_never_becomes_price(raw):
    client,record,events,_=transport(raw)
    with pytest.raises(ValueError,match='payload_invalid'):client(ASSET,record)
    assert bytes.fromhex(events[0]['body_hex'])==raw


@pytest.mark.parametrize('status',[401,429,500])
def test_http_errors_retained_but_not_parsed_as_quote(status):
    client,record,events,order=transport(payload(),status,{'Retry-After':'2'})
    with pytest.raises(ValueError,match='http_'+str(status)):client(ASSET,record)
    assert events[0]['status']==status and order[-1]==('rate',status,{'retry-after':'2'})


def test_credential_echo_is_not_retained_and_capacity_is_not_accepted():
    client,record,events,_=transport(b'fixture-private-key-12345')
    with pytest.raises(ValueError,match='credential_echo'):client(ASSET,record)
    assert not events
    client,record,events,_=transport(payload(),max_bytes=10)
    with pytest.raises(ValueError,match='response_capacity'):client(ASSET,record)
    assert not events[0]['complete']


def test_authority_failure_never_reads_provider():
    def denied():raise ValueError('owner_lost')
    client,record,events,order=transport(payload(),before=denied)
    with pytest.raises(ValueError,match='owner_lost'):client(ASSET,record)
    assert not events and not order


def test_data_wait_rechecks_authority_and_obeys_extended_shared_deadline(tmp_path,monkeypatch):
    import app.crypto_execution.host as module
    host=NativePaperHost(None,settings(),config(tmp_path),'b'*64)
    host.broker=SimpleNamespace(account_id='fixture');seen=[];clock=[100.]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    host._authority=lambda _:seen.append(clock[0])
    host._before_data_read();assert seen==[100.]
    host._data_rate(429,{'Retry-After':'2'})
    host._data_rate(429,{'Retry-After':'1'})
    assert host.next_data==102.
    def wait(delay):
        clock[0]+=delay
        if clock[0]==102.:host._data_rate(429,{'Retry-After':'3'})
        return False
    monkeypatch.setattr(host.stop,'wait',wait)
    host._before_data_read()
    assert seen==[100.,100.,102.,105.]
    host.stop.set()
    with pytest.raises(ValueError,match='stopping'):host._before_data_read()


def test_unknown_data_rate_reset_blocks_subsequent_reads(tmp_path):
    host=NativePaperHost(None,settings(),config(tmp_path),'b'*64)
    host.broker=SimpleNamespace(account_id='fixture');host._authority=lambda _:None
    with pytest.raises(ValueError,match='data_rate_reset_missing'):host._data_rate(429,{})
    with pytest.raises(ValueError,match='data_rate_reset_missing'):host._before_data_read()
