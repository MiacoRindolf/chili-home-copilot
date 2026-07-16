from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings, settings
from app.services.trading.momentum_neural.feature_flags import (
    audit_momentum_settings_fallbacks,
    build_momentum_feature_flag_readiness,
    iter_momentum_feature_flags,
)
from app.services.trading.momentum_neural.operator_readiness import build_momentum_operator_readiness


ROOT = Path(__file__).resolve().parents[1]
MOMENTUM_DIR = ROOT / "app" / "services" / "trading" / "momentum_neural"
APP_DIR = ROOT / "app"
CERT_REPORT = (
    ROOT
    / "docs"
    / "STRATEGY"
    / "CC_REPORTS"
    / "2026-07-01_ross-warrior-playbook-certification.md"
)
SETUP_AUDIT_REPORT = (
    ROOT
    / "docs"
    / "STRATEGY"
    / "CC_REPORTS"
    / "2026-07-01_ross-setup-end-to-end-audit.md"
)
COMPOSE_FILE = ROOT / "docker-compose.yml"
ENV_FILE = ROOT / ".env"

MOMENTUM_EXEC_LAUNCHER_ENV_PREFIX = "CHILI_MOMENTUM_EXEC_"

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSEY_ENV_VALUES = {"0", "false", "no", "off"}


