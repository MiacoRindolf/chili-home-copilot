"""The burst-window exit must count as a STOP.

WHY THIS TEST EXISTS. ``_is_stop_class_exit_reason`` is literally
``"stop" in reason.split("_")``. That is not an accident of style: the in-code note at
live_runner.py:43609 records that ``momentum_break_stop`` was NAMED with the token on
purpose, "para awtomatikong saklaw ng lahat ng stop-class fail-open na exit guards".
The burst-window exit was added in the same block and never got the token, so
``burst_window_exit`` split to {burst, window, exit} and every consumer keyed on
stop-class quietly stood down — including the G4 re-entry escalation ladder, whose
tape re-proof (``tape_back_buy_share > 0.5``) therefore never ran at all.

WHAT THAT COST, MEASURED. WYHG 2026-09-08: eight entries in thirty minutes for
-$212.83, seven of them re-entries after an exit. The gaps were NOT quiet — 394 to 657
prints each — and the aggressor-buy share in them was 13%, 26%, 21%, 36%, 20%. Sellers
held the tape at every single re-entry, and the existing >0.5 bar would have refused
all seven. This test protects a threshold that already existed from being unreachable.

Runnable: pytest tests/test_burst_exit_is_stop_class.py -v
DB-free and import-light on purpose — this is a pure predicate.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.trading.momentum_neural.risk_policy import (
    _STOP_CLASS_EXIT_REASONS_WITHOUT_TOKEN,
    _is_stop_class_exit_reason,
    stop_class_exit_reason,
)


def test_the_burst_window_exit_now_counts_as_a_stop():
    """The whole point. Before this change it returned False."""
    assert _is_stop_class_exit_reason("burst_window_exit") is True


@pytest.mark.parametrize("reason", [
    "stop",
    "trail_stop",
    "grind_trail_stop",
    "deadman_stop",
    "momentum_break_stop",
    "stop_broker_zero_reconcile",
    "trail_stop_retry_cap_broker_zero_reconcile",
])
def test_token_carrying_reasons_are_unchanged(reason):
    """Regression: the token path must keep working exactly as before."""
    assert _is_stop_class_exit_reason(reason) is True


@pytest.mark.parametrize("reason", [
    "bailout",
    "target",
    "scale_out_target",
    "scale_out_limit",
    "max_hold",
    "kill_switch_flatten",
    "operator_flatten",
    "burst_window_entry",      # near-miss: shares two tokens, is NOT an exit
    "window_exit",             # near-miss: a subset of the real reason
    "burst_window_exit_retry",  # near-miss: decorated, NOT the exact string
])
def test_non_stop_reasons_are_still_refused(reason):
    """The exception must be exactly one string, not a prefix or a token family.

    ``burst_window_exit_retry`` is the important row: the set is matched on the WHOLE
    normalised reason, so a decorated variant does not inherit stop-class by accident.
    If such a variant is ever emitted it must be added deliberately, with its own
    measurement, exactly as this one was.
    """
    assert _is_stop_class_exit_reason(reason) is False


@pytest.mark.parametrize("reason", [None, "", "   ", "unknown_thing"])
def test_unknown_still_fails_closed(reason):
    """Unknown/None => False, the documented pre-G4 behaviour."""
    assert _is_stop_class_exit_reason(reason) is False


@pytest.mark.parametrize("reason", [
    "BURST_WINDOW_EXIT",
    "  burst_window_exit  ",
    "Burst_Window_Exit",
])
def test_case_and_whitespace_are_normalised(reason):
    """The live payload is not guaranteed to be trimmed or lower-cased."""
    assert _is_stop_class_exit_reason(reason) is True


def test_public_alias_agrees_with_the_private_one():
    """risk_policy documents this as 'the ONE stop-class classifier'. Both ends of
    the consecutive-stop contract must share one definition, so the alias may never
    drift from the implementation."""
    for reason in ("burst_window_exit", "momentum_break_stop", "bailout", None):
        assert stop_class_exit_reason(reason) is _is_stop_class_exit_reason(reason)


def test_the_exception_set_still_matches_what_live_runner_actually_emits():
    """TRIPWIRE. This exception is a STRING match against a reason produced far away
    in live_runner. If that string is ever renamed — say to ``burst_window_stop_exit``
    — the rename would make the token path work and this set entry dead, which is
    fine; but a rename to anything ELSE would silently restore the original defect
    with no failing test. So assert the producer and the classifier still agree."""
    src = Path(__file__).resolve().parents[1] / (
        "app/services/trading/momentum_neural/live_runner.py"
    )
    text = src.read_text(encoding="utf-8", errors="replace")
    emitted = set(re.findall(r'reason="(burst_window[a-z_]*)"', text))
    assert emitted, "live_runner no longer emits any burst_window* exit reason"
    for reason in emitted:
        assert _is_stop_class_exit_reason(reason) is True, (
            f"live_runner emits {reason!r} but the stop-class classifier refuses it — "
            "the WYHG defect has been reintroduced under a new name"
        )


def test_the_exception_set_is_deliberately_tiny():
    """A guard against this becoming a dumping ground. Every member costs a
    measurement; adding one without evidence is how the book stops meaning anything."""
    assert _STOP_CLASS_EXIT_REASONS_WITHOUT_TOKEN == frozenset({"burst_window_exit"})
