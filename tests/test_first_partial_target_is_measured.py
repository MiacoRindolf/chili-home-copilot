"""[27b] ANG UNANG PARTIAL — SINUKAT SA HUGIS NA TALAGANG TUMATAKBO, HINDI SA HINULA.

ANG TANONG. Ang unang-partial na antas ay tumakbo sa 2.5R dahil iyon ang panalo ng A/B #1271
— pero ang A/B na iyon ay sumukat sa PLANO (ang buong hugis ng trade), hindi sa ANTAS NG
PARTIAL. Tinanong ng [27b] ang tape mismo: sa parehong 130 leg, saan dapat ibenta ang unang
piraso?

ANG UNANG SAGOT AY MALI, AT ANG REVIEW ANG NAGPATUNAY. Tatlong premise ng unang sweep ang
bumagsak, at LAHAT ay lumalala habang bumababa ang antas — mismong ang ehe na sinusukat:

  1. WALANG RUNNER sa tanging live lane (`alpaca_spot`, 1737/1737 session sa 7 araw): sa
     live ang `scaling` ay False doon, kaya `exit_qty = qty` at BUONG posisyon ang lumalabas
     sa target. Ang braso ay `1.0*T`, hindi `0.5*T + 0.5*R_all`.
  2. ANG PARTIAL ANG NAG-AARM NG BREAKEVEN RATCHET (`_scale_out_to_runner`), kaya ang `R_all`
     ay HINDI invariant sa mga braso — ang runner ay napuputol sa entry.
  3. ANG FILL AY HINDI ANG TOUCH: ang trigger ay `bid >= target*(1-0.005)` at ang sumusunod
     na MARKET sell ay nagfi-fill doon ⇒ realized ≈ `T − fill_floor_r`.

CORRECTED SWEEP (130 leg / 59 symbol-day; baseline na walang partial = −73.37 R; timbang
26 OCO-partial / 58 full-flatten mula sa 14-araw na bilang ng lane):

    0.50R +16.38 · 0.65R +20.20 · 0.70R **+21.86** · 0.80R +16.88 · 1.00R +7.58 · 2.50R −12.72

Parehong hugis ay nagpe-peak sa 0.70R (partial+BE +25.01, full-flatten +20.45). Jackknife sa
symbol-day: 0.70R ang argmax sa 58/59 (98%).

⚠️ PARAAN NG PAGSUSULAT NG FILE NA ITO (review 2026-09-10, finding 4). Ang mga bilang sa
itaas ay DOKUMENTASYON — hindi sila assertion. Ang isang test na nagsasabing
`SWEEP[0.7] > SWEEP[2.5]` kung saan pareho silang literal na nakasulat sa ITAAS ng file ay
hindi kayang bumagsak sa anumang pagbabago sa code; ganoon din ang pag-assert ng substring
ng production source. Kaya BAWAT test dito ay tumatawag sa produksyong leaf at bumabagsak
kapag nagbago ang GAWI. Dalawang pagbubukod lang, tahasang nakalabel bilang WIRING GUARD
(hindi sukat): na ang no-runner family set at ang Alpaca family set ay hindi pwedeng
maghiwalay, at na ang entry-side runway floor ay hindi tumuturo sa antas ng partial.

Runnable: pytest tests/test_first_partial_target_is_measured.py -v
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    ALPACA_EXECUTION_FAMILIES,
)
from app.services.trading.momentum_neural.exit_calibration import (
    mfe_percentile_target_r,
    mfe_sample_truncated_by_target,
)
from app.services.trading.momentum_neural.paper_execution import (
    PARTIAL_TRIGGER_TOLERANCE_FRAC,
    _NO_RUNNER_EXECUTION_FAMILIES,
    class_aware_reward_risk,
    consume_exit_intended_price,
    exit_intended_price,
    fill_floor_r,
    first_partial_target_r,
    first_partial_target_source,
    first_partial_target_with_floor,
    first_target_exit_shape,
    first_target_leaves_runner,
    meta_label_feature_target_price,
    partial_trigger_price,
    plan_geometry_target_price,
    runway_reward_risk_floor,
    stamp_exit_intended_price,
    stop_target_prices,
)

# ── ANG POPULASYON NG LEG, SINUKAT (14 araw, live, bounded read-only) ────────
# Ginagamit ito bilang INPUT sa mga tawag sa ibaba, hindi bilang inaasahang sagot.
STOP_PCT_P50, STOP_PCT_P25 = 0.024880, 0.015713      # n=88, live_entry_submitted
SPREAD_BPS_P50 = 40.99                                # n=84, momentum_fill_outcomes entry

# Ang TATLONG leg na ang hubad na trigger ay nasa ILALIM ng entry sa bagong antas
# (n=88, `live_entry_submitted`): (symbol, entry_px, stop_pct).
SUB_ENTRY_TRIGGER_LEGS = (
    ("SKYQ", 3.37, 0.00556),
    ("DPU", 2.88, 0.00571),
    ("SUNE", 3.01, 0.00623),
)


# ══════════════════════════════════════════════════════════════════════════════
# ANG BINDING VALUE — ang antas na ilalapag, at kung saan ito galing
# ══════════════════════════════════════════════════════════════════════════════
def test_the_shipped_default_is_the_level_the_lane_will_place():
    """Ang config default AT ang leaf na binabasa ng tatlong lane ay iisa."""
    assert Settings().chili_momentum_first_partial_target_r == pytest.approx(0.7)
    assert first_partial_target_r("AAPL") == pytest.approx(0.7)
    assert first_partial_target_r(None) == pytest.approx(0.7)


def test_the_first_partial_and_the_plan_rr_are_two_different_numbers():
    """Ang pagbaba ng PLANO ay magpapaluwag ng ENTRY gate; kaya hiwalay ang antas."""
    assert first_partial_target_r("AAPL") != class_aware_reward_risk("AAPL")
    assert class_aware_reward_risk("AAPL") == pytest.approx(2.5)


def test_the_source_is_derived_from_where_the_value_came_from():
    """RESIBONG HINDI NAGSISINUNGALING (review finding 12). Dati ay hubad na stamp ang
    `first_partial_base_source`, kaya ang 3.0 ng crypto at ang override ng operator ay
    parehong iniuulat bilang produkto ng isang EQUITY tape sweep na hindi kailanman
    gumawa ng mga halagang iyon."""
    assert first_partial_target_source("AAPL") == "tape_sweep_130_legs_corrected_0910"
    # crypto: ang antas ay 3.0 at ang pinagmulan ay ang CLASS, hindi ang equity sweep
    assert first_partial_target_r("ETH-USD") == class_aware_reward_risk("ETH-USD")
    assert first_partial_target_source("ETH-USD") == "crypto_class_reward_risk"
    assert first_partial_target_source("ETH-USD") != first_partial_target_source("AAPL")


def test_an_operator_override_is_reported_as_an_override(monkeypatch):
    from app.services.trading.momentum_neural import paper_execution as pe

    monkeypatch.setattr(pe.settings, "chili_momentum_first_partial_target_r", 1.5,
                        raising=False)
    assert first_partial_target_r("AAPL") == pytest.approx(1.5)
    assert first_partial_target_source("AAPL") == (
        "env_override:CHILI_MOMENTUM_FIRST_PARTIAL_TARGET_R"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ANG HUGIS — ang unang target ay BUONG posisyon sa lane na tumatakbo (blocking)
# ══════════════════════════════════════════════════════════════════════════════
def test_on_the_only_live_family_the_first_target_is_the_whole_position():
    """ANG SENTRO NG REVIEW. Ang sweep ay nag-credit ng `0.5*T + 0.5*R_all`; ang lane ay
    nagbebenta ng `1.0*T`. Ang predicate na nagdedesisyon niyan ay dito na nakatira, kaya
    may test na itong maaabot — dati itong nakabaon sa 50k-linyang tick function."""
    scaling, reason = first_target_exit_shape(
        can_split=True, partial_taken=False, execution_family="alpaca_spot"
    )
    assert scaling is False, "alpaca_spot ay hindi nag-iiwan ng runner"
    assert reason == "target", "ang buong posisyon ay lumalabas na may reason='target'"


def test_a_splittable_family_still_keeps_its_runner():
    scaling, reason = first_target_exit_shape(
        can_split=True, partial_taken=False, execution_family="coinbase_spot"
    )
    assert scaling is True
    assert reason == "scale_out_target"


@pytest.mark.parametrize(
    "can_split,partial_taken",
    [(False, False), (True, True), (False, True)],
)
def test_a_position_that_cannot_split_or_already_partialed_exits_whole(
    can_split: bool, partial_taken: bool
):
    """Walang dust na naiiwan at walang dobleng partial: pareho silang buong labasan."""
    scaling, reason = first_target_exit_shape(
        can_split=can_split, partial_taken=partial_taken,
        execution_family="coinbase_spot",
    )
    assert scaling is False and reason == "target"


def test_the_no_runner_family_set_cannot_drift_from_the_alpaca_set():
    """WIRING GUARD (hindi sukat): dalawang set ang naglalarawan ng IISANG katotohanan
    (ang resting deadman ay kumukonsumo ng buong qty_available). Ang paghihiwalay nila ay
    magbabalik ng `0.5*T + 0.5*R_all` na pag-aakala sa lane na nagfa-flatten nang buo."""
    assert set(_NO_RUNNER_EXECUTION_FAMILIES) == set(ALPACA_EXECUTION_FAMILIES)
    for fam in ALPACA_EXECUTION_FAMILIES:
        assert first_target_leaves_runner(fam) is False


# ══════════════════════════════════════════════════════════════════════════════
# ANG TRIGGER — ang "target" ay hindi kailanman nasa ilalim ng binayaran (blocking)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("symbol,entry,stop_pct", SUB_ENTRY_TRIGGER_LEGS)
def test_the_target_trigger_never_sits_below_the_entry_fill(
    symbol: str, entry: float, stop_pct: float
):
    """SINUKAT NA LEG. Sa `rr*stop_pct < tol/(1-tol)` ang hubad na `target*(1-tol)` ay
    bumabagsak sa ILALIM ng entry. Sa lane na walang runner iyon ay BUONG-posisyong
    labasan sa siguradong talo na naisusulat bilang `exit_reason='target'`."""
    rr = first_partial_target_r(symbol)
    target_px = entry * (1.0 + rr * stop_pct)
    naked = target_px * (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC)
    assert naked < entry, "ang leg na ito ay dapat nga sana bumagsak sa ilalim ng entry"

    trigger, floored = partial_trigger_price(target_px, entry_px=entry)
    assert floored is True
    assert trigger >= entry
    assert trigger <= target_px, "ang sahig ay hindi pwedeng lumagpas sa target mismo"


def test_the_floor_is_silent_wherever_the_naked_trigger_already_clears_entry():
    """Walang hinihigpitan: sa karaniwang leg (p50 stop) ang gawi ay byte-identical."""
    entry = 10.0
    target_px = entry * (1.0 + first_partial_target_r("AAPL") * STOP_PCT_P50)
    trigger, floored = partial_trigger_price(target_px, entry_px=entry)
    assert floored is False
    assert trigger == pytest.approx(target_px * (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC))


def test_the_crossover_is_exactly_where_the_algebra_says_it_is():
    """`rr*stop_pct < tol/(1-tol)`. Sinusuri ang KABUUANG hanay ng stop_pct, hindi
    tatlong sample: bawat leg sa ilalim ng crossover ay dapat na-floor, bawat leg sa
    itaas ay dapat hindi nagalaw."""
    rr = first_partial_target_r("AAPL")
    crossover = (PARTIAL_TRIGGER_TOLERANCE_FRAC / (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC)) / rr
    entry = 5.0
    for i in range(1, 400):
        stop_pct = i * 0.0001                       # 1 bps .. 4%
        target_px = entry * (1.0 + rr * stop_pct)
        trigger, floored = partial_trigger_price(target_px, entry_px=entry)
        if stop_pct < crossover * 0.999:
            assert floored is True and trigger >= entry, stop_pct
        elif stop_pct > crossover * 1.001:
            assert floored is False, stop_pct
        assert trigger <= target_px + 1e-12


def test_the_old_level_never_reached_the_crossover_so_this_is_introduced_by_the_change():
    """Ang depekto ay HINDI pre-existing: sa 2.5R ang crossover ay 0.201% stop_pct, na
    wala sa 88 na sinukat na leg (pinakamasikip = 0.556%)."""
    crossover_at = lambda rr: (  # noqa: E731
        (PARTIAL_TRIGGER_TOLERANCE_FRAC / (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC)) / rr
    )
    tightest_measured_stop_pct = min(sp for _s, _e, sp in SUB_ENTRY_TRIGGER_LEGS)
    assert crossover_at(2.5) < tightest_measured_stop_pct
    assert crossover_at(first_partial_target_r("AAPL")) > tightest_measured_stop_pct


def test_the_trigger_floor_fails_open_when_the_entry_is_unmeasurable():
    target_px = 10.0
    naked = target_px * (1.0 - PARTIAL_TRIGGER_TOLERANCE_FRAC)
    for bad in (None, 0.0, -1.0, float("nan")):
        trigger, floored = partial_trigger_price(target_px, entry_px=bad)
        assert floored is False
        assert trigger == pytest.approx(naked)


# ══════════════════════════════════════════════════════════════════════════════
# ANG FILL FLOOR — bumubuklat, hindi label
# ══════════════════════════════════════════════════════════════════════════════
def test_the_fill_floor_is_the_legs_own_two_measured_costs():
    """(trigger tolerance + spread) / stop_pct — walang nakatagong termino."""
    assert fill_floor_r(0.02, 0.0) == pytest.approx(PARTIAL_TRIGGER_TOLERANCE_FRAC / 0.02)
    assert fill_floor_r(0.02, 100.0) == pytest.approx(
        (PARTIAL_TRIGGER_TOLERANCE_FRAC + 0.01) / 0.02
    )
    # mas masikip na stop ⇒ mas mataas na floor (ang direksyon ang mahalaga, di ang bilang)
    assert fill_floor_r(STOP_PCT_P25, SPREAD_BPS_P50) > fill_floor_r(
        STOP_PCT_P50, SPREAD_BPS_P50
    )


def test_the_floor_lifts_the_placed_target_it_does_not_merely_label_it():
    """REVIEW FINDING 2/10: dati itong nagsusulat ng `applied_target_below_fill_floor` at
    saka inilalapag pa rin ang target sa ilalim ng sariling floor nito. Ngayon ay ang
    NAILAPAG na antas mismo ang umaangat."""
    base = first_partial_target_r("AAPL")
    tight_leg_floor = fill_floor_r(0.00556, SPREAD_BPS_P50)
    assert tight_leg_floor > base, "premise: ang leg na ito ay nasa loob ng floor"

    applied, meta = first_partial_target_with_floor(
        base, stop_pct=0.00556, spread_bps=SPREAD_BPS_P50, plan_rr=2.5
    )
    assert applied == pytest.approx(tight_leg_floor)
    assert meta["floor_binding"] is True
    assert applied > base


def test_the_floor_is_capped_by_the_plan_geometry():
    """Ang floor ng napakasikip na stop ay umaabot ng higit 8R; ang 8R na "target" ay
    hindi na target. Ang cap ay ang dokumentadong buong-trade na geometry."""
    applied, meta = first_partial_target_with_floor(
        first_partial_target_r("AAPL"), stop_pct=0.0006, spread_bps=SPREAD_BPS_P50,
        plan_rr=2.5,
    )
    assert applied == pytest.approx(2.5)
    assert meta["fill_floor_capped_at_plan_rr"] is True
    assert meta["fill_floor_r"] > 2.5


def test_the_typical_leg_is_untouched_by_the_floor():
    applied, meta = first_partial_target_with_floor(
        first_partial_target_r("AAPL"), stop_pct=STOP_PCT_P50,
        spread_bps=SPREAD_BPS_P50, plan_rr=2.5,
    )
    assert applied == pytest.approx(first_partial_target_r("AAPL"))
    assert meta["floor_binding"] is False


def test_the_floor_fails_open_to_the_base():
    for bad_stop in (None, 0.0, -0.01):
        applied, meta = first_partial_target_with_floor(
            0.7, stop_pct=bad_stop, spread_bps=SPREAD_BPS_P50, plan_rr=2.5
        )
        assert applied == pytest.approx(0.7)
        assert meta["fill_floor_r"] is None


def test_the_lifted_level_reaches_the_price_that_is_actually_placed():
    """Dulo-dulo: ang antas na ibinalik ng floor ay dapat ang antas ng PRESYO."""
    entry, atr = 4.01, 0.0156
    applied, _ = first_partial_target_with_floor(
        first_partial_target_r("AAPL"), stop_pct=0.00556,
        spread_bps=SPREAD_BPS_P50, plan_rr=2.5,
    )
    stop, target = stop_target_prices(
        entry, atr_pct=atr, reward_risk=applied, partial_capable=False
    )
    assert (target - entry) / (entry - stop) == pytest.approx(applied, abs=1e-9)


# ══════════════════════════════════════════════════════════════════════════════
# ANG MFE POOL — hindi natututo ang target ng sariling bakas
# ══════════════════════════════════════════════════════════════════════════════
def test_a_leg_that_exited_at_its_own_target_is_right_censored():
    """Ang pagsasara ng posisyon ang HUMINTO sa high-water mark, kaya ang `mfe_r` ay
    nagsasabi kung nasaan ang TARGET NATIN, hindi kung gaano kalayo ang galaw."""
    assert mfe_sample_truncated_by_target("target", 0.70, 0.70) is True
    assert mfe_sample_truncated_by_target("scale_out_target", 0.71, 0.70) is True
    # tunay na excursion na lumagpas: hindi censored
    assert mfe_sample_truncated_by_target("target", 3.10, 0.70) is False
    # ibang dahilan ng paglabas: hindi ang target ang humarang sa MFE
    assert mfe_sample_truncated_by_target("trail_stop", 0.70, 0.70) is False
    assert mfe_sample_truncated_by_target("stop", 0.70, 0.70) is False


def test_the_ratchet_down_is_what_the_filter_prevents():
    """Kung papasukin ang mga censored na sample, ang percentile na dapat MAG-ANGAT ay
    matututo ng sariling bakas at hindi na aangat kailanman."""
    base = first_partial_target_r("AAPL")
    censored = [base] * 40                 # bawat leg ay lumabas SA target
    unfiltered = mfe_percentile_target_r(
        censored, percentile=0.6, base_rr=base, min_samples=30
    )
    kept = [s for s in censored
            if not mfe_sample_truncated_by_target("target", s, base)]
    assert kept == [], "lahat sila ay right-censored"
    # ang na-filter na pool ay walang laman ⇒ ang base ang nananatili (walang ratchet down)
    filtered = mfe_percentile_target_r(
        kept, percentile=0.6, base_rr=base, min_samples=30
    )
    assert filtered["target_r"] == pytest.approx(base)
    assert filtered["source"] == "prior_only"
    assert unfiltered["target_r"] <= filtered["target_r"], (
        "ang hindi na-filter na pool ay hindi kayang mag-angat sa base"
    )


def test_a_pool_of_real_excursions_still_lifts_the_target():
    """Ang pangako ng config ('adapts UP') ay dapat buhay pa rin matapos ang filter."""
    base = first_partial_target_r("AAPL")
    real = [2.4, 3.1, 4.0, 2.9, 3.5] * 8
    kept = [s for s in real if not mfe_sample_truncated_by_target("target", s, base)]
    assert len(kept) == len(real)
    lifted = mfe_percentile_target_r(
        kept, percentile=0.6, base_rr=base, min_samples=30
    )
    assert lifted["target_r"] > base


# ══════════════════════════════════════════════════════════════════════════════
# ANG PAGLABAS AY NASUSUKAT NA — intended_price ay may sumusulat na
# ══════════════════════════════════════════════════════════════════════════════
def test_the_exit_reference_price_is_the_side_the_order_crosses():
    assert exit_intended_price(bid=9.90, ask=10.10, side_long=True) == pytest.approx(9.90)
    assert exit_intended_price(bid=9.90, ask=10.10, side_long=False) == pytest.approx(10.10)
    assert exit_intended_price(bid=None, ask=10.10, side_long=True) is None
    assert exit_intended_price(bid=0.0, ask=10.10, side_long=True) is None


def test_the_stamp_is_consumed_so_a_later_exit_cannot_inherit_a_stale_price():
    """REVIEW FINDING 9 (ang tunay na butas). Ang DALAWANG fill-outcome recorder ay
    matagal nang nagbabasa ng `last_exit_intended_price` at WALANG sumusulat nito, kaya
    NULL ang `intended_price` sa 106/106 na exit row at hindi nasusukat ang cost ng
    paglabas. Ngayon ay may nagtatatak — at ang recorder ay KUMUKUHA, hindi tumitingin,
    kaya ang ikalawang malayang exit ay hindi makakamana ng presyo ng una."""
    le: dict = {}
    assert consume_exit_intended_price(le) is None       # wala pang exit: NULL, tama

    stamp_exit_intended_price(le, bid=9.90, ask=10.10, side_long=True)
    assert consume_exit_intended_price(le) == pytest.approx(9.90)
    # ang ikalawang exit ay walang sariling sukat ⇒ NULL, HINDI ang 9.90 ng una
    assert consume_exit_intended_price(le) is None

    stamp_exit_intended_price(le, bid=8.50, ask=8.70, side_long=True)
    assert consume_exit_intended_price(le) == pytest.approx(8.50)


def test_an_unmeasurable_quote_records_nothing_rather_than_something_invented():
    le: dict = {"last_exit_intended_price": 9.90}
    stamp_exit_intended_price(le, bid=None, ask=None, side_long=True)
    # ang lumang tatak ay nananatili hanggang sa ito ay kunin; walang gawa-gawa
    assert consume_exit_intended_price(le) == pytest.approx(9.90)
    stamp_exit_intended_price(le, bid=float("nan"), ask=None, side_long=True)
    assert consume_exit_intended_price(le) is None


# ══════════════════════════════════════════════════════════════════════════════
# ANG PLANO AY HINDI GINALAW — ang ENTRY gate ay hindi lumuwag
# ══════════════════════════════════════════════════════════════════════════════
def test_the_entry_runway_floor_is_the_plan_not_the_partial():
    """Ang runway affordability ay isang ENTRY gate. Kung sinundan nito ang unang
    partial, ang runway na 2.0R — na tinatanggihan ngayon — ay biglang tatanggapin."""
    assert runway_reward_risk_floor("AAPL") == pytest.approx(class_aware_reward_risk("AAPL"))
    assert runway_reward_risk_floor("AAPL") > first_partial_target_r("AAPL")
    runway_rr = 2.0
    assert runway_rr < runway_reward_risk_floor("AAPL")        # tinatanggihan (tama)
    assert runway_rr > first_partial_target_r("AAPL")          # tatanggapin kung isa lang


def test_the_dipbuy_runway_block_reads_the_plan_leaf_not_the_partial_leaf():
    """WIRING GUARD (hindi sukat — ito ang ikalawa at huli). Ang runway affordability ay
    ang NAG-IISANG ENTRY gate na nagbabasa ng isang R:R, at ang pagpapalit nito sa antas
    ng partial ay magpapaluwag ng pasok nang tahimik. Ang gate ay nasa loob ng isang
    daang-linyang function, kaya ang tanging paraan upang bantayan ang KABIT ay ang
    basahin ang BLOKE mismo. (Ang setup-selector ranking sa ibang bahagi ng file ay
    SADYANG ginagamit ang antas ng partial — iyon ang geometry na ilalapag nito.)"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "app/services/trading/momentum_neural/entry_gates.py").read_text(
        encoding="utf-8", errors="replace")
    block = src[src.index("def _dipbuy_signals_ok"):src.index("def _evaluate_deep_reclaim")]
    # ang komento ay NANGANGATWIRAN tungkol sa dalawang leaf; ang KODIGO ang binabantayan
    code = "\n".join(
        ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "runway_reward_risk_floor(symbol)" in code
    assert "first_partial_target_r" not in code, (
        "ang antas ng unang partial ay tumagos sa isang ENTRY gate"
    )


def test_the_meta_label_feature_describes_the_plan_and_says_so():
    """REVIEW FINDING 6. Ang `size_multiplier` ay natutunan sa mga row na isinulat sa
    2.5R na geometry; ang pagpapakain ng 0.70R ay tahimik na pag-shift ng input
    distribution ng isang LIVE sizing lever. Kaya ito ay nananatili sa plano — at ang
    agwat ay may pangalan."""
    entry, stop = 10.0, 9.0
    feat = meta_label_feature_target_price(entry, stop, symbol="AAPL")
    assert feat == pytest.approx(plan_geometry_target_price(entry, stop, symbol="AAPL"))
    assert feat == pytest.approx(entry + class_aware_reward_risk("AAPL") * (entry - stop))
    # ...at ito ay ibang geometry kaysa sa ilalapag ng unang order
    first = entry + first_partial_target_r("AAPL") * (entry - stop)
    assert feat > first
    # hindi ito gumagalaw kapag ginalaw ang unang partial
    assert meta_label_feature_target_price(entry, stop, symbol="AAPL") == pytest.approx(feat)


def test_the_plan_geometry_leaf_fails_open():
    assert plan_geometry_target_price(10.0, 10.0, symbol="AAPL") is None   # zero risk
    assert plan_geometry_target_price(10.0, 11.0, symbol="AAPL") is None   # stop > entry
    assert plan_geometry_target_price(None, 9.0, symbol="AAPL") is None


# ══════════════════════════════════════════════════════════════════════════════
# ANG GEOMETRY — ang target ay talagang lumalapit
# ══════════════════════════════════════════════════════════════════════════════
def test_the_target_actually_moves_in_and_the_stop_does_not_move():
    entry, atr = 4.01, 0.0156
    stop_a, t_plan = stop_target_prices(
        entry, atr_pct=atr, reward_risk=class_aware_reward_risk("AAPL"),
        partial_capable=False,
    )
    stop_b, t_new = stop_target_prices(
        entry, atr_pct=atr, reward_risk=first_partial_target_r("AAPL"),
        partial_capable=False,
    )
    assert stop_a == stop_b, "ang antas ng partial ay hindi ginagalaw ang STOP"
    assert t_new < t_plan
    r = entry - stop_a
    assert (t_new - entry) / r == pytest.approx(first_partial_target_r("AAPL"), abs=1e-9)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
