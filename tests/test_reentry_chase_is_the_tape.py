"""[46] ANG CHASE GATE AY ANG TAPE, HINDI ANG ANTAS.

Ang lumang anti-chase guard ay nagtatanong kung gaano kalayo sa ibabaw ng anchor ng
talunang leg ang presyo (1.5 ATR) — isang tanong tungkol sa ANTAS. Sinukat sa buhay na
`chili` (177 `momentum_reentry_chase_blocked` sa 11 EPISODE, 2026-08-30..09-10) na ang
antas ay walang edge, na ang "ATR" ay isang hardcoded na 1.5%% sa 177/177 hilera, at na
ang TAPE ay nababasa sa BAWAT harang at hinahati nito nang tama ang pinto.

BAWAT NUMERO SA IBABA AY MULING SINUKAT NOONG 2026-09-11 SA KONTRATANG TUMATAKBO.
Ang gate ay kumakain ng tape ng `_g4_reentry_escalation_check`, at iyon ay binabasa sa
ilalim ng ``feature_contract="legacy_time_split"`` — hindi ang default na ``count_v1``.
Ang dalawang kontrata ay hindi magkasundo kung tape+ ba sa 31 ng 177 na instant, kaya
bawat fixture dito ay dala ang HALAGANG BINASA NG KONTRATANG IYON, ang petsa ng pagsukat,
at (kung kapaki-pakinabang) ang halaga ng kabilang kontrata sa parehong instant.

Reproduksyon ng bawat hilera (bounded, read-only sa `chili`):
    signed_tape_accel_features(sym, db=db, as_of=<block ts>, window_prints=255,
                               feature_contract="legacy_time_split")
    prior_leg_high_print(sym, db=db, entry_at=<prior leg live_entry_filled ts>,
                         exit_at=<prior leg live_exit_filled ts>, as_of=<block ts>)
Ang mga halaga ng tape ay HINDI matatag sa paglipas ng linggo (ginagalaw sila ng
publication-eligibility filtering) — kaya nandito ang petsa. Kapag nagbago sila, ang
pagbabago ang balita, hindi ang pagkasira ng test.

Runnable: pytest tests/test_reentry_chase_is_the_tape.py -v
"""
from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from app.config import Settings, settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.paper_execution import (
    regime_atr_pct,
    regime_atr_pct_with_source,
)
from app.services.trading.momentum_neural.risk_policy import reentry_chase_decision

SOURCE = inspect.getsource(lr.tick_live_session)
GATE_SOURCE = inspect.getsource(lr._reentry_chase_gate)
G4_SOURCE = inspect.getsource(lr._g4_reentry_escalation_check)
CAP_R = 1.5


# ── ANG NASUKAT NA POPULASYON ────────────────────────────────────────────────────
# Bawat hilera: ang PRINT basis (ang nagpapasya) at ang QUOTE basis (ang basehan kung
# saan naka-calibrate ang ceiling) sa MISMONG instant ng harang, at ang tape ng
# `legacy_time_split`.  ts ay UTC.
class Row:
    def __init__(self, symbol, ts, px, anchor, risk, q_px, q_anchor, q_risk, accel, bsd):
        self.symbol, self.ts = symbol, ts
        self.px, self.anchor, self.risk = px, anchor, risk
        self.q_px, self.q_anchor, self.q_risk = q_px, q_anchor, q_risk
        self.accel, self.bsd = accel, bsd

    def kw(self, **over) -> dict[str, Any]:
        base = dict(
            live_price=self.px, anchor=self.anchor, risk_unit=self.risk, cap_r=CAP_R,
            quote_price=self.q_px, quote_anchor=self.q_anchor, quote_risk_unit=self.q_risk,
            tape_accel=self.accel, tape_buy_share_delta=self.bsd, tape_stale=False,
            atr_pct_source="fallback_0.015",
            risk_unit_source="regime_atr_pct:fallback_0.015",
            tape_feature_contract="legacy_time_split",
        )
        base.update(over)
        return base


