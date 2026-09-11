"""Ang counter na NAGTATAPOS ng session ay nagbibilang ng maling bagay.

ANG DEPEKTO (nasukat 2026-08-26, inayos 2026-08-27). May iisang landas patungo sa
``live_finished`` sa loob ng ``live_runner``, at ito ay purong COUNTER::

    stopout_cycles >= chili_momentum_max_stopout_reentries   (default 3)

Ang ``live_finished`` ay ABSORBING -- pinipili lang ng runner ang
``LIVE_RUNNER_RUNNABLE_STATES``, kaya walang muling susuri sa simbolo sa natitirang
bahagi ng araw. Ang pasya ay ginagawa NANG MINSAN at hindi na binabalikan.

Ang counter ay nag-uuri ng recycle sa pamamagitan ng TANDA NG RETURN lamang::

    _was_loss = bool(_rb is not None and _rb <= 0)

Salungat iyon sa panuntunang nakasaad TATLONG LINYA sa ibaba nito, para sa G4
escalation level::

    "Only a genuine STOP-class loss raises it (review M1: kill_switch_flatten /
     bailout / max_hold / target exits that close red are NOT entry-level
     failures and do not increment)"

At ang ``stop_class_exit_reason`` mismo ang nagdedeklara ng panuntunan::

    "Public alias of _is_stop_class_exit_reason -- the ONE stop-class classifier.
     Callers gating cadence bookkeeping must use the SAME predicate the
     escalation rule uses so both ends of the 'consecutive stop-class losses'
     contract share one definition."

Ang cap na NAGTATAPOS ng session ang tumatawag na hindi kailanman sumunod. Napansin
na ng docstring ng ``stopout_cycles_after_recycle`` ang ISANG paglihis ("this
mirrors the G4 escalation-level rule beside it ... that the hard counter never
followed") at inayos ang kalahating green-reset; ito ang kabilang kalahati.

NASUKAT -- XPON, isang +58.5% mover, 2026-08-26::

    3 exit, LAHAT last_exit_reason="bailout", bawat isa pula lang
    live_reentry_capped {"reason": "max_stopout_reentries_reached",
                         "trade_cycles": 3, "stopout_cycles": 3}
    ended_at 13:18:05   <- live_finished, ABSORBING

    ...tapos ang tape::
       13:36   6.52
       13:46   7.74      volume ratio  3.84  (papasa sana ang entry gate)
       13:47   7.86      volume ratio 80.64
       13:56   9.50 .. taas 10.13
       13:59   9.72

Nasa TAMANG PANGALAN si CHILI sa TAMANG PRESYO at tinapos siya ng isang counter
28 MINUTO bago ang galaw -- dahil maling na-label ng counter ang sarili nitong
mga exit.

⚠️ HINDI ITO NAG-AALIS NG CAP. Ang tunay na stop-class na chopper ay natatapos pa
rin sa 3, at ang ``symbol_day_loss_lockout`` (netong dolyar) ang tunay na hangganan
ng pinsala -- gaya ng sinasabi mismo ng docstring ng counter.

2026-09-10 UPDATE (tests/test_reentry_ramp_counts_bailouts.py). Ang BAILOUT ay
strike na ulit: 7 araw hanggang 2026-09-10, 18 pulang bailout = -$661.29, 14 sa
re-entry = -$466.28, at ang ``stopout_cap_skipped_non_stop_class`` ay pumutok
nang 20x habang ang TNON ay pumasok nang 4x sa 12 minuto. Ang kaso ng XPON ay
sakop ng day-leader na exemption ng cap (recycles PAST the cap sa escalated na
bar), hindi ng "libre ang bailout". Ang kill_switch / max_hold / pulang target ay
HINDI pa rin strike — iyon ang bahaging nananatili sa pagsusuring ito. Ang
runner ay bumubuo ng ``stop_class_exit_reason OR bailout_class_exit_reason``
(``reentry_ramp_loss_counts``); ang ``_cycles`` sa ibaba ay sumasalamin doon.

2026-09-11 UPDATE [23] (tests/test_cap_counts_exit_verdict_losses.py). BINALIGTAD ang
predicate: ang listahan ng BIBILANGIN ay nabulag sa unang araw sa mga #1385 verdict exit
na pumalit sa bailout (LBGJ 22135 ``tape_accel_rollover`` -358 bps, stopout_cycles 0).
Ngayon bawat PULANG exit ay strike MALIBAN sa pinangalanang non-strike set (utos na
flatten / max_hold / target / scale_out). Ang kill_switch / max_hold / pulang target ay
hindi pa rin strike — ayon sa PANGALAN na ngayon. Ang runner ay tumatawag ng ISANG
predicate (``reentry_ramp_strike_class``); ang ``stop_class_exit_reason`` ay nananatili
bilang pinangalanang revert path (``chili_momentum_reentry_ramp_counts_every_loss=False``).

Runnable: pytest tests/test_stopout_cap_counts_stop_class_only.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.risk_policy import (
    reentry_after_stop_allowed,
    reentry_ramp_loss_counts,
    stop_class_exit_reason,
    stopout_cycles_after_recycle,
)

_SRC = pathlib.Path(LR.__file__)


# ── Ang classifier, at ang eksaktong kaso ng XPON ────────────────────────────


@pytest.mark.parametrize("reason", [
    "bailout",
    "kill_switch_flatten",
    "max_hold",
    "target",
    "scale_out_limit",
])
def test_a_non_stop_exit_is_not_a_stopout(reason):
    """ANG KASO NG XPON. Ang bailout ay hindi pagkabigo ng antas ng entry."""
    assert stop_class_exit_reason(reason) is False


@pytest.mark.parametrize("reason", [
    "stop",
    "trail_stop",
    "stop_broker_zero_reconcile",
    "trail_stop_retry_cap_broker_zero_reconcile",
])
def test_a_genuine_stop_still_counts(reason):
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Ang lunas ay hindi dapat magpahina sa cap
    para sa tunay na chopper -- ang dekoradong dahilan ay dapat pa ring
    maiuri."""
    assert stop_class_exit_reason(reason) is True


