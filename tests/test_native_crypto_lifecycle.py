"""Recovery and full-sale behavior from exact fractional broker evidence."""
from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction
from uuid import uuid4

import pytest

from app.crypto_execution.lifecycle import decimal_text, exposure, initial_cycle, next_action, transition

ACCOUNT = '20000000-0000-0000-0000-000000000001'
CYCLE = '30000000-0000-0000-0000-000000000001'
ASSET = dict(id='276e2673-764b-4ab6-a611-caf665ca6340', symbol='BTC/USD',
             status='active', tradable=True, min_order_size='0.000001',
             min_trade_increment='0.000000001', price_increment='0.1', **{'class': 'crypto'})
INSTRUCTION = dict(symbol='BTC/USD', side='buy', type='limit', time_in_force='ioc',
                   qty='0.000100001', limit_price='60000.1', client_order_id='owned-entry')


def fresh():
    return initial_cycle(cycle_id=CYCLE, account_id=ACCOUNT, asset=ASSET,
                         instruction=INSTRUCTION, context_sha256='a'*64)


def advance(state, kind, **payload):
    if kind == 'position_observed':
        read_id = str(uuid4())
        state = advance(state, 'position_read_started', read_id=read_id)
        payload['read_id'] = read_id
        payload['read_revision'] = state['revision']
    return transition(state, dict(kind=kind, payload=payload))


def order(**changes):
    row = dict(id='10000000-0000-0000-0000-000000000001', client_order_id='owned-entry',
               asset_id=ASSET['id'], symbol='BTC/USD', asset_class='crypto', side='buy',
               status='canceled', type='limit', time_in_force='ioc', qty=INSTRUCTION['qty'],
               filled_qty='0.00004', filled_avg_price='60000', limit_price='60000.1')
    row.update(changes)
    return row


def position(qty='0.0000399', available='0.0000399'):
    return dict(asset_id=ASSET['id'], symbol='BTCUSD', asset_class='crypto', side='long',
                qty=qty, qty_available=available, avg_entry_price='60000')


def held():
    s = advance(fresh(), 'entry_transport_started')
    s = advance(s, 'entry_observed', **order())
    return advance(s, 'position_observed', found=True, position=position())


def exiting():
    s = advance(held(), 'exit_requested', context_sha256='b'*64)
    return advance(s, 'exit_transport_started', position_revision=s['position_revision'],
                   qty=s['position']['qty'], limit_price='60010.1', client_order_id='exit-1')


def test_unknown_entry_transport_restores_reconciliation_without_resubmit_or_release():
    s = advance(fresh(), 'entry_transport_started')
    original = exposure(s)
    s = advance(s, 'transport_observation_unknown', reason='connection_interrupted')
    assert next_action(deepcopy(s)) == 'reconcile_entry_by_client_id'
    assert exposure(s) == original
    with pytest.raises(ValueError, match='not_submittable'):
        advance(s, 'entry_transport_started')
    with pytest.raises(ValueError, match='may_have_started'):
        advance(s, 'cancel_unsubmitted_entry')


def test_canceled_partial_entry_keeps_full_notional_risk_and_actual_fee_adjusted_balance():
    s = held()
    assert next_action(s) == 'manage_held_from_ticks'
    assert exposure(s)['debit'] == '6.0000700001'
    assert exposure(s)['risk'] == '6.0000700001'
    assert s['entry']['filled_qty'] == '0.00004'
    assert s['position']['qty'] == '0.0000399'
    assert not s['fees_complete']


def test_partial_exit_targets_entire_residual_after_terminal_and_fresh_position():
    s = exiting()
    assert s['exit_requests'][0]['qty'] == '0.0000399'
    assert next_action(s) == 'reconcile_exit_by_client_id'
    s = advance(s, 'exit_observed', **order(id='10000000-0000-0000-0000-000000000002',
                client_order_id='exit-1', side='sell', qty='0.0000399',
                filled_qty='0.00002', limit_price='60010.1'))
    assert next_action(s) == 'read_position'
    assert exposure(s)['debit'] == '6.0000700001'
    s = advance(s, 'position_observed', found=True, position=position('0.0000199', '0.0000199'))
    assert next_action(s) == 'submit_full_exit'
    s = advance(s, 'exit_transport_started', position_revision=s['position_revision'],
                qty='0.0000199', limit_price='60008.1', client_order_id='exit-2')
    s = advance(s, 'exit_observed', **order(id='10000000-0000-0000-0000-000000000003',
                client_order_id='exit-2', side='sell', qty='0.0000199',
                status='filled', filled_qty='0.0000199', limit_price='60008.1'))
    assert next_action(s) == 'read_position'  # Filled order alone is not flatness.
    s = advance(s, 'position_observed', found=False)
    assert next_action(s) == 'closed'
    assert exposure(s) == dict(debit='0', risk='0', closed=True)
    assert not s['fees_complete']  # Flat exposure is not settled P&L.


@pytest.mark.parametrize('available', [None, '0', '0.00001'])
def test_unavailable_balance_does_not_authorize_partial_liquidation(available):
    s = advance(held(), 'exit_requested', context_sha256='b'*64)
    s = advance(s, 'position_observed', found=True, position=position(available=available))
    assert next_action(s) == 'reconcile_reserved_balance'
    with pytest.raises(ValueError, match='not_submittable'):
        advance(s, 'exit_transport_started', position_revision=s['position_revision'],
                qty='0.00001', limit_price='60010.1', client_order_id='exit-1')


