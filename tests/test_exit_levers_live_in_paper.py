"""Ang exit lever ay naka-OFF na naghihintay ng A/B na hindi darating (2026-08-26).

ANG DEADLOCK. Apat na exit lever ang naka-`default=False`, bawat isa ay
naghihintay ng "A/B proof" o "counterfactual" bago ma-promote::

    chili_momentum_exit_ladder_live               "not yet A/B-proven net-positive"
    chili_momentum_exit_ofi_hidden_seller_enabled "promote only after OFI+micro proves"
    chili_momentum_exit_ofi_lock_partial_enabled  "log-would-fire-first"
    chili_momentum_exit_candle_confirm_live       "flip ON once the A/B shows..."

NASUKAT: **ZERO** counterfactual event sa 30 araw. Walang trade na maoobserbahan,
kaya walang datos na naiipon, kaya hindi kailanman naaabot ang bar. Bilog ito.

Samantala ang nasukat na capture ratio ay **18.5%** -- +108.35R ang naabot,
+20.02R ang nakuha. Iyon ang butas na sinasarhan ng mga lever na ito.

ANG DATOS AY BUHAY NA. Sinukat 2026-08-26 15:39Z, kada simbolo sa 5 minuto::

    118 hilera   100% may provider_at   100% may bid5/ask5   100% may imbalance5

Ang orihinal na dahilan ng pag-OFF -- walang mapagkakatiwalaang depth -- ay hindi
na totoo. Sa isang **PAPER** account na may tunay na fill, ang pagpapatakbo nito
ANG A/B.

⚠️ TATLO ang binuksan, HINDI apat. Ang `exit_candle_confirm_live` ay
AND-gated at "can only tighten the fire criteria" -- nagpapaANTALA ito ng exit,
na kabaligtaran ng problema, at nagbabasa ito ng **1m candle**, ang mismong
cached na frame na nasukat sa 634s na median na tanda. Ang pagbubukas nito ay
susupilin ang exit batay sa lumang kandila.

Runnable: pytest tests/test_exit_levers_live_in_paper.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings

# Ang tatlong lever na NAGPAPAAGA ng exit -- doktrina ni Ross (partials into strength).
PROMOTED = [
    "chili_momentum_exit_ladder_live",
    "chili_momentum_exit_ofi_hidden_seller_enabled",
    "chili_momentum_exit_ofi_lock_partial_enabled",
]

# Ang lever na nagpapaANTALA ng exit at nagbabasa ng stale na 1m candle.
HELD_BACK = "chili_momentum_exit_candle_confirm_live"


@pytest.mark.parametrize("flag", PROMOTED)
def test_the_exit_levers_are_live(flag):
    """ANG PANGUNAHING KASO. Ang tatlo ay dapat buhay para makapag-ipon ng tunay
    na ebidensya mula sa tunay na fill."""
    assert getattr(settings, flag) is True, (
        "%s ay dapat buhay -- ang A/B ay hindi kailanman darating mula sa "
        "counterfactual kapag walang trade na maoobserbahan" % flag)


def test_the_candle_confirm_gate_is_deliberately_still_off():
    """⚠️ HINDI ITO PAGKALIMOT. Ang gate na ito ay AND-gated at NAGPAPAHIGPIT ng
    fire criteria -- nagpapaANTALA ito ng exit, samantalang 18.5% ang capture
    dahil huli na ang exit. At nagbabasa ito ng 1m candle, ang cached na frame na
    nasukat sa 634s na median. Ang pagbubukas nito ay susupilin ang exit batay sa
    lumang kandila. Isa itong SEPARADONG pasya na may sariling ebidensya."""
    assert getattr(settings, HELD_BACK) is False


def test_the_promoted_set_is_exactly_three():
    """⚠️ BANTAY LABAN SA DRIFT. Kung may nagdagdag ng ikaapat sa promosyon nang
    walang sariling ebidensya, ito ang huhuli."""
    live = [f for f in (*PROMOTED, HELD_BACK) if getattr(settings, f) is True]
    assert len(live) == 3, "inaasahang 3 buhay, nakita: %r" % (live,)


@pytest.mark.parametrize("flag", PROMOTED + [HELD_BACK])
def test_every_lever_remains_a_knob(flag):
    """Ang bawat isa ay dapat naibabalik nang walang deploy."""
    assert isinstance(getattr(settings, flag), bool)
    fields = type(settings).model_fields
    assert flag in fields, "%s ay dapat isang deklaradong setting" % flag
    assert fields[flag].validation_alias is not None, (
        "%s ay dapat may env alias para maibalik nang walang deploy" % flag)


def test_the_reason_for_the_flip_is_recorded_in_the_description():
    """⚠️ Ang susunod na magbabasa ay dapat makita KUNG BAKIT ito binuksan, hindi
    lamang NA ito binuksan. Ang isang default na nagbago nang walang dahilan ay
    ang eksaktong bagay na dating ikinabagsak ng #1024 (13,817-linyang config
    rewrite kung saan tahimik na lumipat ang mga default)."""
    fields = type(settings).model_fields
    for flag in PROMOTED:
        desc = str(fields[flag].description or "")
        assert "2026-08-26" in desc, "%s: dapat may petsa ang pagbabago" % flag
        assert "counterfactual" in desc.lower(), (
            "%s: dapat naitala ang bilog na deadlock" % flag)
        assert "provider_at" in desc, (
            "%s: dapat naitala ang nasukat na kalidad ng depth" % flag)
