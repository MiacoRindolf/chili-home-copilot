from fractions import Fraction as F
import pytest
from app.crypto_execution.opportunity_math import credited_asset_roundtrip

def evaluate(**changes):
    values=dict(entry_ask='100',exit_bid='101',quantity='2',entry_fee_rate='0.0025',
        exit_fee_rate='0.0025',exit_price_increment='0.000000001',fee_evidence_sha256='a'*64)
    return credited_asset_roundtrip(**dict(values,**changes))

def test_fee_currency_changes_both_sellable_quantity_and_quote_credit():
    r=evaluate()
    assert r['entry_quote_debit']==200 and r['entry_base_fee']==F('.005')
    assert r['credited_base_quantity']==F('1.995')
    assert r['exit_quote_credit']==F('1.995')*101*F('.9975')
    assert r['net_quote_pnl']==r['exit_quote_credit']-200
    assert r['actual_fees_settled'] is False and r['fillability_verified'] is False

def test_positive_print_wave_can_have_negative_net_reward_after_costs():
    r=evaluate(entry_ask='77222.8',exit_bid='77231.2',quantity='0.001')
    assert r['gross_price_return']>0 and r['net_return']<0
    assert not r['profitable_at_supplied_prices']

def test_profitable_tick_boundary_is_derived_and_strict_even_at_exact_equality():
    r=evaluate(entry_fee_rate='0',exit_fee_rate='0',exit_price_increment='.01',exit_bid='100')
    assert r['break_even_exit_bid']==100 and r['minimum_profitable_exit_bid']==F('100.01')
    assert r['net_quote_pnl']==0 and not r['profitable_at_supplied_prices']
    r=evaluate()
    lower=r['minimum_profitable_exit_bid']-r['exit_price_increment']
    factor=F('.9975')**2
    assert lower*factor<=100<r['minimum_profitable_exit_bid']*factor

def test_proportional_quantity_changes_pnl_but_does_not_create_edge():
    a=evaluate(quantity='0.000000001');b=evaluate(quantity='3.123456789')
    assert a['net_return']==b['net_return']
    assert a['break_even_exit_bid']==b['break_even_exit_bid']
    assert b['net_quote_pnl']/a['net_quote_pnl']==F('3.123456789')/F('0.000000001')

@pytest.mark.parametrize('changes',[dict(entry_fee_rate=None),dict(entry_fee_rate=0.0025),
    dict(exit_fee_rate='1'),dict(entry_fee_rate='-0.1'),dict(fee_evidence_sha256=''),
    dict(exit_price_increment='0')])
def test_missing_or_invalid_cost_evidence_is_not_a_zero_fee_fallback(changes):
    with pytest.raises(ValueError):evaluate(**changes)
