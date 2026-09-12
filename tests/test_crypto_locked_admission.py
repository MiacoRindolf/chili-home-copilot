"""Actual reciprocal SQL admissions and PAPER GET boundary with a fake broker."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.crypto_execution.admission import LockedNativeAdmissionReader
from app.crypto_execution.equity_claims import locked_equity_snapshot,require_equity_account_locks
from app.crypto_execution.paper_http import PaperCycleHTTP
from app.crypto_execution.store import ACCOUNT_LOCK
from tests.test_native_crypto_cycle_store import store,reserve
from tests.test_native_crypto_account_bridge import bind_ordinary_account
from tests.test_native_crypto_owner import Response
from tests.test_native_crypto_lifecycle import CYCLE


def reader(store,tmp_path,*,funding='20',positions=None,orders=None):
    calls=[]
    broker=PaperCycleHTTP(paper=True,account_id=store.account_id,key='fixture-only-key',
        secret='fixture-only-secret',require_authority=lambda account_id:None,
        timeout_seconds=5,max_response_bytes=1000000)
    def open(request,timeout):
        path=request.full_url.removeprefix('https://paper-api.alpaca.markets')
        assert request.get_method()=='GET' and request.data is None
        calls.append(path)
        if path=='/v2/account':
            data=dict(id=store.account_id,equity='1000',currency='USD',status='ACTIVE',crypto_status='ACTIVE',
                non_marginable_buying_power=funding,cash=funding,accrued_fees='0',account_blocked=False,
                trading_blocked=False,trade_suspended_by_user=False)
        elif path=='/v2/positions':data=positions or []
        else:data=[] if 'before_order_id' in path else (orders or [])
        return Response(200,json.dumps(data).encode())
    broker._opener.open=open
    return LockedNativeAdmissionReader(broker,account_risk_fraction='0.03',journal_root=tmp_path,
        max_journal_bytes=1000000,max_order_pages=10),calls


@pytest.mark.parametrize('funding,admitted',[('15',False),('20',True)])
def test_crypto_admission_sees_equity_reservation_even_before_broker_post(store,db,monkeypatch,tmp_path,funding,admitted):
    from tests.test_ordinary_alpaca_transport_budget import _reserve
    bind_ordinary_account(store,monkeypatch)
    key=_reserve(db,'EQCO')
    admission,calls=reader(store,tmp_path,funding=funding)
    if not admitted:
        with pytest.raises(ValueError,match='funding_exceeded'):reserve(store,reader=admission)
        with pytest.raises(ValueError,match='not_found'):store.read(CYCLE)
    else:
        state=reserve(store,reader=admission)
        receipt=state['admission_receipt']
        assert receipt['external_risk_upper_bound']=='10'
        assert receipt['unreflected_debit_upper_bound']=='16.0000700001'
        assert 'equity:'+key['client_order_id'] in receipt['claim_ids']
        evidence=receipt['evidence_receipt'];journal=Path(evidence['journal']['path'])
        assert hashlib.sha256(journal.read_bytes()).hexdigest()==evidence['journal']['sha256']
        assert evidence['equity_ledger']['external_debit_upper_bound']=='10'
        assert store.audit(CYCLE,max_events=1)==state
    assert calls==['/v2/positions','/v2/orders?status=open&limit=500&direction=desc&nested=false','/v2/account']
    assert list(tmp_path.glob('*.jsonl'))


def test_foreign_broker_order_keeps_crypto_claim_uncreated_and_evidence_retained(store,db,monkeypatch,tmp_path):
    bind_ordinary_account(store,monkeypatch)
    admission,_=reader(store,tmp_path,orders=[dict(id=str(uuid4()),client_order_id='manual',
        asset_class='us_equity',symbol='EQ',side='buy')])
    with pytest.raises(ValueError,match='ownership_unverified'):reserve(store,reader=admission)
    records=[json.loads(line) for line in next(tmp_path.glob('*.jsonl')).read_text().splitlines()]
    assert records[-1]['kind']=='broker_ownership' and not records[-1]['receipt']['ok']
    with pytest.raises(ValueError,match='not_found'):store.read(CYCLE)


def test_reader_never_uses_a_second_connection_for_journaling_inside_shared_lock(store,db,monkeypatch,tmp_path):
    from tests.test_ordinary_alpaca_owned_positions import _held
    bind_ordinary_account(store,monkeypatch)
    _held(db)
    admission,_=reader(store,tmp_path,positions=[dict(asset_id=str(uuid4()),symbol='HLDA',
        qty='10',side='long',asset_class='us_equity')])
    monkeypatch.setattr(store,'record_evidence',lambda *_:pytest.fail('would deadlock on another account-lock connection'))
    state=reserve(store,reader=admission)
    assert state['admission_receipt']['external_risk_upper_bound']=='1'
    assert state['admission_receipt']['evidence_receipt']['broker_ownership']['position_count']==1


@pytest.mark.parametrize('change',[dict(qty='2'),dict(limit_price='11')])
def test_owned_cid_does_not_certify_a_broker_order_with_larger_quantity_or_price(store,db,monkeypatch,tmp_path,change):
    from tests.test_ordinary_alpaca_transport_budget import _reserve
    bind_ordinary_account(store,monkeypatch)
    key=_reserve(db,'BOUND')
    order=dict(id=str(uuid4()),client_order_id=key['client_order_id'],asset_class='us_equity',
        symbol='BOUND',side='buy',type='limit',qty='1',limit_price='10')
    order.update(change)
    admission,_=reader(store,tmp_path,orders=[order])
    with pytest.raises(ValueError,match='buy_instruction_changed'):reserve(store,reader=admission)
    with pytest.raises(ValueError,match='not_found'):store.read(CYCLE)


def test_missing_lock_or_failed_fsync_cannot_create_native_claim(store,db,monkeypatch,tmp_path):
    bind_ordinary_account(store,monkeypatch)
    admission,calls=reader(store,tmp_path)
    with store.engine.begin() as c:
        with pytest.raises(ValueError,match='account_lock_required'):admission(c)
        c.execute(text('SELECT pg_advisory_xact_lock(:key)'),dict(key=ACCOUNT_LOCK))
        with pytest.raises(ValueError,match='adaptive_account_lock_required'):require_equity_account_locks(c)
    assert calls==[] and list(tmp_path.iterdir())==[]
    def fail_fsync(_):raise OSError('test fsync unavailable')
    monkeypatch.setattr('app.crypto_execution.admission.os.fsync',fail_fsync)
    with pytest.raises(OSError):reserve(store,reader=admission)
    assert calls==[]
    with pytest.raises(ValueError,match='not_found'):store.read(CYCLE)


@pytest.mark.parametrize('funding,expected',[(10,1),(20,2)])
def test_simultaneous_native_and_equity_writers_admit_all_and_only_affordable_claims(store,db,monkeypatch,tmp_path,funding,expected):
    from tests.test_legacy_timeshare_sizing_escape import _seed_owner
    from app.services.trading.momentum_neural.alpaca_orphan_claims import reserve_alpaca_entry_risk_committed
    bind_ordinary_account(store,monkeypatch)
    sid,request=_seed_owner(db,'EQRACE');cid='race-'+uuid4().hex
    db.rollback()
    admission,_=reader(store,tmp_path,funding=str(funding));barrier=Barrier(2)
    def native():
        barrier.wait(timeout=10)
        try:reserve(store,reader=admission);return True
        except ValueError as error:
            assert str(error)=='native_cycle_funding_exceeded_or_unavailable';return False
    def equity():
        barrier.wait(timeout=10)
        result=reserve_alpaca_entry_risk_committed(symbol='EQRACE',owner_session_id=sid,
            claim_token='claim-'+cid,client_order_id=cid,post_bind_token='bind-'+cid,
            order_request=request('EQRACE',cid,qty='1'),order_role='primary',reserved_risk_usd=1,
            account_equity_usd=1000,account_buying_power_usd=funding,account_multiplier=1,
            budget_fraction=.03,account_scope='alpaca:paper',per_symbol_cap_usd=100,
            role_metadata={'legacy_timeshare_sizing':True})
        if not result['ok']:assert result['reason']=='native_crypto_shared_account_budget_exceeded',result
        return result['ok']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(native),pool.submit(equity)]
        results=[f.result(timeout=30) for f in futures]
    assert sum(results)==expected,results
