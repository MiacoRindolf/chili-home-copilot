"""L8 — kondisyonal na late/AH monster placement (pure scalar helper, walang I/O).

Dissection 2026-08-01 (sealed L8 proof + instrumented probe): ang A2 schedule
×0.0 sa late/afterhours + pending-park bug ang tunay na pumapatay sa monster
windows — ang JEM 06-30 window ay 100% afterhours (candidate na-park isang oras
bago ang day high), ang JLHL 07-09 ay placeable lang sa unang 5 minuto. Scalar
ang helper (ang caller ang nagme-memoize ng session_low kada minuto) dahil ang
per-attempt na OHLCV fetch ang nag-timeout sa unang proof attempt. Ang mga
fixture ay ang measured na mga kaso: JEM abcd_break sa AH monster (bukas, ×0.5),
random AH chop (sarado — ang 1W/11L −$72.65 class na pinagmulan ng ×0.0).
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.entry_gates import (
    late_window_monster_placement_mult,
)


def _mult(**kw):
    # JEM 06-30 measured geometry: session low 3.94, px 9.3 → uol 2.36.
    base = dict(
        window="afterhours",
        trigger_reason="abcd_break",
        live_price=9.3,
        session_low=3.94,
        enabled=True,
        up_off_low_floor=1.5,
        monster_mult=0.5,
    )
    base.update(kw)
    return late_window_monster_placement_mult(**base)


def test_jem_ah_monster_structural_opens_at_half_size():
    m, dbg = _mult()
    assert m == 0.5 and dbg["opened"] is True


def test_late_band_and_double_bottom_also_open():
    m, dbg = _mult(window="late", trigger_reason="double_bottom_break_tick_ok")
    assert m == 0.5 and dbg["opened"] is True


def test_random_ah_chop_stays_blocked_not_monster():
    # uol = 2.05/1.98 ≈ 1.035 < 1.5 — ang lumang AH-loss class ay nananatiling sarado.
    m, dbg = _mult(live_price=2.05, session_low=1.98)
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
        {"session_low": None},
        {"session_low": 0.0},
        {"monster_mult": 0.0},
    ):
        m, dbg = _mult(**kw)
        assert m == 0.0, kw


def test_all_structural_families_open():
    for trig in (
        "flush_dip_buy", "vwap_reclaim", "raw_break",
        "abcd_break", "abcd_break_tick_ok",
        "double_bottom_break", "double_bottom_break_tick_ok",
    ):
        m, dbg = _mult(trigger_reason=trig)
        assert m == 0.5, trig


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
