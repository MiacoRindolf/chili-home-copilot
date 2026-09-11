"""[59] THE RE-ENTRY GATE OF THE RAMP AT LEVEL 0 = a tape-proven reclaim of the
previous leg's HIGH PRINT.

Before: ``reentry_escalation_decision`` returned ``no_escalation`` at level <= 0
BEFORE any read, so after a GREEN leg (the operator's sell-into-spike -> "buy again
when viable" case) or after a profit decay to 0 there was NO bar at all; the price
compared at level >= 1 was ``tick.ask`` (a quote), the dbg was dropped on a pass,
and the same-day seed carried the reference only when the level was > 0.

Now (one PR, docs/DESIGN/MOMENTUM_LANE.md s.13 + s.13.1):
  * level 0 WITH a prior reference: the newest PRINT must be at or above the prior
    leg's high print (margin 0 R, ``>=`` -- the same comparison every other rung
    makes, so the rung after a GREEN banked round is the LOOSEST one) AND the
    print-indexed tape must lift (accel > 0 AND buy_share_delta > 0, window_prints =
    255) -- ``reclaim_of_prior_leg_high_wait`` / ``reclaim_met_level0``; no reference
    => ``no_escalation`` unchanged; unreadable tape => skipped as at level >= 1; no
    structural-trigger requirement and no leader bypass at level 0;
  * the price is the tape's ``last_print`` (new key on ``_signed_tape_features``),
    ``tick.ask`` a NAMED fallback (``price_kind``), and it must be ALIVE: older than
    max(the window's own gap p99, 14.69 s measured floor) => ``reentry_tape_source_stale``;
  * the level-0 bar is a WAIT, not a day-long lockout: it releases once the tape has
    printed as many prints since the leg's exit as the leg itself consumed
    (``level0_bar_expired_new_tape``);
  * a PASS with a prior leg emits ``g4_reentry_reclaim_proven`` when the reclaim was
    actually proven and ``g4_reentry_pass_unproven`` when it was bypassed or skipped,
    carrying the reference, print, print age, tape, ``spread_bps`` (the L1 the print
    printed against; REPORTED, not enforced) and the ``binding`` block, deduped by
    the deciding values;
  * crypto (``-USD``) has no tape, so level 0 short-circuits before any read;
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
import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural import risk_policy as RP_MOD
from app.services.trading.momentum_neural.risk_policy import reentry_escalation_decision

_SRC = pathlib.Path(LR.__file__)

MEASURED = {
    "green_prior_legs": 15, "green_prior_pnl": -105.23, "green_prior_allowed": 0,
    "red_prior_legs": 30, "red_prior_pnl": -745.53, "red_prior_allowed": 1,
    "auto_rebuy_n": 58, "auto_rebuy_print_usd": 49.91, "auto_rebuy_l1_usd": -336.72,
    "spread_at_reclaim_p50_bps": 52.1,
}


def test_the_measurement_says_bar_not_rebuy():
    """[59] review fix: the first form of this test asserted only on the MEASURED dict
    literal declared above it, so it could not fail for any change to app code. Run the
    measured GREEN-prior instants through the real decision instead and assert what the
    measurement claims: the bar refused every one of them."""
    measured_green_instants = [
        # (last print, prior leg high print, accel, buy_share_delta) -- SKYQ 13:55Z,
        # TNON 13:10Z, WYHG 09-08 09:09Z (the three legs quoted in the module docstring)
        (3.65, 3.80, -3269.0, 0.0452),
        (4.31, 4.39, 15623.0, 0.4046),
        (6.08, 5.99, -1116.0, -0.0814),
    ]
    refused = 0
    for px, hp, accel, bsd in measured_green_instants:
        ok, _ = _l0(live_price=px, prior_high_print=hp, prior_hwm=None, prior_exit_price=None,
                    tape_accel=accel, tape_buy_share_delta=bsd)
        refused += 0 if ok else 1
    assert refused == len(measured_green_instants), "the bar refused every measured GREEN-prior instant"
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


def test_level0_refuses_a_print_below_the_prior_leg_high():
    for px in (3.65, 3.79):
        ok, dbg = _l0(live_price=px)
        assert ok is False, px
        assert dbg["reason"] == "reclaim_of_prior_leg_high_wait"
        assert dbg["reclaim_form"] == "level0_new_high_print"
        assert dbg["reference_kind"] == "prior_leg_high_print"
        assert dbg["reference"] == 3.80 and dbg["required_reclaim"] == 3.80
        assert dbg["margin_r"] == 0


def test_equality_does_not_invert_the_ramp():
    """[59] review fix. The first form refused at equality (strict '>') while level 1 --
    the rung AFTER a losing leg, margin 0, '>=' -- passed at the same reference: the same
    name at the same price was refused after WINNING and allowed after LOSING, i.e. the
    ramp was non-monotone at the rung that matters most. Level 0 now makes the same
    comparison every other rung makes."""
    ok0, dbg0 = _l0(live_price=3.80)
    ok1, dbg1 = _l0(escalation_level=1, structural_trigger=True, live_price=3.80)
    assert ok0 is True and dbg0["reason"] == "reclaim_met_level0"
    assert ok1 is True and dbg1["reason"] == "reclaim_met"
    assert ok0 == ok1, "level 0 (after a GREEN leg) is never stricter than level 1"
    # and one tick below is still a WAIT at level 0
    assert _l0(live_price=3.7999999)[0] is False


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


def _harness(monkeypatch, *, level, prior, tape, high_print, hp_calls=None, as_of=None,
             sealed=True, prints_exceeded=False):
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
        return (high_print, 12425, sealed)

    monkeypatch.setattr(EG, "prior_leg_high_print", _hp)
    monkeypatch.setattr(EG, "prints_since_exceeds", lambda *a, **k: prints_exceeded)
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
    assert p["reclaim_proven"] is True
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
    assert len(calls) == 1, "a SEALED closed leg's high print never changes"
    assert le["g4_prior_leg_high_print_cache"]["high"] == 3.80
    assert le["g4_prior_leg_high_print_cache"]["sealed"] is True
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


def test_the_chase_cap_population_is_unchanged_not_just_its_code(monkeypatch):
    """[59] review fix. The seed now carries the prior-trade stash for the REFERENCE
    alone (a symbol-day whose only earlier leg was green), and that stash can carry
    was_loss=True at level 0 with zero cycles -- a red max_hold/kill_switch exit is neither
    stop- nor bailout-class, so it increments neither counter. The anti-chase cap fires on
    exactly was_loss, so session B would inherit a block it never inherited before this PR.
    A reference-only seed is tagged, and the cap skips tagged stashes."""
    red_prior = dict(GREEN_PRIOR, was_loss=True, exit_reason="max_hold")
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=None, tape=SKYQ_TAPE_1355Z, high_print=3.80)
    for k in ("g4_prior_trade", "g4_escalation_seed_checked", "g4_reentry_escalation"):
        le.pop(k, None)
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 0, "stopout_cycles": 0, "source_session_id": None, "prior_trade": red_prior,
        "prior_trade_session_id": 21591, "sessions_seen": 1})
    monkeypatch.setattr(RP_MOD, "prior_day_rejection_seed", lambda db, s: 0)
    LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert le["g4_prior_trade"]["was_loss"] is True, "the reference still travels"
    assert le["g4_prior_trade"]["seeded_reference_only"] is True, "and it is tagged"
    # a seed that DID carry a level is not tagged -- the cap's original population
    le2, sess2, via2, _ = _harness(monkeypatch, level=0, prior=None, tape=SKYQ_TAPE_1355Z, high_print=3.80)
    for k in ("g4_prior_trade", "g4_escalation_seed_checked", "g4_reentry_escalation"):
        le2.pop(k, None)
    le2["g4_leader_min"] = "202609101355"
    le2["g4_leader_is"] = False
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.01, 10))
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 2, "stopout_cycles": 2, "source_session_id": 21591, "prior_trade": red_prior,
        "prior_trade_session_id": 21591, "sessions_seen": 1})
    LR._g4_reentry_escalation_check(None, sess2, le2, via2, trigger_reason="pullback_break_tick_ok", tick_px=3.66)
    assert "seeded_reference_only" not in le2["g4_prior_trade"]
    # and the cap itself reads the tag BEFORE its was_loss test
    # ([46], 2026-09-11: the guard's header was rewritten when the gate became the TAPE;
    # the tag/was_loss ORDER this test exists for is unchanged, so only the anchor moved.)
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("[46] ANG CHASE GATE AY ANG TAPE, HINDI ANG ANTAS")
    region = src[i: i + 5000]
    assert 'if _cc_prior is not None and bool(_cc_prior.get("seeded_reference_only")):' in region
    assert region.index("seeded_reference_only") < region.index(
        'if _cc_cap > 0 and _cc_prior and bool(_cc_prior.get("was_loss")):')


def test_the_design_doc_records_every_reason_the_bar_can_emit():
    """[59] review fix: the first form pinned two prose NUMBERS out of the write-up.
    Sync the doc against what the CODE can actually emit instead -- every reason string the
    level-0 branch and the new guards can return has to be documented."""
    doc = pathlib.Path(LR.__file__).resolve().parents[4] / "docs" / "DESIGN" / "MOMENTUM_LANE.md"
    text = doc.read_text(encoding="utf-8")
    rp_src = pathlib.Path(RP_MOD.__file__).read_text(encoding="utf-8")
    i = rp_src.index("if lvl <= 0:")
    j = rp_src.index("# 1) structural trigger class required at any escalation level.", i)
    emitted = set(re.findall(r'dbg\["reason"\] = "([a-z0-9_]+)"', rp_src[i:j]))
    emitted |= {"reentry_tape_source_stale", "no_escalation_crypto_no_tape"}
    emitted -= {"no_escalation", "no_live_price_fail_open", "tape_not_confirming"}
    missing = sorted(r for r in emitted if r not in text)
    assert not missing, missing
    for et in ("g4_reentry_reclaim_proven", "g4_reentry_pass_unproven"):
        assert et in text, et


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
    """[59] review fix: the first form rebuilt the runner's payload expression inside
    the test body, so any other syntactic way of re-introducing the clobber left it green.
    COMPILE the runner's own payload expression out of the source and evaluate THAT."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('"blocked_trigger": "momentum_continuation",')
    a = src.rindex("{", 0, i)
    b = src.index('"signed_tape_accel", "tick_rate", "n_ticks")},', i)
    expr = src[a: src.index("})", b) + 1]
    ns = {
        "_mcg_dbg": {"reason": "reclaim_of_prior_leg_high_wait", "signed_tape_accel": -3269.0,
                     "price": 3.65, "price_kind": "last_print", "reference": 3.80},
        "_mc_tape_dbg": {"signed_tape_accel": 812.0, "tick_rate": 4.2, "n_ticks": 40},
        "_mcg_level": 0,
        "le": {"g4_reentry_escalation": 0},
        "_mc_reason": "new_high",
    }
    ns.update({"int": int, "str": str, "__builtins__": {}})
    payload = eval(compile(expr, "<runner payload>", "eval"), ns)
    assert payload["signed_tape_accel"] == -3269.0, "the value that decided"
    assert payload["continuation_signed_tape_accel"] == 812.0, "the other window, named"
    assert payload["price"] == 3.65 and payload["price_kind"] == "last_print"


