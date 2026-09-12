"""Exercise the real cycle owner + SQL journal + PAPER HTTP boundary, with no orders on Alpaca."""
from contextlib import contextmanager
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.crypto_execution.lifecycle import exposure, next_action
from app.crypto_execution.owner import NativeCycleOwner, TickDecision
from app.crypto_execution.paper_http import PaperCycleHTTP, NotTransported
from tests.test_native_crypto_cycle_store import store, admission, reserve, another
from tests.test_native_crypto_lifecycle import ACCOUNT, ASSET, CYCLE, INSTRUCTION, order, position


class Response:
    def __init__(self,status,body): self.status=status;self.body=body
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,limit): return self.body[:limit]


class Broker:
    """Deterministic external broker, independent of the owner's expected transitions."""
    def __init__(self):
        self.orders={};self.balance=Decimal(0);self.calls=[];self.sells=[]
        self.raise_after_accept=False;self.hide_lookup=False;self.available=None
        self.account_id=ACCOUNT;self.live_entry=False;self.position_status=None
    def response(self,status,payload):
        return Response(status,json.dumps(payload).encode())
    def open(self,request,timeout):
        assert request.full_url.startswith('https://paper-api.alpaca.markets/')
        method=request.get_method();url=urlparse(request.full_url)
        payload=json.loads(request.data) if request.data else None
        self.calls.append((method,url.path,payload))
        if url.path=='/v2/account':return self.response(200,dict(id=self.account_id))
        if method=='POST':
            oid=str(uuid4());qty=Decimal(payload['qty'])
            if payload['side']=='buy':
                filled=Decimal('0.00004');self.balance+=Decimal('0.0000399')
                status='partially_filled' if self.live_entry else 'canceled'
            else:
                assert qty==self.balance, 'owner attempted a partial target or stale oversell'
                self.sells.append(str(qty))
                filled=min(qty,Decimal('0.00002'));self.balance-=filled
                status='filled' if filled==qty else 'canceled'
            row=order(id=oid,client_order_id=payload['client_order_id'],side=payload['side'],
                qty=str(qty),filled_qty=str(filled),limit_price=payload['limit_price'],
                filled_avg_price=payload['limit_price'],status=status)
            self.orders[oid]=row
            if self.raise_after_accept:
                self.raise_after_accept=False
                raise TimeoutError('broker accepted, response lost')
            return self.response(200,row)
        if url.path.startswith('/v2/positions/'):
            if self.position_status:return self.response(self.position_status,dict(message='unavailable'))
            if not self.balance:return self.response(404,dict(message='position does not exist'))
            available=str(self.balance) if self.available is None else self.available
            return self.response(200,position(str(self.balance),available))
        if method=='DELETE':
            row=self.orders[url.path.rsplit('/',1)[1]]
            row['status']='canceled'
            return Response(204,b'')
        if ':by_client_order_id' in url.path:
            cid=parse_qs(url.query)['client_order_id'][0]
            matches=[row for row in self.orders.values() if row['client_order_id']==cid]
            if not matches or self.hide_lookup:return self.response(404,dict(message='not found'))
            return self.response(200,matches[0])
        return self.response(200,self.orders[url.path.rsplit('/',1)[1]])


def owner(store,external,*,decision=None,guard=None,reader=admission):
    client=PaperCycleHTTP(paper=True,account_id=ACCOUNT,key='fixture-public-key-12345',
        secret='fixture-private-key-12345',require_authority=guard or (lambda _:None),
        timeout_seconds=2,max_response_bytes=8000)
    client._opener=external
    return NativeCycleOwner(store,client,admission_reader=reader,
        decision_reader=decision or (lambda _:TickDecision('b'*64,True,False)))


