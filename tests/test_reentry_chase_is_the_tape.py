"""[46] ANG CHASE GATE AY ANG TAPE, HINDI ANG ANTAS.

Ang lumang anti-chase guard ay nagtatanong kung gaano kalayo sa ibabaw ng anchor ng
talunang leg ang presyo (1.5 ATR) — isang tanong tungkol sa ANTAS. Sinukat sa buhay na
`chili` (177 `momentum_reentry_chase_blocked` sa 11 EPISODE, 2026-08-30..09-10) na ang
antas ay walang edge, na ang "ATR" ay isang hardcoded na 1.5%% sa 177/177 hilera, at na ang
TAPE ay nababasa sa BAWAT harang at hinahati nito nang tama ang pinto.

BAWAT numero sa ibaba ay NAKA-PIN sa mismong hilera ng `trading_automation_events` at sa
`signed_tape_accel_features(window_prints=255)` na binasa sa mismong instant ng harang —
hindi gawa-gawang datos. Kung magbago ang mekanismo, ang mga ito ang unang magsasabi.

Runnable: pytest tests/test_reentry_chase_is_the_tape.py -v
"""
from __future__ import annotations

import inspect
import json
import textwrap
from typing import Any

import pytest

from app.config import Settings, settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.paper_execution import (
    regime_atr_pct,
    regime_atr_pct_with_source,
)
from app.services.trading.momentum_neural.risk_policy import (
    REENTRY_CHASE_EXT_Q50,
    REENTRY_CHASE_EXT_Q90,
    REENTRY_CHASE_SIZE_FLOOR,
    reentry_chase_decision,
    reentry_chase_size_multiplier,
)

SOURCE = inspect.getsource(lr.tick_live_session)


# ── ANG NASUKAT NA POPULASYON (bawat hilera ay galing sa live `chili`) ───────────
# (symbol, ts, live_price, prior_anchor_hwm, risk_unit_atr, signed_tape_accel, buy_share_delta)
TNON_TAPE_MINUS = ("TNON", "13:37:36", 4.54, 4.28, 0.0642, -57110.0, -0.06875079047202048)
TNON_TAPE_PLUS = ("TNON", "13:37:45", 4.54, 4.28, 0.0642, 7065.0, 0.3189117777990842)
PCLA_TAPE_PLUS = ("PCLA", "14:03:43", 9.40, 9.15, 0.13725, 1400.0, 0.38698139265407616)
PCLA_TAPE_MINUS = ("PCLA", "14:01:53", 9.48, 9.15, 0.13725, 3897.0, -0.08327000543607044)
DLTH_TAPE_MINUS = ("DLTH", "10:44:40", 4.40, 4.28, 0.0642, -6456.0, -0.07052156282123856)
WYHG_TAPE_MINUS = ("WYHG", "08:59:49", 6.04, 5.88, 0.0882, -4535.0, -0.4090642381780304)
SLE_TAPE_MINUS = ("SLE", "15:39:46", 5.09, 4.94, 0.0741, 18590.0, -0.3749446625330728)
AHMA_TAPE_PLUS = ("AHMA", "13:48:36", 1.85, 1.79, 0.02685, 2299.0, 0.3462728921038024)
TNON_HIGH_EXT = ("TNON", "13:40:16", 4.85, 4.28, 0.0642, 498.0, 0.3673653917144671)


def _decide(row, **kw) -> tuple[bool, float, dict[str, Any]]:
    _sym, _ts, px, anchor, atr, accel, bsd = row
    base: dict[str, Any] = dict(
        enabled=True,
        live_price=px,
        anchor=anchor,
        risk_unit=atr,
        cap_r=1.5,
        tape_accel=accel,
        tape_buy_share_delta=bsd,
        tape_stale=False,
        atr_pct_source="fallback_0.015",
    )
    base.update(kw)
    return reentry_chase_decision(**base)


