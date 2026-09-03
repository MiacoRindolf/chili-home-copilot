"""Unit tests for the price-scale checker.

Pure fakes: no database, no network.

The classifier is the whole value of this tool.  Reporting "price disagrees" is
useless on its own -- the actionable question is whether the TAPE is wrong or
the LEVELS are on another scale, and those have opposite fixes.  Dollar volume
is what separates them, because it is invariant under split adjustment while
price is not.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.hydration_price_scale_check import (  # noqa: E402
    DV_HI,
    DV_LO,
    PRICE_HI,
    PRICE_LO,
    classify,
)


def test_agreeing_price_and_volume_is_ok():
    assert classify(price_ratio=1.0, dv_ratio=1.0) == "ok"
    assert classify(price_ratio=1.4, dv_ratio=0.95) == "ok"


def test_split_is_named_when_volume_agrees_but_price_does_not():
    """The measured LGCL case: price off by 150x, dollar volume ratio 1.00.

    Dollar volume agreeing to four figures while price is off by two orders of
    magnitude can only mean one thing -- the tape holds the same trades, priced
    on a different scale. Calling this a tape defect would send someone
    re-hydrating data that is already correct.
    """
    assert classify(price_ratio=150.8, dv_ratio=1.00) == "split_adjusted_levels"
    assert classify(price_ratio=5.0, dv_ratio=1.00) == "split_adjusted_levels"
    # ...and in the inverse direction (a forward split) too.
    assert classify(price_ratio=0.04, dv_ratio=1.00) == "split_adjusted_levels"


def test_volume_disagreement_is_not_called_a_split():
    """A short tape is a COVERAGE problem and must not be mislabelled.

    If dollar volume is short, rows are missing; that is the failure the
    hydrator exists to prevent and it needs re-hydration, not a scale factor.
    """
    assert classify(price_ratio=1.0, dv_ratio=0.2) == "volume_mismatch"
    assert classify(price_ratio=50.0, dv_ratio=0.2) == "scale_and_volume_mismatch"


def test_missing_dollar_volume_downgrades_the_claim_rather_than_guessing():
    """Without the invariant there is no evidence for WHICH hypothesis holds,
    so the status must say the diagnosis is unconfirmed instead of asserting a
    split."""
    assert classify(price_ratio=150.0, dv_ratio=None) == "price_scale_mismatch_unconfirmed"
    assert classify(price_ratio=1.0, dv_ratio=None) == "ok"


def test_bands_are_wide_enough_to_ignore_ordinary_disagreement():
    """`move_high` is a sustained high and the tape statistic is a median print;
    they legitimately differ. Only an order-of-magnitude error should trip."""
    assert PRICE_LO <= 0.6 <= PRICE_HI
    assert PRICE_LO <= 1.9 <= PRICE_HI
    assert classify(price_ratio=1.9, dv_ratio=1.0) == "ok"
    assert classify(price_ratio=0.6, dv_ratio=1.0) == "ok"
    # but a 5x gap is not ordinary
    assert classify(price_ratio=5.0, dv_ratio=1.0) != "ok"


def test_dollar_volume_band_tolerates_a_slightly_different_session_window():
    assert DV_LO <= 0.75 <= DV_HI
    assert DV_LO <= 1.3 <= DV_HI
    assert classify(price_ratio=1.0, dv_ratio=1.3) == "ok"
