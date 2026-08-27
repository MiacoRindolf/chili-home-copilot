"""Ang emergency extended-hours flatten ay may stand-in pricing na (2026-08-27).

ANG INSIDENTE (CELU): ang quote-independent flatten ay na-block ng 30+ tick
dahil WALANG sariwang quote kahit saan afterhours (Alpaca IEX 66 min luma)
habang −12% ang walang-proteksyong posisyon. Ang stand-in ay pinapayagan sa
ENTRY mula pa noong 08-25 pero HINDI sa exit — sinadya (baka magpresyo nang
mas mataas kaysa book), pero sa OUT-NOW na emergency ang tamang sagot ay
stand-in + KONSERBATIBONG haircut PABABA.

Runnable: pytest tests/test_emergency_exit_stand_in.py -v
"""
from __future__ import annotations

import pathlib

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


def test_the_flags_ship_ON_with_the_incident_recorded():
    assert settings.chili_momentum_emergency_exit_stand_in_enabled is True
    assert float(settings.chili_momentum_emergency_exit_stand_in_max_age_seconds) == 900.0
    assert float(settings.chili_momentum_emergency_exit_stand_in_haircut_pct) == 1.0
    desc = str(type(settings).model_fields[
        "chili_momentum_emergency_exit_stand_in_enabled"].description or "")
    assert "CELU" in desc and "2026-08-27" in desc
    assert "PABABA" in desc, "dapat nakasulat ang konserbatibong direksyon"


def test_the_stand_in_retry_is_emergency_branch_only():
    """⚠️ Ang ordinaryong exit ay strict pa rin — ang stand-in retry ay dapat
    nasa loob lamang ng quote_independent extended branch."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("chili_momentum_emergency_exit_stand_in_enabled")
    region = src[max(0, i - 3000):i]
    assert "quote_independent_authority" in region, (
        "ang retry ay dapat nasa emergency branch"
    )
    # AMENDED (AEMD bailout, mamayang gabi ng parehong araw): ang ordinaryong
    # PROTECTIVE exit ay MAY stand-in escalation na sa extended hours --
    # pagkatapos LAMANG ng N deferral, hindi sa unang tick, at RTH ay strict
    # pa rin. Ang lumang assertion ("walang stand-in sa ordinaryong exit") ay
    # pinalitan ng eskalasyong hangganan.
    j = src.index("chili_momentum_exit_stand_in_after_defers")
    esc_region = src[max(0, j - 1200):j + 1200]
    assert "exit_bbo_defer_count" in esc_region, (
        "ang escalation ay dapat naka-gate sa deferral count, hindi unang tick"
    )
    assert "!= 'regular'" in esc_region.replace('"', "'"), (
        "extended hours lamang ang escalation"
    )


def test_the_haircut_lowers_the_pricing_inputs():
    """Ang haircut ay dapat NAGBABABA (1 - hc) — hindi nagtataas — at bounded."""
    src = _SRC.read_text(encoding="utf-8")
    # rindex: ang parehong knob ay ginagamit na rin ng ordinaryong-exit
    # escalation na MAS MAAGA sa file; ang emergency block ang huli.
    i = src.rindex("chili_momentum_emergency_exit_stand_in_haircut_pct")
    region = src[i:i + 900]
    assert "bid *= (1.0 - _si_hc)" in region
    assert "min(0.10, _si_hc)" in region, "haircut cap sa 10%"


def test_the_stand_in_pricing_is_observable():
    src = _SRC.read_text(encoding="utf-8")
    assert "live_emergency_exit_stand_in_pricing" in src, (
        "ang stand-in pricing ay dapat may sariling event"
    )


def test_the_blocked_path_survives_as_the_final_fallback():
    """Kapag pati stand-in ay wala, ang lumang blocked emit + deferral ay buo."""
    src = _SRC.read_text(encoding="utf-8")
    assert "live_emergency_exit_extended_bbo_blocked" in src


def test_zero_age_disables_the_retry():
    src = _SRC.read_text(encoding="utf-8")
    i = src.rindex("chili_momentum_emergency_exit_stand_in_max_age_seconds")
    region = src[i:i + 400]
    assert "_si_age > 0" in region.replace("if _si_age > 0", "_si_age > 0"), (
        "0 => patay ang retry"
    )


def test_the_ordinary_escalation_resets_and_haircuts():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('le.pop("exit_bbo_defer_count", None)')
    assert i > 0, "matagumpay na quote ay nagre-reset ng defer count"
    j = src.index("live_exit_stand_in_pricing")
    assert j > 0, "obserbable ang ordinaryong stand-in pricing"
    k = src.index("_exit_si_used")
    region = src[k:]
    assert "bid *= (1.0 - _si_hc2)" in region, "may haircut pababa"


def test_the_flag_ships_at_3():
    assert int(settings.chili_momentum_exit_stand_in_after_defers) == 3
    desc = str(type(settings).model_fields[
        "chili_momentum_exit_stand_in_after_defers"].description or "")
    assert "AEMD" in desc and "2026-08-27" in desc