def test_roundtrip_uses_exact_whole_balance_after_each_actual_partial_exit(store):
    reserve(store);external=Broker();exit_signal={'on':False}
    driver=owner(store,external,decision=lambda _:TickDecision('b'*64,True,exit_signal['on'],'60010.1'))
    assert driver.step(CYCLE)['outcome']=='entry_observed'
    assert driver.step(CYCLE)['next_action']=='manage_held_from_ticks'
    exit_signal['on']=True
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert driver.step(CYCLE)['closed']
    assert external.sells==['0.0000399','0.0000199']
    state=store.audit(CYCLE,max_events=100)
    assert exposure(state)['closed'] and not state['fees_complete']
    assert sum(method=='POST' and data['side']=='buy' for method,_,data in external.calls)==1


def test_restart_after_accepted_post_timeout_reconciles_cid_without_another_buy(store):
    reserve(store);external=Broker();external.raise_after_accept=True
    with pytest.raises(RuntimeError,match='unknown_no_resubmit'):
        owner(store,external).step(CYCLE)
    restored=owner(another(store),external)
    assert restored.step(CYCLE)['outcome']=='entry_reconciled'
    assert sum(method=='POST' for method,_,_ in external.calls)==1
    assert exposure(store.audit(CYCLE,max_events=40))['debit']=='6.0000700001'


def test_cid_404_after_unknown_submit_never_releases_or_resubmits(store):
    reserve(store);external=Broker();external.raise_after_accept=True
    with pytest.raises(RuntimeError):owner(store,external).step(CYCLE)
    external.hide_lookup=True
    for _ in range(3):
        assert owner(another(store),external).step(CYCLE)['outcome']=='broker_order_unresolved'
    assert sum(method=='POST' for method,_,_ in external.calls)==1
    assert not exposure(store.audit(CYCLE,max_events=50))['closed']


def test_no_tick_decision_does_not_become_an_entry(store):
    reserve(store);external=Broker()
    assert owner(store,external,decision=lambda _:None).step(CYCLE)['outcome']=='waiting_for_tick_entry_decision'
    assert not any(method=='POST' for method,_,_ in external.calls)


def test_invalidated_tick_setup_cancels_only_proven_unsubmitted_cycle(store):
    reserve(store);external=Broker()
    result=owner(store,external,decision=lambda _:TickDecision('c'*64,False,False)).step(CYCLE)
    assert result['closed'] and not any(method=='POST' for method,_,_ in external.calls)


def test_exit_on_partial_live_entry_cancels_then_reconciles_before_sale(store):
    reserve(store);external=Broker();external.live_entry=True;signal={'exit':False}
    driver=owner(store,external,decision=lambda _:TickDecision('c'*64,True,signal['exit'],'60010.1'))
    driver.step(CYCLE);signal['exit']=True
    assert driver.step(CYCLE)['outcome']=='entry_cancel_requested_reconciliation_required'
    assert not external.sells
    assert driver.step(CYCLE)['outcome']=='entry_reconciled'
    assert not external.sells
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert external.sells==['0.0000399']


def test_reserved_balance_does_not_authorize_a_partial_target(store):
    reserve(store);external=Broker();driver=owner(store,external)
    driver.step(CYCLE);driver.step(CYCLE)
    external.available='0.00001'
    exit_owner=owner(store,external,decision=lambda _:TickDecision('b'*64,True,True,'60010.1'))
    assert exit_owner.step(CYCLE)['next_action']=='reconcile_reserved_balance'
    assert not external.sells


def test_account_identity_change_prevents_order_transport(store):
    reserve(store);external=Broker();external.account_id=str(uuid4())
    with pytest.raises(ValueError,match='account_identity_changed'):
        owner(store,external).step(CYCLE)
    assert not any(method=='POST' for method,_,_ in external.calls)


def test_loss_of_paper_authority_before_post_keeps_durable_intent(store):
    reserve(store);external=Broker();calls=[]
    def guard(account):
        calls.append(account)
        if len(calls)==2:raise ValueError('paper_owner_lost')
    with pytest.raises(NotTransported,match='proven_not_transported'):
        owner(store,external,guard=guard).step(CYCLE)
    assert not any(method=='POST' for method,_,_ in external.calls)
    assert next_action(store.audit(CYCLE,max_events=20))=='closed'


