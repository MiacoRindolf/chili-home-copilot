"""Cross-asset ownership and real committed ordinary/native reservation checks."""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.config import settings
from app.crypto_execution import account_bridge as bridge
from app.crypto_execution.funding import crypto_account_truth
from app.crypto_execution.store import ACCOUNT_LOCK
from app.crypto_execution.truth import native_payload
from tests.test_native_crypto_cycle_store import store, reserve
from tests.test_native_crypto_lifecycle import (
    ACCOUNT, ASSET, CYCLE, INSTRUCTION, advance, fresh, held, order, position,
)


def snapshot(*states):
    return dict(states=list(states), risk=Fraction('6.0000700001')*len(states),
        debit=Fraction('6.0000700001')*len(states), cycle_ids=[s['cycle_id'] for s in states],
        snapshot_sha256='a'*64)


def native_position(**changes):
    return dict(asset_class='crypto', product_id='BTC/USD', qty=.0000399, side='long',
                native_crypto_position=position(**changes))


def native_order(**changes):
    return SimpleNamespace(raw=dict(broker_asset_class_echo='crypto',
        native_crypto_order=order(status='new',filled_qty='0',filled_avg_price=None,**changes)))


def test_exact_native_position_does_not_occupy_an_ordinary_equity_slot():
    equity=dict(product_id='EQ',qty=3,side='long',asset_class='us_equity')
    result=bridge.partition_owned_native_exposure(snapshot(held()),
        positions=[equity,native_position()],orders=[])
    assert result['positions']==[equity]
    assert result['native_position_count']==1 and result['native_symbols']==['BTC/USD']


@pytest.mark.parametrize('case', ['foreign','changed','missing','duplicate','lossy','class_conflict','unreconciled'])
def test_native_position_cannot_be_excused_by_symbol_or_display_float(case):
    state=held();rows=[native_position()]
    if case=='foreign':rows[0]['native_crypto_position']['asset_id']=str(uuid4())
    if case=='changed':rows=[native_position(qty='0.0000398',available='0.0000398')]
    if case=='missing':rows=[]
    if case=='duplicate':rows*=2
    if case=='lossy':rows[0]['native_crypto_position']['qty']=.0000399
    if case=='class_conflict':rows[0]['asset_class']='us_equity'
    if case=='unreconciled':state=advance(state,'position_read_started',read_id=str(uuid4()))
    with pytest.raises(ValueError):
        bridge.partition_owned_native_exposure(snapshot(state),positions=rows,orders=[])


def test_owned_pending_entry_is_matched_to_its_frozen_instruction_without_mutating_state():
    state=advance(fresh(),'entry_transport_started');before=deepcopy(state)
    ordinary=dict(id='ordinary',client_order_id='ordinary')
    result=bridge.partition_owned_native_exposure(snapshot(state),positions=[],orders=[ordinary,native_order()])
    assert result['orders']==[ordinary] and result['native_open_order_count']==1
    assert state==before
    for changes in (dict(client_order_id='manual'),dict(qty='0.000100002'),dict(limit_price='60000.2'),dict(replaced_by=CYCLE)):
        with pytest.raises(ValueError):
            bridge.partition_owned_native_exposure(snapshot(state),positions=[],orders=[native_order(**changes)])
    with pytest.raises(ValueError,match='duplicate'):
        bridge.partition_owned_native_exposure(snapshot(state),positions=[],orders=[native_order(),native_order()])
    with pytest.raises(ValueError):
        bridge.partition_owned_native_exposure(snapshot(fresh()),positions=[],orders=[native_order()])


def test_margin_capacity_charge_is_explicit_exact_and_does_not_make_equities_cash_only():
    result=bridge.equity_native_bound(snapshot(held()),ordinary_risk='1',account_risk_budget='10',
        ordinary_bp_required='300',available_bp='400',multiplier='4')
    assert result['ok'] and result['native_margin_capacity_charge_usd']=='24.0002800004'
    assert result['projected_account_risk_usd']=='7.0000700001'
    assert not result['actual_broker_buying_power_change_certified']
    assert result['native_reflection_credit']=='0'
    for bp, risk, admitted in [('324.0002800004','7.0000700001',True),
                              ('324.0002800003','7.0000700001',False),
                              ('324.0002800004','7.0000700000',False)]:
        result=bridge.equity_native_bound(snapshot(held()),ordinary_risk='1',account_risk_budget=risk,
            ordinary_bp_required='300',available_bp=bp,multiplier='4')
        assert result['ok'] is admitted
    with pytest.raises(ValueError):
        bridge.equity_native_bound(snapshot(held()),ordinary_risk='1',account_risk_budget='10',
            ordinary_bp_required='1',available_bp='100',multiplier='None')


