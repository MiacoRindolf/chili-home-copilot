"""Regression for PAPER listing UUID != portfolio UUID, using the real journal."""
import json
from uuid import uuid4
from urllib.parse import urlparse

import pytest

from app.crypto_execution.positions import read_owned_position, resolved_position
from app.crypto_execution.paper_http import BrokerResponse
from app.crypto_execution.account_bridge import partition_owned_native_exposure
from tests.test_native_crypto_owner import Broker, owner
from tests.test_native_crypto_cycle_store import store, reserve
from tests.test_native_crypto_lifecycle import ASSET, CYCLE, position, held
from tests.test_native_crypto_owner import TickDecision

ALIAS='0c077710-c350-4813-ad39-d6fe930a30ac'

def alias_position(qty='0.0000399'):
    return dict(position(qty,qty),asset_id=ALIAS,symbol=ASSET['symbol'].replace('/',''))

class AliasBroker(Broker):
    def open(self,request,timeout):
        path=urlparse(request.full_url).path
        if request.get_method()=='GET':
            if path=='/v2/positions/'+ASSET['id']:
                return self.response(404,dict(message='not found'))
            if path=='/v2/positions':
                return self.response(200,[alias_position(str(self.balance))] if self.balance else [])
            if path=='/v2/positions/'+ALIAS:
                return self.response(200,alias_position(str(self.balance))) if self.balance else self.response(404,{})
            if path=='/v2/assets/'+ALIAS:return self.response(200,ASSET)
        return super().open(request,timeout)

def test_actual_alias_partial_fill_recovers_and_sells_every_remaining_share(store):
    reserve(store);broker=AliasBroker();signal={'exit':False}
    driver=owner(store,broker,decision=lambda _:TickDecision('b'*64,True,signal['exit'],'60010.1'))
    driver.step(CYCLE)
    assert driver.step(CYCLE)['next_action']=='manage_held_from_ticks'
    state=store.read(CYCLE)
    assert state['position']['native_position_alias']['broker_asset_id']==ALIAS
    raw=dict(asset_class='crypto',native_crypto_position=alias_position())
    assert partition_owned_native_exposure(dict(states=[state]),positions=[raw],orders=[])['native_position_count']==1
    signal['exit']=True
    driver.step(CYCLE);driver.step(CYCLE)
    assert driver.step(CYCLE)['closed']
    assert broker.sells==['0.0000399','0.0000199']
    assert store.audit(CYCLE,max_events=150)['closed']

def response(payload,status=200):return BrokerResponse(status,json.dumps(payload).encode())

@pytest.mark.parametrize('case',['wrong_direct','noncrypto_direct','ambiguous','lookup_failed','wrong_symbol'])
def test_unverified_alias_cannot_authorize_flat_or_sale(case):
    row=alias_position();other=dict(ASSET,id=str(uuid4()))
    if case=='wrong_symbol':row['symbol']='FAKE/USD'
    if case=='noncrypto_direct':row['asset_class']='us_equity'
    def read(path):
        if path.startswith('/v2/assets/'):
            return response(other if case=='wrong_direct' else ASSET,503 if case=='lookup_failed' else 200)
        if path=='/v2/positions':return response([row,row])
        return response({},404) if case=='ambiguous' else response(row)
    with pytest.raises(ValueError):read_owned_position(dict(asset=ASSET),read,lambda _:None)

def test_matching_symbol_with_different_resolved_uuid_is_unowned():
    row=alias_position();other=dict(ASSET,id=str(uuid4()))
    def read(path):
        if path=='/v2/positions':return response([row])
        if path.startswith('/v2/assets/'):return response(other)
        return response({},404)
    assert read_owned_position(dict(asset=ASSET),read,lambda _:None)==(200,None)

def test_retained_alias_does_not_excuse_changed_balance_or_false_resolution():
    state=held();state['position']=resolved_position(alias_position(),ASSET,ASSET)
    raw=dict(asset_class='crypto',native_crypto_position=alias_position('0.00003'))
    with pytest.raises(ValueError,match='quantity_changed'):
        partition_owned_native_exposure(dict(states=[state]),positions=[raw],orders=[])
    state['position']['native_position_alias']['resolved_asset']=dict(ASSET,id=str(uuid4()))
    with pytest.raises(ValueError,match='resolution_mismatch'):
        partition_owned_native_exposure(dict(states=[state]),positions=[raw],orders=[])
