"""Fractional executions and delayed fees must remain separate evidence."""
from copy import deepcopy
from decimal import localcontext

import pytest

from scripts.probe_crypto_paper_execution import fill_evidence, owned_position


BUY = {'id': 'buy', 'symbol': 'BTC/USD', 'side': 'buy', 'filled_qty': '0.0001'}
SELL = {'id': 'sell', 'symbol': 'BTC/USD', 'side': 'sell', 'filled_qty': '0.00009975'}


def fills():
    return [
        {'id': 'a', 'activity_type': 'FILL', 'order_id': 'buy', 'symbol': 'BTC/USD',
         'side': 'buy', 'type': 'partial_fill', 'qty': '0.00004', 'price': '60000', 'cum_qty': '0.00004'},
        {'id': 'b', 'activity_type': 'FILL', 'order_id': 'buy', 'symbol': 'BTC/USD',
         'side': 'buy', 'type': 'fill', 'qty': '0.00006', 'price': '61000', 'cum_qty': '0.0001'},
        {'id': 'c', 'activity_type': 'FILL', 'order_id': 'sell', 'symbol': 'BTC/USD',
         'side': 'sell', 'type': 'fill', 'qty': '0.00009975', 'price': '62000'},
    ]


def test_incremental_fills_and_duplicate_delivery_do_not_double_count():
    rows = fills()
    out = fill_evidence([rows[2], rows[0], rows[1], deepcopy(rows[0])], BUY, SELL)
    assert out['fills_reconciled']
    assert out['fill_quantities'] == {'buy': '0.00010', 'sell': '0.00009975'}
    assert out['observed_fill_cashflow_quote'] == '0.12450000'
    assert out['net_realized_pnl'] is None and out['fees_complete'] is False


def test_incomplete_activity_visibility_cannot_report_roundtrip_cashflow():
    out = fill_evidence(fills()[:2], BUY, SELL)
    assert out['matches_reported_order_quantities'] == {'buy': True, 'sell': False}
    assert out['observed_fill_cashflow_quote'] is None


def test_delayed_fee_does_not_get_assigned_to_this_order_or_counted_twice():
    fee = {'id': 'fee', 'activity_type': 'CFEE', 'qty': '-0.00000025',
           'symbol': 'BTCUSD', 'net_amount': '0', 'price': '60000'}
    other = {**fills()[0], 'id': 'other', 'order_id': 'someone-else'}
    out = fill_evidence([*fills(), fee, deepcopy(fee), other], BUY, SELL)
    assert out['unattributed_fee_rows'] == [fee]
    assert out['ignored_activity_ids'] == ['other']
    assert out['fees_complete'] is False and out['net_realized_pnl'] is None


@pytest.mark.parametrize('change', [
    {'qty': '0.001'}, {'qty': '-0.001'}, {'price': 'NaN'}, {'price': 60000.0},
    {'symbol': 'ETH/USD'}, {'side': 'sell'}, {'type': 'trade_bust'},
])
def test_incompatible_evidence_is_not_silently_normalized(change):
    rows = fills()
    rows[0].update(change)
    with pytest.raises(ValueError):
        fill_evidence(rows, BUY, SELL)


def test_conflicting_duplicate_is_not_last_write_wins():
    rows = fills()
    with pytest.raises(ValueError, match='conflicting_activity_identity'):
        fill_evidence([*rows, {**rows[0], 'price': '61000'}], BUY, SELL)


def test_external_decimal_context_cannot_round_fractional_cashflows_or_position_difference():
    with localcontext() as ctx:
        ctx.prec = 2
        out = fill_evidence(fills(), BUY, SELL)
        asset_id='00000000-0000-0000-0000-000000000001'
        position = owned_position([{'asset_id': asset_id, 'asset_class': 'crypto', 'side': 'long',
            'symbol':'BTC/USD', 'qty': '0.00009975123456789', 'qty_available': '0.00009975123456789'}], BUY, asset_id)
    assert out['observed_fill_cashflow_quote'] == '0.12450000'
    assert position['gross_minus_position'] == '2.4876543211E-7'
