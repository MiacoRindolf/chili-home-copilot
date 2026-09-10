"""[59] THE RE-ENTRY GATE OF THE RAMP AT LEVEL 0 = a tape-proven reclaim of the
previous leg's HIGH PRINT.

Before: ``reentry_escalation_decision`` returned ``no_escalation`` at level <= 0
BEFORE any read, so after a GREEN leg (the operator's sell-into-spike -> "buy again
when viable" case) or after a profit decay to 0 there was NO bar at all; the price
compared at level >= 1 was ``tick.ask`` (a quote), the dbg was dropped on a pass,
and the same-day seed carried the reference only when the level was > 0.

Now (one PR, docs/DESIGN/MOMENTUM_LANE.md s.13):
  * level 0 WITH a prior reference: the newest PRINT must be STRICTLY above the
    prior leg's high print (margin 0 R; a print equal to the high is not a new
    high) AND the print-indexed tape must lift (accel > 0 AND buy_share_delta > 0,
    window_prints = 255) -- ``reclaim_of_prior_leg_high_wait`` / ``reclaim_met_level0``;
    no reference => ``no_escalation`` unchanged; unreadable tape => skipped as at
    level >= 1; no structural-trigger requirement and no leader bypass at level 0;
  * the price is the tape's ``last_print`` (new key on ``_signed_tape_features``),
    ``tick.ask`` a NAMED fallback (``price_kind``);
  * a PASS with a prior leg emits ``g4_reentry_reclaim_proven`` carrying the
    reference, print, tape, ``spread_bps`` (the L1 the print printed against;
    REPORTED, not enforced) and the ``binding`` block;
  * the continuation fire and the same-day seed cover the level-0 case.

MEASURED (2026-09-10, 14 d live, read-only):
  * bar at the 45 live re-entry instants: prior=GREEN 15 legs = -$105.23, refused
    ALL 15 (12 no_reclaim -$86.41, 3 tape_neg -$18.82), allowed 0; prior=RED 30 =
    -$745.53, allowed 1 (WYHG 09-08 09:00Z, -$48.88);
  * the AUTOMATIC re-buy is refuted at L1: [59]-form re-entry at the ASK of the
    reclaim print, exit at the BID of the next G-all trigger, n=58: print +$49.91
    -> L1 -$336.72; spread at reclaim p25 27.6 / p50 52.1 / p75 82.3 / p90 130.2 bps.
    Hence a WAIT inside the normal entry path, never a mechanical re-buy.

Runnable: pytest tests/test_reentry_bar_level0_prior_leg_high.py -v
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.risk_policy import reentry_escalation_decision

_SRC = pathlib.Path(LR.__file__)

MEASURED = {
    "green_prior_legs": 15, "green_prior_pnl": -105.23, "green_prior_allowed": 0,
    "red_prior_legs": 30, "red_prior_pnl": -745.53, "red_prior_allowed": 1,
    "auto_rebuy_n": 58, "auto_rebuy_print_usd": 49.91, "auto_rebuy_l1_usd": -336.72,
    "spread_at_reclaim_p50_bps": 52.1,
}


def test_the_measurement_says_bar_not_rebuy():
    assert MEASURED["green_prior_allowed"] == 0 and MEASURED["green_prior_pnl"] < 0
    assert MEASURED["auto_rebuy_print_usd"] > 0 > MEASURED["auto_rebuy_l1_usd"], (
        "positive at the print, negative at L1: the reclaim is a refusal, not a re-buy")


# ── the pure bar at level 0 ──────────────────────────────────────────────────


def _l0(**kw):
    base = dict(enabled=True, escalation_level=0, structural_trigger=False,
                live_price=3.81, prior_hwm=3.79, prior_exit_price=3.72, prior_risk_dist=0.2,
                tape_accel=5000.0, tape_buy_share_delta=0.12, prior_high_print=3.80,
                prints_since_high=0)
    base.update(kw)
    return reentry_escalation_decision(**base)


def test_level0_refuses_a_print_at_or_below_the_prior_leg_high():
    for px in (3.65, 3.79, 3.80):
        ok, dbg = _l0(live_price=px)
        assert ok is False, px
        assert dbg["reason"] == "reclaim_of_prior_leg_high_wait"
        assert dbg["reclaim_form"] == "level0_new_high_print"
        assert dbg["reference_kind"] == "prior_leg_high_print"
        assert dbg["reference"] == 3.80 and dbg["required_reclaim"] == 3.80
        assert dbg["margin_r"] == 0


def test_a_print_equal_to_the_high_is_not_a_new_high():
    """STRICT '>': at level >= 1 the escalated bar is '>=' (pinned elsewhere); at level 0
    the bar IS the high print, so equality is a touch, not a reclaim."""
    assert _l0(live_price=3.80)[0] is False
    assert _l0(live_price=3.8000001)[0] is True
    # ... whereas level 1 with margin 0 still passes at equality (unchanged)
    ok1, dbg1 = _l0(escalation_level=1, structural_trigger=True, live_price=3.80)
    assert ok1 is True and dbg1["reason"] == "reclaim_met"


def test_level0_passes_a_new_high_print_with_the_tape_lifting():
    ok, dbg = _l0(live_price=3.81)
    assert ok is True and dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_hold"] == "accel_and_buy_share_delta"
    assert dbg["tape_hold_form"] == "print_indexed_accel_and_buy_share_delta"


@pytest.mark.parametrize("accel,bsd", [(-1116.0, -0.081), (5000.0, -0.01), (-1.0, 0.3), (0.0, 0.3), (5000.0, 0.0)])
def test_level0_refuses_when_the_tape_does_not_lift(accel, bsd):
    ok, dbg = _l0(live_price=3.90, tape_accel=accel, tape_buy_share_delta=bsd)
    assert ok is False and dbg["reason"] == "tape_not_confirming"


def test_level0_without_a_reference_is_no_escalation_unchanged():
    ok, dbg = _l0(prior_high_print=None, prior_hwm=None, prior_exit_price=None, live_price=1.0)
    assert (ok, dbg["reason"]) == (True, "no_escalation")
    # the pre-[59] contract on a bare level 0 is byte-identical
    ok2, dbg2 = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False, live_price=None,
        prior_hwm=None, prior_exit_price=None, prior_risk_dist=None, tape_accel=None)
    assert (ok2, dbg2["reason"]) == (True, "no_escalation")


def test_level0_reference_fallbacks_say_so():
    ok, dbg = _l0(prior_high_print=None, live_price=3.795)
    assert ok is True and dbg["reference_kind"] == "hwm_fallback" and dbg["reference"] == 3.79
    ok2, dbg2 = _l0(prior_high_print=None, prior_hwm=None, live_price=3.71)
    assert ok2 is False and dbg2["reference_kind"] == "exit_price_fallback" and dbg2["reference"] == 3.72


def test_level0_unreadable_tape_does_not_starve_but_says_so():
    ok, dbg = _l0(live_price=3.81, tape_accel=None, tape_buy_share_delta=None)
    assert ok is True and dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_hold"] == "unreadable_skipped"


def test_level0_legacy_tape_form_when_only_accel_is_readable():
    ok, dbg = _l0(live_price=3.81, tape_accel=10.0, tape_buy_share_delta=None)
    assert ok is True and dbg["tape_hold"] == "accel"
    ok2, dbg2 = _l0(live_price=3.81, tape_accel=-10.0, tape_buy_share_delta=None, tape_back_buy_share=0.9)
    assert ok2 is True and dbg2["tape_hold"] == "majority_buy"
    ok3, dbg3 = _l0(live_price=3.81, tape_accel=-10.0, tape_buy_share_delta=None, tape_back_buy_share=0.2)
    assert ok3 is False and dbg3["reason"] == "tape_not_confirming"


def test_level0_no_print_fails_open_like_the_escalated_path():
    for bad in (None, float("nan"), 0.0, -1.0):
        ok, dbg = _l0(live_price=bad)
        assert ok is True and dbg["reason"] == "no_live_price_fail_open", bad


def test_level0_has_no_structural_requirement_and_no_leader_bypass():
    # non-structural fire with the bar met => passes (no substitute needed at level 0)
    ok, dbg = _l0(structural_trigger=False, live_price=3.81)
    assert ok is True and "reclaim_structural_substitute" not in dbg
    # the leader below the high print with a lifting tape still WAITS (no ignition bypass)
    ok2, dbg2 = _l0(structural_trigger=True, is_day_leader=True, live_price=3.70)
    assert ok2 is False and dbg2["reason"] == "reclaim_of_prior_leg_high_wait"


def test_level0_receipt_carries_the_binding_inputs():
    _, dbg = _l0(live_price=3.65)
    for k in ("reference", "reference_kind", "required_reclaim", "reclaim_form", "margin_r",
              "prior_high_print", "buy_share_delta", "prints_since_high", "tape_hold_form", "live_price"):
        assert k in dbg, k


def test_escalated_path_keeps_its_margin_and_reports_it():
    ok, dbg = _l0(escalation_level=3, structural_trigger=True, live_price=4.19)
    assert ok is False and dbg["reason"] == "reclaim_not_met"
    assert dbg["margin_r"] == 2 and dbg["required_reclaim"] == pytest.approx(3.80 + 2 * 0.2)
    assert dbg["reclaim_form"] == "escalated_reclaim_with_margin"
    ok2, dbg2 = _l0(escalation_level=3, structural_trigger=True, live_price=4.20)
    assert ok2 is True and dbg2["reason"] == "reclaim_met"


# ── measured legs, as fixed expectations (real functions on the live tape) ──


def test_measured_skyq_09_10_1355z_green_prior_no_reclaim():
    """SKYQ session 21591, re-entry 13:55:34Z after a GREEN leg (+181.59): prior leg
    high print 3.80 (12,425 prints), last print 3.65 (bid 3.65 / ask 3.66, 27.4 bps),
    accel -3,269, buy_share_delta +0.045 => WAIT. The leg went on to lose."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False, live_price=3.65,
        prior_hwm=None, prior_exit_price=3.72, prior_risk_dist=None, tape_accel=-3269.0,
        prior_high_print=3.80, tape_buy_share_delta=0.0452, prints_since_high=243)
    assert ok is False and dbg["reason"] == "reclaim_of_prior_leg_high_wait"


