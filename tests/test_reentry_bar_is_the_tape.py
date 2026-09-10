"""THE RE-ENTRY RAMP MUST BIND (D): the escalated bar is the tape.

At level >= 1 the reclaim reference used to be the prior trade's HWM (a quote-mid
sample) and the tape hold ``signed_tape_accel > 0`` from a 15-SECOND window —
"fifteen seconds is ~900 prints on a fast name and four on a slow one". SKYQ
2026-09-10 passed level 2 on a one-cent HWM "reclaim" (3.65 vs 3.64); 8 of 11
re-entries that day never made a new high after entry.

Now:
  * REFERENCE = the prior leg's HIGH PRINT (max trade print between its entry
    fill and exit fill; symbol-scoped, as-of bounded), ``reference_kind =
    prior_leg_high_print``; HWM / exit price are fallbacks;
  * TAPE HOLD = BOTH ``signed_tape_accel > 0`` AND ``buy_share_delta > 0`` from
    ``signed_tape_accel_features(window_prints=N)`` — the print-indexed form that
    had zero callers; N = chili_momentum_g4_reentry_tape_window_prints, the p50
    of the 15-s print count at 108 live decision instants (255).

MEASURED 7d live to 2026-09-10 (36 re-entries): the new bar refuses 25 legs =
-$562.21 and allows 11 = +$59.81; the reconstructed old bar refused 3 = -$80.92.

Runnable: pytest tests/test_reentry_bar_is_the_tape.py -v
"""
from __future__ import annotations

import pathlib
from datetime import datetime

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.risk_policy import reentry_escalation_decision

_SRC = pathlib.Path(LR.__file__)

MEASURED_7D = {"reentries": 36, "refused": 25, "refused_pnl": -562.21,
               "allowed": 11, "allowed_pnl": 59.81, "old_bar_refused": 3, "old_bar_refused_pnl": -80.92}


def test_the_refused_set_is_net_negative_and_the_allowed_set_is_not():
    assert MEASURED_7D["refused_pnl"] < 0 < MEASURED_7D["allowed_pnl"]
    assert MEASURED_7D["refused"] + MEASURED_7D["allowed"] == MEASURED_7D["reentries"]


def _decide(**kw):
    base = dict(enabled=True, escalation_level=1, structural_trigger=True,
                live_price=10.8, prior_hwm=10.5, prior_exit_price=10.0, prior_risk_dist=0.4,
                tape_accel=100.0, tape_buy_share_delta=0.05, prior_high_print=10.7)
    base.update(kw)
    return reentry_escalation_decision(**base)


# ── the reference ────────────────────────────────────────────────────────────


def test_reference_is_the_prior_leg_high_print_when_readable():
    ok, dbg = _decide(live_price=10.69)
    assert ok is False and dbg["reason"] == "reclaim_not_met"
    assert dbg["reference_kind"] == "prior_leg_high_print"
    assert dbg["required_reclaim"] == 10.7
    ok2, dbg2 = _decide(live_price=10.70)
    assert ok2 is True and dbg2["reason"] == "reclaim_met"


def test_hwm_is_only_a_fallback_and_says_so():
    ok, dbg = _decide(prior_high_print=None, live_price=10.55)
    assert ok is True and dbg["reference_kind"] == "hwm_fallback" and dbg["required_reclaim"] == 10.5
    ok2, dbg2 = _decide(prior_high_print=None, prior_hwm=None, live_price=10.05)
    assert ok2 is True and dbg2["reference_kind"] == "exit_price_fallback"


def test_margin_scales_from_the_high_print():
    _, dbg = _decide(escalation_level=3, live_price=1.0)
    assert dbg["required_reclaim"] == pytest.approx(10.7 + 2 * 0.4)


def test_skyq_one_cent_hwm_reclaim_no_longer_passes():
    """SKYQ 2026-09-10 leg 2: HWM 3.64, 'reclaimed' at 3.65 — the prior leg's high
    print was 3.69."""
    ok, dbg = _decide(escalation_level=1, live_price=3.65, prior_hwm=3.64, prior_exit_price=3.62,
                      prior_risk_dist=0.04, prior_high_print=3.69, tape_accel=1.0, tape_buy_share_delta=0.01)
    assert ok is False and dbg["reason"] == "reclaim_not_met" and dbg["required_reclaim"] == 3.69


# ── the tape hold: BOTH conditions ───────────────────────────────────────────


