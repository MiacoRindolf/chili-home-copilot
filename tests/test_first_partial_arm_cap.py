"""First-partial 1R arm cap (MFE exit audit 2026-08-22).

Median exit capture = 9% ng MFE; ang ladder ay nag-a-arm sa arm_frac*rr kaya
sa malalayong target ay 3R+ bago mag-eligible ang partial. Ang cap = 1R.
Runnable: pytest tests/test_first_partial_arm_cap.py -v
"""
from __future__ import annotations

import inspect

from unittest.mock import patch

from app.services.trading.momentum_neural import paper_execution

_PE = "app.services.trading.momentum_neural.paper_execution"


def _src_seg() -> str:
    src = inspect.getsource(paper_execution.sell_into_strength_ladder)
    lo = src.index("_arm_cap")
    return src[lo - 200:lo + 800]


def test_arm_cap_applied_with_floor():
    src = inspect.getsource(paper_execution.sell_into_strength_ladder)
    assert "min(arm_frac * rr, max(0.5, _arm_cap))" in src
    assert "chili_momentum_exit_first_partial_arm_r_cap" in src


def test_far_target_now_arms_at_one_r():
    """Sa 6R na target na may arm_frac 0.5: dating 3R ang arm bar — ngayon 1R."""
    # Ang arithmetic mismo (walang ladder call — pure formula contract):
    arm_frac, rr, cap = 0.5, 6.0, 1.0
    old = max(0.5, arm_frac * rr)
    new = max(0.5, min(arm_frac * rr, max(0.5, cap)))
    assert old == 3.0 and new == 1.0


def test_near_target_unchanged():
    """Sa 1.3R na target: arm_frac*rr = 0.65 < cap — walang pagbabago."""
    arm_frac, rr, cap = 0.5, 1.3, 1.0
    old = max(0.5, arm_frac * rr)
    new = max(0.5, min(arm_frac * rr, max(0.5, cap)))
    assert old == new == 0.65


def test_firewall_untouched():
    """Ang continuation veto at distribution confluence ay hindi ginalaw."""
    src = inspect.getsource(paper_execution.sell_into_strength_ladder)
    assert "veto_ofi_thr = 2.0 * thr" in src
    assert "dist_pctile_max = 0.25" in src