def test_ordinary_exact_totals_cannot_round_an_exceeded_native_budget_into_admission():
    from app.services.trading.momentum_neural.ordinary_alpaca_ledger import ordinary_risk_totals
    totals=ordinary_risk_totals(positions=[dict(owner_id=1,symbol='EQ',client_order_id='owned',
        quantity='3',entry_price='0.10000000000000000000000000001',stop_price=None)],
        pending=[],candidate_symbol='OTHER',exact=True)
    assert totals['account_open_risk_usd']=='0.30000000000000000000000000003'
    result=bridge.equity_native_bound(snapshot(held()),ordinary_risk=totals['account_open_risk_usd'],
        account_risk_budget='6.3000700001',ordinary_bp_required='0',available_bp='100',multiplier='4')
    assert result['buying_power_fits'] and not result['risk_fits']


def test_sdk_sidecar_preserves_exact_quantity_and_native_uuid_without_inventing_precision(monkeypatch):
    from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter
    row=SimpleNamespace(**position())
    row.asset_id=UUID(ASSET['id']);row.qty=Decimal('0.000039900000000000000001')
    row.qty_available=row.qty
    adapter=object.__new__(AlpacaSpotAdapter)
    monkeypatch.setattr(adapter,'_account_client',lambda:SimpleNamespace(get_all_positions=lambda:[row]))
    rows,_=adapter.list_positions()
    assert rows[0]['native_crypto_position']['qty']=='0.000039900000000000000001'
    assert rows[0]['native_crypto_position']['asset_id']==ASSET['id']
    normalized=adapter._normalize_order(SimpleNamespace(**order()))
    assert normalized.raw['native_crypto_order']['qty']==INSTRUCTION['qty']
    assert normalized.raw['native_crypto_order']['asset_id']==ASSET['id']
    assert native_payload(dict(order(),qty=.0001))['qty']==.0001


def test_snapshot_requires_real_lock_and_rejects_mismatched_journal_head(store):
    reserve(store)
    schema=store.cycles.split('.')[0]
    with store.engine.begin() as c:
        with pytest.raises(ValueError,match='account_lock_required'):
            bridge.native_account_snapshot(c,account_id=ACCOUNT,schema=schema)
        c.execute(text('SELECT pg_advisory_xact_lock(:key)'),dict(key=ACCOUNT_LOCK))
        result=bridge.native_account_snapshot(c,account_id=ACCOUNT,schema=schema)
        assert result['debit']==Fraction('6.0000700001') and result['cycle_ids']==[CYCLE]
        c.execute(text(f'UPDATE {store.cycles} SET head_sha256=:sha'),dict(sha='0'*64))
        with pytest.raises(ValueError,match='projection_unverified'):
            bridge.native_account_snapshot(c,account_id=ACCOUNT,schema=schema)


def bind_ordinary_account(store,monkeypatch):
    from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID
    store.account_id=TEST_ALPACA_ACCOUNT_ID
    monkeypatch.setattr(settings,'chili_alpaca_paper',True)
    monkeypatch.setattr(settings,'chili_alpaca_expected_account_id',store.account_id)
    monkeypatch.setattr(settings,'chili_momentum_legacy_alpaca_dispatch_enabled',True)
    original=bridge.native_account_snapshot
    monkeypatch.setattr(bridge,'native_account_snapshot',
        lambda c,*,account_id: original(c,account_id=account_id,schema=store.cycles.split('.')[0]))
    def admission(c):
        account=crypto_account_truth(dict(id=store.account_id,currency='USD',status='ACTIVE',
            crypto_status='ACTIVE',non_marginable_buying_power='100',cash='100',accrued_fees='0',
            account_blocked=False,trading_blocked=False,trade_suspended_by_user=False),
            expected_account_id=store.account_id)
        return dict(account=account,observation_id=str(uuid4()),external_claims=[],
                    account_risk_budget='100',external_risk_upper_bound='0')
    return admission


@pytest.mark.parametrize('budget,bp,multiplier,allowed,reason', [
    (.1,100,4,True,None),(.07,100,4,False,'native_crypto_shared_account_budget_exceeded'),
    (.1,30,4,False,'native_crypto_shared_account_budget_exceeded'),
    (.1,100,None,False,'native_crypto_shared_account_budget_unavailable'),
])
def test_real_ordinary_commit_counts_native_pending_cycle(store,db,monkeypatch,budget,bp,multiplier,allowed,reason):
    from tests.test_legacy_timeshare_sizing_escape import _seed_owner
    from app.services.trading.momentum_neural.alpaca_orphan_claims import reserve_alpaca_entry_risk_committed
    admission=bind_ordinary_account(store,monkeypatch)
    reserve(store,reader=admission)
    owner,request=_seed_owner(db,'CROSS')
    cid='cross-'+uuid4().hex
    result=reserve_alpaca_entry_risk_committed(symbol='CROSS',claim_token='claim-'+cid,
        owner_session_id=owner,client_order_id=cid,post_bind_token='bind-'+cid,
        order_request=request('CROSS',cid,qty='1'),order_role='primary',reserved_risk_usd=1,
        account_equity_usd=100,budget_fraction=budget,account_buying_power_usd=bp,
        account_multiplier=multiplier,account_scope='alpaca:paper',
        role_metadata={'legacy_timeshare_sizing':True},per_symbol_cap_usd=100)
    assert result['ok'] is allowed,result
    if allowed:
        assert result['native_crypto_account_bound']['native_cycle_ids']==[CYCLE]
        assert result['projected_account_risk_usd']==float('7.0000700001')
    else:assert result['reason']==reason,result


