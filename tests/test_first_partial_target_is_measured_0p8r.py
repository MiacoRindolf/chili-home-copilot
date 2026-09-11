"""[27b] ANG UNANG PARTIAL AY 0.8R — SINUKAT SA PRINT, HINDI PINILI.

ANG TANONG. Ang unang-partial na antas ay tumatakbo sa 2.5R dahil iyon ang panalo ng A/B
#1271 — pero ang A/B na iyon ay sumukat sa PLANO (ang buong hugis ng trade), hindi sa
ANTAS NG PARTIAL. Tinanong ng [27b] ang tape mismo: sa parehong 130 leg, sa parehong entry
at sa parehong HULING PRESYO, saan dapat ibenta ang unang piraso?

ANG SUKAT (130 leg / 59 symbol-day; live `momentum_mfe_realized` legs na may stop_distance,
entry/exit mula `momentum_fill_outcomes`, tape = `iqfeed_trade_ticks` sa pagitan ng entry at
exit). Dalawang braso na parehong nagtatapos sa IISANG huling presyo — ang partial LANG ang
pagkakaiba, kaya walang double-count:

    BASELINE  = lahat sa huling presyo                                   -73.37 R
    PARTIAL   = 0.5*T + 0.5*R_all kung may PRINT >= entry + T*stop, else R_all

    0.3R +34.23 · 0.4R +33.59 · 0.5R +30.03 · 0.6R +31.32 · 0.7R +31.84
    0.8R +28.15 · 0.9R +26.49 · 1.0R +21.60 · 1.2R +15.00 · 1.5R +14.77
    2.0R +15.19 · 2.5R  +3.76 · 3.0R  +2.75 · 4.0R  +1.64

BAKIT HINDI ANG 0.3-0.7 NA PLATEAU. Ang sweep ay nagbibilang ng PRINT TOUCH; ang live na
partial ay nangangailangan ng BID na umabot sa `target * 0.995` at pagkatapos ay isang benta
na tumatawid sa spread. SINUKAT ang dalawang bayad na iyon (14 araw, live):
    stop_pct   p50 2.488% / p25 1.571%   (n=88, live_entry_submitted)
    spread     p50 41.0 bps / p75 61.1   (n=84, momentum_fill_outcomes entry)
    => fill floor 0.37R (p50 stop) / 0.58R (p25 stop)
Ang 0.3-0.5R ay NASA LOOB ng floor na iyon — ang tape ay tumatama, ang bid ay hindi. 0.58 +
0.165 (isang p50 spread) = 0.745 => ang 0.8R ang UNANG grid level na may buong spread na
margin. Ito ang pinakamababang MAAANING antas, hindi ang pinakamataas na iskor.

BAKIT HINDI BINABA ANG IISANG KNOB. Ang `chili_momentum_risk_reward_risk_ratio` (2.5) ay
hindi lang target — ito rin ang bumabantay sa dip-buy runway affordability (ENTRY), sa
setup-selector ranking, sa trail patience at sa `arm_r` ng exit ratchets. Ang pagbaba nito
ay tahimik na magpapaluwag ng ENTRY gate. Kaya BAGONG NAMED VALUE, hindi bagong halaga sa
luma. Walang enable knob: LIVE at ON, iniuulat sa resibo.

HANGGANAN (nakasulat, hindi itinatago): ang BUNTOT (peak>=5R, n=5) ay gustong WALANG maagang
partial (-19.07 sa 0.8R vs -14.82 sa 2.5R). Magkasalungat ang katawan at ang buntot; ang
tunay na lunas ay per-leg na tail classifier. Hangga't wala iyon, ang net ay pabor sa MABABA.

Runnable: pytest tests/test_first_partial_target_is_measured_0p8r.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings
from app.services.trading.momentum_neural.paper_execution import (
    PARTIAL_TRIGGER_TOLERANCE_FRAC,
    class_aware_reward_risk,
    fill_floor_r,
    first_partial_target_r,
    stop_target_prices,
)

_ROOT = Path(__file__).resolve().parents[1]
_LIVE = _ROOT / "app/services/trading/momentum_neural/live_runner.py"
_PAPER = _ROOT / "app/services/trading/momentum_neural/paper_runner.py"
_REPLAY = _ROOT / "app/services/trading/momentum_neural/replay_v2.py"
_GATES = _ROOT / "app/services/trading/momentum_neural/entry_gates.py"

# ── ANG SUKAT, BILANG DATOS (130 leg / 59 symbol-day, baseline -73.37 R) ──────
SWEEP_VS_BASELINE_R = {
    0.3: +34.23, 0.4: +33.59, 0.5: +30.03, 0.6: +31.32, 0.7: +31.84,
    0.8: +28.15, 0.9: +26.49, 1.0: +21.60, 1.2: +15.00, 1.5: +14.77,
    2.0: +15.19, 2.5: +3.76, 3.0: +2.75, 4.0: +1.64,
}
BODY_VS_BASELINE_R = {0.8: +47.22, 2.5: +18.58}   # peak < 5R, n = 125
TAIL_VS_BASELINE_R = {0.8: -19.07, 2.5: -14.82}   # peak >= 5R, n = 5
LARGEST_LEG_SHARE_OF_GAIN = 0.10                  # AUUD 09-01 = 10% ng pakinabang
N_LEGS, N_SYMBOL_DAYS, N_BODY, N_TAIL = 130, 59, 125, 5

# ── ANG FILL FLOOR, BILANG DATOS (14 araw, live; bounded read-only queries) ───
STOP_PCT_P50, STOP_PCT_P25, N_STOP_SAMPLES = 0.024880, 0.015713, 88
SPREAD_BPS_P50, SPREAD_BPS_P75, N_SPREAD_SAMPLES = 40.99, 61.07, 84


@pytest.fixture(scope="module")
def live_src() -> str:
    return _LIVE.read_text(encoding="utf-8", errors="replace")


# ── ANG BINDING VALUE ────────────────────────────────────────────────────────
def test_the_default_is_the_measured_level():
    assert Settings().chili_momentum_first_partial_target_r == 0.8


def test_equity_first_partial_resolves_to_the_measured_level():
    assert first_partial_target_r("AAPL") == pytest.approx(0.8)
    assert first_partial_target_r(None) == pytest.approx(0.8)


def test_the_measured_level_beats_the_plan_rr_on_the_same_legs():
    """Ang buong dahilan ng PR, nakasulat bilang datos: parehong 130 leg, parehong
    huling presyo, ang partial LANG ang pagkakaiba."""
    assert SWEEP_VS_BASELINE_R[0.8] > SWEEP_VS_BASELINE_R[2.5]
    assert SWEEP_VS_BASELINE_R[0.8] - SWEEP_VS_BASELINE_R[2.5] == pytest.approx(24.39, abs=0.01)
    assert Settings().chili_momentum_first_partial_target_r in SWEEP_VS_BASELINE_R


def test_the_body_and_the_tail_disagree_and_we_say_so():
    """HINDI ito unanimous. Ang katawan ay gusto ng MAAGA, ang buntot ay ayaw.
    Ang net ay pabor sa mababa dahil n=125 ang katawan at n=5 lang ang buntot."""
    assert BODY_VS_BASELINE_R[0.8] > BODY_VS_BASELINE_R[2.5]      # +47.22 vs +18.58
    assert TAIL_VS_BASELINE_R[0.8] < TAIL_VS_BASELINE_R[2.5]      # -19.07 vs -14.82
    assert N_BODY + N_TAIL == N_LEGS
    net = BODY_VS_BASELINE_R[0.8] + TAIL_VS_BASELINE_R[0.8]
    assert net == pytest.approx(SWEEP_VS_BASELINE_R[0.8], abs=0.02)


def test_the_result_is_not_one_lucky_leg():
    """Ang overfit na hugis ay iisang leg na may hawak ng buong pakinabang."""
    assert LARGEST_LEG_SHARE_OF_GAIN < 0.5
    assert N_SYMBOL_DAYS >= 30 and N_LEGS >= 100


# ── ANG FILL FLOOR: BAKIT 0.8 AT HINDI ANG MAS MATAAS NA ISKOR NA 0.3-0.7 ────
def test_the_fill_floor_is_computed_from_the_legs_own_stop_and_spread():
    """(trigger tolerance + spread) / stop_pct — dalawang bayad, parehong nasusukat."""
    assert fill_floor_r(STOP_PCT_P50, SPREAD_BPS_P50) == pytest.approx(0.366, abs=0.005)
    assert fill_floor_r(STOP_PCT_P25, SPREAD_BPS_P50) == pytest.approx(0.579, abs=0.005)
    # eksaktong porma, walang nakatagong termino
    assert fill_floor_r(0.02, 0.0) == pytest.approx(PARTIAL_TRIGGER_TOLERANCE_FRAC / 0.02)
    assert fill_floor_r(0.02, 100.0) == pytest.approx((0.005 + 0.01) / 0.02)


def test_the_richer_plateau_sits_inside_the_fill_floor_so_it_is_not_claimed():
    """Mas mataas ang iskor ng 0.3-0.5R — at hindi ito maaabot ng BID. Hindi natin
    inaangkin ang hindi natin mafi-fill."""
    floor_p25 = fill_floor_r(STOP_PCT_P25, SPREAD_BPS_P50)
    for level in (0.3, 0.4, 0.5):
        assert SWEEP_VS_BASELINE_R[level] > SWEEP_VS_BASELINE_R[0.8]   # mas mataas ang iskor
        assert level < floor_p25                                       # ...at nasa loob ng floor


def test_0p8_clears_the_p25_floor_by_a_full_spread():
    """0.58 + 0.165 = 0.745 -> ang 0.8 ang unang grid level sa itaas noon."""
    floor_p25 = fill_floor_r(STOP_PCT_P25, SPREAD_BPS_P50)
    one_spread_in_r = (SPREAD_BPS_P50 / 10_000.0) / STOP_PCT_P50
    assert one_spread_in_r == pytest.approx(0.165, abs=0.005)
    assert 0.8 >= floor_p25 + one_spread_in_r
    grid = sorted(SWEEP_VS_BASELINE_R)
    first_clear = next(x for x in grid if x >= floor_p25 + one_spread_in_r)
    assert first_clear == 0.8, "ang 0.8 ay ang UNANG maaaning level, hindi ang pinili"
    assert Settings().chili_momentum_first_partial_target_r == first_clear


def test_fill_floor_is_fail_soft():
    assert fill_floor_r(None, 40.0) is None
    assert fill_floor_r(0.0, 40.0) is None
    assert fill_floor_r(-0.01, 40.0) is None
    assert fill_floor_r(0.02, None) == pytest.approx(0.25)   # walang spread => tolerance lang
    assert fill_floor_r(0.02, float("nan")) == pytest.approx(0.25)


# ── ANG PLANO'NG R:R AY HINDI GINALAW (ang ENTRY gate ay hindi lumuwag) ───────
def test_the_plan_rr_is_untouched():
    assert Settings().chili_momentum_risk_reward_risk_ratio == 2.5
    assert class_aware_reward_risk("AAPL") == pytest.approx(2.5)


def test_the_two_levels_are_separate_values():
    assert first_partial_target_r("AAPL") != class_aware_reward_risk("AAPL")
    assert Settings(
        CHILI_MOMENTUM_FIRST_PARTIAL_TARGET_R=1.3
    ).chili_momentum_first_partial_target_r == 1.3
    assert Settings(
        CHILI_MOMENTUM_FIRST_PARTIAL_TARGET_R=1.3
    ).chili_momentum_risk_reward_risk_ratio == 2.5


def test_the_dipbuy_runway_gate_still_reads_the_plan_rr():
    """ANG COUPLING NA IWINASAN. Ang runway affordability ay isang ENTRY gate na
    nagbabasa ng parehong knob. Kung binaba natin ang 2.5 sa 0.8, ang gate na ito ay
    tatanggap ng runway na dati'y tinatanggihan — tahimik na pagpapaluwag ng ENTRY."""
    src = _GATES.read_text(encoding="utf-8", errors="replace")
    block = src[src.index("def _dipbuy_signals_ok"):src.index("def _evaluate_deep_reclaim")]
    assert "rr_target = float(class_aware_reward_risk(symbol))" in block
    assert "runway_rr_unaffordable" in block
    assert "first_partial_target_r" not in block, (
        "ang unang-partial na antas ay tumagos sa isang ENTRY gate"
    )
    # at ang desisyon mismo: runway 2.0 ay TINATANGGIHAN sa 2.5, TATANGGAPIN sana sa 0.8
    runway_rr = 2.0
    assert runway_rr < class_aware_reward_risk("AAPL")          # declined ngayon (tama)
    assert runway_rr > first_partial_target_r("AAPL")           # tatanggapin sana kung isa lang


