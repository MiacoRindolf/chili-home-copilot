"""Real committed ledger and broker-ownership checks, without broker transport."""
from types import SimpleNamespace
import uuid

import pytest

from app.config import settings
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.alpaca_orphan_claims import reserve_alpaca_entry_risk_committed
from tests.test_legacy_timeshare_sizing_escape import _seed_owner
from tests.test_alpaca_governed_place_bbo import _fresh


def _held(db, symbol='HLDA'):
    owner_id, _ = _seed_owner(db, symbol)
    owner = db.get(TradingAutomationSession, owner_id)
    snapshot = dict(owner.risk_snapshot_json)
    snapshot['momentum_live_execution'] = dict(snapshot['momentum_live_execution'],
        entry_client_order_id='held-entry-a', position={
            'quantity':10, 'avg_entry_price':10, 'stop_price':9.9})
    owner.risk_snapshot_json = snapshot
    owner.state = 'live_cancelled'
    db.commit()
    return owner


def test_known_held_symbol_allows_another_within_total_risk_and_bp(db, monkeypatch):
    monkeypatch.setattr(settings,'chili_alpaca_paper',True)
    monkeypatch.setattr(settings,'chili_momentum_legacy_alpaca_dispatch_enabled',True)
    _held(db)
    owner_id, request = _seed_owner(db, 'HLDB')
    cid = 'held-sibling-' + uuid.uuid4().hex[:10]
    reply = reserve_alpaca_entry_risk_committed(
        symbol='HLDB',claim_token='claim-' + cid,owner_session_id=owner_id,client_order_id=cid,
        post_bind_token='bind-' + cid,order_request=request('HLDB',cid,qty='1'),order_role='primary',
        reserved_risk_usd=1,account_equity_usd=100,budget_fraction=.03,
        account_buying_power_usd=100,account_scope='alpaca:paper',
        role_metadata={'legacy_timeshare_sizing':True},per_symbol_cap_usd=100)
    assert reply['ok'], reply
    assert reply['account_open_risk_usd'] == 1
    assert reply['projected_account_risk_usd'] == 2


@pytest.mark.parametrize('bp,reason',[(None,'ordinary_account_buying_power_unavailable'),
                                     (1,'ordinary_account_buying_power_exceeded')])
def test_held_account_needs_actual_remaining_buying_power(db,monkeypatch,bp,reason):
    monkeypatch.setattr(settings,'chili_alpaca_paper',True)
    monkeypatch.setattr(settings,'chili_momentum_legacy_alpaca_dispatch_enabled',True)
    _held(db)
    owner_id, request = _seed_owner(db,'BPBB')
    cid = 'bp-' + uuid.uuid4().hex[:10]
    reply = reserve_alpaca_entry_risk_committed(
        symbol='BPBB',claim_token='claim-' + cid,owner_session_id=owner_id,client_order_id=cid,
        post_bind_token='bind-' + cid,order_request=request('BPBB',cid,qty='1'),order_role='primary',
        reserved_risk_usd=1,account_equity_usd=100,budget_fraction=.03,
        account_buying_power_usd=bp,account_scope='alpaca:paper',
        role_metadata={'legacy_timeshare_sizing':True},per_symbol_cap_usd=100)
    assert not reply['ok'] and reply['reason'] == reason, reply


def test_broker_owned_posture_matches_direction_quantity_and_unowned_orders(db):
    owner = _held(db)
    class Adapter:
        positions = [{'product_id':'HLDA','qty':10,'side':'long','asset_class':'us_equity'}]
        orders = []
        def list_positions(self): return self.positions, _fresh()
        def list_open_orders(self,*,strict):
            assert strict
            return self.orders, _fresh()
    adapter = Adapter()
    ok, receipt = lr._strict_alpaca_owned_entry_posture(adapter,owner)
    assert ok and receipt['owned_position_symbols'] == ['HLDA'],receipt
    assert lr._strict_alpaca_empty_entry_posture(adapter)[0] is False
    for qty, side in ((-10,'short'),(10,'short'),(10,None),(11,'long')):
        adapter.positions = [{'product_id':'HLDA','qty':qty,'side':side}]
        assert lr._strict_alpaca_owned_entry_posture(adapter,owner)[0] is False
    adapter.positions = [{'product_id':'HLDA','qty':10,'side':'long'}]
    adapter.orders = [{'id':'manual-order','client_order_id':'unowned'}]
    assert lr._strict_alpaca_owned_entry_posture(adapter,owner)[1]['reason'] == 'alpaca_unowned_open_order_present'


def test_adapter_retains_broker_position_side(monkeypatch):
    from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter
    adapter = object.__new__(AlpacaSpotAdapter)
    rows = [SimpleNamespace(symbol='LONG',qty='3',side=SimpleNamespace(value='long')),
            SimpleNamespace(symbol='SHORT',qty='-3',side=SimpleNamespace(value='short')),
            SimpleNamespace(symbol='UNKNOWN',qty='3')]
    monkeypatch.setattr(adapter,'_account_client',lambda:SimpleNamespace(get_all_positions=lambda:rows))
    positions, _ = adapter.list_positions()
    assert [(p['product_id'],p['qty'],p['side']) for p in positions] == [
        ('LONG',3,'long'),('SHORT',-3,'short'),('UNKNOWN',3,'')]


