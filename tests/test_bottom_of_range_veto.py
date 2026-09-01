"""Bottom-of-range entry veto (#1262) — doktrina ni Ross 2026-09-01 07:11 ET.

"WETO this one popped up yesterday and then sold off. Went red on the day and
it's just popping up again off the low. THERE'S NOTHING THERE."

SINUKAT sa LAHAT ng 4 na live entry natin (08-31..09-01): bawat isa ay pumasok
sa ILALIM NA KWARTO ng day range — RDHL 0.23 (−2.05), GYGY 0.28 (−29.20),
SSM 0.25 (−25.98), AUUD 0.21 (−44.01). 4/4 talo, −101.24. Bumibili tayo
malapit sa MABABA; si Ross ay bumibili malapit sa TAAS.

Ang range ay dapat makahulugan bago tumawag ng "ilalim" — walang veto kapag
maliit pa ang day range (maagang bahagi ng galaw = ingay ang posisyon).

Runnable: pytest tests/test_bottom_of_range_veto.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.entry_gates import (
    bottom_of_range_entry_veto,
)


def _call(**over):
    # WETO-class: day 7.10–8.20 (15.5% range), entry 7.35 => pos 0.23
    kw = dict(
        enabled=True,
        price=7.35,
        day_low=7.10,
        day_high=8.20,
        min_range_pct=8.0,
        pos_floor=0.35,
    )
    kw.update(over)
    return bottom_of_range_entry_veto(**kw)


def test_bottom_quartile_is_vetoed():
    veto, dbg = _call()
    assert veto is True
    assert dbg["pos_in_range"] < 0.35
    assert dbg["day_range_pct"] > 8.0


def test_near_high_passes():
    """Breakout entry malapit sa taas — ito ang gusto natin."""
    veto, dbg = _call(price=8.10)
    assert veto is False
    assert dbg["pos_in_range"] > 0.9


def test_mid_range_passes():
    veto, _ = _call(price=7.75)   # pos ~0.59
    assert veto is False


def test_small_range_is_skipped():
    """Maagang galaw: 2% na range ⇒ ang posisyon ay ingay, walang veto."""
    veto, dbg = _call(day_low=7.10, day_high=7.24, price=7.11)
    assert veto is False
    assert dbg["skipped"] == "range_not_meaningful_yet"


def test_flag_off_is_noop():
    assert _call(enabled=False)[0] is False


def test_missing_data_fails_open():
    assert _call(price=None)[0] is False
    assert _call(day_low=None)[0] is False
    assert _call(day_high=None)[0] is False
    assert _call(day_low=8.0, day_high=7.0)[0] is False  # baligtad


def test_all_four_live_losers_are_vetoed():
    """Ang eksaktong apat na sinukat — lahat dapat ma-veto."""
    cases = [
        ("RDHL", 0.23), ("GYGY", 0.28), ("SSM", 0.25), ("AUUD", 0.21),
    ]
    for sym, pos in cases:
        lo, hi = 1.00, 2.00
        px = lo + pos * (hi - lo)
        veto, dbg = _call(price=px, day_low=lo, day_high=hi)
        assert veto is True, f"{sym} pos={pos} ay dapat na-veto"


def test_floor_is_configurable():
    veto, _ = _call(pos_floor=0.10)   # mas mababang bar
    assert veto is False
