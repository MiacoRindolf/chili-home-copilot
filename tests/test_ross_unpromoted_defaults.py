"""Default containment for unpromoted Ross-parity experiments.

Mechanism tests may opt individual candidates in explicitly.  Missing or
unprojected settings must preserve the previously deployed behavior until a
sealed paired OOS result earns promotion.
"""
from __future__ import annotations

import inspect

from app.config import Settings


_UNPROMOTED_DEFAULTS = (
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


def test_unpromoted_candidates_are_opt_in():
    for name in _UNPROMOTED_DEFAULTS:
        assert Settings.model_fields[name].default is False, name


def test_missing_setting_fallbacks_do_not_silently_promote_candidates():
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
    assert set(owners) == set(_UNPROMOTED_DEFAULTS)
    for name, modules in owners.items():
        for module in modules:
            source = inspect.getsource(module)
            assert f'getattr(settings, "{name}", True)' not in source
            assert f'getattr(_settings, "{name}", True)' not in source
