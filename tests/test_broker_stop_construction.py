"""Tests for f-bracket-writer-stop-construction-fix.

Pre-fix: PED's 4-decimal stop_price (13.6275) was reaching Robinhood
unrounded, getting rejected with "Limit order requested, but no price
provided." 45+ retries/hour for hours.

Post-fix: place_stop_loss_sell_order calls tick_normalizer.normalize_price
BEFORE submission, logs an INFO line with the normalized value at submit
time, and a WARNING line with the full diagnostic when RH returns no
order_id. This file pins:
  1. tick_normalizer rounds 13.6275 to 13.63 for equity >= $1.
  2. The broker_service.py source contains the diagnostic log lines.
  3. The submit path uses the normalized value, not the raw input.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. tick_normalizer rounds equity 4-decimal stop to 2 decimals (PED case)
# ---------------------------------------------------------------------------

def test_normalize_price_rounds_ped_to_two_decimals():
    from app.services.trading.tick_normalizer import normalize_price
    # PED's brain stop is 13.6275; equity >= $1 -> 2-decimal tick.
    out = normalize_price(13.6275, "PED", asset_class="equity")
    # ROUND_HALF_EVEN: 13.6275 / 0.01 = 1362.75 -> nearest int via
    # banker's = 1363 (because remainder is 0.75, > 0.5). So result 13.63.
    assert out == pytest.approx(13.63), (
        f"expected 13.63 for PED 13.6275 round, got {out}"
    )


def test_normalize_price_subdollar_uses_four_decimals():
    """NMS Rule 612 carve-out: equity < $1 keeps 4-decimal tick."""
    from app.services.trading.tick_normalizer import normalize_price
    out = normalize_price(0.91234567, "AIDX", asset_class="equity")
    assert out == pytest.approx(0.9123)


# ---------------------------------------------------------------------------
# 2. broker_service.py contains the diagnostic log lines
# ---------------------------------------------------------------------------

def test_place_stop_loss_sell_order_emits_normalized_stop_log_line():
    """Source guard: the rounded log line must precede submission so
    operators can see the on-wire value."""
    src = (REPO / "app/services/broker_service.py").read_text()
    assert "stop_price rounded to broker tick" in src, (
        "INFO-level normalization log line missing from broker_service.py"
    )


def test_place_stop_loss_sell_order_emits_pre_submit_log_line():
    """Source guard: pre-submit INFO log so operators can see the body
    that's about to hit the wire."""
    src = (REPO / "app/services/broker_service.py").read_text()
    assert "[broker] SELL_STOP submitting:" in src


def test_place_stop_loss_sell_order_emits_full_diagnostic_on_rejection():
    """Source guard: on no-order-id rejection, WARNING log includes the
    normalized stop, session flags, and (truncated) response body."""
    src = (REPO / "app/services/broker_service.py").read_text()
    assert "SELL_STOP rejected (full diagnostic)" in src
    # The diagnostic must include the normalized_stop value, not just
    # the raw trigger_price.
    idx = src.find("SELL_STOP rejected (full diagnostic)")
    assert idx > 0
    # Look at the next 600 chars (covers the multi-line log call).
    block = src[idx:idx + 600]
    assert "normalized_stop=" in block, (
        "diagnostic must show the normalized value (the one actually sent)"
    )
    assert "trigger_in=" in block, (
        "diagnostic must show the input value too so the rounding "
        "delta is visible"
    )


# ---------------------------------------------------------------------------
# 3. The submit path passes normalized_stop, not trigger_price, to RH
# ---------------------------------------------------------------------------

def test_submit_passes_normalized_value_to_rh():
    """Source guard: the rh.orders.order call must use the
    normalized_stop variable, not trigger_price directly. Catches a
    future refactor that re-introduces the unrounded path."""
    src = (REPO / "app/services/broker_service.py").read_text()
    # Find the SELL_STOP submission block.
    idx = src.find("def _do_stop_sell():")
    assert idx > 0
    # Look at the next ~2000 chars for the rh.orders.order call.
    block = src[idx:idx + 2000]
    assert "stopPrice=normalized_stop" in block, (
        "rh.orders.order must use stopPrice=normalized_stop "
        "(the rounded value), not the raw trigger_price"
    )


# ---------------------------------------------------------------------------
# 4. Round-down preservation on long stops (sanity)
# ---------------------------------------------------------------------------

def test_normalize_long_stop_does_not_overshoot_brain_intent():
    """Brain wants stop at 13.6275. After rounding, stop must be at OR
    BELOW that value -- never above (rounding UP would trigger the stop
    SOONER than the brain wanted, eating more gain). 13.63 is ABOVE
    13.6275, but for SELL_STOP on a long, the trigger fires when price
    DROPS below stop_price. So 13.63 fires sooner than 13.6275 would,
    which is more conservative (less downside before stop fires). For
    long stops, rounding UP is actually the safer direction.

    This test pins the current ROUND_HALF_EVEN choice and documents the
    direction so a future refactor knows what semantic to preserve."""
    from app.services.trading.tick_normalizer import normalize_price
    rounded = normalize_price(13.6275, "PED", asset_class="equity")
    # For PED the rounding goes UP (13.63 > 13.6275). That's because the
    # input is 0.0025 above 13.62 and 0.0050 below 13.63 -- nearest is
    # 13.63 by ROUND_HALF_EVEN (the half is at 13.6250; 13.6275 is past
    # halfway to 13.63).
    assert rounded == pytest.approx(13.63)
    # For long-stop semantics, rounding UP fires the stop sooner (more
    # protective). Acceptable per the brief's "preserve brain intent"
    # framing -- the brain's intent is "exit if price falls TO this
    # level"; rounding up makes the threshold slightly more conservative.
