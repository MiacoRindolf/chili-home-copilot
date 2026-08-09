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


@pytest.fixture(autouse=True)
def _isolate_sidecar_tempdir(monkeypatch, tmp_path):
    """WALANG test ang dapat sumulat sa TUNAY na sidecar path.

    Ang sidecar ay isang PRODUCTION diagnostic na binabasa ng
    `paper_lane_chain_probe.py`. Nang hindi ito naka-isolate, ang emit test dito
    ay sumulat ng `{"rejected_cooldown": 16}` sa tunay na temp file — at nang
    tingnan ko ang lane pagkatapos, MUKHANG may production census na. Muntik
    kong iulat ang sariling test bilang datos ng lane.

    Autouse: bawat test sa file na ito, walang maaalala.
    """
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


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
        self._captured_paper_admission_raw_samples = {}

    _record = lrl.LiveRunnerLoop._record_captured_paper_admission_outcome
    # Ang TUNAY na sidecar writer, hindi stub — para tunay na nasusubok ang
    # pagsulat sa disk at hindi lang ang pagbibilang.
    _append_admission_census_sidecar = (
        lrl.LiveRunnerLoop._append_admission_census_sidecar
    )


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("initial_candidate_read_unavailable", "initial_candidate_read_unavailable"),
        (
            "INITIAL_INGRESS_PRESSURE_UNAVAILABLE: current sample is stale",
            "initial_ingress_pressure_unavailable",
        ),
        ("postgres://user:secret@host", "captured_paper_admission_rejected"),
        ("free form provider failure", "captured_paper_admission_rejected"),
    ],
)
def test_admission_reason_code_preserves_only_safe_typed_prefix(raw, expected):
    assert lrl._captured_paper_admission_reason_code(raw) == expected


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


# ─────────────────────────────────────────────────────────────────────────────
# SIDECAR — ang durable na daan palabas.
#
# Ang log line lang ay hindi sapat: sa unang RTH na may instrumentasyon ay WALA
# akong mabasang census. Ang tatlong service log ng captured-paper host ay may
# ZERO `[live_loop]` line kahit kailan, at ang tanging may Python WARNING output
# ay tumigil isang oras BAGO magbukas ang market. Instrumento na walang
# mababasang labasan ay hindi instrumento.
# ─────────────────────────────────────────────────────────────────────────────


def _sidecar_path():
    import os
    import tempfile

    return os.path.join(tempfile.gettempdir(), "chili_admission_census.jsonl")


def test_ang_sidecar_ay_talagang_nasusulat(tmp_path):
    """Hindi sapat na tawagin ang writer — dapat may TUNAY na linya sa disk."""
    import json as _json

    lp = _Loop()
    lp._append_admission_census_sidecar({"rejected_cooldown": 5, "admitted": 1}, 6, 1)

    p = tmp_path / "chili_admission_census.jsonl"
    assert p.is_file(), "walang naisulat na sidecar"
    rec = _json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["total"] == 6
    assert rec["admitted"] == 1
    assert rec["outcomes"]["rejected_cooldown"] == 5
    assert rec["at"] and rec["pid"]


