"""Deterministic fractional long-cycle transitions; no network or timed decisions.

The owner durably records transport intent before calling the broker. Restoring
an in-flight intent requests reconciliation, never another blind submission.
"""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import re

from .truth import asset_identity, crypto_order_truth, crypto_position_truth, decimal, identity


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def decimal_text(value):
    """Exact finite decimal rendering independent of ambient Decimal precision."""
    value=Fraction(value); denominator=value.denominator; twos=fives=0
    while denominator%2==0:denominator//=2;twos+=1
    while denominator%5==0:denominator//=5;fives+=1
    if denominator!=1:raise ValueError('native_cycle_non_decimal_amount')
    scale=max(twos,fives)
    numerator=value.numerator*(2**(scale-twos))*(5**(scale-fives))
    sign='-' if numerator<0 else '';digits=str(abs(numerator))
    if not scale:return sign+digits
    digits=digits.zfill(scale+1)
    return sign+digits[:-scale]+'.'+digits[-scale:]


def initial_cycle(*,cycle_id,account_id,asset,instruction,context_sha256):
    aid,symbol=asset_identity(asset)
    if type(context_sha256) is not str or re.fullmatch('[0-9a-f]{64}',context_sha256) is None:
        raise ValueError('native_cycle_context_identity_required')
    qty=decimal(instruction.get('qty'));limit=decimal(instruction.get('limit_price'))
    minimum=decimal(asset.get('min_order_size'));step=decimal(asset.get('min_trade_increment'))
    price_step=decimal(asset.get('price_increment'))
    if (asset.get('status')!='active' or asset.get('tradable') is not True or
            instruction.get('symbol')!=symbol or instruction.get('side')!='buy' or
            instruction.get('type')!='limit' or instruction.get('time_in_force') not in ('gtc','ioc') or
            set(instruction)-{'symbol','side','type','time_in_force','qty','limit_price','client_order_id'} or
            type(instruction.get('client_order_id')) is not str or not instruction['client_order_id'] or
            qty<minimum or (Fraction(qty)/Fraction(step)).denominator!=1 or
            (Fraction(limit)/Fraction(price_step)).denominator!=1):
        raise ValueError('native_cycle_instruction_not_native_limit_buy')
    frozen_asset=dict(id=aid,symbol=symbol,price_increment=format(price_step,'f'),
        min_trade_increment=format(step,'f'),min_order_size=format(minimum,'f'),**{'class':'crypto'})
    frozen_instruction=dict(instruction,qty=format(qty,'f'),limit_price=format(limit,'f'))
    return dict(contract='native_crypto_long_cycle_v2',cycle_id=identity(cycle_id),account_id=identity(account_id),
        asset=frozen_asset,instruction=frozen_instruction,context_sha256=context_sha256,
        quote_currency=symbol.split('/')[1],entry_started=False,entry=None,entry_revision=None,
        exit_requested=False,exit_requests=[],exits=[],position=None,position_known=False,
        position_revision=None,position_query=None,revision=0,closed=False,closed_reason=None,
        original_debit=decimal_text(Fraction(qty)*Fraction(limit)),fees_complete=False)


def _decoded_entry(state,row):
    expected=state['entry']['id'] if state['entry'] else row.get('id')
    order=crypto_order_truth(row,asset=state['asset'],expected_order_id=expected)
    frozen=state['instruction']
    if (order.client_order_id!=frozen['client_order_id'] or order.side!='buy' or
            order.quantity!=decimal(frozen['qty']) or order.limit_price!=decimal(frozen['limit_price']) or
            order.order_type!='limit' or order.time_in_force!=frozen['time_in_force'] or
            order.replaces is not None or order.replaced_by is not None):
        raise ValueError('native_cycle_entry_identity_changed')
    return order


def _entry(state):return _decoded_entry(state,state['entry']) if state['entry'] else None


def _latest_exit(state):
    if not state['exits'] or state['exits'][-1] is None:return None
    row=state['exits'][-1]
    return crypto_order_truth(row,asset=state['asset'],expected_order_id=row['id'])


