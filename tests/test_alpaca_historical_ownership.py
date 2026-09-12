"""Old unbound terminal flags cannot freeze current PAPER or authorize exposure."""
from uuid import uuid4

import pytest

from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import alpaca_orphan_claims as claims,live_runner
from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID,_claim
from tests.test_legacy_timeshare_sizing_escape import _seed_owner


def historical(db,*,state='live_error',position=None):
    sid,_=_seed_owner(db,'HIST')
    row=db.get(TradingAutomationSession,sid)
    row.state=state
    row.risk_snapshot_json=dict(momentum_live_execution=dict(side_long=True,entry_submitted=True,
        entry_order_id=str(uuid4()),entry_client_order_id='historical-cid',position=position))
    db.commit()
    return row


def check(**changes):
    values=dict(account_scope='alpaca:paper',alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
                broker_positions=[],broker_orders=[])
    values.update(changes)
    return claims.certify_alpaca_owned_entry_posture_committed(**values)


def test_terminal_unbound_history_without_current_exposure_does_not_block_account(db):
    row=historical(db);sid=row.id
    result=check()
    assert result['ok'],result
    assert result['historical_unbound_session_ids']==[sid]
    assert result['position_count']==result['open_order_count']==0
    assert live_runner._strict_alpaca_account_identity(object(),row)[1]['reason']=='alpaca_account_identity_unfrozen'
    db.refresh(row)
    assert row.risk_snapshot_json['momentum_live_execution']['entry_submitted'] is True


@pytest.mark.parametrize('broker_evidence',['order_id','client_id','position'])
def test_excluded_history_never_whitelists_an_actual_broker_exposure(db,broker_evidence):
    row=historical(db);live=row.risk_snapshot_json['momentum_live_execution']
    if broker_evidence=='position':
        result=check(broker_positions=[dict(product_id='HIST',qty=1,side='long',asset_class='us_equity')])
    else:
        order=(dict(id=live['entry_order_id'],client_order_id='new') if broker_evidence=='order_id'
               else dict(id=str(uuid4()),client_order_id=live['entry_client_order_id']))
        result=check(broker_orders=[order])
    assert not result['ok'],result


@pytest.mark.parametrize('case',['active','held','claimed','bound_foreign'])
def test_current_or_economic_rows_still_require_account_identity(db,case):
    row=historical(db,state='live_pending_entry' if case=='active' else 'live_error',
                   position=dict(quantity=1,avg_entry_price=10) if case=='held' else None)
    if case=='claimed':
        _claim(db,symbol='HIST',owner_session_id=row.id,token='active-claim',cid='historical-cid',
            phase='claimed',role='primary',reserved_risk_usd=1)
        db.commit()
    if case=='bound_foreign':
        row.risk_snapshot_json=dict(row.risk_snapshot_json,alpaca_account_scope='alpaca:paper',alpaca_account_id=str(uuid4()))
        db.commit()
    result=check()
    assert not result['ok'] and result['reason']=='alpaca_account_generation_mismatch',result