# ══════════════════════════════════════════════════════════════════════════════
# [59] REVIEW FIXES (2026-09-10) — one test per confirmed finding
# ══════════════════════════════════════════════════════════════════════════════


# ── the deciding print must be ALIVE (blocking) ──────────────────────────────


def test_a_stale_tape_cannot_prove_a_reclaim():
    """The window is bounded by COUNT, not by time, and the halt-gap trim only inspects
    gaps INSIDE it — so a ten-minute-dead burst satisfies BOTH halves of the bar (a
    print above the reference AND a lifting tape) on data the market no longer offers,
    and the fill lands wherever the quote has faded to. Stale ⇒ WAIT."""
    ok, dbg = _l0(live_price=4.72, prior_high_print=4.50, tape_accel=12000.0,
                  tape_buy_share_delta=0.3, tape_stale=True, tape_age_s=612.0,
                  tape_age_bound_s=14.69)
    assert ok is False and dbg["reason"] == "reentry_tape_source_stale"
    assert dbg["tape_age_s"] == 612.0 and dbg["tape_age_bound_s"] == 14.69
    assert dbg["reclaim_proven"] is False
    # the escalated ladder is guarded by the same read
    ok2, dbg2 = _l0(escalation_level=2, structural_trigger=True, live_price=9.99,
                    tape_stale=True, tape_age_s=300.0, tape_age_bound_s=14.69)
    assert ok2 is False and dbg2["reason"] == "reentry_tape_source_stale"