def test_measured_tnon_09_10_1310z_green_prior_no_reclaim_despite_a_lifting_tape():
    """TNON session 21587, re-entry 13:10:41Z after a GREEN leg (+36.75): high print
    4.39, last print 4.31, accel +15,623, buy_share_delta +0.405 (the tape lifts, but
    below the high) => WAIT. The leg bailed out at -25.73."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False, live_price=4.31,
        prior_hwm=None, prior_exit_price=4.33, prior_risk_dist=None, tape_accel=15623.0,
        prior_high_print=4.39, tape_buy_share_delta=0.4046, prints_since_high=0)
    assert ok is False and dbg["reason"] == "reclaim_of_prior_leg_high_wait"


def test_measured_wyhg_09_08_0909z_green_prior_new_high_but_the_tape_refuses():
    """WYHG session 20268, re-entry 09:09:42Z after a GREEN leg (+19.60): high print
    5.99, last print 6.08 (a new high), accel -1,116, buy_share_delta -0.081 => the
    tape refuses (the print alone is not proof)."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False, live_price=6.08,
        prior_hwm=None, prior_exit_price=5.95, prior_risk_dist=None, tape_accel=-1116.0,
        prior_high_print=5.99, tape_buy_share_delta=-0.0814, prints_since_high=0)
    assert ok is False and dbg["reason"] == "tape_not_confirming"


