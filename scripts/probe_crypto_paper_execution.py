"""Isolated minimum-size PAPER execution experiment, never a strategy runner.

Requires the ordinary execution process to be absent and owns the same ALPA/OWNR
lease for the entire experiment. There is exactly one IOC entry request. Close
only its proven position through Alpaca's single-asset full-liquidation endpoint.
Every request/response is fsynced, including the intent BEFORE broker mutations.
No LIVE endpoint, global liquidation, equity zero-fee assumption or DB row write.
Transport deadlines below are experiment resource bounds, not strategy windows.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, Inexact, localcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAPER = 'https://paper-api.alpaca.markets'
DATA = 'https://data.alpaca.markets'
SYMBOL = 'BTC/USD'  # Explicit diagnostic instrument, not a universe/ranking rule.
TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def exact(value):
    if not isinstance(value, (str, Decimal)):
        raise ValueError('decimal_string_required')
    out = Decimal(value)
    if not out.is_finite():
        raise ValueError('finite_decimal_required')
    return out


def minimum_instruction(asset, quote):
    if (asset.get('symbol') != SYMBOL or asset.get('class') != 'crypto'
            or asset.get('status') != 'active' or asset.get('tradable') is not True):
        raise ValueError('native_crypto_asset_not_tradable')
    UUID(asset['id'])
    minimum, step, price_step = [exact(asset[k]) for k in
        ('min_order_size', 'min_trade_increment', 'price_increment')]
    ask, bid = exact(quote['ap']), exact(quote['bp'])
    if min(minimum, step, price_step, ask, bid) <= 0 or bid > ask or not quote.get('t'):
        raise ValueError('asset_or_quote_constraints_unavailable')
    with localcontext() as ctx:
        # Arithmetic resource bound: never silently round a quantity/cost.
        ctx.prec = 128
        ctx.traps[Inexact] = True
        # Division is only for finding the next integer multiple; Fraction
        # prevents Decimal context rounding before the ceiling operation.
        from fractions import Fraction
        def ceil_multiple(value, increment):
            ratio = Fraction(value) / Fraction(increment)
            n = -(-ratio.numerator // ratio.denominator)
            return increment * n
        qty = ceil_multiple(minimum, step)
        limit = ceil_multiple(ask, price_step)
        cost = qty * limit
    return {'symbol': SYMBOL, 'qty': format(qty, 'f'), 'side': 'buy',
        'type': 'limit', 'time_in_force': 'ioc', 'limit_price': format(limit, 'f')}, cost


def owned_position(positions, buy, asset_id):
    if not isinstance(positions, list) or len(positions) != 1:
        raise ValueError('expected_one_owned_diagnostic_position')
    from app.services.trading.venue.crypto_execution_truth import crypto_position_truth
    p = crypto_position_truth(positions[0], asset={'id': asset_id, 'class': 'crypto', 'symbol': SYMBOL})
    qty, available, filled = p.quantity, p.available_quantity, exact(buy['filled_qty'])
    if not p.whole_balance_available or not 0 < qty <= filled:
        raise ValueError('position_ownership_or_availability_not_proven')
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.traps[Inexact] = True
        difference = filled - qty
    return {'gross_filled_qty': str(filled), 'position_qty': str(qty),
        'available_qty': str(available), 'gross_minus_position': str(difference),
        'difference_is_proven_fee': False}


def fill_evidence(activities, buy, sell):
    """Reconcile observed executions; never promote missing fees to net P&L.

    FILL.qty is incremental. cum_qty is a progress report, not another fill.
    Activity IDs deduplicate overlap; conflicting versions require investigation.
    Fee rows can lack order IDs and arrive after this probe, so retain them
    without attributing an account-wide fee to this one pair of orders.
    """
    if not isinstance(activities, list):
        raise ValueError('activity_list_required')
    if not buy.get('id') or not sell.get('id') or buy['id'] == sell['id']:
        raise ValueError('distinct_order_ids_required')
    orders = {buy['id']: buy, sell['id']: sell}
    seen, ignored, fee_rows = {}, [], []
    quantities = {oid: Decimal(0) for oid in orders}
    notionals = {oid: Decimal(0) for oid in orders}
    with localcontext() as ctx:
        ctx.prec = 128  # Computational bound; arithmetic must be exact.
        ctx.traps[Inexact] = True
        for row in activities:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                raise ValueError('activity_identity_required')
            aid = row['id']
            if aid in seen:
                if seen[aid] != row:
                    raise ValueError('conflicting_activity_identity')
                continue
            seen[aid] = row
            if row.get('activity_type') in {'CFEE', 'FEE'}:
                fee_rows.append(row)
                continue
            oid = row.get('order_id')
            if row.get('activity_type') != 'FILL' or oid not in orders:
                ignored.append(aid)
                continue
            order = orders[oid]
            if row.get('side') != order['side'] or row.get('symbol') != order['symbol']:
                raise ValueError('fill_order_identity_mismatch')
            qty, price = exact(row['qty']), exact(row['price'])
            if qty <= 0 or price <= 0 or row.get('type') not in {'fill', 'partial_fill'}:
                raise ValueError('unsupported_fill_evidence')
            quantities[oid] += qty
            notionals[oid] += qty * price
        matches = {}
        for oid, order in orders.items():
            expected = exact(order['filled_qty'])
            if expected < 0 or quantities[oid] > expected:
                raise ValueError('activity_quantity_exceeds_order_fill')
            matches[oid] = quantities[oid] == expected
        complete = all(matches.values())
        cashflow = notionals[sell['id']] - notionals[buy['id']]
    return {'fill_quantities': {k: str(v) for k, v in quantities.items()},
        'fill_notionals': {k: str(v) for k, v in notionals.items()},
        'matches_reported_order_quantities': matches,
        'fills_reconciled': complete,
        'observed_fill_cashflow_quote': str(cashflow) if complete else None,
        'ignored_activity_ids': ignored, 'unattributed_fee_rows': fee_rows,
        'fees_complete': False, 'net_realized_pnl': None}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('authenticated_redirect_refused')


class Journal:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=False)
        self.path = directory / 'events.jsonl'
        self.handle = self.path.open('x', encoding='utf-8')
        self.chain = '0' * 64

    def add(self, event, **data):
        payload = {'at': stamp(), 'event': event, 'previous_sha256': self.chain, **data}
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
        self.chain = hashlib.sha256(raw.encode()).hexdigest()
        self.handle.write(json.dumps({'payload': payload, 'sha256': self.chain}, default=str) + '\n')
        self.handle.flush()
        os.fsync(self.handle.fileno())


class Client:
    def __init__(self, settings, journal, lease=None):
        if str(settings.get('CHILI_ALPACA_PAPER', '')).lower() not in {'true', '1'}:
            raise ValueError('saved_environment_not_paper')
        self.pin = str(UUID(settings['CHILI_ALPACA_EXPECTED_ACCOUNT_ID']))
        self.key, self.secret = settings['CHILI_ALPACA_API_KEY'], settings['CHILI_ALPACA_API_SECRET']
        if not self.key or not self.secret:
            raise ValueError('paper_credentials_missing')
        self.journal, self.lease = journal, lease
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, payload=None, *, data=False):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('relative_api_path_required')
        if method != 'GET':
            if data or self.lease is None or not self.lease.held_by_me():
                raise ValueError('mutation_requires_owned_paper_lease')
            account = self.request('GET', '/v2/account')
            if account['id'] != self.pin:
                raise ValueError('paper_account_identity_changed')
        self.journal.add('request', method=method, path=path, payload=payload, data_endpoint=data)
        raw = None if payload is None else json.dumps(payload).encode()
        req = Request((DATA if data else PAPER) + path, data=raw, method=method,
            headers={'APCA-API-KEY-ID': self.key, 'APCA-API-SECRET-KEY': self.secret,
                     'Content-Type': 'application/json'})
        if method != 'GET' and not self.lease.held_by_me():
            raise ValueError('paper_lease_lost_before_transport')
        try:
            with self.opener.open(req, timeout=20) as response:
                body, status = response.read(), response.status
        except HTTPError as exc:
            body, status = exc.read(), exc.code
        except Exception as exc:
            self.journal.add('transport_unknown', method=method, path=path, error_type=type(exc).__name__)
            raise RuntimeError('request_outcome_unknown_no_automatic_resubmit') from None
        if self.key.encode() in body or self.secret.encode() in body:
            raise RuntimeError('credential_echo_refused')
        self.journal.add('response', method=method, path=path, status=status, raw=body.decode('utf-8'))
        if not 200 <= status < 300:
            raise RuntimeError('broker_http_' + str(status))
        return json.loads(body, parse_float=Decimal)

    def terminal(self, order):
        # Poll the same broker order, never retry POST on observation timeout.
        deadline = time.monotonic() + 60
        oid = str(UUID(order['id']))
        while order.get('status') not in TERMINAL:
            if time.monotonic() >= deadline:
                raise RuntimeError('order_observation_deadline_unresolved')
            time.sleep(1)
            order = self.request('GET', '/v2/orders/' + oid)
        self.journal.add('terminal_order', order=order)
        return order


def experiment(client, journal, *, execute, run_id):
    account = client.request('GET', '/v2/account')
    if account.get('id') != client.pin or account.get('status') != 'ACTIVE':
        raise ValueError('paper_account_not_verified')
    for flag in ('account_blocked', 'trading_blocked', 'trade_suspended_by_user'):
        if account.get(flag) is not False:
            raise ValueError('paper_account_not_ready_' + flag)
    if client.request('GET', '/v2/positions') or client.request('GET', '/v2/orders?status=open&limit=500'):
        raise ValueError('diagnostic_requires_flat_unencumbered_account')
    clock = client.request('GET', '/v2/clock')
    if clock.get('is_open') is not False:
        raise ValueError('do_not_displace_active_equity_market')
    asset = client.request('GET', '/v2/assets/BTC%2FUSD')
    quote = client.request('GET', '/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD', data=True)['quotes'][SYMBOL]
    instruction, ceiling = minimum_instruction(asset, quote)
    instruction['client_order_id'] = 'astra-crypto-probe-' + run_id
    if ceiling > exact(account['non_marginable_buying_power']):
        raise ValueError('insufficient_non_marginable_buying_power')
    journal.add('plan', instruction=instruction, native_asset=asset, quote=quote,
        maximum_entry_notional=str(ceiling), basis='minimum broker quantity at rounded current ask',
        momentum_strategy=False, fees='unresolved_until_actual_evidence')
    if not execute:
        return {'status': 'prepared_read_only', 'instruction': instruction, 'maximum_entry_notional': str(ceiling)}
    since = stamp()
    buy = client.terminal(client.request('POST', '/v2/orders', instruction))
    if (buy.get('client_order_id') != instruction['client_order_id'] or buy.get('symbol') != SYMBOL
            or buy.get('side') != 'buy' or buy.get('asset_id') != asset['id']
            or not 0 <= exact(buy['filled_qty']) <= exact(instruction['qty'])):
        raise ValueError('broker_entry_echo_mismatch')
    if exact(buy['filled_qty']) == 0:
        if client.request('GET', '/v2/positions') or client.request('GET', '/v2/orders?status=open&limit=500'):
            raise ValueError('zero_fill_is_not_flat_account')
        return {'status': 'no_fill_flat', 'buy_order_id': buy['id']}
    positions = client.request('GET', '/v2/positions')
    comparison = owned_position(positions, buy, asset['id'])
    # No other instruction may reserve the acquired balance before full close.
    if client.request('GET', '/v2/orders?status=open&limit=500'):
        raise ValueError('unexpected_open_order_before_full_close')
    journal.add('owned_full_close', comparison=comparison, position=positions[0], buy_order_id=buy['id'])
    # No all-account liquidation. The exact native asset UUID avoids ambiguous
    # dash/slash symbol routing and lets the broker close its complete balance.
    sell = client.terminal(client.request('DELETE', '/v2/positions/' + asset['id']))
    if (sell.get('side') != 'sell' or sell.get('symbol') != SYMBOL
            or sell.get('asset_id') != asset['id']):
        raise ValueError('broker_exit_echo_mismatch')
    final_positions = client.request('GET', '/v2/positions')
    final_orders = client.request('GET', '/v2/orders?status=open&limit=500')
    flat = not final_positions and not final_orders
    journal.add('final_census', positions=final_positions, orders=final_orders, flat=flat)
    if not flat:
        raise RuntimeError('full_close_residual_requires_reconciliation')
    activities = client.request('GET', '/v2/account/activities?' + urlencode({
        'activity_types': 'FILL,CFEE,FEE', 'after': since, 'direction': 'asc', 'page_size': 100}))
    final_account = client.request('GET', '/v2/account')
    evidence = fill_evidence(activities, buy, sell)
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.traps[Inexact] = True
        cash_delta = exact(final_account['cash']) - exact(account['cash'])
    return {'status': 'roundtrip_flat', 'buy_order_id': buy['id'], 'sell_order_id': sell['id'],
        'comparison': comparison, 'buy': buy, 'sell': sell,
        'account_cash_delta': str(cash_delta), 'fill_evidence': evidence,
        'activities': activities, 'activity_page_short': len(activities) < 100,
        'fees_complete': False, 'strategy_enabled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--supervisor-file', type=Path)
    args = parser.parse_args()
    from dotenv import dotenv_values
    journal = Journal(args.output_dir)
    lease = None
    try:
        if args.execute:
            if args.supervisor_file is None:
                raise ValueError('existing_supervisor_required')
            spec = importlib.util.spec_from_file_location('paper_supervisor', args.supervisor_file)
            supervisor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(supervisor)
            lease = supervisor.Lease()
            lease.cur.execute("SET statement_timeout='20s'")
            if not lease.acquire() or not lease.held_by_me():
                raise ValueError('another_paper_owner_is_running')
            census = supervisor._producer_census(self_pid=os.getpid())
            counters = lease.six_counters()
            journal.add('exclusive_owner', pid=os.getpid(), backend_pid=lease.backend_pid,
                supervisor_sha256=hashlib.sha256(args.supervisor_file.read_bytes()).hexdigest(),
                producer_census=census, database_counters=counters)
            if not census.get('clean') or any(counters.values()):
                raise ValueError('paper_producer_or_state_census_not_clean')
        client = Client(dotenv_values(args.env_file), journal, lease)
        result = experiment(client, journal, execute=args.execute, run_id=uuid4().hex[:20])
        journal.add('result', result=result)
        (args.output_dir/'result.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
        print(json.dumps({'status': result['status'], 'output_dir': str(args.output_dir)}))
    except Exception as exc:
        journal.add('stopped', error_type=type(exc).__name__, reason=str(exc))
        print(json.dumps({'status': 'stopped', 'reason': str(exc), 'journal': str(journal.path)}))
        raise SystemExit(1)
    finally:
        if lease is not None:
            lease.conn.close()
        journal.handle.close()


if __name__ == '__main__':
    main()