def test_a_fresh_print_and_an_unreadable_tape_are_both_untouched():
    """Stale is NOT the same as absent: an unreadable tape still fails open."""
    ok, dbg = _l0(live_price=3.81, tape_stale=False, tape_age_s=0.4, tape_age_bound_s=14.69)
    assert ok is True and dbg["reason"] == "reclaim_met_level0"
    ok2, dbg2 = _l0(live_price=3.81, tape_accel=None, tape_buy_share_delta=None, tape_stale=None)
    assert ok2 is True and dbg2["tape_hold"] == "unreadable_skipped"


def test_the_window_reports_its_own_cadence_so_the_age_bound_is_derived():
    out = EG._signed_tape_features(_rows(60), window_s=15.0, tick_rate_floor_pctile=0.5)
    assert out["gap_p99_s"] == pytest.approx(1.0), "one print per second in the fixture"
    assert out["gap_max_s"] == pytest.approx(1.0)
    assert out["last_ts"] == pytest.approx(1_800_000_059.0)


def test_the_runner_computes_the_print_age_against_the_derived_bound(monkeypatch):
    """The bound is max(the window's own gap p99, the measured floor over the names we
    trade). A hot name is not refused on a three-second pause; a ten-minute-old burst is."""
    now = datetime(2026, 9, 10, 13, 55, 34)
    fresh_ts = (now - datetime(1970, 1, 1)).total_seconds() - 1.0
    stale_ts = (now - datetime(1970, 1, 1)).total_seconds() - 600.0
    fresh = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12,
                 last_print=3.81, last_bid=3.80, last_ask=3.82, last_ts=fresh_ts, gap_p99_s=0.4)
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=fresh, high_print=3.80)
    ok, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert ok is True and dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_age_s"] == pytest.approx(1.0, abs=0.01)
    assert dbg["tape_age_bound_s"] == pytest.approx(
        float(settings.chili_momentum_g4_reentry_max_print_age_seconds))
    assert dbg["binding"]["price_age_s"] == pytest.approx(1.0, abs=0.01)

    stale = dict(fresh, last_ts=stale_ts)
    le2, sess2, via2, emitted2 = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=stale, high_print=3.80)
    ok2, dbg2, _ = LR._g4_reentry_escalation_check(None, sess2, le2, via2, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert ok2 is False and dbg2["reason"] == "reentry_tape_source_stale"
    assert dbg2["tape_age_s"] == pytest.approx(600.0, abs=0.01)
    assert [et for et, _ in emitted2] == [], "a refusal is the caller's receipt"


def test_a_slow_names_own_cadence_raises_the_bound(monkeypatch):
    """A name whose own window p99 gap is 90 s is not refused at 60 s of quiet."""
    now = datetime(2026, 9, 10, 13, 55, 34)
    ts = (now - datetime(1970, 1, 1)).total_seconds() - 60.0
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12,
                last_print=3.81, last_bid=3.80, last_ask=3.82, last_ts=ts, gap_p99_s=90.0)
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape, high_print=3.80)
    ok, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert ok is True and dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_age_bound_s"] == pytest.approx(90.0)