def test_the_exit_ratchets_still_arm_off_the_plan_rr():
    """`arm_r = max(0.5, arm_frac*rr)`. Sa 0.8 ito ay 0.5R — mag-aarm ang bawat ratchet
    halos kaagad. Hindi iyon ang sinukat ng [27b], kaya hindi ito ginalaw."""
    replay = _REPLAY.read_text(encoding="utf-8", errors="replace")
    for fn in ("ofi_exhaustion_lock", "tape_accel_reversal_exit",
               "sell_into_strength_ladder", "ask_side_pressure_lock"):
        assert fn in replay
    assert replay.count("reward_risk=class_aware_reward_risk(s)") >= 4, (
        "ang exit ratchets ay dapat manatili sa plano'ng R:R"
    )
    assert "reward_risk=first_partial_target_r(s)" in replay, (
        "ang UNANG TARGET ng replay ay dapat sumunod sa live"
    )


# ── PARITY: pareho ang antas na inilalapag ng live, ng paper at ng replay ────
def test_live_paper_and_replay_place_the_same_first_target(live_src: str):
    assert "_base_rr = float(first_partial_target_r(sess.symbol))" in live_src
    assert "_plan_rr = float(class_aware_reward_risk(sess.symbol))" in live_src
    paper = _PAPER.read_text(encoding="utf-8", errors="replace")
    assert "requested_reward_risk = first_partial_target_r(sess.symbol)" in paper
    # ...at ang parehong leaf ang tinatawag ng tatlo, kaya iisa ang halaga kada simbolo
    for sym in ("AAPL", "TNON", "ETH-USD", "ORCA-USD"):
        assert first_partial_target_r(sym) == first_partial_target_r(sym.lower().upper())


