from concurrent.futures import ThreadPoolExecutor
from decimal import localcontext
from fractions import Fraction as F
from itertools import combinations
from threading import Barrier
from uuid import UUID
import pytest
from sqlalchemy import text

from app.crypto_execution.allocation import allocate_native_lots
from tests.test_native_crypto_cycle_store import store,admission,reserve,another
from tests.test_native_crypto_lifecycle import ASSET


def candidate(n,*,price='1',minimum='1',step='1'):
    return dict(cycle_id=str(UUID(int=100+n)),asset=dict(ASSET,id=str(UUID(int=200+n)),symbol=f'COIN{n}/USD',
        min_order_size=minimum,min_trade_increment=step,price_increment='0.1'),
        limit_price=price,context_sha256=f'{n:064x}',opportunity_key=f'{n+1000:064x}',client_order_id='candidate-'+str(n))


def allocate(rows,funding='100',risk='100'):
    return allocate_native_lots(rows,funding_available=funding,risk_available=risk)


def test_all_affordable_candidates_receive_native_lots_without_top_n():
    rows=[candidate(1,price='3',minimum='3',step='2'),candidate(2,price='2'),candidate(3,price='1')]
    r=allocate(rows,funding='60',risk='60')
    assert r['allocated_count']==3 and not r['minimum_lot_resource_conflict']
    assert r['continuous_notional_level']==dict(numerator=20,denominator=1)
    assert [a['quote_debit'] for a in r['allocated']]==['18','20','20']
    assert r['unallocated_rounding_or_unaffordable_residual']=='2'
    assert not r['profit_optimality_claimed'] and not r['order_authority']


def test_water_level_respects_large_broker_minimum_without_starving_other_candidate():
    r=allocate([candidate(1,minimum='100'),candidate(2)],funding='150',risk='200')
    assert [a['quantity'] for a in r['allocated']]==['100','50']
    assert r['usable_budget']=='150' and r['continuous_notional_level']==dict(numerator=50,denominator=1)


def test_actual_resource_conflict_maximizes_count_not_first_symbol_or_price_momentum_rank():
    r=allocate([candidate(1,price='6'),candidate(2,price='3'),candidate(3,price='3')],funding='6',risk='9')
    assert r['minimum_lot_resource_conflict'] and r['allocated_count']==2
    assert [a['symbol'] for a in r['allocated']]==['COIN2/USD','COIN3/USD']
    assert r['deferred'][0]['reason']=='minimum_lots_exceed_shared_budget'


def test_maximum_participation_matches_exhaustive_feasible_subsets():
    costs=[2,3,5,7,11]
    rows=[candidate(i+1,price=str(c)) for i,c in enumerate(costs)]
    for budget in range(29):
        best=max(k for k in range(6) if any(sum(s)<=budget for s in combinations(costs,k)))
        r=allocate(rows,funding=str(budget),risk='100')
        assert r['allocated_count']==best and F(r['allocated_quote_debit'])<=budget


def test_exact_quantities_are_independent_of_ambient_decimal_precision():
    rows=[candidate(1,price='60000.1',minimum='0.000001',step='0.000000001'),
          candidate(2,price='2000.3',minimum='0.000001',step='0.000000001')]
    expected=allocate(rows,funding='10',risk='10')
    with localcontext() as ctx:
        ctx.prec=2
        assert allocate(rows,funding='10',risk='10')==expected
    for a,o in zip(expected['allocated'],rows):
        assert (F(a['quantity'])/F(o['asset']['min_trade_increment'])).denominator==1


@pytest.mark.parametrize('fault',['float','duplicate','non_usd','inactive'])
def test_invalid_native_allocation_inputs_never_become_rounded_funding_authority(fault):
    rows=[candidate(1)]
    if fault=='float':rows[0]['limit_price']=1.0
    if fault=='duplicate':rows.append(dict(rows[0]))
    if fault=='non_usd':rows[0]['asset']['symbol']='COIN1/BTC'
    if fault=='inactive':rows[0]['asset']['tradable']=False
    with pytest.raises(ValueError):allocate(rows)


def test_batch_reserves_all_candidates_with_one_locked_snapshot_and_replays_without_resizing(store):
    calls=[]
    def reader(c):
        from app.crypto_execution.equity_claims import require_equity_account_locks
        require_equity_account_locks(c);calls.append(True)
        return admission(c,funding='10',risk='10')
    rows=[candidate(1),candidate(2)]
    result=store.reserve_all(rows,admission_reader=reader)
    assert len(result['created'])==2 and len(calls)==1
    assert [s['instruction']['qty'] for s in result['created']]==['5','5']
    for state in result['created']:
        receipt=state['admission_receipt']
        assert receipt['account_risk_required']=='10'
        assert len(receipt['claim_ids'])==3  # includes the explicit zero external claim
        assert store.audit(state['cycle_id'],max_events=10)==state
    # A fresh source publication can carry the same economic opportunity key.
    replay=[dict(o,context_sha256='f'*64) for o in rows]
    again=store.reserve_all(replay,admission_reader=reader)
    assert not again['created'] and len(again['reused'])==2 and len(calls)==1
    assert [s['instruction']['qty'] for s in again['reused']]==['5','5']
    with pytest.raises(ValueError,match='candidate_replay_changed'):
        store.reserve_all([dict(rows[0],opportunity_key='e'*64)],admission_reader=reader)


def test_existing_native_and_external_claims_are_included_before_allocating_new_quantities(store):
    held=reserve(store)
    def reader(c):
        r=admission(c,funding='20',risk='12',external='1');r['external_risk_upper_bound']='2'
        return r
    result=store.reserve_all([candidate(1,minimum='.1',step='.1'),candidate(2,minimum='.1',step='.1')],admission_reader=reader)
    assert len(result['created'])==2
    allocation=result['allocation'];expected=F(12)-2-F(held['original_debit'])
    assert F(allocation['risk_available'])==expected
    assert F(allocation['funding_available'])==20-1-F(held['original_debit'])
    assert sum(F(s['original_debit']) for s in result['created'])<=expected


def test_owned_asset_does_not_prevent_other_candidates_from_being_considered(store):
    held=reserve(store);o=candidate(1)
    o['asset']=ASSET;o['limit_price']='60000.1'
    result=store.reserve_all([o,candidate(2)],admission_reader=lambda c:admission(c,funding='20',risk='20'))
    assert len(result['already_owned'])==1 and result['already_owned'][0]['existing_cycle_id']==held['cycle_id']
    assert len(result['created'])==1 and result['created'][0]['asset']['symbol']=='COIN2/USD'


def test_partial_batch_insert_failure_rolls_back_all_cycle_and_event_rows(store,monkeypatch):
    insert=store._insert;count=0
    def failing(c,state):
        nonlocal count
        count+=1
        if count==2:raise OSError('injected storage failure')
        insert(c,state)
    monkeypatch.setattr(store,'_insert',failing)
    with pytest.raises(OSError):store.reserve_all([candidate(1),candidate(2)],admission_reader=admission)
    with store.engine.connect() as c:
        assert c.execute(text(f'SELECT count(*) FROM {store.cycles}')).scalar_one()==0
        assert c.execute(text(f'SELECT count(*) FROM {store.events}')).scalar_one()==0


def test_competing_batches_cannot_spend_same_account_budget_twice(store):
    gate=Barrier(2)
    def worker(n):
        gate.wait()
        return another(store).reserve_all([candidate(n)],admission_reader=admission)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,[1,2]))
    assert sum(len(r['created']) for r in results)==1
    assert sum(F(s['original_debit']) for r in results for s in r['created'])==10
