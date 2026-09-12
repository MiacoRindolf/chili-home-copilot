"""A filled first entry must be attributable before visiting sibling entries."""
from decimal import Decimal
from urllib.parse import urlparse

import pytest

from app.crypto_execution.account_bridge import partition_owned_native_exposure
from app.crypto_execution.lifecycle import exposure
from tests.test_native_crypto_owner import store, reserve, owner, Broker
from tests.test_native_crypto_lifecycle import CYCLE
from tests.test_native_crypto_position_alias import AliasBroker, alias_position, ALIAS


def test_terminal_entry_resolves_broker_alias_before_yielding_first_cycle(store):
    reserve(store);external=AliasBroker()
    result=owner(store,external).step(CYCLE)
    state=store.audit(CYCLE,max_events=60)
    assert result['outcome']=='entry_observed'
    assert state['position_known'] and state['position_revision']>state['entry_revision']
    assert state['position']['native_position_alias']['broker_asset_id']==ALIAS
    partition=partition_owned_native_exposure(dict(states=[state]),positions=[dict(
        asset_class='crypto',native_crypto_position=alias_position())],orders=[])
    assert partition['native_position_count']==1
    assert exposure(state)['debit']=='2.400004'


def test_unknown_post_reconciles_exact_id_then_settles_without_another_buy(store):
    reserve(store);external=AliasBroker();external.raise_after_accept=True
    driver=owner(store,external)
    with pytest.raises(RuntimeError,match='unknown_no_resubmit'):driver.step(CYCLE)
    assert not store.read(CYCLE)['position_known']
    assert driver.step(CYCLE)['outcome']=='entry_reconciled'
    state=store.audit(CYCLE,max_events=70)
    assert state['position_known'] and state['position']['native_position_alias']['broker_asset_id']==ALIAS
    assert sum(method=='POST' for method,_,_ in external.calls)==1


def test_live_partial_entry_never_uses_position_as_terminal_proof(store):
    reserve(store);external=Broker();external.live_entry=True
    driver=owner(store,external)
    assert driver.step(CYCLE)['next_action']=='reconcile_entry'
    assert driver.step(CYCLE)['next_action']=='reconcile_entry'
    assert not store.read(CYCLE)['position_known']
    assert not any(path.startswith('/v2/positions') for _,path,_ in external.calls)


def test_failed_terminal_position_read_keeps_bound_and_retries_only_read(store):
    reserve(store);external=Broker();external.position_status=503
    driver=owner(store,external)
    assert driver.step(CYCLE)['outcome']=='broker_position_unresolved'
    state=store.audit(CYCLE,max_events=60)
    assert not state['position_known'] and not state['closed']
    assert exposure(state)['debit']=='2.400004'
    external.position_status=None
    assert driver.step(CYCLE)['outcome']=='position_reconciled'
    assert store.read(CYCLE)['position_known']
    assert sum(method=='POST' for method,_,_ in external.calls)==1


def test_terminal_zero_fill_releases_only_after_actual_flat_census(store):
    reserve(store)
    class ZeroFill(Broker):
        def open(self,request,timeout):
            response=super().open(request,timeout)
            if request.get_method()=='POST':
                import json
                row=json.loads(response.body)
                row.update(filled_qty='0',filled_avg_price=None,status='canceled')
                self.orders[row['id']]=row;self.balance=Decimal(0)
                return self.response(200,row)
            return response
    external=ZeroFill()
    assert owner(store,external).step(CYCLE)['closed']
    assert any(path=='/v2/positions' for _,path,_ in external.calls)
    assert exposure(store.audit(CYCLE,max_events=60))['debit']=='0'


def test_failed_alias_lookup_cannot_make_a_filled_cycle_flat(store):
    reserve(store)
    class Unresolved(AliasBroker):
        def open(self,request,timeout):
            if urlparse(request.full_url).path=='/v2/assets/'+ALIAS:
                return self.response(503,{})
            return super().open(request,timeout)
    external=Unresolved()
    with pytest.raises(ValueError,match='alias_resolution_unavailable'):owner(store,external).step(CYCLE)
    state=store.audit(CYCLE,max_events=60)
    assert not state['position_known'] and not state['closed']
    assert exposure(state)['debit']=='2.400004'