#                     symbol   ts(UTC)      print px  anchor   risk      quote px anchor  risk
PCLA_MINUS = Row("PCLA", "14:01:53", 9.43, 9.15, 0.13725, 9.48, 9.15, 0.13725, 3913.0, -0.05359276157126447)
PCLA_PLUS = Row("PCLA", "14:03:43", 9.425, 9.15, 0.13725, 9.40, 9.15, 0.13725, 1309.0, 0.37652048630570484)
TNON_PLUS = Row("TNON", "13:37:36", 4.60, 4.32, 0.0648, 4.54, 4.28, 0.0642, 62789.0, 0.09116984542971485)
TNON_FAR = Row("TNON", "13:40:16", 4.7785, 4.32, 0.0648, 4.85, 4.28, 0.0642, 965.0, 0.441488806976347)
# ── ANG TATLONG PURONG CHOP. Ang PRINT ay nasa LOOB ng banda sa lahat ng tatlo at ang
# QUOTE ay nasa itaas — sila ang 32/177 na butas na isinasara ng UNION.
DLTH_MINUS = Row("DLTH", "10:44:40", 4.37, 4.28, 0.0642, 4.40, 4.28, 0.0642, -7491.0, -0.09959362461201882)
WYHG_MINUS = Row("WYHG", "08:59:49", 6.0247, 5.94, 0.0891, 6.04, 5.88, 0.0882, -6472.0, -0.47967340180318546)
SLE_MINUS = Row("SLE", "15:39:46", 5.06, 4.978, 0.07467, 5.09, 4.94, 0.0741, 18590.0, -0.3749446625330728)
# Kaparehong instant ng SLE sa ilalim ng DEFAULT na `count_v1` na kontrata — KABALIGTARAN
# ang sagot (accel +7,077 / bsd +0.2085 laban sa +18,590 / −0.3749; n_ticks 255 vs 120).
SLE_COUNT_V1_ACCEL, SLE_COUNT_V1_BSD = 7077.0, 0.2084677173235847
# MIMI: ang PRINT ay NASA IBABA ng anchor (ext −0.62) habang ang QUOTE ay +3.85 ATR sa
# itaas — ang pinakamalaking pagkakaiba ng dalawang basehan sa buong populasyon.
MIMI_PLUS = Row("MIMI", "14:24:19", 1.0304, 1.04, 0.0156, 1.10, 1.04, 0.0156, 36798.0, 0.6559290803029473)


def _decide(row: Row, **over) -> tuple[bool, dict[str, Any]]:
    return reentry_chase_decision(**row.kw(**over))


# ── ANG TAPE ANG SUMASAGOT, HINDI ANG ANTAS ─────────────────────────────────────
def test_the_level_is_the_same_and_only_the_tape_differs():
    """ANG BUONG PR SA ISANG TEST. Dalawang PCLA na block instant, 110 s ang pagitan,
    PAREHONG anchor at PAREHONG ceiling, at ang MAS MATAAS na presyo (9.43) ang
    TINATANGGIHAN habang ang MAS MABABA (9.425) ay PUMAPASOK — dahil sa pagitan nila ang
    buy_share_delta ay lumipat mula −0.0536 patungong +0.3765. Ang ANTAS ay hindi ang
    sagot."""
    minus_ok, minus = _decide(PCLA_MINUS)
    plus_ok, plus = _decide(PCLA_PLUS)
    assert minus["chase_ceiling"] == plus["chase_ceiling"]
    assert minus["above_band"] is plus["above_band"] is True
    assert minus["live_price"] > plus["live_price"]
    assert minus_ok is False and minus["reason"] == "reentry_chase_tape_wait"
    assert plus_ok is True and plus["reason"] == "reentry_chase_tape_admit"


def test_tnon_is_admitted_at_its_first_tape_plus_instant_despite_the_old_ceiling():
    """TNON 09-10 13:37:36 @4.60 print — 4.32 ATR sa ibabaw ng high print 4.32, malayo sa
    itaas ng ceiling 4.4172, at nag-print papuntang 4.98. Ito ang unang tape+ instant ng
    77-block na episode sa ilalim ng kontratang tumatakbo."""
    admit, dbg = _decide(TNON_PLUS)
    assert admit is True
    assert dbg["reason"] == "reentry_chase_tape_admit"
    assert dbg["above_band"] is True                       # ang LUMANG gate ay hinarang ito
    assert dbg["band_basis"] == "both"
    assert dbg["chase_ceiling"] == pytest.approx(4.4172, abs=1e-4)
    assert dbg["extension_atr"] == pytest.approx(4.3210, abs=1e-4)


def test_pcla_is_admitted_at_its_first_tape_plus_instant():
    admit, dbg = _decide(PCLA_PLUS)
    assert admit is True
    assert dbg["reason"] == "reentry_chase_tape_admit"
    assert dbg["extension_atr"] == pytest.approx(2.0036, abs=1e-4)