def next_action(state):
    if state['closed']:return 'closed'
    if not state['entry_started']:
        return 'cancel_unsubmitted_entry' if state['exit_requested'] else 'submit_entry'
    entry=_entry(state)
    if entry is None:return 'reconcile_entry_by_client_id'
    if not entry.terminal:
        return 'cancel_then_reconcile_entry' if state['exit_requested'] else 'reconcile_entry'
    exit=_latest_exit(state)
    exit_unsent=bool(state['exit_requests'] and state['exit_requests'][-1].get('proven_unsent'))
    if state['exit_requests'] and not exit_unsent and (exit is None or not exit.terminal):return 'reconcile_exit_by_client_id'
    frontier=max(state['entry_revision'] or 0,state.get('exit_revision') or 0)
    if not state['position_known'] or state['position_revision']<=frontier:return 'read_position'
    if state['position'] is None:return 'reconcile_unexplained_flat_position'
    if entry.filled_quantity==0:return 'reconcile_unexplained_position'
    if not state['exit_requested']:return 'manage_held_from_ticks'
    position=crypto_position_truth(state['position'],asset=state['asset'])
    return 'submit_full_exit' if position.whole_balance_available else 'reconcile_reserved_balance'


def transition(prior,event):
    state=deepcopy(prior);kind=event.get('kind');payload=event.get('payload',{})
    if state['closed'] and kind!='broker_evidence':raise ValueError('native_cycle_already_closed')
    if type(payload) is not dict:raise ValueError('native_cycle_event_payload_invalid')
    revision=state['revision']+1
    if kind=='broker_evidence':
        # Raw request/response evidence lives in the append-only event, not in
        # the growing state projection. Evidence alone cannot release exposure.
        state['last_transport_evidence']={k:payload.get(k) for k in ('phase','request_id','method','path')}
    elif kind=='transport_proven_unsent':
        proof=state.get('last_transport_evidence',{})
        if (proof.get('phase')!='not_transported' or proof.get('method')!='POST' or
                proof.get('path')!='/v2/orders' or proof.get('request_id')!=payload.get('request_id')):
            raise ValueError('native_cycle_no_unsent_transport_evidence')
        if payload.get('side')=='buy':
            if (not state['entry_started'] or state['entry'] is not None or
                    payload.get('intent_revision')!=state.get('entry_intent_revision')):
                raise ValueError('native_cycle_unsent_entry_identity_changed')
            state.update(closed=True,closed_reason='entry_transport_proven_unsent')
        elif payload.get('side')=='sell':
            if (not state['exit_requests'] or state['exits'][-1] is not None or
                    payload.get('intent_revision')!=state['exit_requests'][-1].get('intent_revision')):
                raise ValueError('native_cycle_unsent_exit_identity_changed')
            state['exit_requests'][-1]['proven_unsent']=True
        else:raise ValueError('native_cycle_unsent_transport_side_invalid')
    elif kind=='entry_cancel_transport_started':
        entry=_entry(state)
        if (entry is None or entry.terminal or not state['exit_requested'] or
                identity(payload.get('order_id'))!=entry.order_id):
            raise ValueError('native_cycle_entry_cancel_not_bound')
        state['last_cancel_request']=deepcopy(payload)
    elif kind=='entry_transport_started':
        if next_action(state)!='submit_entry':raise ValueError('native_cycle_entry_not_submittable')
        state['entry_started']=True
        if state['contract']=='native_crypto_long_cycle_v2':state['entry_intent_revision']=revision
    elif kind=='transport_observation_unknown':
        if not state['entry_started']:raise ValueError('native_cycle_no_transport_to_reconcile')
        # Record uncertainty without releasing capital or making an intent
        # submit-able again. Exact error/raw response evidence belongs upstream.
        state['last_unknown_transport']=deepcopy(payload)
    elif kind=='cancel_unsubmitted_entry':
        if state['entry_started']:raise ValueError('native_cycle_transport_may_have_started')
        state.update(closed=True,closed_reason='proven_no_transport')
    elif kind=='entry_observed':
        if not state['entry_started']:raise ValueError('native_cycle_entry_not_started')
        new=_decoded_entry(state,payload);old=_entry(state)
        if old and (new.filled_quantity<old.filled_quantity or old.terminal and
                (new.status!=old.status or new.filled_quantity!=old.filled_quantity)):
            raise ValueError('native_cycle_entry_evidence_regressed')
        state['entry']=deepcopy(payload);state['entry_revision']=revision
    elif kind=='exit_requested':
        if type(payload.get('context_sha256')) is not str or re.fullmatch('[0-9a-f]{64}',payload['context_sha256']) is None:
            raise ValueError('native_cycle_exit_context_required')
        state['exit_requested']=True;state['exit_context_sha256']=payload['context_sha256']
    elif kind in ('position_read_started','position_observed'):
        entry=_entry(state)
        if entry is None or not entry.terminal:raise ValueError('native_cycle_entry_still_unresolved')
        exit=_latest_exit(state)
        exit_unsent=bool(state['exit_requests'] and state['exit_requests'][-1].get('proven_unsent'))
        if state['exit_requests'] and not exit_unsent and (exit is None or not exit.terminal):
            raise ValueError('native_cycle_exit_still_unresolved')
        frontier=max(state['entry_revision'] or 0,state.get('exit_revision') or 0)
        if kind=='position_read_started':
            read_id=identity(payload.get('read_id'))
            if state['position_query'] and read_id==state['position_query']['read_id']:
                raise ValueError('native_cycle_position_read_identity_reused')
            state.update(position_query=dict(read_id=read_id,after_revision=frontier,read_revision=revision),position_known=False,revision=revision)
            return state
        query=state['position_query']
        if (query is None or identity(payload.get('read_id'))!=query['read_id'] or query['after_revision']!=frontier or
                type(payload.get('read_revision')) is not int or payload['read_revision']!=query['read_revision']):
            raise ValueError('native_cycle_position_read_not_bound_to_order_frontier')
        if type(payload.get('found')) is not bool:raise ValueError('native_cycle_position_presence_unknown')
        position=payload.get('position')
        if payload['found']:
            observed=crypto_position_truth(position,asset=state['asset'])
            sold=sum((Fraction(decimal(x['filled_qty'],zero=True)) for x in state['exits'] if x),Fraction(0))
            remaining=Fraction(entry.filled_quantity)-sold
            if observed.quantity>remaining:raise ValueError('native_cycle_position_exceeds_owned_gross')
        elif position is not None:raise ValueError('native_cycle_absence_has_position')
        state.update(position=deepcopy(position),position_known=True,position_revision=revision,position_query=None)
        flat=not payload['found'] or observed.quantity==0
        if flat and (entry.filled_quantity==0 or any(decimal(x['filled_qty'],zero=True)>0 for x in state['exits'] if x)):
            state.update(closed=True,closed_reason='terminal_orders_and_native_position_flat')
    elif kind=='exit_transport_started':
        if next_action(state)!='submit_full_exit':raise ValueError('native_cycle_full_exit_not_submittable')
        position=crypto_position_truth(state['position'],asset=state['asset'])
        if (payload.get('position_revision')!=state['position_revision'] or
                decimal(payload.get('qty'))!=position.quantity or
                type(payload.get('client_order_id')) is not str or not payload['client_order_id'] or
                payload['client_order_id']==state['instruction']['client_order_id'] or
                any(x['client_order_id']==payload['client_order_id'] for x in state['exit_requests'])):
            raise ValueError('native_cycle_exit_does_not_bind_whole_available_balance')
        if (Fraction(decimal(payload.get('limit_price')))/Fraction(decimal(state['asset']['price_increment']))).denominator!=1:
            raise ValueError('native_cycle_exit_price_off_native_grid')
        request=deepcopy(payload)
        if state['contract']=='native_crypto_long_cycle_v2':request['intent_revision']=revision
        state['exit_requests'].append(request);state['exits'].append(None)
        state['position_known']=False
    elif kind=='exit_observed':
        if not state['exit_requests']:raise ValueError('native_cycle_exit_not_started')
        old=_latest_exit(state);request=state['exit_requests'][-1]
        if request.get('proven_unsent'):raise ValueError('native_cycle_observation_for_unsent_exit')
        new=crypto_order_truth(payload,asset=state['asset'],expected_order_id=old.order_id if old else payload.get('id'))
        if (new.side!='sell' or new.client_order_id!=request['client_order_id'] or
                new.quantity!=decimal(request['qty']) or new.limit_price!=decimal(request['limit_price']) or
                new.order_type!='limit' or new.time_in_force!='ioc' or new.replaces or new.replaced_by):
            raise ValueError('native_cycle_exit_identity_changed')
        if old and (new.filled_quantity<old.filled_quantity or old.terminal and
                (new.status!=old.status or new.filled_quantity!=old.filled_quantity)):
            raise ValueError('native_cycle_exit_evidence_regressed')
        sold=sum((Fraction(decimal(x['filled_qty'],zero=True)) for x in state['exits'][:-1] if x),Fraction(0))
        if sold+Fraction(new.filled_quantity)>Fraction(_entry(state).filled_quantity):
            raise ValueError('native_cycle_exit_exceeds_gross_entry')
        state['exits'][-1]=deepcopy(payload);state['exit_revision']=revision
    else:raise ValueError('native_cycle_unknown_event')
    state['revision']=revision
    return state


def exposure(state):
    """Full-notional unprotected bound; no invented stop, reflection or settled fee."""
    if state['closed']:return dict(debit='0',risk='0',closed=True)
    # Until the owner reconciles funding reflection/settlement, retain the full
    # frozen instruction, including when an entry only partially filled.
    return dict(debit=state['original_debit'],risk=state['original_debit'],closed=False)
