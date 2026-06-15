"""Replay v2 selection->entry alignment: the as-of arming must mirror the live
auto_arm's fresh-impulse discipline (drop FADED 24h leaders unless their trigger is
FIRING). Parity by construction — the gate reuses the SAME
``ross_momentum.intraday_impulse_freshness`` helper + threshold the live auto_arm
calls (auto_arm._candidate_freshness / _known_fresh / _require_fresh_impulse).

These are pure-frame unit tests (no DB tape / no network): they pin the parity rule
the replay's arming gate delegates to, so a future edit that breaks the firing/
fresh/faded contract trips here.
"""
from __future__ import annotations

import pandas as pd

import app.services.trading.momentum_neural.replay_v2 as rv
from app.services.trading.momentum_neural.ross_momentum import intraday_impulse_freshness


def _fresh_frame() -> pd.DataFrame:
    """A clean up-impulse closing near its recent high (is_fresh True)."""
    highs = list(range(10, 24))
    return pd.DataFrame(
        {
            "High": highs,
            "Low": [h - 1 for h in highs],
            "Close": [h - 0.5 for h in highs[:-1]] + [22.9],
        }
    )


def _faded_frame() -> pd.DataFrame:
    """Ran up then rolled back into the lower portion of its range (faded)."""
    return pd.DataFrame(
        {
            "High": [20, 21, 22, 23, 24, 25, 24, 23, 22, 21, 20, 19, 18, 17],
            "Low": [19, 20, 21, 22, 23, 24, 23, 22, 21, 20, 19, 18, 17, 15],
            "Close": [19.5, 20.5, 21.5, 22.5, 23.5, 24.5, 23, 22, 21, 20, 19, 18, 17, 15.2],
        }
    )


def test_freshness_helper_classifies_fresh_vs_faded():
    # Anchors the test fixtures to the SAME helper the live auto_arm uses.
    assert intraday_impulse_freshness(_fresh_frame()).is_fresh is True
    assert intraday_impulse_freshness(_faded_frame()).is_fresh is False


def test_fresh_name_is_armed():
    # Rule 2: a name positively known to be in a fresh up-impulse is watched.
    assert rv.freshness_arm_decision(_fresh_frame(), firing=False) is True


def test_faded_name_is_dropped_when_not_firing():
    # The core fix: a faded 24h leader is NOT pinned to the watch slot.
    assert rv.freshness_arm_decision(_faded_frame(), firing=False) is False


def test_firing_break_always_armable_even_if_faded():
    # Rule 1: a FIRING break is always a valid arm, regardless of freshness — exactly
    # the live auto_arm rule ('a name whose break is FIRING now is always valid').
    assert rv.freshness_arm_decision(_faded_frame(), firing=True) is True
    assert rv.freshness_arm_decision(_fresh_frame(), firing=True) is True


def test_unknown_freshness_not_armed_on_freshness_alone():
    # Rule 3: an unusable frame -> is_fresh False -> not armed unless firing (mirrors
    # live _known_fresh treating None/unknown as not-fresh).
    empty = pd.DataFrame({"High": [], "Low": [], "Close": []})
    assert rv.freshness_arm_decision(empty, firing=False) is False
    assert rv.freshness_arm_decision(empty, firing=True) is True


def test_threshold_matches_live_auto_arm_default():
    # The replay's freshness cutoff must equal the live auto_arm's
    # (_freshness_retracement_threshold default 0.50) so the two share one definition
    # of 'shallow' — parity by construction.
    assert rv.FRESHNESS_RETRACEMENT_THRESHOLD == 0.50


def test_filter_default_on_is_faithful():
    # ON by default = faithful (the reversible knob defaults to the parity behavior).
    assert rv.REPLAY_FRESHNESS_FILTER is True