@pytest.mark.parametrize("row", [DLTH_MINUS, WYHG_MINUS, SLE_MINUS])
def test_the_no_tape_plus_episodes_stay_in_the_wait(row: Row):
    """DLTH / WYHG / SLE: ZERO tape+ instant sa buong episode nila, at LAHAT sila
    bumagsak."""
    admit, dbg = _decide(row)
    assert admit is False
    assert dbg["reason"] == "reentry_chase_tape_wait"
    assert dbg["tape_readable"] is True
    assert dbg["tape_plus"] is False
    assert dbg["above_band"] is True


def test_a_positive_accel_with_selling_prints_is_not_tape_plus():
    """SLE 15:39:46: accel +18,590 pero buy_share_delta −0.375. Ang accel lamang ay bilis
    ng tape; ang buy_share_delta ang nagsasabi kung SINO ang aggressor."""
    admit, dbg = _decide(SLE_MINUS)
    assert dbg["tape_accel"] > 0
    assert dbg["buy_share_delta"] < 0
    assert dbg["tape_plus"] is False
    assert admit is False


# ── (R1) ANG BANDA AY UNION: HINDI LUMILIIT ANG ABOT NG HARANG ──────────────────
@pytest.mark.parametrize("row", [DLTH_MINUS, WYHG_MINUS, SLE_MINUS])
def test_a_print_inside_the_band_whose_quote_is_above_it_is_still_judged(row: Row):
    """ANG 32/177 NA BUTAS. Ang unang anyo ng PR na ito ay inilipat ang nagpapasyang
    presyo sa last print at ang anchor sa high print habang ang ceiling ay nananatiling
    naka-calibrate sa (ask laban sa quote HWM). Sa DLTH/WYHG/SLE ang PRINT ay nasa LOOB ng
    banda at ang QUOTE ay nasa itaas: sa print-only na banda sila ay papasok sa BUONG laki
    nang walang tanong sa tape at walang hilera sa libro (24 sa 32 ay tape−). Sa UNION
    sila ay hinuhusgahan."""
    admit, dbg = _decide(row)
    assert dbg["above_band_print_basis"] is False
    assert dbg["above_band_quote_basis"] is True
    assert dbg["above_band"] is True
    assert dbg["band_basis"] == "quote"
    assert admit is False


@pytest.mark.parametrize("row", [DLTH_MINUS, WYHG_MINUS, SLE_MINUS])
def test_the_print_only_band_is_exactly_the_hole_the_union_closes(row: Row):
    """Ang NEGATIBONG kontrol ng test sa itaas: alisin ang quote basis at ang parehong
    hilera ay tahimik na papasok."""
    admit, dbg = _decide(row, quote_price=None, quote_anchor=None, quote_risk_unit=None)
    assert admit is True
    assert dbg["above_band"] is False
    assert dbg["reason"] == "reentry_chase_within_band"


def test_mimi_shows_how_far_apart_the_two_bases_are():
    """MIMI 09-09 14:24:19: ang huling PRINT (1.0304) ay nasa IBABA ng high print ng
    talunang leg (1.04) — ext −0.62 — habang ang ASK (1.10) ay +3.85 ATR sa itaas nito.
    Isang sentimo ng spread sa $1.04 ay 0.64 ATR: hindi maliit ang pagkakaiba ng basehan,
    at pareho itong iniuulat."""
    admit, dbg = _decide(MIMI_PLUS)
    assert dbg["extension_atr"] == pytest.approx(-0.6154, abs=1e-4)
    assert dbg["extension_atr_quote_basis"] == pytest.approx(3.8462, abs=1e-4)
    assert dbg["above_band_print_basis"] is False
    assert dbg["above_band_quote_basis"] is True
    assert admit is True and dbg["reason"] == "reentry_chase_tape_admit"


def test_inside_both_bands_nothing_is_decided():
    admit, dbg = _decide(PCLA_MINUS, live_price=9.20, quote_price=9.20)
    assert admit is True
    assert dbg["above_band"] is False
    assert dbg["band_basis"] == "within_band"
    assert dbg["reason"] == "reentry_chase_within_band"


