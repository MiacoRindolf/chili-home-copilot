"""BACKSIDE STRUCTURE-AFTER-PULLBACK EXCEPTION — ang YJ +$3,000 (2026-08-19) — bilang RESIBO.

Ang sticky bench ay nagla-latch sa TAAS para pigilan tayong HUMABOL sa pangalang tumakbo na.
Pagkatapos ay vinevet nito ang PULLBACK entry, na siya namang setup ni Ross; ang exception na
ito ang nagpreserba sa structural trigger na nag-retrace sa loob ng #1274 window (3%..12%),
kasama ang #1256 VWAP-hold leg.

Ni-replay sa naitalang tape, YJ 13:13-13:22Z: **519 hakbang, ZERO entry, 460 bench veto**,
payload ``reason=benched_backside_chasing_top benched_at_hod=6.30
blocked_trigger=double_bottom_break_tick_ok``.

⚠️ [56] (2026-09-11): WALA NANG VETO. Ang trigger na pumutok habang benched ay PINAPANATILI sa
bawat kaso; ang lohikang ito ay nasa ``live_runner._backside_bench_legacy_structure_verdict`` at
isinusulat LAMANG ang ``legacy_verdict`` ng resibo — kung ano ang GINAWA SANA ng lumang code.

REVIEW FIX: ang dating anyo ng file na ito ay may SARILING kopya ng exception (``_exception_fires``)
at nag-a-assert na "ang chase sa taas ay vinevet pa rin" — pumapasa pa rin kahit wala nang
tumatanggi sa runner. Ngayon ay DINADRAYB ang tunay na function, at ang mga assertion ay tungkol
sa HATOL na nire-record, hindi sa pasok na hinaharangan.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_runner import structural_trigger_reasons


def _verdict(*, trigger, hod, price, bench_dbg=None) -> tuple[bool, dict[str, Any]]:
    le: dict[str, Any] = {} if hod is None else {"benched_backside_hod": hod}
    return lr._backside_bench_legacy_structure_verdict(
        le, bench_dbg=bench_dbg, trigger_reason=trigger, bench_px=price,
    )


# ─────────────────── ang TUNAY na sandali ng YJ ───────────────────


def test_yj_curl_would_have_been_preserved():
    """double_bottom_break_tick_ok sa ~5.50, benched sa HOD 6.30 = 12.7% retrace. Sa #1274
    window (max 12%) ay LAMPAS ito — kaya ang lumang code (pagkatapos ng #1274) ay nag-veto
    sana; ang resibo ay nagsasabi niyon at ang sukat ay nasa dbg."""
    preserved, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=6.30, price=5.50)
    assert dbg["retrace_pct"] == pytest.approx(12.7, abs=0.01)
    max_pct = float(settings.chili_momentum_backside_unbench_max_retrace_pct)
    assert preserved is (max_pct <= 0.0 or dbg["retrace_pct"] <= max_pct)


def test_a_pullback_inside_the_window_records_structure_exception():
    preserved, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=10.0, price=9.0)
    assert preserved is True
    assert dbg["retrace_pct"] == pytest.approx(10.0)
    assert dbg["min_retrace_pct"] == pytest.approx(
        float(settings.chili_momentum_backside_unbench_min_retrace_pct))


def test_a_chase_at_the_high_records_a_veto_verdict_only():
    """ANG DATING "chase at the high is still vetoed": ngayon ay HATOL lamang — ang pasok ay
    pinapanatili ng runner (tingnan ang test_backside_bench_conditions_not_vetoes_56)."""
    for price in (6.28, 6.17):
        preserved, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=6.30, price=price)
        assert preserved is False
        assert dbg["retrace_pct"] < dbg["min_retrace_pct"]


def test_a_non_structural_trigger_records_a_veto_verdict():
    for trig in ("momentum_ok_rel_vol", "score_only"):
        preserved, dbg = _verdict(trigger=trig, hod=6.30, price=5.00)
        assert preserved is False
        assert dbg == {"structural_trigger": False}


def test_a_deep_fade_past_the_1274_window_records_a_veto_verdict():
    preserved, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=10.0, price=7.0)
    assert preserved is False and dbg["retrace_pct"] == pytest.approx(30.0)


def test_the_1256_vwap_hold_leg_still_shapes_the_verdict(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0)
    inside = {"vwap_reclaim_declined": {"session_vwap": 9.5}}
    below, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=10.0, price=9.0, bench_dbg=inside)
    assert below is False and dbg["vwap_hold"] is False and dbg["session_vwap"] == 9.5
    held, dbg2 = _verdict(trigger="double_bottom_break_tick_ok", hod=10.0, price=9.0,
                          bench_dbg={"vwap_reclaim_declined": {"session_vwap": 8.8}})
    assert held is True and dbg2["vwap_hold"] is True


def test_flag_off_records_a_veto_verdict(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_structure_unbench_enabled", False)
    preserved, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=10.0, price=9.0)
    assert preserved is False and dbg == {"structure_unbench_enabled": False}


def test_missing_or_bad_inputs_record_the_old_fail_closed_verdict():
    for hod, px in ((None, 5.5), (6.30, None), (0.0, 5.5), (None, None)):
        preserved, dbg = _verdict(trigger="double_bottom_break_tick_ok", hod=hod, price=px)
        assert preserved is False and dbg.get("inputs_unreadable") is True


@pytest.mark.parametrize(
    "trigger",
    ["pullback_break_tick_ok", "hod_break_tick_ok", "abcd_break_tick_ok",
     "first_pullback_tick_ok", "wick_reclaim"],
)
def test_every_structural_family_is_eligible(trigger):
    assert trigger in structural_trigger_reasons()
    preserved, _dbg = _verdict(trigger=trigger, hod=10.0, price=9.0)
    assert preserved is True


def test_settings_are_wired_and_bounded():
    assert getattr(settings, "chili_momentum_backside_structure_unbench_enabled", None) is True
    v = float(getattr(settings, "chili_momentum_backside_unbench_min_retrace_pct", -1))
    assert 0.0 < v <= 12.7