def test_the_age_floor_setting_carries_its_derivation():
    name = "chili_momentum_g4_reentry_max_print_age_seconds"
    assert float(getattr(settings, name)) == pytest.approx(14.69)
    desc = str(type(settings).model_fields[name].description or "")
    assert "p99" in desc and "96,360" in desc and "2026-09-10" in desc


# ── the level-0 bar is a WAIT, not a day-long lockout ────────────────────────


def test_the_level0_bar_releases_once_the_tape_has_rebuilt():
    """TNON: a green leg exits 13:10 with high print 4.39, the name retraces to 3.60 and
    at 15:20 builds a fresh, unrelated setup at 3.90. With no release valve every trigger
    in this and EVERY later session of the ET day returns the same WAIT. The release is a
    tape condition: as many prints since the exit as the leg itself consumed."""
    ok, dbg = _l0(live_price=3.90, prior_high_print=4.39, prior_hwm=None, prior_exit_price=None,
                  tape_accel=9000.0, tape_buy_share_delta=0.2,
                  level0_bar_prints_budget=12425, level0_bar_prints_exceeded=True)
    assert ok is True and dbg["reason"] == "level0_bar_expired_new_tape"
    assert dbg["reclaim_proven"] is False
    assert dbg["level0_bar_prints_budget"] == 12425
    # inside the budget the bar still binds
    ok2, dbg2 = _l0(live_price=3.90, prior_high_print=4.39, prior_hwm=None, prior_exit_price=None,
                    tape_accel=9000.0, tape_buy_share_delta=0.2,
                    level0_bar_prints_budget=12425, level0_bar_prints_exceeded=False)
    assert ok2 is False and dbg2["reason"] == "reclaim_of_prior_leg_high_wait"
    # an unreadable probe keeps the bar (fail-CLOSED on the release)
    ok3, dbg3 = _l0(live_price=3.90, prior_high_print=4.39, prior_hwm=None, prior_exit_price=None,
                    tape_accel=9000.0, tape_buy_share_delta=0.2,
                    level0_bar_prints_budget=12425, level0_bar_prints_exceeded=None)
    assert ok3 is False and dbg3["reason"] == "reclaim_of_prior_leg_high_wait"


