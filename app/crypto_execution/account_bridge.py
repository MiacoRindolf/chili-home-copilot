"""Exact native claims visible to other PAPER account reservation writers."""
from decimal import Decimal
from fractions import Fraction
import json
import re

from sqlalchemy import text

from .funding import amount
from .lifecycle import digest,decimal_text,exposure,transition
from .store import ACCOUNT_LOCK
from .truth import crypto_order_truth,crypto_position_truth,identity


def require_account_lock(connection):
    key=ACCOUNT_LOCK & ((1<<64)-1)
    held=connection.execute(text('''SELECT EXISTS (
        SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
        AND granted AND classid=:high AND objid=:low AND objsubid=1)'''),
        {'high':key>>32,'low':key & ((1<<32)-1)}).scalar_one()
    if held is not True:raise ValueError('native_bridge_account_lock_required')


def native_account_snapshot(connection,*,account_id,schema='public'):
    """Read every active native claim under the existing primary account lock.

    Joining the event head validates the same current state exposed by the owner.
    No LIMIT, symbol rank, timestamp-derived release or broker reflection credit.
    """
    if not re.fullmatch('[a-z][a-z0-9_]*',schema):raise ValueError('native_bridge_schema_invalid')
    account_id=identity(account_id)
    require_account_lock(connection)
    rows=connection.execute(text(f'''SELECT c.*,e.event_sha256 AS event_head,e.state_sha256 AS event_state
        FROM {schema}.native_crypto_cycles c LEFT JOIN {schema}.native_crypto_cycle_events e
        ON e.cycle_id=c.cycle_id AND e.revision=c.revision
        WHERE c.account_id=:account AND NOT c.closed ORDER BY c.cycle_id'''),
        {'account':account_id}).mappings().all()
    states=[];debit=risk=Fraction(0)
    for row in rows:
        state=json.loads(row['state_json']);bound=exposure(state)
        if (state['account_id']!=account_id or state['cycle_id']!=str(row['cycle_id']) or
                state['asset']['id']!=str(row['asset_id']) or state['revision']!=row['revision'] or
                state['instruction']['client_order_id']!=row['client_order_id'] or
                state['quote_currency']!=row['quote_currency'] or state['quote_currency']!='USD' or
                bound['closed'] is not False or digest(state)!=row['state_sha256'] or
                row['event_head']!=row['head_sha256'] or row['event_state']!=row['state_sha256'] or
                Decimal(bound['debit'])!=row['debit'] or Decimal(bound['risk'])!=row['risk']):
            raise ValueError('native_bridge_projection_unverified')
        debit+=Fraction(amount(bound['debit']));risk+=Fraction(amount(bound['risk']))
        states.append(state)
    return dict(account_id=account_id,states=states,debit=debit,risk=risk,
        cycle_ids=[state['cycle_id'] for state in states],
        snapshot_sha256=digest([dict(cycle_id=s['cycle_id'],revision=s['revision'],state_sha256=digest(s)) for s in states]))


def equity_native_bound(snapshot,*,ordinary_risk,account_risk_budget,
                        ordinary_bp_required,available_bp,multiplier):
    """Conservative cross-asset margin-capacity bound, never a broker BP quote.

    Crypto has no margin borrowing and 100% maintenance. Until reflection is
    proven, reserve its entire frozen cash debit in margin-capacity units using
    this account's reported margin multiplier. This may overlap broker charges;
    it does not claim that observed RegT/DTBP always moves by multiplier*debit.
    """
    risk=Fraction(amount(ordinary_risk))+snapshot['risk']
    budget=Fraction(amount(account_risk_budget))
    scale=Fraction(amount(multiplier))
    # Documented account classes, not a strategy parameter or fitted threshold.
    if scale not in (1,2,4):raise ValueError('native_bridge_account_multiplier_unverified')
    shadow_bp=scale*snapshot['debit']
    required=Fraction(amount(ordinary_bp_required))+shadow_bp
    available=Fraction(amount(available_bp))
    if min(risk,budget,required,available)<0:raise ValueError('native_bridge_budget_invalid')
    return dict(ok=risk<=budget and required<=available,
        risk_fits=risk<=budget,buying_power_fits=required<=available,
        native_risk_usd=decimal_text(snapshot['risk']),native_debit_upper_bound_usd=decimal_text(snapshot['debit']),
        native_margin_capacity_charge_usd=decimal_text(shadow_bp),
        account_multiplier=decimal_text(scale),projected_account_risk_usd=decimal_text(risk),
        buying_power_required_upper_bound_usd=decimal_text(required),
        account_buying_power_usd=decimal_text(available),native_cycle_ids=snapshot['cycle_ids'],
        native_snapshot_sha256=snapshot['snapshot_sha256'],
        native_reflection_credit='0',policy='full_native_cash_debit_in_account_margin_capacity_units',
        actual_broker_buying_power_change_certified=False)


