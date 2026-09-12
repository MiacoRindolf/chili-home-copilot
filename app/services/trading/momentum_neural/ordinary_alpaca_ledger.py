"""Risk bounds for already-certified ordinary PAPER long positions/instructions.

The caller certifies account generation, owner, direction and frozen requests
under the account lock. This module supplies arithmetic, never authentication.
A live pending instruction retains its FULL reservation until terminal. An
identified partial position covered by that instruction is not charged twice.
"""
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math


def _number(value, *, zero=False):
    if isinstance(value, bool) or value is None:
        raise ValueError('ordinary_ledger_number_unavailable')
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('ordinary_ledger_number_invalid') from None
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not zero):
        raise ValueError('ordinary_ledger_number_invalid')
    return Fraction(parsed)


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('ordinary_ledger_total_overflow')
    return result


def ordinary_risk_totals(*, positions, pending, candidate_symbol, exact=False):
    """Exact sums over supplied decimals; no count/clock rule or risk-rate fit.

    Missing stop on a certified long charges its full entry notional. A pending
    order can cover its own partial position only with exact owner/CID lineage,
    quantity <= frozen instruction, basis <= buy limit and reserved risk >=
    held structural risk. Otherwise evidence must be reconciled before entry.
    """
    held = {}
    for item in positions:
        owner = item['owner_id']
        if type(owner) is not int or owner <= 0 or owner in held:
            raise ValueError('ordinary_position_owner_invalid')
        qty, entry = _number(item['quantity']), _number(item['entry_price'])
        stop = None if item['stop_price'] is None else _number(item['stop_price'])
        risk = qty * (entry if stop is None else max(entry-stop, 0))
        held[owner] = {**item, 'qty':qty, 'entry':entry, 'risk':risk,
                       'gross':qty*entry, 'stop_unknown':stop is None}

    covered = set()
    seen = set()
    pending_risk = pending_symbol_risk = pending_gross = Fraction(0)
    for item in pending:
        owner, cid = item['owner_id'], item['client_order_id']
        if type(owner) is not int or owner <= 0 or not isinstance(cid, str) or not cid:
            raise ValueError('ordinary_pending_identity_invalid')
        if owner in seen:
            raise ValueError('ordinary_pending_owner_not_unique')
        seen.add(owner)
        qty = _number(item['quantity'])
        limit = _number(item['limit_price'])
        risk = _number(item['reserved_risk_usd'])
        position = held.get(owner)
        if position is not None:
            if (position['symbol'] != item['symbol'] or position['client_order_id'] != cid
                    or position['qty'] > qty or position['entry'] > limit
                    or position['risk'] > risk):
                raise ValueError('ordinary_partial_position_not_covered')
            covered.add(owner)
        pending_risk += risk
        pending_gross += qty * limit
        if item['symbol'] == candidate_symbol:
            pending_symbol_risk += risk

    open_risk = open_symbol_risk = open_gross = Fraction(0)
    for owner, position in held.items():
        if owner in covered:
            continue
        open_risk += position['risk']
        open_gross += position['gross']
        if position['symbol'] == candidate_symbol:
            open_symbol_risk += position['risk']
    serialize = _float
    if exact:
        from app.crypto_execution.lifecycle import decimal_text
        serialize = decimal_text
    return {
        'account_open_risk_usd':serialize(open_risk),
        'active_claim_risk_usd':serialize(pending_risk),
        'symbol_open_risk_usd':serialize(open_symbol_risk),
        'symbol_active_claim_risk_usd':serialize(pending_symbol_risk),
        'open_entry_notional_usd':serialize(open_gross),
        'pending_entry_notional_upper_bound_usd':serialize(pending_gross),
        'covered_partial_position_owner_ids':sorted(covered),
        'unstopped_position_owner_ids':sorted(owner for owner,p in held.items() if p['stop_unknown']),
        'held_position_count':len(held), 'pending_instruction_count':len(seen),
        'risk_contract':'ordinary_owned_long_full_pending_reservation_v1',
        'pending_policy':'retain_full_instruction_until_terminal',
    }