def test_crypto_is_byte_identical_because_the_sweep_was_equity_tape():
    """Ang sukat ay EQUITY tape. Walang inaangkin sa crypto, kaya ang crypto override
    (3.0) ang nananatiling antas — max(0.8, 3.0) == max(2.5, 3.0)."""
    assert first_partial_target_r("ETH-USD") == class_aware_reward_risk("ETH-USD")
    assert first_partial_target_r("ETH-USD") == pytest.approx(3.0)


# ── ANG RESIBO: ang bumubuklat na halaga ay iniuulat ────────────────────────
def test_the_receipt_reports_the_binding_value_and_its_floor(live_src: str):
    for key in ("first_partial_base_r", "first_partial_base_source", "plan_rr",
                "fill_floor_r", "fill_floor_stop_pct", "fill_floor_spread_bps",
                "fill_floor_trigger_tolerance_frac", "applied_target_below_fill_floor"):
        assert f'"{key}"' in live_src, f"nawawala sa resibo: {key}"
    assert '"tape_sweep_130_legs_0910"' in live_src


def test_the_trigger_tolerance_has_a_name_now(live_src: str):
    """Ang 0.005 ay KALAHATI ng fill floor; hindi ito pwedeng manatiling walang pangalan."""
    assert PARTIAL_TRIGGER_TOLERANCE_FRAC == 0.005
    assert (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC) == 0.995, "dapat byte-identical"
    assert "bid >= target_px * (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC)" in live_src
    assert not re.search(r"bid >= target_px \* 0\.995", live_src)


