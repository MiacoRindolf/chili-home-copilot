"""Ross-parity L2b (2026-07-25): structural consumption for ORB + inverse-H&S.

The coverage audit found orb_break*/inverse_head_shoulders_break* emit pullback_low/high
under the standard debug keys but were NEVER in STRUCTURAL_TRIGGER_REASONS — their
structural stops were silently dropped (ATR fallback) and they were excluded from the
leader/chase structural bypasses. The accessor ``structural_trigger_reasons()`` is now
the single read point for all three consumers; the extension is flag-gated
(``chili_momentum_orb_ihs_structural_stop_enabled``) so one env flip reverts both
detectors to the legacy ATR-stop behavior.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.services.trading.momentum_neural.live_runner import (
    ORB_IHS_STRUCTURAL_TRIGGER_REASONS,
    STRUCTURAL_TRIGGER_REASONS,
    structural_trigger_reasons,
)

_LR = "app.services.trading.momentum_neural.live_runner"


def test_extension_reasons_exact_strings():
    # the four fire-reason strings must match entry_gates' emissions exactly
    assert ORB_IHS_STRUCTURAL_TRIGGER_REASONS == (
        "orb_break", "orb_break_tick_ok",
        "inverse_head_shoulders_break", "inverse_head_shoulders_break_tick_ok",
    )


def test_flag_on_includes_extension():
    with patch(f"{_LR}.settings") as ms:
        ms.chili_momentum_orb_ihs_structural_stop_enabled = True
        got = structural_trigger_reasons()
    for r in ORB_IHS_STRUCTURAL_TRIGGER_REASONS:
        assert r in got
    for r in STRUCTURAL_TRIGGER_REASONS:
        assert r in got  # base set always present


def test_flag_off_is_legacy_base_tuple():
    with patch(f"{_LR}.settings") as ms:
        ms.chili_momentum_orb_ihs_structural_stop_enabled = False
        got = structural_trigger_reasons()
    assert got == STRUCTURAL_TRIGGER_REASONS
    for r in ORB_IHS_STRUCTURAL_TRIGGER_REASONS:
        assert r not in got


def test_unpromoted_structural_candidates_default_and_fallback_off():
    assert (
        Settings.model_fields[
            "chili_momentum_orb_ihs_structural_stop_enabled"
        ].default
        is False
    )
    assert (
        Settings.model_fields["chili_momentum_ross_stop_alignment_enabled"].default
        is False
    )

    # A partial/mock settings projection must not silently enable the wider
    # structural/chase path.
    with patch(f"{_LR}.settings", new=SimpleNamespace()):
        assert structural_trigger_reasons() == STRUCTURAL_TRIGGER_REASONS


def test_ross_stop_alignment_missing_setting_fallback_is_off():
    import inspect

    from app.services.trading.momentum_neural import entry_gates

    src = inspect.getsource(entry_gates)
    assert src.count(
        'getattr(settings, "chili_momentum_ross_stop_alignment_enabled", False)'
    ) == 3
    assert (
        'getattr(settings, "chili_momentum_ross_stop_alignment_enabled", True)'
        not in src
    )


def test_base_tuple_unchanged_by_this_pr():
    # the legacy tuple itself must not have gained the new reasons (the flag governs them)
    for r in ORB_IHS_STRUCTURAL_TRIGGER_REASONS:
        assert r not in STRUCTURAL_TRIGGER_REASONS


def test_no_raw_membership_checks_remain():
    # every consumer must read the accessor, or the flag silently loses a site
    import inspect

    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner)
    assert src.count("in STRUCTURAL_TRIGGER_REASONS") == 0, (
        "raw membership check found — all consumers must call structural_trigger_reasons()"
    )
