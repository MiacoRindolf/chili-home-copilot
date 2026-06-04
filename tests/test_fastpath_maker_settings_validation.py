"""f-fastpath-maker-only settings-validation tests.

Pins the maker fast-path settings introduced for maker-only mode:

  * ``execution_mode`` defaults to ``"taker"`` (bit-identical to today).
  * ``cost_aware_maker_fee_bps`` defaults to ``40.0`` (Coinbase Advanced
    Trade retail tier 1 maker, per-side, in bps).
  * ``maker_cancel_on_timeout_s`` defaults to ``10`` seconds.
  * ``maker_first_taker_fallback_s`` defaults to ``5`` seconds.
  * ``maker_tick_fraction_of_mid`` defaults to ``1e-4`` (1bp of mid).

The brief explicitly calls out
``test_cost_aware_maker_fee_bps_default_is_retail_tier_1`` as a hard
acceptance check (preventing the same defect class as today's earlier
fee-fix where the default was a wrong value). Cowork's review verifies
this test name + assertion.

All tests are sub-millisecond -- pure dataclass + env-loader checks.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from app.services.trading.fast_path.settings import FastPathSettings, load


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _env_clean(*names):
    """Clear env vars for the duration of the test then restore."""
    saved = {n: os.environ.pop(n, None) for n in names}
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


# ---------------------------------------------------------------------------
# Default-value pins (the brief's explicit acceptance criterion)
# ---------------------------------------------------------------------------

def test_cost_aware_maker_fee_bps_default_is_retail_tier_1():
    """f-fastpath-maker-only acceptance criterion: default must be
    exactly ``40.0`` bps (Coinbase Advanced Trade retail tier 1 maker
    fee, per-side). The brief explicitly calls out this assertion to
    prevent a recurrence of the defect class where today's earlier
    ``cost_aware_taker_fee_bps`` shipped with the wrong default."""
    s = FastPathSettings()
    assert s.cost_aware_maker_fee_bps == 40.0, (
        f"cost_aware_maker_fee_bps default must be 40.0 (Coinbase "
        f"retail tier 1 maker, per-side); got {s.cost_aware_maker_fee_bps}"
    )


def test_execution_mode_default_is_taker():
    """Default must be 'taker' so behaviour at switchover is bit-
    identical to today. Maker modes are explicitly opt-in via env."""
    s = FastPathSettings()
    assert s.execution_mode == "taker"


def test_maker_cancel_on_timeout_s_default():
    s = FastPathSettings()
    assert s.maker_cancel_on_timeout_s == 10


def test_maker_first_taker_fallback_s_default():
    s = FastPathSettings()
    assert s.maker_first_taker_fallback_s == 5


def test_maker_tick_fraction_default_is_one_bp():
    s = FastPathSettings()
    assert s.maker_tick_fraction_of_mid == pytest.approx(1e-4)


# ---------------------------------------------------------------------------
# Plausible-range pins (defensive against typos that would silently
# drop the magnitude by 10x)
# ---------------------------------------------------------------------------

def test_cost_aware_maker_fee_bps_within_plausible_range():
    """Coinbase volume tiers run from 40 bps (tier 1) down to 0 bps
    (rebate-eligible, tiers 7-9). Anything outside [1, 100] is a typo."""
    s = FastPathSettings()
    assert 1.0 <= s.cost_aware_maker_fee_bps <= 100.0


# ---------------------------------------------------------------------------
# Env-override path
# ---------------------------------------------------------------------------

def test_load_overrides_execution_mode_from_env():
    with _env_clean("CHILI_FAST_PATH_EXECUTION_MODE"):
        os.environ["CHILI_FAST_PATH_EXECUTION_MODE"] = "maker_only"
        s = load()
    assert s.execution_mode == "maker_only"


def test_load_overrides_maker_fee_from_env():
    with _env_clean("CHILI_FAST_PATH_COST_AWARE_MAKER_FEE_BPS"):
        os.environ["CHILI_FAST_PATH_COST_AWARE_MAKER_FEE_BPS"] = "8.0"
        s = load()
    assert s.cost_aware_maker_fee_bps == 8.0


def test_load_overrides_cancel_timeout_from_env():
    with _env_clean("CHILI_FAST_PATH_MAKER_CANCEL_ON_TIMEOUT_S"):
        os.environ["CHILI_FAST_PATH_MAKER_CANCEL_ON_TIMEOUT_S"] = "3"
        s = load()
    assert s.maker_cancel_on_timeout_s == 3


def test_load_overrides_fallback_seconds_from_env():
    with _env_clean("CHILI_FAST_PATH_MAKER_FIRST_TAKER_FALLBACK_S"):
        os.environ["CHILI_FAST_PATH_MAKER_FIRST_TAKER_FALLBACK_S"] = "2"
        s = load()
    assert s.maker_first_taker_fallback_s == 2


def test_load_overrides_maker_tick_fraction_from_env():
    with _env_clean("CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID"):
        os.environ["CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID"] = "0.0002"
        s = load()
    assert s.maker_tick_fraction_of_mid == pytest.approx(0.0002)


@pytest.mark.parametrize("raw", ["0", "-0.1", "nan", "0.02", "bad"])
def test_load_rejects_implausible_maker_tick_fraction(raw):
    with _env_clean("CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID"):
        os.environ["CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID"] = raw
        s = load()
    assert s.maker_tick_fraction_of_mid == pytest.approx(1e-4)


def test_load_invalid_execution_mode_string_defaults_to_taker():
    """Unsupported execution modes must not leak into downstream gates."""
    with _env_clean("CHILI_FAST_PATH_EXECUTION_MODE"):
        os.environ["CHILI_FAST_PATH_EXECUTION_MODE"] = "BIZARRE_MODE"
        s = load()
    assert s.execution_mode == "taker"