# ── ANG DALAWANG TUNAY NA MOVE AY PUMAPASOK SA PINAKAMAAGANG INSTANT NILA ────────
def test_tnon_is_admitted_at_its_first_tape_plus_instant_despite_the_old_ceiling():
    """TNON 09-10 13:37:45 @4.54 — 4.05 ATR sa ibabaw ng anchor 4.28, MALAYO sa itaas ng
    lumang ceiling 4.3763, at nag-print pa ito papuntang 4.98 (MFE +6.85 ATR). Ito ang
    unang tape+ instant ng 77-block na episode."""
    admit, mult, dbg = _decide(TNON_TAPE_PLUS)
    assert admit is True
    assert dbg["reason"] == "reentry_chase_tape_admit"
    assert dbg["above_band"] is True                      # ang LUMANG gate ay hinarang ito
    assert dbg["chase_ceiling"] == pytest.approx(4.3763, abs=1e-4)
    assert dbg["extension_atr"] == pytest.approx(4.0498, abs=1e-4)
    assert mult == pytest.approx(1.0)                     # nasa ilalim ng q50 6.19


def test_pcla_is_admitted_at_its_first_tape_plus_instant():
    """PCLA 09-10 14:03:43 @9.40 (anchor 9.15, ceiling 9.3559) -> 10.78 (+10.05 ATR)."""
    admit, mult, dbg = _decide(PCLA_TAPE_PLUS)
    assert admit is True
    assert dbg["reason"] == "reentry_chase_tape_admit"
    assert dbg["above_band"] is True
    assert dbg["extension_atr"] == pytest.approx(1.8215, abs=1e-4)
    assert mult == pytest.approx(1.0)


def test_the_level_is_identical_and_only_the_tape_differs():
    """ANG BUONG PR SA ISANG TEST. Dalawang TNON na block instant, 9 segundo ang pagitan,
    EKSAKTONG parehong presyo (4.54), parehong anchor, parehong ceiling, parehong extension
    — at magkaibang sagot, dahil sa pagitan nila ay lumipat ang accel mula -57,110 patungong
    +7,065 at ang buy_share_delta mula -0.069 patungong +0.319."""
    minus_ok, _, minus = _decide(TNON_TAPE_MINUS)
    plus_ok, _, plus = _decide(TNON_TAPE_PLUS)
    assert minus["extension_atr"] == plus["extension_atr"]
    assert minus["chase_ceiling"] == plus["chase_ceiling"]
    assert minus["above_band"] is plus["above_band"] is True
    assert minus_ok is False and minus["reason"] == "reentry_chase_tape_wait"
    assert plus_ok is True and plus["reason"] == "reentry_chase_tape_admit"


# ── ANG TATLONG PURONG CHOP AY TINATANGGIHAN PA RIN (ang tanging kutsilyo) ───────
@pytest.mark.parametrize("row", [DLTH_TAPE_MINUS, WYHG_TAPE_MINUS, SLE_TAPE_MINUS])
def test_the_no_tape_plus_episodes_stay_in_the_wait(row):
    """DLTH / WYHG / SLE: ZERO tape+ instant sa buong episode nila, at LAHAT sila bumagsak.
    Ang SLE ay nagdadala ng POSITIBONG accel (18,590) na may NEGATIBONG buy_share_delta
    (-0.375) — ang eksaktong dahilan kung bakit KAILANGAN ang dalawang termino."""
    admit, mult, dbg = _decide(row)
    assert admit is False
    assert dbg["reason"] == "reentry_chase_tape_wait"
    assert dbg["tape_readable"] is True
    assert dbg["tape_plus"] is False
    assert dbg["above_band"] is True
    # Ang WAIT ay may laki pa rin sa resibo: kapag kumalas ang tape sa susunod na tick,
    # ang extension ang nagsasabi kung gaano kalaki, hindi kung pumasok ba.
    assert 0.0 < mult <= 1.0


def test_a_positive_accel_with_selling_prints_is_not_tape_plus():
    """SLE 15:39:46: accel +18,590 pero buy_share_delta -0.375. Ang accel lamang ay bilis
    ng tape; ang buy_share_delta ang nagsasabi kung SINO ang aggressor."""
    admit, _, dbg = _decide(SLE_TAPE_MINUS)
    assert dbg["tape_accel"] > 0
    assert dbg["buy_share_delta"] < 0
    assert dbg["tape_plus"] is False
    assert admit is False


def test_pcla_tape_minus_instant_waits_two_minutes_before_its_admit():
    """PCLA 14:01:53 @9.48 (MAS MATAAS na presyo) ay tinatanggihan, at ang 14:03:43 @9.40
    (MAS MABABA) ay pumapasok. Ang ANTAS ay hindi ang sagot."""
    high_ok, _, high = _decide(PCLA_TAPE_MINUS)
    low_ok, _, low = _decide(PCLA_TAPE_PLUS)
    assert high["live_price"] > low["live_price"]
    assert high_ok is False and low_ok is True