def test_ang_sidecar_ay_nag_a_append_hindi_nag_o_overwrite(tmp_path):
    """Cumulative ang counters, pero kailangan pa rin ng kasaysayan para makita
    ang paglaki sa paglipas ng oras."""
    lp = _Loop()
    lp._append_admission_census_sidecar({"admitted": 1}, 1, 1)
    lp._append_admission_census_sidecar({"admitted": 2}, 2, 2)
    lines = [
        l for l in (tmp_path / "chili_admission_census.jsonl")
        .read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    assert len(lines) == 2


def test_ang_sidecar_ay_hindi_sumisira_ng_admission(monkeypatch):
    """Ang pagmamasid ay hindi dapat magtapon. Kahit hindi masulatan ang disk."""
    import tempfile

    monkeypatch.setattr(
        tempfile, "gettempdir", lambda: "Z:/wala-itong-ganitong-drive"
    )
    lp = _Loop()
    lp._append_admission_census_sidecar({"admitted": 1}, 1, 1)  # hindi dapat magtapon


def test_binabasa_ng_probe_ang_sidecar():
    """Ang writer at ang reader ay dapat magkaintindihan — walang saysay ang
    sidecar kung hindi ito mahanap ng probe."""
    import pathlib
    import re

    src = pathlib.Path("scripts/paper_lane_chain_probe.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "chili_admission_census.jsonl" in src, "hindi hinahanap ng probe ang sidecar"
    loop_src = pathlib.Path(
        "app/services/trading/momentum_neural/live_runner_loop.py"
    ).read_text(encoding="utf-8", errors="replace")
    # Iisang pangalan ng file sa magkabilang panig.
    names = set(re.findall(r'"(chili_admission_census\.jsonl)"', src)) & set(
        re.findall(r'"(chili_admission_census\.jsonl)"', loop_src)
    )
    assert names, "hindi tugma ang pangalan ng sidecar sa writer at reader"


def test_walang_test_na_sumusulat_sa_TUNAY_na_sidecar(tmp_path):
    """Ang bitag na muntik nang magpahamak.

    Ang sidecar ay production diagnostic. Kapag sumulat dito ang isang test,
    ang susunod na tumingin sa lane ay makakakita ng SYNTHETIC na counter at
    aakalaing datos ng produksyon iyon — eksaktong nangyari sa akin
    (`rejected_cooldown: 16` mula sa emit test, na-ulat sana bilang lane data).

    Pinatutunayan nito na tumatalab ang autouse na isolation: ang path na
    nakikita ng writer ay NASA LOOB ng tmp_path, hindi sa tunay na temp.
    """
    import os
    import tempfile

    seen = tempfile.gettempdir()
    assert str(tmp_path) == seen, "hindi naka-isolate ang tempdir sa test na ito"
    lp = _Loop()
    lp._append_admission_census_sidecar({"admitted": 1}, 1, 1)
    assert os.path.isfile(os.path.join(seen, "chili_admission_census.jsonl"))


# ─────────────────────────────────────────────────────────────────────────────
# RAW-REASON SAMPLING — huwag itago ng normalizer ang tunay na dahilan.
#
# Unang tunay na census (08-07): 17,008 rejection, LAHAT lumabas bilang ang
# fallback na `rejected_captured_paper_admission_rejected` — dahil ang hilaw na
# dahilan ay hindi tumugma sa regex at ni-normalize BAGO binilang. Ang sarili
# kong instrumento ang nagtago ng sagot. Ngayon ay tinatabi ang bounded na
# sample ng mga hilaw na anyo bago mag-normalize.
# ─────────────────────────────────────────────────────────────────────────────


class _LoopS(_Loop):
    _sample_admission_raw_reason = lrl.LiveRunnerLoop._sample_admission_raw_reason


def test_ang_hilaw_na_dahilan_ay_naitatabi_bago_ma_normalize():
    lp = _LoopS()
    lp._sample_admission_raw_reason("Below A-setup quality floor (change none < 10%) — not a live setup")
    lp._sample_admission_raw_reason("Below A-setup quality floor (change none < 10%) — not a live setup")
    s = lp._captured_paper_admission_raw_samples
    assert len(s) == 1
    k = next(iter(s))
    assert s[k] == 2
    assert "Below A-setup quality floor" in k


def test_ang_sample_ay_pinuputol_at_nililinis():
    lp = _LoopS()
    lp._sample_admission_raw_reason("x" * 500 + "\x00\n\t" + "y" * 50)
    k = next(iter(lp._captured_paper_admission_raw_samples))
    assert len(k) <= lrl._CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_LEN
    assert "\x00" not in k and "\n" not in k


def test_ang_sample_keys_ay_may_hangganan():
    lp = _LoopS()
    for i in range(50):
        lp._sample_admission_raw_reason(f"Iba-ibang dahilan numero {i}")
    assert len(lp._captured_paper_admission_raw_samples) <= lrl._CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_KEYS


def test_ang_sampling_ay_hindi_kailanman_sumasabog():
    lp = _LoopS()
    lp._captured_paper_admission_raw_samples = None  # sadyang sira
    lp._sample_admission_raw_reason("kahit ano")  # hindi dapat magtapon


def test_ang_sidecar_ay_may_raw_samples(tmp_path):
    import json as _json

    lp = _LoopS()
    lp._append_admission_census_sidecar(
        {"rejected_captured_paper_admission_rejected": 3}, 3, 0,
        raw_samples={"Below A-setup quality floor (…)": 3},
    )
    rec = _json.loads(
        (tmp_path / "chili_admission_census.jsonl").read_text(encoding="utf-8").strip()
    )
    assert rec["raw_samples"] == {"Below A-setup quality floor (…)": 3}


def test_ang_call_site_ay_nagsa_sample_ng_hilaw_sa_fallback_na_landas():
    """Istrukturang bantay: sa _admit_iqfeed_symbol, ang sampler ay dapat
    tumanggap ng HILAW na dahilan (`raw_reason`, bago ang helper) at nasa
    purong-fallback na landas — kapag ang typed-prefix extraction ng
    `_captured_paper_admission_reason_code` ay hindi tumama at mawawala na
    sana ang tunay na dahilan."""
    import inspect

    src = inspect.getsource(lrl.LiveRunnerLoop._admit_iqfeed_symbol)
    assert "_sample_admission_raw_reason(raw_reason)" in src, (
        "ang sampler ay dapat tumanggap ng hilaw na dahilan, hindi ang "
        "na-normalize nang resulta"
    )
    i_helper = src.find("_captured_paper_admission_reason_code(")
    i_guard = src.find('== "captured_paper_admission_rejected"')
    i_sample = src.find("_sample_admission_raw_reason(raw_reason)")
    assert 0 < i_helper < i_guard < i_sample, (
        "ang sampler ay dapat nasa fallback-guard pagkatapos ng helper"
    )
