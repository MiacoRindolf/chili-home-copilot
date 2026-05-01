"""Tests for tick_normalizer — the venue-aware precision helper.

These tests are the canonical specification of the tick rules. If the
broker's behavior changes (e.g., Robinhood loosens their sub-penny rule),
these tests fail and the venue rules get updated in lockstep.
"""
from __future__ import annotations

import pytest

from app.services.trading.tick_normalizer import (
    normalize_price,
    normalize_quantity,
    tick_size,
)


# ── Equity, price >= $1 — 2 decimals ──────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (2.5898, 2.59),
        (1.3326, 1.33),
        (4.0158, 4.02),
        (4.1766, 4.18),
        (11.0584, 11.06),
        (150.0, 150.0),
        (1.84, 1.84),
        (1.005, 1.00),  # banker's rounding: 1.005 → 1.00 (round half to even)
        (1.015, 1.02),  # 1.015 → 1.02 (1 is odd, rounds away from even neighbor)
    ],
)
def test_equity_above_dollar_rounds_to_two_decimals(raw, expected):
    assert normalize_price(raw, "AAPL") == expected


def test_equity_above_dollar_aligns_to_penny_tick():
    assert tick_size("AAPL", 150.0) == pytest.approx(0.01)


# ── Equity, price < $1 — 4 decimals (NMS sub-dollar) ──────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.5234, 0.5234),
        (0.99999, 1.0),  # boundary — rounds up to >= $1, 2-decimal tick
        (0.12345, 0.1234),  # banker's: trailing 5 next-to-4 (even) → keeps 4
        (0.12355, 0.1236),  # 5 → next is even (6)
    ],
)
def test_equity_below_dollar_rounds_to_four_decimals(raw, expected):
    assert normalize_price(raw, "PENNY") == expected


def test_equity_below_dollar_aligns_to_basis_point_tick():
    assert tick_size("PENNY", 0.5) == pytest.approx(0.0001)


# ── Crypto — 8 decimals ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.10984, 0.10984),  # the DOGE-USD case from the production incident
        (0.000000123, 0.00000012),  # banker's: 123 ends in 3, half is 0.5e-9 → keeps 12
        (78293.82, 78293.82),  # BTC-USD typical price
        (0.0000000001, 0.0),
    ],
)
def test_crypto_keeps_eight_decimals(raw, expected):
    assert normalize_price(raw, "DOGE-USD") == pytest.approx(expected, abs=1e-12)


def test_crypto_aligns_to_eight_decimal_tick():
    assert tick_size("BTC-USD", 78293.82) == pytest.approx(1e-8)


def test_btc_8dec_value_round_trips():
    raw = 12345.12345678
    assert normalize_price(raw, "BTC-USD") == pytest.approx(raw, abs=1e-12)


# ── Production-incident regression test ───────────────────────────────


def test_doge_stop_no_longer_destroyed():
    """Regression: DOGE-USD at 0.10984 must not become 0.11.

    This is the exact scenario from the 2026-05-01 incident — broker_service
    was rounding crypto submissions to 2 decimals, causing 1.4% destructive
    silent slippage on every crypto stop.
    """
    raw = 0.10984
    normalized = normalize_price(raw, "DOGE-USD")
    assert normalized == pytest.approx(0.10984, abs=1e-9)
    # And specifically NOT 0.11.
    assert abs(normalized - 0.11) > 1e-4


def test_4dec_equity_stop_aligns_cleanly_to_penny():
    """Regression: CCCC at 2.5898 must align cleanly so Robinhood accepts it.

    The brain stores 4-decimal stops; the broker rounds to 2; Robinhood's
    validator was flagging the truncated value as invalid. With the
    normalizer, the value is tick-aligned at storage time so submission
    is unambiguous.
    """
    assert normalize_price(2.5898, "CCCC") == 2.59
    assert normalize_price(1.3326, "CRDL") == 1.33
    assert normalize_price(4.0158, "TLS") == 4.02
    assert normalize_price(4.1766, "VFS") == 4.18


# ── Options ───────────────────────────────────────────────────────────

# OCC-format symbol: AAPL241220C00150000 = AAPL 2024-12-20 Call $150.00
SAMPLE_CALL_3PLUS = "AAPL241220C00150000"
SAMPLE_CALL_LOW = "AAPL241220C00010000"


def test_option_premium_above_three_uses_penny_tick():
    assert tick_size(SAMPLE_CALL_3PLUS, 5.0) == pytest.approx(0.01)
    assert normalize_price(5.234, SAMPLE_CALL_3PLUS) == 5.23


def test_option_premium_below_three_uses_nickel_tick():
    # OPRA "nickel tier" — sub-$3 options aligned to $0.05
    assert tick_size(SAMPLE_CALL_LOW, 1.0) == pytest.approx(0.05)
    assert normalize_price(0.07, SAMPLE_CALL_LOW) == pytest.approx(0.05)
    assert normalize_price(0.13, SAMPLE_CALL_LOW) == pytest.approx(0.15)


# ── Quantity rounding ─────────────────────────────────────────────────


def test_equity_qty_six_decimal_fractional():
    # Robinhood fractional shares: 6-decimal qty
    assert normalize_quantity(1.234567, "AAPL") == pytest.approx(1.234567)
    assert normalize_quantity(1.2345678, "AAPL") == pytest.approx(1.234568)


def test_crypto_qty_eight_decimal():
    assert normalize_quantity(0.00012345, "BTC-USD") == pytest.approx(0.00012345)
    assert normalize_quantity(0.000000018, "BTC-USD") == pytest.approx(0.00000002)


def test_option_qty_is_integer_contracts():
    # OCC symbols quote in whole contracts only
    assert normalize_quantity(5.7, SAMPLE_CALL_3PLUS) == pytest.approx(6.0)


# ── Edge cases ────────────────────────────────────────────────────────


def test_none_price_returns_none():
    assert normalize_price(None, "AAPL") is None


def test_zero_price_returns_zero():
    assert normalize_price(0.0, "AAPL") == 0.0


def test_string_price_parses():
    assert normalize_price("2.5898", "CCCC") == 2.59


def test_garbage_input_passes_through_with_warning(caplog):
    with caplog.at_level("WARNING"):
        result = normalize_price("not a number", "AAPL")
    assert result == 0.0  # last-resort
    assert any("could not parse" in r.message for r in caplog.records)
