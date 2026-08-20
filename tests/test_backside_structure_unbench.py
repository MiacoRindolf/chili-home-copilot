"""BACKSIDE STRUCTURE-AFTER-PULLBACK EXCEPTION — ang YJ +$3,000 (2026-08-19).

Ang sticky bench ay nagla-latch sa TAAS para pigilan tayong HUMABOL sa pangalang
tumakbo na — tama iyon. Pero pagkatapos ay vinevet nito ang PULLBACK entry, na
siya namang setup na aktwal na tina-trade ni Ross, at ang tanging umiiral na
labasan ay nangangailangan ng BUONG VWAP round-trip.

Ni-replay sa naitalang tape, YJ 13:13-13:22Z: **519 hakbang, ZERO entry, 460 bench
veto**, payload:

    reason=benched_backside_chasing_top   benched_at_hod=6.30
    blocked_trigger=double_bottom_break_tick_ok

Isang TUNAY na structure trigger ang pumutok at kinain ito ng bench, habang
`vwap_reclaim_not_below_enough` ay pumutok nang 518 beses dahil hindi bumaba nang
sapat ang curl sa ilalim ng VWAP para makamit ang umiiral na exception.

⚠️ SAVE ito, hindi entry: nananatiling naka-latch ang bench marker, at ang bawat
downstream chase-guard, extension veto, bid-prop confirmer, spread at risk gate
ay tumatakbo pa rin.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.live_runner import (
    structural_trigger_reasons,
)


def _exception_fires(*, trigger, hod, price, enabled=True, min_pct=3.0):
    """Ang eksaktong lohika ng exception sa veto site."""
    if not enabled:
        return False
    if trigger not in structural_trigger_reasons():
        return False
    if not hod or not price or float(hod) <= 0:
        return False
    retrace = (float(hod) - float(price)) / float(hod) * 100.0
    return retrace >= float(min_pct)


# ─────────────────── ang TUNAY na sandali ng YJ ───────────────────


def test_yj_curl_is_preserved():
    """Ang mismong na-block na trigger sa replay: double_bottom_break_tick_ok sa
    ~5.50, benched sa HOD 6.30 = 12.7% retrace -> DAPAT MA-PRESERVE."""
    assert _exception_fires(
        trigger="double_bottom_break_tick_ok", hod=6.30, price=5.50
    ) is True


def test_chase_at_the_high_is_still_vetoed():
    """ANG MAHALAGA: ang trigger na pumuputok MISMO sa taas ay tinatanggihan pa
    rin — iyon ang chase na ipinagbabawal ng bench."""
    assert _exception_fires(
        trigger="double_bottom_break_tick_ok", hod=6.30, price=6.28
    ) is False
    # 2% pa lang ang layo -> kulang.
    assert _exception_fires(
        trigger="double_bottom_break_tick_ok", hod=6.30, price=6.17
    ) is False


def test_non_structural_trigger_is_never_preserved():
    """Ang exception ay para LANG sa structural trigger (may pullback_low, kaya
    may structural stop). Ang iba ay tinatanggihan pa rin kahit malalim ang dip."""
    assert _exception_fires(
        trigger="momentum_ok_rel_vol", hod=6.30, price=5.00
    ) is False
    assert _exception_fires(
        trigger="score_only", hod=6.30, price=4.00
    ) is False


def test_flag_off_restores_the_veto():
    assert _exception_fires(
        trigger="double_bottom_break_tick_ok", hod=6.30, price=5.50, enabled=False
    ) is False


def test_missing_or_bad_inputs_fail_closed():
    """Walang HOD / walang presyo / zero HOD -> panatilihin ang veto."""
    for hod, px in ((None, 5.5), (6.30, None), (0.0, 5.5), (None, None)):
        assert _exception_fires(
            trigger="double_bottom_break_tick_ok", hod=hod, price=px
        ) is False


@pytest.mark.parametrize(
    "trigger",
    ["pullback_break_tick_ok", "hod_break_tick_ok", "abcd_break_tick_ok",
     "first_pullback_tick_ok", "wick_reclaim"],
)
def test_every_structural_family_is_eligible(trigger):
    """Lahat ng structural family ay may parehong pagtrato — walang espesyal na
    kaso na madaling malimutan kapag nadagdagan ang tuple."""
    assert _exception_fires(trigger=trigger, hod=10.0, price=9.0) is True


def test_settings_are_wired_and_bounded():
    assert getattr(
        settings, "chili_momentum_backside_structure_unbench_enabled", None
    ) is True
    v = float(
        getattr(settings, "chili_momentum_backside_unbench_min_retrace_pct", -1)
    )
    assert 0.0 <= v <= 90.0
    # Kailangang saklawin ang tunay na 12.7% na retrace ng YJ...
    assert v <= 12.7
    # ...pero hindi zero, kung hindi ay puputok ito isang tick sa ilalim ng taas.
    assert v > 0.0
