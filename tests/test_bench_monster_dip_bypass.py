"""L8 — structural monster-dip bench bypass (pure helper, walang I/O).

Bench study 2026-08-01 (4-window golden-tape sim, JEM/JLHL unlock vs LHSW/JZXN
guards): ang purong day-geometry ay BALIKTAD ang selectivity bilang bypass key
(true sa near-anchor hovers na dump class, false sa deep winner dips), kaya ang
NECESSARY key ay STRUCTURE — ang fired trigger ay kailangang mula sa dip-reclaim
families. Ang mga fixture dito ay ang mismong measured na mga kaso: JEM E3
dip→reclaim (admit), JEM 22:17 near-anchor hover (no_real_discount), LHSW
late-day fade (rolled_over), JZXN fade (not_monster_day), below-VWAP hover
(level-test ay hindi kailanman bypass; ang vwap_reclaim cross ay exempt).
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import bench_monster_dip_bypass


def _frame(closes, *, low_min, high_max, vols):
    idx = pd.date_range("2026-06-30 14:00", periods=len(closes), freq="5min")
    highs = [min(c + 0.15, high_max) for c in closes]
    highs[closes.index(max(closes))] = high_max
    lows = [max(c - 0.15, low_min) for c in closes]
    lows[closes.index(min(closes))] = low_min
    return pd.DataFrame(
        {"High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _jem_frame(*, vol_shape="bottom"):
    # JEM 06-30 measured shape: open ~4, run sa HOD 12.95, fade sa ~9.3;
    # day low 3.94. Ang vol_shape ang nagpoposisyon ng session VWAP:
    # "bottom" (mabigat sa baba) → VWAP ~6.5 (px sa ibabaw);
    # "top" (mabigat sa taas) → VWAP ~11 (px sa ilalim = hover class).
    closes = [4.0, 5.5, 8.0, 11.0, 12.9, 12.0, 10.8, 9.9, 9.3]
    vols = [50, 30, 10, 5, 5, 5, 5, 5, 5] if vol_shape == "bottom" else [5, 5, 5, 50, 50, 10, 5, 5, 5]
    return _frame(closes, low_min=3.94, high_max=12.95, vols=vols)


def _bypass(df, **kw):
    base = dict(
        benched_at_hod=12.95,
        live_price=9.26,
        trigger_reason="flush_dip_buy",
        enabled=True,
        up_off_low_floor=1.5,
        min_discount_frac=0.10,
        day_retrace_ceiling=0.50,
        vwap_hold_buffer=0.0,
    )
    base.update(kw)
    return bench_monster_dip_bypass(df, **base)


def test_jem_e3_reclaim_admitted():
    # E3 dip 8.71→reclaim 9.26 sa anchor 12.95: uol 2.35, discount 28.5%,
    # day_retrace 0.41, px sa ibabaw ng bottom-weighted VWAP → BYPASS.
    ok, dbg = _bypass(_jem_frame())
    assert ok is True and dbg["bypass"] is True


def test_structure_is_necessary():
    # JEM E2 hover: pumasa ang LAHAT ng geometry pero walang structural fire —
    # ang non-structural trigger ay hindi kailanman bypass.
    ok, dbg = _bypass(_jem_frame(), trigger_reason="momentum_continuation")
    assert ok is False and dbg["reject"] == "non_structural_trigger"


def test_near_anchor_hover_rejected():
    # JEM 22:17 class: px 11.87 vs anchor 12.95 = 8.3% discount < 10% floor.
    ok, dbg = _bypass(_jem_frame(), live_price=11.87)
    assert ok is False and dbg["reject"] == "no_real_discount"


def test_rolled_over_rejected():
    # LHSW late-day class: uol 1.65 pasado, discount pasado, pero ibinigay na
    # ang 0.71 ng day range → rolled_over.
    df = _frame([4.2, 6.0, 9.0, 12.5, 11.0, 8.5, 7.0, 6.7, 6.6],
                low_min=4.0, high_max=13.0, vols=[50, 30, 10, 5, 5, 5, 5, 5, 5])
    ok, dbg = _bypass(df, benched_at_hod=13.0, live_price=6.6, trigger_reason="raw_break")
    assert ok is False and dbg["reject"] == "rolled_over"


def test_jzxn_fade_not_monster_day():
    # JZXN 18:50: uol 1.106 < 1.5 — ang monster floor ang gumagabay sa fade days.
    df = _frame([2.25, 2.1, 1.9, 1.7, 1.55, 1.5, 1.57],
                low_min=1.42, high_max=2.38, vols=[10, 10, 10, 10, 10, 10, 10])
    ok, dbg = _bypass(df, benched_at_hod=1.85, live_price=1.57, trigger_reason="vwap_reclaim")
    assert ok is False and dbg["reject"] == "not_monster_day"


def test_below_vwap_hover_never_bypasses_but_cross_exempt():
    top_weighted = _jem_frame(vol_shape="top")
    ok, dbg = _bypass(top_weighted, trigger_reason="flush_dip_buy")
    assert ok is False and dbg["reject"] == "below_vwap_hover"
    # Ang vwap_reclaim trigger AY ang cross mismo — exempt sa level check.
    ok2, dbg2 = _bypass(top_weighted, trigger_reason="vwap_reclaim")
    assert ok2 is True and dbg2["bypass"] is True


def test_flag_off_never_bypasses():
    ok, dbg = _bypass(_jem_frame(), enabled=False)
    assert ok is False


def test_fail_toward_legacy_on_bad_inputs():
    for kw in (
        {"benched_at_hod": None},
        {"live_price": None},
        {"live_price": 0.0},
        {"trigger_reason": None},
    ):
        ok, dbg = _bypass(_jem_frame(), **kw)
        assert ok is False, kw
    ok, dbg = _bypass(None)  # walang frame → veto stays
    assert ok is False


@pytest.mark.parametrize(
    "kw,reject",
    [
        ({"min_discount_frac": 0.30}, "no_real_discount"),   # 28.5% < 30%
        ({"day_retrace_ceiling": 0.30}, "rolled_over"),      # 0.41 > 0.30
        ({"up_off_low_floor": 2.5}, "not_monster_day"),      # 2.35 < 2.5
    ],
)
def test_knobs_bind_verbatim(kw, reject):
    ok, dbg = _bypass(_jem_frame(), **kw)
    assert ok is False and dbg["reject"] == reject
