"""THE RE-ENTRY RAMP MUST BIND (B): the continuation fire cannot step around G4.

``live_runner`` blocks a fired trigger with ``blocked_wait_reason =
g4_reentry_escalation_wait`` and then, in the SAME tick's WAIT branch, the
momentum-continuation fire transitions the session to ENTRY_CANDIDATE without
ever re-running ``reentry_escalation_decision``.

SKYQ 2026-09-10: blocked at level 2 (required 4.09) at 13:58:17, entered at 3.68
one second later via this path. 7 days: 58 continuation fires bypassed a G4 wait;
9 of the resulting fills are re-entries worth -$280.13, and the new bar refuses
all 9 when evaluated at the fire.

The fix routes the fire through the ONE check (``_g4_reentry_escalation_check``)
with its own live inputs; a fire that does not clear the bar stays in WAIT and
emits ``g4_reentry_escalation_blocked`` with reason
``continuation_fire_did_not_clear_bar``.

Runnable: pytest tests/test_continuation_fire_cannot_bypass_g4.py -v
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.risk_policy import reentry_escalation_decision

_SRC = pathlib.Path(LR.__file__)


# ── source guards ────────────────────────────────────────────────────────────


def _continuation_region() -> str:
    src = _SRC.read_text(encoding="utf-8")
    a = src.index("FIX 1: MOMENTUM-CONTINUATION ENTRY (additive new-high fire)")
    b = src.index("if _continuation_fired:", a)
    return src[a:b]


def test_the_fire_re_runs_the_one_check_with_its_own_inputs():
    region = _continuation_region()
    assert "_g4_reentry_escalation_check(" in region
    assert 'trigger_reason="momentum_continuation"' in region
    assert "tick_px=_mc_px" in region, "the fire's LIVE price, not the wait's"


def test_the_re_run_is_gated_on_the_g4_wait_reason():
    """REVIEW FIX (2026-09-10): the wait reason alone was a hole (SKYQ 21591 13:56:45Z entered
    at level >= 1 from a volume wait with no G4 read). The re-check must ALSO run whenever the
    name is escalated, regardless of why the standard trigger did not fire."""
    region = _continuation_region()
    assert '_trigger_reason == "g4_reentry_escalation_wait"' in region
    assert 'int(le.get("g4_reentry_escalation") or 0) > 0' in region, (
        "the continuation re-check must be gated on the escalation LEVEL, not only on the wait reason")


def test_a_failed_re_run_stays_in_wait_with_a_receipt():
    region = _continuation_region()
    i = region.index("_g4_reentry_escalation_check(")
    tail = region[i: i + 2500]
    assert "_mc_tape_ok = False" in tail, "the fire must be withdrawn, not just logged"
    assert '"continuation_fire_did_not_clear_bar"' in tail
    assert '"g4_reentry_escalation_blocked"' in tail
    # the receipt carries the bar's own verdict beside the wrapper reason
    assert '"decision_reason"' in tail


def test_the_re_run_precedes_the_transition():
    """The check must run BEFORE the ENTRY_CANDIDATE transition in source order."""
    region = _continuation_region()
    assert region.index("_g4_reentry_escalation_check(") < region.index(
        "_safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)"
    )


def test_exactly_two_callers_of_the_one_check():
    """The trigger path and the continuation fire — one check, two doors."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    n = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_g4_reentry_escalation_check"
    )
    assert n == 2, n


def test_the_trigger_path_no_longer_inlines_the_decision():
    """Both doors must go through the helper; an inlined second copy is how the
    two paths drifted apart in the first place."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    callers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "reentry_escalation_decision"
            ):
                callers.add(fn.name)
    assert callers == {"_g4_reentry_escalation_check"}, callers


# ── the pure bar, on SKYQ's own numbers ──────────────────────────────────────


def test_skyq_2026_09_10_leg3_is_refused_at_the_fire():
    """The bypassed fire: level 2, prior leg high print 3.66, R 0.28, price 3.70,
    print-indexed tape accel -10,996 / buy_share_delta -0.091. Non-structural, so
    the substitute applies: positive tape AND reclaim of 3.66 + 0.28 + 0.28 = 4.22."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=3.70, prior_hwm=3.64, prior_exit_price=3.62, prior_risk_dist=0.28,
        tape_accel=-10996.0, tape_back_buy_share=0.44, noise_abs=0.02,
        prior_high_print=3.66, tape_buy_share_delta=-0.0906, prints_since_high=0,
    )
    assert ok is False
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg["reference_kind"] == "prior_leg_high_print"
    assert dbg["substitute_required"] == pytest.approx(4.22, abs=1e-6)


