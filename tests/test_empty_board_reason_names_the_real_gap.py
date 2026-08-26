"""Kapag walang laman ang board, sabihin kung ALIN ang kulang (2026-08-26).

ANG PUWANG. Ang ``_fresh_live_eligible_candidates`` ay may TATLONG kondisyon sa
IISANG query::

    scope == "symbol"
    live_eligible IS TRUE
    freshness_ts >= cutoff

Kapag walang laman ang resulta ay iniuulat ng auto-arm ang ``no_fresh_live_eligible``
-- isang pangalang nagsasabing STALENESS ang problema. **Sa tatlo sa apat na kaso ay
nagsisinungaling ito.**

NASUKAT NANG BUHAY (2026-08-26, premarket, YYGH -- ang pangalang binubuksan ni Ross
ang stream niya)::

    [auto_arm] symbols=['YYGH'] armed=0 skipped=no_fresh_live_eligible
    viability row ng YYGH:  20 SEGUNDO ang tanda   <- SARIWA
    live_eligible = f                              <- ang TUNAY na dahilan

Ang maling pangalan ay nagturo sa akin ng isang oras na paghahanap ng staleness
gayong eligibility pala ang tanong. Ang bawat susunod na magbabasa ng log na iyon ay
malilinlang sa parehong paraan.

⚠️ TUMATAKBO LAMANG ANG DIAGNOSIS SA WALANG-LAMAN NA DAAN -- bihira at pang-diagnose
-- kaya dalawang COUNT lang ang gastos, at hindi sa mainit na daan.

Runnable: pytest tests/test_empty_board_reason_names_the_real_gap.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.auto_arm import _empty_board_diagnosis


class _Scalar:
    def __init__(self, value):
        self._value = value

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self._value


class _Db:
    """Ibinabalik ang fresh count sa unang query at eligible sa pangalawa --
    ang eksaktong pagkakasunod ng dalawang COUNT sa diagnosis."""

    def __init__(self, fresh, eligible):
        self._answers = [fresh, eligible]
        self.calls = 0

    def query(self, *a, **k):
        value = self._answers[min(self.calls, len(self._answers) - 1)]
        self.calls += 1
        return _Scalar(value)


@pytest.mark.parametrize("fresh,eligible,expected", [
    (0, 0, "no_viability_rows"),
    (0, 7, "viability_rows_all_stale"),
    (7, 0, "none_live_eligible"),
    (7, 7, "no_fresh_live_eligible"),
])
def test_each_shortfall_gets_its_own_name(fresh, eligible, expected):
    """ANG PANGUNAHING KASO. Apat na natatanging pangalan kapalit ng isang naglalabo."""
    reason, counts = _empty_board_diagnosis(_Db(fresh, eligible))
    assert reason == expected
    assert counts["fresh_rows"] == fresh
    assert counts["eligible_rows"] == eligible


def test_the_YYGH_case_is_named_eligibility_not_staleness():
    """ANG BUHAY NA KASO. Sariwang hilera, hindi eligible -- dapat NEVER itong
    tawaging stale."""
    reason, _ = _empty_board_diagnosis(_Db(fresh=1, eligible=0))
    assert reason == "none_live_eligible"
    assert "stale" not in reason
    assert "fresh" not in reason


def test_the_original_name_survives_ONLY_for_the_true_case():
    """⚠️ Ang lumang pangalan ay may isang tumpak na kahulugan: may sariwa, may
    eligible, walang tumatawid. Iyon lamang ang dapat magdala nito."""
    assert _empty_board_diagnosis(_Db(3, 3))[0] == "no_fresh_live_eligible"
    for f, e in ((0, 0), (0, 5), (5, 0)):
        assert _empty_board_diagnosis(_Db(f, e))[0] != "no_fresh_live_eligible"


def test_a_broken_count_falls_back_honestly():
    """⚠️ ANG DIAGNOSIS AY HINDI KAILANMAN DAPAT MAGPABAGSAK NG PASS. Kung hindi ito
    masukat ay ibinabalik ang lumang pangalan at NULL na bilang -- hindi isang
    hulang numero."""
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    reason, counts = _empty_board_diagnosis(_Boom())
    assert reason == "no_fresh_live_eligible"
    assert counts == {"fresh_rows": None, "eligible_rows": None}


def test_the_counts_are_carried_not_just_the_name():
    """Ang bilang ang nagpapakita ng HUGIS ng kakulangan -- 0-sa-40 ay ibang
    kuwento kaysa 39-sa-40."""
    _, counts = _empty_board_diagnosis(_Db(fresh=39, eligible=0))
    assert counts["fresh_rows"] == 39
    assert counts["eligible_rows"] == 0


def test_the_empty_board_site_reports_both_reason_and_counts():
    """BANTAY. Ang diagnosis ay walang silbi kung hindi ito naiuulat."""
    import inspect

    from app.services.trading.momentum_neural import auto_arm

    src = inspect.getsource(auto_arm)
    assert "_empty_board_diagnosis(" in src, "dapat tinatawag ang diagnosis"
    assert 'out["board_empty_counts"]' in src, "dapat dala ang bilang sa telemetry"
    assert src.count('out["skipped"] = "no_fresh_live_eligible"') == 0, (
        "walang dapat na naka-hardcode pa ang lumang naglalabong pangalan"
    )
