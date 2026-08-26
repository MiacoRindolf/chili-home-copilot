"""Ang `board_build` ay isang numero na hindi nagsasabi KUNG SAAN (2026-08-26).

NASUKAT: 5 SLOW PASS sa 15:28-15:43Z na may `board_build` na 143-237 SEGUNDO
laban sa isang 10-segundong cadence. Ang buong pass: p50 31,014ms, p90
243,562ms, max 257,485ms. Ang `momentum_live_runner_batch` ay 29% (16/55) ang
lumalampas sa agwat nito.

Ang bawat ignition na tumama sa loob ng pass ay naghihintay ng buong tagal bago
ma-arm -- at ang edge ni Ross ay nasa unang mga segundo.

Alam natin KUNG GAANO kabagal at hindi KUNG SAAN. Purong instrumentasyon ito:
walang binabagong daloy at walang bagong tawag.

Runnable: pytest tests/test_auto_arm_names_its_slow_phase.py -v
"""
from __future__ import annotations

import ast
import pathlib

from app.services.trading.momentum_neural import auto_arm as AA

_SRC = pathlib.Path(AA.__file__)


def _fn_src(name: str) -> str:
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return "\n".join(src.splitlines()[n.lineno - 1: n.end_lineno])
    raise AssertionError("walang %s" % name)


def test_the_checkpoints_exist():
    src = _SRC.read_text(encoding="utf-8")
    assert "_phase_marks" in src
    assert 'def _mark(' in src


def test_every_named_step_is_marked():
    """Ang apat na hangganan na naghahati sa mabigat na bloke."""
    src = _SRC.read_text(encoding="utf-8")
    for name in ("pre_reap", "watching_reaper", "loss_history", "board_done"):
        assert '_mark("%s")' % name in src, "nawawala ang marka: %s" % name


def test_the_deltas_are_reported():
    """Ang marka ay walang silbi kung hindi iniuulat ang PAGITAN nila."""
    src = _SRC.read_text(encoding="utf-8")
    assert '_phases["d_%s" % _mname]' in src
    assert '"board_build": round(' in src, "ang lumang sukat ay nananatili"


def test_the_watching_reaper_is_still_scoped_out():
    """⚠️ HINDI BINABAGO ANG DALOY. Ang reaper ay tumatakbo pa rin LAMANG sa
    full pass (`_scoped_syms is None`) -- eksaktong gaya ng dati."""
    src = _SRC.read_text(encoding="utf-8")
    assert "if _scoped_syms is None:\n        reaped = _reap_stale_watching_sessions(" in src


def test_instrumentation_cannot_fail_a_pass():
    """⚠️ Ito ay tumatakbo sa arm path. Ang isang nabigong marka ay hindi dapat
    magpabagsak ng pass."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.find("def _mark(")
    assert i > 0
    assert "except Exception:" in src[i: i + 300]


def test_no_new_calls_were_added_inside_the_measured_block():
    """⚠️ Ang instrumentasyon na nagpapabagal sa bagay na sinusukat nito ay
    walang saysay. Ang tanging naidagdag ay `monotonic()`."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.find("def _mark(")
    body = src[i: i + 300]
    assert "monotonic()" in body
    for forbidden in ("db.query", "requests.", "fetch", "http"):
        assert forbidden not in body, "bagong tawag sa mainit na daan: %s" % forbidden