def test_the_same_fire_clears_when_the_tape_and_price_actually_prove_it():
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=4.25, prior_hwm=3.64, prior_exit_price=3.62, prior_risk_dist=0.28,
        tape_accel=12000.0, tape_back_buy_share=0.7, noise_abs=0.02,
        prior_high_print=3.66, tape_buy_share_delta=0.12, prints_since_high=0,
    )
    assert ok is True
    assert dbg["reclaim_structural_substitute"] is True


# ── the helper end-to-end with fakes (no DB) ─────────────────────────────────


class _Tick:
    def __init__(self, ask):
        self.ask = ask
        self.mid = ask


def _harness(monkeypatch, *, level, prior, tape, high_print, px):
    now = datetime(2026, 9, 10, 17, 58, 17)
    monkeypatch.setattr(LR, "_utcnow", lambda: now)
    monkeypatch.setattr(LR, "_replay_l2_as_of_or_none", lambda: now)
    emitted = []
    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: emitted.append((et, payload)))
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {"level": 0, "stopout_cycles": 0, "source_session_id": None, "prior_trade": None, "sessions_seen": 0})
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.01, 10))
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: tape)
    monkeypatch.setattr(EG, "prior_leg_high_print", lambda *a, **k: (high_print, 812, True))
    monkeypatch.setattr(EG, "prints_since_exceeds", lambda *a, **k: False)
    le = {
        "g4_reentry_escalation": level,
        "g4_escalation_seed_checked": True,
        "g4_prior_trade": prior,
        # leader read cached for this minute so no board query runs
        "g4_leader_min": now.strftime("%Y%m%d%H%M"),
        "g4_leader_is": False,
    }
    sess = SimpleNamespace(id=21591, symbol="SKYQ", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    return le, sess, via, emitted


def test_helper_refuses_the_continuation_fire_on_skyq_inputs(monkeypatch):
    prior = {"high_water_mark": 3.64, "exit_price": 3.62, "risk_dist": 0.28,
             "was_loss": True, "exit_reason": "momentum_break_stop",
             "entry_filled_at_utc": "2026-09-10T17:50:00+00:00",
             "exited_at_utc": "2026-09-10T17:57:00"}
    tape = {"signed_tape_accel": -10996.0, "back_buy_share": 0.44,
            "buy_share_delta": -0.0906, "prints_since_high": 0, "n_ticks": 255}
    le, sess, via, emitted = _harness(monkeypatch, level=2, prior=prior, tape=tape, high_print=3.66, px=3.70)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_continuation", tick_px=3.70)
    assert ok is False
    assert lvl == 2
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg["reference_kind"] == "prior_leg_high_print"
    assert dbg["prior_high_print"] == 3.66
    assert dbg["buy_share_delta"] == pytest.approx(-0.0906)
    assert dbg["prints_since_high"] == 0
    assert dbg["tape_window_prints"] >= 4
    assert dbg["trigger_reason"] == "momentum_continuation"


@pytest.mark.parametrize("prior", [{}, None])
def test_helper_is_a_no_op_before_any_read_at_level_zero_without_a_prior_leg(monkeypatch, prior):
    """[59] (2026-09-10): level 0 WITH a prior leg now reads the tape (the bar is that
    leg's high print — tests/test_reentry_bar_level0_prior_leg_high.py). Level 0
    WITHOUT one (the first leg of the day, an empty or absent stash) is still a no-op
    before any read."""
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=prior, tape=None, high_print=None, px=3.7)
    if prior is None:
        le.pop("g4_prior_trade", None)
    # a tape read here would be a regression: make it explode if called
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read at level 0 without a prior leg")))
    monkeypatch.setattr(EG, "prior_leg_high_print", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read at level 0 without a prior leg")))
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_continuation", tick_px=3.7)
    assert (ok, lvl, dbg["reason"]) == (True, 0, "no_escalation")
    assert emitted == []


def test_the_re_run_also_covers_a_level_zero_name_with_a_prior_leg():
    """[59]: a continuation fire after a GREEN leg (level 0, g4_prior_trade present) must
    go through the same helper — otherwise the level-0 bar has a side door."""
    region = _continuation_region()
    assert 'isinstance(le.get("g4_prior_trade"), dict)' in region