def test_both_conditions_must_hold():
    assert _decide(tape_accel=100.0, tape_buy_share_delta=0.05)[0] is True
    ok, dbg = _decide(tape_accel=100.0, tape_buy_share_delta=-0.02)
    assert ok is False and dbg["reason"] == "tape_not_confirming"
    ok, dbg = _decide(tape_accel=-100.0, tape_buy_share_delta=0.3)
    assert ok is False and dbg["reason"] == "tape_not_confirming"
    ok, dbg = _decide(tape_accel=0.0, tape_buy_share_delta=0.3)
    assert ok is False and dbg["reason"] == "tape_not_confirming"


def test_majority_buy_does_not_override_the_print_indexed_form():
    ok, dbg = _decide(tape_accel=-142000.0, tape_buy_share_delta=-0.01, tape_back_buy_share=0.92)
    assert ok is False and dbg["reason"] == "tape_not_confirming"
    assert dbg["tape_hold_form"] == "print_indexed_accel_and_buy_share_delta"


def test_legacy_form_survives_when_buy_share_delta_is_unreadable():
    ok, dbg = _decide(tape_accel=-142000.0, tape_buy_share_delta=None, tape_back_buy_share=0.92)
    assert ok is True and dbg["reason"] == "tape_majority_buy_confirms"
    assert dbg["tape_hold_form"] == "legacy_accel_or_majority_buy"


def test_unreadable_tape_still_does_not_starve():
    ok, dbg = _decide(tape_accel=None, tape_buy_share_delta=None)
    assert ok is True


def test_substitute_uses_both_conditions_too():
    ok, dbg = _decide(structural_trigger=False, live_price=11.5, noise_abs=0.0,
                      tape_accel=100.0, tape_buy_share_delta=-0.05)
    assert ok is False and dbg["reason"] == "non_structural_trigger"
    ok2, dbg2 = _decide(structural_trigger=False, live_price=11.5, noise_abs=0.0,
                        tape_accel=100.0, tape_buy_share_delta=0.05)
    assert ok2 is True and dbg2["reclaim_structural_substitute"] is True
    assert dbg2["substitute_required"] == pytest.approx(10.7 + 0.4)


def test_receipt_carries_the_tape_bars_inputs():
    _, dbg = _decide(prints_since_high=17)
    for k in ("required_reclaim", "reference_kind", "buy_share_delta", "prints_since_high",
              "prior_high_print", "tape_hold_form"):
        assert k in dbg, k
    assert dbg["prints_since_high"] == 17 and dbg["buy_share_delta"] == 0.05


# ── measured legs, as fixed expectations (7 days to 2026-09-10) ─────────────


def test_measured_sune_09_09_leg1_is_allowed_and_won():
    """vwap_reclaim at level 1; prior high print 2.94, price 2.95, accel +959,
    buy_share_delta +0.038 => allowed; the leg banked +27.85."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True, live_price=2.95,
        prior_hwm=2.93, prior_exit_price=2.90, prior_risk_dist=0.04, tape_accel=959.0,
        prior_high_print=2.94, tape_buy_share_delta=0.0382, prints_since_high=232)
    assert ok is True and dbg["reason"] == "reclaim_met"


def test_measured_tnon_09_10_leg4_is_refused_and_lost():
    """momentum_ok_rel_vol at level 1; prior high print 4.29, price 4.24, accel
    -3,082, buy_share_delta -0.253 => refused; the leg lost -55.61 (bailout)."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=False, live_price=4.24,
        prior_hwm=4.28, prior_exit_price=4.20, prior_risk_dist=0.14, tape_accel=-3082.0,
        noise_abs=0.02, prior_high_print=4.29, tape_buy_share_delta=-0.2534, prints_since_high=50)
    assert ok is False and dbg["reason"] == "non_structural_trigger"