# ── (R2) ANG KONTRATA NG TAPE AY NASA RESIBO, AT ANG DERIVATION AY TUMAKBO RITO ──
def test_the_shipped_contract_and_the_default_contract_disagree_on_sle():
    """SLE 09-08 15:39:46, MISMONG instant, dalawang kontrata: `legacy_time_split`
    (ang TUMATAKBO) ay nagbibigay ng +18,590 / −0.3749 sa 120 na tick na hinati sa ORAS ⇒
    tape−, WAIT; ang default na `count_v1` ay +7,077 / +0.2085 sa 255 na tick na hinati sa
    BILANG ⇒ tape+, ADMIT. 31 sa 177 na instant ang hindi magkasundo (17.5%%), kaya ang
    pangalan ng kontrata ay nasa resibo at ang derivation ay tumakbo sa TUMATAKBO."""
    wait_ok, wait = _decide(SLE_MINUS)
    admit_ok, admit = _decide(
        SLE_MINUS, tape_accel=SLE_COUNT_V1_ACCEL, tape_buy_share_delta=SLE_COUNT_V1_BSD,
        tape_feature_contract="count_v1",
    )
    assert wait_ok is False and wait["tape_feature_contract"] == "legacy_time_split"
    assert admit_ok is True and admit["tape_feature_contract"] == "count_v1"
    assert wait["extension_atr"] == admit["extension_atr"]      # PAREHONG antas


def test_the_gate_reports_the_contract_its_tape_was_read_under():
    """Ang halaga ay galing sa `_g4e_dbg`; ang pangalan ng kontrata ay sumasakay kasama
    nito, kaya hindi na kailangang hulaan ng sinumang bumabasa ng resibo."""
    assert 'feature_contract="legacy_time_split"' in G4_SOURCE
    assert '_g4e_dbg["tape_feature_contract"] = _g4e_tape_contract' in G4_SOURCE
    assert "tape_feature_contract=_cc_tape_contract" in GATE_SOURCE


# ── (R3) WALANG BANDA NG LAKI — SINUKAT, HINDI IPINALAGAY ───────────────────────
def test_the_decision_returns_no_size_multiplier():
    """Ang unang anyo ay nagbabalik ng (admit, size_mult, dbg) na may ramp na 1.0 sa/ibaba
    ng q50 6.19. Ang 6 na unang-admit na instant (doon nangyayari ang entry) ay may
    extension na −0.62..4.32 — LAHAT sa ilalim ng 6.19 — kaya ang ramp ay 1.0 sa BAWAT
    pasok na nililikha ng pagbabago; at ang continuation ay PATAAS sa mas malayong
    extension (0.8667/0.8824/1.0000 sa 47 tape+ instant) habang sa 21 tunay na napunang
    post-loss re-entry ito ay hindi monotone (0.7143/0.2857/0.7143). Walang sukat ang
    nagbibigay ng size-down; kaya walang multiplier."""
    out = _decide(TNON_FAR)
    assert len(out) == 2
    admit, dbg = out
    assert admit is True and dbg["reason"] == "reentry_chase_tape_admit"
    assert dbg["extension_atr"] == pytest.approx(7.0756, abs=1e-4)   # malayo, buong laki
    assert dbg["binding"]["size_band"] == "none_measured"
    assert "size_mult" not in dbg


def test_no_size_band_machinery_survives_anywhere():
    for dead in ("REENTRY_CHASE_EXT_Q50", "REENTRY_CHASE_EXT_Q90",
                 "REENTRY_CHASE_SIZE_FLOOR", "reentry_chase_size_multiplier"):
        assert not hasattr(rp, dead), dead
    for dead in ("reentry_chase_size", "reentry_chase_post_floor", "_reentry_chase_mult"):
        assert dead not in SOURCE, dead
    assert "reentry_chase_size" not in lr._RECYCLE_ENTRY_STATE_KEYS


def test_no_new_off_by_default_knob_was_added():
    """Walang dark flag: ang bagong gawi ay LIVE + ON, at walang bagong settings knob."""
    assert settings.chili_momentum_reentry_chase_cap_enabled is True
    assert not [
        n for n in Settings.model_fields
        if n.startswith("chili_momentum_reentry_chase") and n.endswith("_enabled")
        and Settings.model_fields[n].default is False
    ]
    for n in ("reentry_chase_ext_q50", "reentry_chase_ext_q90", "reentry_chase_size_floor"):
        assert f"chili_momentum_{n}" not in Settings.model_fields


