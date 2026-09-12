"""Terminal unfilled principal releases capacity without claiming settlement."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.crypto_execution.lifecycle import exposure,terminal_entry_principal,CREDITED_ASSET_FEE_BASIS,initial_cycle
from app.crypto_execution.store import migrate_terminal_entry_projection
from tests.test_native_crypto_lifecycle import fresh,advance,order,ACCOUNT,ASSET,CYCLE,INSTRUCTION
from tests.test_native_crypto_cycle_store import store,reserve,apply,admission
from tests.test_native_crypto_owner import owner,Broker

def bind(state):
    return advance(state,'terminal_entry_principal_bound',entry_revision=state['entry_revision'],
        order_id=state['entry']['id'],fee_basis=CREDITED_ASSET_FEE_BASIS)

def test_unresolved_or_live_order_never_releases_unfilled_quantity():
    for s in (fresh(),advance(fresh(),'entry_transport_started'),
              advance(advance(fresh(),'entry_transport_started'),'entry_observed',**order(status='partially_filled'))):
        assert exposure(s)['debit']==s['original_debit']
        with pytest.raises(ValueError,match='terminal_entry_required'):terminal_entry_principal(s)

def test_actual_ltc_terminal_bound_keeps_filled_principal_without_inventing_fee_settlement():
    asset=dict(ASSET,id='19c466e7-e8f7-43e0-a8c5-f17ec9f5de8d',symbol='LTC/USD',price_increment='0.000000001',min_order_size='0.01867051')
    instruction=dict(INSTRUCTION,symbol='LTC/USD',qty='5.653390366',limit_price='53.77')
    s=advance(initial_cycle(cycle_id=CYCLE,account_id=ACCOUNT,asset=asset,instruction=instruction,context_sha256='a'*64),'entry_transport_started')
    s=advance(s,'entry_observed',**order(asset_id=asset['id'],symbol=asset['symbol'],qty='5.653390366',limit_price='53.77',filled_qty='0.231820395',filled_avg_price='53.74'))
    assert exposure(s)['debit']=='303.98279997982'  # Legacy semantics until proof event.
    s=bind(s);r=s['terminal_entry_bound']
    assert r['debit']=='12.46498263915' and r['unfilled_principal_at_limit']=='291.51781734067'
    assert not r['fee_settlement_verified'] and r['broker_reflection_credit']=='0'
    assert exposure(s)==dict(debit='12.46498263915',risk='12.46498263915',closed=False)

def test_wrong_frontier_or_fee_basis_cannot_release_the_reservation():
    s=advance(advance(fresh(),'entry_transport_started'),'entry_observed',**order())
    payload=dict(entry_revision=s['entry_revision'],order_id=s['entry']['id'],fee_basis=CREDITED_ASSET_FEE_BASIS)
    for change in (dict(entry_revision=0),dict(order_id=str(uuid4())),dict(fee_basis='assume_no_fees')):
        with pytest.raises(ValueError,match='frontier_or_policy_changed'):
            advance(s,'terminal_entry_principal_bound',**dict(payload,**change))
    s=bind(s);s['terminal_entry_bound']['debit']='0'
    with pytest.raises(ValueError,match='evidence_changed'):exposure(s)

def test_corrected_reported_fill_price_is_never_understated_by_old_limit_or_receipt():
    s=bind(advance(advance(fresh(),'entry_transport_started'),'entry_observed',**order()))
    s=advance(s,'entry_observed',**order(filled_avg_price='200000'))
    assert exposure(s)['debit']=='8' and s['terminal_entry_bound']['buy_fill_price_exceeds_limit']
    assert s['terminal_entry_bound']['entry_revision']==s['entry_revision']

def test_terminal_zero_fill_can_release_principal_without_claiming_cycle_closed():
    s=bind(advance(advance(fresh(),'entry_transport_started'),'entry_observed',**order(filled_qty='0',filled_avg_price=None)))
    assert exposure(s)==dict(debit='0',risk='0',closed=False)

def test_later_independent_candidate_can_reserve_after_partial_terminal_entry(store):
    reserve(store);driver=owner(store,Broker());driver.step(CYCLE)
    assert exposure(store.read(CYCLE))['debit']=='2.400004'
    # Original6.0000700001 + another5 would exceed10. The filled principal plus
    # the new independent order fits; no fake broker reflection is credited.
    asset=dict(ASSET,id=str(uuid4()),symbol='ETH/USD')
    instruction=dict(INSTRUCTION,symbol='ETH/USD',qty='0.00005',limit_price='100000',client_order_id='later-independent')
    second=reserve(store,cycle=str(uuid4()),asset=asset,instruction=instruction)
    assert second['admission_receipt']['account_risk_required']=='7.400004'
    assert len(store.open_cycle_ids())==2
    assert store.audit(CYCLE,max_events=30)['terminal_entry_bound']['filled_quantity']=='0.00004'

def test_unresolved_existing_order_still_prevents_overallocation(store):
    reserve(store)
    with pytest.raises(ValueError,match='funding_exceeded|risk_budget_exceeded'):
        reserve(store,cycle=str(uuid4()),asset=dict(ASSET,id=str(uuid4()),symbol='ETH/USD'),
            instruction=dict(INSTRUCTION,symbol='ETH/USD',qty='0.00005',limit_price='100000',client_order_id='blocked'))


def test_all_later_affordable_candidates_share_capacity_after_held_reconciliation(store):
    from tests.test_native_crypto_allocation import candidate
    from app.crypto_execution.account_bridge import native_account_snapshot
    reserve(store);driver=owner(store,Broker());driver.step(CYCLE);driver.step(CYCLE)
    with store.engine.begin() as c:
        store._lock(c)
        before=native_account_snapshot(c,account_id=ACCOUNT,schema=store.cycles.split('.')[0])
    assert before['risk']==Decimal('2.400004')
    result=store.reserve_all([candidate(1,minimum='.1',step='.1'),candidate(2,minimum='.1',step='.1')],admission_reader=admission)
    assert len(result['created'])==2 and len(store.open_cycle_ids())==3
    assert {s['instruction']['qty'] for s in result['created']}=={'3.7'}
    with store.engine.begin() as c:
        store._lock(c)
        after=native_account_snapshot(c,account_id=ACCOUNT,schema=store.cycles.split('.')[0])
    assert after['risk']==Decimal('9.800004') and after['risk']<10
    assert store.audit(CYCLE,max_events=40)['position_known']

def test_constraint_migration_preserves_old_states_then_allows_bound_event(store):
    s=apply(store,reserve(store),'entry_transport_started')
    s=apply(store,s,'entry_observed',**order())
    schema=store.cycles.split('.')[0]
    with store.engine.begin() as c:
        c.execute(text(f'ALTER TABLE {store.cycles} DROP CONSTRAINT native_crypto_debit_projection'))
        c.execute(text(f"ALTER TABLE {store.cycles} ADD CHECK(debit=CASE WHEN closed THEN 0 ELSE (state_json::jsonb->>'original_debit')::numeric END)"))
    before=deepcopy(store.audit(CYCLE,max_events=3))
    with store.engine.begin() as c:migrate_terminal_entry_projection(c,schema=schema)
    assert store.audit(CYCLE,max_events=3)==before
    s=apply(store,s,'terminal_entry_principal_bound',entry_revision=s['entry_revision'],order_id=s['entry']['id'],fee_basis=CREDITED_ASSET_FEE_BASIS)
    assert exposure(s)['debit']=='2.400004'
    with store.engine.begin() as c:migrate_terminal_entry_projection(c,schema=schema)
    assert store.audit(CYCLE,max_events=4)==s
    with pytest.raises(DBAPIError):
        with store.engine.begin() as c:c.execute(text(f'UPDATE {store.cycles} SET debit=0,risk=0'))

def test_unrecognized_constraint_is_not_silently_removed(store):
    schema=store.cycles.split('.')[0]
    with store.engine.begin() as c:
        c.execute(text(f'ALTER TABLE {store.cycles} DROP CONSTRAINT native_crypto_debit_projection'))
        c.execute(text(f"ALTER TABLE {store.cycles} ADD CHECK(debit >= (state_json::jsonb->>'original_debit')::numeric)"))
    with pytest.raises(ValueError,match='unrecognized_legacy_projection'):
        with store.engine.begin() as c:migrate_terminal_entry_projection(c,schema=schema)


def test_failed_migration_restores_the_original_constraint_and_preserves_rows(store):
    reserve(store);schema=store.cycles.split('.')[0]
    with store.engine.begin() as c:
        c.execute(text(f'ALTER TABLE {store.cycles} DROP CONSTRAINT native_crypto_debit_projection'))
        c.execute(text(f"ALTER TABLE {store.cycles} ADD CONSTRAINT legacy_bound CHECK(debit=CASE WHEN closed THEN 0 ELSE (state_json::jsonb->>'original_debit')::numeric END)"))
        c.execute(text(f"UPDATE {store.cycles} SET state_json=jsonb_set(state_json::jsonb,'{{terminal_entry_bound}}','{{\"debit\":\"0\"}}')::text"))
        before=c.execute(text(f'SELECT state_json,debit FROM {store.cycles}')).one()
    with pytest.raises(DBAPIError):
        with store.engine.begin() as c:migrate_terminal_entry_projection(c,schema=schema)
    with store.engine.connect() as c:
        assert c.execute(text(f'SELECT state_json,debit FROM {store.cycles}')).one()==before
        names=c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid=to_regclass(:table)"),dict(table=store.cycles)).scalars().all()
    assert 'legacy_bound' in names and 'native_crypto_debit_projection' not in names