def test_the_escalated_ladder_has_no_such_expiry():
    """The release valve is the level-0 branch's own; level >= 1 keeps its four."""
    ok, dbg = _l0(escalation_level=2, structural_trigger=True, live_price=3.90,
                  level0_bar_prints_budget=10, level0_bar_prints_exceeded=True)
    assert ok is False and dbg["reason"] == "reclaim_not_met"


def test_the_runner_counts_the_prints_since_the_prior_exit(monkeypatch):
    calls = []

    def _pse(sym, **kw):
        calls.append((sym, kw))
        return True

    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z, high_print=3.80)
    monkeypatch.setattr(EG, "prints_since_exceeds", _pse)
    ok, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert ok is True and dbg["reason"] == "level0_bar_expired_new_tape"
    assert calls and calls[0][1]["k"] == 12425, "the budget is the leg's own print count"
    assert calls[0][1]["since_at"] == GREEN_PRIOR["exited_at_utc"]
    assert dbg["binding"]["level0_bar_prints_budget"] == 12425


def test_prints_since_exceeds_is_bounded_and_as_of_bounded(monkeypatch):
    from app.services.trading.momentum_neural import optional_db_read as ODR

    monkeypatch.setattr(ODR, "optional_fetchall", lambda db, stmt, params=None: db.execute(stmt, params).fetchall())
    db = _FakeDB([(1,)])
    out = EG.prints_since_exceeds("SKYQ", db=db, since_at="2026-09-10T13:53:29", k=812,
                                  as_of=datetime(2026, 9, 10, 13, 55, 34))
    assert out is True
    sql, params = db.statements[0]
    assert "OFFSET :k LIMIT 1" in sql and params["k"] == 812
    assert "observed_at > :a" in sql and "observed_at <= :c" in sql
    assert params["c"] == datetime(2026, 9, 10, 13, 55, 34)
    assert "count(" not in sql.lower(), "bounded by OFFSET, never a full count"
    # fail-closed inputs
    assert EG.prints_since_exceeds("BTC-USD", db=db, since_at="2026-09-10T13:53:29", k=8) is None
    assert EG.prints_since_exceeds("SKYQ", db=None, since_at="2026-09-10T13:53:29", k=8) is None
    assert EG.prints_since_exceeds("SKYQ", db=db, since_at=None, k=8) is None
    assert EG.prints_since_exceeds("SKYQ", db=db, since_at="2026-09-10T13:53:29", k=0) is None


# ── the pass receipt names PROOF only when the bar was proven ────────────────


def test_a_leader_ignition_bypass_is_not_a_proven_reclaim():
    """leader_ignition_bypass is set in the branch where the price is BELOW required."""
    ok, dbg = _l0(escalation_level=2, structural_trigger=True, is_day_leader=True,
                  live_price=4.05, prior_high_print=4.50, prior_risk_dist=0.10,
                  tape_accel=12000.0, tape_buy_share_delta=None, tape_back_buy_share=0.62)
    assert ok is True and dbg["reason"] == "leader_ignition_bypass"
    assert dbg["reclaim_proven"] is False
    assert float(dbg["required_reclaim"]) > float(dbg["live_price"])


