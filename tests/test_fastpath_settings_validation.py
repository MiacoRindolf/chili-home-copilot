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
    DEFAULT_UNIVERSE_LEARNING_RETENTION_HORIZON_S,
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


def test_cost_aware_live_fee_default_is_off_for_unit_determinism():
    assert FastPathSettings().cost_aware_live_fee_enabled is False


def test_cost_aware_live_fee_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_COST_AWARE_LIVE_FEE_ENABLED": "true"},
    ):
        loaded = load()
    assert loaded.cost_aware_live_fee_enabled is True


def test_bool_loader_tolerates_inline_operator_note():
    """A malformed inline note after a bool should not disable a gate."""
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED": "1 until soak completes"},
    ):
        loaded = load()
    assert loaded.cost_aware_admission_enabled is True


def test_universe_empty_fallback_defaults_off():
    """Empty rotator output should be visible instead of hidden by stale pairs."""
    assert FastPathSettings().universe_empty_fallback_enabled is False


def test_fast_path_pairs_default_empty_until_operator_configures():
    """No baked-in static coin list: rotation owns symbol selection."""
    assert FastPathSettings().pairs == []


def test_fast_path_pairs_loader_default_matches_dataclass():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHILI_FAST_PATH_PAIRS", None)
        loaded = load()
    assert loaded.pairs == FastPathSettings().pairs


def test_fast_path_pairs_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_PAIRS": "zec-usd; inj-usd, pendle-usd"},
    ):
        loaded = load()
    assert loaded.pairs == ["ZEC-USD", "INJ-USD", "PENDLE-USD"]


def test_universe_empty_fallback_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_EMPTY_FALLBACK_ENABLED": "true"},
    ):
        loaded = load()
    assert loaded.universe_empty_fallback_enabled is True


def test_universe_shadow_paper_fills_default_observe_only():
    assert FastPathSettings().universe_shadow_paper_fills_enabled is False


def test_universe_shadow_paper_fills_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_SHADOW_PAPER_FILLS_ENABLED": "true"},
    ):
        loaded = load()
    assert loaded.universe_shadow_paper_fills_enabled is True


def test_universe_shadow_terminal_reprobe_default_off():
    assert FastPathSettings().universe_shadow_terminal_reprobe_enabled is False


def test_universe_shadow_terminal_reprobe_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_SHADOW_TERMINAL_REPROBE_ENABLED": "true"},
    ):
        loaded = load()
    assert loaded.universe_shadow_terminal_reprobe_enabled is True


def test_universe_shadow_capacity_probe_default_off():
    assert FastPathSettings().universe_shadow_capacity_probe_enabled is False


def test_universe_shadow_capacity_probe_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_SHADOW_CAPACITY_PROBE_ENABLED": "true"},
    ):
        loaded = load()
    assert loaded.universe_shadow_capacity_probe_enabled is True


def test_negative_edge_filter_ttl_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_NEGATIVE_EDGE_FILTER_TTL_S": "45"},
    ):
        loaded = load()
    assert loaded.negative_edge_filter_ttl_s == 45


def test_maker_attempt_adverse_filter_env_overrides_work():
    with mock.patch.dict(
        os.environ,
        {
            "CHILI_FAST_PATH_MAKER_ATTEMPT_ADVERSE_FILTER_ENABLED": "false",
            "CHILI_FAST_PATH_MAKER_ATTEMPT_ADVERSE_FILTER_WINDOW_H": "12",
        },
    ):
        loaded = load()
    assert loaded.maker_attempt_adverse_filter_enabled is False
    assert loaded.maker_attempt_adverse_filter_window_h == 12


def test_universe_shadow_min_top_book_defaults_to_exec_notional():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_EXEC_NOTIONAL_USD": "37.5"},
    ):
        loaded = load()
    assert loaded.universe_shadow_min_top_of_book_usd == 37.5


def test_universe_shadow_min_top_book_env_override_works():
    with mock.patch.dict(
        os.environ,
        {
            "CHILI_FAST_PATH_EXEC_NOTIONAL_USD": "37.5",
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_MIN_TOP_OF_BOOK_USD": "125",
        },
    ):
        loaded = load()
    assert loaded.universe_shadow_min_top_of_book_usd == 125.0


def test_universe_min_range_24h_bps_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_MIN_RANGE_24H_BPS": "225"},
    ):
        loaded = load()
    assert loaded.universe_min_range_24h_bps == 225.0


def test_universe_spread_default_follows_executor_spread_cap():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHILI_FAST_PATH_UNIVERSE_MAX_SPREAD_BPS", None)
        os.environ["CHILI_FAST_PATH_EXEC_MAX_SPREAD_BPS"] = "6.5"
        loaded = load()
    assert loaded.universe_max_spread_bps == 6.5


def test_universe_adaptive_range_floor_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_ADAPTIVE_RANGE_FLOOR_ENABLED": "false"},
    ):
        loaded = load()
    assert loaded.universe_adaptive_range_floor_enabled is False


def test_universe_missing_grace_passes_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_MISSING_GRACE_PASSES": "3"},
    ):
        loaded = load()
    assert loaded.universe_missing_grace_passes == 3


