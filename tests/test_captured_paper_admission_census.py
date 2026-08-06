"""Ang sealed captured-paper admitter ay hindi na dapat tahimik na mabigo.

ANG SINUKAT NA KASO (2026-08-05). Tumakbo ang lane nang 51 minuto ng RTH,
gumawa ng 550 selection event sa 18 symbol, at may mga pangalang umabot sa
`live_eligible` NANG WALANG veto — pero ZERO ang naarm, at halos walang naiwang
ebidensya kung bakit. Apat na landas ang tahimik na nawawala sa
``LiveRunnerLoop._admit_iqfeed_symbol``:

  1. tinanggihan NANG WALANG reason string -> walang log kahit kailan
  2. rate-limited na log na WALANG BILANG -> 3,000 rejection ay kamukha ng 1
  3. hindi-Mapping na resulta -> tahimik na ``return None``
  4. exception sa admitter -> ``_log.debug``, na hindi lumalabas sa runtime na
     walang logging config (root WARNING ang default)

Pagmamasid lamang ang census — walang desisyong nababago nito. Iyon ang
kontratang binabantayan dito: ang bawat landas ay NABIBILANG, at ang resulta na
ibinabalik sa caller ay HINDI nagbabago.
"""
from __future__ import annotations

import logging

import pytest

from app.services.trading.momentum_neural import live_runner_loop as lrl


class _Loop:
    """Ang pinakamaliit na tunay na bahagi: ang aktwal na method ng klase,
    nakakabit sa isang bagay na may kailangan lang nitong estado.

    ⚠️ Ang method ay nakabalot sa try/except (sinadya — ang pagmamasid ay hindi
    dapat makasira ng admission), kaya ang KULANG na attribute dito ay hindi
    sumasabog: tahimik itong pumapalya. Nangyari na iyon — nawala ang
    `_ticks` at ang pagbibilang ay mukhang gumagana habang hindi na lumalabas
    ang buod. Kaya may test sa ibaba na nagsisiguro na sakop ng double na ito
    ang bawat attribute na hinahawakan ng method.
    """

    def __init__(self):
        import threading

        self._iqfeed_admission_lock = threading.Lock()
        self._captured_paper_admission_outcomes = {}
        self._captured_paper_admission_census_monotonic = None
        self._captured_paper_admission_census_ticks = 0

    _record = lrl.LiveRunnerLoop._record_captured_paper_admission_outcome


def test_ang_test_double_ay_sakop_ang_bawat_attribute_na_hinahawakan():
    """Ang try/except sa method ay nagtatago ng kulang na attribute sa double.

    Kinukuha ang bawat `self.<attr>` na binabanggit ng method at tinitiyak na
    mayroon nito ang double — kung hindi, ang buong test file ay tahimik na
    sumusukat ng wala.
    """
    import inspect
    import re

    src = inspect.getsource(lrl.LiveRunnerLoop._record_captured_paper_admission_outcome)
    attrs = set(re.findall(r"self\.(_[A-Za-z0-9_]+)", src))
    lp = _Loop()
    missing = [a for a in sorted(attrs) if not hasattr(lp, a)]
    assert not missing, f"kulang sa test double: {missing}"


def test_bawat_kalalabasan_ay_nabibilang():
    lp = _Loop()
    for outcome in ("admitted", "rejected_unspecified", "admitted",
                    "non_mapping_result", "admitter_exception"):
        lp._record(outcome)
    c = lp._captured_paper_admission_outcomes
    assert c["admitted"] == 2
    assert c["rejected_unspecified"] == 1
    assert c["non_mapping_result"] == 1
    assert c["admitter_exception"] == 1


