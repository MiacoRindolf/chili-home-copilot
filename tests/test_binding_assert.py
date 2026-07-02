"""WAVE-1 FIX-9 — DEPLOY BINDING-ASSERT.

Two deploys dropped critical env pins (the 1m pullback interval = −$137; the R3 flags)
with nothing at startup comparing the EFFECTIVE live binding to the deploy intent. The
binding-assert COMPUTES each live binding from settings, logs one line per value + a
summary, and hard-fails on drift ONLY in strict mode (default: warn-loud).

These tests pin:
  * a clean settings object => all bindings OK, no raise (default warn-only).
  * a DRIFTED setting => a DRIFT result + a DRIFT warn log; strict mode RAISES.
  * strict-off never raises even on drift (the safe default).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural.binding_assert import (
    BindingDriftError,
    evaluate_bindings,
    run_binding_assert,
)


def test_clean_settings_all_ok_and_no_raise(caplog):
    with caplog.at_level(logging.INFO):
        results = run_binding_assert(settings, strict=False)
    assert results, "manifest must not be empty"
    assert all(r.ok for r in results), [r.name for r in results if not r.ok]
    # One OK line per binding + a SUMMARY line.
    text = caplog.text
    assert "[binding_assert]" in text
    assert "SUMMARY all_ok" in text


def _drifted_settings():
    """A settings clone with ONE deploy-critical pin dropped: the pullback interval
    reverted to a wrong timeframe (the −$137 class), everything else intact."""
    kw = {
        "chili_momentum_pullback_entry_interval": "5m",
        "chili_momentum_midday_deweight_enabled": settings.chili_momentum_midday_deweight_enabled,
        "chili_momentum_run_r_breaker_enabled": settings.chili_momentum_run_r_breaker_enabled,
        "chili_momentum_stop_ratchet_strict_enabled": settings.chili_momentum_stop_ratchet_strict_enabled,
        "chili_momentum_floor_raise_only_enabled": settings.chili_momentum_floor_raise_only_enabled,
        "chili_momentum_explosive_prequal_floor_enabled": settings.chili_momentum_explosive_prequal_floor_enabled,
        "chili_momentum_early_premarket_enabled": settings.chili_momentum_early_premarket_enabled,
        "chili_momentum_early_premarket_min_movers": settings.chili_momentum_early_premarket_min_movers,
        "chili_momentum_spread_cap_em_fallback_enabled": settings.chili_momentum_spread_cap_em_fallback_enabled,
    }
    # Force a DRIFT on the boolean strict-ratchet flag (expected True, live False).
    kw["chili_momentum_stop_ratchet_strict_enabled"] = False
    return SimpleNamespace(**kw)


def test_drifted_setting_produces_drift_result_and_log(caplog):
    drifted = _drifted_settings()
    with caplog.at_level(logging.WARNING):
        results = run_binding_assert(drifted, strict=False)  # warn-only: no raise
    by_name = {r.name: r for r in results}
    assert by_name["stop_ratchet_strict_enabled"].ok is False
    assert by_name["stop_ratchet_strict_enabled"].expected is True
    assert by_name["stop_ratchet_strict_enabled"].live is False
    # A DRIFT line + the drift SUMMARY were logged.
    assert "DRIFT" in caplog.text
    assert "SUMMARY drift=" in caplog.text


def test_strict_mode_raises_on_drift():
    drifted = _drifted_settings()
    with pytest.raises(BindingDriftError) as ei:
        run_binding_assert(drifted, strict=True)
    assert "stop_ratchet_strict_enabled" in str(ei.value)


def test_strict_off_never_raises_even_on_drift():
    drifted = _drifted_settings()
    # The safe default: a drift (or a missing manifest entry) can never kill prod.
    results = run_binding_assert(drifted, strict=False)
    assert any(not r.ok for r in results)


def test_evaluate_bindings_is_pure_and_never_raises_on_bad_settings():
    # A settings object missing every attribute must not raise; each binding reports a
    # live value (from the getattr fallback) and OK/DRIFT accordingly.
    empty = SimpleNamespace()
    results = evaluate_bindings(empty)
    assert results
    # No exception; the boolean bindings fall back to False (=> DRIFT vs expected True).
    assert any(not r.ok for r in results)