# ── (R9) WALANG EBIDENSYA AY HINDI POSITIBONG EBIDENSYA ─────────────────────────
@pytest.mark.parametrize("over,reason", [
    ({}, "reentry_chase_tape_wait"),
    ({"tape_accel": None, "tape_buy_share_delta": None}, "reentry_chase_tape_unreadable_wait"),
    ({"tape_buy_share_delta": None}, "reentry_chase_tape_unreadable_wait"),
    ({"tape_stale": True}, "reentry_chase_tape_unreadable_wait"),
])
def test_less_evidence_never_buys_more_permission(over, reason):
    """ANG BALIGTAD NA PASYA NA INAAYOS NITO. Ang unang anyo ay nagpapapasok sa HINDI
    nababasang tape (sa size floor, sa anumang extension) habang tinatanggihan ang
    NABABASANG negatibo — kaya ang kaso na may ZERO ebidensya ay pumapasok at ang kaso na
    may NEGATIBONG ebidensya ay hindi. Ang `buy_share_delta=None` ay hindi bihira: ito ang
    ibinabalik ng tape helper kapag kulang sa 4 na print ang window — eksakto ang
    nangyayari sa manipis na tape pagkatapos ng halt resume, kung saan sadyang
    muling nag-a-anchor ang `chase_cap_halt_reanchor`."""
    admit, dbg = _decide(DLTH_MINUS, **over)
    assert admit is False
    assert dbg["reason"] == reason


def test_the_wait_is_not_a_lockout_it_releases_on_the_next_tape_plus_print():
    """Ang parehong instant, ang parehong presyo — ang tape lamang ang nagbago."""
    wait_ok, _ = _decide(DLTH_MINUS)
    release_ok, release = _decide(DLTH_MINUS, tape_accel=1234.0, tape_buy_share_delta=0.21)
    assert wait_ok is False
    assert release_ok is True and release["reason"] == "reentry_chase_tape_admit"


def test_the_dead_atr_ceiling_fallback_branch_is_gone():
    """0 sa 5,680 na live `momentum_symbol_viability` na hilera sa 2 araw ang may dalang
    `atr_pct`, kaya ang `atr_pct_source` ay `fallback_0.015` sa 100%% ng kaso at ang
    sangay na `regime`/`regime_meta` ay hindi kailanman tumatakbo; ang stale na sub-case ay
    dobleng patay dahil `reentry_escalation_decision` ay bumabalik nang False sa
    `tape_stale` bago pa marating ang gate. Ang pinagmulan ay NANANATILING iniuulat."""
    for src in ("regime", "regime_meta", "fallback_0.015", None):
        admit, dbg = _decide(DLTH_MINUS, tape_accel=None, tape_buy_share_delta=None,
                             atr_pct_source=src)
        assert admit is False
        assert dbg["reason"] == "reentry_chase_tape_unreadable_wait"
        assert dbg["atr_pct_source"] == src
    assert "reentry_chase_atr_ceiling_fallback" not in inspect.getsource(reentry_chase_decision)
    assert "reentry_chase_unmeasured_size_floor" not in inspect.getsource(reentry_chase_decision)


def test_the_dead_enabled_parameter_is_gone():
    """Ang tanging call site ay nagta-hardcode ng `enabled=True`; ang flag ay sinusuri sa
    call site. Ang parameter ay patay na makinarya."""
    assert "enabled" not in inspect.signature(reentry_chase_decision).parameters


def test_unusable_inputs_fail_open():
    for kw in (
        {"anchor": None, "quote_anchor": None},
        {"risk_unit": 0.0, "quote_risk_unit": 0.0},
        {"live_price": None, "quote_price": None},
        {"cap_r": 0.0},
    ):
        admit, dbg = _decide(DLTH_MINUS, **kw)
        assert admit is True
        assert dbg["reason"] == "chase_inputs_unusable_fail_open"


# ── ANG ATR SOURCE AT ANG RISK-UNIT SOURCE AY MAGKAIBANG TANONG ─────────────────
def test_the_hardcoded_atr_fallback_is_named_and_the_old_value_is_unchanged():
    """177/177 na chase block ang may risk_unit_atr/anchor = EKSAKTONG 1.5000%%: ang
    `regime_atr_pct` ay tahimik na nagbabalik ng 0.015. Ang halaga ay hindi nagbago; ang
    KATAHIMIKAN ang binura."""
    assert regime_atr_pct_with_source({}) == (0.015, "fallback_0.015")
    assert regime_atr_pct_with_source({"atr_pct": None}) == (0.015, "fallback_0.015")
    assert regime_atr_pct_with_source({"atr_pct": 0.08}) == (0.08, "regime")
    assert regime_atr_pct_with_source({"meta": {"atr_pct": 0.06}}) == (0.06, "regime_meta")
    assert regime_atr_pct_with_source({"atr_pct": 5.0}) == (0.12, "regime")   # clamp
    for rg in ({}, {"atr_pct": 0.08}, {"meta": {"atr_pct": 0.06}}, {"atr_pct": "x"}):
        assert regime_atr_pct(rg) == regime_atr_pct_with_source(rg)[0]


