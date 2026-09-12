"""Exact native crypto read evidence, separate from whole-share equity authority.

These records do not grant order/position ownership or certify snapshot freshness.
Order cumulative fills, held balance and available balance are different facts.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID


def value(row, key, default=None):
    item = row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)
    return getattr(item, 'value', item)


def identity(raw):
    try:
        if type(raw) is not str and not isinstance(raw, UUID):
            raise ValueError
        return str(UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('crypto_native_uuid_invalid') from None


def decimal(raw, *, zero=False, optional=False):
    if raw is None and optional:
        return None
    # Accept provider strings/Decimals, never reconstruct precision from floats.
    if type(raw) not in (str, Decimal):
        raise ValueError('crypto_native_exact_decimal_required')
    try:
        out = Decimal(raw)
    except InvalidOperation:
        raise ValueError('crypto_native_decimal_invalid') from None
    if not out.is_finite() or out < 0 or (out == 0 and not zero):
        raise ValueError('crypto_native_decimal_invalid')
    return out


def asset_identity(asset):
    asset_id = identity(value(asset, 'id'))
    symbol = value(asset, 'symbol')
    asset_class = value(asset, 'class', value(asset, 'asset_class'))
    if asset_class != 'crypto' or type(symbol) is not str or not re.fullmatch(r'[A-Z0-9]+/[A-Z0-9]+', symbol):
        raise ValueError('crypto_native_asset_identity_invalid')
    return asset_id, symbol


def matched_asset(row, asset):
    asset_id, symbol = asset_identity(asset)
    # The legacy concatenation is accepted ONLY with the matching native UUID;
    # no symbol-only guessing or splitting of a concatenated name is allowed.
    if (identity(value(row, 'asset_id')) != asset_id or value(row, 'asset_class') != 'crypto'
            or value(row, 'symbol') not in (symbol, symbol.replace('/', ''))):
        raise ValueError('crypto_native_asset_echo_mismatch')
    return asset_id, symbol


@dataclass(frozen=True)
class CryptoOrderTruth:
    order_id: str
    client_order_id: str
    asset_id: str
    native_symbol: str
    broker_symbol: str
    side: str
    status: str
    order_type: str
    time_in_force: str
    quantity: Decimal | None
    notional: Decimal | None
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    replaces: str | None
    replaced_by: str | None

    @property
    def terminal(self):
        # A replaced order requires successor reconciliation, not release of the
        # original reservation as if no child could fill. Unknown status stays open.
        return self.status in ('filled', 'canceled', 'expired', 'rejected') and self.replaced_by is None


def crypto_order_truth(row, *, asset, expected_order_id):
    asset_id, symbol = matched_asset(row, asset)
    order_id = identity(value(row, 'id'))
    if order_id != identity(expected_order_id):
        raise ValueError('crypto_native_order_echo_mismatch')
    cid = value(row, 'client_order_id')
    side, status = value(row, 'side'), value(row, 'status')
    order_type = value(row, 'type', value(row, 'order_type'))
    tif = value(row, 'time_in_force')
    if (type(cid) is not str or not cid or side not in ('buy', 'sell') or
            type(status) is not str or not status or
            order_type not in ('market', 'limit', 'stop_limit') or tif not in ('gtc', 'ioc')):
        raise ValueError('crypto_native_order_fields_invalid')
    quantity = decimal(value(row, 'qty'), optional=True)
    notional = decimal(value(row, 'notional'), optional=True)
    filled = decimal(value(row, 'filled_qty'), zero=True)
    average = decimal(value(row, 'filled_avg_price'), zero=True, optional=True)
    limit = decimal(value(row, 'limit_price'), optional=True)
    stop = decimal(value(row, 'stop_price'), optional=True)
    if quantity is None and notional is None:
        raise ValueError('crypto_native_requested_size_unavailable')
    if (quantity is not None and filled > quantity) or (status == 'filled' and
            (filled == 0 or quantity is not None and filled != quantity)):
        raise ValueError('crypto_native_fill_quantity_inconsistent')
    if (order_type in ('limit', 'stop_limit') and limit is None) or (order_type == 'stop_limit' and stop is None):
        raise ValueError('crypto_native_order_price_unavailable')
    if value(row, 'extended_hours', False) is not False or value(row, 'order_class', 'simple') not in ('simple', ''):
        raise ValueError('crypto_native_equity_order_shape')
    if value(row, 'legs'):
        raise ValueError('crypto_native_unexpected_order_legs')
    replaces = value(row, 'replaces')
    replaced_by = value(row, 'replaced_by')
    return CryptoOrderTruth(order_id, cid, asset_id, symbol, value(row, 'symbol'), side, status,
        order_type, tif, quantity, notional, filled, average if average else None, limit, stop,
        identity(replaces) if replaces is not None else None,
        identity(replaced_by) if replaced_by is not None else None)


@dataclass(frozen=True)
class CryptoPositionTruth:
    asset_id: str
    native_symbol: str
    broker_symbol: str
    quantity: Decimal
    available_quantity: Decimal | None
    average_entry_price: Decimal | None

    @property
    def whole_balance_available(self):
        # Availability is not ownership, a fee attribution, or an exit trigger.
        return self.quantity > 0 and self.available_quantity == self.quantity


def crypto_position_truth(row, *, asset):
    asset_id, symbol = matched_asset(row, asset)
    quantity = decimal(value(row, 'qty'), zero=True)
    available = decimal(value(row, 'qty_available'), zero=True, optional=True)
    if value(row, 'side') != 'long' or available is not None and available > quantity:
        raise ValueError('crypto_native_position_balance_invalid')
    average = decimal(value(row, 'avg_entry_price'), optional=True)
    return CryptoPositionTruth(asset_id, symbol, value(row, 'symbol'), quantity, available, average)
