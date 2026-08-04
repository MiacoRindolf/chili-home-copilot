"""L10c — ang shape gate ng scale ladder (pure, walang I/O).

BAKIT: ang L10b proof (08-04, 4 golden windows) ay nagpakita na ang multi-level
scale ladder ay KONDISYONAL sa hugis ng galaw, hindi basta mabuti o masama:

  HYFM |2026-08-03  (34-segundong single-bar vertical)  +0.22  ->  +24.46
  JLHL |2026-07-09  (halt-stairs, 83% ng hold sa halt)  +23.63 ->  +12.67
  TC   |2026-07-01  (chop)                              -5.55  ->   -2.98
  CLRO |2026-07-02  (hindi naabot ang ladder)           -11.37 ->  -11.37

Sa vertical, ang tranches ay nababangko papasok sa pagsabog kung saan nakaupo ang
round-number liquidity. Sa stairs, ang parehong partials ay pumuputol sa buntot.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    VERTICAL_LEG_MAX_SECONDS,
    vertical_leg_shape,
)


def _shape(**kw):
    base = dict(leg_age_seconds=30.0, halt_lit=False)
    base.update(kw)
    return vertical_leg_shape(**base)


def test_hyfm_class_batang_leg_ay_vertical():
    # Ang HYFM leg ay 34s — malayo sa ilalim ng window.
    ok, reason = _shape(leg_age_seconds=34.0)
    assert ok is True and reason == "vertical"


def test_jlhl_class_halt_ay_stairs():
    ok, reason = _shape(halt_lit=True)
    assert ok is False and reason == "halt_stairs"


def test_matandang_leg_ay_stairs():
    ok, reason = _shape(leg_age_seconds=240.0)
    assert ok is False and reason == "leg_mature_1m_owns"


def test_halt_ay_nananaig_kahit_bata_ang_leg():
    # Ang JLHL ay may halt-stairs mula sa maagang bahagi — ang halt ang senyales,
    # hindi ang edad.
    ok, reason = _shape(leg_age_seconds=5.0, halt_lit=True)
    assert ok is False and reason == "halt_stairs"


@pytest.mark.parametrize(
    "age,expected",
    [(179.0, True), (VERTICAL_LEG_MAX_SECONDS, False), (181.0, False)],
)
def test_hangganan_ay_mahigpit_na_lampas(age, expected):
    ok, _ = _shape(leg_age_seconds=age)
    assert ok is expected


def test_iisang_base_kasama_ang_L10_structure_floor():
    # Ang L10 helper ay dating may sariling literal na 180.0; ngayon ay
    # iisang constant na ang ginagamit ng dalawa — walang duplicated na numero.
    from app.services.trading.momentum_neural import paper_execution as pe
    import inspect

    src = inspect.getsource(pe.monster_structure_floor_candidate)
    assert "VERTICAL_LEG_MAX_SECONDS" in src
    assert "180.0" not in src, "bumalik ang duplicated na literal"
    assert VERTICAL_LEG_MAX_SECONDS == 180.0


def test_fail_toward_hindi_vertical_sa_sirang_input():
    # Ang hindi alam na edad ay ibinabalik sa single scale-out — ang mas
    # konserbatibong dating ugali.
    for kw in ({"leg_age_seconds": None}, {"leg_age_seconds": float("nan")}):
        ok, reason = _shape(**kw)
        assert ok is False, kw
        assert reason == "leg_age_unknown", (kw, reason)