def test_the_receipt_carries_every_binding_and_is_json_safe():
    _, dbg = _decide(TNON_FAR)
    b = dbg["binding"]
    assert b["admission_rule"] == "signed_tape_accel>0 AND buy_share_delta>0"
    assert b["size_band"] == "none_measured"
    assert "legacy_time_split" in b["derivations"]
    for key in ("live_price", "anchor", "risk_unit", "chase_cap_r", "quote_price",
                "quote_anchor", "atr_pct_source", "risk_unit_source",
                "tape_feature_contract", "tape_accel", "buy_share_delta",
                "extension_atr", "extension_atr_quote_basis", "chase_ceiling",
                "chase_ceiling_quote_basis", "above_band", "above_band_print_basis",
                "above_band_quote_basis", "band_basis", "reason"):
        assert key in dbg, key
    json.dumps(dbg)  # nakatira ito sa event payload


# ── ANG SHIPPED GATE MISMO (tinatawag, hindi hinahati sa string) ────────────────
class _Tick:
    def __init__(self, ask=None, mid=None):
        self.ask, self.mid = ask, mid


class _Via:
    def __init__(self, regime=None):
        self.regime_snapshot_json = regime if regime is not None else {}
        self.viability_score = 0.9


class _Sess:
    id = 1
    symbol = "DLTH"


@pytest.fixture()
def emitted(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: seen.append((ev, payload)))
    return seen


def _le(**over) -> dict:
    prior = {
        "was_loss": True, "high_water_mark": 4.28, "exit_price": 4.20,
        "risk_dist": 0.11, "exit_reason": "stop",
    }
    prior.update(over.pop("prior", {}))
    le = {"g4_prior_trade": prior}
    le.update(over)
    return le


def _g4e(**over) -> dict:
    d = {
        "price": 4.37, "price_kind": "last_print", "prior_high_print": 4.28,
        "tape_accel": -7491.0, "buy_share_delta": -0.09959362461201882,
        "tape_source_stale": False, "tape_feature_contract": "legacy_time_split",
    }
    d.update(over)
    return d


def test_the_shipped_gate_waits_on_dlth_and_writes_the_ledger_row(emitted):
    """Ang buong daan, tinatawag: DLTH 09-03 10:44:40 — print 4.37 (sa LOOB ng banda),
    ask 4.40 (sa ITAAS), tape −7,491/−0.0996 ⇒ WAIT, at may hilera sa libro."""
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via(), tick=_Tick(ask=4.40),
        g4e_dbg=_g4e(), trigger_reason="abcd_break_tick_ok",
    )
    assert admit is False
    assert dbg["reason"] == "reentry_chase_tape_wait"
    assert dbg["band_basis"] == "quote"
    assert dbg["anchor_kind"] == "prior_leg_high_print"
    assert dbg["price_kind"] == "last_print"
    assert [ev for ev, _ in emitted] == ["momentum_reentry_chase_blocked"]
    payload = emitted[0][1]
    assert payload["blocked_trigger"] == "abcd_break_tick_ok"
    assert payload["prior_anchor_hwm"] == pytest.approx(4.28)
    assert payload["risk_unit_atr"] == pytest.approx(0.0642)
    assert payload["prior_exit_reason"] == "stop"
    json.dumps(payload)


def test_the_shipped_gate_admits_a_tape_plus_and_names_it_in_the_ledger(emitted):
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via(), tick=_Tick(ask=4.40),
        g4e_dbg=_g4e(tape_accel=8800.0, buy_share_delta=0.31),
        trigger_reason="abcd_break_tick_ok",
    )
    assert admit is True
    assert dbg["reason"] == "reentry_chase_tape_admit"
    assert [ev for ev, _ in emitted] == ["momentum_reentry_chase_tape_admit"]
    assert emitted[0][1]["trigger"] == "abcd_break_tick_ok"


def test_the_shipped_gate_is_silent_inside_both_bands(emitted):
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via(), tick=_Tick(ask=4.30),
        g4e_dbg=_g4e(price=4.29), trigger_reason="abcd_break_tick_ok",
    )
    assert admit is True
    assert dbg["reason"] == "reentry_chase_within_band"
    assert emitted == []