def test_the_config_carries_the_derivation_not_just_the_number():
    """Walang magic number: ang description ang dapat magpaliwanag kung saan galing."""
    fields = Settings.model_fields
    desc = (fields["chili_momentum_first_partial_target_r"].description or "")
    assert "130 legs" in desc and "+28.15" in desc and "+3.76" in desc
    assert "fill floor" in desc.lower()
    plan_desc = (fields["chili_momentum_risk_reward_risk_ratio"].description or "")
    assert plan_desc, "[37]: ang plano'ng R:R ay walang description dati"
    assert "#1271" in plan_desc and "NO LONGER the first-partial level" in plan_desc


# ── ANG GEOMETRY: ang target ay talagang lumalapit ──────────────────────────
def test_the_target_actually_moves_in():
    entry, atr = 4.01, 0.0156
    stop_a, t_plan = stop_target_prices(entry, atr_pct=atr, reward_risk=2.5, partial_capable=True)
    stop_b, t_new = stop_target_prices(entry, atr_pct=atr, reward_risk=0.8, partial_capable=True)
    assert stop_a == stop_b, "ang antas ng partial ay hindi ginagalaw ang STOP"
    assert t_new < t_plan
    r = entry - stop_a
    assert (t_new - entry) / r == pytest.approx(0.8, abs=1e-9)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
