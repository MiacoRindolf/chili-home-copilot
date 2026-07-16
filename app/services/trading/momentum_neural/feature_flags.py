"""Operator-visible momentum feature flag registry.

This module prevents a handler from being silently off because a
``getattr(settings, "..._enabled", False)`` fallback was never promoted to
first-class configuration.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ....config import settings


@dataclass(frozen=True)
class MomentumFeatureFlagSpec:
    flag: str
    category: str
    default: bool
    disabled_reason: str
    proof_gate: str
    hard_safety: bool = False


_ALPHA_PROOF_DEFAULT = (
    "handler_specific_unit_or_replay_fixture_must_cover_fresh_provider_data_"
    "structural_stop_alias_floor_setup_trace_and_replay_attribution"
)


def _alpha(
    flag: str,
    *,
    default: bool = False,
    proof_gate: str = _ALPHA_PROOF_DEFAULT,
) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="alpha_entry",
        default=default,
        disabled_reason="disabled_until_deterministic_replay_or_operator_enablement",
        proof_gate=proof_gate,
    )


def _add_runner(flag: str) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="add_runner",
        default=False,
        disabled_reason="disabled_until_add_lifecycle_fixture_or_operator_enablement",
        proof_gate="unit_or_replay_fixture_must_cover_entered_to_trailing_arm_add_submit_cooldown_risk_budget_and_exit_lifecycle",
    )


def _exit(flag: str) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="exit",
        default=False,
        disabled_reason="disabled_until_exit_fixture_or_operator_enablement",
        proof_gate="unit_or_replay_fixture_must_cover_fresh_quote_broker_truth_structural_stop_and_no_duplicate_exit_order",
    )


def _sizing(flag: str, *, default: bool = False) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="adaptive_sizing",
        default=default,
        disabled_reason="disabled_until_sizing_replay_or_operator_enablement",
        proof_gate="sizing_fixture_must_show_soft_multiplier_math_hard_risk_gate_precedence_and_no_magic_floor_override",
    )


def _hard(flag: str) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="hard_safety",
        default=False,
        disabled_reason="hard_safety_gate_disabled_by_configuration",
        proof_gate="explicit_operator_enablement_plus_unit_fixture_must_prove_fail_closed_without_blocking_documented_safe_cases",
        hard_safety=True,
    )


def _safety(flag: str, *, default: bool = True) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="hard_safety",
        default=default,
        disabled_reason="hard_safety_gate_disabled_by_configuration",
        proof_gate="unit_or_replay_fixture_must_prove_fail_closed_hard_safety_behavior_and_no_dark_block_for_documented_safe_cases",
        hard_safety=True,
    )


def _runtime(flag: str, *, default: bool = True) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="runtime_or_scheduler",
        default=default,
        disabled_reason="runtime_lane_disabled_by_operator_or_compose_profile",
        proof_gate="runtime_readiness_fixture_must_show_effective_state_single_worker_no_placeholder_no_duplicate_worker_and_no_generic_scheduler_entry_path",
    )


def _advisory(flag: str, *, default: bool = False) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="advisory_or_telemetry",
        default=default,
        disabled_reason="disabled_until_operator_enablement_or_readiness_fixture",
        proof_gate="readiness_or_telemetry_fixture_must_show_effective_state_reason_and_non_blocking_failure_mode",
    )


def _entry_gate(flag: str, *, default: bool = True) -> MomentumFeatureFlagSpec:
    return MomentumFeatureFlagSpec(
        flag=flag,
        category="entry_gate_or_veto",
        default=default,
        disabled_reason="disabled_by_operator_until_entry_gate_fixture_or_replay_reenablement",
        proof_gate="entry_gate_fixture_must_show_trigger_or_veto_math_structural_stop_telemetry_and_no_dark_block",
    )


MOMENTUM_FEATURE_FLAGS: tuple[MomentumFeatureFlagSpec, ...] = (
    # Ross/mechanizable alpha-entry handlers.
    _alpha(
        "chili_momentum_wedge_break_entry_enabled",
        default=True,
        proof_gate="converging_trendline_fixture_must_prove_tick_break_structural_stop_alias_floor_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_absorption_snap_entry_enabled",
        default=True,
        proof_gate="l2_tape_fixture_must_prove_absorption_to_snap_fresh_depth_structural_stop_alias_floor_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_blue_sky_entry_enabled",
        default=True,
        proof_gate="daily_levels_fixture_must_prove_clear_sky_room_tick_break_structural_stop_alias_floor_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_round_number_entry_timing_enabled",
        default=True,
        proof_gate="round_half_level_fixture_must_prove_timing_context_tick_trigger_structural_stop_and_no_overhead_dark_block",
    ),
    _entry_gate("chili_momentum_deep_reclaim_dipbuy_enabled"),
    _entry_gate("chili_momentum_deep_reclaim_enabled"),
    _entry_gate("chili_momentum_entry_flow_veto_enabled"),
    _entry_gate("chili_momentum_entry_extension_veto_enabled"),
    _entry_gate("chili_momentum_backside_vwap_reclaim_enabled"),
    _entry_gate("chili_momentum_flush_dip_buy_enabled"),
    _entry_gate("chili_momentum_vwap_reclaim_enabled"),
    _entry_gate("chili_momentum_wick_reclaim_entry_enabled"),
    _entry_gate("chili_momentum_abcd_entry_enabled"),
    _entry_gate("chili_momentum_double_bottom_entry_enabled"),
    _entry_gate("chili_momentum_entry_first_pullback_enabled"),
    _entry_gate("chili_momentum_explosive_raw_break_enabled"),
    _entry_gate("chili_momentum_backside_veto_enabled"),
    _entry_gate("chili_momentum_red_vol_exhaustion_veto_enabled"),
    _entry_gate("chili_momentum_explosive_floor_enabled"),
    _entry_gate("chili_momentum_orb_entry_enabled"),
    _entry_gate("chili_momentum_red_to_green_entry_enabled"),
    _alpha(
        "chili_momentum_bull_flag_entry_enabled",
        default=True,
        proof_gate="flag_geometry_fixture_must_prove_flag_hold_tick_break_structural_stop_alias_floor_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_cup_and_handle_entry_enabled",
        default=True,
        proof_gate="cup_handle_fixture_must_prove_handle_hold_breakout_fresh_tape_structural_stop_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_ma_vwap_pullback_enabled",
        default=True,
        proof_gate="ma_vwap_pullback_fixture_must_prove_reclaim_hold_tick_fire_structural_stop_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_tape_hold_entry_enabled",
        default=True,
        proof_gate="tape_hold_fixture_must_prove_wait_reason_pullback_levels_tape_ok_tick_fire_structural_stop_and_trace",
    ),
    _alpha(
        "chili_momentum_momentum_continuation_entry_enabled",
        proof_gate="continuation_fixture_must_prove_fresh_event_source_non_generic_ross_universe_structural_stop_and_replay_attribution",
    ),
    _alpha(
        "chili_momentum_premarket_pivot_macd_entry_enabled",
        default=True,
        proof_gate="premarket_pivot_fixture_must_prove_open_window_macd_recross_cold_market_refusal_fresh_tape_structural_stop_and_replay_attribution",
    ),
    _alpha("chili_momentum_ask_thins_dip_entry_enabled"),
    _alpha("chili_momentum_sub_vwap_trap_entry_enabled"),
    _alpha("chili_momentum_pulling_away_roc_entry_enabled"),
    _alpha("chili_momentum_bottom_reversal_entry_enabled"),
    _alpha("chili_momentum_inverse_head_shoulders_entry_enabled"),
    _alpha(
        "chili_momentum_big_buyer_bid_starter_enabled",
        default=True,
        proof_gate="bid_support_fixture_must_prove_fresh_depth_tight_spread_self_relative_bid_stack_and_annotation_only_no_entry_or_veto",
    ),
    _alpha("chili_momentum_l2_confirm_enabled"),
    _alpha("chili_momentum_entry_tight_false_break_reclaim_enabled"),
    _alpha("chili_momentum_add_into_halt_enabled"),
    _alpha("chili_momentum_entry_l2_veto_enabled"),
    _alpha("chili_momentum_wick_reclaim_slow_recovery_gate_enabled"),
    _alpha("chili_momentum_dip_velocity_conviction_enabled"),
    _alpha("chili_momentum_candle_quality_multitf_veto_enabled"),
    _alpha("chili_momentum_break_candle_adaptive_close_pos_enabled"),
    _alpha("chili_momentum_second_leg_preference_enabled"),
    _alpha("chili_momentum_anticipation_starter_enabled"),
    # Add/runner lifecycle.
    _add_runner("chili_momentum_pyramid_enabled"),
    _add_runner("chili_momentum_pyramid_discrete_add_enabled"),
    _add_runner("chili_momentum_scale_grid_enabled"),
    _add_runner("chili_momentum_smart_hold_enabled"),
    _add_runner("chili_momentum_pullback_add_enabled"),
    _add_runner("chili_momentum_micropullback_reentry_enabled"),
    _add_runner("chili_momentum_flag_breakout_add_enabled"),
    _add_runner("chili_momentum_iceberg_add_probe_enabled"),
    _add_runner("chili_momentum_adaptive_reentry_cooldown_enabled"),
    # Exit and reconciliation handlers.
    _exit("chili_momentum_bos_exit_live_enabled"),
    _exit("chili_momentum_exit_adaptive_equity_enabled"),
    _exit("chili_momentum_exit_ladder_enabled"),
    _exit("chili_momentum_exit_ofi_lock_enabled"),
    _exit("chili_momentum_exit_ofi_lock_partial_enabled"),
    _exit("chili_momentum_exit_ofi_hidden_seller_enabled"),
    _exit("chili_momentum_exit_candle_confirm_enabled"),
    _exit("chili_momentum_exit_tape_accel_reversal_enabled"),
    _exit("chili_momentum_cadence_aware_exit_enabled"),
    _exit("chili_momentum_velocity_persistence_exit_enabled"),
    _exit("chili_momentum_exit_topping_tail_enabled"),
    _exit("chili_momentum_breakout_bailout_enabled"),
    _exit("chili_momentum_measured_move_exit_enabled"),
    _exit("chili_momentum_stop_l2_confirm_enabled"),
    _exit("chili_momentum_broker_truth_reconciliation_enabled"),
    _exit("chili_momentum_broker_truth_label_enabled"),
    _exit("chili_momentum_instant_bid_below_fill_cut_enabled"),
    _exit("chili_momentum_instant_bid_above_fill_confirm_enabled"),
    _exit("chili_momentum_sub5min_scalp_bailout_enabled"),
    _exit("chili_momentum_bail_on_no_confirmation_enabled"),
    # Adaptive sizing and context dampeners.
    _sizing("chili_momentum_per_symbol_fatigue_enabled"),
    _sizing("chili_momentum_win_cycle_fatigue_enabled"),
    _sizing("chili_momentum_hot_cold_size_enabled"),
    _sizing("chili_momentum_fatigue_derate_enabled"),
    _sizing("chili_momentum_timeofday_schedule_enabled"),
    _sizing("chili_momentum_catalyst_conviction_enabled"),
    _sizing("chili_momentum_green_day_graduation_enabled"),
    _sizing("chili_momentum_explosive_recalibration_enabled"),
    _sizing("chili_momentum_entry_extension_rvol_boost_enabled"),
    _sizing("chili_momentum_conviction_rvol_fallback_enabled"),
    _sizing("chili_momentum_midday_deweight_enabled"),
    _sizing("chili_momentum_regime_holdtime_enabled"),
    _sizing("chili_momentum_a_setup_size_floor_enabled", default=True),
    _sizing("chili_momentum_frontside_adaptive_enabled", default=True),
    _sizing("chili_momentum_kelly_conviction_enabled", default=True),
    _sizing("chili_momentum_meta_label_derate_enabled", default=True),
    _sizing("chili_momentum_prior_day_pnl_damper_enabled", default=True),
    _sizing("chili_momentum_red_intraday_size_down_enabled", default=True),
    _sizing("chili_momentum_squeeze_entry_sizeup_enabled", default=True),
    _sizing("chili_momentum_squeeze_exit_hold_enabled", default=True),
    _sizing("chili_momentum_daily_room_size_down_enabled", default=True),
    _sizing("chili_momentum_float_turnover_size_down_enabled", default=True),
    _sizing("chili_momentum_a_setup_notional_floor_enabled", default=True),
    # Hard safety / veto gates.
    _safety("chili_momentum_max_loss_circuit_enabled"),
    _safety("chili_momentum_max_loss_fresh_quote_guard_enabled"),
    _safety("chili_momentum_atomic_risk_budget_enabled"),
    _safety("chili_momentum_broker_zero_trust_clamp_enabled"),
    _safety("chili_momentum_consecutive_loss_halt_enabled"),
    _safety("chili_momentum_daily_trade_count_budget_enabled"),
    _safety("chili_momentum_fill_boundary_breaker_recheck_enabled"),
    _safety("chili_momentum_liquidity_risk_cap_enabled"),
    _safety("chili_momentum_lost_vwap_flatten_enabled"),
    _safety("chili_momentum_run_r_breaker_enabled"),
    _hard("chili_momentum_hard_no_trade_regime_enabled"),
    _hard("chili_momentum_hard_no_trade_midday_enabled"),
    _hard("chili_momentum_halt_chain_risk_gate_enabled"),
    _hard("chili_momentum_halt_down_cascade_liquidate_enabled"),
    _hard("chili_momentum_false_halt_avoid_enabled"),
    _hard("chili_momentum_halt_resumption_direction_enabled"),
    _hard("chili_momentum_overhead_veto_enabled"),
    _hard("chili_momentum_red_candle_entry_block_enabled"),
    _hard("chili_momentum_order_burst_candle_guard_enabled"),
    _hard("chili_momentum_adaptive_spread_cost_veto_enabled"),
    _hard("chili_momentum_dip_buy_rth_only_enabled"),
    _hard("chili_momentum_overnight_dark_flatten_enabled"),
    # Existing/default-off operational toggles that must remain visible.
    _advisory("chili_momentum_micropull_enabled"),
    _advisory("chili_momentum_micro_pullback_primary_enabled", default=True),
    _advisory("chili_momentum_extreme_explosive_eligible_enabled"),
    _advisory("chili_momentum_volnorm_exit_enabled"),
    _advisory("chili_momentum_volnorm_trail_enabled", default=True),
    _advisory("chili_momentum_volnorm_trail_maturity_widen_enabled", default=True),
    _advisory("chili_momentum_replay_regression_enabled", default=True),
    _advisory("chili_momentum_live_runner_replay_snapshot_enabled", default=True),
    _advisory("chili_momentum_neural_enabled", default=True),
    _advisory("chili_momentum_entry_gates_enabled", default=True),
    _advisory("chili_momentum_performance_sizing_enabled", default=True),
    _advisory("chili_momentum_universe_uncapped_enabled"),
    _advisory("chili_momentum_universe_tick_record_enabled", default=True),
    _advisory("chili_momentum_tape_delta_ignite_enabled", default=True),
    _advisory("chili_momentum_attention_leadership_enabled", default=True),
    _advisory("chili_momentum_premarket_gap_full_universe_enabled", default=True),
    _advisory("chili_momentum_live_eligible_recency_grace_enabled", default=True),
    _advisory("chili_momentum_independent_smallcap_a_plus_enabled", default=True),
    _advisory("chili_momentum_nbbo_tape_enabled", default=True),
    _advisory("chili_momentum_ross_rvol_feed_enabled", default=True),
    _advisory("chili_momentum_ross_transcript_bridge_enabled", default=True),
    _advisory("chili_momentum_thin_spread_squeeze_lane_enabled", default=True),
    _runtime("chili_momentum_paper_runner_enabled"),
    _runtime("chili_momentum_paper_runner_scheduler_enabled"),
    _runtime("chili_momentum_live_runner_enabled", default=False),
    _runtime("chili_momentum_live_runner_scheduler_enabled", default=False),
    _runtime("chili_momentum_live_runner_loop_enabled"),
    _runtime("chili_momentum_live_runner_loop_iqfeed_notify_enabled"),
    _runtime("chili_momentum_live_runner_loop_iqfeed_poll_fallback_enabled"),
    _runtime("chili_momentum_live_runner_loop_iqfeed_tape_enabled"),
    _runtime("chili_momentum_auto_arm_live_enabled"),
    _runtime("chili_momentum_auto_arm_live_scheduler_enabled", default=False),
    _advisory("chili_momentum_auto_arm_live_scheduler_fallback_enabled"),
    _advisory("chili_momentum_live_runner_batch_fallback_enabled"),
    _advisory("chili_momentum_family_regime_prefilter_enabled"),
    _advisory("chili_momentum_decouple_watching_enabled"),
    _advisory("chili_momentum_watch_fanout_adaptive_enabled", default=True),
    _advisory("chili_momentum_fill_log_enabled"),
    _advisory("chili_momentum_overnight_trading_enabled"),
    _advisory("chili_momentum_overnight_tape_enabled"),
    _advisory("chili_momentum_ws_ignition_enabled"),
    _advisory("chili_momentum_live_same_session_reentry_enabled"),
    _entry_gate("chili_momentum_adaptive_target_enabled"),
    _entry_gate("chili_momentum_bid_prop_confirmer_enabled"),
    _entry_gate("chili_momentum_clean_decline_terminal_enabled"),
    _entry_gate("chili_momentum_early_premarket_enabled"),
    _entry_gate("chili_momentum_early_trail_arm_enabled"),
    _entry_gate("chili_momentum_entry_chase_enabled"),
    _entry_gate("chili_momentum_entry_fast_poll_enabled"),
    _entry_gate("chili_momentum_entry_inline_repeg_enabled"),
    _entry_gate("chili_momentum_entry_placement_governor_enabled"),
    _entry_gate("chili_momentum_entry_quote_refetch_enabled"),
    _entry_gate("chili_momentum_event_select_primary_enabled"),
    _entry_gate("chili_momentum_ext_hours_quote_age_enabled"),
    _entry_gate("chili_momentum_opening_bell_suppression_enabled"),
    _entry_gate("chili_momentum_recycle_entry_state_reset_enabled"),
    _entry_gate("chili_momentum_reentry_after_stop_bound_enabled"),
    _entry_gate("chili_momentum_ross_breakout_starter_enabled"),
    _entry_gate("chili_momentum_ross_event_admission_enabled"),
    _entry_gate("chili_momentum_ross_feed_health_enabled"),
    _entry_gate("chili_momentum_runaway_cross_enabled"),
    _entry_gate("chili_momentum_setup_selector_enabled"),
    _entry_gate("chili_momentum_spread_cap_em_fallback_enabled"),
    _entry_gate("chili_momentum_sticky_backside_bench_enabled"),
    _entry_gate("chili_momentum_tick_first_pullback_enabled"),
    _entry_gate("chili_momentum_vertical_chase_enabled"),
    _entry_gate("chili_momentum_vertical_chase_nohalt_thrust_enabled"),
)


def iter_momentum_feature_flags() -> Iterable[MomentumFeatureFlagSpec]:
    return MOMENTUM_FEATURE_FLAGS


def build_momentum_feature_flag_readiness(settings_obj: Any = settings) -> list[dict[str, Any]]:
    """Return operator-visible effective state for all registered feature flags."""
    fields = getattr(settings_obj.__class__, "model_fields", {})
    rows: list[dict[str, Any]] = []
    for spec in MOMENTUM_FEATURE_FLAGS:
        field = fields.get(spec.flag) if isinstance(fields, dict) else None
        present = field is not None or hasattr(settings_obj, spec.flag)
        value = bool(getattr(settings_obj, spec.flag, spec.default))
        setting_default = bool(getattr(field, "default", spec.default)) if field is not None else None
        env_aliases = _validation_alias_names(getattr(field, "validation_alias", None)) if field is not None else ()
        active_env_aliases = tuple(alias for alias in env_aliases if alias in os.environ)
        if active_env_aliases:
            effective_source = "environment"
        elif field is not None:
            effective_source = "settings_default"
        elif hasattr(settings_obj, spec.flag):
            effective_source = "runtime_attribute"
        else:
            effective_source = "registry_default"
        rows.append(
            {
                "flag": spec.flag,
                "category": spec.category,
                "present_in_settings": bool(present),
                "default": bool(spec.default),
                "settings_default": setting_default,
                "effective_value": value,
                "effective_source": effective_source,
                "active_env_aliases": list(active_env_aliases),
                "disabled_reason": None if value else spec.disabled_reason,
                "proof_gate": spec.proof_gate,
                "hard_safety": bool(spec.hard_safety),
            }
        )
    return rows


def _validation_alias_names(alias: Any) -> tuple[str, ...]:
    if alias is None:
        return ()
    choices = getattr(alias, "choices", None)
    if choices is None:
        return (str(alias),)
    out: list[str] = []
    for choice in choices:
        if isinstance(choice, (list, tuple)):
            out.extend(str(part) for part in choice)
        else:
            out.append(str(choice))
    return tuple(out)


def _momentum_neural_root() -> Path:
    return Path(__file__).resolve().parent


def _category_for_setting(name: str, source: str) -> str:
    lower = f"{name} {source}".lower()
    if any(token in lower for token in ("max_loss", "stop", "halt", "hard_no_trade", "risk", "liquidity")):
        return "hard_safety_or_risk"
    if any(token in lower for token in ("size", "floor", "fatigue", "kelly", "daily_room", "frontside")):
        return "sizing_or_derate"
    if any(token in lower for token in ("add", "pyramid", "reentry", "trail", "hold")):
        return "runner_or_add"
    if any(token in lower for token in ("entry", "break", "reclaim", "pullback", "vwap", "macd", "l2")):
        return "alpha_entry"
    if any(token in lower for token in ("exit", "bailout", "flatten", "giveback")):
        return "exit"
    if any(token in lower for token in ("quote", "spread", "nbbo", "tape", "ofi", "flow")):
        return "market_data_quality"
    if any(token in lower for token in ("telemetry", "capture", "diagnostic", "log")):
        return "telemetry"
    return "other"


def _risk_for_setting(name: str, category: str) -> str:
    lower = name.lower()
    if category in {"hard_safety_or_risk", "sizing_or_derate", "runner_or_add", "exit"}:
        return "high"
    if any(token in lower for token in ("entry", "spread", "quote", "tape", "flow", "vwap", "l2")):
        return "medium"
    return "low"


def _safe_unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


def _literal_default(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _is_settings_ref(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"settings", "settings_obj", "_settings"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"settings", "settings_obj"}
    return False


def audit_momentum_settings_fallbacks(
    settings_obj: Any = settings,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Audit fallback literals used by momentum code when a Setting is absent.

    This is a visibility tool, not a blocker. It surfaces remaining fallback knobs
    so they can be promoted deliberately by risk category instead of staying as
    invisible magic numbers.
    """
    root = root or _momentum_neural_root()
    fields = set(getattr(settings_obj.__class__, "model_fields", {}))
    rows_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            tree = ast.parse(text)
        except Exception:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
                continue
            if len(node.args) < 2 or not _is_settings_ref(node.args[0]):
                continue
            key_node = node.args[1]
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            name = key_node.value
            if not name.startswith("chili_momentum_"):
                continue
            default_node = node.args[2] if len(node.args) >= 3 else None
            default_expr = _safe_unparse(default_node)
            default_value = _literal_default(default_node)
            category = _category_for_setting(name, rel)
            risk = _risk_for_setting(name, category)
            key = (name, rel, int(getattr(node, "lineno", 0) or 0))
            rows_by_key[key] = {
                "setting": name,
                "source": rel,
                "line": int(getattr(node, "lineno", 0) or 0),
                "default_expr": default_expr,
                "default_value": default_value,
                "present_in_settings": name in fields,
                "category": category,
                "risk": risk,
                "default_false_control": default_value is False,
            }
    rows = sorted(rows_by_key.values(), key=lambda r: (r["present_in_settings"], r["risk"], r["category"], r["setting"], r["source"], r["line"]))
    missing = [r for r in rows if not r["present_in_settings"]]
    missing_by_category: dict[str, int] = {}
    missing_by_risk: dict[str, int] = {}
    for row in missing:
        missing_by_category[row["category"]] = missing_by_category.get(row["category"], 0) + 1
        missing_by_risk[row["risk"]] = missing_by_risk.get(row["risk"], 0) + 1
    return {
        "total_getattr_fallbacks": len(rows),
        "unique_settings_seen": len({r["setting"] for r in rows}),
        "missing_settings_count": len({r["setting"] for r in missing}),
        "missing_fallback_sites": len(missing),
        "missing_default_false_controls": len({r["setting"] for r in missing if r["default_false_control"]}),
        "missing_by_category": dict(sorted(missing_by_category.items())),
        "missing_by_risk": dict(sorted(missing_by_risk.items())),
        "rows": rows,
        "missing_rows": missing,
    }
