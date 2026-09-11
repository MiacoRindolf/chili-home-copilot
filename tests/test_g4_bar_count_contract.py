"""[23] WALANG ORASAN SA LOOB NG BAR — ang G4 tape read ay ``count_v1``.

Pinanatili ng [29] ang re-entry ramp sa ``feature_contract="legacy_time_split"`` nang
sadya: ang 255 print ay pinipili sa BILANG, pero ang ``signed_tape_accel`` ay hinahati sa
GITNA NG ORAS at ang discontinuity trim ay ``window_s/2`` = 7.5 s — isang orasan sa loob
ng bar — habang ang ``buy_share_delta`` ay count-split na. Ang dalawang kalahati ng iisang
bar ay hinahati sa dalawang magkaibang axis.

SINUKAT (read-only, bounded, 2026-09-11):
  * hindi magkasundo kung tape+ sa 465/2,175 = 21.4% ng G4 instant (1 araw, 9 cluster);
    walang tape ang legacy sa 67/2,244 (3.0% — ang mabagal na pangalan, pinutol ng 7.5-s
    trim) laban sa 2/2,244 sa count_v1;
  * walang edge ang alinman sa 8 araw (first touch +/-2% sa 15 min, <= 4 kada
    symbol-15min): TT 40/84 = 0.476, FF 60/133 = 0.451, count-lang-tape+ 32/58 = 0.552,
    legacy-lang-tape+ 10/16 = 0.625; admitted/refused count_v1 0.507/0.470, legacy
    0.500/0.482 ⇒ ang DOKTRINA ang nagpapasya (print-indexed, walang segundo);
  * ang [46] chase gate (kumakain ng PAREHONG tape) ay muling sinukat sa 177-hilerang
    populasyon nito sa dalawang kontrata — ang legacy ay nag-reproduce ng bawat [46] numero
    (47/177, 31/177, 32 & 24, 0.8667/0.8824/1.0000, unang-admit na ext −0.62..4.32);
  * ang [7] door 1 (0.81, walang reference) ay BUMABASA ng tanda ng tape (pasa sa tape+
    lamang) — kaya muling sinukat: class-A tape+ sa count_v1 26/59 = 0.441 ⇒ 0.441/0.548 =
    0.804 (legacy 24/51 = 0.471 ⇒ 0.859); ang 0.81 ay nananatili.

Runnable: pytest tests/test_g4_bar_count_contract.py -v
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural import optional_db_read as ODR
from app.services.trading.momentum_neural import risk_policy as RP
from app.services.trading.momentum_neural.risk_policy import reentry_chase_decision

from tests.test_reentry_bar_level0_prior_leg_high import GREEN_PRIOR, _FakeDB, _harness

G4_SOURCE = inspect.getsource(LR._g4_reentry_escalation_check)
#: The REAL tape function, captured before any test monkeypatches the module attribute.
_ORIGINAL_TAPE_FN = EG.signed_tape_accel_features


# ── the wiring ───────────────────────────────────────────────────────────────


def _tape_calls() -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(G4_SOURCE))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_g4e_tape_fn"
    ]


def test_the_g4_read_passes_count_v1():
    """AST PIN: the ONE tape read of the bar names ``count_v1`` as a literal keyword."""
    calls = _tape_calls()
    assert len(calls) == 1, len(calls)
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw["feature_contract"], ast.Constant)
    assert kw["feature_contract"].value == "count_v1"
    assert isinstance(kw["window_prints"], ast.Name) and kw["window_prints"].id == "_g4e_window_prints"
    assert "legacy_time_split" not in ast.unparse(calls[0])


def test_window_prints_is_unchanged():
    assert int(settings.chili_momentum_g4_reentry_tape_window_prints) == 255


# ── the synthetic tape where the two splits disagree ─────────────────────────

_NOW = datetime(2026, 9, 10, 13, 55, 34)
_NOW_EPOCH = (_NOW - datetime(1970, 1, 1)).total_seconds()


def _disagreeing_rows() -> list[tuple]:
    """20 prints over 9 s. Prints 0-9 (t 0.0-0.9 s) are SELLS at the bid, prints 10-15
    (t 1.0-1.5 s) BUYS at the ask, prints 16-19 (t 6, 7, 8, 9 s) SELLS.

    TIME midpoint = 4.5 s ⇒ front = prints 0-15 (buy 600), back = 16-19 (buy 0)
    ⇒ accel −600. COUNT midpoint = print 10 ⇒ front = 0-9 (buy 0), back = 10-19
    (buy 600) ⇒ accel +600. ``buy_share_delta`` is count-split in BOTH (+0.6).
    No gap exceeds either trim (legacy 7.5 s; count p90 1.0 s x 7.82)."""
    t0 = _NOW_EPOCH - 10.0
    bid, ask = 0.99, 1.01
    rows = []
    for i in range(10):
        rows.append((bid, 100, bid, ask, t0 + 0.1 * i))
    for i in range(10, 16):
        rows.append((ask, 100, bid, ask, t0 + 0.1 * i))
    for t in (6.0, 7.0, 8.0, 9.0):
        rows.append((bid, 100, bid, ask, t0 + t))
    return rows


def _fake_fetch(monkeypatch):
    monkeypatch.setattr(ODR, "optional_fetchall",
                        lambda db, stmt, params=None, **kw: db.execute(stmt, params).fetchall())


def _read(rows, contract, monkeypatch):
    _fake_fetch(monkeypatch)
    return _ORIGINAL_TAPE_FN("SYNC", db=_FakeDB(rows), window_prints=255,
                             as_of=_NOW, feature_contract=contract)


def test_the_two_contracts_disagree_on_the_same_prints(monkeypatch):
    rows = _disagreeing_rows()
    legacy = _read(rows, "legacy_time_split", monkeypatch)
    count = _read(rows, "count_v1", monkeypatch)
    assert legacy is not None and count is not None
    assert legacy["split"] == "time" and count["split"] == "count"
    assert legacy["signed_tape_accel"] == pytest.approx(-600.0)
    assert count["signed_tape_accel"] == pytest.approx(600.0)
    # the share delta was already count-split under both
    assert legacy["buy_share_delta"] == pytest.approx(0.6)
    assert count["buy_share_delta"] == pytest.approx(0.6)
    assert legacy["last_print"] == count["last_print"] == pytest.approx(0.99)


def test_the_bar_resolves_by_count(monkeypatch):
    """The SHIPPED helper, reading the SAME prints through the REAL tape function: the
    count split says buyers are lifting, and the bar passes; the time split on the same
    prints would have refused (``tape_not_confirming``)."""
    rows = _disagreeing_rows()
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=None,
                                      high_print=0.98)
    _fake_fetch(monkeypatch)
    calls: list[dict] = []

    def _spy(symbol, **kw):
        calls.append(dict(kw))
        kw = dict(kw)
        kw["db"] = _FakeDB(rows)
        return _ORIGINAL_TAPE_FN(symbol, **kw)

    monkeypatch.setattr(EG, "signed_tape_accel_features", _spy)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.01)
    assert len(calls) == 1
    assert calls[0]["feature_contract"] == "count_v1"
    assert calls[0]["window_prints"] == 255
    assert (ok, lvl) == (True, 0), dbg
    assert dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_feature_contract"] == "count_v1"
    assert dbg["binding"]["tape_split"] == "count"
    assert dbg["signed_tape_accel"] == pytest.approx(600.0)
    # the legacy read of the same prints, through the same pure decision, refuses
    legacy = _ORIGINAL_TAPE_FN("SYNC", db=_FakeDB(rows), window_prints=255, as_of=_NOW,
                               feature_contract="legacy_time_split")
    ok_l, dbg_l = RP.reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False,
        live_price=legacy["last_print"], prior_hwm=GREEN_PRIOR["high_water_mark"],
        prior_exit_price=GREEN_PRIOR["exit_price"], prior_risk_dist=GREEN_PRIOR["risk_dist"],
        tape_accel=legacy["signed_tape_accel"], prior_high_print=0.98,
        tape_buy_share_delta=legacy["buy_share_delta"], tape_stale=False,
    )
    assert ok_l is False and dbg_l["reason"] == "tape_not_confirming"


def test_the_receipt_names_the_contract_even_on_a_refusal(monkeypatch):
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR,
                                tape={"signed_tape_accel": -5.0, "buy_share_delta": -0.1,
                                      "back_buy_share": 0.4, "n_ticks": 255,
                                      "last_print": 3.81, "last_bid": 3.80, "last_ask": 3.82,
                                      "feature_contract": "count_v1", "split": "count"},
                                high_print=3.80)
    ok, dbg, _ = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert ok is False and dbg["reason"] == "tape_not_confirming"
    assert dbg["tape_feature_contract"] == "count_v1"
    assert dbg["binding"]["tape_split"] == "count"


# ── [46] re-derived under count_v1 (the chase gate eats the same tape) ────────

CAP_R = 1.5

#: 2026-09-11, both contracts re-read side by side on the [46] 177-row population
#: (momentum_reentry_chase_blocked 2026-08-30..09-10). The legacy re-read reproduces
#: every [46] number; the count_v1 numbers are what ships after [23].
REDERIVED_46 = {
    "rows": 177, "episodes": 11,
    "tape_plus": {"legacy_time_split": 47, "count_v1": 66},
    "disagree": 31, "legacy_minus_count_plus": 25, "legacy_plus_count_minus": 6,
    "inside_band_print_basis": 32,
    "inside_band_tape_minus": {"legacy_time_split": 24, "count_v1": 23},
    "episodes_admitted": {"legacy_time_split": 6, "count_v1": 8},
    "first_touch_up": {"legacy_time_split": 3, "count_v1": 3},
    "first_admit_ext": {
        "legacy_time_split": [-0.62, 1.09, 1.51, 2.0, 2.56, 4.32],
        "count_v1": [-1.85, -0.62, 1.1, 1.51, 2.11, 2.21, 2.56, 4.32],
    },
    "tercile": {
        "legacy_time_split": ((13, 15), (15, 17), (15, 15)),
        "count_v1": ((19, 22), (20, 22), (21, 22)),
    },
    "withdrawn_q50": 6.19,
}

#: (symbol, ts UTC, print, anchor=high print, risk, quote, quote anchor, quote risk,
#:  count_v1 accel, count_v1 bsd, expected count_v1 decision). Read 2026-09-11.
COUNT_V1_ROWS = [
    ("TNON", "13:37:36", 4.60, 4.32, 0.0648, 4.54, 4.28, 0.0642, 533.0, 0.2724795640326976, True),
    ("PCLA", "14:01:53", 9.43, 9.15, 0.13725, 9.48, 9.15, 0.13725, -165.0, -0.05359276157126447, False),
    ("PCLA", "14:02:44", 9.44, 9.15, 0.13725, 9.40, 9.15, 0.13725, 1413.0, 0.34699125666709574, True),
    ("DLTH", "10:44:40", 4.37, 4.28, 0.0642, 4.40, 4.28, 0.0642, -3623.0, -0.09959362461201882, False),
    ("WYHG", "08:59:49", 6.0247, 5.94, 0.0891, 6.04, 5.88, 0.0882, -4162.0, -0.47967340180318546, False),
    ("SLE", "15:39:46", 5.06, 4.978, 0.07467, 5.09, 4.94, 0.0741, 7077.0, 0.2084677173235847, True),
    ("TPET", "13:44:41", 2.10, 2.16, 0.0324, 2.21, 2.15, 0.03225, 6687.0, 0.3012470793316471, True),
    ("MIMI", "14:24:19", 1.0304, 1.04, 0.0156, 1.10, 1.04, 0.0156, 35954.0, 0.6559290803029473, True),
]


@pytest.mark.parametrize("row", COUNT_V1_ROWS, ids=[f"{r[0]}-{r[1]}" for r in COUNT_V1_ROWS])
def test_the_chase_gate_on_the_count_v1_tape(row):
    sym, ts, px, anc, ru, qpx, qanc, qru, accel, bsd, admit_expected = row
    admit, dbg = reentry_chase_decision(
        live_price=px, anchor=anc, risk_unit=ru, cap_r=CAP_R,
        quote_price=qpx, quote_anchor=qanc, quote_risk_unit=qru,
        tape_accel=accel, tape_buy_share_delta=bsd, tape_stale=False,
        atr_pct_source="fallback_0.015", risk_unit_source="regime_atr_pct:fallback_0.015",
        tape_feature_contract="count_v1",
    )
    assert dbg["above_band"] is True          # all 177 were above the union band
    assert admit is admit_expected, (sym, ts, dbg["reason"])
    assert dbg["reason"] == ("reentry_chase_tape_admit" if admit_expected else "reentry_chase_tape_wait")
    assert dbg["tape_feature_contract"] == "count_v1"


def test_the_episode_split_under_count_v1():
    """LIDR / DLTH / WYHG are still refused (zero tape+ instants under either contract);
    SLE and TPET join the admitted set; the admitted set goes UP first 3/8 vs 3/6 under
    legacy — eleven clusters do not separate the contracts."""
    r = REDERIVED_46
    assert r["episodes_admitted"]["count_v1"] - r["episodes_admitted"]["legacy_time_split"] == 2
    assert r["first_touch_up"]["count_v1"] == r["first_touch_up"]["legacy_time_split"] == 3
    assert r["tape_plus"]["count_v1"] - r["tape_plus"]["legacy_time_split"] == (
        r["legacy_minus_count_plus"] - r["legacy_plus_count_minus"])
    assert r["disagree"] == r["legacy_minus_count_plus"] + r["legacy_plus_count_minus"]


def test_there_is_still_no_size_band_under_count_v1():
    """R3 under the contract that now ships: the first admitted instants all sit below
    the withdrawn q50, and continuation RISES with extension."""
    r = REDERIVED_46
    for c in ("legacy_time_split", "count_v1"):
        assert max(r["first_admit_ext"][c]) < r["withdrawn_q50"], c
        (lo_h, lo_n), _mid, (hi_h, hi_n) = r["tercile"][c]
        assert (hi_h / hi_n) / (lo_h / lo_n) > 1.0, c
    (lo_h, lo_n), _mid, (hi_h, hi_n) = r["tercile"]["count_v1"]
    assert round((hi_h / hi_n) / (lo_h / lo_n), 4) == 1.1053
    for dead in ("REENTRY_CHASE_EXT_Q50", "REENTRY_CHASE_EXT_Q90", "REENTRY_CHASE_SIZE_FLOOR"):
        assert not hasattr(RP, dead), dead


def test_the_chase_derivation_names_count_v1_and_its_numbers():
    ref = RP._REENTRY_CHASE_DERIVATIONS_REF
    assert "feature_contract=count_v1" in ref
    assert "tape+ 66/177" in ref
    assert "0.8636/0.9091/0.9545" in ref and "1.1053" in ref
    assert "legacy_time_split reproduced 47/177" in ref
    assert "measured under legacy_time_split" not in ref


# ── [7] door 1 reads the sign of the tape: re-measured under both contracts ───

#: class-A (no reference, level 1) instants since 2026-09-08, one sample per
#: (symbol, 15-min bucket, class), forward 15-min MFE >= 2% (the [7] metric).
DOOR1_REMEASURE = {
    "count_v1": {"tape_plus": (26, 59), "tape_minus": (33, 71)},
    "legacy_time_split": {"tape_plus": (24, 51), "tape_minus": (37, 80)},
    "reference": 0.548,
}


def test_door1_multiplier_survives_the_contract_switch():
    h, n = DOOR1_REMEASURE["count_v1"]["tape_plus"]
    ratio = (h / n) / DOOR1_REMEASURE["reference"]
    assert round(ratio, 3) == 0.804
    shipped = float(settings.chili_momentum_g4_substitute_no_reference_size_mult)
    assert shipped == 0.81
    assert abs(ratio - shipped) < 0.01
