"""L1 — range-position chase governor decision rule (pure helper, no I/O).

Golden-baseline autopsy (2026-07-27): upper-half-of-range entries lost 17:1
(−469.50/18 trades; the ≥75% blowoff bucket 0/4) while entry PRICES were
otherwise Ross-grade. The governor promotes the frontside tilt's ADVISORY defer
to a REAL one-tick defer ONLY on positive chase evidence: strength in the
regime-adaptive defer tail AND day_range_pos at/above the floor.

Contract pins (2026-07-27 adversarial review):
- the floor binds VERBATIM — 0.0 is legal ("defer ANY weak-tail entry") and
  must never be falsy-coerced back to the 0.50 default;
- missing/None/unreadable range read ⇒ NO defer (fail toward pre-L1 behavior);
- flag off / advisory-defer false ⇒ NO defer (the flag-off parity arm in
  miniature).
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import chase_defer_decision


def _decide(**kw) -> bool:
    base = dict(
        enabled=True,
        advisory_defer=True,
        day_range_pos=0.90,
        range_pos_floor=0.50,
    )
    base.update(kw)
    return chase_defer_decision(**base)


def test_weak_tail_upper_range_defers() -> None:
    assert _decide() is True


def test_flag_off_never_defers() -> None:
    # The kill-switch parity path: everything else screams "chase" but the
    # flag is off ⇒ advisory-only, byte-identical to pre-L1.
    assert _decide(enabled=False) is False


def test_no_advisory_defer_never_defers() -> None:
    # Strong-anywhere entries are untouched — the governor only PROMOTES an
    # advisory defer that already holds, it never originates one.
    assert _decide(advisory_defer=False) is False


def test_below_floor_never_defers() -> None:
    # Weak-but-LOW (dip) entries are untouched.
    assert _decide(day_range_pos=0.30) is False


def test_exact_floor_boundary_defers() -> None:
    # The autopsy bucket boundary is INCLUSIVE (>=).
    assert _decide(day_range_pos=0.50) is True


def test_missing_range_read_never_defers() -> None:
    # Positive chase evidence only: no range read ⇒ no defer.
    assert _decide(day_range_pos=None) is False


def test_unreadable_range_read_never_defers() -> None:
    assert _decide(day_range_pos="oops") is False


def test_floor_zero_binds_verbatim() -> None:
    # 0.0 is a LEGAL operator floor ("defer ANY weak-tail entry regardless of
    # range position") — pins the `or 0.50` falsy-coercion defect against
    # regression: with the coercion, 0.05 < 0.50 would NOT defer.
    assert _decide(day_range_pos=0.05, range_pos_floor=0.0) is True
    assert _decide(day_range_pos=0.0, range_pos_floor=0.0) is True


def test_floor_one_requires_the_top_of_range() -> None:
    assert _decide(day_range_pos=0.99, range_pos_floor=1.0) is False
    assert _decide(day_range_pos=1.0, range_pos_floor=1.0) is True


@pytest.mark.parametrize("pos", [0.50, 0.75, 0.90, 1.0])
def test_autopsy_upper_half_bucket_defers(pos: float) -> None:
    assert _decide(day_range_pos=pos) is True


@pytest.mark.parametrize("pos", [0.0, 0.10, 0.30, 0.49])
def test_autopsy_lower_half_bucket_untouched(pos: float) -> None:
    assert _decide(day_range_pos=pos) is False
