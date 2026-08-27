"""A3: ang accel-ignition override sa static rvol floor (2026-08-27).

ANG EBIDENSYA (927 labelled ignitions, 4 OOS days, 11 simbolo): ang static
rvol≥5.0 floor ay nag-bench ng 319/328 CONTINUED winners (97.3%, 4/4 araw
replikado) — ang mga panalo ay nag-i-ignite mula sa IBABA ng sariling
baseline (median proxy <1.0 bawat araw). Ang ACCEL20≥3.0 na may $1k
prev-window floor ang tanging matatag-sa-lahat-ng-araw na signal (39.2%
admitted WR vs ~25% malinis na static). Ross mismo (PPCB video): "low
relative volume RISING FAST" ang unang alert.

Runnable: pytest tests/test_accel_ignition_override.py -v
"""
from __future__ import annotations

import pathlib

from app.config import settings
from app.services.trading.momentum_neural import ross_momentum as RM
from app.services.trading.momentum_neural import ignition_loop as IL


def _sig(**kw):
    base = {"vol_ratio": 1.0, "daily_change_perc": 25.0, "todays_change_perc": 25.0}
    base.update(kw)
    return base


def test_the_flags_ship_ON_with_the_evidence_recorded():
    assert settings.chili_momentum_accel_ignition_override_enabled is True
    assert float(settings.chili_momentum_accel_ignition_min_ratio) == 3.0
    assert float(settings.chili_momentum_accel_ignition_min_prev_dv_usd) == 1000.0
    desc = str(type(settings).model_fields[
        "chili_momentum_accel_ignition_override_enabled"].description or "")
    assert "319/328" in desc and "2026-08-27" in desc


def test_low_rvol_with_real_acceleration_is_not_benched():
    """ANG PANGUNAHING KASO (ang PPCB read): rvol 1.0 (mababa sa 5.0 floor)
    pero accel 4x sa may-$5k na denominator ⇒ HINDI below-floor."""
    s = _sig(accel_20s_dv=4.0, prev_20s_dv_usd=5000.0)
    assert RM.below_explosive_floor(s) is False


def test_low_rvol_without_acceleration_is_still_benched(monkeypatch):
    """Sa LEGACY mode (demotion OFF): ang floor ay buhay para sa tunay na
    walang-buhay na tape at ang mahinang accel ay hindi nag-o-override."""
    monkeypatch.setattr(
        settings, "chili_momentum_static_rvol_floor_demoted", False, raising=False)
    s = _sig(accel_20s_dv=1.1, prev_20s_dv_usd=5000.0)
    assert RM.below_explosive_floor(s) is True


def test_a_thin_tape_ratio_explosion_never_overrides(monkeypatch):
    """⚠️ LOAD-BEARING $1K FLOOR (sa legacy mode): prev20 ~ $40 na may
    5-digit na ratio = pekeng acceleration — hindi ito ang dahilan ng pagpasa
    kailanman (0824: 7/11 na ganito ay FAILED)."""
    monkeypatch.setattr(
        settings, "chili_momentum_static_rvol_floor_demoted", False, raising=False)
    s = _sig(accel_20s_dv=12000.0, prev_20s_dv_usd=40.0)
    assert RM.below_explosive_floor(s) is True


def test_missing_stamps_fail_open_to_legacy(monkeypatch):
    """Walang stamp ⇒ walang override ⇒ legacy na floor semantics (sa
    demotion-OFF mode; sa demotion-ON, tingnan ang demoted test sa ibaba)."""
    monkeypatch.setattr(
        settings, "chili_momentum_static_rvol_floor_demoted", False, raising=False)
    assert RM.below_explosive_floor(_sig()) is True


def test_the_change_floor_still_runs_even_with_acceleration():
    """⚠️ Tulad ng float_rotation: RVOL leg LANG ang nilalaktawan — ang
    pangalang halos hindi gumagalaw ay benched pa rin."""
    s = _sig(
        accel_20s_dv=5.0, prev_20s_dv_usd=5000.0,
        daily_change_perc=2.0, todays_change_perc=2.0,
    )
    assert RM.below_explosive_floor(s) is True


def test_the_knob_off_restores_legacy(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_accel_ignition_override_enabled",
        False, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_static_rvol_floor_demoted", False, raising=False)
    s = _sig(accel_20s_dv=4.0, prev_20s_dv_usd=5000.0)
    assert RM.below_explosive_floor(s) is True


def test_the_ignition_scorer_stamps_both_fields():
    src = pathlib.Path(IL.__file__).read_text(encoding="utf-8")
    i = src.index("accel_20s_dv")
    region = src[max(0, i - 2500):i + 500]
    assert "prev_20s_dv_usd" in region
    assert "interval '40 seconds'" in region, "bounded 40s query lamang"
    assert "except Exception" in region, "fail-open ang feed"
    assert "run_momentum_neural_tick" in src[i:], (
        "ang stamp ay dapat bago ang scorer"
    )


# ── Static floor demotion (2026-08-27 R-weighted GO) ─────────────────────────


def test_demoted_floor_no_longer_benches_on_static_rvol(monkeypatch):
    """ANG DEMOTION: mababang rvol, walang accel stamp — dating benched; sa
    demotion, hindi na (ang change floor pa rin ang bantay sa patay na move)."""
    s = _sig()  # rvol 1.0, +25% move, walang accel stamps
    assert RM.below_explosive_floor(s) is False


def test_demotion_off_restores_the_legacy_veto(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_static_rvol_floor_demoted",
        False, raising=False)
    assert RM.below_explosive_floor(_sig()) is True


def test_the_change_floor_survives_the_demotion():
    """⚠️ Ang halos-hindi-gumagalaw na pangalan ay benched PA RIN — ang
    demotion ay sa rvol leg lamang."""
    s = _sig(daily_change_perc=2.0, todays_change_perc=2.0)
    assert RM.below_explosive_floor(s) is True


def test_the_shadow_log_exists_with_a_bounded_memo():
    import pathlib

    src = pathlib.Path(RM.__file__).read_text(encoding="utf-8")
    assert "static_rvol_shadow_bench" in src, "may shadow log dapat"
    assert "_STATIC_FLOOR_SHADOW_MEMO" in src
    assert "2048" in src, "hard cap sa memo (CLAUDE.md cache rule)"
    desc = str(type(settings).model_fields[
        "chili_momentum_static_rvol_floor_demoted"].description or "")
    assert "54.39" in desc and "-9.77" in desc, "nakatala ang R evidence"