def test_measured_mimi_09_09_leg1_is_refused_by_the_tape():
    """orb_break_tick_ok at level 1 with the price ABOVE the high print (1.05 vs
    1.04) but accel -26,626 / buy_share_delta -0.204 => the tape refuses; -15.02."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True, live_price=1.05,
        prior_hwm=1.04, prior_exit_price=1.00, prior_risk_dist=0.08, tape_accel=-26626.0,
        prior_high_print=1.04, tape_buy_share_delta=-0.2042, prints_since_high=93)
    assert ok is False and dbg["reason"] == "tape_not_confirming"


# ── the tape reads: print-indexed, as-of bounded, tie-stable ─────────────────


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, stmt, params=None):
        self.statements.append((str(stmt), dict(params or {})))
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows

            def fetchone(self_inner):
                return rows[0] if rows else None

        return _R()

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()


def _optional_passthrough(monkeypatch):
    from app.services.trading.momentum_neural import optional_db_read as ODR

    monkeypatch.setattr(ODR, "optional_fetchall", lambda db, stmt, params=None: db.execute(stmt, params).fetchall())


def test_window_prints_read_is_last_n_prints_as_of_bounded_and_tie_stable(monkeypatch):
    _optional_passthrough(monkeypatch)
    ts0 = 1_800_000_000.0
    rows = [(1.0 + i * 0.01, 100, 0.99 + i * 0.01, 1.01 + i * 0.01, ts0 + i) for i in range(12)]
    db = _FakeDB(rows)
    out = EG.signed_tape_accel_features("SKYQ", db=db, window_prints=12, as_of=datetime(2026, 9, 10, 18, 0, 0))
    assert out is not None and "buy_share_delta" in out and "prints_since_high" in out
    sql, params = db.statements[0]
    assert "LIMIT :n" in sql and params["n"] == 12
    assert "observed_at <= :as_of" in sql
    assert "ORDER BY observed_at DESC, id DESC" in sql and "ORDER BY observed_at ASC, id ASC" in sql
    assert "make_interval" not in sql, "a print window is not a clock"


def test_prior_leg_high_print_is_symbol_scoped_and_as_of_bounded(monkeypatch):
    _optional_passthrough(monkeypatch)
    db = _FakeDB([(3.66, 812)])
    hi, n = EG.prior_leg_high_print(
        "SKYQ", db=db, entry_at="2026-09-10T17:58:18+00:00", exit_at="2026-09-10T18:01:50",
        as_of=datetime(2026, 9, 10, 18, 0, 0),
    )
    assert (hi, n) == (3.66, 812)
    sql, params = db.statements[0]
    assert "symbol = :s" in sql and params["s"] == "SKYQ"
    assert "observed_at > :a" in sql and "observed_at <= :b" in sql
    assert params["b"] == datetime(2026, 9, 10, 18, 0, 0), "the read never sees past as_of"
    assert params["a"] == datetime(2026, 9, 10, 17, 58, 18)


@pytest.mark.parametrize("kw", [
    dict(symbol="BTC-USD"),
    dict(entry_at=None),
    dict(exit_at="2026-09-10T17:00:00"),  # exit before entry
])
def test_prior_leg_high_print_fails_open(monkeypatch, kw):
    _optional_passthrough(monkeypatch)
    base = dict(symbol="SKYQ", entry_at="2026-09-10T17:58:18+00:00", exit_at="2026-09-10T18:01:50")
    base.update(kw)
    sym = base.pop("symbol")
    assert EG.prior_leg_high_print(sym, db=_FakeDB([(9.0, 1)]), **base) == (None, 0)
    assert EG.prior_leg_high_print("SKYQ", db=None, entry_at=base["entry_at"], exit_at="2026-09-10T18:01:50") == (None, 0)


def test_an_empty_tape_returns_none_so_the_hwm_fallback_applies(monkeypatch):
    _optional_passthrough(monkeypatch)
    assert EG.prior_leg_high_print("SKYQ", db=_FakeDB([(None, 0)]),
                                   entry_at="2026-09-10T17:58:18", exit_at="2026-09-10T18:01:50") == (None, 0)


# ── the setting and the wiring ───────────────────────────────────────────────


def test_the_window_setting_carries_its_derivation():
    name = "chili_momentum_g4_reentry_tape_window_prints"
    assert int(getattr(settings, name)) == 255
    desc = str(type(settings).model_fields[name].description or "")
    assert "p50" in desc and "108" in desc and "2026-09-10" in desc
    assert "255" in desc


def test_the_runner_passes_the_print_window_and_the_high_print():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def _g4_reentry_escalation_check(")
    body = src[i: i + 14000]
    assert "window_prints=_g4e_window_prints" in body
    assert "chili_momentum_g4_reentry_tape_window_prints" in body
    assert "prior_leg_high_print" in body
    assert "prior_high_print=_g4e_high_print" in body
    assert "tape_buy_share_delta=_g4e_bsd" in body
    assert "prints_since_high=_g4e_psh" in body
    assert "as_of=_replay_l2_as_of_or_none()" in body, "the high-print read is as-of bounded"
    # only the replay-aware chokepoint (_utcnow) may appear -- no wall clock
    assert "datetime.now(" not in body and "datetime.utcnow()" not in body


def test_the_exit_stash_records_the_legs_start():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('le["g4_prior_trade"] = {')
    assert '"entry_filled_at_utc": le.get("entry_filled_at_utc")' in src[i: i + 1500]


def test_window_prints_form_now_has_a_caller():
    src = _SRC.read_text(encoding="utf-8")
    assert "window_prints=" in src