# ── the tape returns the PRINT (and the L1 it printed against) ───────────────


def _rows(n, *, ts0=1_800_000_000.0):
    return [(1.0 + i * 0.01, 100, 0.99 + i * 0.01, 1.01 + i * 0.01, ts0 + i) for i in range(n)]


def test_signed_tape_features_return_the_last_print_and_its_l1():
    out = EG._signed_tape_features(_rows(12), window_s=15.0, tick_rate_floor_pctile=0.5)
    assert out is not None
    assert out["last_print"] == pytest.approx(1.11)
    assert out["last_bid"] == pytest.approx(1.10) and out["last_ask"] == pytest.approx(1.12)
    assert out["last_ts"] == pytest.approx(1_800_000_011.0)


def test_last_print_is_the_newest_print_even_when_the_window_is_gap_restricted():
    rows = _rows(6) + [(2.0 + i * 0.01, 50, 1.99 + i * 0.01, 2.01 + i * 0.01, 1_800_000_100.0 + i) for i in range(4)]
    out = EG._signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.5)
    assert out is not None and out["gap_restricted"] is True
    assert out["last_print"] == pytest.approx(2.03)


def test_last_print_ignores_unparseable_and_zero_rows():
    rows = _rows(5) + [("x", 1, None, None, 1_800_000_010.0), (0.0, 5, 1.0, 1.1, 1_800_000_011.0)]
    out = EG._signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.5)
    assert out["last_print"] == pytest.approx(1.04)


