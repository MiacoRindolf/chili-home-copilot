"""Ang halt ay sinusukat laban sa WALL CLOCK, hindi sa tape (2026-08-26).

ANG PUWANG. `print_recency_state` ay sumusukat ng `last_print_age_s` mula sa wall
clock papunta sa pinakabagong print ng simbolo. Pinagsasama niyon ang dalawang
magkaibang bagay::

    tumigil ang simbolong ito        (ang gusto nating makita)
    nahuhuli ang buong PIPELINE      (ang aktwal na nasusukat)

NASUKAT SA BUHAY (2026-08-26 14:27Z, RTH, habang 409s ang likod ng tape)::

    simbolo   laban sa WALL   laban sa FRONTIER
    CRE            408.7            0.0
    DAIC           408.8            0.0
    YYGH           409.0            0.3
    RDIB           409.2            0.5
    MSS            409.6            0.9
    XPON           582.4          173.7
    VCIG           612.1          203.4

Laban sa wall clock ay LAHAT mukhang 400s tahimik, at LAHAT ay minarkahang naka-
halt -- anim na pangalan sa loob ng 7 segundo sa isa't isa. Laban sa frontier ay
lima ang AKTIBONG NAGPI-PRINT at dalawa lang ang tunay na tahimik.

Bunga: **93 sa 130** na `live_blocked_by_risk` ngayong umaga ay
`suspected_halt_active` -- halos lahat peke.

⚠️ PAREHONG BUG NA NAAYOS NA SA `tape_ingest_recency_age_s` (2026-08-24), kung
saan ang parehong pagsasama ay SUMUPIL sa halt inference sa DALAWANG TUNAY na
LULD halt. Ang landas na ito ay hindi naabot noon.

Runnable: pytest tests/test_halt_inference_uses_tape_frontier.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural.nbbo_tape import print_recency_state

NOW = datetime(2026, 8, 26, 14, 27, 0, tzinfo=timezone.utc)

# Ang eksaktong hilera na nakuha sa buhay na tape, 2026-08-26 14:27Z.
MEASURED = [
    # (symbol, wall_age_s, frontier_age_s, aktibo_pa_ba)
    ("CRE", 408.7, 0.0, True),
    ("DAIC", 408.8, 0.0, True),
    ("YYGH", 409.0, 0.3, True),
    ("RDIB", 409.2, 0.5, True),
    ("MSS", 409.6, 0.9, True),
    ("XPON", 582.4, 173.7, False),
    ("VCIG", 612.1, 203.4, False),
]

PIPELINE_LAG = 408.7  # ang frontier ay 408.7s ang likod ng wall clock


class _Result:
    def __init__(self, row=None, scalar=None):
        self._row, self._scalar = row, scalar

    def fetchone(self):
        return self._row

    def scalar(self):
        return self._scalar


class _Nested:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Db:
    """Unang execute = per-symbol na hilera; pangalawa = frontier."""

    def __init__(self, wall_age, *, frontier_lag=PIPELINE_LAG, recent_n=50,
                 median_gap=0.4, frontier_none=False):
        self._wall_age = wall_age
        self._frontier_lag = frontier_lag
        self._recent_n = recent_n
        self._median_gap = median_gap
        self._frontier_none = frontier_none
        self.calls = 0

    def begin_nested(self):
        return _Nested()

    def execute(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            return _Result(row=(self._wall_age, self._recent_n, self._median_gap))
        if self._frontier_none:
            return _Result(scalar=None)
        naive = NOW.replace(tzinfo=None)
        return _Result(scalar=naive - timedelta(seconds=self._frontier_lag))


@pytest.mark.parametrize("sym,wall,frontier,aktibo", MEASURED)
def test_the_measured_board_resolves_correctly(sym, wall, frontier, aktibo):
    """ANG PANGUNAHING KASO -- ang buhay na tape, hilera kada hilera."""
    st = print_recency_state(_Db(wall), sym, now_utc=NOW)
    assert st is not None
    assert st["frontier_relative"] is True
    assert abs(st["last_print_age_s"] - frontier) < 0.15, (
        "%s: dapat %.1fs laban sa frontier, hindi %.1fs laban sa wall"
        % (sym, frontier, wall))
    # Ang WALL na sukat ay dinadala pa rin -- iyon ang nagpapakita ng pagkakaiba.
    assert abs(st["last_print_age_wall_s"] - wall) < 0.15
    assert abs(st["pipeline_lag_s"] - PIPELINE_LAG) < 0.15


@pytest.mark.parametrize("sym,wall,frontier,aktibo", MEASURED)
def test_the_thirty_second_floor_separates_active_from_silent(sym, wall, frontier, aktibo):
    """⚠️ ANG BUONG PUNTO. Sa 30s na sahig ng halt gap, ang WALL na sukat ay
    nagmamarka ng LAHAT ng pito bilang naka-halt; ang FRONTIER na sukat ay
    nagmamarka lamang ng dalawa."""
    st = print_recency_state(_Db(wall), sym, now_utc=NOW)
    frontier_says_halted = st["last_print_age_s"] > 30.0
    wall_says_halted = st["last_print_age_wall_s"] > 30.0
    assert wall_says_halted is True, "ang wall na sukat ay nagmamarka ng lahat"
    assert frontier_says_halted is (not aktibo)


def test_a_genuinely_silent_symbol_is_still_caught_when_the_pipeline_is_current():
    """⚠️ HINDI ITO NAGPAPAHINA NG DETECTION. Kapag kasalukuyan ang pipeline
    (frontier ~ now), ang frontier na sukat ay katumbas ng wall na sukat."""
    st = print_recency_state(_Db(240.0, frontier_lag=0.5), "HALTED", now_utc=NOW)
    assert st["last_print_age_s"] > 200.0
    assert st["frontier_relative"] is True


def test_a_dead_pipeline_makes_us_ABSTAIN_not_pass():
    """⚠️⚠️ ANG DIREKSYON NG KALIGTASAN. Kung ang frontier MISMO ay lampas na sa
    dead-threshold, hindi lang nahuhuli ang pipeline -- hindi na ito
    mapagkakatiwalaan. Ang pagbabalik ng 'sariwa' doon ay fail-OPEN: mabubulag
    tayo sa isang TUNAY na halt. Umaabstain tayo."""
    st = print_recency_state(_Db(2000.0, frontier_lag=1800.0), "X", now_utc=NOW)
    assert st is None


def test_the_dead_threshold_is_a_knob_not_a_magic_number():
    from app.config import settings as s
    assert float(getattr(s, "chili_momentum_halt_print_pipeline_dead_seconds")) == 900.0


def test_the_knob_reverts_to_wall_clock(monkeypatch):
    """Gawi bago ang 2026-08-26, nang walang deploy."""
    monkeypatch.setattr(
        settings, "chili_momentum_halt_print_frontier_relative", False, raising=False)
    st = print_recency_state(_Db(408.7), "CRE", now_utc=NOW)
    assert st["frontier_relative"] is False
    assert abs(st["last_print_age_s"] - 408.7) < 0.15


def test_a_failed_frontier_read_falls_back_to_the_old_measure():
    """⚠️ Ang nabigong frontier read ay hindi dapat magpabago ng gawi -- ang
    lumang sukat ang ibinabalik, may bandilang nagsasabi nito."""
    st = print_recency_state(_Db(408.7, frontier_none=True), "CRE", now_utc=NOW)
    assert st is not None
    assert st["frontier_relative"] is False
    assert abs(st["last_print_age_s"] - 408.7) < 0.15
    assert st["pipeline_lag_s"] is None


def test_no_tape_still_means_no_inference():
    """Ang orihinal na kontrata ay hindi ginagalaw."""
    class _Empty(_Db):
        def execute(self, *a, **k):
            self.calls += 1
            return _Result(row=(None, 0, None))

    assert print_recency_state(_Empty(0.0), "X", now_utc=NOW) is None


def test_the_frontier_age_is_never_negative():
    """Ang simbolong nagpi-print SA MISMONG frontier ay 0.0, hindi negatibo."""
    st = print_recency_state(_Db(408.7, frontier_lag=500.0), "AHEAD", now_utc=NOW)
    assert st["last_print_age_s"] >= 0.0