# ── ANG BANDA NG LAKI: KUMUKONDISYON, HINDI NAGVE-VETO ──────────────────────────
def test_ahma_pins_the_bands_known_weakness():
    """AHMA 13:48:36 @1.85 — tape+ sa MABABANG extension (2.23 ATR), kaya laki 1.0 kahit
    ang episode ay bumagsak (MAE -14.5 ATR sa unang 5 min). NAKA-PIN ito nang sadya: mahina
    ang banda (hindi monotone ang gitnang tercile, 7 cluster lang), kaya kapag may
    nagbago rito ay MAKIKITA — hindi ito tinutune, iniuulat."""
    admit, mult, dbg = _decide(AHMA_TAPE_PLUS)
    assert admit is True
    assert dbg["extension_atr"] == pytest.approx(2.2346, abs=1e-4)
    assert dbg["extension_atr"] < REENTRY_CHASE_EXT_Q50
    assert mult == pytest.approx(1.0)


def test_the_furthest_tape_plus_instant_sizes_down_to_the_floor():
    """TNON 13:40:16 @4.85 = 8.88 ATR sa ibabaw ng anchor, sa itaas ng q90 8.10 ⇒ floor."""
    admit, mult, dbg = _decide(TNON_HIGH_EXT)
    assert admit is True
    assert dbg["extension_atr"] == pytest.approx(8.8785, abs=1e-4)
    assert mult == pytest.approx(REENTRY_CHASE_SIZE_FLOOR)
    assert dbg["size_band"]["band"] == "at_floor"


def test_the_size_ramp_is_the_62_shape_and_never_vetoes():
    at_q50, _ = reentry_chase_size_multiplier(
        REENTRY_CHASE_EXT_Q50, floor=REENTRY_CHASE_SIZE_FLOOR,
        q50=REENTRY_CHASE_EXT_Q50, q90=REENTRY_CHASE_EXT_Q90,
    )
    at_q90, _ = reentry_chase_size_multiplier(
        REENTRY_CHASE_EXT_Q90, floor=REENTRY_CHASE_SIZE_FLOOR,
        q50=REENTRY_CHASE_EXT_Q50, q90=REENTRY_CHASE_EXT_Q90,
    )
    mid, _ = reentry_chase_size_multiplier(
        (REENTRY_CHASE_EXT_Q50 + REENTRY_CHASE_EXT_Q90) / 2.0,
        floor=REENTRY_CHASE_SIZE_FLOOR,
        q50=REENTRY_CHASE_EXT_Q50, q90=REENTRY_CHASE_EXT_Q90,
    )
    far, _ = reentry_chase_size_multiplier(
        99.0, floor=REENTRY_CHASE_SIZE_FLOOR,
        q50=REENTRY_CHASE_EXT_Q50, q90=REENTRY_CHASE_EXT_Q90,
    )
    assert at_q50 == pytest.approx(1.0)
    assert at_q90 == pytest.approx(REENTRY_CHASE_SIZE_FLOOR)
    assert mid == pytest.approx(1.0 - 0.5 * (1.0 - REENTRY_CHASE_SIZE_FLOOR))
    assert far == pytest.approx(REENTRY_CHASE_SIZE_FLOOR)
    # HINDI KAILANMAN VETO, at hindi kailanman lalampas sa ilalim ng frontside floor.
    assert far > 0.0
    assert far >= float(settings.chili_momentum_frontside_size_floor)


def test_no_extension_never_shrinks_the_size():
    mult, band = reentry_chase_size_multiplier(
        None, floor=REENTRY_CHASE_SIZE_FLOOR,
        q50=REENTRY_CHASE_EXT_Q50, q90=REENTRY_CHASE_EXT_Q90,
    )
    assert mult == pytest.approx(1.0)
    assert band is None


# ── ANG FALLBACK AY MAY PANGALAN, HINDI TAHIMIK ─────────────────────────────────
def test_unreadable_tape_falls_back_to_the_named_atr_ceiling_when_the_atr_is_real():
    admit, _, dbg = _decide(TNON_TAPE_PLUS, tape_accel=None, tape_buy_share_delta=None,
                            atr_pct_source="regime")
    assert admit is False
    assert dbg["reason"] == "reentry_chase_atr_ceiling_fallback"
    assert dbg["tape_readable"] is False
    assert dbg["atr_pct_source"] == "regime"


