"""The mechanical PAPER probe owns only its single diagnostic position."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.probe_crypto_paper_execution import (
    Client, SYMBOL, experiment, minimum_instruction, owned_position,
)

ASSET = {'id': '00000000-0000-0000-0000-000000000001', 'symbol': SYMBOL,
    'class': 'crypto', 'status': 'active', 'tradable': True,
    'min_order_size': '0.0001', 'min_trade_increment': '0.000000001', 'price_increment': '0.01'}
QUOTE = {'ap': Decimal('60000.001'), 'bp': Decimal('60000'), 't': '2026-09-12T07:30:00Z'}


def test_minimum_quantity_and_limit_derive_from_native_constraints():
    order, cap = minimum_instruction(ASSET, QUOTE)
    assert order['qty'] == '0.000100000' and order['limit_price'] == '60000.01'
    assert cap == Decimal('6.000001')
    assert order['time_in_force'] == 'ioc' and 'position_intent' not in order


def test_non_power_of_ten_increment_is_not_decimal_places_rounding():
    asset = {**ASSET, 'min_order_size': '0.000010', 'min_trade_increment': '0.000003'}
    order, _ = minimum_instruction(asset, QUOTE)
    assert order['qty'] == '0.000012'


@pytest.mark.parametrize('key,value', [('min_order_size', 'NaN'), ('min_trade_increment', '0'),
    ('price_increment', '-1'), ('class', 'us_equity'), ('symbol', 'ETH/USD'), ('tradable', False)])
def test_invalid_asset_cannot_form_entry(key, value):
    with pytest.raises(ValueError):
        minimum_instruction({**ASSET, key: value}, QUOTE)


def test_crossed_or_float_quote_does_not_create_instruction():
    for quote in ({**QUOTE, 'bp': Decimal('70000')}, {**QUOTE, 'ap': 60000.001}):
        with pytest.raises(ValueError):
            minimum_instruction(ASSET, quote)


def position(**kw):
    return {'asset_id': ASSET['id'], 'asset_class': 'crypto', 'side': 'long',
        'qty': '0.00009975', 'qty_available': '0.00009975', **kw}


def test_net_position_is_observed_without_inventing_fee_causation():
    result = owned_position([position()], {'filled_qty': '0.0001'}, ASSET['id'])
    assert result['gross_minus_position'] == '2.5E-7'
    assert result['difference_is_proven_fee'] is False


@pytest.mark.parametrize('rows', [[position(qty='0.0002')], [position(qty_available='0')],
    [position(side='short')], [position(asset_id='other')], [position(), position()], []])
def test_foreign_reserved_or_unobserved_position_cannot_be_closed(rows):
    with pytest.raises(ValueError):
        owned_position(rows, {'filled_qty': '0.0001'}, ASSET['id'])


class Journal:
    def __init__(self):
        self.events = []
    def add(self, event, **data):
        self.events.append((event, data))


class Broker:
    pin = 'paper-account'
    def __init__(self, *, fill=True):
        self.calls, self.entered, self.closed, self.fill = [], False, False, fill
    def terminal(self, order):
        return order
    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if path == '/v2/account':
            return {'id': self.pin, 'status': 'ACTIVE', 'account_blocked': False,
                'trading_blocked': False, 'trade_suspended_by_user': False,
                'non_marginable_buying_power': '100', 'cash': '100'}
        if path == '/v2/positions':
            return [position()] if self.entered and self.fill and not self.closed else []
        if path.startswith('/v2/orders?'):
            return []
        if path == '/v2/clock':
            return {'is_open': False}
        if path.startswith('/v2/assets/'):
            return deepcopy(ASSET)
        if path.startswith('/v1beta3/'):
            return {'quotes': {SYMBOL: QUOTE}}
        if method == 'POST':
            assert path == '/v2/orders'
            self.entered = True
            return {**payload, 'id': 'buy-id', 'asset_id': ASSET['id'], 'filled_qty': '0.0001' if self.fill else '0',
                'status': 'filled' if self.fill else 'canceled'}
        if method == 'DELETE':
            assert path == '/v2/positions/' + ASSET['id'] and payload is None
            self.closed = True
            return {'id': 'sell-id', 'asset_id': ASSET['id'], 'side': 'sell', 'symbol': SYMBOL,
                'status': 'filled', 'filled_qty': '0.00009975'}
        if path.startswith('/v2/account/activities?'):
            return []
        pytest.fail('Unexpected request')


def test_read_only_plan_never_submits():
    broker = Broker()
    result = experiment(broker, Journal(), execute=False, run_id='test')
    assert result['status'] == 'prepared_read_only'
    assert all(method == 'GET' for method, _, _ in broker.calls)


def test_full_close_uses_owned_native_balance_not_gross_fill_or_zero_fees():
    broker = Broker()
    result = experiment(broker, Journal(), execute=True, run_id='test')
    assert result['status'] == 'roundtrip_flat' and result['fees_complete'] is False
    assert result['strategy_enabled'] is False
    assert [method for method, _, _ in broker.calls if method != 'GET'] == ['POST', 'DELETE']
    assert result['comparison']['available_qty'] == '0.00009975'


def test_terminal_zero_fill_never_liquidates():
    broker = Broker(fill=False)
    result = experiment(broker, Journal(), execute=True, run_id='test')
    assert result['status'] == 'no_fill_flat'
    assert not any(method == 'DELETE' for method, _, _ in broker.calls)


@pytest.mark.parametrize('lease', [None, SimpleNamespace(held_by_me=lambda: False)])
def test_mutation_without_exact_owner_stops_before_any_http(lease):
    client = Client({'CHILI_ALPACA_PAPER': 'true',
        'CHILI_ALPACA_EXPECTED_ACCOUNT_ID': ASSET['id'],
        'CHILI_ALPACA_API_KEY': 'key', 'CHILI_ALPACA_API_SECRET': 'secret'}, Journal(), lease)
    with pytest.raises(ValueError, match='mutation_requires_owned_paper_lease'):
        client.request('POST', '/v2/orders', {})