def test_last_l1_is_none_when_the_print_carries_no_quote():
    rows = [(1.0 + i * 0.01, 100, None, None, 1_800_000_000.0 + i) for i in range(6)]
    out = EG._signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.5)
    assert out["last_print"] == pytest.approx(1.05)
    assert out["last_bid"] is None and out["last_ask"] is None


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

        return _R()

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()


def test_window_prints_form_returns_last_print_as_of_bounded(monkeypatch):
    from app.services.trading.momentum_neural import optional_db_read as ODR

    monkeypatch.setattr(ODR, "optional_fetchall", lambda db, stmt, params=None: db.execute(stmt, params).fetchall())
    db = _FakeDB(_rows(12))
    out = EG.signed_tape_accel_features("SKYQ", db=db, window_prints=12, as_of=datetime(2026, 9, 10, 13, 55, 34))
    assert out["last_print"] == pytest.approx(1.11)
    sql, params = db.statements[0]
    assert "LIMIT :n" in sql and params["n"] == 12 and "observed_at <= :as_of" in sql


# ── the runner: level 0 with a prior leg reads the tape, compares the PRINT ──


class _Tick:
    def __init__(self, ask, bid=None):
        self.ask = ask
        self.bid = bid
        self.mid = ask


GREEN_PRIOR = {"exit_price": 3.72, "high_water_mark": 3.79, "risk_dist": 0.20, "was_loss": False,
               "exit_reason": "burst_window_exit", "exited_at_utc": "2026-09-10T13:53:29",
               "entry_filled_at_utc": "2026-09-10T13:50:56+00:00"}


def _harness(monkeypatch, *, level, prior, tape, high_print, hp_calls=None, as_of=None):
    now = datetime(2026, 9, 10, 13, 55, 34)
    monkeypatch.setattr(LR, "_utcnow", lambda: now)
    monkeypatch.setattr(LR, "_replay_l2_as_of_or_none", lambda: as_of)
    emitted = []
    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: emitted.append((et, payload)))
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 0, "stopout_cycles": 0, "source_session_id": None, "prior_trade": None, "sessions_seen": 0})
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct",
                        lambda db, s, entry_price: (_ for _ in ()).throw(AssertionError("band read at level 0")))
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: tape)

    def _hp(*a, **k):
        if hp_calls is not None:
            hp_calls.append(k)
        return (high_print, 12425)

    monkeypatch.setattr(EG, "prior_leg_high_print", _hp)
    le = {"g4_reentry_escalation": level, "g4_escalation_seed_checked": True}
    if prior is not None:
        le["g4_prior_trade"] = prior
    sess = SimpleNamespace(id=21591, symbol="SKYQ", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    return le, sess, via, emitted


SKYQ_TAPE_1355Z = {"signed_tape_accel": -3269.0, "back_buy_share": 0.5, "buy_share_delta": 0.0452,
                   "prints_since_high": 243, "n_ticks": 255, "last_print": 3.65, "last_bid": 3.65, "last_ask": 3.66}


def test_level0_with_a_prior_leg_reads_the_tape_and_compares_the_print(monkeypatch):
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z, high_print=3.80)
    # the quote is ABOVE the high print; the PRINT is not -- the print decides
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.85)
    assert (ok, lvl) == (False, 0)
    assert dbg["reason"] == "reclaim_of_prior_leg_high_wait"
    assert dbg["price"] == 3.65 and dbg["price_kind"] == "last_print" and dbg["quote_px"] == 3.85
    assert dbg["reference"] == 3.80 and dbg["reference_kind"] == "prior_leg_high_print"
    assert dbg["spread_bps"] == pytest.approx(27.36, abs=0.01) and dbg["spread_kind"] == "last_print_l1"
    assert dbg["signed_tape_accel"] == -3269.0 and dbg["buy_share_delta"] == pytest.approx(0.0452)
    assert dbg["tape_window_prints"] == int(settings.chili_momentum_g4_reentry_tape_window_prints)
    assert dbg["prior_leg_was_loss"] is False
    assert dbg["binding"]["window_prints"] == 255 and dbg["binding"]["margin_r"] == 0
    assert dbg["binding"]["reclaim_form"] == "level0_new_high_print"
    assert dbg["binding"]["spread_policy"] == "reported_not_enforced"
    assert [et for et, _ in emitted] == [], "a refusal is the caller's receipt (g4_reentry_escalation_blocked)"