def test_a_stale_tape_is_not_evidence_and_uses_the_same_named_fallback():
    admit, _, dbg = _decide(TNON_TAPE_PLUS, tape_stale=True, atr_pct_source="regime_meta")
    assert admit is False
    assert dbg["tape_source_stale"] is True
    assert dbg["tape_readable"] is False
    assert dbg["reason"] == "reentry_chase_atr_ceiling_fallback"


def test_a_hardcoded_atr_never_vetoes_silently_it_sizes_down_and_says_so():
    """WALANG SUKAT SA MAGKABILANG PANIG: hindi mabasa ang tape AT ang "ATR" ay ang literal
    na 0.015. Ang +2.25%% ay hindi sukat ng pangalan, kaya hindi ito humaharang — bumababa
    ang laki sa floor at nakasulat sa resibo kung bakit."""
    admit, mult, dbg = _decide(TNON_TAPE_PLUS, tape_accel=None, tape_buy_share_delta=None,
                               atr_pct_source="fallback_0.015")
    assert admit is True
    assert dbg["reason"] == "reentry_chase_unmeasured_size_floor"
    assert mult == pytest.approx(REENTRY_CHASE_SIZE_FLOOR)
    assert dbg["atr_pct_source"] == "fallback_0.015"


def test_below_the_band_nothing_is_decided():
    admit, mult, dbg = _decide(TNON_TAPE_MINUS, live_price=4.30)
    assert admit is True
    assert dbg["above_band"] is False
    assert dbg["reason"] == "reentry_chase_within_band"


def test_unusable_inputs_fail_open():
    for kw in ({"anchor": None}, {"risk_unit": 0.0}, {"live_price": None}, {"cap_r": 0.0}):
        admit, mult, dbg = _decide(TNON_TAPE_MINUS, **kw)
        assert admit is True
        assert mult == pytest.approx(1.0)
        assert dbg["reason"] == "chase_inputs_unusable_fail_open"


# ── ANG ATR SOURCE: ANG BAGONG NATUKLASAN, NAKA-PIN ─────────────────────────────
def test_the_hardcoded_atr_fallback_is_named_and_the_old_value_is_unchanged():
    """177/177 na chase block ang may risk_unit_atr/anchor = EKSAKTONG 1.5000%%: ang
    `regime_atr_pct` ay tahimik na nagbabalik ng 0.015. Ang halaga ay hindi nagbago; ang
    KATAHIMIKAN ang binura."""
    assert regime_atr_pct_with_source({}) == (0.015, "fallback_0.015")
    assert regime_atr_pct_with_source({"atr_pct": None}) == (0.015, "fallback_0.015")
    assert regime_atr_pct_with_source({"atr_pct": 0.08}) == (0.08, "regime")
    assert regime_atr_pct_with_source({"meta": {"atr_pct": 0.06}}) == (0.06, "regime_meta")
    # clamp sa [0.004, 0.12] — sukat pa rin ang pinagmulan
    assert regime_atr_pct_with_source({"atr_pct": 5.0}) == (0.12, "regime")
    for rg in ({}, {"atr_pct": 0.08}, {"meta": {"atr_pct": 0.06}}, {"atr_pct": "x"}):
        assert regime_atr_pct(rg) == regime_atr_pct_with_source(rg)[0]


def test_the_receipt_carries_every_binding_and_is_json_safe():
    _, _, dbg = _decide(TNON_HIGH_EXT)
    b = dbg["binding"]
    assert b["ext_q50"] == pytest.approx(REENTRY_CHASE_EXT_Q50)
    assert b["ext_q90"] == pytest.approx(REENTRY_CHASE_EXT_Q90)
    assert b["size_floor"] == pytest.approx(REENTRY_CHASE_SIZE_FLOOR)
    assert b["admission_rule"] == "signed_tape_accel>0 AND buy_share_delta>0"
    assert "derivations" in b
    for key in ("live_price", "anchor", "risk_unit", "chase_cap_r", "atr_pct_source",
                "tape_accel", "buy_share_delta", "extension_atr", "chase_ceiling",
                "above_band", "size_mult", "reason"):
        assert key in dbg, key
    json.dumps(dbg)  # nakatira ito sa event payload


