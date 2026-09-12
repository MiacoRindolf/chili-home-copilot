from dataclasses import replace
from decimal import Decimal as D
import json
from uuid import uuid4
import pytest

from app.crypto_execution.selection import assess_native_context,assess_all_native,NativeTickDecisionReader
from app.crypto_execution.tick_context import CryptoTickContext
from app.crypto_execution.lifecycle import digest
from app.tick_math.structural_prefix import Limits
from app.tick_math.wave_context import WavePhase
from scripts.crypto_trade_frames import timestamp_ns
from tests.test_native_crypto_owner import store,Broker,owner,reserve
from tests.test_native_crypto_lifecycle import ASSET,CYCLE,INSTRUCTION

PRICES=[97,100,95,94,89,90,91,92,97,98,95,90,91,86,87,88,91,86,91,92,91,96,93,96,91,90,85,80,75,80,83,78,79]
FEES=dict(entry_fee_rate='0.0025',exit_fee_rate='0.0025',sha256='c'*64)
BASE=timestamp_ns('2026-09-12T00:00:00Z')

def add(b,price,*,side='B'):
    i=b.frame_sequence+1;t=f'2026-09-12T00:00:00.{i:09d}Z';p=D(price)
    rows=([dict(T='subscription',trades=list(b.assets),quotes=list(b.assets))] if i==1 else [])
    rows.extend([dict(T='q',S='BTC/USD',t=t,bp=str(p-D('.1')),ap=str(p+D('.1')),bs='1',**{'as':'1'}),
        dict(T='t',S='BTC/USD',t=t,i=i,p=str(p),s='1',tks=side)])
    b.append(json.dumps(rows),frame_sequence=i,received_ns=BASE+i+1000,published_ns=BASE+i+1001)

def context(*,side='B',shift=0):
    assets=[ASSET,dict(ASSET,id=str(uuid4()),symbol='ETH/USD')]
    b=CryptoTickContext(assets=assets,location='us',connection_id='selection-fixture',
        limits=Limits(100,100,100),max_frame_bytes=65536,max_pending_quotes=100)
    for p in PRICES:add(b,p+shift,side=side)
    return b,assets

def test_real_nested_print_geometry_drives_front_front_decision_and_cost_is_not_a_forecast():
    b,assets=context();view=b.view('BTC/USD')
    assert view.wave.local_phase.side==view.wave.parent_phase.side=='front'
    a=assess_native_context(view,ASSET,fee_evidence=FEES);r=a.receipt()
    assert a.decision.entry_allowed is True and not a.decision.exit_requested
    assert r['entry_limit_price']=='79.1' and r['exit_limit_price']=='78.9'
    assert r['current_roundtrip_scenario']['profitable_at_supplied_prices'] is False
    assert r['profit_forecast'] is None and not r['quantity_allocated'] and not r['order_authority']
    assert digest(r)==a.sha256==a.decision.context_sha256
    r['entry_allowed']=False
    assert a.receipt()['entry_allowed'] is True

def test_all_members_including_cold_symbols_remain_visible_without_top_n():
    b,assets=context();views=tuple(b.view(s) for s in b.assets)
    result=assess_all_native(views,assets,fee_evidence=FEES)
    assert [x.symbol for x in result]==['BTC/USD','ETH/USD']
    assert result[1].decision.entry_allowed is None
    with pytest.raises(ValueError,match='membership_incomplete'):assess_all_native(views[:1],assets,fee_evidence=FEES)
    with pytest.raises(ValueError,match='mixed_source_publications'):
        assess_all_native((views[0],replace(views[1],source_root_sha256='e'*64)),assets,fee_evidence=FEES)

@pytest.mark.parametrize('side',['S',None])
def test_price_recovery_alone_does_not_fabricate_reported_buyer_dominance(side):
    b,_=context(side=side);a=assess_native_context(b.view('BTC/USD'),ASSET,fee_evidence=FEES)
    assert a.decision.entry_allowed is False
    assert 'buyer_dominance_not_established' in a.receipt()['reasons']

def test_parent_back_local_front_is_explicit_study_case_not_a_claim_of_bad_opportunity():
    b,_=context();v=b.view('BTC/USD')
    v=replace(v,wave=replace(v.wave,parent_phase=WavePhase('back','fixture_parent_pullback')))
    a=assess_native_context(v,ASSET,fee_evidence=FEES)
    assert a.decision.entry_allowed is None and not a.decision.exit_requested
    assert 'parent_back_local_front_requires_study' in a.receipt()['reasons']

@pytest.mark.parametrize('fees',[None,{},dict(FEES,entry_fee_rate='broken')])
def test_missing_fee_evidence_prevents_new_entry_but_not_full_sale(fees):
    b,_=context();a=assess_native_context(b.view('BTC/USD'),ASSET,fee_evidence=fees)
    assert a.decision.entry_allowed is None
    add(b,77)
    a=assess_native_context(b.view('BTC/USD'),ASSET,fee_evidence=fees)
    assert a.decision.exit_requested and a.decision.entry_allowed is False
    assert a.receipt()['exit_fraction']=='1' and a.decision.exit_limit_price=='76.9'