def test_the_tape_leg_no_longer_erases_the_step2_reason():
    """tape_majority_buy_confirms was an UNCONDITIONAL overwrite, so it replaced
    no_reclaim_reference (no reclaim was ever checked) and leader_ignition_bypass."""
    ok, dbg = _l0(escalation_level=2, structural_trigger=True,
                  prior_high_print=None, prior_hwm=None, prior_exit_price=None,
                  live_price=4.05, tape_accel=-400.0, tape_buy_share_delta=None,
                  tape_back_buy_share=0.71)
    assert ok is True and dbg["reason"] == "no_reclaim_reference"
    assert dbg["tape_hold"] == "majority_buy" and dbg["reclaim_proven"] is False
    ok2, dbg2 = _l0(escalation_level=2, structural_trigger=True, is_day_leader=True,
                    live_price=4.05, prior_high_print=4.50, prior_risk_dist=0.10,
                    tape_accel=-400.0, tape_buy_share_delta=None, tape_back_buy_share=0.71)
    assert ok2 is True and dbg2["reason"] == "leader_ignition_bypass"
    assert dbg2["reclaim_proven"] is False


def test_an_unproven_pass_gets_its_own_event_type(monkeypatch):
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=12000.0, buy_share_delta=None,
                back_buy_share=0.62, last_print=4.05, last_bid=4.04, last_ask=4.06)
    prior = dict(GREEN_PRIOR, was_loss=True, risk_dist=0.10)
    le, sess, via, emitted = _harness(monkeypatch, level=2, prior=prior, tape=tape, high_print=4.50)
    le["g4_leader_min"] = "202609101355"
    le["g4_leader_is"] = True
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.01, 10))
    ok, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.06)
    assert ok is True and dbg["reason"] == "leader_ignition_bypass"
    kinds = [et for et, _ in emitted]
    assert kinds == ["g4_reentry_pass_unproven"], kinds
    payload = emitted[0][1]
    assert payload["reclaim_proven"] is False
    assert payload["required_reclaim"] == 4.60 and payload["price"] == 4.05
    assert payload["reference"] == 4.50


def test_the_pass_receipt_is_deduped_across_ticks_and_doors(monkeypatch):
    """Both doors call the helper on the SAME tick and the trigger fires every tick; the
    receipt is one row per CHANGE of the deciding values, not one per call."""
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12,
                last_print=3.81, last_bid=3.80, last_ask=3.82)
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape, high_print=3.80)
    for _ in range(4):
        LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert [et for et, _ in emitted] == ["g4_reentry_reclaim_proven"]
    # a different deciding print writes a new row
    tape["last_print"] = 3.95
    LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.96)
    assert len([et for et, _ in emitted if et == "g4_reentry_reclaim_proven"]) == 2


def test_the_binding_block_carries_values_not_prose(monkeypatch):
    """~450 bytes of constant derivation prose on every emitted row, on an event already
    running 1,141-2,061 rows/day. The receipt carries the VALUE and points at the doc."""
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12,
                last_print=3.81, last_bid=3.80, last_ask=3.82)
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape, high_print=3.80)
    _, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    binding = dbg["binding"]
    assert binding["derivations"] == LR._G4E_BINDING_DERIVATIONS_REF
    assert "MOMENTUM_LANE.md" in binding["derivations"]
    for gone in ("window_prints_derivation", "margin_derivation", "spread_derivation"):
        assert gone not in binding, gone
    assert len(repr(binding)) < 420, repr(binding)
    # the sentences still exist, once, at module level
    assert "52.1" in LR._G4E_BINDING_DERIVATIONS["spread_bps"]
    assert "96,360" in LR._G4E_BINDING_DERIVATIONS["price_age_bound_s"]


# ── the cached high print must be SEALED ────────────────────────────────────


def test_an_unsealed_high_print_is_never_cached(monkeypatch):
    """iqfeed_trade_ticks is written after the fact (SKYQ 09-10 available_at-observed_at
    p95 0.64 s / max 4.04 s; TNON p99 3.75 s) and the bridge has a documented
    silent-hang, while the first post-exit trigger arrives at p10 7.76 s. A read that
    lands mid-stall returns a PARTIAL max — caching it froze a wrong reference for the
    session and the receipt reported it as binding."""
    calls = []
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z,
                                high_print=3.74, hp_calls=calls, sealed=False)
    for _ in range(3):
        _, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert len(calls) == 3, "an unsealed read repeats and self-heals"
    assert "g4_prior_leg_high_print_cache" not in le
    assert dbg["prior_leg_high_print_sealed"] is False
    assert dbg["reference"] == 3.74 and dbg["reference_kind"] == "prior_leg_high_print"


