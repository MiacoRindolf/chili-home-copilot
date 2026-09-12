"""Exact cost geometry for crypto fees charged in the credited asset.

This evaluates a supplied price/fee scenario, not the probability of reaching
an exit price, order fillability, actual charged fees, or settled account P&L.
Fee inputs must come with a caller-retained evidence identity. There is no
implicit zero-fee fallback and no embedded fee tier or strategy threshold.
"""
from fractions import Fraction
import re
from .truth import decimal

def credited_asset_roundtrip(*,entry_ask,exit_bid,quantity,entry_fee_rate,exit_fee_rate,
                            exit_price_increment,fee_evidence_sha256):
    if type(fee_evidence_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',fee_evidence_sha256):
        raise ValueError('crypto_opportunity_fee_evidence_required')
    ask,bid,qty,step=(Fraction(decimal(v)) for v in (entry_ask,exit_bid,quantity,exit_price_increment))
    buy_fee,sell_fee=(Fraction(decimal(v,zero=True)) for v in (entry_fee_rate,exit_fee_rate))
    if max(buy_fee,sell_fee)>=1:raise ValueError('crypto_opportunity_fee_rate_invalid')
    credited_base=qty*(1-buy_fee)
    debit=qty*ask;gross=credited_base*bid;credit=gross*(1-sell_fee)
    factor=(1-buy_fee)*(1-sell_fee)
    break_even=ask/factor
    minimum_profitable_bid=(break_even//step+1)*step
    return dict(contract='crypto_credited_asset_roundtrip_scenario_v1',
        entry_ask=ask,exit_bid=bid,ordered_base_quantity=qty,entry_quote_debit=debit,
        entry_base_fee=qty*buy_fee,credited_base_quantity=credited_base,
        exit_quote_gross=gross,exit_quote_fee=gross*sell_fee,exit_quote_credit=credit,
        net_quote_pnl=credit-debit,net_return=(credit-debit)/debit,
        gross_price_return=bid/ask-1,break_even_exit_bid=break_even,
        minimum_profitable_exit_bid=minimum_profitable_bid,exit_price_increment=step,
        profitable_at_supplied_prices=credit>debit,fee_evidence_sha256=fee_evidence_sha256,
        fee_basis='caller_supplied_credited_asset_rates',
        fillability_verified=False,actual_fees_settled=False,forecast_probability=None,
        base_quantity_quantization_applied=False)