def test_an_unknown_reason_is_not_stop_class_but_is_a_cap_strike():
    """Ang STOP-CLASS classifier ay hindi nagbago: hindi kilala => hindi stop-class
    (ang L4 whipsaw cadence ay nananatili sa purong stop-class). Pero ang CAP ay
    binaligtad ng [23]: ang hindi kilalang PULANG exit ay strike — ganoon mismo nabulag
    ang cap nang dalawang beses (bailout 2026-08-27, verdict exits 2026-09-11)."""
    assert stop_class_exit_reason(None) is False
    assert stop_class_exit_reason("") is False
    assert reentry_ramp_loss_counts(None) is True
    assert reentry_ramp_loss_counts("") is True


# ── Ang counter, sa buong tatlong-strike na kadena ───────────────────────────


def _cycles(reasons, red=True):
    """Patakbuhin ang counter sa isang serye ng exit, tulad ng ginagawa ng runner.

    Ang runner ay nagpapasa ng TATLONG estado: umaabante (stop-class na pula),
    humahawak (pula pero hindi stop-class), o nagre-reset (berde).
    """
    n = 0
    for r in reasons:
        # 2026-09-10: stop-class OR bailout advances (the runner's composite).
        counts = bool(red) and reentry_ramp_loss_counts(r)
        holds = bool(red) and not counts
        n = stopout_cycles_after_recycle(
            prev_stopout_cycles=n,
            recycle_was_stopout=counts,
            recycle_holds_streak=holds,
        )
    return n


def test_three_bailouts_reach_the_cap_since_2026_09_10():
    """ANG KASO NG XPON, BINALIKTAD NG SUKAT. Tatlong pulang bailout ay tatlong
    strike (7d live: 18 pulang bailout -$661.29). Ang XPON na +58% ay sakop ng
    day-leader na exemption ng cap, hindi ng libreng bailout."""
    n = _cycles(["bailout", "bailout", "bailout"])
    assert n == 3
    ok, why = reentry_after_stop_allowed(
        stopout_cycles=n,
        max_stopout_reentries=int(settings.chili_momentum_max_stopout_reentries),
        enabled=True,
    )
    assert ok is False and why == "max_stopout_reentries_reached"