def test_level0_pass_emits_the_reclaim_proven_receipt(monkeypatch):
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12, prints_since_high=0,
                last_print=3.81, last_bid=3.80, last_ask=3.82)
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape, high_print=3.80)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert (ok, lvl, dbg["reason"]) == (True, 0, "reclaim_met_level0")
    proven = [p for et, p in emitted if et == "g4_reentry_reclaim_proven"]
    assert len(proven) == 1
    p = proven[0]
    assert p["reference"] == 3.80 and p["reference_kind"] == "prior_leg_high_print"
    assert p["price"] == 3.81 and p["price_kind"] == "last_print"
    assert p["escalation_level"] == 0 and p["reclaim_form"] == "level0_new_high_print"
    assert p["signed_tape_accel"] == 5000.0 and p["buy_share_delta"] == 0.12 and p["prints_since_high"] == 0
    assert p["tape_window_prints"] == 255 and p["tape_n_prints"] == 255
    assert p["spread_bps"] == pytest.approx(1e4 * 0.02 / 3.81, abs=0.01)
    assert p["tape_hold"] == "accel_and_buy_share_delta"
    assert p["binding"]["window_prints"] == 255 and p["binding"]["margin_r"] == 0
    assert "52.1" in p["binding"]["spread_derivation"], "the L1 refutation travels with the receipt"
    assert p["prior_leg_entry_filled_at_utc"] == GREEN_PRIOR["entry_filled_at_utc"]


def test_escalated_pass_also_emits_the_receipt(monkeypatch):
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12, last_print=4.25, last_bid=4.24, last_ask=4.26)
    prior = dict(GREEN_PRIOR, was_loss=True, risk_dist=0.20)
    le, sess, via, emitted = _harness(monkeypatch, level=2, prior=prior, tape=tape, high_print=3.80)
    le["g4_leader_min"] = "202609101355"
    le["g4_leader_is"] = False
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.01, 10))
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.26)
    assert (ok, lvl, dbg["reason"]) == (True, 2, "reclaim_met")
    assert dbg["required_reclaim"] == pytest.approx(4.00) and dbg["margin_r"] == 1
    proven = [p for et, p in emitted if et == "g4_reentry_reclaim_proven"]
    assert len(proven) == 1 and proven[0]["binding"]["margin_r"] == 1
    assert proven[0]["binding"]["reclaim_form"] == "escalated_reclaim_with_margin"