def test_a_green_prior_leg_is_never_touched(emitted):
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), _le(prior={"was_loss": False}), _Via(), tick=_Tick(ask=9.99),
        g4e_dbg=_g4e(), trigger_reason="abcd_break_tick_ok",
    )
    assert (admit, dbg) == (True, None)
    assert emitted == []


def test_a_reference_only_seed_is_never_a_chase_anchor(emitted):
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), _le(prior={"seeded_reference_only": True}), _Via(),
        tick=_Tick(ask=9.99), g4e_dbg=_g4e(), trigger_reason="abcd_break_tick_ok",
    )
    assert (admit, dbg) == (True, None)


def test_no_tape_at_all_still_judges_on_the_quote_basis_and_waits(emitted):
    """Crypto (walang equity tick tape), flag-off na escalation check, at ang fail-open na
    exception handler ng ibang gate ay LAHAT dumarating dito nang walang `_g4e_dbg`."""
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via(), tick=_Tick(ask=4.40),
        g4e_dbg=None, trigger_reason="abcd_break_tick_ok",
    )
    assert admit is False
    assert dbg["reason"] == "reentry_chase_tape_unreadable_wait"
    assert dbg["anchor_kind"] == "prior_quote_hwm"
    assert dbg["price_kind"] == "quote_ask_fallback"
    assert dbg["tape_source"] == "unread"


def test_the_risk_unit_source_is_its_own_question_not_the_atr_reads(emitted):
    """Ang unang anyo ay nagtatakda ng `atr_pct_source = "prior_risk_dist"` NANG WALANG
    KONDISYON sa fallback — kaya ang TUNAY na `regime` na pagbasa ay naiuulat bilang bigo,
    at ang `prior_risk_dist` (na SUKAT din — ang stop ng naunang leg) ay nakukwenta bilang
    "walang sukat". Dalawang magkaibang tanong, dalawang field ngayon."""
    _, real = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via({"atr_pct": 0.08}), tick=_Tick(ask=4.40),
        g4e_dbg=_g4e(), trigger_reason="t",
    )
    assert real["atr_pct_source"] == "regime"
    assert real["risk_unit_source"] == "regime_atr_pct:regime"
    assert real["risk_unit"] == pytest.approx(0.08 * 4.28)
    # Ang ATR read ay nabigo (walang anchor mula sa ATR) pero ang prior stop ay alam.
    _, fallback = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via({"atr_pct": "x"}), tick=_Tick(ask=4.40),
        g4e_dbg=_g4e(), trigger_reason="t",
    )
    assert fallback["risk_unit_source"] == "regime_atr_pct:fallback_0.015"
    _, no_atr = lr._reentry_chase_gate(
        None, _Sess(), _le(), object(), tick=_Tick(ask=4.40),
        g4e_dbg=_g4e(), trigger_reason="t",
    )
    assert no_atr["atr_pct_source"] is None
    assert no_atr["risk_unit_source"] == "prior_risk_dist"
    assert no_atr["risk_unit"] == pytest.approx(0.11)


def test_the_halt_reanchor_still_moves_the_anchor_and_says_so(emitted):
    """XPON 2026-08-26: ang LULD halt ay muling nagpepresyo ng pangalan sa auction, kaya
    ang anchor ay tumataas sa resumption open at ang MAAGANG resume drive ay pumapasok."""
    le = _le(prior={"exited_at_utc": "2026-08-26T13:00:00"},
             halt_resumption_open=4.90, halt_resumed_at_utc="2026-08-26T13:05:00")
    admit, dbg = lr._reentry_chase_gate(
        None, _Sess(), le, _Via(), tick=_Tick(ask=4.40),
        g4e_dbg=_g4e(), trigger_reason="halt_resume_dip_ok",
    )
    assert dbg["anchor_kind"] == "halt_resumption_open"
    assert dbg["halt_reanchored"] is True
    assert dbg["anchor"] == pytest.approx(4.90)
    assert admit is True and dbg["reason"] == "reentry_chase_within_band"


def test_the_flag_off_gate_says_nothing(monkeypatch, emitted):
    monkeypatch.setattr(lr.settings, "chili_momentum_reentry_chase_cap_enabled", False)
    out = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via(), tick=_Tick(ask=9.99),
        g4e_dbg=_g4e(), trigger_reason="t",
    )
    assert out == (True, None)
    assert emitted == []


