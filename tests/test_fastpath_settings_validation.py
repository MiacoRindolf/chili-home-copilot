"""Tests for fast-path settings defaults and env-override validation.

Specifically guards against the class of bug discovered 2026-05-07
where ``cost_aware_taker_fee_bps`` defaulted to 5.0 (volume tier ~6+)
when the operator's account is on retail tier 1 (60 bps). A wrong fee
default silently mis-calibrates ``gate_cost_aware_admission``.

Helper-level tests. No DB. No broker. Fast.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

from app.services.trading.fast_path.settings import (
    FastPathSettings,
    load,
)


# ---------------------------------------------------------------------------
# cost_aware_taker_fee_bps default + override
# ---------------------------------------------------------------------------

def test_cost_aware_taker_fee_bps_default_is_retail_tier_1():
    """Dataclass default must equal Coinbase Advanced Trade retail
    tier 1 taker fee (60 bps per-side).

    History: 2026-05-07 review of f-fastpath-universe-rotation found
    the default was 5.0 (volume tier ~6+, ≥$75M 30d volume) which is
    not the operator's account. The cost-aware gate uses this value
    as part of ``2 * (taker_fee + spread)`` so the wrong default
    silently mis-calibrates the gate by ~110 bps.
    """
    s = FastPathSettings()
    assert s.cost_aware_taker_fee_bps == 60.0, (
        "default must be 60 bps (Coinbase retail tier 1 taker, per-side); "
        f"got {s.cost_aware_taker_fee_bps}"
    )


def test_cost_aware_taker_fee_bps_loader_default_matches_dataclass():
    """The env loader's default must agree with the dataclass default.

    Catches drift between the two defaults — they live in different
    code locations and can de-sync silently.
    """
    # Clear any env var that might mask the default
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS", None)
        loaded = load()
    expected = FastPathSettings().cost_aware_taker_fee_bps
    assert loaded.cost_aware_taker_fee_bps == expected, (
        f"loader default {loaded.cost_aware_taker_fee_bps} != "
        f"dataclass default {expected}"
    )


def test_cost_aware_taker_fee_bps_env_override_works():
    """Operator must be able to override via env var. Higher-volume
    tier (e.g., tier 4 = 15 bps) is the realistic override case."""
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS": "15.0"},
    ):
        loaded = load()
    assert loaded.cost_aware_taker_fee_bps == 15.0


def test_cost_aware_taker_fee_bps_in_plausible_range():
    """The default value must be in a plausible bps range for any
    Coinbase volume tier.

    Lower bound 1.0 = below any plausible tier (tier 8 = 4 bps, tier
    9 = 4 bps); below this is almost certainly a typo (e.g. someone
    wrote 0.05 thinking percent, not bps).

    Upper bound 200.0 = above tier 0 ceiling (120 bps); above this
    suggests someone wrote percent (60 = 6000 bps under that
    interpretation) or confused round-trip with per-side.

    This is a smell test — not a hard validation. The loader does NOT
    enforce these bounds at runtime (per-knob asserts at boot would
    block legitimate Coinbase One / promo-tier overrides). The test
    runs at CI time as a regression guard.
    """
    s = FastPathSettings()
    LOWER = 1.0   # below tier 9 floor
    UPPER = 200.0  # above tier 0 ceiling, with margin
    assert LOWER <= s.cost_aware_taker_fee_bps <= UPPER, (
        f"cost_aware_taker_fee_bps default {s.cost_aware_taker_fee_bps} "
        f"is outside plausible range [{LOWER}, {UPPER}] bps; check "
        f"whether someone swapped per-side <-> round-trip or bps <-> percent"
    )


# ---------------------------------------------------------------------------
# Sanity: other admission gate thresholds also in plausible ranges
# ---------------------------------------------------------------------------

def test_universe_admission_thresholds_in_plausible_ranges():
    """Sister regression guard: the four universe-admission knobs
    have sensible defaults. If anyone flips one to a percent-vs-bps or
    per-side-vs-round-trip mistake, this test catches it before the
    rotator silently admits the wrong pairs.
    """
    s = FastPathSettings()
    # 24h volume threshold: $1M < x < $1B (tighten to $10M is the
    # current default; either side of that range is suspicious)
    assert 1_000_000.0 <= s.universe_min_volume_24h_usd <= 1_000_000_000.0
    # Spread cap: 1 bps < x < 100 bps (10 bps default)
    assert 1.0 <= s.universe_max_spread_bps <= 100.0
    # Top-of-book size: $100 < x < $1M ($5k default)
    assert 100.0 <= s.universe_min_top_of_book_usd <= 1_000_000.0
    # Trades per 24h: 10 < x < 1M (1k default)
    assert 10 <= s.universe_min_trades_24h <= 1_000_000