def test_quote_is_a_named_fallback_when_the_window_has_no_print(monkeypatch):
    tape = {k: v for k, v in SKYQ_TAPE_1355Z.items() if k not in ("last_print", "last_bid", "last_ask")}
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape, high_print=3.80)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert dbg["price"] == 3.66 and dbg["price_kind"] == "quote_ask_fallback"
    assert dbg["spread_bps"] is None and dbg["spread_kind"] is None
    # no tape at all: still the quote, still named
    le2, sess2, via2, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=None, high_print=3.80)
    _, dbg2, _ = LR._g4_reentry_escalation_check(None, sess2, le2, via2, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert dbg2["price_kind"] == "quote_ask_fallback" and dbg2["reason"] == "reclaim_of_prior_leg_high_wait"
    # no quote either: named as absent, fail-open
    le3, sess3, via3, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=None, high_print=3.80)
    ok3, dbg3, _ = LR._g4_reentry_escalation_check(None, sess3, le3, via3, trigger_reason="momentum_ok_rel_vol", tick_px=None)
    assert ok3 is True and dbg3["price_kind"] is None and dbg3["reason"] == "no_live_price_fail_open"


def test_level0_no_reference_at_all_is_no_escalation_with_no_receipt(monkeypatch):
    prior = {"exit_reason": "burst_window_exit", "exited_at_utc": "2026-09-10T13:53:29"}  # nothing usable
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=prior, tape=SKYQ_TAPE_1355Z, high_print=None)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert (ok, lvl, dbg["reason"]) == (True, 0, "no_escalation")
    assert emitted == []


def test_the_prior_legs_high_print_is_read_once_per_leg_live(monkeypatch):
    calls = []
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z, high_print=3.80, hp_calls=calls)
    for _ in range(3):
        LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert len(calls) == 1, "a closed leg's high print never changes"
    assert le["g4_prior_leg_high_print_cache"]["high"] == 3.80
    # the next leg invalidates it
    le["g4_prior_trade"] = dict(GREEN_PRIOR, exited_at_utc="2026-09-10T14:10:00")
    LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert len(calls) == 2


def test_the_high_print_is_not_cached_in_replay(monkeypatch):
    calls = []
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z, high_print=3.80,
                                hp_calls=calls, as_of=datetime(2026, 9, 10, 13, 52, 0))
    for _ in range(2):
        LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert len(calls) == 2 and "g4_prior_leg_high_print_cache" not in le
    assert all(c["as_of"] == datetime(2026, 9, 10, 13, 52, 0) for c in calls)


# ── source pins: the wiring ──────────────────────────────────────────────────


def _helper_body() -> str:
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def _g4_reentry_escalation_check(")
    j = src.index("def tick_live_session(", i)
    return src[i:j]


def test_the_helper_short_circuits_only_without_a_prior_leg():
    body = _helper_body()
    assert "if _g4e_level <= 0 and not _g4e_prior:" in body
    assert body.index("_g4e_prior = le.get") < body.index("if _g4e_level <= 0 and not _g4e_prior:")


def test_the_price_is_the_last_print_with_the_quote_named_as_fallback():
    body = _helper_body()
    assert '_g4e_tape.get("last_print")' in body
    assert '"last_print"' in body and '"quote_ask_fallback"' in body
    assert "live_price=_g4e_px" in body


def test_the_pass_receipt_is_emitted_from_the_one_helper():
    body = _helper_body()
    assert '"g4_reentry_reclaim_proven"' in body
    assert '"spread_bps"' in body and '"binding"' in body and '"price_kind"' in body
    src = _SRC.read_text(encoding="utf-8")
    assert src.count('"g4_reentry_reclaim_proven"') == 1, "one receipt writer, both doors"


def test_the_continuation_gate_covers_the_level0_prior_leg_case():
    src = _SRC.read_text(encoding="utf-8")
    a = src.index("FIX 1: MOMENTUM-CONTINUATION ENTRY (additive new-high fire)")
    b = src.index("if _continuation_fired:", a)
    region = src[a:b]
    i = region.index('_trigger_reason == "g4_reentry_escalation_wait"')
    cond = region[i: i + 400]
    assert 'int(le.get("g4_reentry_escalation") or 0) > 0' in cond
    assert 'isinstance(le.get("g4_prior_trade"), dict)' in cond


def test_the_same_day_seed_applies_the_reference_outside_the_level_condition():
    body = _helper_body()
    i = body.index("same_day_escalation_seed(")
    seed = body[i: i + 3000]
    assert "_sd_pt_applied" in seed
    assert seed.index('le["g4_prior_trade"] = dict(_sd_pt)') < seed.index("if _sd_level > 0 or _sd_cycles > 0:")
    assert "if _sd_level > 0 or _sd_cycles > 0 or _sd_pt_applied:" in seed


def test_no_new_knob_and_no_cooldown():
    """The only binding constant is window_prints (255, #1376's derivation); the margin
    at level 0 is 0 by construction; the spread is reported, not enforced."""
    names = [n for n in type(settings).model_fields if "reclaim" in n and "level0" in n]
    assert names == [], names
    body = _helper_body()
    assert "cooldown" not in body.lower()
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    n = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "_g4_reentry_escalation_check")
    assert n == 2, "still one check, two doors"


