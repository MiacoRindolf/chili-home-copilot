from dataclasses import FrozenInstanceError
from decimal import Decimal
import json

import pytest

from scripts.crypto_trade_frames import decode_frame, timestamp_ns


TRADE = {"T": "t", "S": "BTC/USD", "p": "100.123456789123456789", "s": "0.000000001",
         "i": 3447222699101865076, "t": "2026-09-12T04:05:06.123456789Z", "tks": "B"}
QUOTE = {"T": "q", "S": "BTC/USD", "bp": "100", "ap": "101", "bs": "1", "as": "2",
         "t": "2026-09-12T04:05:06.123456788Z"}


def decode(messages):
    return decode_frame(json.dumps(messages), feed_location="us", connection_id="fixture-connection",
                        frame_sequence=1, received_ns=timestamp_ns("2026-09-12T04:05:07Z"),
                        subscribed_symbols=frozenset({"BTC/USD", "ETH/USD"}))


def test_exact_trade_identity_decimal_precision_and_nanoseconds():
    frame = decode([TRADE])
    trade = frame.trades[0]
    assert trade.trade_id == 3447222699101865076
    assert trade.price == Decimal("100.123456789123456789")
    assert trade.size == Decimal("0.000000001")
    assert trade.event_ns % 1_000_000_000 == 123456789
    assert trade.aggressor_sign == 1 and trade.side_basis == "alpaca_reported_taker"
    with pytest.raises(FrozenInstanceError):
        trade.price = Decimal(1)


@pytest.mark.parametrize("side,expected,basis", [("B", 1, "alpaca_reported_taker"),
    ("S", -1, "alpaca_reported_taker"), (None, 0, "unknown"), ("", 0, "unknown")])
def test_reported_taker_side_is_not_inverted_or_fabricated(side, expected, basis):
    trade = decode([{**TRADE, "tks": side}]).trades[0]
    assert (trade.aggressor_sign, trade.side_basis) == (expected, basis)


def test_one_frame_keeps_spike_reversal_and_interleaved_quote_membership():
    frame = decode([TRADE, QUOTE, {**TRADE, "i": 20, "p": "110"},
                    {**TRADE, "i": 21, "p": "99", "tks": "S"}])
    assert [t.member_index for t in frame.trades] == [0, 2, 3]
    assert frame.quotes[0].member_index == 1
    assert [t.price for t in frame.trades] == [Decimal(TRADE["p"]), 110, 99]
    assert frame.message_count == 4


@pytest.mark.parametrize("change", [{"i": 1.5}, {"i": True}, {"p": "NaN"}, {"s": "0"},
    {"tks": "SELL"}, {"t": "2026-09-12T04:05:06"}, {"S": "UNKNOWN/USD"}, {"T": "b"}])
def test_invalid_member_rejects_whole_frame_without_returning_a_partial_prefix(change):
    with pytest.raises(ValueError):
        decode([TRADE, {**TRADE, **change}])


def test_offset_timestamps_preserve_same_nanosecond_instant():
    assert timestamp_ns("2026-09-12T06:05:06.123456789+02:00") == timestamp_ns(TRADE["t"])


def test_zero_or_crossed_quotes_are_retained_as_non_executable_evidence():
    quote = decode([{**QUOTE, "bp": "102"}]).quotes[0]
    assert not quote.two_sided_uncrossed and quote.bid == 102
    assert not decode([{**QUOTE, "bs": "0"}]).quotes[0].two_sided_uncrossed


def test_duplicate_json_keys_and_nonfinite_json_numbers_are_rejected():
    from scripts.crypto_trade_frames import decode_json
    for raw in ('{"p":1,"p":2}', '[NaN]', '[Infinity]'):
        with pytest.raises(ValueError):
            decode_json(raw)


def test_numeric_json_decimals_do_not_pass_through_binary_float():
    raw = json.dumps([TRADE]).replace('"100.123456789123456789"', '100.123456789123456789')
    frame = decode_frame(raw, feed_location="us", connection_id="fixture", frame_sequence=1,
                         received_ns=1, subscribed_symbols=frozenset({"BTC/USD"}))
    assert frame.trades[0].price == Decimal("100.123456789123456789")