def test_universe_shadow_exploration_floor_defaults_to_hysteresis():
    with mock.patch.dict(
        os.environ,
        {
            "CHILI_FAST_PATH_UNIVERSE_HYSTERESIS_RANKS": "7",
        },
    ):
        os.environ.pop("CHILI_FAST_PATH_UNIVERSE_MIN_SHADOW_EXPLORATION_N", None)
        loaded = load()
    assert loaded.universe_min_shadow_exploration_n == 7


def test_universe_shadow_exploration_floor_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_MIN_SHADOW_EXPLORATION_N": "0"},
    ):
        loaded = load()
    assert loaded.universe_min_shadow_exploration_n == 0


def test_universe_learning_retention_defaults_to_short_horizon_and_floor():
    with mock.patch.dict(
        os.environ,
        {
            "CHILI_FAST_PATH_UNIVERSE_HYSTERESIS_RANKS": "5",
        },
    ):
        os.environ.pop("CHILI_FAST_PATH_UNIVERSE_MIN_SHADOW_EXPLORATION_N", None)
        os.environ.pop("CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_MAX_N", None)
        loaded = load()
    assert loaded.universe_learning_retention_horizon_s == (
        DEFAULT_UNIVERSE_LEARNING_RETENTION_HORIZON_S
    )
    assert loaded.universe_min_shadow_exploration_n == 5
    assert loaded.universe_learning_retention_max_n == 5


def test_universe_learning_retention_env_overrides_work():
    with mock.patch.dict(
        os.environ,
        {
            "CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_HORIZON_S": "120",
            "CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_MAX_N": "2",
        },
    ):
        loaded = load()
    assert loaded.universe_learning_retention_horizon_s == 120
    assert loaded.universe_learning_retention_max_n == 2


def test_universe_snapshot_fetch_concurrency_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_SNAPSHOT_FETCH_CONCURRENCY": "6"},
    ):
        loaded = load()
    assert loaded.universe_snapshot_fetch_concurrency == 6


def test_universe_rest_request_pacing_env_override_works():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_UNIVERSE_REST_REQUEST_PACING_S": "0.2"},
    ):
        loaded = load()
    assert loaded.universe_rest_request_pacing_s == 0.2


def test_scanner_threshold_env_overrides_work():
    with mock.patch.dict(
        os.environ,
        {
            "CHILI_FAST_PATH_SCANNER_VOL_BREAKOUT_LOOKBACK": "12",
            "CHILI_FAST_PATH_SCANNER_VOL_BREAKOUT_MULT": "2.5",
            "CHILI_FAST_PATH_SCANNER_IMBALANCE_LONG_THRESHOLD": "0.72",
            "CHILI_FAST_PATH_SCANNER_IMBALANCE_SHORT_THRESHOLD": "0.28",
            "CHILI_FAST_PATH_SCANNER_IMBALANCE_COOLDOWN_S": "17",
            "CHILI_FAST_PATH_SCANNER_SPREAD_SQUEEZE_BPS": "2.25",
            "CHILI_FAST_PATH_SCANNER_SPREAD_SQUEEZE_VOL_MULT": "1.4",
            "CHILI_FAST_PATH_SCANNER_SPREAD_SQUEEZE_COOLDOWN_S": "45",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_ENABLED": "false",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_WINDOW": "6",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_AVG_IMBALANCE": "0.7",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_MICROPRICE_BPS": "0.4",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MAX_SPREAD_BPS": "2.75",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_MID_MOVE_BPS": "0.35",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_COOLDOWN_S": "29",
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_TOUCH_NOTIONAL_USD": "18.5",
            "CHILI_FAST_PATH_SCANNER_MAX_PENDING_DEFERRED": "42",
        },
    ):
        loaded = load()
    assert loaded.scanner_vol_breakout_lookback == 12
    assert loaded.scanner_vol_breakout_mult == 2.5
    assert loaded.scanner_imbalance_long_threshold == 0.72
    assert loaded.scanner_imbalance_short_threshold == 0.28
    assert loaded.scanner_imbalance_cooldown_s == 17.0
    assert loaded.scanner_spread_squeeze_bps == 2.25
    assert loaded.scanner_spread_squeeze_vol_mult == 1.4
    assert loaded.scanner_spread_squeeze_cooldown_s == 45.0
    assert loaded.scanner_book_pressure_enabled is False
    assert loaded.scanner_book_pressure_window == 6
    assert loaded.scanner_book_pressure_min_avg_imbalance == 0.7
    assert loaded.scanner_book_pressure_min_microprice_bps == 0.4
    assert loaded.scanner_book_pressure_max_spread_bps == 2.75
    assert loaded.scanner_book_pressure_min_mid_move_bps == 0.35
    assert loaded.scanner_book_pressure_cooldown_s == 29.0
    assert loaded.scanner_book_pressure_min_touch_notional_usd == 18.5
    assert loaded.scanner_max_pending_deferred == 42


def test_scanner_book_pressure_touch_notional_defaults_to_exec_notional():
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_EXEC_NOTIONAL_USD": "37.5"},
    ):
        loaded = load()
    assert loaded.scanner_book_pressure_min_touch_notional_usd == 37.5


@pytest.mark.parametrize("raw", ["0", "-1", "bad"])
def test_scanner_max_pending_deferred_rejects_invalid_env(raw):
    with mock.patch.dict(
        os.environ,
        {"CHILI_FAST_PATH_SCANNER_MAX_PENDING_DEFERRED": raw},
    ):
        loaded = load()
    assert loaded.scanner_max_pending_deferred == 1000


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