def test_the_chase_cap_is_unchanged_was_loss_only():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("ANTI-CHASE re-entry guard")
    assert 'if _cc_cap > 0 and _cc_prior and bool(_cc_prior.get("was_loss")):' in src[i: i + 4000]


def test_the_design_doc_records_the_level0_bar():
    doc = pathlib.Path(LR.__file__).resolve().parents[4] / "docs" / "DESIGN" / "MOMENTUM_LANE.md"
    text = doc.read_text(encoding="utf-8")
    assert "reclaim_of_prior_leg_high_wait" in text and "g4_reentry_reclaim_proven" in text
    assert "52.1" in text
    assert ("−$336.72" in text) or ("-$336.72" in text), "the L1 refutation is recorded"


def test_the_continuation_blocked_receipt_reports_the_value_that_decided():
    """[59] FIX (found reviewing this PR): the continuation-fire blocked receipt spread
    ``_mc_tape_dbg``'s own tape keys AFTER the decision dbg. ``signed_tape_accel`` did
    not exist on the dbg before [59], so the collision was invisible — but [59] puts the
    DECIDING accel (window_prints=255) on the dbg, and the later spread silently
    overwrote it with the continuation's own (different) window under the SAME name.
    A receipt must carry the value that decided; the continuation's tape gets its own
    name."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('"reason": "continuation_fire_did_not_clear_bar",')
    tail = src[i: i + 1400]
    assert '**{("continuation_" + k): _mc_tape_dbg.get(k) for k in (' in tail
    # no bare re-spread of the three tape keys after the decision dbg
    assert '**{k: _mc_tape_dbg.get(k) for k in (' not in tail
    # and the deciding accel from the helper survives to the payload
    j = src.index('"blocked_trigger": "momentum_continuation",')
    head = src[j: i]
    assert "**{k: v for k, v in _mcg_dbg.items() if k != \"reason\"}," in head


def test_the_deciding_accel_is_not_clobbered_by_the_continuation_window():
    """Executable form of the same pin: build the payload the runner builds and assert
    the accel that refused (the helper's) is the one on the receipt."""
    _mcg_dbg = {"reason": "reclaim_of_prior_leg_high_wait", "signed_tape_accel": -3269.0,
                "price": 3.65, "price_kind": "last_print", "reference": 3.80}
    _mc_tape_dbg = {"signed_tape_accel": 812.0, "tick_rate": 4.2, "n_ticks": 40}
    payload = {
        "blocked_trigger": "momentum_continuation",
        "escalation_level": 0,
        **{k: v for k, v in _mcg_dbg.items() if k != "reason"},
        "decision_reason": _mcg_dbg.get("reason"),
        "reason": "continuation_fire_did_not_clear_bar",
        "continuation_reason": "new_high",
        **{("continuation_" + k): _mc_tape_dbg.get(k) for k in (
            "signed_tape_accel", "tick_rate", "n_ticks")},
    }
    assert payload["signed_tape_accel"] == -3269.0, "the value that decided"
    assert payload["continuation_signed_tape_accel"] == 812.0, "the other window, named"
    assert payload["price"] == 3.65 and payload["price_kind"] == "last_print"
