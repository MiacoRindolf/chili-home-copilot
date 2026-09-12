"""Independent arithmetic/lineage examples for ordinary multi-position bounds."""
import pytest

from app.services.trading.momentum_neural.ordinary_alpaca_ledger import ordinary_risk_totals


def position(**changes):
    item = dict(owner_id=1, symbol='AAA', client_order_id='entry-a',
                quantity=4, entry_price=10, stop_price=9)
    item.update(changes)
    return item


def pending(**changes):
    item = dict(owner_id=1, symbol='AAA', client_order_id='entry-a',
                quantity=10, limit_price=10, reserved_risk_usd=10)
    item.update(changes)
    return item


def test_partial_fill_consumes_one_full_instruction_reservation():
    totals = ordinary_risk_totals(positions=[position()], pending=[pending()], candidate_symbol='BBB')
    assert totals['account_open_risk_usd'] == 0
    assert totals['active_claim_risk_usd'] == 10
    assert totals['pending_entry_notional_upper_bound_usd'] == 100
    assert totals['covered_partial_position_owner_ids'] == [1]


def test_completed_instruction_leaves_actual_held_risk():
    totals = ordinary_risk_totals(positions=[position()], pending=[], candidate_symbol='AAA')
    assert totals['account_open_risk_usd'] == 4
    assert totals['symbol_open_risk_usd'] == 4
    assert totals['active_claim_risk_usd'] == 0


def test_distinct_held_and_pending_symbols_sum_without_rank_or_count():
    totals = ordinary_risk_totals(positions=[position()],
        pending=[pending(owner_id=2, symbol='BBB', client_order_id='entry-b', reserved_risk_usd=7)],
        candidate_symbol='CCC')
    assert totals['account_open_risk_usd'] + totals['active_claim_risk_usd'] == 11
    assert totals['symbol_open_risk_usd'] + totals['symbol_active_claim_risk_usd'] == 0


@pytest.mark.parametrize('changes', [
    {'client_order_id':'another-entry'}, {'symbol':'BBB'}, {'quantity':3},
    {'limit_price':9}, {'reserved_risk_usd':3},
])
def test_inconsistent_partial_fill_cannot_release_reserved_risk(changes):
    with pytest.raises(ValueError, match='ordinary_partial_position_not_covered'):
        ordinary_risk_totals(positions=[position()],pending=[pending(**changes)],candidate_symbol='BBB')


def test_unknown_stop_charges_full_long_notional():
    p = position()
    p['stop_price'] = None
    totals = ordinary_risk_totals(positions=[p],pending=[],candidate_symbol='BBB')
    assert totals['account_open_risk_usd'] == 40
    assert totals['unstopped_position_owner_ids'] == [1]
    with pytest.raises(ValueError,match='ordinary_partial_position_not_covered'):
        ordinary_risk_totals(positions=[p],pending=[pending()],candidate_symbol='BBB')


@pytest.mark.parametrize('bad', [None, True, 0, -1, 'NaN', 'Infinity'])
def test_unknown_or_invalid_quantity_cannot_be_counted_as_zero(bad):
    p = position()
    p['quantity'] = bad
    with pytest.raises(ValueError):
        ordinary_risk_totals(positions=[p],pending=[],candidate_symbol='BBB')


def test_positive_stop_above_basis_does_not_create_negative_risk_credit():
    p = position()
    p['stop_price'] = 11
    totals = ordinary_risk_totals(positions=[p],pending=[pending(owner_id=2)],candidate_symbol='BBB')
    assert totals['account_open_risk_usd'] == 0
    assert totals['active_claim_risk_usd'] == 10


def test_duplicate_owner_records_are_ambiguous_not_additional_cash():
    with pytest.raises(ValueError,match='ordinary_position_owner_invalid'):
        ordinary_risk_totals(positions=[position(),position()],pending=[],candidate_symbol='BBB')
    with pytest.raises(ValueError,match='ordinary_pending_owner_not_unique'):
        ordinary_risk_totals(positions=[],pending=[pending(),pending()],candidate_symbol='BBB')


def test_decimal_risk_does_not_accumulate_binary_roundoff():
    positions = [dict(owner_id=i, symbol=str(i), client_order_id=str(i),
                      quantity='3', entry_price='0.3', stop_price='0.2') for i in range(1,11)]
    assert ordinary_risk_totals(positions=positions,pending=[],candidate_symbol='X')['account_open_risk_usd'] == 3