def test_pretransport_funding_failure_does_not_cross_http(store):
    reserve(store);external=Broker()
    with pytest.raises(ValueError,match='funding_exceeded'):
        owner(store,external,reader=lambda c:admission(c,funding='1')).step(CYCLE)
    assert not any(method=='POST' for method,_,_ in external.calls)
    assert not store.read(CYCLE)['entry_started']


def test_per_cycle_fence_blocks_same_cycle_but_not_another_symbol(store):
    reserve(store);external=Broker();second=str(uuid4())
    asset=dict(ASSET,id=str(uuid4()),symbol='ETH/USD')
    reserve(store,cycle=second,asset=asset,instruction=dict(INSTRUCTION,symbol='ETH/USD',client_order_id='other'),
            reader=lambda c:admission(c,funding='100',risk='100'))
    with store.cycle_owner(CYCLE) as check:
        check()
        assert owner(another(store),external).step(CYCLE)['outcome']=='cycle_owned_by_another_worker'
        with another(store).cycle_owner(second) as other:
            assert other is not None
            other()
    with another(store).cycle_owner(CYCLE) as after:
        assert after is not None


def test_raw_malformed_response_is_retained_before_decode_error(store):
    reserve(store)
    class Malformed(Broker):
        def open(self,request,timeout):return Response(200,b'{unparseable')
    with pytest.raises(ValueError):owner(store,Malformed()).step(CYCLE)
    with store.engine.connect() as c:
        records=c.execute(text(f'SELECT event_json FROM {store.events} ORDER BY revision')).scalars().all()
    assert any('7b756e706172736561626c65' in raw for raw in records)
    assert not store.read(CYCLE)['entry_started']


def test_http_error_position_is_unknown_not_flat(store):
    reserve(store);external=Broker();driver=owner(store,external)
    driver.step(CYCLE);external.position_status=500
    assert driver.step(CYCLE)['outcome']=='broker_position_unresolved'
    assert not store.read(CYCLE)['closed']


def test_rejected_store_evidence_prevents_transport_before_any_http(store,monkeypatch):
    reserve(store);external=Broker()
    monkeypatch.setattr(store,'record_evidence',lambda *args:(_ for _ in ()).throw(ValueError('evidence_disk_failed')))
    with pytest.raises(ValueError,match='evidence_disk_failed'):
        owner(store,external).step(CYCLE)
    assert not external.calls


def test_tick_invalidated_after_budget_commit_is_not_sent_and_does_not_strand_claim(store):
    reserve(store);external=Broker();decisions=[]
    def decision(state):
        decisions.append(state['entry_started'])
        return TickDecision('b'*64,not state['entry_started'],False)
    with pytest.raises(NotTransported):owner(store,external,decision=decision).step(CYCLE)
    assert decisions==[False,True]
    assert not any(method=='POST' for method,_,_ in external.calls)
    assert exposure(store.audit(CYCLE,max_events=20))['closed']


def test_proven_unsent_exit_preserves_position_and_allows_new_full_balance_attempt(store):
    reserve(store);external=Broker();driver=owner(store,external)
    driver.step(CYCLE);driver.step(CYCLE)
    calls=[]
    def guard(account):
        calls.append(account)
        if len(calls)==3:raise ValueError('owner_disappeared_before_sell')
    exit_owner=owner(store,external,guard=guard,decision=lambda _:TickDecision('c'*64,True,True,'60010.1'))
    with pytest.raises(NotTransported):exit_owner.step(CYCLE)
    state=store.audit(CYCLE,max_events=60)
    assert not state['closed'] and state['exit_requests'][-1]['proven_unsent']
    assert not external.sells
    retry=owner(store,external,decision=lambda _:TickDecision('c'*64,True,True,'60010.1'))
    assert retry.step(CYCLE)['outcome']=='full_exit_observed'
    assert external.sells==['0.0000399']
