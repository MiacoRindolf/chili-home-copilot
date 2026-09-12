"""Account changes wake tick selection without ranking or weakening ownership."""
import json
from uuid import uuid4

from app.crypto_execution.owner import TickDecision
from app.crypto_execution.host import NativePaperHost
from app.crypto_execution.account_bridge import partition_owned_native_exposure
from tests.test_native_crypto_owner import store,Broker,owner,reserve
from tests.test_native_crypto_lifecycle import ASSET,CYCLE,order,position
from tests.test_native_crypto_host import runtime,Source,context,config,settings


def test_terminal_zero_fill_sale_returns_reconciled_balance_for_sibling_admission(store):
    reserve(store)
    class ZeroSell(Broker):
        def open(self,request,timeout):
            payload=json.loads(request.data) if request.data else None
            if request.get_method()=='POST' and payload['side']=='sell':
                row=order(id=str(uuid4()),client_order_id=payload['client_order_id'],side='sell',
                    qty=payload['qty'],filled_qty='0',filled_avg_price=None,
                    limit_price=payload['limit_price'],status='canceled')
                self.orders[row['id']]=row;self.sells.append(payload['qty'])
                return self.response(200,row)
            return super().open(request,timeout)
    external=ZeroSell();signal={'exit':False}
    driver=owner(store,external,decision=lambda _:TickDecision('b'*64,True,signal['exit'],'60001'))
    driver.step(CYCLE);driver.step(CYCLE);signal['exit']=True
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    state=store.audit(CYCLE,max_events=100)
    assert state['position_known'] and state['position_revision']>state['exit_revision']
    assert not state['closed'] and external.sells==['0.0000399']
    partition=partition_owned_native_exposure(dict(states=[state]),positions=[dict(asset_class='crypto',
        native_crypto_position=position())],orders=[])
    assert partition['native_position_count']==1
    assert state['terminal_entry_bound']['debit']=='2.400004', 'no sale credit from a zero fill'


def test_failed_postsale_position_read_keeps_bound_and_reconciliation_requirement(store):
    reserve(store);external=Broker();signal={'exit':False}
    driver=owner(store,external,decision=lambda _:TickDecision('b'*64,True,signal['exit'],'60001'))
    driver.step(CYCLE);driver.step(CYCLE);signal['exit']=True
    original=external.open
    def provider(request,timeout):
        response=original(request,timeout)
        if request.get_method()=='POST' and json.loads(request.data)['side']=='sell':
            external.position_status=500
        return response
    external.open=provider
    assert driver.step(CYCLE)['outcome']=='broker_position_unresolved'
    state=store.audit(CYCLE,max_events=100)
    assert not state['position_known'] and not state['closed']
    assert state['terminal_entry_bound']['debit']=='2.400004'


def test_runtime_wakes_for_capital_and_reconciled_balance_but_not_journal_only_activity(store):
    b,assets=context();r=runtime(store,Source(tuple(b.view(s) for s in b.assets)),assets)
    assert r.select()['created']==1
    assert r.reconcile()['account_capacity_changed'] # terminal entry releases unfilled allocation
    assert r.reconcile()['account_capacity_changed'] # exact new owned balance
    assert not r.reconcile()['account_capacity_changed'] # mere holding/source/broker evidence
    assert r.select()['created']==0 # same tick does not invent another cycle


def test_account_capacity_change_wakes_waiting_selection_without_source_publication(tmp_path):
    host=NativePaperHost(None,settings(),config(tmp_path),'b'*64)
    class Runtime:
        def reconcile(self,stopping):
            return dict(account_capacity_changed=True,outcomes={},errors={})
    host.runtime=Runtime()
    host.stop.wait=lambda _:True
    assert not host.published.is_set()
    host._cycles_loop()
    assert host.published.is_set()


def test_one_cycle_state_read_failure_does_not_skip_sibling(store,monkeypatch):
    from dataclasses import replace
    b,assets=context();v=b.view('BTC/USD')
    r=runtime(store,Source((v,replace(v,symbol=assets[1]['symbol'],asset_id=assets[1]['id']))),assets)
    r.select();ids=store.open_cycle_ids();read=store.read;visited=[]
    def observed(cid):
        if cid==ids[0]:raise ValueError('native_fixture_state_unavailable')
        return read(cid)
    monkeypatch.setattr(store,'read',observed)
    monkeypatch.setattr(r.owner,'step',lambda cid:visited.append(cid) or dict(outcome='visited'))
    result=r.reconcile()
    assert visited==[ids[1]] and ids[0] in result['errors'] and ids[1] in result['outcomes']
