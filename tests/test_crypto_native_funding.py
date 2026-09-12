"""Native cash, pending debit bounds and quote currencies cannot become equity margin."""
from decimal import Decimal, localcontext
from fractions import Fraction
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.crypto_execution.funding import crypto_account_truth, funding_residual, FundingClaim
from app.services.trading.venue import alpaca_spot as ap
from tests.test_crypto_paper_execution_probe import Broker,Journal,experiment

ACCOUNT='20000000-0000-0000-0000-000000000001'
READ='40000000-0000-0000-0000-000000000001'


def row(**changes):
    return {'id':ACCOUNT,'status':'ACTIVE','crypto_status':'ACTIVE','currency':'USD',
        'account_blocked':False,'trading_blocked':False,'trade_suspended_by_user':False,
        'non_marginable_buying_power':'10132.76','buying_power':'40531.04','cash':'10132.76',
        'accrued_fees':'0',**changes}


def account(**changes):return crypto_account_truth(row(**changes),expected_account_id=ACCOUNT)


def claim(cid,total,reflected='0',currency='USD'):
    return FundingClaim(cid,ACCOUNT,currency,Decimal(total),Decimal(reflected),READ)


def test_actual_four_times_margin_never_becomes_crypto_funding():
    a=account()
    result=funding_residual(a,quote_currency='USD',claims=())
    assert result['remaining_funding_bound']==Fraction(Decimal('10132.76'))
    assert not hasattr(a,'buying_power') and not result['unavailable_reasons']
    assert not result['atomic_reservation_performed']


def test_concurrent_claims_are_exact_and_reflected_part_is_not_subtracted_twice():
    # Observed available balance700 already reflects300 from claimA. Only the
    # remaining100 ofA and the unreflected200 ofB are subtracted from that read.
    a=account(non_marginable_buying_power='700')
    with localcontext() as ctx:
        ctx.prec=2
        result=funding_residual(a,quote_currency='USD',claims=[claim('A','400','300'),claim('B','200')],observation_id=READ)
    assert result['local_debit_upper_bound']==600
    assert result['certified_reflected_amount']==300
    assert result['unreflected_debit_upper_bound']==300
    assert result['remaining_funding_bound']==400
    assert not result['reflection_proven_by_this_function']


def test_unknown_reflection_retains_debit_bound_and_does_not_infer_from_order_status():
    r=funding_residual(account(non_marginable_buying_power='700'),quote_currency='USD',
        claims=[claim('A','400'),claim('B','200')])
    assert r['remaining_funding_bound']==100 and r['certified_reflected_amount']==0


@pytest.mark.parametrize('currency',['BTC','USDC','USDT','EUR'])
def test_non_usd_quotes_cannot_spend_usd_or_assumed_stablecoin_peg(currency):
    r=funding_residual(account(),quote_currency=currency,claims=())
    assert r['remaining_funding_bound'] is None
    assert 'quote_asset_funding_not_in_account_endpoint' in r['unavailable_reasons']


@pytest.mark.parametrize('change',[
    {'crypto_status':None},{'crypto_status':'INACTIVE'},{'status':'ACCOUNT_CLOSED'},
    {'currency':None},{'currency':'EUR'},{'account_blocked':True},{'trading_blocked':None},
    {'trade_suspended_by_user':'false'},{'account_blocked':0},{'non_marginable_buying_power':None},
])
def test_missing_or_blocked_native_readiness_is_explicit(change):
    r=funding_residual(account(**change),quote_currency='USD',claims=())
    assert r['remaining_funding_bound'] is None and r['unavailable_reasons']


@pytest.mark.parametrize('amount',['0','-1'])
def test_negative_or_zero_available_power_is_observed_but_not_borrowed(amount):
    a=account(non_marginable_buying_power=amount,cash='-20')
    assert a.non_marginable_buying_power==Decimal(amount) and a.cash==Decimal('-20')
    assert funding_residual(a,quote_currency='USD',claims=())['remaining_funding_bound']==0


@pytest.mark.parametrize('changes',[{'non_marginable_buying_power':10132.76},
    {'non_marginable_buying_power':'NaN'},{'cash':True},{'accrued_fees':'Infinity'},
    {'id':'30000000-0000-0000-0000-000000000001'}])
def test_wrong_account_or_imprecise_values_are_not_readable(changes):
    with pytest.raises(ValueError):account(**changes)


@pytest.mark.parametrize('claims',[
    [claim('A','100'),claim('A','100')],[claim('A','100','101')],
    [claim('A','-1')],[claim('A','1','-1')],[claim('A','1',currency='USDC')],
])
def test_bad_claim_identity_or_reflection_cannot_increase_budget(claims):
    with pytest.raises(ValueError):funding_residual(account(),quote_currency='USD',claims=claims)