@pytest.mark.parametrize('manual_after_reservation', [False, True])
def test_ordinary_second_symbol_checks_real_ownership_at_both_post_boundaries(
    db, monkeypatch, manual_after_reservation,
):
    """Exercise the consumer branch and committed ownership scan with fake HTTP.

    Reservation arithmetic/concurrency has separate committed-DB tests. Here its
    receipt is injected to isolate the literal POST's ownership recheck.
    """
    from tests.test_alpaca_governed_place_bbo import (
        TEST_ALPACA_ACCOUNT_ID, _alpaca_session, _CertifiedAdapter,
        _creator_reservation, _rail, _tick,
    )
    monkeypatch.setattr(settings, 'chili_alpaca_paper', True)
    monkeypatch.setattr(settings, 'chili_alpaca_expected_account_id', TEST_ALPACA_ACCOUNT_ID)
    monkeypatch.setattr(settings, 'chili_momentum_legacy_alpaca_dispatch_enabled', True)
    monkeypatch.setattr('app.services.trading.momentum_neural.market_profile.market_session_now',
                        lambda _symbol, *, now=None: 'regular')
    owner = _held(db)
    owner.risk_snapshot_json = dict(owner.risk_snapshot_json, alpaca_account_id=TEST_ALPACA_ACCOUNT_ID)
    db.commit()

    class Adapter(_CertifiedAdapter):
        posture_reads = 0
        def __init__(self):
            self.account_reads = []
        def get_account_snapshot(self):
            snapshot = dict(super().get_account_snapshot(), equity=10000, last_equity=10000,
                            buying_power=12345 + len(self.account_reads), status='ACTIVE')
            self.account_reads.append(snapshot)
            return snapshot
        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            tick = _tick('ACTU')
            return tick, tick.freshness
        def list_positions(self):
            self.posture_reads += 1
            return [{'product_id':'HLDA','qty':10,'side':'long'}], _fresh()
        def list_open_orders(self, *, strict):
            assert strict
            orders = ([{'id':'manual','client_order_id':'manual'}]
                      if manual_after_reservation and self.posture_reads == 2 else [])
            return orders, _fresh()

    frozen, releases, transports, posts = {}, [], [], []
    monkeypatch.setattr(lr, 'reserve_alpaca_entry_risk_committed', _creator_reservation(frozen))
    monkeypatch.setattr(lr, 'release_entry_claim_pre_post_committed',
                        lambda **kw: releases.append(kw) or True)
    monkeypatch.setattr(lr, 'mark_entry_transport_started_committed',
                        lambda **kw: transports.append(kw) or True)
    monkeypatch.setattr(lr, 'update_action_claim_phase_committed', lambda **kw: True)
    monkeypatch.setattr(lr, '_final_alpaca_financial_breaker_admission',
                        lambda _sess, *, phase: (True, {'ok':True,'phase':phase}))
    monkeypatch.setattr(lr, '_record_final_alpaca_breaker_admission', lambda *_a: None)
    monkeypatch.setattr('app.services.trading.momentum_neural.risk_policy._account_equity_usd',
                        lambda *_a, **_kw: 10000)

    def post(**kw):
        posts.append(kw)
        return {'ok':True,'order_id':'new-symbol-order','client_order_id':kw['client_order_id'],
                'status':'open'}

    adapter = Adapter()
    result = lr._governed_place(
        adapter, post, sess=_alpaca_session(), rail_reservation=_rail(),
        alpaca_order_role='primary', alpaca_risk_stop_price=9.50,
        product_id='ACTU', side='buy', position_intent='buy_to_open',
        base_size='5', limit_price='10.50', client_order_id='second-symbol-governed',
        extended_hours=False, time_in_force='day',
    )
    assert adapter.posture_reads == 2, result
    assert frozen['account_buying_power_usd'] == adapter.account_reads[1]['buying_power']
    assert frozen['role_metadata']['broker_account_posture']['posture_contract'] == 'owned_exposure'
    if manual_after_reservation:
        assert result['error'] == 'alpaca_unowned_open_order_present', result
        assert result['entry_claim_pre_post_released'] is True
        assert len(releases) == 1 and transports == posts == []
    else:
        assert result['ok'], result
        assert len(posts) == len(transports) == 1 and releases == []
        assert posts[0]['limit_price'] == frozen['order_request']['limit_price']
        assert transports[0]['ordinary_account_snapshot']['buying_power'] == adapter.account_reads[-1]['buying_power']
        assert transports[0]['ordinary_account_snapshot']['buying_power'] != frozen['account_buying_power_usd']
