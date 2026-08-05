"""Operator-approved default-ON momentum levers and exact kill switches."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.config import Settings


_USER_APPROVED_DEFAULT_ON = (
    "chili_momentum_sub_vwap_trap_entry_enabled",
    "chili_momentum_catalyst_arb_flat_gate_enabled",
    "chili_momentum_tick_break_tape_confirm_enabled",
    "chili_momentum_universe_float_gate_enabled",
    "chili_momentum_flush_dip_volume_gate_enabled",
    "chili_momentum_ross_stop_alignment_enabled",
    "chili_momentum_orb_ihs_structural_stop_enabled",
    "chili_momentum_fresh_ignition_reentry_bypass_enabled",
    # 2026-07-27 golden-baseline autopsy levers (operator-approved, default ON):
    "chili_momentum_chase_defer_enabled",
    "chili_momentum_whipsaw_rapid_escalation_enabled",
    # 2026-07-31 L6 (scorecard v2: JLHL +393%/JEM +224% benched ng morning proxy):
    "chili_momentum_flush_dip_fresh_hod_afternoon_enabled",
    # 2026-08-01 L7 (JLHL/JEM vs JZXN distribution study — day-context dip geometry):
    "chili_momentum_dip_monster_context_enabled",
    "chili_momentum_late_ah_monster_placement_enabled",
    "chili_momentum_monster_structure_floor_enabled",
    # 2026-08-04 L10b — price-structure partials. HINDI bagong lever: TUMATAKBO NA
    # ito sa produksyon via env samantalang False ang code default, kaya ang lahat
    # ng sealed replay ay sumukat ng IBANG exit configuration kaysa sa lane. Ang
    # pag-flip ng default ay PARITY CORRECTION at pag-alis ng dark flag.
    "chili_momentum_scale_grid_enabled",
)

# EVIDENCE-RETIRED default-OFF levers: still in the sealed arm roster (per-lever
# A/B stays expressible) but the PRODUCTION default is False on measured
# evidence. bail_on_no_confirmation: 2026-07-31 L2a sweep + per-trade
# decomposition — net −$530 (killed winners + bail→cooldown churn/lockouts)
# vs −$46.58 redundant protection (structural backstops breakout_failed_fast_bail
# and tape-accel caught every genuinely failing breakout a few seconds later).
_EVIDENCE_RETIRED_DEFAULT_OFF = (
    "chili_momentum_bail_on_no_confirmation_enabled",
)


def test_user_approved_momentum_levers_default_on():
    for name in _USER_APPROVED_DEFAULT_ON:
        assert Settings.model_fields[name].default is True, name


def test_evidence_retired_levers_default_off():
    for name in _EVIDENCE_RETIRED_DEFAULT_OFF:
        assert Settings.model_fields[name].default is False, name


def test_missing_setting_fallbacks_preserve_default_on_doctrine():
    from app.services.trading.momentum_neural import (
        entry_gates,
        live_runner,
        paper_execution,
        pipeline,
        universe,
        viability,
    )

    owners = {
        "chili_momentum_sub_vwap_trap_entry_enabled": (entry_gates,),
        "chili_momentum_catalyst_arb_flat_gate_enabled": (pipeline, viability),
        "chili_momentum_tick_break_tape_confirm_enabled": (entry_gates,),
        "chili_momentum_universe_float_gate_enabled": (universe,),
        "chili_momentum_flush_dip_volume_gate_enabled": (entry_gates,),
        "chili_momentum_ross_stop_alignment_enabled": (entry_gates,),
        "chili_momentum_orb_ihs_structural_stop_enabled": (live_runner,),
        "chili_momentum_fresh_ignition_reentry_bypass_enabled": (live_runner,),
        "chili_momentum_chase_defer_enabled": (live_runner,),
        "chili_momentum_whipsaw_rapid_escalation_enabled": (live_runner,),
        "chili_momentum_flush_dip_fresh_hod_afternoon_enabled": (entry_gates,),
        "chili_momentum_dip_monster_context_enabled": (entry_gates,),
        "chili_momentum_late_ah_monster_placement_enabled": (entry_gates,),
        "chili_momentum_monster_structure_floor_enabled": (live_runner,),
        # DALAWANG may-ari: parehong may sariling getattr fallback ang
        # paper_execution.scale_grid_enabled() at live_runner._resolve_scale_grid.
        # Ang isa sa kanila ay naiwang False matapos ang 2026-08-04 parity fix —
        # ito ang pumipigil na maulit iyon.
        "chili_momentum_scale_grid_enabled": (paper_execution, live_runner),
    }
    assert set(owners) == set(_USER_APPROVED_DEFAULT_ON)
    for name, modules in owners.items():
        for module in modules:
            source = inspect.getsource(module)
            defaults = [
                call.args[2].value
                for call in ast.walk(ast.parse(source))
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "getattr"
                and len(call.args) >= 3
                and isinstance(call.args[1], ast.Constant)
                and call.args[1].value == name
                and isinstance(call.args[2], ast.Constant)
            ]
            assert defaults, (name, module.__name__)
            assert set(defaults) == {True}, (name, module.__name__, defaults)

    # Evidence-retired levers: the getattr fallback must MATCH the config
    # default (False) so the report-binding doctrine holds sa parehong daan.
    from app.services.trading.momentum_neural import live_runner as _lr
    for name in _EVIDENCE_RETIRED_DEFAULT_OFF:
        source = inspect.getsource(_lr)
        defaults = [
            call.args[2].value
            for call in ast.walk(ast.parse(source))
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "getattr"
            and len(call.args) >= 3
            and isinstance(call.args[1], ast.Constant)
            and call.args[1].value == name
            and isinstance(call.args[2], ast.Constant)
        ]
        assert defaults, name
        assert set(defaults) == {False}, (name, defaults)


def test_replay_ab_tool_uses_the_complete_closed_operator_policy() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "replay_ab_dark_flags.py"
    ).read_text(encoding="utf-8")
    assignments = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "APPROVED_STRATEGY_FLAGS_BY_SLUG"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    pairs = tuple(ast.literal_eval(assignments[0].value))
    assert len(pairs) == 16
    # Ang arm roster = default-ON levers + evidence-retired levers: ang retired
    # flag ay nananatili sa sealed grammar (kaya nasusukat pa rin per-arm) kahit
    # OFF na ang production default.
    assert {flag for _, flag in pairs} == (
        set(_USER_APPROVED_DEFAULT_ON) | set(_EVIDENCE_RETIRED_DEFAULT_OFF)
    )
    assert "arbitrary FLAGS_JSON is forbidden in sealed replay" in source
    assert "type(value) is not bool" in source