def test_inactive_asset_cannot_enter_but_exit_signal_is_preserved():
    b,_=context();inactive=dict(ASSET,tradable=False)
    assert assess_native_context(b.view('BTC/USD'),inactive,fee_evidence=FEES).decision.entry_allowed is False
    add(b,77)
    assert assess_native_context(b.view('BTC/USD'),inactive,fee_evidence=FEES).decision.exit_requested


@pytest.mark.parametrize('bid,exit_requested',[('78',False),('77.9',True)])
def test_new_quote_can_withdraw_entry_before_another_trade_arrives(bid,exit_requested):
    b,_=context();before=b.view('BTC/USD');i=b.frame_sequence+1
    assert before.wave.local_valley.price==78 and before.wave.local_phase.side=='front'
    b.append(json.dumps([dict(T='q',S='BTC/USD',t=f'2026-09-12T00:00:00.{i:09d}Z',
        bp=bid,ap=str(D(bid)+D('.1')),bs='1',**{'as':'1'})]),
        frame_sequence=i,received_ns=BASE+i+1000,published_ns=BASE+i+1001)
    after=b.view('BTC/USD');a=assess_native_context(after,ASSET,fee_evidence=FEES)
    assert after.print_count==before.print_count and after.wave.local_phase.side=='front'
    assert a.decision.entry_allowed is False and a.decision.exit_requested is exit_requested
    assert a.receipt()['current_quote_source_sequence']>before.current_quote_source_sequence

def test_callback_requires_durable_receipt_and_revalidates_reserved_price_cap():
    b,_=context(shift=59921);records=[]
    reader=NativeTickDecisionReader(context_reader=lambda _:b.view('BTC/USD'),asset_reader=lambda _:ASSET,
        fee_reader=lambda:FEES,record=records.append)
    state=dict(cycle_id=CYCLE,asset=ASSET,instruction=INSTRUCTION)
    assert reader(state).entry_allowed is True
    assert records[-1]['context_sha256']==digest(records[-1]['selection'])
    too_high=dict(state,instruction=dict(INSTRUCTION,limit_price='60001'))
    assert reader(too_high).entry_allowed is False
    def failed(_):raise OSError('durable receipt failed')
    reader.record=failed
    with pytest.raises(OSError):reader(state)

def test_real_owner_uses_shared_context_callback_for_entry_then_full_balance_exit(store):
    b,_=context(shift=59921);reserve(store);external=Broker()
    reader=NativeTickDecisionReader(context_reader=lambda _:b.view('BTC/USD'),asset_reader=lambda _:ASSET,
        fee_reader=lambda:FEES,record=lambda value:store.record_evidence(CYCLE,dict(phase='native_selection',**value)))
    driver=owner(store,external,decision=reader)
    assert driver.step(CYCLE)['outcome']=='entry_observed'
    assert driver.step(CYCLE)['next_action']=='manage_held_from_ticks'
    add(b,59998)
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert driver.step(CYCLE)['outcome']=='full_exit_observed'
    assert driver.step(CYCLE)['closed']
    assert external.sells==['0.0000399','0.0000199']
    state=store.audit(CYCLE,max_events=100)
    assert state['closed'] and not state['fees_complete']


def test_source_absence_does_not_skip_held_broker_position_reconciliation(store):
    b,_=context(shift=59921);reserve(store);external=Broker();source={'available':True}
    reader=NativeTickDecisionReader(context_reader=lambda _:b.view('BTC/USD') if source['available'] else None,
        asset_reader=lambda _:ASSET,fee_reader=lambda:FEES,
        record=lambda value:store.record_evidence(CYCLE,dict(phase='native_selection',**value)))
    driver=owner(store,external,decision=reader)
    assert driver.step(CYCLE)['outcome']=='entry_observed'
    source['available']=False
    assert driver.step(CYCLE)['outcome']=='position_reconciled'
    assert any(method=='GET' and path.startswith('/v2/positions/') for method,path,_ in external.calls)
    assert store.read(CYCLE)['position_known']


def test_failed_decision_receipt_never_reaches_entry_transport(store):
    b,_=context(shift=59921);reserve(store);external=Broker()
    def failed(_):raise OSError('receipt storage unavailable')
    reader=NativeTickDecisionReader(context_reader=lambda _:b.view('BTC/USD'),asset_reader=lambda _:ASSET,
        fee_reader=lambda:FEES,record=failed)
    with pytest.raises(OSError):owner(store,external,decision=reader).step(CYCLE)
    assert not any(method=='POST' for method,_,_ in external.calls)
    assert not store.read(CYCLE)['entry_started']