def test_three_red_max_holds_still_do_not_reach_the_cap():
    """Ang bahaging NANATILI mula 2026-08-27: ang hindi-stop, hindi-bailout na
    pulang exit ay hindi strike."""
    n = _cycles(["max_hold", "kill_switch_flatten", "max_hold"])
    assert n == 0


def test_three_real_stopouts_still_terminalize():
    """⚠️ Ang chopper ay dapat pa ring humihinto. Iyon ang buong layunin ng cap."""
    n = _cycles(["stop", "trail_stop", "stop"])
    assert n == 3
    ok, why = reentry_after_stop_allowed(
        stopout_cycles=n,
        max_stopout_reentries=int(settings.chili_momentum_max_stopout_reentries),
        enabled=True,
    )
    assert ok is False
    assert why == "max_stopout_reentries_reached"


def test_a_mixed_sequence_only_counts_the_stops():
    """Dalawang tunay na stop; ang tatlong pulang hindi-stop ay HUMAHAWAK -- hindi
    umaabante at hindi nagre-reset."""
    n = _cycles(["bailout", "stop", "max_hold", "trail_stop", "target"])
    assert n == 3, "dalawang stop at isang bailout (2026-09-10); hawak ang max_hold at pulang target"


def test_a_red_non_stop_exit_HOLDS_the_streak_it_does_not_clear_it():
    """⚠️ ANG SUBTLETY NA NAHULI NG SARILING TEST KO. Ang counter ay CONSECUTIVE
    streak: ang pagpasa ng False ay NAGLILINIS nito -- iyon ang panuntunan ng
    berdeng recycle ("a banked winner proves the chop regime ended"). Ang pulang
    bailout ay walang pinatutunayang ganoon, kaya hindi rin ito dapat magbigay ng
    malinis na simula sa isang chopper. Kung hindi, ang chopper na naghahalo ng
    stop at bailout ay hindi kailanman aabot sa cap."""
    n = stopout_cycles_after_recycle(
        prev_stopout_cycles=2, recycle_was_stopout=False, recycle_holds_streak=True)
    assert n == 2, "dapat HUMAWAK, hindi maglinis"


def test_a_chopper_alternating_stop_and_bailout_still_reaches_the_cap():
    """Ang tunay na panganib ng maling reset: isang pangalang naghahalo ng stop at
    bailout ay tatakbo nang walang hanggan."""
    assert _cycles(["stop", "bailout", "stop", "bailout", "stop"]) == 5  # bawat isa ay strike mula 2026-09-10


def test_a_green_recycle_still_resets_the_streak():
    """⚠️ HINDI DAPAT MABAGO ANG UMIIRAL NA GAWI. Ang berdeng recycle ay
    nagre-reset -- ang aral ng HUIZ 2026-08-20 na 3 chop strike ang nagyelo sa
    session sa pagitan ng vertical at ng pangalawang binti nito."""
    n = stopout_cycles_after_recycle(prev_stopout_cycles=2, recycle_was_stopout=False)
    assert n == 0


def test_the_cap_default_is_unchanged():
    """Ang lunas ay nagbabago ng KUNG ANO ang binibilang, hindi ng ILAN."""
    assert int(settings.chili_momentum_max_stopout_reentries) == 3


# ── Ang knob ─────────────────────────────────────────────────────────────────


def test_the_fix_ships_ON_with_a_revert_knob():
    name = "chili_momentum_stopout_cap_stop_class_only"
    assert getattr(settings, name) is True
    fields = type(settings).model_fields
    assert name in fields
    assert fields[name].validation_alias is not None
    desc = str(fields[name].description or "")
    assert "2026-08-27" in desc
    assert "XPON" in desc, "dapat nakatala ang nasukat na kaso, hindi lang ang layunin"
    assert "bailout" in desc


# ── Bantay sa istruktura ─────────────────────────────────────────────────────