def test_ang_buod_ay_lumalabas_sa_WARNING_hindi_INFO(caplog):
    """Sinadya ang antas: may runtime na walang logging config, kaya root
    WARNING ang default at tahimik na nawawala ang INFO — iyon mismo ang
    nakagat sa replay harness ngayong araw."""
    n = lrl._CAPTURED_PAPER_ADMISSION_CENSUS_TICKS
    lp = _Loop()
    # Ang orasan ay binabasa lang tuwing ika-N; punuin hanggang sa tsekpoint.
    for _ in range(n - 1):
        lp._record("rejected_cooldown")
    lp._captured_paper_admission_census_monotonic = -1e9  # matagal nang lumipas
    with caplog.at_level(logging.WARNING, logger=lrl.__name__):
        lp._record("rejected_cooldown")
    msgs = [r.message for r in caplog.records if "admission census" in r.message]
    assert msgs, "walang buod na lumabas sa WARNING"
    assert f"rejected_cooldown={n}" in msgs[-1]
    assert f"{n} attempt" in msgs[-1]


def test_ang_pagbibilang_ay_HINDI_bumabasa_ng_orasan_kada_kalalabasan():
    """Ang tunay na kontrata sa likod ng tick batching.

    Ang admission ay may mga testong nagbibigay ng EKSAKTONG badyet ng
    `time.monotonic()` na halaga. Nang bumasa ang census kada kalalabasan,
    nadoble ang clock reads, na-exhaust ang kanilang iterator, at ang
    `StopIteration` ay naging tahimik na `None` na resulta — isang tunay na
    pagbabago ng ugali mula sa dapat sanang pagmamasid lamang.
    """
    lp = _Loop()
    calls = {"n": 0}
    real = lrl.time.monotonic

    def counting():
        calls["n"] += 1
        return real()

    lrl.time.monotonic = counting
    try:
        for _ in range(lrl._CAPTURED_PAPER_ADMISSION_CENSUS_TICKS - 1):
            lp._record("admitted")
    finally:
        lrl.time.monotonic = real
    assert calls["n"] == 0, (
        f"bumasa ng orasan {calls['n']} beses sa "
        f"{lrl._CAPTURED_PAPER_ADMISSION_CENSUS_TICKS - 1} kalalabasan — dapat zero"
    )


def test_hindi_lumalabas_bago_ang_agwat():
    """Ang census ay hindi dapat maging bagong ingay kada tick."""
    lp = _Loop()
    for _ in range(50):
        lp._record("rejected_cooldown")
    assert lp._captured_paper_admission_outcomes["rejected_cooldown"] == 50
    # walang exception, at nananatiling naka-akumula — ang paglabas ay
    # panahunan, hindi kada pangyayari


def test_ang_census_ay_hindi_kailanman_sumasabog():
    """Ang pagmamasid ay hindi dapat makasira ng admission. Kahit sirang estado
    ay dapat ligtas — kaya nakabalot ito sa try/except."""
    lp = _Loop()
    lp._captured_paper_admission_outcomes = None  # sadyang sira
    lp._record("admitted")  # hindi dapat magtapon


@pytest.mark.parametrize(
    "src,expected",
    [
        ("admission_token_unavailable", "admission_token_unavailable"),
        ("non_mapping_result", "non_mapping_result"),
        ("admitter_exception", "admitter_exception"),
        ("rejected_unspecified", "rejected_unspecified"),
    ],
)
def test_ang_apat_na_dating_tahimik_ay_may_sariling_pangalan(src, expected):
    """Bawat isa sa apat ay dapat makilala nang HIWALAY sa census — kung
    pinagsama-sama sila, hindi natin malalaman kung alin ang nangyayari."""
    lp = _Loop()
    lp._record(src)
    assert expected in lp._captured_paper_admission_outcomes


def test_ang_apat_na_landas_ay_talagang_naka_instrumento_sa_source():
    """Istrukturang bantay: kung may mag-aalis ng isang call site, dito
    babagsak — hindi sa isang tahimik na production run makalipas ang linggo."""
    import inspect

    src = inspect.getsource(lrl.LiveRunnerLoop._admit_iqfeed_symbol)
    for needed in (
        "admission_token_unavailable",
        "non_mapping_result",
        "rejected_unspecified",
        "admitter_exception",
        '_record_captured_paper_admission_outcome("admitted")',
    ):
        assert needed in src, f"nawala ang instrumentasyon para sa: {needed}"
    # Ang exception path ay dapat WARNING, hindi debug.
    assert "_log.warning(" in src
