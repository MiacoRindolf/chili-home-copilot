"""[23] ANG LEADER BYPASS AY RANKING NA NAGWA-WAIVE NG BAR — SUKATIN, HUWAG PANG BAGUHIN.

``risk_policy.reentry_escalation_decision`` step 2: kapag ang presyo ay MAS MABABA sa
``required`` pero ang pangalan ay day-leader AT structural ang trigger AT tape+ ⇒ PASA
(``leader_ignition_bypass``) — ang buong ``(level-1)*R`` na margin ay waived. At ang cap
exemption (``live_reentry_cap_leader_exempt``) ay ranking din, kaya lampas sa cap ay walang
bar.

SINUKAT (live `chili`, read-only, 2026-09-11 hanggang 11:00Z): TNON 8 bypass fill = -$82.51
laban sa 2 reclaim-proven fill = +$24.26; ang 2 lampas-cap bypass fill (22129, pagkatapos ng
09:24:46 exemption sa stopout_cycles 3 / level 4) = +$23.98. Forward ([7] metric, 15-min
MFE >= 2%, isang sample kada symbol-15min, 30 araw): non-leader na parehong hugis na
tinanggihan 6/11 = 0.545 (8 cluster) = reference 0.548; leader bypass 4/5 = 0.80 (1
cluster); proven 2/3 (1 cluster). HINDI makapagpasya ⇒ WALANG pagbabago ng gawi.

ANG SLICE AY RESIBO: ang pass receipts (``g4_reentry_pass_unproven`` /
``g4_reentry_reclaim_proven``) ay nagdadala na ng ``is_day_leader``, ``structural_trigger``,
``stopout_cycles``, ``past_stopout_cap`` at ang binding na ``max_stopout_reentries`` —
para masukat ang lampas-cap na populasyon. Ang DEDUPE KEY ay hindi ginalaw (hindi sila
nagpapasya), at ang blocked receipt (``**dbg``, 1,141-2,061 hilera/araw) ay byte-identical.

SUSUNOD NA HAKBANG (nasa planner row [23]): pagkatapos ng 5 araw ng resibo, patakbuhin muli
ang forward na may REALIZED P&L kada klase; kung lampas-cap bypass < proven, ang lampas-cap na
re-entry ay papayagan LAMANG kapag ``reclaim_proven`` (tape ang nagpapatunay ng exemption).

DB-free (the runner harness monkeypatches every read). Runnable:
pytest tests/test_g4_pass_receipt_carries_leader_and_cap.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

from tests.test_g4_substitute_fails_open_on_missing_data import (
    POS_TAPE,
    _blocked_emit_payload,
    _lr_harness,
)
from tests.test_reentry_bar_level0_prior_leg_high import (
    GREEN_PRIOR,
    SKYQ_TAPE_1355Z,
    _harness,
)

NEW_PASS_KEYS = (
    "is_day_leader", "structural_trigger", "stopout_cycles", "past_stopout_cap",
    "max_stopout_reentries",
)

#: The live measurement behind the slice (2026-09-11 to 11:00Z, TNON 22129/22141).
TNON_0911 = {
    "bypass_fills": 8, "bypass_pnl": -82.51,
    "proven_fills": 2, "proven_pnl": 24.26,
    "post_cap_bypass_fills": 2, "post_cap_bypass_pnl": 23.98,
}
#: Forward, the [7] metric (15-min MFE >= 2%), one sample per symbol-15-min, 30 d.
FORWARD_30D = {
    "nonleader_refused_same_shape": (6, 11, 8),     # hits, n, clusters
    "leader_bypass": (4, 5, 1),
    "reclaim_proven": (2, 3, 1),
    "reference": 0.548,
}


def test_the_measurement_does_not_decide():
    """Why this slice is a receipt and not a behaviour change: the leader bypass's
    forward sample is ONE cluster, and the realized legs point the other way."""
    h, n, cl = FORWARD_30D["leader_bypass"]
    assert cl == 1 and n < 10
    rh, rn, _ = FORWARD_30D["nonleader_refused_same_shape"]
    assert abs(rh / rn - FORWARD_30D["reference"]) < 0.01
    assert TNON_0911["bypass_pnl"] < 0 < TNON_0911["proven_pnl"]


def _bypass(monkeypatch, *, stopout_cycles):
    """The shipped bypass: level 2, prior leg RED, price 4.05 BELOW required 4.60, day
    leader (cached), structural trigger, tape lifting (accel > 0, legacy majority form)."""
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=12000.0, buy_share_delta=None,
                back_buy_share=0.62, last_print=4.05, last_bid=4.04, last_ask=4.06)
    prior = dict(GREEN_PRIOR, was_loss=True, risk_dist=0.10)
    le, sess, via, emitted = _harness(monkeypatch, level=2, prior=prior, tape=tape, high_print=4.50)
    le["g4_leader_min"] = "202609101355"
    le["g4_leader_is"] = True
    le["stopout_cycles"] = stopout_cycles
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.01, 10))
    return le, sess, via, emitted


def test_a_leader_bypass_pass_receipt_carries_leader_trigger_and_cap(monkeypatch):
    le, sess, via, emitted = _bypass(monkeypatch, stopout_cycles=3)
    ok, dbg, _ = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.06)
    assert ok is True and dbg["reason"] == "leader_ignition_bypass"
    kinds = [et for et, _ in emitted]
    assert kinds == ["g4_reentry_pass_unproven"], kinds
    p = emitted[0][1]
    assert p["decision_reason"] == "leader_ignition_bypass"
    assert p["is_day_leader"] is True
    assert p["structural_trigger"] is True
    assert p["stopout_cycles"] == 3
    assert p["max_stopout_reentries"] == int(settings.chili_momentum_max_stopout_reentries) == 3
    assert p["past_stopout_cap"] is True
    # the pass is BELOW the bar — the receipt still says so
    assert p["reclaim_proven"] is False and p["price"] < p["required_reclaim"]


def test_below_the_cap_the_receipt_says_so(monkeypatch):
    le, sess, via, emitted = _bypass(monkeypatch, stopout_cycles=1)
    LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.06)
    p = emitted[0][1]
    assert p["stopout_cycles"] == 1 and p["past_stopout_cap"] is False


def test_the_cap_value_is_the_binding_that_ships(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_max_stopout_reentries", 5)
    le, sess, via, emitted = _bypass(monkeypatch, stopout_cycles=4)
    LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.06)
    p = emitted[0][1]
    assert p["max_stopout_reentries"] == 5 and p["past_stopout_cap"] is False


def test_the_dedupe_key_is_unchanged(monkeypatch):
    """The four new fields do not decide, so they are not in the key: the same deciding
    values on the next tick write nothing, even when the cycle count differs."""
    le, sess, via, emitted = _bypass(monkeypatch, stopout_cycles=3)
    LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.06)
    assert le["g4_reentry_pass_receipt_key"] == (
        "g4_reentry_pass_unproven|2|leader_ignition_bypass|None|4.05|4.5"
    )
    le["stopout_cycles"] = 4
    LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=4.06)
    assert [et for et, _ in emitted] == ["g4_reentry_pass_unproven"]


def test_a_proven_pass_carries_the_same_fields(monkeypatch):
    tape = dict(SKYQ_TAPE_1355Z, signed_tape_accel=5000.0, buy_share_delta=0.12,
                prints_since_high=0, last_print=3.81, last_bid=3.80, last_ask=3.82)
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape,
                                      high_print=3.80)
    ok, dbg, _ = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert ok is True and dbg["reason"] == "reclaim_met_level0"
    proven = [p for et, p in emitted if et == "g4_reentry_reclaim_proven"]
    assert len(proven) == 1
    p = proven[0]
    assert p["structural_trigger"] is False
    assert p["is_day_leader"] is None            # level 0 reads no board (fail-closed None)
    assert p["stopout_cycles"] == 0 and p["past_stopout_cap"] is False


def test_the_blocked_receipt_is_byte_identical(monkeypatch):
    """THE [59]/[7] BYTE BUDGET. The refusal event is ``**dbg`` at 1,141-2,061 rows/day;
    the new fields live on the deduped PASS receipt only. ``is_day_leader`` and
    ``structural_trigger`` were already in ``dbg`` before [23] — nothing new rides."""
    neg_tape = dict(POS_TAPE, signed_tape_accel=-28_237.0, buy_share_delta=-0.20,
                    back_buy_share=0.429)
    le, sess, via, _emitted, _ = _lr_harness(monkeypatch, tape=neg_tape)
    le["stopout_cycles"] = 4
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.88)
    assert ok is False
    payload = _blocked_emit_payload(dbg, level=lvl, prev_reason="momentum_ok_rel_vol")
    for k in ("stopout_cycles", "past_stopout_cap", "max_stopout_reentries"):
        assert k not in payload, k
        assert k not in dbg, k
    # already present before [23] (the decision's own debug) — unchanged, not new
    assert "is_day_leader" in payload and "structural_trigger" in payload


@pytest.mark.parametrize("key", NEW_PASS_KEYS)
def test_the_new_fields_are_written_on_the_pass_payload_in_source(key):
    import inspect

    src = inspect.getsource(LR._g4_reentry_escalation_check)
    assert f'_g4e_pass_payload["{key}"]' in src, key
