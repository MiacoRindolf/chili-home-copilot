"""[62] — ANG PULLBACK→BAGONG-HIGH CYCLE SCANNER.

"Marami ring talo kasi nag-enter sa backside after tuloy-tuloy na successful pullbacks"
(operator 2026-09-10 23:20Z). Ang scanner ang bumibilang ng serye: ilang kumpletong
pullback→bagong-high cycle na ang natapos sa symbol-day, at lumiliit na ba ang kasalukuyang
spike kumpara sa huli. Print-indexed lahat — walang orasan, walang bar.

Sinusuri dito ang DALISAY na core (0/1/2 cycle, buong retrace, onset restart, JSON round-trip,
incremental == batch) at ang conditioning (monotone, size-DOWN lamang, floor, fail-open).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.trading.momentum_neural.tape_cycles import (
    CYCLE_EXHAUSTION_FLOOR,
    CYCLE_EXHAUSTION_Q50,
    CYCLE_EXHAUSTION_Q90,
    CYCLE_EXHAUSTION_TERMS,
    CYCLE_LEDGER_MAX_CYCLES,
    CYCLE_PULLBACK_FRAC_BASE,
    PullbackCycleScanner,
    cycle_exhaustion_score,
    cycle_exhaustion_size_multiplier,
    cycle_features_at,
)

T0 = datetime(2026, 9, 10, 13, 30, 0)


def _tape(prices, *, sizes=None, buys=None, step_ms=250):
    """(observed_at, id, price, size, bid, ask). Ang bid/ask ay nakaposisyon para ang
    Lee-Ready ay magbigay ng BUY (px >= ask) o SELL (px <= bid) ayon sa `buys`."""
    rows = []
    for i, px in enumerate(prices):
        sz = 100.0 if sizes is None else float(sizes[i])
        is_buy = True if buys is None else bool(buys[i])
        bid = px - 0.01 if is_buy else px
        ask = px if is_buy else px + 0.01
        rows.append((T0 + timedelta(milliseconds=step_ms * i), i + 1, float(px), sz, bid, ask))
    return rows


# ── ANG BILANG NG CYCLE ──────────────────────────────────────────────────────
def test_no_cycle_without_a_new_high_after_the_pullback():
    """Isang spike at isang pullback na HINDI pa nababawi = 0 kumpletong cycle."""
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.10, 1.20, 1.30, 1.10]))  # 50% retrace ng 0.30 na amp, walang bagong high
    assert sc.n_cycles == 0
    assert sc.in_pullback is True
    assert sc.hod == pytest.approx(1.30)


def test_one_cycle_closes_on_the_print_above_the_prior_high():
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.30, 1.10, 1.31]))
    assert sc.n_cycles == 1
    c = sc.cycles[0]
    assert c["spike_low"] == pytest.approx(1.00)
    assert c["hi"] == pytest.approx(1.30)
    assert c["pb_low"] == pytest.approx(1.10)
    assert sc.in_pullback is False


def test_two_cycles_and_the_second_spike_starts_at_the_pullback_low():
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.30, 1.10, 1.31, 1.50, 1.28, 1.52]))
    assert sc.n_cycles == 2
    assert sc.cycles[1]["spike_low"] == pytest.approx(1.10)  # ang low ng UNANG pullback
    assert sc.cycles[1]["hi"] == pytest.approx(1.50)
    assert sc.cycles[1]["pb_low"] == pytest.approx(1.28)


def test_retrace_below_the_fraction_does_not_open_a_pullback():
    """0.30 na amp, 0.5 na praksyon ⇒ kailangan ng >= 0.15 na atras. Ang 0.10 ay hindi sapat,
    kaya ang bagong high ay HINDI nagsasara ng cycle: isang tuloy-tuloy na takbo lang ito."""
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.30, 1.20, 1.35]))
    assert sc.in_pullback is False
    assert sc.n_cycles == 0
    assert sc.hod == pytest.approx(1.35)


def test_a_full_retrace_below_the_spike_low_still_closes_a_cycle():
    """[53] sukat 3: 83% ng spike ay gumagawa PA RIN ng bagong high pagkatapos ng BUONG retrace.
    Kaya ang buong retrace (pb_low <= spike_low) ay BILANG na cycle, hindi pagtanggi."""
    # Cycle 0 muna (1.00→1.40, pb 1.10) para hindi na gumalaw ang onset; ang cycle 1 ay
    # umaatras sa 1.05 — mas MABABA pa sa sariling spike low na 1.10 — at nagsasara pa rin.
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.40, 1.10, 1.41, 1.80, 1.05, 1.81]))
    assert sc.n_cycles == 2
    c = sc.cycles[1]
    assert c["spike_low"] == pytest.approx(1.10)
    assert c["pb_low"] == pytest.approx(1.05)
    assert c["pb_low"] < c["spike_low"]
    assert c["pb_depth_ratio"] > 1.0  # mas malalim pa sa sariling amplitude


# ── ANG ONSET ────────────────────────────────────────────────────────────────
def test_onset_low_restarts_lower_before_the_first_cycle():
    """Ang tape ay nagsisimula sa gitna ng galaw ([59]: median na unang tick 13:57Z), kaya
    habang WALANG kumpletong cycle ay sinusundan pababa ng onset ang bawat mas mababang low."""
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.20, 1.10, 1.00, 0.90]))
    assert sc.onset_low == pytest.approx(0.90)
    assert sc.n_cycles == 0


def test_onset_low_is_frozen_after_the_first_cycle():
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.30, 1.10, 1.31]))
    assert sc.n_cycles == 1
    onset = sc.onset_low
    sc.feed(_tape([0.50, 0.40], step_ms=1000))  # mas mababa pa sa onset — hindi na ito gumagalaw
    assert sc.onset_low == pytest.approx(onset)


# ── PERSISTENCE AT PARITY ────────────────────────────────────────────────────
def test_to_dict_from_dict_round_trip_is_exact():
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape([1.00, 1.30, 1.10, 1.31, 1.50, 1.28, 1.52, 1.45]))
    import json

    d = json.loads(json.dumps(sc.to_dict()))  # dapat JSON-safe: nakatira ito sa live_exec
    sc2 = PullbackCycleScanner.from_dict(d)
    assert sc2.to_dict() == sc.to_dict()
    assert cycle_features_at(sc2, 1.45) == cycle_features_at(sc, 1.45)


def test_incremental_feed_equals_batch_feed():
    """Ang runner ay nagpapakain ng tipak kada tick; ang sukat ay nagbabasa ng buong araw.
    Dapat IISA ang estado — kung hindi, ang live at ang replay ay dalawang makina."""
    rows = _tape([1.00, 1.30, 1.10, 1.31, 1.50, 1.28, 1.52, 1.45, 1.60, 1.40, 1.61])
    batch = PullbackCycleScanner(0.5)
    batch.feed(rows)
    inc = PullbackCycleScanner(0.5)
    for r in rows:
        inc.feed([r])
    assert inc.to_dict() == batch.to_dict()


def test_persisted_state_survives_a_round_trip_mid_feed():
    rows = _tape([1.00, 1.30, 1.10, 1.31, 1.50, 1.28, 1.52])
    whole = PullbackCycleScanner(0.5)
    whole.feed(rows)
    split = PullbackCycleScanner(0.5)
    split.feed(rows[:4])
    split = PullbackCycleScanner.from_dict(split.to_dict())
    split.feed(rows[4:])
    assert split.to_dict() == whole.to_dict()


def test_feed_never_raises_on_broken_rows():
    sc = PullbackCycleScanner(0.5)
    taken = sc.feed(
        [
            (T0, 1, None, 100.0, 1.0, 1.01),          # walang presyo
            (T0, 2, "abc", 100.0, 1.0, 1.01),          # basag na presyo
            (T0, 3, 0.0, 100.0, 1.0, 1.01),            # presyong zero
            (None, 4, 1.00, 100.0, 1.0, 1.01),         # walang stamp
            (T0, 5, 1.00, 100.0, 1.0, 1.01),           # tama
        ]
    )
    assert taken == 1
    assert sc.n_prints == 1


def test_ledger_is_bounded_but_the_index_is_not():
    sc = PullbackCycleScanner(0.5, max_cycles=3)
    px = [1.00]
    lo = 1.00
    for _ in range(6):  # 6 na cycle; ang bagong spike low = ang low ng nakaraang pullback
        hi = lo + 0.30
        px += [hi, hi - 0.20, hi + 0.01]
        lo = hi - 0.20
    sc.feed(_tape(px))
    assert sc.n_cycles >= 5
    assert len(sc.cycles) <= 3  # ang JSON ay may hangganan
    assert sc.cycles[-1]["k"] == sc.n_cycles - 1  # ang HULI ang itinatago


# ── ANG FEATURES SA DESISYONG SANDALI ────────────────────────────────────────
def test_features_on_a_three_spike_tape_with_shrinking_amplitude_and_fading_buy_share():
    """Tatlong spike: 1.00→1.60 (amp 0.60), 1.40→1.70 (0.30), 1.55→1.60 (0.05 sa ngayon);
    at bumabagsak ang buy share sa huling spike. Iyon mismo ang hugis ng pagod."""
    px = (
        [1.00, 1.30, 1.60, 1.25, 1.61]          # cycle 0: spike 1.00→1.60 (amp 0.60), pb 1.25
        + [1.70, 1.45, 1.71]                     # cycle 1: spike 1.25→1.70 (amp 0.45), pb 1.45
        + [1.60, 1.58]                           # kasalukuyang spike: 1.45→1.71 (amp 0.26)
    )
    buys = [True] * 6 + [False] * 4              # nagfa-fade ang aggressor-buy sa dulo
    sc = PullbackCycleScanner(0.5)
    sc.feed(_tape(px, buys=buys))
    assert sc.n_cycles == 2
    f = cycle_features_at(sc, 1.58)
    assert f["cycle_index"] == 2
    assert f["amp_ratio"] is not None and f["amp_ratio"] < 1.0      # lumiliit ang spike
    assert f["buy_share_delta"] is not None and f["buy_share_delta"] < 0.0  # humihina ang bid
    assert 0.0 <= f["pos_in_range"] <= 1.0
    assert f["ext_x_amp0"] is not None and f["ext_x_amp0"] > 0.0
    assert f["prints_since_high"] >= 1
    assert f["last_pb_depth_ratio"] is not None


def test_features_are_empty_before_any_print():
    assert cycle_features_at(PullbackCycleScanner(0.5), 1.0) == {}
    assert cycle_features_at(None, 1.0) == {}


# ── ANG SCORE AT ANG MULTIPLIER ──────────────────────────────────────────────
def test_measured_constants_are_the_reported_bindings():
    assert CYCLE_PULLBACK_FRAC_BASE == 0.50
    assert CYCLE_EXHAUSTION_Q50 == 0.5400
    assert CYCLE_EXHAUSTION_Q90 == 0.7109
    assert CYCLE_EXHAUSTION_FLOOR == 0.3125
    assert CYCLE_LEDGER_MAX_CYCLES == 16
    # Ang bawat termino ay may sinukat na clustered AUC sa LABAS ng [0.40, 0.60].
    assert set(CYCLE_EXHAUSTION_TERMS) == {
        "pos_in_range",
        "ext_x_amp0",
        "last_pb_depth_ratio",
        "cur_buy_share",
    }
    for name, (sign, q_lo, q_hi, auc) in CYCLE_EXHAUSTION_TERMS.items():
        assert sign in (1.0, -1.0), name
        assert q_hi > q_lo, name
        assert auc <= 0.40 or auc >= 0.60, name
    # Ang cycle_index mismo ay HINDI termino ng score (monotone-in-time sa loob ng araw).
    assert "cycle_index" not in CYCLE_EXHAUSTION_TERMS


def test_score_is_none_without_readable_terms():
    s, d = cycle_exhaustion_score({}, CYCLE_EXHAUSTION_TERMS)
    assert s is None
    assert d["reason"] in ("no_features", "no_readable_terms")
    s, d = cycle_exhaustion_score(None, CYCLE_EXHAUSTION_TERMS)
    assert s is None


def test_score_rises_as_the_tape_gets_more_exhausted():
    fresh = {"pos_in_range": 0.99, "ext_x_amp0": 1.0, "last_pb_depth_ratio": 2.00, "cur_buy_share": 0.60}
    tired = {"pos_in_range": 0.40, "ext_x_amp0": 90.0, "last_pb_depth_ratio": 0.50, "cur_buy_share": 0.40}
    s_fresh, _ = cycle_exhaustion_score(fresh, CYCLE_EXHAUSTION_TERMS)
    s_tired, _ = cycle_exhaustion_score(tired, CYCLE_EXHAUSTION_TERMS)
    assert s_fresh == pytest.approx(0.0, abs=1e-9)
    assert s_tired == pytest.approx(1.0, abs=1e-9)


def test_score_uses_only_the_terms_it_can_read():
    s, d = cycle_exhaustion_score({"pos_in_range": 0.40}, CYCLE_EXHAUSTION_TERMS)
    assert d["n_terms"] == 1
    assert s == pytest.approx(1.0)


def test_multiplier_is_monotone_non_increasing_and_never_sizes_up():
    prev = 1.0
    for s in [i / 50.0 for i in range(51)]:
        m, _ = cycle_exhaustion_size_multiplier(
            s, floor=CYCLE_EXHAUSTION_FLOOR, q50=CYCLE_EXHAUSTION_Q50, q90=CYCLE_EXHAUSTION_Q90
        )
        assert 0.0 < m <= 1.0
        assert m >= CYCLE_EXHAUSTION_FLOOR - 1e-12
        assert m <= prev + 1e-12
        prev = m


def test_multiplier_anchors_at_the_measured_quantiles():
    m50, _ = cycle_exhaustion_size_multiplier(
        CYCLE_EXHAUSTION_Q50, floor=CYCLE_EXHAUSTION_FLOOR, q50=CYCLE_EXHAUSTION_Q50, q90=CYCLE_EXHAUSTION_Q90
    )
    m90, _ = cycle_exhaustion_size_multiplier(
        CYCLE_EXHAUSTION_Q90, floor=CYCLE_EXHAUSTION_FLOOR, q50=CYCLE_EXHAUSTION_Q50, q90=CYCLE_EXHAUSTION_Q90
    )
    m_top, _ = cycle_exhaustion_size_multiplier(
        1.0, floor=CYCLE_EXHAUSTION_FLOOR, q50=CYCLE_EXHAUSTION_Q50, q90=CYCLE_EXHAUSTION_Q90
    )
    assert m50 == pytest.approx(1.0)
    assert m90 == pytest.approx(CYCLE_EXHAUSTION_FLOOR)
    assert m_top == pytest.approx(CYCLE_EXHAUSTION_FLOOR)  # HINDI KAILANMAN veto


def test_multiplier_fails_open_on_missing_inputs():
    assert cycle_exhaustion_size_multiplier(None, floor=0.3125, q50=0.54, q90=0.71)[0] == 1.0
    assert cycle_exhaustion_size_multiplier(0.9, floor=None, q50=0.54, q90=0.71)[0] == 1.0
    assert cycle_exhaustion_size_multiplier(0.9, floor=0.3125, q50=0.71, q90=0.54)[0] == 1.0


# ── ANG ESTADO AY SYMBOL-DAY, HINDI PER-TRADE ────────────────────────────────
def test_tape_cycle_state_is_not_cleared_on_recycle():
    """Ang recycle ay nagsisimula ng bagong TRADE, hindi ng bagong ARAW ng tape. Kung mabubura
    ang ledger kada cycle, ang bawat re-entry ay magmumukhang unang pasok ng araw — at iyon
    mismo ang butas na sinusukat ng [62]."""
    from app.services.trading.momentum_neural.live_runner import _RECYCLE_ENTRY_STATE_KEYS

    assert "tape_cycle_state" not in _RECYCLE_ENTRY_STATE_KEYS
