"""Ross-parity L5 / GAP-B (2026-07-25): fresh-ignition re-entry cap exemption.

Pure-decision matrix for ``fresh_ignition_reentry_allowed`` (the terminalization-edge
grant) + source-level wiring asserts on live_runner (the FSM block exists, emits the
exemption event, and consults the pure helper — the same wiring-assert idiom as
test_structural_trigger_reasons).
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading.momentum_neural.risk_policy import fresh_ignition_reentry_allowed


# ── pure decision matrix ─────────────────────────────────────────────────────

def test_grant_when_ignition_and_budget():
    ok, reason = fresh_ignition_reentry_allowed(
        enabled=True, ignition_ok=True, ignition_exemptions=0, max_ignition_exemptions=1)
    assert ok is True
    assert reason == "ignition_exempt"


def test_flag_off_never_grants():
    ok, reason = fresh_ignition_reentry_allowed(
        enabled=False, ignition_ok=True, ignition_exemptions=0, max_ignition_exemptions=1)
    assert ok is False
    assert reason == "flag_off"


def test_cold_tape_never_grants():
    ok, reason = fresh_ignition_reentry_allowed(
        enabled=True, ignition_ok=False, ignition_exemptions=0, max_ignition_exemptions=1)
    assert ok is False
    assert reason == "no_fresh_ignition"


def test_exhausted_budget_never_grants():
    ok, reason = fresh_ignition_reentry_allowed(
        enabled=True, ignition_ok=True, ignition_exemptions=1, max_ignition_exemptions=1)
    assert ok is False
    assert reason == "max_ignition_exemptions_reached"


def test_zero_budget_never_grants():
    ok, reason = fresh_ignition_reentry_allowed(
        enabled=True, ignition_ok=True, ignition_exemptions=0, max_ignition_exemptions=0)
    assert ok is False
    assert reason == "no_exemption_budget"


def test_bad_basis_fails_closed():
    ok, reason = fresh_ignition_reentry_allowed(
        enabled=True, ignition_ok=True,
        ignition_exemptions="x", max_ignition_exemptions=1)  # type: ignore[arg-type]
    assert ok is False
    assert reason == "bad_basis_fail_closed"


@pytest.mark.parametrize("used,cap,expect", [(0, 2, True), (1, 2, True), (2, 2, False)])
def test_budget_boundary(used, cap, expect):
    ok, _ = fresh_ignition_reentry_allowed(
        enabled=True, ignition_ok=True, ignition_exemptions=used, max_ignition_exemptions=cap)
    assert ok is expect


# ── FSM wiring asserts (source-level, same idiom as structural_trigger_reasons) ──

def _runner_src() -> str:
    from app.services.trading.momentum_neural import live_runner

    return inspect.getsource(live_runner)


def test_fsm_block_exists_and_emits_event():
    src = _runner_src()
    assert "chili_momentum_fresh_ignition_reentry_bypass_enabled" in src
    assert "live_reentry_cap_ignition_exempt" in src
    assert "fresh_ignition_reentry_allowed(" in src  # the pure helper is consulted


def test_fsm_block_sits_before_terminalization_and_after_leader_exempt():
    src = _runner_src()
    i_leader = src.index("live_reentry_cap_leader_exempt")
    i_ign = src.index("live_reentry_cap_ignition_exempt")
    i_capped = src.index("live_reentry_capped")
    assert i_leader < i_ign < i_capped, (
        "ignition exemption must sit AFTER the leader exemption and BEFORE the "
        "terminal live_reentry_capped emit"
    )


def test_fsm_reads_both_ignition_legs():
    src = _runner_src()
    blk_start = src.index("chili_momentum_fresh_ignition_reentry_bypass_enabled")
    blk = src[blk_start:blk_start + 4000]
    assert "tape_confirms_hold" in blk          # leg 1: executed-tape confirmer
    assert "tape_running_up_signal_map" in blk  # leg 2: running-up burst map
    assert "fail-closed" in blk or "FAIL-CLOSED" in blk
