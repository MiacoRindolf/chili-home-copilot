"""FIX A2: ang ws_ignition signal ay may float_shares na sa sandali ng ignition.

ANG SINUKAT (2026-08-27 hapon): ang WS ignition signal ay walang float_shares,
ang leg-4 ng A-setup quality floor ay fail-closed sa missing float, at ang
tanging tagapuno ng datum ay ang 300s batch refresh — kaya BIMODAL ang arm lag
(5/17 na-arm ≤7s na may dating buong row; 12/17 sa 64s–2.85h, median 399s) at
28/45 na na-skip na simbolo ay hindi na-arm buong araw (JFB +20%, TJGC +25%).

Ang lunas ay PUMUPUNO ng datum (Polygon share count, cached) — hindi
binabago o nilalaktawan ang gate.

Runnable: pytest tests/test_ignition_float_feed.py -v
"""
from __future__ import annotations

import ast
import pathlib

from app.config import settings
from app.services.trading.momentum_neural import ignition_loop as IL

_SRC = pathlib.Path(IL.__file__)


def _score_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_score_symbol"
    )


def test_the_flag_ships_ON_with_the_measurement_recorded():
    assert settings.chili_momentum_ignition_float_feed_enabled is True
    desc = str(type(settings).model_fields[
        "chili_momentum_ignition_float_feed_enabled"].description or "")
    assert "399s" in desc, "dapat nakasulat ang sinukat na lag"
    assert "fail-closed" in desc
    assert "2026-08-27" in desc


def test_the_feed_stamps_float_shares_before_the_scorer_runs():
    """ANG PANGUNAHING KASO: ang stamp ay dapat NAUUNA sa run_momentum_neural_tick
    para makita ito ng _extract/_first_float sa viability evaluation."""
    src = ast.unparse(_score_fn())
    i_feed = src.index("float_shares")
    i_tick = src.index("run_momentum_neural_tick(")
    assert i_feed < i_tick, "ang float stamp ay dapat bago ang scorer"
    assert "get_ticker_float" in src


def test_none_is_fail_open_the_key_is_simply_absent():
    """⚠️ Sa None/miss ang signal ay byte-identical sa luma — ang batch refresh
    ang backfill; walang bagong harang."""
    src = ast.unparse(_score_fn())
    i = src.index("get_ticker_float")
    region = src[i:i + 400]
    assert "is not None" in region and "> 0" in region, (
        "ang stamp ay dapat kondisyonal sa totoong positibong halaga"
    )


def test_a_feed_error_never_kills_the_scoring_path():
    src = ast.unparse(_score_fn())
    i = src.index("get_ticker_float")
    region = src[max(0, i - 600):i + 600]
    assert "except Exception" in region, "fail-open dapat ang lookup error"


def test_the_feed_is_flag_gated_for_byte_identical_off():
    src = ast.unparse(_score_fn())
    assert "chili_momentum_ignition_float_feed_enabled" in src
    i_flag = src.index("chili_momentum_ignition_float_feed_enabled")
    i_call = src.index("get_ticker_float")
    assert i_flag < i_call, "ang flag check ay dapat bago ang lookup"


def test_the_leg4_gate_itself_is_untouched():
    """⚠️ Ang lunas ay PUMUPUNO ng datum, hindi nagluluwag ng gate: ang
    fail-closed na 'no-float' leg sa viability ay dapat buo pa rin."""
    via = (_SRC.parent / "viability.py").read_text(encoding="utf-8")
    assert '"no-float"' in via or "'no-float'" in via, (
        "ang no-float fail-closed leg ay dapat nananatili sa viability.py"
    )