def _read_env_assignments(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        env[name.strip()] = value.strip().strip("'\"")
    return env


def _fallback_false_momentum_flags() -> set[str]:
    pattern = re.compile(
        r'getattr\(\s*(?:self\.)?settings(?:_obj)?\s*,\s*"([^"]+)"\s*,\s*False\s*\)'
    )
    flags: set[str] = set()
    for path in MOMENTUM_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            flag = match.group(1)
            if flag.startswith("chili_momentum_") and flag.endswith("_enabled"):
                flags.add(flag)
    return flags


def _fallback_false_momentum_controls() -> set[str]:
    pattern = re.compile(r'getattr\([^)]*"([^"]+)"\s*,\s*False\s*\)')
    controls: set[str] = set()
    for path in MOMENTUM_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            name = match.group(1)
            if name.startswith("chili_momentum_"):
                controls.add(name)
    return controls


def _app_getattr_momentum_enabled_controls() -> set[str]:
    pattern = re.compile(r'getattr\([^)]*"([^"]+_enabled)"\s*,')
    controls: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            name = match.group(1)
            if name.startswith("chili_momentum_"):
                controls.add(name)
    return controls


def test_no_fallback_false_momentum_flag_is_unregistered() -> None:
    registered = {spec.flag for spec in iter_momentum_feature_flags()}
    missing = sorted(_fallback_false_momentum_flags() - registered)
    assert missing == []


def test_no_app_momentum_enabled_getattr_is_unregistered() -> None:
    registered = {spec.flag for spec in iter_momentum_feature_flags()}
    missing = sorted(_app_getattr_momentum_enabled_controls() - registered)
    assert missing == []


def test_no_fallback_false_momentum_control_is_absent_from_settings() -> None:
    fields = set(Settings.model_fields)
    missing = sorted(_fallback_false_momentum_controls() - fields)
    assert missing == []


def test_no_app_momentum_enabled_getattr_is_absent_from_settings() -> None:
    fields = set(Settings.model_fields)
    missing = sorted(_app_getattr_momentum_enabled_controls() - fields)
    assert missing == []


def test_momentum_env_names_are_settings_or_explicit_launcher_proxy() -> None:
    """Do not confuse compose-only exec launcher vars with app feature flags.

    ``CHILI_MOMENTUM_EXEC_*`` values are consumed by docker-compose variable
    substitution and translated into real ``CHILI_MOMENTUM_*`` settings inside
    the live execution worker. Every non-proxy momentum env name must remain a
    first-class Settings field so handlers cannot silently disappear.
    """

    text = COMPOSE_FILE.read_text(encoding="utf-8", errors="ignore")
    if ENV_FILE.exists():
        text += "\n" + ENV_FILE.read_text(encoding="utf-8", errors="ignore")
    env_names = set(re.findall(r"CHILI_MOMENTUM_[A-Z0-9_]+", text))
    fields = {name.upper() for name in Settings.model_fields}
    missing = sorted(
        name
        for name in env_names
        if not name.startswith(MOMENTUM_EXEC_LAUNCHER_ENV_PREFIX)
        and name not in fields
    )

    assert missing == []


def test_momentum_exec_launcher_env_cannot_silently_disable_live_lane() -> None:
    """The canonical momentum worker may use launcher-only compose proxies.

    Those proxies must not recreate the July 2026 placeholder/regression class:
    live runner off, event loop off, generic scheduler path on, or Ross universe
    boundary off. Generic web/app defaults are allowed to be conservative; this
    test only audits explicit ``CHILI_MOMENTUM_EXEC_*`` launcher overrides.
    """

    env = _read_env_assignments(ENV_FILE)

    required_truthy_if_present = {
        "CHILI_MOMENTUM_EXEC_LIVE_RUNNER_ENABLED",
        "CHILI_MOMENTUM_EXEC_LIVE_RUNNER_LOOP_ENABLED",
        "CHILI_MOMENTUM_EXEC_IQFEED_TAPE_ENABLED",
        "CHILI_MOMENTUM_EXEC_IQFEED_NOTIFY_ENABLED",
        "CHILI_MOMENTUM_EXEC_IQFEED_POLL_FALLBACK_ENABLED",
        "CHILI_MOMENTUM_EXEC_ROSS_EVENT_ADMISSION_ENABLED",
        "CHILI_MOMENTUM_EXEC_AUTO_ARM_LIVE_ENABLED",
        "CHILI_MOMENTUM_EXEC_ROSS_EQUITY_UNIVERSE_REQUIRED",
    }
    required_falsey_if_present = {
        "CHILI_MOMENTUM_EXEC_LIVE_RUNNER_SCHEDULER_ENABLED",
        "CHILI_MOMENTUM_EXEC_BATCH_FALLBACK_ENABLED",
        "CHILI_MOMENTUM_EXEC_AUTO_ARM_SCHEDULER_ENABLED",
        "CHILI_MOMENTUM_EXEC_AUTO_ARM_SCHEDULER_FALLBACK_ENABLED",
    }

    unsafe = []
    for name in sorted(required_truthy_if_present):
        value = env.get(name)
        if value is not None and value.lower() not in _TRUTHY_ENV_VALUES:
            unsafe.append(f"{name}={value}")
    for name in sorted(required_falsey_if_present):
        value = env.get(name)
        if value is not None and value.lower() not in _FALSEY_ENV_VALUES:
            unsafe.append(f"{name}={value}")

    image = env.get("CHILI_MOMENTUM_EXEC_IMAGE", "")
    if "placeholder" in image.lower():
        unsafe.append(f"CHILI_MOMENTUM_EXEC_IMAGE={image}")
    known_stale_exec_images = {
        "chili-app:main-clean-855f24c",
        "chili-app:main-clean-d718991",
    }
    if image in known_stale_exec_images:
        unsafe.append(
            f"CHILI_MOMENTUM_EXEC_IMAGE={image} "
            "missing restored OFI/outcome/catalyst + Ross universe guard contracts"
        )

    assert unsafe == []


def test_registered_momentum_feature_flags_are_first_class_settings() -> None:
    fields = set(Settings.model_fields)
    missing = sorted(spec.flag for spec in iter_momentum_feature_flags() if spec.flag not in fields)
    assert missing == []


def test_c1_quote_freshness_guard_knobs_are_first_class_settings() -> None:
    fields = set(Settings.model_fields)
    required = {
        "chili_momentum_quote_freshness_floor_seconds",
        "chili_momentum_max_loss_fresh_quote_guard_enabled",
        "chili_momentum_max_loss_phantom_divergence_spread_mult",
        "chili_momentum_max_loss_phantom_divergence_fallback_bps",
    }

    assert required <= fields


def test_high_risk_money_moving_momentum_knobs_are_first_class_settings() -> None:
    fields = set(Settings.model_fields)
    required = {
        "chili_momentum_quote_freshness_ceiling_seconds",
        "chili_momentum_quote_freshness_cadence_mult",
        "chili_momentum_max_loss_circuit_enabled",
        "chili_momentum_max_loss_risk_multiple",
        "chili_momentum_a_setup_size_floor_enabled",
        "chili_momentum_a_setup_size_floor_fraction",
        "chili_momentum_a_setup_size_floor_viability_min",
        "chili_momentum_a_setup_size_floor_frontside_min",
        "chili_momentum_a_setup_size_floor_entry_bar_margin",
        "chili_momentum_a_setup_notional_floor_enabled",
        "chili_momentum_a_setup_notional_floor_target_fraction",
        "chili_momentum_early_trail_arm_enabled",
        "chili_momentum_no_confirmation_min_hold_seconds",
        "chili_momentum_no_confirmation_window_seconds",
        "chili_momentum_no_confirmation_buffer_bps",
        "chili_momentum_pullback_add_enabled",
        "chili_momentum_pullback_add_max",
        "chili_momentum_pullback_add_cooldown_seconds",
        "chili_momentum_pullback_add_risk_fraction",
        "chili_momentum_pullback_add_strength_floor",
        "chili_momentum_pullback_add_depth_lo_frac",
        "chili_momentum_pullback_add_depth_hi_frac",
        "chili_momentum_micropullback_reentry_enabled",
        "chili_momentum_micropullback_reentry_max",
        "chili_momentum_micropullback_reentry_cooldown_seconds",
        "chili_momentum_micropullback_reentry_risk_fraction",
        "chili_momentum_micropullback_reentry_max_dip_pct",
        "chili_momentum_micropullback_reentry_ofi_thr",
        "chili_momentum_micropullback_reentry_trade_flow_thr",
        "chili_momentum_flag_breakout_add_enabled",
        "chili_momentum_flag_breakout_add_max",
        "chili_momentum_flag_breakout_add_cooldown_seconds",
        "chili_momentum_flag_breakout_add_risk_fraction",
        "chili_momentum_flag_breakout_add_strength_floor",
        "chili_momentum_flag_breakout_add_margin_frac",
        "chili_momentum_pyramid_max_adds",
        "chili_momentum_pyramid_min_cushion_r",
        "chili_momentum_pyramid_add_risk_fraction",
        "chili_momentum_pyramid_add_submit_retry_max",
        "chili_momentum_adaptive_pullback_depth_ceiling_enabled",
        "chili_momentum_entry_extension_floor_pct",
        "chili_momentum_entry_extension_atr_mult",
        "chili_momentum_round_number_overhead_band_fraction",
        "chili_momentum_cup_and_handle_lookback_bars",
        "chili_momentum_cup_and_handle_max_handle_bars",
        "chili_momentum_l2_snapshot_max_age_seconds",
        "chili_momentum_l2_distribution_window_snaps",
        "chili_momentum_l2_distribution_min_snaps",
        "chili_momentum_l2_level_match_spread_mult",
        "chili_momentum_l2_ask_eaten_pctile_ceiling",
        "chili_momentum_l2_bid_refill_pctile_floor",
        "chili_momentum_l2_target_print_window_seconds",
        "chili_momentum_trade_flow_window_seconds",
        "chili_momentum_trade_flow_tick_limit",
        "chili_momentum_flow_slope_window_seconds",
        "chili_momentum_flow_slope_snapshot_limit",
        "chili_momentum_realized_vol_window_seconds",
        "chili_momentum_realized_vol_tick_limit",
        "chili_momentum_realized_vol_min_ticks",
        "chili_momentum_big_buyer_bid_max_spread_bps",
        "chili_momentum_big_buyer_bid_pctile_ceiling",
        "chili_momentum_exit_submit_max_attempts",
        "chili_momentum_exit_submit_backoff_base_seconds",
        "chili_momentum_exit_submit_backoff_max_seconds",
        "chili_momentum_exit_limit_repeg_seconds",
        "chili_momentum_exit_cancel_covering_sells",
        "chili_momentum_eod_flatten_lead_min",
        "chili_momentum_bos_exit_live_enabled",
        "chili_momentum_bos_exit_buffer_pct",
        "chili_momentum_exit_adaptive_equity_enabled",
        "chili_momentum_exit_ladder_enabled",
        "chili_momentum_exit_ofi_lock_enabled",
        "chili_momentum_exit_candle_confirm_enabled",
        "chili_momentum_exit_tape_accel_reversal_enabled",
        "chili_momentum_cadence_aware_exit_enabled",
        "chili_momentum_velocity_persistence_exit_enabled",
        "chili_momentum_measured_move_exit_scale_fraction",
        "chili_momentum_measured_move_exit_double_top_atr_mult",
        "chili_momentum_sub5min_scalp_bailout_minutes",
        "chili_momentum_exit_ofi_arm_frac",
        "chili_momentum_exit_ofi_base_lock_bps",
        "chili_momentum_exit_accel_reversal_giveback_frac",
        "chili_momentum_squeeze_exit_tail_pctl",
        "chili_momentum_squeeze_exit_max_widen",
        "chili_momentum_overnight_dark_flatten_onset_ticks",
        "chili_momentum_catalyst_conviction_step",
        "chili_momentum_catalyst_conviction_max_multiplier",
        "chili_momentum_green_day_step_per_day",
        "chili_momentum_green_day_max_multiplier",
        "chili_momentum_green_day_lookback_days",
        "chili_momentum_consecutive_loss_halt_enabled",
        "chili_momentum_consecutive_loss_halt_count",
        "chili_momentum_crypto_reward_risk_ratio",
        "chili_momentum_daily_trade_count_budget_enabled",
        "chili_momentum_daily_trade_count_base",
        "chili_momentum_daily_trade_count_max_multiple",
        "chili_momentum_fatigue_derate_floor",
        "chili_momentum_fatigue_full_session_minutes",
        "chili_momentum_kelly_conviction_enabled",
        "chili_momentum_kelly_conviction_gain",
        "chili_momentum_kelly_conviction_max_multiplier",
        "chili_momentum_prior_day_pnl_damper_enabled",
        "chili_momentum_prior_day_damper_lookback_days",
        "chili_momentum_prior_day_damper_z_threshold",
        "chili_momentum_prior_day_damper_floor",
        "chili_momentum_prior_day_damper_slope",
        "chili_momentum_red_intraday_full_down_units",
        "chili_momentum_red_intraday_size_floor",
        "chili_momentum_red_intraday_size_down_enabled",
        "chili_momentum_meta_label_min_size",
        "chili_momentum_max_aggregate_risk_pct_of_equity",
        "chili_momentum_max_aggregate_crypto_risk_pct_of_equity",
        "chili_momentum_liquidity_risk_cap_enabled",
        "chili_momentum_liquidity_risk_floor",
        "chili_momentum_risk_liquidity_participation_fraction",
        "chili_momentum_run_r_breaker_enabled",
        "chili_momentum_run_r_breaker_viability_bump",
        "chili_momentum_run_r_breaker_lookback",
        "chili_momentum_run_r_breaker_short_window",
        "chili_momentum_run_r_breaker_min_history",
        "chili_momentum_entry_ask_heavy_size_fraction",
        "chili_momentum_entry_fast_poll_enabled",
        "chili_momentum_entry_fast_poll_seed_interval_s",
        "chili_momentum_entry_fast_poll_widen_factor",
        "chili_momentum_entry_fast_poll_max_iters",
        "chili_momentum_entry_fast_poll_max_wall_s",
        "chili_momentum_entry_chase_enabled",
        "chili_momentum_entry_chase_ceiling_bps",
        "chili_momentum_entry_chase_move_ratio",
        "chili_momentum_entry_guard_move_ratio",
        "chili_momentum_entry_flow_veto_ofi",
        "chili_momentum_entry_flow_veto_trade_flow",
        "chili_momentum_entry_inline_repeg_enabled",
        "chili_momentum_entry_inline_repeg_max_delay_s",
        "chili_momentum_entry_max_repegs",
        "chili_momentum_entry_max_rest_bars",
        "chili_momentum_entry_placement_governor_enabled",
        "chili_momentum_entry_quote_refetch_enabled",
        "chili_momentum_instant_bid_cut_window_seconds",
        "chili_momentum_instant_bid_confirm_window_seconds",
        "chili_momentum_instant_bid_cut_margin_bps",
        "chili_momentum_recycle_entry_state_reset_enabled",
        "chili_momentum_require_live_atr_for_entry",
        "chili_momentum_skip_spread_gate_for_limit_entry",
        "chili_momentum_explosive_atr_pct_floor",
        "chili_momentum_explosive_rvol_floor",
        "chili_momentum_frontside_adaptive_enabled",
        "chili_momentum_frontside_defer_pctile",
        "chili_momentum_frontside_size_floor",
        "chili_momentum_smart_hold_k_atr",
        "chili_momentum_smart_hold_t_flow_floor",
        "chili_momentum_smart_hold_s_flow_floor",
        "chili_momentum_smart_hold_time_floor_min_samples",
        "chili_momentum_smart_hold_time_floor_q",
        "chili_momentum_smart_hold_rho",
        "chili_momentum_lost_vwap_flatten_enabled",
        "chili_momentum_lost_vwap_margin_sigma",
        "chili_momentum_breakout_bailout_lock_in_seconds",
        "chili_momentum_breakout_bailout_lock_in_explosive_seconds",
        "chili_momentum_exit_candle_confirm_use_macd",
        "chili_momentum_velocity_persist_frac",
        "chili_momentum_clean_decline_terminal_enabled",
        "chili_momentum_overnight_max_stale_sec",
        "chili_momentum_broker_zero_trust_clamp_enabled",
        "chili_momentum_broker_zero_confirm_reads",
        "chili_momentum_squeeze_entry_top_pctl",
        "chili_momentum_squeeze_entry_max_mult",
        "chili_momentum_spread_cost_derate_floor",
        "chili_momentum_spread_cost_max_fraction_of_r",
        "chili_momentum_spread_cost_reclaim_max_fraction_of_r",
        "chili_momentum_spread_anomaly_p50_mult",
        "chili_momentum_spread_anomaly_extreme_p90_mult",
        "chili_momentum_spread_norm_lookback_days",
        "chili_momentum_spread_cost_derate_engage_frac",
        "chili_momentum_spread_cap_em_fallback_enabled",
        "chili_momentum_spread_cap_em_fallback_shrink",
        "chili_momentum_spread_cap_em_fallback_price_tier_bps",
        "chili_momentum_spread_stability_window_bars",
        "chili_momentum_spread_stability_min_samples",
        "chili_momentum_ext_hours_quote_age_enabled",
        "chili_momentum_ext_hours_quote_ceiling_seconds",
        "chili_momentum_quote_block_diagnostics",
        "chili_momentum_bid_prop_spread_blowout_mult",
        "chili_momentum_nbbo_tape_enabled",
        "chili_momentum_nbbo_tape_retention_days",
        "chili_momentum_universe_tick_retention_days",
        "chili_momentum_universe_tick_record_enabled",
        "chili_momentum_running_up_lookback_min",
        "chili_momentum_running_up_min_pct",
        "chili_momentum_running_up_max_symbols",
        "chili_momentum_ignition_min_pct",
        "chili_momentum_event_select_primary_enabled",
        "chili_momentum_tape_delta_min_seconds",
        "chili_momentum_ross_rvol_feed_enabled",
        "chili_momentum_premarket_start_et",
        "chili_momentum_afterhours_end_et",
        "chili_momentum_early_premarket_enabled",
        "chili_momentum_selection_prep_lead_min",
        "chili_momentum_early_premarket_min_movers",
        "chili_momentum_early_premarket_window_min",
        "chili_momentum_live_eligible_max_spread_bps",
        "chili_momentum_thin_spread_hard_loss_fraction",
        "chili_momentum_thin_spread_squeeze_lane_enabled",
        "chili_momentum_squeeze_entry_sizeup_enabled",
        "chili_momentum_live_eligible_allow_extreme_explosive",
        "chili_momentum_meta_label_derate_enabled",
        "chili_momentum_live_capture_features",
        "chili_momentum_midday_viability_bump",
        "chili_momentum_opening_bell_suppression_enabled",
        "chili_momentum_opening_bell_suppress_base_min",
        "chili_momentum_order_burst_guard_window_minutes",
        "chili_momentum_bid_prop_confirmer_enabled",
        "chili_momentum_bid_prop_min_samples",
        "chili_momentum_bid_prop_max_samples",
        "chili_momentum_runaway_cross_enabled",
        "chili_momentum_runaway_cross_ask_band_bps",
        "chili_momentum_vertical_chase_enabled",
        "chili_momentum_vertical_chase_min_confluence",
        "chili_momentum_vertical_chase_max_bps",
        "chili_momentum_setup_selector_enabled",
        "chili_momentum_sticky_backside_bench_enabled",
        "chili_momentum_auto_arm_watch_extend_seconds",
        "chili_momentum_max_open_positions_per_correlation_bucket",
        "chili_momentum_news_catalyst_max_age_min",
        "chili_momentum_adaptive_scale_vol_ref_pct",
        "chili_momentum_adaptive_target_enabled",
        "chili_momentum_adaptive_target_rr_cap",
        "chili_momentum_adaptive_target_room_capture",
        "chili_momentum_adaptive_scale_vol_tilt",
        "chili_momentum_crypto_scale_out_fraction",
        "chili_momentum_scale_grid_r_multiples",
        "chili_momentum_scale_grid_fractions",
        "chili_momentum_hard_no_trade_event_window_min",
        "chili_momentum_hard_no_trade_event_times_utc",
        "chili_momentum_halt_stale_ticks",
        "chili_momentum_halt_resume_cooldown_seconds",
        "chili_momentum_halt_down_cascade_threshold",
        "chili_momentum_add_into_halt_swing_lookback",
        "chili_momentum_vertical_chase_nohalt_thrust_enabled",
        "chili_momentum_vertical_chase_nohalt_min_confluence",
        "chili_momentum_overnight_max_loss_pct_bp",
        "chili_momentum_overnight_size_fraction",
        "chili_momentum_extreme_vol_risk_bounded_fraction",
        "chili_momentum_reentry_after_stop_bound_enabled",
        "chili_momentum_max_stopout_reentries",
        "chili_momentum_adaptive_reentry_cooldown_enabled",
        "chili_momentum_reentry_profit_cooldown_factor",
        "chili_momentum_reentry_cooldown_vol_ref_atr_pct",
        "chili_momentum_reentry_cooldown_vol_span",
        "chili_momentum_pullback_retracement_threshold",
        "chili_momentum_ofi_threshold",
        "chili_momentum_iceberg_add_probe_enabled",
        "chili_momentum_iceberg_add_refill_ratio",
        "chili_momentum_exit_ladder_rung_bps",
        "chili_momentum_stop_l2_confirm_max_age_s",
        "chili_momentum_stop_l2_confirm_min_snaps",
        "chili_momentum_stop_l2_confirm_max_ticks",
        "chili_momentum_squeeze_exit_hold_enabled",
        "chili_momentum_trail_floor_bps",
        "chili_momentum_trail_ceiling_bps",
        "chili_momentum_volnorm_trail_enabled",
        "chili_momentum_volnorm_trail_k",
        "chili_momentum_volnorm_trail_maturity_widen_enabled",
        "chili_momentum_volnorm_trail_maturity_max_widen",
        "chili_momentum_volnorm_trail_max_dist_pct",
        "chili_momentum_cadence_atr_pct_slow_threshold",
        "chili_momentum_regime_holdtime_hot_mult",
        "chili_momentum_regime_holdtime_cold_mult",
        "chili_momentum_lane_leak_cleanup_threshold_s",
    }

    assert required <= fields


def test_momentum_feature_flag_readiness_is_operator_visible_and_reasoned() -> None:
    rows = build_momentum_feature_flag_readiness(settings)
    by_flag = {row["flag"]: row for row in rows}
    assert by_flag["chili_momentum_tape_hold_entry_enabled"]["present_in_settings"] is True
    assert by_flag["chili_momentum_tape_hold_entry_enabled"]["category"] == "alpha_entry"
    assert by_flag["chili_momentum_tape_hold_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_tape_hold_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_tape_hold_entry_enabled"]["disabled_reason"] is None
    assert "tape_hold_fixture" in by_flag["chili_momentum_tape_hold_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_ma_vwap_pullback_enabled"]["default"] is True
    assert by_flag["chili_momentum_ma_vwap_pullback_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_ma_vwap_pullback_enabled"]["disabled_reason"] is None
    assert "ma_vwap_pullback_fixture" in by_flag["chili_momentum_ma_vwap_pullback_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_bull_flag_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_bull_flag_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_bull_flag_entry_enabled"]["disabled_reason"] is None
    assert "flag_geometry_fixture" in by_flag["chili_momentum_bull_flag_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_cup_and_handle_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_cup_and_handle_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_cup_and_handle_entry_enabled"]["disabled_reason"] is None
    assert "cup_handle_fixture" in by_flag["chili_momentum_cup_and_handle_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_blue_sky_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_blue_sky_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_blue_sky_entry_enabled"]["disabled_reason"] is None
    assert "daily_levels_fixture" in by_flag["chili_momentum_blue_sky_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_wedge_break_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_wedge_break_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_wedge_break_entry_enabled"]["disabled_reason"] is None
    assert "converging_trendline_fixture" in by_flag["chili_momentum_wedge_break_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_round_number_entry_timing_enabled"]["default"] is True
    assert by_flag["chili_momentum_round_number_entry_timing_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_round_number_entry_timing_enabled"]["disabled_reason"] is None
    assert "round_half_level_fixture" in by_flag["chili_momentum_round_number_entry_timing_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_absorption_snap_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_absorption_snap_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_absorption_snap_entry_enabled"]["disabled_reason"] is None
    assert "l2_tape_fixture" in by_flag["chili_momentum_absorption_snap_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_premarket_pivot_macd_entry_enabled"]["default"] is True
    assert by_flag["chili_momentum_premarket_pivot_macd_entry_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_premarket_pivot_macd_entry_enabled"]["disabled_reason"] is None
    assert "premarket_pivot_fixture" in by_flag["chili_momentum_premarket_pivot_macd_entry_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_big_buyer_bid_starter_enabled"]["default"] is True
    assert by_flag["chili_momentum_big_buyer_bid_starter_enabled"]["effective_value"] is True
    assert by_flag["chili_momentum_big_buyer_bid_starter_enabled"]["disabled_reason"] is None
    assert "bid_support_fixture" in by_flag["chili_momentum_big_buyer_bid_starter_enabled"]["proof_gate"]
    assert by_flag["chili_momentum_pyramid_enabled"]["category"] == "add_runner"
    assert by_flag["chili_momentum_pyramid_enabled"]["settings_default"] is False
    assert by_flag["chili_momentum_pyramid_enabled"]["effective_source"] in {
        "settings_default",
        "environment",
    }
    assert isinstance(by_flag["chili_momentum_pyramid_enabled"]["active_env_aliases"], list)
    assert by_flag["chili_momentum_hard_no_trade_regime_enabled"]["hard_safety"] is True

    disabled_reasons = [str(row.get("disabled_reason") or "").lower() for row in rows]
    assert all("soak" not in reason for reason in disabled_reasons)
    proof_gates = [str(row.get("proof_gate") or "").lower() for row in rows]
    assert all(proof_gates)
    assert all("soak" not in gate for gate in proof_gates)
    disabled_rows = [row for row in rows if not bool(row.get("effective_value"))]
    assert disabled_rows
    assert all(str(row.get("proof_gate") or "") for row in disabled_rows)
    assert all(
        "fixture" in str(row.get("proof_gate") or "")
        or row.get("category") == "hard_safety"
        for row in disabled_rows
    )

    readiness = build_momentum_operator_readiness(execution_family="robinhood_agentic_mcp")
    operator_rows = readiness.get("momentum_feature_flags")
    assert isinstance(operator_rows, list)
    assert any(row.get("flag") == "chili_momentum_tape_hold_entry_enabled" for row in operator_rows)
    fallback_audit = readiness.get("momentum_settings_fallback_audit")
    assert isinstance(fallback_audit, dict)
    assert fallback_audit["missing_default_false_controls"] == 0
    assert fallback_audit["missing_settings_count"] >= 0


def test_feature_flag_readiness_surfaces_runtime_env_override_source(monkeypatch) -> None:
    monkeypatch.setenv("CHILI_MOMENTUM_PYRAMID_ENABLED", "true")

    rows = build_momentum_feature_flag_readiness(settings)
    by_flag = {row["flag"]: row for row in rows}

    row = by_flag["chili_momentum_pyramid_enabled"]
    assert row["settings_default"] is False
    assert row["effective_source"] == "environment"
    assert row["active_env_aliases"] == ["CHILI_MOMENTUM_PYRAMID_ENABLED"]


def test_momentum_settings_fallback_audit_has_no_missing_magic_knobs() -> None:
    audit = audit_momentum_settings_fallbacks(settings)

    assert audit["total_getattr_fallbacks"] > 0
    assert audit["unique_settings_seen"] > 0
    assert audit["missing_default_false_controls"] == 0
    assert isinstance(audit["missing_by_category"], dict)
    assert isinstance(audit["missing_by_risk"], dict)
    assert audit["missing_settings_count"] == 0
    assert audit["missing_by_category"] == {}
    assert audit["missing_by_risk"] == {}
    assert audit["missing_rows"] == []


def test_momentum_settings_fallback_audit_recognizes_private_settings_alias(tmp_path) -> None:
    module = tmp_path / "alias_probe.py"
    module.write_text(
        "from app.config import settings as _settings\n"
        "ENABLED = getattr(_settings, \"chili_momentum_live_runner_enabled\", False)\n",
        encoding="utf-8",
    )

    audit = audit_momentum_settings_fallbacks(settings, root=tmp_path)

    assert audit["total_getattr_fallbacks"] == 1
    assert audit["rows"][0]["setting"] == "chili_momentum_live_runner_enabled"
    assert audit["rows"][0]["present_in_settings"] is True
    assert audit["missing_default_false_controls"] == 0


def test_volnorm_exit_flag_is_advisory_while_live_volnorm_trail_is_first_class() -> None:
    """The older volnorm-exit module is not the live runner switch.

    The live runner uses the newer ratchet-only ``volnorm_trail`` path. Keep the
    old flag visible so it cannot be mistaken for a hidden disabled alpha
    handler, but do not classify it as a blocked exit path.
    """

    rows = build_momentum_feature_flag_readiness(settings)
    by_flag = {row["flag"]: row for row in rows}

    assert by_flag["chili_momentum_volnorm_exit_enabled"]["category"] == "advisory_or_telemetry"
    assert by_flag["chili_momentum_volnorm_exit_enabled"]["present_in_settings"] is True
    assert by_flag["chili_momentum_volnorm_exit_enabled"]["hard_safety"] is False
    assert "operator_enablement" in by_flag["chili_momentum_volnorm_exit_enabled"]["disabled_reason"]

    fields = set(Settings.model_fields)
    assert {
        "chili_momentum_volnorm_trail_enabled",
        "chili_momentum_volnorm_trail_k",
        "chili_momentum_volnorm_trail_maturity_widen_enabled",
        "chili_momentum_volnorm_trail_maturity_max_widen",
        "chili_momentum_volnorm_trail_max_dist_pct",
    } <= fields
    assert settings.chili_momentum_volnorm_trail_enabled is True


def test_fatigue_controls_remain_visible_soft_sizing_not_hard_safety() -> None:
    rows = build_momentum_feature_flag_readiness(settings)
    by_flag = {row["flag"]: row for row in rows}
    for flag in (
        "chili_momentum_per_symbol_fatigue_enabled",
        "chili_momentum_win_cycle_fatigue_enabled",
        "chili_momentum_fatigue_derate_enabled",
    ):
        row = by_flag[flag]
        assert row["present_in_settings"] is True
        assert row["category"] == "adaptive_sizing"
        assert row["hard_safety"] is False
        assert "sizing_fixture" in row["proof_gate"]
        assert "soak" not in str(row["disabled_reason"] or "").lower()


def test_no_market_soak_blocker_phrasing_in_momentum_surfaces() -> None:
    forbidden = re.compile(
        r"do not enable without soak|keep off until soak|until soaked|needs soak|"
        r"requires soak|unsoaked|live-soak blocker|market-soak gate|paper-soak gate",
        re.IGNORECASE,
    )
    checked_paths = list(MOMENTUM_DIR.rglob("*.py")) + [
        ROOT / "app" / "config.py",
        CERT_REPORT,
        SETUP_AUDIT_REPORT,
    ]
    offenders: list[str] = []
    for path in checked_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if forbidden.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_ross_video_audit_requires_visual_frame_evidence_not_transcript_only() -> None:
    required_phrases = (
        "transcript text is discovery/index",
        "frames or keyframes",
        "Do not certify trade/no-trade correctness from transcript mentions alone.",
    )
    surfaces = [CERT_REPORT, SETUP_AUDIT_REPORT]
    for path in surfaces:
        text = path.read_text(encoding="utf-8", errors="ignore")
        missing = [phrase for phrase in required_phrases if phrase not in text]
        assert missing == [], f"{path.name} missing Ross visual-evidence rule phrases: {missing}"


def test_setup_audit_current_status_does_not_resurrect_stale_runtime_blockers() -> None:
    text = SETUP_AUDIT_REPORT.read_text(encoding="utf-8", errors="ignore")
    stale_current_status_phrases = (
        "live runner intentionally disabled",
        "running older worker image",
        "running worker still has scheduler env true",
        "Current blockers before live-ready",
        "Deploy exactly one real momentum worker",
        "Keep live entries paused until the image is smoke-verified",
        "The local selector fix covers the config trap, but it is not deployed",
    )
    offenders = [phrase for phrase in stale_current_status_phrases if phrase in text]
    assert offenders == []