def partition_owned_native_exposure(snapshot,*,positions,orders):
    """Remove only fully matched native exposure from the ordinary equity view.

    A native asset UUID and exact available/gross quantities survive alongside
    the legacy display floats. CID alone or a ticker-shaped string is not proof.
    Pending fills must be reconciled by the native owner before stale position
    state can authorize another symbol's entry.
    """
    by_asset={state['asset']['id']:state for state in snapshot['states']}
    native_seen=set();ordinary_positions=[];ordinary_orders=[];symbols=[]
    for row in positions:
        if type(row) is not dict:raise ValueError('native_bridge_position_shape_invalid')
        if row.get('asset_class')!='crypto':
            if 'native_crypto_position' in row:raise ValueError('native_bridge_position_class_conflict')
            ordinary_positions.append(row);continue
        raw=row.get('native_crypto_position')
        if type(raw) is not dict:raise ValueError('native_bridge_exact_position_missing')
        aid=identity(raw.get('asset_id'));state=by_asset.get(aid)
        if state is None or aid in native_seen:raise ValueError('native_bridge_position_owner_unknown')
        if not state['position_known'] or state['position'] is None:
            raise ValueError('native_bridge_position_reconciliation_required')
        observed=crypto_position_truth(raw,asset=state['asset'])
        retained=crypto_position_truth(state['position'],asset=state['asset'])
        if observed.quantity<=0 or observed.quantity!=retained.quantity:
            raise ValueError('native_bridge_position_quantity_changed')
        native_seen.add(aid);symbols.append(state['asset']['symbol'])
    for aid,state in by_asset.items():
        if state['position'] is not None and crypto_position_truth(state['position'],asset=state['asset']).quantity>0 and aid not in native_seen:
            raise ValueError('native_bridge_owned_position_missing_at_broker')
    order_ids=set();client_ids=set()
    for order in orders:
        normalized=(order.get('raw',order) if type(order) is dict else getattr(order,'raw',{}))
        normalized=normalized if type(normalized) is dict else {}
        asset_class=normalized.get('broker_asset_class_echo',normalized.get('asset_class'))
        if asset_class!='crypto':
            if 'native_crypto_order' in normalized:raise ValueError('native_bridge_order_class_conflict')
            ordinary_orders.append(order);continue
        raw=normalized.get('native_crypto_order')
        if type(raw) is not dict:raise ValueError('native_bridge_exact_order_missing')
        state=by_asset.get(identity(raw.get('asset_id')))
        if state is None:raise ValueError('native_bridge_order_owner_unknown')
        observed=crypto_order_truth(raw,asset=state['asset'],expected_order_id=raw.get('id'))
        if observed.order_id in order_ids or observed.client_order_id in client_ids:
            raise ValueError('native_bridge_duplicate_open_order')
        order_ids.add(observed.order_id);client_ids.add(observed.client_order_id)
        if observed.terminal:raise ValueError('native_bridge_terminal_order_in_open_census')
        if observed.side=='buy' and observed.client_order_id==state['instruction']['client_order_id']:
            transition(state,dict(kind='entry_observed',payload=raw))
        elif (observed.side=='sell' and state['exit_requests'] and
                observed.client_order_id==state['exit_requests'][-1]['client_order_id']):
            transition(state,dict(kind='exit_observed',payload=raw))
        else:raise ValueError('native_bridge_order_instruction_not_owned')
    return dict(positions=ordinary_positions,orders=ordinary_orders,native_symbols=sorted(symbols),
                native_position_count=len(native_seen),native_open_order_count=len(orders)-len(ordinary_orders))
