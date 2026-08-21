"""HUIZ 2026-08-20 second-leg lockout — green recycle must RESET the strike counter.

Ang stopout_cycles ay bumibilang ng CONSECUTIVE futility sa isang chopper; ang
panalong recycle ay nagpapatunay na tapos na ang chop, kaya hindi dapat manatili
ang mga strike papunta sa continuation leg (HUIZ: 3 chop stopouts 12:03-12:08 →
vertical NAGBAYAD → session na-lock sa 12:14 → naiwan ang 12:20 leg sa $4.18).
PURE (no DB). Runnable: pytest tests/test_stopout_green_reset.py -v
"""
from app.services.trading.momentum_neural.risk_policy import (
    reentry_after_stop_allowed,
    stopout_cycles_after_recycle,
)


def test_loss_recycle_increments():
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=0, recycle_was_stopout=True
    ) == 1
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=2, recycle_was_stopout=True
    ) == 3


def test_green_recycle_resets_to_zero():
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=3, recycle_was_stopout=False
    ) == 0
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=1, recycle_was_stopout=False
    ) == 0


def test_first_cycle_green_stays_zero():
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=0, recycle_was_stopout=False
    ) == 0


def test_bad_basis_fails_safe():
    # Basura na prev -> itrato bilang 0 (huwag kailanman mag-crash sa recycle path).
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=None, recycle_was_stopout=True  # type: ignore[arg-type]
    ) == 1
    assert stopout_cycles_after_recycle(
        prev_stopout_cycles=-4, recycle_was_stopout=True
    ) == 1


def test_huiz_doctrinal_sequence():
    """Ang mismong HUIZ sequence: 3 chop strikes -> winner -> DAPAT payagan ulit."""
    c = 0
    for _ in range(3):  # trades 2-4: bailout/stop chop
        c = stopout_cycles_after_recycle(
            prev_stopout_cycles=c, recycle_was_stopout=True
        )
    # DATI: c=3 -> kandado bago pa ang winner recycle.
    allowed_old, reason_old = reentry_after_stop_allowed(
        enabled=True, stopout_cycles=c, max_stopout_reentries=3
    )
    assert allowed_old is False and reason_old == "max_stopout_reentries_reached"
    # Trade 5 = ang vertical winner -> green recycle -> reset.
    c = stopout_cycles_after_recycle(prev_stopout_cycles=c, recycle_was_stopout=False)
    allowed_new, reason_new = reentry_after_stop_allowed(
        enabled=True, stopout_cycles=c, max_stopout_reentries=3
    )
    assert allowed_new is True and reason_new == "allowed"


def test_pure_chopper_still_terminalizes():
    """Walang panalo kailanman -> ang cap ay pumapasok pa rin nang eksakto tulad ng dati."""
    c = 0
    for _ in range(3):
        c = stopout_cycles_after_recycle(
            prev_stopout_cycles=c, recycle_was_stopout=True
        )
    allowed, reason = reentry_after_stop_allowed(
        enabled=True, stopout_cycles=c, max_stopout_reentries=3
    )
    assert allowed is False and reason == "max_stopout_reentries_reached"