@pytest.mark.parametrize('change', [dict(qty='0.00001'), dict(position_revision=0),
                                  dict(limit_price='60010.11'), dict(client_order_id='owned-entry')])
def test_exit_intent_must_bind_exact_full_balance_position_revision_and_grid(change):
    s = advance(held(), 'exit_requested', context_sha256='b'*64)
    before = deepcopy(s)
    request = dict(qty='0.0000399', position_revision=s['position_revision'],
                   limit_price='60010.1', client_order_id='exit-1')
    request.update(change)
    with pytest.raises(ValueError):
        advance(s, 'exit_transport_started', **request)
    assert s == before


def test_partial_live_entry_must_cancel_and_reconcile_before_selling():
    s = advance(fresh(), 'entry_transport_started')
    s = advance(s, 'entry_observed', **order(status='partially_filled'))
    s = advance(s, 'exit_requested', context_sha256='b'*64)
    assert next_action(s) == 'cancel_then_reconcile_entry'
    with pytest.raises(ValueError, match='entry_still_unresolved'):
        advance(s, 'position_observed', found=True, position=position())


def test_zero_fill_terminal_needs_native_flat_read_before_releasing_claim():
    s = advance(fresh(), 'entry_transport_started')
    s = advance(s, 'entry_observed', **order(filled_qty='0', filled_avg_price=None))
    assert not exposure(s)['closed']
    assert next_action(s) == 'read_position'
    s = advance(s, 'position_observed', found=False)
    assert exposure(s)['closed']


def test_positive_entry_with_unexplained_flatness_keeps_claim():
    s = advance(held(), 'position_observed', found=False)
    assert next_action(s) == 'reconcile_unexplained_flat_position'
    assert not exposure(s)['closed']


@pytest.mark.parametrize('change', [dict(filled_qty='0.00003'), dict(status='new'),
                                   dict(client_order_id='alien'), dict(replaced_by=CYCLE)])
def test_entry_regression_or_lineage_change_preserves_prior_state(change):
    s = held()
    with pytest.raises(ValueError):
        advance(s, 'entry_observed', **order(**change))
    assert s == held()


def test_unresolved_exit_cannot_disappear_on_a_position_read():
    s = exiting()
    with pytest.raises(ValueError, match='exit_still_unresolved'):
        advance(s, 'position_observed', found=False)
    assert not s['closed']


def test_exact_notional_and_decimal_rendering_do_not_depend_on_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 2
        assert fresh()['original_debit'] == '6.0000700001'
        for value in ('0', '-0.00125', '1.12345678901234567890123456789', '10000'):
            assert Decimal(decimal_text(Fraction(Decimal(value)))) == Decimal(value)
    with pytest.raises(ValueError, match='non_decimal_amount'):
        decimal_text(Fraction(1, 3))


@pytest.mark.parametrize('change', [dict(qty='0.000000001'), dict(qty='0.0001000011'),
                                   dict(limit_price='60000.11'), dict(time_in_force='day'),
                                   dict(qty=0.0001), dict(side='sell')])
def test_native_entry_constraints_precede_any_intent(change):
    instruction = dict(INSTRUCTION, **change)
    with pytest.raises(ValueError):
        initial_cycle(cycle_id=CYCLE, account_id=ACCOUNT, asset=ASSET,
                      instruction=instruction, context_sha256='a'*64)


def test_old_position_response_cannot_cross_a_new_order_observation():
    s = held()
    read_id = str(uuid4())
    s = advance(s, 'position_read_started', read_id=read_id)
    read_revision = s['revision']
    s = advance(s, 'entry_observed', **order())
    with pytest.raises(ValueError, match='not_bound_to_order_frontier'):
        transition(s, dict(kind='position_observed', payload=dict(
            read_id=read_id, read_revision=read_revision, found=True, position=position())))
    assert next_action(s) == 'read_position'


def test_position_observation_requires_its_own_unconsumed_read_intent():
    s = held()
    with pytest.raises(ValueError, match='not_bound_to_order_frontier'):
        transition(s, dict(kind='position_observed', payload=dict(
            read_id=str(uuid4()), found=False)))


def test_decimal_instruction_freezes_to_json_safe_exact_text():
    s = initial_cycle(cycle_id=CYCLE, account_id=ACCOUNT, asset=ASSET,
        instruction=dict(INSTRUCTION, qty=Decimal(INSTRUCTION['qty'])), context_sha256='a'*64)
    assert s['instruction']['qty'] == INSTRUCTION['qty']


def test_historical_v1_entry_event_replays_without_new_transport_fields():
    s=fresh();s['contract']='native_crypto_long_cycle_v1'
    s=advance(s,'entry_transport_started')
    assert 'entry_intent_revision' not in s


def test_unknown_http_outcome_cannot_be_reclassified_as_proven_unsent():
    s=advance(fresh(),'entry_transport_started')
    s=advance(s,'broker_evidence',phase='transport_unknown',method='POST',path='/v2/orders',request_id=CYCLE)
    with pytest.raises(ValueError,match='no_unsent_transport_evidence'):
        advance(s,'transport_proven_unsent',side='buy',intent_revision=1,request_id=CYCLE)
