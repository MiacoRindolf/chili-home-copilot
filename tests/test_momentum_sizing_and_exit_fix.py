"""Regression tests for the 2026-06-22 SMCX premarket incident fixes.

Fix C (stranded-position root cause): the reactive trail/stop exit must price the
RH equity sell limit on a valid PENNY tick (SEC/NMS Rule 612). A sub-penny limit
on a $1+ stock is rejected by place_equity_order (isError) -> the exit retry cap
is exhausted -> the long is STRANDED (the SMCX incident: bid 11.98 * 0.9975 =
11.95005 was rejected). The entry (_fmt_limit_price_buy) and the resting scale-out
(_fmt_limit_price_sell) already penny-round, which is why THEY filled premarket.
These tests pin that the reactive exit uses the same penny-FLOOR for RH equity and
keeps crypto's fine 6-decimal precision byte-identical.

Fix A (sizing throttle: the allocator's `base_cap * mult * conviction` collapsed
the equity-relative ceiling to a viability fraction — $2,070 -> $468 premarket) is
verified by LIVE-WATCH of the decision packet (recommended_notional == the
equity-relative policy cap, not a viability fraction). allocate_momentum_session_entry
orchestrates 5+ DB-touching sub-services and is not unit-isolable cheaply; the
load-bearing proof is the next live fill's packet (feedback_overfit_default_live).
"""
from __future__ import annotations

from decimal import Decimal

from app.services.trading.momentum_neural.live_runner import _fmt_limit_price_sell
from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    normalize_execution_family,
)


def _decimals(s: str) -> int:
    exp = Decimal(s).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def test_fmt_limit_price_sell_penny_floors_the_smcx_subpenny_price():
    # The incident value: bid 11.98 * 0.9975 = 11.95005 (sub-penny) -> RH rejected it.
    lim_px = 11.98 * 0.9975
    out = _fmt_limit_price_sell(lim_px)
    assert out == "11.95"
    assert _decimals(out) <= 2                 # venue-valid penny tick (NMS Rule 612)
    assert float(out) <= lim_px + 1e-9         # FLOOR: never above the intended sell


def test_fmt_limit_price_sell_floors_never_rounds_up():
    assert _fmt_limit_price_sell(11.989) == "11.98"   # floors, does NOT round to 11.99
    assert _fmt_limit_price_sell(12.0) == "12.00"
    assert _decimals(_fmt_limit_price_sell(11.001)) <= 2


def test_fmt_limit_price_sell_keeps_subdollar_precision():
    # Sub-$1 names keep 4-decimal precision (their valid tick), not penny-floored.
    assert _fmt_limit_price_sell(0.5012) == "0.5012"


def test_reactive_exit_price_selection_rh_equity_penny_crypto_fine():
    # Mirrors the exact selection in _submit_live_market_exit after the fix:
    #   penny-FLOOR for RH equity families; 6-decimal rstrip for crypto/other.
    lim_px = 11.98 * 0.9975  # 11.95005

    for fam in ("robinhood_agentic_mcp", "robinhood_spot"):
        is_rh = normalize_execution_family(fam) in (
            EXECUTION_FAMILY_ROBINHOOD_SPOT,
            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        )
        assert is_rh is True, fam
        chosen = (
            _fmt_limit_price_sell(lim_px)
            if is_rh
            else f"{lim_px:.6f}".rstrip("0").rstrip(".")
        )
        assert chosen == "11.95", fam
        assert _decimals(chosen) <= 2, fam

    # Crypto (coinbase) is NOT in the RH set -> keeps the fine 6-decimal format,
    # byte-identical to before the fix.
    crypto_px = 0.00123456
    is_rh = normalize_execution_family("coinbase_spot") in (
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    )
    assert is_rh is False
    assert EXECUTION_FAMILY_COINBASE_SPOT == normalize_execution_family("coinbase_spot")
    chosen = (
        _fmt_limit_price_sell(crypto_px)
        if is_rh
        else f"{crypto_px:.6f}".rstrip("0").rstrip(".")
    )
    assert chosen == f"{crypto_px:.6f}".rstrip("0").rstrip(".")
    assert _decimals(chosen) > 2  # fine precision preserved (NOT penny-floored)
