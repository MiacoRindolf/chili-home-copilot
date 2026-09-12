"""Multiple PAPER symbols clear the retired quota; financial admission remains real."""
from types import SimpleNamespace
import uuid

import pytest

from app.config import settings
from app.services.trading.momentum_neural import risk_policy as rp


@pytest.mark.parametrize('open_count', [0, 1, 2, 20])
def test_paper_candidates_do_not_require_a_rank_one_exception(monkeypatch, open_count):
    # This gate must not query historical episodes, recent win rate or rank to
    # decide whether another qualified PAPER candidate can seek admission.
    monkeypatch.setattr(rp, 'settings', SimpleNamespace(chili_alpaca_paper=True))

    def forbidden(*args, **kwargs):
        raise AssertionError('Retired episode/rank evidence must not be consulted')

    monkeypatch.setattr(rp, '_count_symbol_episodes_today', forbidden)
    monkeypatch.setattr(rp, '_top_ranked_live_eligible_symbol', forbidden)
    monkeypatch.setattr(rp, '_recent_realized_r', forbidden)
    monkeypatch.setattr(rp, 'equity_relative_loss_cap', forbidden)
    for symbol in ('FIRST', 'SECOND', 'THIRD'):
        allowed, receipt = rp.daily_trade_count_budget_decision(
            object(), execution_family='alpaca_spot', open_entry_count=open_count, symbol=symbol)
        assert allowed
        assert receipt['reason'] == 'alpaca_paper_episode_quota_retired'
        assert receipt['candidate_symbol'] == symbol
        assert receipt['financial_admission_granted'] is False
        assert receipt['rank_exemption_required'] is False


@pytest.mark.parametrize('family,paper', [
    ('alpaca_spot', False), ('alpaca_short', True),
    ('robinhood_agentic', True), ('coinbase_spot', True), (None, True),
])
def test_no_expansion_to_live_brokers_or_shorting(monkeypatch, family, paper):
    # Disable the legacy gate to observe that the other branch is still chosen,
    # without any broker/database read. Existing tests exercise its active quota.
    monkeypatch.setattr(rp, 'settings', SimpleNamespace(
        chili_alpaca_paper=paper, chili_momentum_daily_trade_count_budget_enabled=False))
    assert rp.daily_trade_count_budget_decision(
        None, execution_family=family, symbol='OTHER') == (True, {'reason': 'disabled'})


def test_two_symbols_pass_quota_but_share_the_committed_account_budget(db, monkeypatch):
    from tests.test_legacy_timeshare_sizing_escape import _seed_owner
    from app.services.trading.momentum_neural.alpaca_orphan_claims import reserve_alpaca_entry_risk_committed

    monkeypatch.setattr(settings, 'chili_alpaca_paper', True)
    monkeypatch.setattr(settings, 'chili_momentum_legacy_alpaca_dispatch_enabled', True)
    # Explicit fixture dollars exercise the real reservation boundary, not a
    # proposed strategy calibration. Budget = 1000 * .03 = 30.
    replies = []
    for symbol, risk in (('QTA', 10.0), ('QTB', 10.0), ('QTC', 15.0)):
        owner_id, request = _seed_owner(db, symbol)
        allowed, receipt = rp.daily_trade_count_budget_decision(
            db, execution_family='alpaca_spot', symbol=symbol)
        assert allowed and receipt['financial_admission_granted'] is False
        cid = 'quota-' + uuid.uuid4().hex[:12]
        replies.append(reserve_alpaca_entry_risk_committed(
            symbol=symbol, claim_token='claim-' + uuid.uuid4().hex,
            owner_session_id=owner_id, client_order_id=cid, post_bind_token='bind-' + cid,
            order_request=request(symbol, cid, qty='1'), order_role='primary',
            reserved_risk_usd=risk, account_equity_usd=1000.0, budget_fraction=.03,
            account_buying_power_usd=1000.0,
            account_scope='alpaca:paper', role_metadata={'legacy_timeshare_sizing': True},
            per_symbol_cap_usd=100.0))
    assert replies[0]['ok'], replies[0]
    assert replies[1]['ok'], replies[1]
    assert not replies[2]['ok'], replies[2]
    assert replies[2]['reason'] == 'account_risk_budget_exceeded'


def test_two_racing_symbols_cannot_spend_the_same_remaining_account_budget(db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.test_legacy_timeshare_sizing_escape import _seed_owner
    from app.services.trading.momentum_neural.alpaca_orphan_claims import reserve_alpaca_entry_risk_committed

    monkeypatch.setattr(settings, 'chili_alpaca_paper', True)
    monkeypatch.setattr(settings, 'chili_momentum_legacy_alpaca_dispatch_enabled', True)
    owners = [(symbol, *_seed_owner(db, symbol)) for symbol in ('RCA','RCB')]
    start = Barrier(2)

    def reserve(item):
        symbol, owner_id, request = item
        cid = 'race-' + uuid.uuid4().hex[:12]
        start.wait(timeout=30)
        return reserve_alpaca_entry_risk_committed(
            symbol=symbol, claim_token='claim-' + uuid.uuid4().hex,
            owner_session_id=owner_id, client_order_id=cid, post_bind_token='bind-' + cid,
            order_request=request(symbol, cid, qty='1'), order_role='primary',
            reserved_risk_usd=15, account_equity_usd=1000, budget_fraction=.02,
            account_buying_power_usd=1000, account_scope='alpaca:paper',
            role_metadata={'legacy_timeshare_sizing':True}, per_symbol_cap_usd=100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve,item) for item in owners]
        replies = [future.result(timeout=120) for future in futures]
    assert sum(bool(reply.get('ok')) for reply in replies) == 1, replies
    denied = next(reply for reply in replies if not reply.get('ok'))
    assert denied['reason'] == 'account_risk_budget_exceeded', replies
    assert denied['projected_account_risk_usd'] == 30
