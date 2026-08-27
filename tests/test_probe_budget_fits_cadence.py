"""Ang probe budget ay dapat kasya sa 10s na auto-arm cadence (2026-08-27).

ANG SINUKAT: default na 18s probe budget sa 10s cadence = aritmetikang
garantisadong overrun. Passes 13–29s, 33 apscheduler max_instances skips sa
isang hapon, efektibong cadence 20–30s — sa 3-SEGUNDONG ignition spikes
("4s is 3.5s too long" — Ross). Ang "arm from whatever completed" na disenyo
ng probe wave ang ginagawang ligtas ang mas maliit na budget: ang hindi
na-probe ay nase-serbisyuhan ng susunod na pass na 10s na lang ang layo.

Runnable: pytest tests/test_probe_budget_fits_cadence.py -v
"""
from __future__ import annotations

import ast
import pathlib

from app.services.trading.momentum_neural import auto_arm as AA

_SRC = pathlib.Path(AA.__file__)


def test_the_default_budget_fits_inside_the_cadence():
    """ANG PANGUNAHING KASO: budget < 10s cadence, na may puwang para sa
    board_build/reap/arm overhead."""
    assert AA._probe_time_budget() <= 8.0, (
        "ang probe budget ay dapat kasya sa 10s cadence"
    )
    assert AA._probe_time_budget() >= 1.0


def test_the_arithmetic_is_recorded_at_the_budget():
    import inspect

    doc = str(inspect.getdoc(AA._probe_time_budget) or "")
    assert "2026-08-27" in doc
    assert "max_instances" in doc, "dapat nakasulat ang sinukat na sintomas"


def test_the_knob_still_overrides():
    """Ang operator override ay buhay pa — ang default lang ang bumaba."""
    src = ast.unparse(next(
        n for n in ast.walk(ast.parse(_SRC.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_probe_time_budget"
    ))
    assert "chili_momentum_auto_arm_probe_time_budget_seconds" in src


def test_an_over_cadence_pass_is_no_longer_silent():
    """⚠️ Ang 13–29s na pass ay dating tahimik (60s lang ang threshold) —
    ngayon ang bawat lampas-sa-cadence na pass ay naglalabas ng phase
    breakdown para mapangalanan ang kumakain ng oras."""
    src = _SRC.read_text(encoding="utf-8")
    assert 'total"] >= 10.0' in src
    assert "lampas sa 10s cadence" in src


def test_the_probe_wave_is_marked_for_the_breakdown():
    """Ang d_probe_wave delta ang magpapangalan kung ang probe wave ang
    kumakain — kung wala ito, bulag pa rin ang breakdown sa pinakahinihinalang
    hakbang."""
    src = _SRC.read_text(encoding="utf-8")
    assert '_mark("probe_wave")' in src
