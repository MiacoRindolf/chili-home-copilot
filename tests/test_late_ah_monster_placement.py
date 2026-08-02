"""L8 — kondisyonal na late/AH monster placement (pure helper, walang I/O).

Dissection 2026-08-01 (sealed L8 proof + instrumented probe): ang A2 schedule
×0.0 sa late/afterhours + pending-park bug ang tunay na pumapatay sa monster
windows — ang JEM 06-30 window ay 100% afterhours (candidate na-park isang oras
bago ang day high), ang JLHL 07-09 ay placeable lang sa unang 5 minuto. Ang mga
fixture dito ay ang measured na mga kaso: JEM abcd_break sa AH monster (bukas,
×0.5), JLHL double_bottom (bukas), random AH chop na non-structural o hindi
monster (sarado — ang 1W/11L −$72.65 class na pinagmulan ng ×0.0).
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    late_window_monster_placement_mult,
)


def _frame(closes, *, low_min, vols=None):
    idx = pd.date_range("2026-06-30 14:00", periods=len(closes), freq="5min")
    lows = [max(c - 0.15, low_min) for c in closes]
    lows[closes.index(min(closes))] = low_min
    return pd.DataFrame(
        {
            "High": [c + 0.15 for c in closes],
            "Low": lows,
            "Close": closes,
            "Volume": vols or [10] * len(closes),
        },
        index=idx,
    )


# JEM 06-30 hugis: day low 3.94, burst hanggang ~12.9 — monster (uol >> 1.5).
_JEM = _frame([4.0, 5.5, 8.0, 11.0, 12.9, 12.0, 10.8, 9.9, 9.3], low_min=3.94)
# Random AH chop: flat na pangalan, uol ~1.05 — ang 1W/11L class.
_CHOP = _frame([2.05, 2.1, 2.02, 2.08, 2.0, 2.06], low_min=1.98)


def _mult(df=_JEM, **kw):
    base = dict(
        window="afterhours",
        trigger_reason="abcd_break",
        live_price=9.3,
        enabled=True,
        up_off_low_floor=1.5,
        monster_mult=0.5,
    )
    base.update(kw)
    return late_window_monster_placement_mult(df, **base)


def test_jem_ah_monster_structural_opens_at_half_size():
    m, dbg = _mult()
    assert m == 0.5 and dbg["opened"] is True


def test_late_band_and_double_bottom_also_open():
    m, dbg = _mult(window="late", trigger_reason="double_bottom_break_tick_ok")
    assert m == 0.5 and dbg["opened"] is True


def test_random_ah_chop_stays_blocked_not_monster():
    # uol = 2.05/1.98 ≈ 1.035 < 1.5 — ang lumang AH-loss class ay nananatiling sarado.
    m, dbg = _mult(df=_CHOP, live_price=2.05)
    assert m == 0.0 and dbg["reject"] == "not_monster_day"


def test_non_structural_trigger_stays_blocked():
    # momentum continuation ay hindi dip-reclaim structure — sarado kahit monster.
    m, dbg = _mult(trigger_reason="momentum_ok_tick_stream")
    assert m == 0.0 and dbg["reject"] == "non_structural_trigger"


def test_only_late_ah_windows_are_in_scope():
    for win in ("hot", "midday", "unknown", "", None):
        m, dbg = _mult(window=win)
        assert m == 0.0 and dbg["reject"] == "not_late_ah_window", win


def test_flag_off_never_opens():
    m, dbg = _mult(enabled=False)
    assert m == 0.0


def test_fail_toward_legacy_on_bad_inputs():
    for kw in (
        {"live_price": None},
        {"live_price": 0.0},
        {"trigger_reason": None},
        {"monster_mult": 0.0},
    ):
        m, dbg = _mult(**kw)
        assert m == 0.0, kw
    m, dbg = _mult(df=None)  # walang frame → blocked stays blocked
    assert m == 0.0


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({"up_off_low_floor": 3.0}, 0.0),   # 9.3/3.94=2.36 < 3.0 → sarado
        ({"monster_mult": 0.25}, 0.25),      # verbatim ang mult knob
    ],
)
def test_knobs_bind_verbatim(kw, expected):
    m, _ = _mult(**kw)
    assert m == expected