def _cooldown_region() -> str:
    """Ang bahagi ng runner na nagse-set ng last_recycle_was_stopout.

    [23]: 4000 -> 4600. Ang strike_class / non_strike_basis na resibo at ang pinangalanang
    revert path ay nagdagdag ng ~370 char sa pagitan ng ``_was_loss = ...`` at ng anchor
    (ang layo ay 3,865 sa origin/main, 4,238 ngayon); ang pagsusuri ay pareho pa rin."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('le["last_recycle_was_stopout"]')
    # [23] review fix: 4600 -> 8200 — the leg-provenance / whole-trade block now sits
    # between ``_rb = ...`` and the anchor.
    return src[max(0, i - 8200): i + 400]


def test_the_cap_input_is_gated_by_the_shared_classifier():
    """ANG BANTAY. Ang bahaging nagtatakda ng input ng cap ay dapat tumawag sa
    IISANG classifier -- hindi sa sarili nitong pag-uuri."""
    region = _cooldown_region()
    assert "stop_class_exit_reason" in region, (
        "ang input ng cap ay dapat dumaan sa iisang stop-class classifier"
    )


def test_the_classifier_is_imported_from_risk_policy():
    """⚠️ Isang LOKAL na kopya ng panuntunan ang paraan kung paano nagsimula ang
    paglihis na ito. Isang import, isang kahulugan."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    found = False
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and "risk_policy" in (n.module or ""):
            if any(a.name == "stop_class_exit_reason" for a in n.names):
                found = True
    assert found, "dapat ini-import mula sa risk_policy, hindi lokal na kinokopya"


def test_the_escalation_input_was_not_disturbed():
    """⚠️ ISANG HATOL KADA RECYCLE. Ang `_was_loss` ay nagpapakain sa cap, sa escalation
    level AT sa whipsaw cadence helper — kaya IISANG halaga ang binabasa ng tatlo.

    [23] review fix (2026-09-11): hindi na ito "purong tanda ng return". Ang
    `last_exit_return_bps` ay ang HULING TRANCHE; ang `g4_prior_trade.was_loss` ay ang
    BUONG TRADE (scale-outs + huling tranche). Session 21589 (09-10): scale_out_limit
    +$22.96 tapos trail_stop sa entry, pnl 0.0 ⇒ ang +$22.96 na trade ay naging STRIKE.
    Ngayon: ang whole-trade na hatol kapag ang stash ay sa MISMONG leg na ito, kung hindi
    ay ang tanda ng return (ang lumang basa, pinangalanan); at ang labasang walang presyo
    (ibang leg ang may-ari ng mga halaga) ay HOLD. Ang tatlong mambabasa ay iisa pa rin."""
    region = _cooldown_region()
    assert '_was_loss = bool(_stash_raw.get("was_loss"))' in region
    assert "_was_loss = bool(_rb is not None and _rb <= 0)" in region
    assert '_loss_basis = "whole_trade"' in region and '_loss_basis = "final_tranche"' in region
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('le["last_recycle_was_stopout"]')
    after = src[i: i + 9000]
    # the whipsaw helper and the level rule read the SAME verdict the cap read
    assert after.count("was_loss=_was_loss") >= 2


def test_the_skip_is_observable():
    """⚠️ Ang tahimik na pagbabago ng gawi ay hindi masusuri. Ang bawat pagkakataong
    HINDI umabante ang cap ay dapat mag-iwan ng bakas -- kung hindi ay walang
    paraan para malaman kung tama ang lunas na ito bukas."""
    region = _cooldown_region()
    assert "stopout_cap_skipped_non_stop_class" in region


def test_there_is_still_exactly_one_path_to_finished():
    """⚠️ Ang buong pagsusuring ito ay nakasalalay sa `live_finished` na may IISANG
    pasukan sa loob ng runner. Ang pangalawa ay isang bagong paraan para matapos
    ang session nang hindi dumadaan sa cap na ito."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    n_transitions = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_safe_transition":
            if any(
                isinstance(a, ast.Name) and a.id == "STATE_LIVE_FINISHED"
                for a in node.args
            ):
                n_transitions += 1
    assert n_transitions == 1, (
        "inaasahang EKSAKTONG isang paglipat patungong live_finished sa loob ng "
        "live_runner, nakita: %d. Ang bago ay kailangan ng sariling pagsusuri sa "
        "momentum." % n_transitions
    )
