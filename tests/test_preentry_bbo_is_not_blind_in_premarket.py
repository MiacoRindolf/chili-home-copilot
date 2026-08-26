"""Ang PRE-ENTRY na tick ay bulag sa premarket (2026-08-26).

ANG PUWANG. Ang ``_live_tick_bbo`` ay nagruruta ng HELD na tick sa mahigpit na
execution-BBO na kontrata na may stand-in fallback (naidagdag 2026-08-25). Ang
PRE-ENTRY ay bumabagsak sa ordinaryong ``adapter.get_best_bid_ask``, na may 60s
na hangganan na **hindi ipinapatupad laban sa provider clock**.

NASUKAT NANG BUHAY (2026-08-26 11:15Z, premarket -- ang mismong mga pangalan sa
scanner ni Ross, habang ini-scalp niya ang DAIC)::

    DAIC  ordinaryo -> None                       => `no_bbo`
          stand-in  -> 6.51/6.54, 0.6s ang tanda
    RDIB  ordinaryo -> 7.31/20.75, provider_time 2026-08-25 20:00 (15 ORAS),
                       spread 9,579 bps
          stand-in  -> 12.38/12.40, 0.24s, 16 bps
    YYGH  ordinaryo -> 1.56/1.59, provider_time 2026-08-25 20:59 (14 ORAS)
          stand-in  -> 2.13/2.13, 0.43s        => 36% MALI ang presyo

Sa mismong oras na iyon, ang DAIC ay may 85 hilera ng depth sa loob ng 3 minuto
(1.5s ang tanda, may `provider_at`) at 16,526 na trade tick. Hindi kawalan ng
datos ito -- source-routing artifact.

⚠️ ANG PANGALAWANG PARAAN ANG MAS MALALA. Ang `no_bbo` ay bumabagsak nang malakas
at nakikita sa `live_blocked_by_risk`; sa `tick_live_session` ay tinetermina pa
nito ang armed session. Ngunit ang quote ng KAHAPON na ipinapasa bilang buhay ay
TAHIMIK na lumalason sa mid, sa spread gate, sa trigger eval, at sa HWM.

⚠️⚠️ HINDI NITO GINAGALAW ANG SUBMIT BOUNDARY. Ang re-price bago mag-order ay may
sariling `_final_entry_bbo` sa ibang mga call site (12525, 12582, 13295, 35035).
Ang binabago rito ay ang quote na NAGPAPATAKBO ng tick.

Runnable: pytest tests/test_preentry_bbo_is_not_blind_in_premarket.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)

_PRE_ENTRY_STATE = "armed_pending_runner"
_HELD_STATE = sorted(LR._HELD_LIVE_STATES)[0]


class _Adapter:
    """Binibilang kung ALIN ang landas na tinatanong -- iyon ang buong tanong."""

    def __init__(self):
        self.ordinary_calls = 0

    def get_best_bid_ask(self, product_id):
        self.ordinary_calls += 1
        return "POISONED_STALE_QUOTE", "ordinary_freshness"


class _Tick:
    def __init__(self, name):
        self.name = name
        self.freshness = name + "_freshness"

    def __repr__(self):
        return "<%s>" % self.name


@pytest.fixture
def spy(monkeypatch):
    """Pinapalitan ang `_final_entry_bbo` para masukat ang RUTA, hindi ang
    panloob na validation nito (may sariling saklaw iyon)."""
    calls = []

    def _fake(adapter, product_id, *, max_age_seconds,
              allow_stand_in=False, stand_in_max_age_seconds=None):
        calls.append({
            "max_age_seconds": max_age_seconds,
            "allow_stand_in": allow_stand_in,
            "stand_in_max_age_seconds": stand_in_max_age_seconds,
        })
        return _fake.results.pop(0)

    _fake.results = []
    monkeypatch.setattr(LR, "_final_entry_bbo", _fake)
    return calls, _fake


def test_a_live_direct_book_still_wins_and_nothing_changes(spy):
    """ANG KALIGTASAN. Sa RTH, may buhay na direktang book, kaya hindi kailanman
    naaabot ang stand-in at walang niluluwagan."""
    calls, fake = spy
    fake.results = [(_Tick("direct"), {"ok": True, "tier": "direct"})]
    a = _Adapter()
    tick, fr, snap = LR._live_tick_bbo(
        a, "DAIC", execution_family="alpaca_spot", state=_PRE_ENTRY_STATE)
    assert tick.name == "direct"
    assert len(calls) == 1, "isang tawag lang -- hindi umaabot sa stand-in"
    assert calls[0]["allow_stand_in"] is False
    assert snap == {"ok": True, "tier": "direct"}


def test_a_dead_direct_book_falls_through_to_the_stand_in(spy):
    """ANG PANGUNAHING KASO -- ang premarket na DAIC."""
    calls, fake = spy
    fake.results = [(None, {"ok": False}), (_Tick("stand_in"), {"ok": True})]
    a = _Adapter()
    tick, fr, snap = LR._live_tick_bbo(
        a, "DAIC", execution_family="alpaca_spot", state=_PRE_ENTRY_STATE)
    assert tick.name == "stand_in"
    assert len(calls) == 2
    assert calls[1]["allow_stand_in"] is True
    assert calls[1]["stand_in_max_age_seconds"] == 15.0


def test_the_poisoned_ordinary_path_is_NEVER_consulted_for_alpaca(spy):
    """⚠️ ITO ANG BUG NA PUMAPATAY. Ang `get_best_bid_ask` ang nagbalik ng
    15-ORAS na quote bilang buhay. Hindi ito dapat maabot ng alpaca pre-entry
    KAHIT NA parehong walang naibigay ang mahigpit at ang stand-in -- ang MALI
    na presyo ay mas masama kaysa WALANG presyo, dahil tahimik itong lumalason."""
    calls, fake = spy
    fake.results = [(None, {"ok": False}), (None, {"ok": False, "reason": "x"})]
    a = _Adapter()
    tick, fr, snap = LR._live_tick_bbo(
        a, "RDIB", execution_family="alpaca_spot", state=_PRE_ENTRY_STATE)
    assert tick is None
    assert a.ordinary_calls == 0, "hindi kailanman dapat gamitin ang lasong landas"


def test_the_block_now_carries_evidence_not_just_a_name(spy):
    """Ang dating payload ay reason-lamang -- walang simbolo, walang edad, walang
    pinagmulan. Walang masasagot ang log kung bakit tumahimik ang lane."""
    calls, fake = spy
    fake.results = [(None, {"ok": False}),
                    (None, {"ok": False, "reason": "execution_bbo_unavailable",
                            "unavailable_kind": "stale_beyond_ceiling",
                            "age_seconds": 54000.0})]
    tick, fr, snap = LR._live_tick_bbo(
        _Adapter(), "RDIB", execution_family="alpaca_spot", state=_PRE_ENTRY_STATE)
    assert snap is not None, "dapat may dalang ebidensya ang harang"
    assert snap["unavailable_kind"] == "stale_beyond_ceiling"
    assert snap["age_seconds"] == 54000.0


def test_the_direct_ceiling_rejects_yesterdays_close(spy):
    """⚠️ ANG NUMERONG BUMABASAG NG LASON. Ang 60s na hangganan ng ordinaryong
    landas ay nagpapasa ng 15-oras na quote; ang pre-entry ay humihingi ng
    sariling hangganan na masikip para sa sarang libro pero maluwag para sa
    anumang tunay na buhay na RTH quote."""
    calls, fake = spy
    fake.results = [(_Tick("direct"), {})]
    LR._live_tick_bbo(_Adapter(), "DAIC",
                      execution_family="alpaca_spot", state=_PRE_ENTRY_STATE)
    assert calls[0]["max_age_seconds"] == 10.0
    assert calls[0]["max_age_seconds"] < 60.0, "dapat mas masikip kaysa ordinaryo"
    assert calls[0]["max_age_seconds"] >= 5.0, "dapat maluwag para sa buhay na quote"


def test_the_knob_can_turn_the_stand_in_off(spy, monkeypatch):
    """Naibabalik ang gawi bago ang 2026-08-26 nang walang deploy."""
    calls, fake = spy
    monkeypatch.setattr(LR.settings, "chili_momentum_preentry_stand_in_enabled",
                        False, raising=False)
    fake.results = [(None, {"ok": False})]
    tick, fr, snap = LR._live_tick_bbo(
        _Adapter(), "DAIC", execution_family="alpaca_spot", state=_PRE_ENTRY_STATE)
    assert tick is None
    assert len(calls) == 1, "hindi dapat umabot sa stand-in kapag naka-off"


@pytest.mark.parametrize("family", ["coinbase_spot", "robinhood_spot", "", None])
def test_non_alpaca_families_are_byte_identical(spy, family):
    """⚠️ Ang Coinbase/Robinhood ay may sariling libro at sariling kontrata --
    hindi sila dapat maapektuhan."""
    calls, fake = spy
    a = _Adapter()
    tick, fr, snap = LR._live_tick_bbo(
        a, "BTC-USD", execution_family=family, state=_PRE_ENTRY_STATE)
    assert tick == "POISONED_STALE_QUOTE"
    assert fr == "ordinary_freshness"
    assert snap is None
    assert a.ordinary_calls == 1
    assert calls == [], "walang execution-BBO na ruta para sa hindi-alpaca"


def test_the_held_path_is_untouched(spy):
    """Ang held branch (2026-08-25) ay dapat manatiling eksakto -- 2.0s na
    mahigpit na hangganan, hindi ang 10s ng pre-entry."""
    calls, fake = spy
    fake.results = [(None, {"ok": False}), (_Tick("held_stand_in"), {"ok": True})]
    tick, fr, snap = LR._live_tick_bbo(
        _Adapter(), "DAIC", execution_family="alpaca_spot", state=_HELD_STATE)
    assert tick.name == "held_stand_in"
    assert calls[0]["max_age_seconds"] == 2.0, "held = 2s, hindi ang pre-entry na 10s"
    assert calls[1]["allow_stand_in"] is True


def test_the_submit_boundary_stand_in_set_did_not_grow():
    """⚠️⚠️ ANG BANTAY NA PINAKAMAHALAGA -- AT ANG MALING PREMISE KO.

    Sumulat muna ako ng bantay na nagsasabing WALANG call site sa labas ng
    ``_live_tick_bbo`` ang dapat humingi ng stand-in. **Nahuli ako nito.** Ang
    APAT na call site (6407, 6682, 6874, 35100) ang humihingi na nito bago pa ang
    pagbabagong ito -- sinadya, dokumentado, at may sariling nasukat na dahilan
    (2026-08-20, XRPI 15:47: ang aprubadong BBO ay 0.12s pero 10.4s ang ginugol
    ng reservation path bago ang seam, kaya ang tanong ay "may sariwang sanang
    market ba NGAYON").

    Kaya ang tamang bantay ay hindi "wala kahit isa" kundi "hindi lumaki".
    ⚠️ At may ibig sabihin ito sa pagbabagong ito: kung ang stand-in ay
    pinagkakatiwalaan na sa mismong seam na NAGPEPRESYO ng order, ang paggamit
    nito para sa quote na SUMUSUKAT lang ng tick ay mas maliit na panganib,
    hindi mas malaki."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    # ⚠️ DALAWA ANG RUTER (2026-08-26, ikalawang PR). Nahuli ako ng bantay na ito
    # nang idagdag ang `_lifecycle_bid_ask` -- tama iyon: BAGO iyon at kailangang
    # tingnan. Sinasadya ang pagbubukod dahil ruter din ito ng KAPAREHONG
    # kontrata, hindi isang bagong seam na nagpepresyo ng order.
    spans = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)
             and n.name in ("_live_tick_bbo", "_lifecycle_bid_ask")]
    assert len(spans) == 2, "dapat dalawa ang ruter"
    outside_total = 0
    outside_stand_in = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_final_entry_bbo":
            continue
        if any(lo <= node.lineno <= hi for lo, hi in spans):
            continue
        outside_total += 1
        if any(k.arg == "allow_stand_in" and
               getattr(k.value, "value", None) is True
               for k in node.keywords):
            outside_stand_in.append(node.lineno)
    assert outside_total >= 4, "dapat may mga call site sa labas na sinusuri"
    assert len(outside_stand_in) == 4, (
        "APAT ang kilalang stand-in sa labas ng _live_tick_bbo noong 2026-08-26 "
        "(ang XRPI re-fetch at ang mga kapatid nito sa entry seam). Nakakita ng "
        "%d sa linya %r -- kung nagdagdag ka ng bago sa isang seam na nagpepresyo "
        "ng order, iyon ay sariling pasya na nangangailangan ng sariling ebidensya."
        % (len(outside_stand_in), outside_stand_in))