def test_prior_leg_high_print_reports_whether_the_rows_landed(monkeypatch):
    from app.services.trading.momentum_neural import optional_db_read as ODR

    monkeypatch.setattr(ODR, "optional_fetchall", lambda db, stmt, params=None: db.execute(stmt, params).fetchall())
    db = _FakeDB([(3.80, 12425, 0)])
    assert EG.prior_leg_high_print("SKYQ", db=db, entry_at="2026-09-10T13:50:56",
                                   exit_at="2026-09-10T13:53:29") == (3.80, 12425, False)
    db2 = _FakeDB([(3.80, 12425, 7)])
    assert EG.prior_leg_high_print("SKYQ", db=db2, entry_at="2026-09-10T13:50:56",
                                    exit_at="2026-09-10T13:53:29") == (3.80, 12425, True)
    sql, params = db2.statements[0]
    assert "FILTER (WHERE observed_at <= :b)" in sql and "FILTER (WHERE observed_at > :b)" in sql
    assert params["c"] > params["b"], "the seal probe is bounded past the exit"


# ── crypto does not inherit the bar ─────────────────────────────────────────


def test_crypto_at_level_zero_short_circuits_before_any_read(monkeypatch):
    """A -USD name has no iqfeed_trade_ticks: the helper skips the tape read and
    prior_leg_high_print returns (None, 0, False), so the level-0 bar would degenerate to
    tick.ask vs the quote-mid HWM — a refusal with zero tape proof resting on exactly the
    quote-mid opinion this bar replaces. Zero of the [59] measurements cover it."""
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=None, high_print=None)
    sess.symbol = "BTC-USD"
    monkeypatch.setattr(EG, "signed_tape_accel_features",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("tape read on crypto")))
    monkeypatch.setattr(EG, "prior_leg_high_print",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("high print read on crypto")))
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert (ok, lvl, dbg["reason"]) == (True, 0, "no_escalation_crypto_no_tape")
    assert emitted == []


def test_crypto_at_level_one_is_unchanged(monkeypatch):
    """The escalated ladder on crypto is the old one and is not touched here."""
    prior = dict(GREEN_PRIOR, was_loss=True)
    le, sess, via, _ = _harness(monkeypatch, level=2, prior=prior, tape=None, high_print=None)
    sess.symbol = "BTC-USD"
    le["g4_leader_min"] = "202609101355"
    le["g4_leader_is"] = False
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=3.66)
    assert lvl == 2 and dbg["reason"] != "no_escalation_crypto_no_tape"


# ── the docstring states the contract the code actually has ─────────────────


def test_the_helper_docstring_states_the_current_short_circuit():
    doc = LR._g4_reentry_escalation_check.__doc__ or ""
    assert "no_escalation_crypto_no_tape" in doc
    assert "level <= 0 \u21d2" not in doc
    body = _helper_body()
    assert "if _g4e_level <= 0 and _g4e_is_crypto:" in body


def test_a_leg_with_no_readable_print_still_gets_a_release(monkeypatch):
    """When the leg's own tape is unreadable the reference degrades to the quote-mid HWM
    -- the weakest form of the bar -- so it must not become the permanent one. Its budget
    is the deciding window itself, and the basis is named on the receipt."""
    calls = []

    def _pse(sym, **kw):
        calls.append(kw)
        return False

    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z,
                                high_print=None)
    monkeypatch.setattr(EG, "prior_leg_high_print", lambda *a, **k: (None, 0, True))
    monkeypatch.setattr(EG, "prints_since_exceeds", _pse)
    _, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert dbg["reference_kind"] == "hwm_fallback"
    assert dbg["binding"]["level0_bar_prints_budget"] == 255
    assert dbg["binding"]["level0_bar_prints_budget_basis"] == "tape_window_prints"
    assert calls and calls[0]["k"] == 255


def test_the_expiry_is_monotone_and_read_once(monkeypatch):
    """Once the tape has printed past the budget it never un-prints: the probe is not
    re-run every tick after that."""
    calls = []

    def _pse(sym, **kw):
        calls.append(kw)
        return True

    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=SKYQ_TAPE_1355Z, high_print=3.80)
    monkeypatch.setattr(EG, "prints_since_exceeds", _pse)
    for _ in range(4):
        ok, dbg, _ = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
        assert ok is True and dbg["reason"] == "level0_bar_expired_new_tape"
    assert len(calls) == 1, "the probe runs until it says yes, then stops"
    assert le["g4_level0_bar_expired_key"].startswith(GREEN_PRIOR["exited_at_utc"])