def test_a_zero_cap_disables_the_gate(monkeypatch, emitted):
    monkeypatch.setattr(lr.settings, "chili_momentum_reentry_chase_cap_r", 0.0)
    out = lr._reentry_chase_gate(
        None, _Sess(), _le(), _Via(), tick=_Tick(ask=9.99),
        g4e_dbg=_g4e(), trigger_reason="t",
    )
    assert out == (True, None)


def test_the_gate_opens_no_new_db_read():
    """Ang tape ay ang PAREHONG `_g4e_dbg` na kinuwenta na ng escalation check sa tick na
    ito: walang pangalawang query, parehong as-of."""
    assert "signed_tape_accel_features" not in GATE_SOURCE
    assert "_top_ranked_live_eligible_symbol" not in GATE_SOURCE
    assert "tape_confirms_hold" not in GATE_SOURCE
    assert "db.execute" not in GATE_SOURCE
    assert "g4e_dbg" in GATE_SOURCE


# ── (R6) PAREHONG PINTO NG PASOK ANG DUMADAAN SA GATE ───────────────────────────
def test_both_entry_doors_run_the_chase_gate():
    """Ang momentum-continuation fire ay lumilipat nang DIRETSO sa
    STATE_LIVE_ENTRY_CANDIDATE; bago ang review fix na ito ay hindi nito kailanman
    nakikita ang chase guard (sinukat: BIAF 2026-09-03, continuation fire 09:10:50 at
    chase block sa standard path makalipas ang 19 s). Dalawang call site ngayon, at ang
    isa ay nagpapangalan sa pintong iyon."""
    assert SOURCE.count("_reentry_chase_gate(") == 2
    assert 'trigger_reason="momentum_continuation",' in SOURCE
    i_cont = SOURCE.index('trigger_reason="momentum_continuation",\n')
    i_transition = SOURCE.index("_safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)", i_cont)
    assert i_cont < i_transition                      # bago ang paglipat, hindi pagkatapos


def test_the_continuation_gate_reuses_the_same_ticks_escalation_tape():
    first = SOURCE.index("_reentry_chase_gate(")
    second = SOURCE.index("_reentry_chase_gate(", first + 1)
    call = SOURCE[second:SOURCE.index(")", SOURCE.index("trigger_reason=", second))]
    assert "g4e_dbg=_mcg_dbg" in call
    assert 'trigger_reason="momentum_continuation"' in call
    assert "_mcg_dbg = None" in SOURCE        # walang NameError kapag OFF ang G4 flag


# ── ANG PATAY NA MAKINARYA AY BINURA (at ang DAHILAN ay itinama) ────────────────
def test_the_leader_ignition_bypass_and_its_flag_are_gone():
    """BINURA — pero HINDI dahil sa "0 event laban sa 187 block". Ang lane ay tumatakbo nang
    may `CHILI_MOMENTUM_CHASE_CAP_LEADER_BYPASS_ENABLED=0` sa sarili nitong `.env`, kaya
    naka-OFF ang switch sa LAHAT ng 187 na iyon at walang sinasabi ang bilang tungkol sa
    kakayahang maabot — ang derived na numero ay hindi kailanman pinatunayan laban sa
    config. Binura ito dahil ang KASO nito (ang tunay na bagong leg ng day leader, may
    bumibiling tape) ay eksakto ang pinapapasok ng tape admission, sa parehong tick, nang
    walang board read at walang pangalawang tape query."""
    assert "chili_momentum_chase_cap_leader_bypass_enabled" not in Settings.model_fields
    assert not hasattr(settings, "chili_momentum_chase_cap_leader_bypass_enabled")
    assert "momentum_reentry_chase_leader_bypass" not in SOURCE
    assert "momentum_reentry_chase_leader_bypass" not in GATE_SOURCE
    assert "_cc_bypass" not in SOURCE


def test_the_latch_description_no_longer_advertises_a_deleted_read_site():
    """Ang `chili_momentum_leader_definitive_latch_enabled` ay nag-aanunsyo ng "all three
    leader-read sites ... chase-cap bypass" — isa sa mga iyon ay binura ng PR na ito."""
    desc = Settings.model_fields["chili_momentum_leader_definitive_latch_enabled"].description or ""
    assert "chase-cap bypass" in desc          # pinangalanan pa rin ang nawala...
    assert "deleted by [46]" in desc           # ...at sinasabi nitong wala na ito
    assert "all three leader-read sites" not in desc
