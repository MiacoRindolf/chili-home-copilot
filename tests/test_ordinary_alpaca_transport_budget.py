"""Real committed account revalidation at the one-way POST permission boundary."""
from datetime import datetime, timezone
import uuid

import pytest

from app.config import settings
from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID
from tests.test_legacy_timeshare_sizing_escape import _seed_owner


def _reserve(db, symbol):
    owner_id, request = _seed_owner(db, symbol)
    cid = 'transport-' + uuid.uuid4().hex
    key = dict(symbol=symbol, owner_session_id=owner_id, client_order_id=cid,
               claim_token='claim-' + cid, post_bind_token='bind-' + cid,
               account_scope='alpaca:paper')
    reply = claims.reserve_alpaca_entry_risk_committed(
        **key, order_request=request(symbol,cid,qty='1'),order_role='primary',
        reserved_risk_usd=10,account_equity_usd=1000,account_buying_power_usd=1000,
        budget_fraction=.03,per_symbol_cap_usd=100,
        role_metadata={'legacy_timeshare_sizing':True})
    assert reply['ok'], reply
    return dict(key, alpaca_account_id=TEST_ALPACA_ACCOUNT_ID)


def _snapshot(**changes):
    value = dict(account_id=TEST_ALPACA_ACCOUNT_ID, paper=True, equity=1000,
                 buying_power=1000, checked_at_utc=datetime.now(timezone.utc).isoformat())
    value.update(changes)
    return value


@pytest.fixture(autouse=True)
def _ordinary_paper(monkeypatch):
    monkeypatch.setattr(settings,'chili_alpaca_paper',True)
    monkeypatch.setattr(settings,'chili_momentum_legacy_alpaca_dispatch_enabled',True)


def _read(key):
    readable, claim = claims.read_action_claim_committed(
        symbol=key['symbol'], account_scope=key['account_scope'])
    assert readable and claim
    return claim


def test_post_boundary_recomputes_sibling_risk_and_bp_and_consumes_only_once(db):
    first, second = _reserve(db,'PSTA'), _reserve(db,'PSTB')
    # Both reserved against $30 risk and $1000 BP. At literal POST the two
    # frozen instructions still require $20 total, regardless of their rank.
    bad_snapshots = [
        _snapshot(equity=500),  # shared risk is 20 > 500*.03
        _snapshot(buying_power=15),  # both instructions need 20, not just candidate 10
        _snapshot(buying_power=None), _snapshot(buying_power=True),
        _snapshot(account_id='another-account'), _snapshot(paper=False),
    ]
    assert not claims.mark_entry_transport_started_committed(**first)
    for snapshot in bad_snapshots:
        assert not claims.mark_entry_transport_started_committed(
            **first,ordinary_account_snapshot=snapshot)
        observed = _read(first)
        assert observed['phase'] == 'claimed'
        assert 'entry_transport_started' not in observed['metadata']
        assert observed['metadata']['entry_financial_revalidation']['ok'] is False
    assert claims.mark_entry_transport_started_committed(
        **first,ordinary_account_snapshot=_snapshot(buying_power=20))
    observed = _read(first)
    assert observed['phase'] == 'submit_indeterminate'
    check = observed['metadata']['entry_financial_revalidation']
    assert check['projected_account_risk_usd'] == 20
    assert check['buying_power_required_upper_bound_usd'] == 20
    assert check['account_budget_usd'] == 30
    assert check['reason'] == 'ordinary_transport_budget_revalidated'
    assert not claims.mark_entry_transport_started_committed(
        **first,ordinary_account_snapshot=_snapshot())
    assert claims.mark_entry_transport_started_committed(
        **second,ordinary_account_snapshot=_snapshot(buying_power=20))


def test_revalidation_cannot_resurrect_a_released_creator_generation(db):
    key = _reserve(db,'PSTR')
    assert claims.release_entry_claim_pre_post_committed(**key,reason='test-no-http')
    assert not claims.mark_entry_transport_started_committed(
        **key,ordinary_account_snapshot=_snapshot())
    claim = _read(key)
    assert claim['phase'] == 'resolved'
    assert 'entry_transport_started' not in claim['metadata']


def test_release_between_initial_read_and_account_lock_cannot_be_rebound(db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    key = _reserve(db,'PSTW')
    # Seed/reservation commits are complete. Return the fixture's read-only
    # checkout before two workers use the deliberately two-slot pytest pool.
    db.rollback()
    before_lock, released = Event(), Event()
    reserve = claims._reserve_alpaca_entry_risk
    def paused_reserve(*args, **kwargs):
        assert kwargs['revalidate_existing_only'] is True
        before_lock.set()
        assert released.wait(timeout=30)
        return reserve(*args, **kwargs)
    monkeypatch.setattr(claims, '_reserve_alpaca_entry_risk', paused_reserve)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claims.mark_entry_transport_started_committed,
                             **key,ordinary_account_snapshot=_snapshot())
        try:
            assert before_lock.wait(timeout=30)
            assert claims.release_entry_claim_pre_post_committed(**key,reason='test-racing-release')
        finally:
            released.set()
        assert future.result(timeout=60) is False
    claim = _read(key)
    assert claim['phase'] == 'resolved'
    assert 'entry_transport_started' not in claim['metadata']
