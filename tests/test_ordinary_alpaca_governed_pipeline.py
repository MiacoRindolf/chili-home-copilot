"""Ordinary multi-symbol path with real persisted owners, claims and POST fence."""
from dataclasses import replace
import pytest

from app.config import settings
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr, alpaca_orphan_claims as claims
from app.services.trading.venue.protocol import FreshnessMeta
from tests.test_legacy_timeshare_sizing_escape import _seed_owner
from tests.test_ordinary_alpaca_owned_positions import _held
from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID
from tests.test_alpaca_governed_place_bbo import _alpaca_session, _CertifiedAdapter, _fresh, _rail, _tick


@pytest.mark.parametrize('final_bp,expire_after_scan', [(1000,False),(0,False),(1000,True)])
def test_real_ordinary_pipeline_with_held_sibling_and_changing_broker_budget(db,monkeypatch,final_bp,expire_after_scan):
    monkeypatch.setattr(settings,'chili_alpaca_paper',True)
    monkeypatch.setattr(settings,'chili_alpaca_expected_account_id',TEST_ALPACA_ACCOUNT_ID)
    monkeypatch.setattr(settings,'chili_momentum_legacy_alpaca_dispatch_enabled',True)
    monkeypatch.setattr('app.services.trading.momentum_neural.market_profile.market_session_now',
                        lambda _symbol, *, now=None:'regular')
    # These unrelated financial-breaker fixtures supply healthy account status;
    # ownership, reservation, revalidation, release, and transport CAS stay real.
    monkeypatch.setattr(lr,'_final_alpaca_financial_breaker_admission',
                        lambda _sess, *, phase:(True,{'ok':True,'phase':phase}))
    monkeypatch.setattr(lr,'_record_final_alpaca_breaker_admission',lambda *_a:None)
    monkeypatch.setattr('app.services.trading.momentum_neural.risk_policy._account_equity_usd',
                        lambda *_a,**_k:10000)
    _held(db)
    owner_id, _ = _seed_owner(db,'ACTU')
    owner = db.get(TradingAutomationSession,owner_id)
    snapshot = _alpaca_session().risk_snapshot_json
    snapshot['alpaca_account_id'] = TEST_ALPACA_ACCOUNT_ID
    snapshot['confirmed_arm_generation'].update(
        session_id=owner_id,alpaca_account_id=TEST_ALPACA_ACCOUNT_ID)
    owner.risk_snapshot_json = snapshot
    db.commit()
    quote_expired = [False]
    if expire_after_scan:
        revalidate = claims._revalidate_ordinary_entry_before_transport
        def aging_scan(*args,**kwargs):
            result = revalidate(*args,**kwargs)
            quote_expired[0] = True
            return result
        monkeypatch.setattr(claims,'_revalidate_ordinary_entry_before_transport',aging_scan)

    class AgingFreshness(FreshnessMeta):
        def age_seconds(self,*,now=None):
            if quote_expired[0]:
                return self.max_age_seconds + 1
            return super().age_seconds(now=now)

    class Broker(_CertifiedAdapter):
        reads = 0
        def get_account_snapshot(self):
            self.reads += 1
            return {'ok':True,'paper':True,'account_id':TEST_ALPACA_ACCOUNT_ID,
                    'equity':10000,'last_equity':10000,'status':'ACTIVE',
                    'buying_power':1000 if self.reads <= 2 else final_bp}
        def get_execution_bbo(self,_symbol,*,max_age_seconds):
            tick = _tick('ACTU')
            meta = tick.freshness
            tick = replace(tick,freshness=AgingFreshness(meta.retrieved_at_utc,
                           meta.provider_time_utc,meta.max_age_seconds))
            return tick,tick.freshness
        def list_positions(self):
            return [{'product_id':'HLDA','qty':10,'side':'long'}],_fresh()
        def list_open_orders(self,*,strict):
            assert strict
            return [],_fresh()

    posts = []
    def post(**kw):
        posts.append(kw)
        return {'ok':True,'order_id':'pipeline-new-symbol','client_order_id':kw['client_order_id'],
                'status':'open'}
    result = lr._governed_place(
        Broker(),post,sess=owner,rail_reservation=_rail(),alpaca_order_role='primary',
        alpaca_risk_stop_price=9.50,product_id='ACTU',side='buy',position_intent='buy_to_open',
        base_size='5',limit_price='10.50',client_order_id='pipeline-candidate',
        extended_hours=False,time_in_force='day')
    readable, claim = claims.read_action_claim_committed(symbol='ACTU',account_scope='alpaca:paper')
    assert readable and claim,result
    detail = claim['metadata']['entry_financial_revalidation']
    assert detail['account_buying_power_usd'] == final_bp
    assert detail['account_open_risk_usd'] == 1
    if final_bp and not expire_after_scan:
        assert result['ok'],result
        assert len(posts) == 1
        assert claim['phase'] == 'submitted'
        assert detail['ok'] is True
    else:
        assert not result['ok'] and posts == [],result
        assert result['entry_claim_pre_post_released'] is True
        assert claim['phase'] == 'resolved'
        if expire_after_scan:
            assert claim['metadata']['entry_quote_revalidation']['reason'] == 'ordinary_transport_quote_stale'
        else:
            assert detail['reason'] == 'ordinary_account_buying_power_exceeded'
        assert 'entry_transport_started' not in claim['metadata']
