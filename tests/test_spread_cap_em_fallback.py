"""FIX A — adaptive spread-cap ROBUSTNESS (the stale_bbo / wide_bbo #1-killer fix).

The live spread cap scales with the name's expected move (win-win). The DEFECT:
on a cold/thin 15m frame (exactly the low-float names Ross trades pre-momentum)
``_expected_move_bps_from_ohlcv`` returns None, so the cap COLLAPSES to the 12bps
mega-cap floor and blocks the mover. FIX A derives a CONSERVATIVE name-own-data
fallback so the cap scales instead of collapsing.

These tests pin the WIN-WIN INVARIANT (a toxic wide spread on a genuinely
small-move name STILL blocks: fallback is an under-estimate + abs_cap hard-caps)
and PARITY-OFF (flag False => collapse-to-floor, byte-identical)."""

from __future__ import annotations

import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural import live_runner


def _cold_frame() -> pd.DataFrame:
    """A 2-bar frame: too thin for the primary 5-bar ATR (returns None) but enough
    for the relaxed fallback. Realized range ~ a few % on a ~$3 low-float."""
    return pd.DataFrame(
        {
            "High": [3.10, 3.30],
            "Low": [2.95, 3.05],
            "Close": [3.00, 3.20],
            "Open": [3.00, 3.05],
            "Volume": [100000, 250000],
        }
    )


def test_primary_em_returns_none_on_thin_frame() -> None:
    # Confirms the DEFECT precondition: the primary expected-move is None on <5 bars.
    assert live_runner._expected_move_bps_from_ohlcv(_cold_frame()) is None


def test_fallback_derives_conservative_em_from_thin_frame() -> None:
    em = live_runner._conservative_em_fallback_bps(_cold_frame(), price=3.20)
    assert em is not None and em > 0
    # Conservative: the shrunk fallback must be < the UNSHRUNK realized-range estimate
    # (we never loosen the cap more than a confident full-frame read would).
    high = _cold_frame()["High"]
    low = _cold_frame()["Low"]
    close = _cold_frame()["Close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1).dropna()
    unshrunk = (float(tr.mean()) / float(close.iloc[-1])) * 10_000.0
    assert em < unshrunk


def test_price_tier_floor_when_no_candles() -> None:
    # No usable candles -> price-tier floor. A sub-$5 low-float gets the full tier;
    # a liquid high-priced name gets none (no free loosening).
    low_px = live_runner._conservative_em_fallback_bps(None, price=2.50)
    hi_px = live_runner._conservative_em_fallback_bps(None, price=50.0)
    assert low_px is not None and low_px > 0
    assert hi_px is None  # >= $20 => zero tier => no fallback


def test_cap_scales_with_fallback_instead_of_collapsing() -> None:
    # Flag ON: the cold-frame cap is LIFTED above the 12bps collapse-floor.
    fallback = live_runner._conservative_em_fallback_bps(_cold_frame(), price=3.20)
    cap_on = live_runner._adaptive_live_max_spread_bps(None, fallback_em_bps=fallback)
    collapse_floor = live_runner._adaptive_live_max_spread_bps(None)
    assert collapse_floor == float(settings.chili_momentum_risk_max_spread_bps_live)
    assert cap_on > collapse_floor


def test_parity_off_is_byte_identical_collapse() -> None:
    # Flag OFF => the cap collapses to the floor exactly as before, even with a fallback.
    orig = settings.chili_momentum_spread_cap_em_fallback_enabled
    try:
        settings.chili_momentum_spread_cap_em_fallback_enabled = False
        fallback = live_runner._conservative_em_fallback_bps(_cold_frame(), price=3.20)
        cap_off = live_runner._adaptive_live_max_spread_bps(None, fallback_em_bps=fallback)
        assert cap_off == live_runner._adaptive_live_max_spread_bps(None)
        assert cap_off == float(settings.chili_momentum_risk_max_spread_bps_live)
    finally:
        settings.chili_momentum_spread_cap_em_fallback_enabled = orig


def test_win_win_invariant_small_move_name_still_blocks_toxic_spread() -> None:
    """A genuinely small-move name (tiny realized range, e.g. a quiet ~$40 large-cap)
    must NOT have its cap loosened enough to tolerate a toxic wide spread."""
    quiet = pd.DataFrame(
        {  # ~0.1% bars on a $40 name => tiny expected move
            "High": [40.04, 40.05],
            "Low": [39.97, 39.98],
            "Close": [40.00, 40.01],
            "Open": [40.00, 40.00],
            "Volume": [500000, 480000],
        }
    )
    fallback = live_runner._conservative_em_fallback_bps(quiet, price=40.01)
    cap = live_runner._adaptive_live_max_spread_bps(None, fallback_em_bps=fallback)
    # A toxic ~3% (300bps) round-trip spread must still exceed the cap (i.e. still block).
    toxic_spread_bps = 300.0
    assert toxic_spread_bps > cap, (
        f"win-win VIOLATED: cap {cap:.1f}bps would admit a {toxic_spread_bps}bps "
        "toxic spread on a small-move name"
    )


def test_abs_cap_hard_caps_the_fallback() -> None:
    # Even a huge fallback EM can never push the cap past the abs_cap (Ross's hard
    # "spread too wide -> skip" backstop survives the fallback).
    huge = live_runner._adaptive_live_max_spread_bps(None, fallback_em_bps=100_000.0)
    assert huge <= float(settings.chili_momentum_risk_max_spread_bps_abs_cap)
