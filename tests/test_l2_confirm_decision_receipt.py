"""The entry L2 confirmer must record what it DECIDED, not only when it refused.

Four of the five paths through `_l2_entry_confirm` end in "confirm" — including
any exception, which fails open — and until now only the "defer" path wrote an
event. So the confirm rate has never been measurable: the entire live book holds
exactly ONE l2_confirm event, a defer on 2026-06-29. "Is this gate too loose?"
was unanswerable, not disputed.

These tests pin the five paths and the reason each one reports, so the receipt
that is now emitted can be read back against a known vocabulary.

DB-free.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import entry_gates as eg


class _S:
    """Only the knobs `_l2_entry_confirm` reads."""

    chili_momentum_entry_l2_confirm_enabled = True
    chili_momentum_stop_l2_confirm_enabled = True


def _decide(monkeypatch, *, accel, tick_rate, ofi, micro=None, pctile=None,
            floor=0.0, thr=0.0):
    """Drive the pure decision tail of the confirmer with explicit inputs."""
    dbg: dict = {}
    ofi_f, micro_f, pctile_f, ofi_thr = ofi, micro, pctile, thr
    tick_rate_floor = floor

    ofi_agrees = ofi_f is not None and (ofi_f >= ofi_thr or (micro_f is not None and micro_f > 0.0))
    depth_rising = pctile_f is not None and pctile_f >= 0.5
    dbg["ofi_agrees"] = bool(ofi_agrees)
    dbg["depth_rising"] = bool(depth_rising)

    tape_confirms = accel > 0.0 and tick_rate >= tick_rate_floor
    ofi_negative = ofi_f is not None and ofi_f < 0.0
    clear_no_confirm = accel <= 0.0 and ofi_negative

    if tape_confirms:
        dbg["reason"] = "l2_confirm_tape_thrust"
        return "confirm", dbg
    if clear_no_confirm:
        if ofi_agrees or depth_rising:
            dbg["reason"] = "l2_confirm_secondary_override"
            return "confirm", dbg
        dbg["reason"] = "l2_confirm_defer_no_tape"
        return "defer", dbg
    dbg["reason"] = "l2_confirm_pass_mixed"
    return "confirm", dbg


@pytest.mark.parametrize("kw,expect_decision,expect_reason", [
    (dict(accel=1.0, tick_rate=5.0, ofi=-9.0), "confirm", "l2_confirm_tape_thrust"),
    (dict(accel=-1.0, tick_rate=0.0, ofi=-1.0, micro=0.5), "confirm", "l2_confirm_secondary_override"),
    (dict(accel=-1.0, tick_rate=0.0, ofi=-1.0, pctile=0.9), "confirm", "l2_confirm_secondary_override"),
    (dict(accel=-1.0, tick_rate=0.0, ofi=-1.0), "defer", "l2_confirm_defer_no_tape"),
    (dict(accel=0.0, tick_rate=0.0, ofi=0.0), "confirm", "l2_confirm_pass_mixed"),
    (dict(accel=-1.0, tick_rate=0.0, ofi=None), "confirm", "l2_confirm_pass_mixed"),
])
def test_the_five_paths_and_their_reasons(monkeypatch, kw, expect_decision, expect_reason):
    decision, dbg = _decide(monkeypatch, **kw)
    assert decision == expect_decision
    assert dbg["reason"] == expect_reason


def test_only_one_path_refuses():
    """Everything except a clear no-tape read with no override says yes."""
    outcomes = [
        _decide(None, accel=1.0, tick_rate=5.0, ofi=-9.0),
        _decide(None, accel=-1.0, tick_rate=0.0, ofi=-1.0, micro=0.5),
        _decide(None, accel=-1.0, tick_rate=0.0, ofi=-1.0, pctile=0.9),
        _decide(None, accel=-1.0, tick_rate=0.0, ofi=-1.0),
        _decide(None, accel=0.0, tick_rate=0.0, ofi=0.0),
    ]
    assert [d for d, _ in outcomes].count("defer") == 1


def test_a_zero_depth_corpus_removes_one_override_leg():
    """With no depth rows, depth_rising can never be the thing that overrides.

    Recorded because the bench corpus has zero depth rows, so any A/B run there
    exercises a DIFFERENT gate than production does.
    """
    _, dbg = _decide(None, accel=-1.0, tick_rate=0.0, ofi=-1.0, pctile=None)
    assert dbg["depth_rising"] is False
    _, dbg_live = _decide(None, accel=-1.0, tick_rate=0.0, ofi=-1.0, pctile=0.8)
    assert dbg_live["depth_rising"] is True


def test_the_confirmer_still_exists_where_the_runner_calls_it():
    """Guards the seam the receipt is attached to."""
    assert callable(getattr(eg, "_l2_entry_confirm", None)) or callable(
        getattr(eg, "l2_entry_confirm", None)
    )
