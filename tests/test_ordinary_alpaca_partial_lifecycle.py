"""A full buy instruction may fill in pieces without duplicating reserved risk."""
import uuid

from app.config import settings
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
from tests.test_legacy_timeshare_sizing_escape import _seed_owner
from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID
from tests.test_ordinary_alpaca_transport_budget import _snapshot, _quote


def _reserve(db,symbol,qty,risk):
    owner_id,request = _seed_owner(db,symbol)
    cid = 'partial-' + uuid.uuid4().hex
    key = dict(symbol=symbol,owner_session_id=owner_id,client_order_id=cid,
               claim_token='claim-'+cid,post_bind_token='bind-'+cid,account_scope='alpaca:paper')
    result = claims.reserve_alpaca_entry_risk_committed(
        **key,order_request=request(symbol,cid,qty=str(qty)),order_role='primary',
        reserved_risk_usd=risk,account_equity_usd=1000,account_buying_power_usd=1000,
        budget_fraction=.03,per_symbol_cap_usd=100,role_metadata={'legacy_timeshare_sizing':True})
    assert result['ok'],result
    return key,result


def test_partial_to_terminal_counts_full_pending_then_actual_remaining_position(db,monkeypatch):
    monkeypatch.setattr(settings,'chili_alpaca_paper',True)
    monkeypatch.setattr(settings,'chili_momentum_legacy_alpaca_dispatch_enabled',True)
    first,_ = _reserve(db,'PLFA',10,10)
    assert claims.mark_entry_transport_started_committed(
        **first,alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,ordinary_account_snapshot=_snapshot(),ordinary_quote_check=_quote)
    assert claims.update_action_claim_phase_committed(
        symbol=first['symbol'],claim_token=first['claim_token'],client_order_id=first['client_order_id'],
        broker_order_id='partial-oid',phase='submitted',account_scope='alpaca:paper')
    owner = db.get(TradingAutomationSession,first['owner_session_id'])
    snapshot = dict(owner.risk_snapshot_json)
    snapshot['momentum_live_execution'] = dict(snapshot['momentum_live_execution'],
        entry_client_order_id=first['client_order_id'],entry_order_id='partial-oid',entry_submitted=True,
        position={'quantity':4,'avg_entry_price':10,'stop_price':9})
    owner.risk_snapshot_json = snapshot
    # Persisted position must dominate the still-pending FSM label.
    db.commit()
    _,second = _reserve(db,'PLFB',1,1)
    assert second['covered_partial_position_owner_ids'] == [first['owner_session_id']]
    assert second['account_open_risk_usd'] == 0
    assert second['active_claim_risk_usd'] == 10
    assert second['projected_account_risk_usd'] == 11

    assert claims.resolve_action_claim_committed(
        symbol=first['symbol'],claim_token=first['claim_token'],client_order_id=first['client_order_id'],
        broker_order_id='partial-oid',broker_order_status='canceled',durable_entry_adopted=True,
        account_scope='alpaca:paper')
    _,third = _reserve(db,'PLFC',1,1)
    assert third['covered_partial_position_owner_ids'] == []
    assert third['account_open_risk_usd'] == 4
    assert third['active_claim_risk_usd'] == 1  # still-pending PLFB
    assert third['projected_account_risk_usd'] == 6