@pytest.mark.parametrize('observation',[None,'50000000-0000-0000-0000-000000000001'])
def test_reflection_from_another_account_read_cannot_release_this_claim(observation):
    with pytest.raises(ValueError,match='reflection_observation_mismatch'):
        funding_residual(account(),quote_currency='USD',claims=[claim('A','100','100')],observation_id=observation)


def test_cross_account_claim_does_not_enter_funding_sum():
    c=FundingClaim('A','50000000-0000-0000-0000-000000000001','USD',Decimal('100'),Decimal('0'))
    with pytest.raises(ValueError,match='claim_identity_invalid'):
        funding_residual(account(),quote_currency='USD',claims=[c])


def test_fee_observation_is_not_declared_complete_or_deducted_again():
    a=account(accrued_fees='2',pending_transfer_out='10')
    assert a.accrued_fees==2 and a.pending_transfer_out==10
    r=funding_residual(a,quote_currency='USD',claims=())
    assert r['remaining_funding_bound']==Fraction(Decimal('10132.76'))
    assert not hasattr(a,'fees_complete')


def test_native_funding_reader_preserves_sdk_decimal_strings(monkeypatch):
    monkeypatch.setattr(ap,'_paper',lambda:True)
    monkeypatch.setattr(ap,'_expected_account_id',lambda:ACCOUNT)
    a=ap.AlpacaSpotAdapter(); assert a.bind_account_id(ACCOUNT)
    monkeypatch.setattr(a,'_account_client',lambda:SimpleNamespace(get_account=lambda:SimpleNamespace(**row())))
    r=a.get_crypto_account_truth()
    assert r['readable'] and r['account'].non_marginable_buying_power==Decimal('10132.76')
    assert r['read_id'] and r['received_ns']>=r['requested_ns']
    a._bound_account_id=None
    monkeypatch.setattr(a,'_account_client',lambda:pytest.fail('unbound transport'))
    assert not a.get_crypto_account_truth()['readable']


@pytest.mark.parametrize('override',[
    {'crypto_status':None},{'currency':'USDC'},{'non_marginable_buying_power':'1','buying_power':'100000'},
])
def test_mechanical_execution_does_not_reach_post_with_unfunded_or_inactive_crypto(override):
    class ChangedBroker(Broker):
        def request(self,method,path,payload=None,**kwargs):
            out=super().request(method,path,payload,**kwargs)
            if path=='/v2/account':out.update(override)
            return out
    b=ChangedBroker()
    with pytest.raises(ValueError):experiment(b,Journal(),execute=True,run_id='test')
    assert all(method=='GET' for method,_,_ in b.calls)


def test_standalone_probe_can_validate_funding_and_full_close_without_app_settings(tmp_path):
    # Real probe launch exposed an eager app.services.trading package import.
    # A fresh isolated interpreter must exercise both helpers before any POST,
    # without pytest's DATABASE_URL or the desktop checkout's .env masking it.
    root=str(Path(__file__).resolve().parents[1])
    code='''import sys
sys.path.insert(0, ROOT)
from scripts.probe_crypto_paper_execution import owned_position, crypto_account_truth, funding_residual
asset_id='00000000-0000-0000-0000-000000000001'
account={'id':asset_id,'status':'ACTIVE','crypto_status':'ACTIVE','currency':'USD',
    'non_marginable_buying_power':'10','account_blocked':False,'trading_blocked':False,'trade_suspended_by_user':False}
funding=funding_residual(crypto_account_truth(account,expected_account_id=asset_id),quote_currency='USD',claims=())
position=owned_position([{'asset_id':asset_id,'asset_class':'crypto','symbol':'BTCUSD','side':'long',
    'qty':'0.00009975','qty_available':'0.00009975'}],{'filled_qty':'0.0001'},asset_id)
assert funding['remaining_funding_bound']==10 and position['available_qty']=='0.00009975'
assert not any(name in sys.modules for name in ('app.config','app.services.trading','sqlalchemy','alpaca'))
print('standalone_funding_and_full_close_math_verified')
'''.replace('ROOT',repr(root),1)
    env={k:v for k,v in os.environ.items() if k.upper() not in ('DATABASE_URL','TEST_DATABASE_URL','CHILI_PYTEST','PYTHONPATH')
         and not k.upper().startswith('CHILI_ALPACA')}
    result=subprocess.run([sys.executable,'-I','-B','-c',code],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='standalone_funding_and_full_close_math_verified'
