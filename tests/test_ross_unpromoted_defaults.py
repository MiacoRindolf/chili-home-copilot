"""Operator-approved default-ON momentum levers and exact kill switches."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.config import Settings


_USER_APPROVED_DEFAULT_ON = (
    "chili_momentum_sub_vwap_trap_entry_enabled",
    "chili_momentum_bail_on_no_confirmation_enabled",
    "chili_momentum_catalyst_arb_flat_gate_enabled",
    "chili_momentum_tick_break_tape_confirm_enabled",
    "chili_momentum_universe_float_gate_enabled",
    "chili_momentum_flush_dip_volume_gate_enabled",
    "chili_momentum_ross_stop_alignment_enabled",
    "chili_momentum_orb_ihs_structural_stop_enabled",
    "chili_momentum_fresh_ignition_reentry_bypass_enabled",
)


def test_user_approved_momentum_levers_default_on():
    for name in _USER_APPROVED_DEFAULT_ON:
        assert Settings.model_fields[name].default is True, name


def test_missing_setting_fallbacks_preserve_default_on_doctrine():
    from app.services.trading.momentum_neural import (
        entry_gates,
        live_runner,
        pipeline,
        universe,
        viability,
    )

    owners = {
        "chili_momentum_sub_vwap_trap_entry_enabled": (entry_gates,),
        "chili_momentum_bail_on_no_confirmation_enabled": (live_runner,),
        "chili_momentum_catalyst_arb_flat_gate_enabled": (pipeline, viability),
        "chili_momentum_tick_break_tape_confirm_enabled": (entry_gates,),
        "chili_momentum_universe_float_gate_enabled": (universe,),
        "chili_momentum_flush_dip_volume_gate_enabled": (entry_gates,),
        "chili_momentum_ross_stop_alignment_enabled": (entry_gates,),
        "chili_momentum_orb_ihs_structural_stop_enabled": (live_runner,),
        "chili_momentum_fresh_ignition_reentry_bypass_enabled": (live_runner,),
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


def test_legacy_ab_tool_uses_an_explicit_off_baseline() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "replay_ab_dark_flags.py"
    ).read_text(encoding="utf-8")
    assignments = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "ARMS"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    arms = ast.literal_eval(assignments[0].value)
    assert arms["base"] == {
        "chili_momentum_sub_vwap_trap_entry_enabled": False,
        "chili_momentum_bail_on_no_confirmation_enabled": False,
    }
    assert arms["both"] == {
        "chili_momentum_sub_vwap_trap_entry_enabled": True,
        "chili_momentum_bail_on_no_confirmation_enabled": True,
    }
