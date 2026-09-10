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
        # Itala ang SQL at params ng bawat tawag para masuri ng test ang HUGIS
        # ng probe at ang binding ng knob, hindi lang ang sagot.
        self.sql = getattr(a[0], "text", str(a[0])) if a else ""
        self.params = a[1] if len(a) > 1 else k.get("parameters")
        if self.calls == 1:
            return _Result(row=(self._wall_age, self._recent_n, self._median_gap))
        if self._frontier_none:
            return _Result(scalar=None)
        naive = NOW.replace(tzinfo=None)
        return _Result(scalar=naive - timedelta(seconds=self._frontier_lag))


class _DbProbeRaises(_Db):
    """Ang frontier probe MISMO ang bumabagsak (hindi lang walang laman)."""

    def execute(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            return _Result(row=(self._wall_age, self._recent_n, self._median_gap))
        raise RuntimeError("relation iqfeed_trade_ticks does not exist")


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


def test_the_frontier_probe_never_scans_the_whole_tape():
    """ANG REGRESSION NA IPINADALA KO AT INAYOS (2026-08-26).

    Ang unang anyo ng frontier query ay `WHERE observed_at >= gap_since`, na
    mukhang bounded. Ang tape ay 211M na hilera at ang bitmap scan ay naging
    LOSSY: 467,161 heap block, 5,100,637 na hilerang inalis ng index recheck.
    NASUKAT: 420,716 ms kada tawag, APAT na sabay na tumatakbo, humaharang sa
    buhay na lane sa gitna ng session.

    Ang murang anyo -- isang backward scan sa PK -- ay 0.556 ms at nagbabalik ng
    EKSAKTONG PAREHONG timestamp.

    Panghabambuhay na bantay: hindi ito dapat bumalik.
    """
    import pathlib

    from app.services.trading.momentum_neural import nbbo_tape as NT

    # 2026-09-10: ang dating bantay ay naghahanap ng IPINAGBABAWAL NA SUBSTRING
    # ("WHERE observed_at >= :gap_since"). Papasa iyon sa kahit anong bagong
    # predicate na iba ang spelling pero pareho ang sakit. Suriin ang HUGIS ng
    # mismong SQL string na tinatakbo ng probe:
    #   * ang tanging LIMIT ay nasa LOOB, sa backward PK scan (hard bound);
    #   * ang tanging WHERE ay sa LABAS ng tail, sa alias `t` -- kaya ang sala ay
    #     hindi kailanman nagpapalawak ng scan;
    #   * walang predicate sa observed_at sa loob ng tail (iyon ang 73 GB scan).
    sql = " ".join(NT.frontier_probe_sql().split())
    inner = sql[sql.index("(") + 1: sql.index(") t")]
    outer = sql[sql.index(") t") + 3:]
    assert "ORDER BY id DESC LIMIT :tail" in inner, (
        "ang frontier probe ay dapat isang backward PK scan na may hard LIMIT")
    assert "WHERE" not in inner, (
        "walang predicate sa loob ng tail -- iyon ang anyo na nag-scan ng 73 GB")
    assert "observed_at >=" not in sql and ":gap_since" not in sql
    assert outer.strip().startswith("WHERE"), (
        "ang sala sa arrival delay ay nasa LABAS ng tail, sa alias t")
    assert "available_at IS NULL" in outer, (
        "ang hilerang hindi pa nare-release ay real-time at LAGING kasama")
    assert ":fence" in outer and "available_at AT TIME ZONE" in outer
    # at ang tumatakbong code ay ang string na ito, hindi isang lumang kopya
    src = pathlib.Path(NT.__file__).read_text(encoding="utf-8")
    code = chr(10).join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    i = code.find("chili_momentum_halt_print_frontier_relative")
    assert i > 0, "dapat umiiral ang frontier block"
    assert "text(frontier_probe_sql())" in code[i: i + 2500], (
        "ang probe ay dapat tumawag sa frontier_probe_sql(), hindi mag-inline ng SQL")


# ── 2026-09-10: ang tail ay hindi puro real-time, at ang sagot ay may PANGALAN ──
#
# NASUKAT sa pinakabagong 150,000 id ng buhay na tape: 31.7% ng hilera ay galing
# sa 15-minutong DELAYED na IQFeed entitlement (37 simbolo): parehong
# source='iqfeed_l1', MAY available_at, pero ~900 s na luma ang observed_at.
# Ang pinakamahabang sunod-sunod na di-real-time na hilera ay 250. Kapag puro
# delayed ang tail, ang "frontier" ay 900 s na luma, lumalampas sa dead
# threshold, at TAHIMIK na umaabstain ang halt inference para sa BAWAT simbolo.


def test_the_probe_binds_tail_and_fence_from_settings_not_literals():
    db = _Db(408.7)
    print_recency_state(db, "CRE", now_utc=NOW)
    assert db.calls == 2
    assert set(db.params) == {"tail", "fence"}
    assert db.params["tail"] == int(settings.chili_momentum_halt_frontier_tail_rows)
    assert db.params["fence"] == float(settings.chili_momentum_halt_frontier_max_arrival_delay_s)


def test_the_tail_and_fence_are_derived_from_the_measured_population():
    """Hindi magic number: bawat halaga ay may distribusyong pinagmulan sa
    description nito, at ang halaga ay nasa loob ng bandang sinusukat niyon."""
    from app.config import Settings

    tail = Settings.model_fields["chili_momentum_halt_frontier_tail_rows"]
    fence = Settings.model_fields["chili_momentum_halt_frontier_max_arrival_delay_s"]
    for f in (tail, fence):
        assert f.description and "HINANGO" in f.description, "dapat nakasulat ang derivation"
    # tail: dapat lampas sa pinakamahabang nasukat na run (250) nang may margin
    assert int(settings.chili_momentum_halt_frontier_tail_rows) >= 4 * 250
    # fence: dapat nasa WALANG-LAMAN na banda -- lampas sa pinakamasamang bridge
    # stall (109.9 s) at mas maikli sa delayed floor (899.9 s), na may margin
    v = float(settings.chili_momentum_halt_frontier_max_arrival_delay_s)
    assert 2.0 * 109.9 <= v <= 899.9 / 2.0, v


def test_the_basis_is_named_frontier_on_the_measured_board():
    st = print_recency_state(_Db(408.7), "CRE", now_utc=NOW)
    assert st["frontier_basis"] == "frontier"
    assert st["frontier_relative"] is True


def test_a_tail_with_nothing_real_time_names_its_basis_instead_of_silently_using_the_wall_clock():
    """Tumakbo ang probe, walang hilera ang pumasa sa fence (puro delayed o
    hindi pa nare-release). Bumabalik sa wall-clock na sukat GAYA NG DATI --
    pero sinasabi nito kung bakit."""
    st = print_recency_state(_Db(408.7, frontier_none=True), "CRE", now_utc=NOW)
    assert st is not None
    assert st["frontier_relative"] is False
    assert st["frontier_basis"] == "tail_all_filtered"
    assert abs(st["last_print_age_s"] - 408.7) < 0.15
    assert st["pipeline_lag_s"] is None


def test_a_probe_exception_is_named_probe_failed_and_keeps_the_old_measure():
    st = print_recency_state(_DbProbeRaises(408.7), "CRE", now_utc=NOW)
    assert st is not None
    assert st["frontier_relative"] is False
    assert st["frontier_basis"] == "probe_failed"
    assert abs(st["last_print_age_s"] - 408.7) < 0.15


def test_the_knob_off_basis_is_named(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_halt_print_frontier_relative", False, raising=False)
    st = print_recency_state(_Db(408.7), "CRE", now_utc=NOW)
    assert st["frontier_basis"] == "knob_off"
    assert st["frontier_relative"] is False


def test_the_dead_pipeline_abstain_is_no_longer_silent(caplog):
    """Dating: return None, walang log, walang event. Ngayon: isang WARNING na
    nagsasabi ng lag, ng threshold, ng tail at ng fence -- para makita ng
    operator na umaabstain ang halt inference para sa bawat simbolo."""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.services.trading.momentum_neural.nbbo_tape"):
        st = print_recency_state(_Db(2000.0, frontier_lag=1800.0), "X", now_utc=NOW)
    assert st is None
    msgs = [r.getMessage() for r in caplog.records if "ABSTAINS" in r.getMessage()]
    assert msgs, "ang abstain ay dapat may log"
    assert "1800.0s" in msgs[0] and "dead 900s" in msgs[0]