# ── ANG PATAY NA MAKINARYA AY BINURA ────────────────────────────────────────────
def test_the_leader_ignition_bypass_and_its_flag_are_gone():
    """0 putok ng `momentum_reentry_chase_leader_bypass` sa BUONG kasaysayan laban sa 187
    block (2026-07-06..09-10); 134/187 ng block ay non-structural na trigger kaya hindi ito
    kayang pumutok. Ang kaso nito (tunay na bagong leg ng leader) ay hawak na ng tape."""
    assert "chili_momentum_chase_cap_leader_bypass_enabled" not in Settings.model_fields
    assert not hasattr(settings, "chili_momentum_chase_cap_leader_bypass_enabled")
    assert '_emit(db, sess, "momentum_reentry_chase_leader_bypass"' not in SOURCE
    assert "_cc_bypass" not in SOURCE
    assert 'getattr(settings, "chili_momentum_chase_cap_leader_bypass_enabled"' not in SOURCE


def test_no_new_off_by_default_knob_was_added():
    """Walang dark flag: ang bagong gawi ay LIVE + ON, at ang mga derived na halaga ay
    module constant na may derivation (anyo ng [62]), hindi settings."""
    assert settings.chili_momentum_reentry_chase_cap_enabled is True
    assert not [
        n for n in Settings.model_fields
        if n.startswith("chili_momentum_reentry_chase") and n.endswith("_enabled")
        and Settings.model_fields[n].default is False
    ]
    for n in ("reentry_chase_ext_q50", "reentry_chase_ext_q90", "reentry_chase_size_floor"):
        assert f"chili_momentum_{n}" not in Settings.model_fields


def test_the_shipped_gate_reads_the_same_ticks_tape_and_never_opens_a_new_db_read():
    i = SOURCE.index("[46] ANG CHASE GATE AY ANG TAPE")
    j = SOURCE.index("# BOTTOM-OF-RANGE ENTRY VETO", i)
    block = SOURCE[i:j]
    assert "reentry_chase_decision(" in block
    assert "_g4e_dbg" in block                       # ang tape ng PAREHONG tick
    assert "signed_tape_accel_features" not in block  # zero bagong pagbasa
    assert "_top_ranked_live_eligible_symbol" not in block
    assert "tape_confirms_hold" not in block
    assert 'atr_pct_source=_cc_atr_src' in block
    assert '_via_atr_pct_with_source(via)' in block
    # ang anchor ay ang HIGH PRINT ng naunang leg ([59]) bago ang quote-mid HWM
    assert block.index('_g4e_dbg.get("prior_high_print")') < block.index('_cc_prior.get("high_water_mark")')


def test_the_gate_still_emits_its_ledger_name_plus_the_new_admit_name():
    i = SOURCE.index("[46] ANG CHASE GATE AY ANG TAPE")
    j = SOURCE.index("# BOTTOM-OF-RANGE ENTRY VETO", i)
    block = SOURCE[i:j]
    assert '"momentum_reentry_chase_blocked"' in block       # hindi nasira ang lumang ledger
    assert '"momentum_reentry_chase_tape_admit"' in block    # ang bagong pasok ay may pangalan
    assert '"reentry_chase_cap_wait"' in block


# ── ANG LAKI AY KUMAKAGAT SA PAPER LANE (aral ng [62]) ──────────────────────────
def _post_floor_block() -> str:
    start = SOURCE.index("        # [46] ANG RE-ENTRY CHASE EXTENSION")
    end = SOURCE.index("        # SHELF-REGISTRATION DAMPER", start)
    return textwrap.dedent(SOURCE[start:end])


def _run_block(*, paper_floor_fired: bool, eff: float, le: dict) -> dict[str, Any]:
    ns: dict[str, Any] = {
        "_paper_floor_fired": paper_floor_fired,
        "_eff_max_loss": eff,
        "le": le,
    }
    exec(compile(_post_floor_block(), "<post_floor>", "exec"), ns, ns)  # noqa: S102
    return {"eff": ns["_eff_max_loss"], "le": le}


def test_the_multiplier_is_reapplied_after_the_paper_floor():
    """Ang eksaktong daan sa paper: base 390.00 -> floor -> 390.00 -> chase 0.6845 ->
    266.96. Kung wala ang re-apply, 390.00 (resibo lamang, gaya ng day_open_ramp dati)."""
    le = {"reentry_chase_size": {"mult": REENTRY_CHASE_SIZE_FLOOR, "extension_atr": 8.88,
                                 "reason": "reentry_chase_tape_admit", "binding": {}}}
    out = _run_block(paper_floor_fired=True, eff=390.0, le=le)
    assert out["eff"] == pytest.approx(390.0 * REENTRY_CHASE_SIZE_FLOOR, abs=0.01)
    assert le["reentry_chase_post_floor"]["mult"] == pytest.approx(REENTRY_CHASE_SIZE_FLOOR)
    assert le["reentry_chase_post_floor"]["effective_usd"] == pytest.approx(266.96, abs=0.01)


