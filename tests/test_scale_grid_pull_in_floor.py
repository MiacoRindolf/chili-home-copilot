"""L10e — ang pull-in floor ng scale-grid rungs (pure, walang I/O).

SINUKAT NA DISTRIBUSYON na nagbunsod nito (scale-grid freeze instrumentation sa
HYFM + JLHL golden windows, 2026-08-04):

  freeze                     1R      rung 1            % ng R
  HYFM  entry 3.66         3.25%   3.70 (+1.09%)         34%
  JLHL  entry 11.82        4.01%   12.00 (+1.52%)        38%
  JLHL  entry 13.54        9.75%   14.00 (+3.40%)        35%
  JLHL  entry  9.50        9.75%   9.55 (+0.53%)        5.4%   <-- patolohiko

Ang huli ay nagbenta ng KALAHATING posisyon sa 5% ng daan papunta sa 1R habang
ang pangalan ay tumakbo papuntang 13.54+. Ugat: ang sub-steps ng
`round_numbers_above` (step*0.1, step*0.05) ay nagbibigay ng nickel/dime "levels"
kapag ang presyo ay nasa DULO ng decade nito — +0.53% sa $9.50, samantalang ang
parehong hugis ng step ay +4.2% sa $0.12 kung saan ito idinisenyo.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import paper_execution as pe
from app.services.trading.momentum_neural.paper_execution import (
    _GRID_MIN_PULL_IN_R_FRACTION,
    round_numbers_above,
    scale_grid_levels,
)


@pytest.fixture(autouse=True)
def _grid_on(monkeypatch):
    monkeypatch.setattr(pe, "scale_grid_enabled", lambda: True)
    monkeypatch.setattr(
        pe.settings, "chili_momentum_scale_grid_r_multiples", "1.0,2.0", raising=False
    )
    monkeypatch.setattr(
        pe.settings, "chili_momentum_scale_grid_fractions", "0.5,0.25", raising=False
    )


def test_ang_ugat_nickel_levels_sa_dulo_ng_decade():
    """Dokumentado ang mismong asal na nagdudulot ng problema."""
    lv = round_numbers_above(9.50)
    # +0.53% at +1.05% — tick noise na ipinapasa bilang "psych levels".
    assert 9.55 in lv and 9.60 in lv, lv


def test_dokumentado_float_gap_sa_sub_cent_grid():
    """⚠️ HIWALAY NA BUG, hindi saklaw ng L10e — itinatala lang.

    Sinasabi ng docstring ng `round_numbers_above` na ang $0.12 ay makakakuha ng
    $0.125/$0.13. HINDI: ang `0.12/0.01` ay 11.999999999999998 sa binary float,
    kaya ang `floor` ay 11 at ang kinukuwentang level ay 0.12 mismo — natatanggihan
    ng strictly-above check. Ganoon din ang 0.005 na step. Kaya ang aktwal na
    resulta ay [0.15, 0.2] lang, at ang crypto/sub-cent na kaso na binanggit sa
    docstring ay hindi talaga naiseserbisyuhan. Hindi ito hinahawakan dito para
    manatiling iisang pagbabago ang PR na ito.
    """
    assert round_numbers_above(0.12) == [0.15, 0.2]


def test_jlhl_patolohikong_pull_in_ay_tinatanggihan():
    # entry 9.50, stop 8.5738 -> 1R = 10.43. Ang 9.55 ay 5.4% lang ng R.
    levels = scale_grid_levels(9.50, 8.5738, side_long=True)
    assert levels, "hindi dapat nawala ang ladder — ang PULL-IN ang sira, hindi ito"
    first_px = levels[0][0]
    assert first_px > 9.55, f"tinanggap pa rin ang nickel level: {first_px}"
    # Bumabalik sa orihinal na R target.
    assert first_px == pytest.approx(9.50 + (9.50 - 8.5738), abs=1e-6)


def test_hyfm_lehitimong_pull_in_ay_pinapanatili():
    # entry 3.66, stop 3.5411 -> 1R = 3.779; ang 3.70 ay 34% ng R (pasado).
    levels = scale_grid_levels(3.66, 3.5411, side_long=True)
    assert levels
    assert levels[0][0] == pytest.approx(3.70, abs=1e-9)


def test_jlhl_whole_dollar_pull_in_ay_pinapanatili():
    # entry 13.54, stop 12.2198 -> 1R = 14.86; ang 14.00 ay 35% ng R (pasado).
    levels = scale_grid_levels(13.54, 12.2198, side_long=True)
    assert levels
    assert levels[0][0] == pytest.approx(14.00, abs=1e-9)

    # entry 11.82, stop 11.3455 -> 1R = 12.29; ang 12.00 ay 38% ng R (pasado).
    levels2 = scale_grid_levels(11.82, 11.3455, side_long=True)
    assert levels2
    assert levels2[0][0] == pytest.approx(12.00, abs=1e-9)


def test_hangganan_ay_nasa_gitna_ng_sinukat_na_puwang():
    # Ang lahat ng lehitimo ay 34%+; ang patolohiko ay 5.4%. Ang base ay dapat
    # nasa loob ng puwang na iyon, may margin sa magkabilang panig.
    assert 0.054 < _GRID_MIN_PULL_IN_R_FRACTION < 0.34


def test_ladder_ay_ascending_pa_rin_at_may_runner():
    for entry, stop in ((9.50, 8.5738), (3.66, 3.5411), (13.54, 12.2198)):
        levels = scale_grid_levels(entry, stop, side_long=True)
        pxs = [p for p, _ in levels]
        assert pxs == sorted(pxs) and len(set(pxs)) == len(pxs), (entry, pxs)
        assert all(p > entry for p in pxs), (entry, pxs)
        assert sum(f for _, f in levels) < 1.0, (entry, levels)
