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
    # ang ordinaryong exit fetch (live_exit_deferred_final_bbo) ay WALANG stand-in
    j = src.index("live_exit_deferred_final_bbo")
    ordinary_region = src[max(0, j - 1500):j]
    assert "allow_stand_in" not in ordinary_region, (
        "ang ordinaryong exit ay hindi dapat nagpapasa ng stand-in"
    )


def test_the_haircut_lowers_the_pricing_inputs():
    """Ang haircut ay dapat NAGBABABA (1 - hc) — hindi nagtataas — at bounded."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("chili_momentum_emergency_exit_stand_in_haircut_pct")
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
    i = src.index("chili_momentum_emergency_exit_stand_in_max_age_seconds")
    region = src[i:i + 400]
    assert "_si_age > 0" in region.replace("if _si_age > 0", "_si_age > 0"), (
        "0 => patay ang retry"
    )