def test_no_double_apply_on_the_real_money_path():
    le = {"reentry_chase_size": {"mult": REENTRY_CHASE_SIZE_FLOOR}}
    out = _run_block(paper_floor_fired=False, eff=390.0, le=le)
    assert out["eff"] == pytest.approx(390.0)
    assert "reentry_chase_post_floor" not in le


def test_the_post_floor_receipt_never_survives_into_a_full_size_leg():
    """Ang depekto ng `frontside_size_tilt` / [62], naabot muli: isinusulat lamang kapag
    kumakagat, hindi kailanman binubura."""
    le: dict[str, Any] = {"reentry_chase_size": {"mult": REENTRY_CHASE_SIZE_FLOOR}}
    first = _run_block(paper_floor_fired=True, eff=390.0, le=le)
    assert "reentry_chase_post_floor" in first["le"]
    le["reentry_chase_size"] = {"mult": 1.0}
    second = _run_block(paper_floor_fired=True, eff=390.0, le=le)
    assert second["eff"] == pytest.approx(390.0)
    assert "reentry_chase_post_floor" not in second["le"]


def test_the_stash_itself_is_cleared_on_every_chase_pass():
    """Ang `reentry_chase_size` ay binubura sa UNAHAN ng gate, kaya ang isang pass na hindi
    umabot sa desisyon (walang prior loss, flag off) ay hindi nagmamana ng lumang laki."""
    i = SOURCE.index("[46] ANG CHASE GATE AY ANG TAPE")
    j = SOURCE.index('chili_momentum_reentry_chase_cap_enabled', i)
    assert 'le.pop("reentry_chase_size", None)' in SOURCE[i:j]


def test_both_receipts_are_cleared_on_recycle():
    keys = lr._RECYCLE_ENTRY_STATE_KEYS
    assert "reentry_chase_size" in keys
    assert "reentry_chase_post_floor" in keys


def test_entry_filled_payload_carries_the_receipt():
    i = SOURCE.index('"live_entry_filled"')
    assert '"reentry_chase_size": le.get("reentry_chase_size")' in SOURCE[i:]
    assert '"reentry_chase_post_floor": le.get("reentry_chase_post_floor")' in SOURCE[i:]


def test_the_multiplier_is_in_the_stacked_product_too():
    """Sa real-money path ay walang paper floor, kaya ang product ang tanging daan."""
    assert "_safe_mult(_reentry_chase_mult)" in SOURCE
    assert '"reentry_chase": round(float(_safe_mult(_reentry_chase_mult)), 4)' in SOURCE


def test_the_stash_crosses_the_tick_from_the_trigger_state_to_the_sizing_state():
    """Ang gate ay nasa `STATE_WATCHING_LIVE` at ang sizing ay nasa
    `STATE_LIVE_ENTRY_CANDIDATE` — IBANG TICK. Kaya ang stash ay dapat nakasulat sa `le`
    (na-commit), at ang pop ay dapat NASA trigger state, hindi sa sizing state; kung
    lumipat ang pop sa sizing branch ay palaging 1.0 ang mult at resibo lang muli ito."""
    i_watch = SOURCE.index("if st == STATE_WATCHING_LIVE:")
    i_cand = SOURCE.index("if st == STATE_LIVE_ENTRY_CANDIDATE:", i_watch)
    i_pop = SOURCE.index('le.pop("reentry_chase_size", None)')
    i_gate = SOURCE.index("[46] ANG CHASE GATE AY ANG TAPE")
    i_size = SOURCE.index('le.pop("reentry_chase_post_floor", None)')
    assert i_watch < i_pop < i_cand
    assert i_watch < i_gate < i_cand
    assert i_size > i_cand
    # ...at ang stash ay na-commit sa parehong bloke kung saan ito isinulat
    block = SOURCE[i_gate:SOURCE.index("# BOTTOM-OF-RANGE ENTRY VETO", i_gate)]
    assert 'le["reentry_chase_size"] = {' in block
    assert "_commit_le(sess, le)" in block
