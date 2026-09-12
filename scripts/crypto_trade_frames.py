"""Exact Alpaca crypto trade/quote message decoding, with source provenance.

No bars, inferred trade IDs, wall-clock strategy windows or silent row trimming.
The caller owns subscription acknowledgement, durable publication, duplicate/gap
handling and the consumer frontier. Decoding alone grants no trading authority.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


@dataclass(frozen=True)
class CryptoTrade:
    symbol: str
    trade_id: int
    price: Decimal
    size: Decimal
    event_ns: int
    received_ns: int
    member_index: int
    reported_taker_side: str | None
    aggressor_sign: int
    side_basis: str = "alpaca_reported_taker"


@dataclass(frozen=True)
class CryptoQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    event_ns: int
    received_ns: int
    member_index: int

    @property
    def two_sided_uncrossed(self) -> bool:
        return self.bid > 0 and self.ask >= self.bid and self.bid_size > 0 and self.ask_size > 0


@dataclass(frozen=True)
class CryptoFrame:
    feed_location: str
    connection_id: str
    frame_sequence: int
    raw_sha256: str
    received_ns: int
    trades: tuple[CryptoTrade, ...]
    quotes: tuple[CryptoQuote, ...]
    control_types: tuple[str, ...]
    message_count: int


def timestamp_ns(value: object) -> int:
    """RFC3339 to integer nanoseconds without float or microsecond truncation."""
    if not isinstance(value, str):
        raise ValueError("crypto_timestamp_missing")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if match is None:
        raise ValueError("crypto_timestamp_invalid")
    try:
        second = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("crypto_timestamp_invalid") from None
    whole = calendar.timegm(second.astimezone(timezone.utc).utctimetuple())
    return whole * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def _pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("crypto_duplicate_json_key")
        result[key] = value
    return result


def decode_json(raw: str):
    def invalid_constant(_):
        raise ValueError("crypto_nonfinite_json_number")
    return json.loads(raw, parse_float=Decimal, parse_int=int,
                      parse_constant=invalid_constant, object_pairs_hook=_pairs_without_duplicates)


def _number(value: object, *, positive: bool) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("crypto_number_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("crypto_number_invalid") from None
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError("crypto_number_invalid")
    return number


def decode_frame(raw: str | bytes, *, feed_location: str, connection_id: str,
                 frame_sequence: int, received_ns: int,
                 subscribed_symbols: frozenset[str]) -> CryptoFrame:
    """Validate the entire received message before returning any constituent."""
    if feed_location not in {"us", "us-1", "eu-1"}:
        raise ValueError("crypto_feed_location_unknown")
    if not isinstance(connection_id, str) or not connection_id:
        raise ValueError("crypto_connection_identity_missing")
    if type(frame_sequence) is not int or frame_sequence <= 0:
        raise ValueError("crypto_frame_sequence_invalid")
    if type(received_ns) is not int or received_ns <= 0:
        raise ValueError("crypto_receive_clock_invalid")
    encoded = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    payload = decode_json(encoded.decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("crypto_frame_must_be_nonempty_array")
    trades, quotes, controls = [], [], []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError("crypto_message_invalid")
        kind = item.get("T")
        if kind in {"success", "subscription"}:
            controls.append(kind)
            continue
        if kind == "error":
            raise ValueError("crypto_provider_error")
        if kind not in {"t", "q"}:
            raise ValueError("crypto_non_trade_quote_message")
        symbol = item.get("S")
        if symbol not in subscribed_symbols:
            raise ValueError("crypto_symbol_not_subscribed")
        event_ns = timestamp_ns(item.get("t"))
        if kind == "t":
            trade_id = item.get("i")
            if type(trade_id) is not int or trade_id < 0:
                raise ValueError("crypto_trade_identity_invalid")
            side = item.get("tks")
            # An absent side is unresolved; an undocumented value must not be
            # interpreted as seller-initiated by an else branch.
            if side not in {None, "", "B", "S"}:
                raise ValueError("crypto_taker_side_invalid")
            trades.append(CryptoTrade(
                symbol, trade_id, _number(item.get("p"), positive=True),
                _number(item.get("s"), positive=True), event_ns, received_ns,
                index, side or None, {"B": 1, "S": -1}.get(side, 0),
                "alpaca_reported_taker" if side else "unknown",
            ))
        else:
            quotes.append(CryptoQuote(
                symbol, _number(item.get("bp"), positive=False),
                _number(item.get("ap"), positive=False),
                _number(item.get("bs"), positive=False),
                _number(item.get("as"), positive=False), event_ns, received_ns, index,
            ))
    return CryptoFrame(feed_location, connection_id, frame_sequence,
        hashlib.sha256(encoded).hexdigest(), received_ns, tuple(trades), tuple(quotes),
        tuple(controls), len(payload))
