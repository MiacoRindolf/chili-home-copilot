"""Native admission sees real adaptive packet/events and deduplicates proven CIDs."""
from decimal import Decimal
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.crypto_execution.equity_claims import locked_equity_snapshot
from app.crypto_execution.lifecycle import decimal_text
from tests.test_native_crypto_cycle_store import store
from tests.test_native_crypto_account_bridge import bind_ordinary_account


def setup_account(store,monkeypatch):
    from tests.test_alpaca_locked_daily_pnl_authority import ACCOUNT_ID
    bind_ordinary_account(store,monkeypatch)
    store.account_id=ACCOUNT_ID
    monkeypatch.setattr(settings,'chili_alpaca_expected_account_id',ACCOUNT_ID)


def read(store):
    with store.engine.begin() as c:
        store._lock(c)
        return locked_equity_snapshot(c,account_id=store.account_id)


def independent_adaptive(db):
    from tests.test_alpaca_locked_daily_pnl_authority import _broker_facts,_request_for_bundle
    from app.services.trading.momentum_neural.adaptive_risk_reservation import AdaptiveRiskReservationStore
    from app.services.trading.momentum_neural.adaptive_risk_policy import resolve_adaptive_risk
    adaptive=AdaptiveRiskReservationStore(db.get_bind());cid='native-independent-'+uuid4().hex
    db.rollback()
    with db.begin():
        bundle=adaptive.lock_alpaca_paper_admission_bundle(broker_account_facts=_broker_facts(decision_id=cid),
            symbol='EQAD',correlation_cluster='equity:eqad',session=db)
        request=_request_for_bundle(bundle,decision_id=cid,symbol='EQAD',correlation_cluster='equity:eqad')
        resolved=resolve_adaptive_risk(request.policy,request.inputs)
        decision=adaptive.reserve(request,session=db,locked_alpaca_paper_bundle=bundle,
            prepared_resolution=resolved,prepared_decision_packet=resolved.to_decision_packet())
        assert decision.admission_accepted,decision
    return request,decision


def test_standalone_adaptive_reservation_is_visible_without_ordinary_claim_or_position(store,db,monkeypatch):
    setup_account(store,monkeypatch)
    request,decision=independent_adaptive(db)
    result=read(store)
    assert len(result['external_claims'])==1
    assert result['external_claims'][0].claim_id=='equity:'+request.client_order_id
    assert result['external_claims'][0].debit_upper_bound==decision.gross_notional_usd
    assert decision.gross_notional_usd>=Decimal(str(decision.quantity_shares))*Decimal(str(request.entry_limit_price))
    assert Fraction(result['external_risk_upper_bound'])==Fraction(str(decision.structural_risk_usd))
    assert result['receipt']['ordinary_instructions']=={}
    assert result['receipt']['adaptive'][0]['state']=='reserved'


def test_verified_adaptive_precommit_and_ordinary_claim_are_one_pending_instruction(store,db,monkeypatch):
    from tests.test_adaptive_alpaca_lifecycle import _live_session,_precommit_adaptive_reservation
    setup_account(store,monkeypatch)
    session=_live_session(db,symbol='EQDU');cid='native-duplicate-'+uuid4().hex
    request,decision=_precommit_adaptive_reservation(db,session,symbol='EQDU',cid=cid)
    db.rollback()
    result=read(store)
    assert result['receipt']['deduplicated_pending_client_ids']==[cid]
    assert len(result['external_claims'])==1
    assert result['external_claims'][0].debit_upper_bound==max(decision.gross_notional_usd,
        Decimal(str(decision.quantity_shares))*Decimal(str(request.entry_limit_price)))
    assert Fraction(result['external_risk_upper_bound'])==Fraction(str(decision.structural_risk_usd))


@pytest.mark.parametrize('corruption',['head','pending_gross','packet_account'])
def test_adaptive_projection_or_identity_cannot_hide_reserved_dollars(store,db,monkeypatch,corruption):
    setup_account(store,monkeypatch)
    request,decision=independent_adaptive(db)
    if corruption=='head':
        with pytest.raises(DBAPIError,match='event head regressed'):
            db.execute(text('UPDATE adaptive_risk_reservations SET last_event_sha256=:sha WHERE reservation_id=:id'),
                dict(sha='0'*64,id=decision.reservation_id))
        db.rollback()
        assert read(store)['external_claims'][0].debit_upper_bound==decision.gross_notional_usd
        return
    if corruption=='packet_account':
        # Querying for another account must reject this same scope's active row;
        # no immutable packet mutation or fake reinterpretation of its identity.
        store.account_id=str(uuid4())
    else:
        column='last_event_sha256' if corruption=='head' else 'pending_gross_notional_usd'
        value='0'*64 if corruption=='head' else Decimal(0)
        db.execute(text(f'UPDATE adaptive_risk_reservations SET {column}=:value WHERE reservation_id=:id'),
            dict(value=value,id=decision.reservation_id))
        db.commit()
    with pytest.raises(ValueError):read(store)