def test_real_posture_allows_reconciled_native_holding_beside_owned_equity(store,db,monkeypatch):
    from tests.test_ordinary_alpaca_owned_positions import _held
    from app.services.trading.momentum_neural.alpaca_orphan_claims import certify_alpaca_owned_entry_posture_committed
    admission=bind_ordinary_account(store,monkeypatch)
    state=reserve(store,reader=admission)
    for event in [dict(kind='entry_transport_started'),dict(kind='entry_observed',payload=order()),
                  dict(kind='position_read_started',payload=dict(read_id=CYCLE))]:
        state=store.apply(CYCLE,event_id=str(uuid4()),event=event,
                          expected_revision=state['revision'],admission_reader=admission)['state']
    store.apply(CYCLE,event_id=str(uuid4()),expected_revision=state['revision'],event=dict(
        kind='position_observed',payload=dict(read_id=CYCLE,read_revision=state['revision'],found=True,position=position())))
    _held(db)
    result=certify_alpaca_owned_entry_posture_committed(account_scope='alpaca:paper',
        alpaca_account_id=store.account_id,broker_orders=[],broker_positions=[native_position(),
            dict(product_id='HLDA',qty=10,side='long',asset_class='us_equity')])
    assert result['ok'],result
    assert result['position_count']==2 and result['native_owned_position_count']==1
    assert result['owned_position_symbols']==['HLDA']


def test_native_claim_added_after_equity_reservation_is_seen_before_transport(store,db,monkeypatch):
    from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
    from tests.test_ordinary_alpaca_transport_budget import _reserve, _read, _snapshot, _quote
    admission=bind_ordinary_account(store,monkeypatch)
    key=_reserve(db,'LATE')
    reserve(store,reader=admission)
    assert not claims.mark_entry_transport_started_committed(**key,
        ordinary_account_snapshot=_snapshot(buying_power=30,multiplier=4),ordinary_quote_check=_quote)
    current=_read(key)
    assert current['phase']=='claimed' and 'entry_transport_started' not in current['metadata']
    assert current['metadata']['entry_financial_revalidation']['reason']=='native_crypto_shared_account_budget_exceeded'
    assert claims.mark_entry_transport_started_committed(**key,
        ordinary_account_snapshot=_snapshot(buying_power=35,multiplier=4),ordinary_quote_check=_quote)
    assert _read(key)['phase']=='submit_indeterminate'


@pytest.mark.parametrize('bp,admitted',[(100,1),(200,2)])
def test_concurrent_equity_siblings_preserve_native_capacity_under_real_account_lock(store,db,monkeypatch,bp,admitted):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.test_legacy_timeshare_sizing_escape import _seed_owner
    from app.services.trading.momentum_neural.alpaca_orphan_claims import reserve_alpaca_entry_risk_committed
    admission=bind_ordinary_account(store,monkeypatch)
    reserve(store,reader=admission)
    candidates=[]
    for symbol in ('EQA','EQB'):
        owner,request=_seed_owner(db,symbol);cid='parallel-'+uuid4().hex
        candidates.append(dict(symbol=symbol,claim_token='claim-'+cid,owner_session_id=owner,
            client_order_id=cid,post_bind_token='bind-'+cid,order_request=request(symbol,cid,qty='6'),
            order_role='primary',reserved_risk_usd=1,account_equity_usd=100,budget_fraction=.5,
            account_buying_power_usd=bp,account_multiplier=4,account_scope='alpaca:paper',
            role_metadata={'legacy_timeshare_sizing':True},per_symbol_cap_usd=100))
    db.rollback()
    barrier=Barrier(2)
    def submit(candidate):
        barrier.wait(timeout=10)
        return reserve_alpaca_entry_risk_committed(**candidate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,candidates))
    assert sum(r['ok'] for r in results)==admitted,results
    for result in results:
        if result['ok']:
            assert result['native_crypto_account_bound']['native_cycle_ids']==[CYCLE]
        else:assert result['reason'] in ('ordinary_account_buying_power_exceeded','native_crypto_shared_account_budget_exceeded'),result
