"""[23] ANG CAP AY BULAG ULIT — SA BAGONG PANGALAN NG BAILOUT.

Ang 2026-09-10 na predicate ng terminal stop-out cap ay LISTAHAN NG BIBILANGIN::

    reentry_ramp_loss_counts = _is_stop_class_exit_reason OR bailout_class_exit_reason

Pinalitan ng #1377/#1385 ang opinion bailouts ng mga tape verdict exit
(``tape_accel_rollover`` = G, ``tape_sellers_took_it`` = D) — at WALA sa dalawa ang may
``stop`` o ``bailout`` token. Kaya mula sa unang live na araw, ang pulang verdict exit ay
LIBRE sa cap:

    LBGJ 22135, 2026-09-11 09:45:43Z
      stopout_cap_skipped_non_stop_class
        {exit_reason: tape_accel_rollover, return_bps: -358.42, stopout_cycles: 0}  (-$40.00)
    LBGJ 22147, 10:18:21Z  g4_same_day_seed {seed_level: 1, seed_stopout_cycles: 0}

ANG AYOS: ang KLASE, hindi ang pangalan. Bawat PULANG exit ay strike MALIBAN sa
pinangalanang ``_CAP_NON_STRIKE_EXIT_REASONS`` (ang apat na UTOS na flatten na
pinangangalanan na ng live_runner bilang iisang klase + ang mga NAKAPLANONG labasan). Ang
susunod na bagong pangalan ng exit ay strike kapag pula.

SINUKAT (14 araw hanggang 2026-09-11, momentum_fill_outcomes mode=live side=exit, pula =
realized <= 0): bailout 27, stop 19, operator_flatten 9 (-$531.90), trail_stop 8,
tick_deadman_stop 7, momentum_break_stop 4, deadman_stop 2, tape_accel_rollover 1,
burst_window_exit 1 — ang pagbaligtad ay nagbabago ng EKSAKTONG 1 leg (ang LBGJ).

Ang DB-backed na mga test sa ibaba ay nagpapatakbo ng TUNAY na EXITED -> WATCHING recycle
ng ``tick_live_session`` (ang harness ng tests/test_no_cooldown_between_legs.py) sa
truncating ``db`` fixture (TEST_DATABASE_URL, _test DB).

Runnable: pytest tests/test_cap_counts_exit_verdict_losses.py -v
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural import risk_policy as RP
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_EXITED, STATE_WATCHING_LIVE
from app.services.trading.momentum_neural.risk_policy import (
    cap_non_strike_exit_reason,
    reentry_ramp_loss_counts,
    reentry_ramp_strike_class,
    stop_class_exit_reason,
    stopout_cycles_after_recycle,
)

from tests.test_momentum_live_runner import (  # noqa: F401
    _mk_adapter,
    _uid,
    _venue_connected_by_default,
)
from tests.test_no_cooldown_between_legs import _events, _le, _seed, _tick

_SRC = pathlib.Path(LR.__file__).read_text(encoding="utf-8")

#: The live receipt this PR answers to (read-only, bounded, 2026-09-11).
LBGJ_22135 = {
    "exit_reason": "tape_accel_rollover",
    "return_bps": -358.4229390681007,
    "stopout_cycles": 0,
    "realized_pnl_usd": -40.00,
}

#: 14 d census of RED live exits (count, momentum_fill_outcomes) and which of them the
#: predicate counted BEFORE / AFTER the inversion. Exactly one class changes.
CENSUS_14D_RED = {
    "bailout": 27, "stop": 19, "operator_flatten": 9, "trail_stop": 8,
    "tick_deadman_stop": 7, "momentum_break_stop": 4, "deadman_stop": 2,
    "tape_accel_rollover": 1, "burst_window_exit": 1,
}


def _old_rule(reason) -> bool:
    """The 2026-09-10 predicate, verbatim, for the before/after pin."""
    return bool(RP._is_stop_class_exit_reason(reason) or RP.bailout_class_exit_reason(reason))


def _cap_counts(reason, *, red: bool) -> bool:
    """The runner's composite: only a RED exit can be a strike (``_was_loss`` gates it)."""
    return bool(red) and reentry_ramp_loss_counts(reason)


# ── the class, not the name ──────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["tape_accel_rollover", "tape_sellers_took_it", "tick_deadman_stop"])
def test_a_red_exit_verdict_is_a_strike(reason):
    assert _cap_counts(reason, red=True) is True


@pytest.mark.parametrize("reason", ["tape_accel_rollover", "tape_sellers_took_it", "tick_deadman_stop"])
def test_a_green_exit_verdict_is_not_a_strike(reason):
    """G sells INTO the spike above entry — usually green. A green exit never counts."""
    assert _cap_counts(reason, red=False) is False


def test_the_two_tape_verdicts_are_labelled_exit_verdict_and_the_deadman_is_a_stop():
    assert reentry_ramp_strike_class("tape_accel_rollover") == "exit_verdict"
    assert reentry_ramp_strike_class("tape_sellers_took_it") == "exit_verdict"
    # the `stop` token wins the precedence — stop-class accounting is unchanged
    assert reentry_ramp_strike_class("tick_deadman_stop") == "stop"
    assert reentry_ramp_strike_class("bailout") == "bailout"
    assert reentry_ramp_strike_class("stop_broker_zero_reconcile") == "stop"


@pytest.mark.parametrize("reason", [
    "kill_switch_flatten", "operator_flatten", "overnight_pricebus_dark_flatten", "eod_flatten",
    "alpaca_fractional_remainder_day_close",
    "max_hold", "target", "scale_out_target", "scale_out_limit",
    # suffix-decorated reconcile reasons keep their class
    "kill_switch_flatten_broker_zero_reconcile", "max_hold_retry_cap_broker_zero_reconcile",
    "operator_flatten_broker_zero_reconcile", "target_broker_zero_reconcile",
])
def test_a_red_named_non_strike_is_not_a_strike(reason):
    assert cap_non_strike_exit_reason(reason) is True
    assert reentry_ramp_strike_class(reason) is None
    assert _cap_counts(reason, red=True) is False


@pytest.mark.parametrize("reason", [
    "some_future_tape_verdict", "lost_vwap_confirmed", "breakout_failed_fast_bail",
    "exit_broker_zero_reconcile", None, "", "   ",
])
def test_an_unknown_or_missing_red_reason_is_a_strike(reason):
    """The error points toward caution, not toward blindness: a loss whose name the cap
    has never seen is still a loss (the exact way the cap went blind twice)."""
    assert reentry_ramp_strike_class(reason) == "other_red"
    assert _cap_counts(reason, red=True) is True


def test_a_prefix_is_not_a_substring():
    """``target`` matches ``target`` and ``target_*`` — not a reason that merely CONTAINS
    it; ``runner_target`` is an unknown red reason and therefore a strike."""
    assert cap_non_strike_exit_reason("runner_target") is False
    assert reentry_ramp_strike_class("runner_target") == "other_red"
    assert cap_non_strike_exit_reason("targeted") is False


def test_the_one_stop_class_classifier_is_not_widened():
    """The L4 whipsaw cadence keys on stop_class_exit_reason on BOTH ends."""
    assert stop_class_exit_reason("tape_accel_rollover") is False
    assert stop_class_exit_reason("tape_sellers_took_it") is False


def test_the_inversion_changes_exactly_one_measured_class():
    """14 d census: every class keeps its old verdict except tape_accel_rollover."""
    changed = {r for r in CENSUS_14D_RED if _old_rule(r) != reentry_ramp_loss_counts(r)}
    assert changed == {"tape_accel_rollover"}
    assert CENSUS_14D_RED["tape_accel_rollover"] == 1
    # operator_flatten (9 red, -$531.90) stays non-strike BY NAME
    assert reentry_ramp_loss_counts("operator_flatten") is False


# ── drift pins ───────────────────────────────────────────────────────────────


def test_every_exit_verdict_reason_is_a_strike_when_red():
    """DRIFT PIN: the trigger->reason map of the whole-exit tape verdict. A reason added
    there must be a strike when red without anyone remembering this file."""
    reasons = {reason for reason, _tag in LR._EXIT_VERDICT_ACTIONS.values()}
    assert reasons, "the verdict map must not be empty"
    for reason in reasons:
        assert _cap_counts(reason, red=True) is True, reason
        assert reentry_ramp_strike_class(reason) in ("exit_verdict", "stop"), reason


def test_the_verdict_label_set_matches_the_live_runner_map():
    """The LABEL set in risk_policy must equal the runner's map (the count does not
    depend on it — an unknown red reason is a strike anyway — but the receipt does)."""
    assert RP._EXIT_VERDICT_EXIT_REASONS == frozenset(
        reason for reason, _tag in LR._EXIT_VERDICT_ACTIONS.values()
    )


def _urgent_flatten_set() -> set[str]:
    """The ``_urgent = str(reason or "") in {...}`` literal of the exit-order builder —
    the runner's own name for the commanded-flatten class."""
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_urgent"
            and isinstance(node.value, ast.Compare)
            and isinstance(node.value.comparators[0], ast.Set)
        ):
            return {e.value for e in node.value.comparators[0].elts if isinstance(e, ast.Constant)}
    raise AssertionError("the runner's commanded-flatten (_urgent) set was not found")


def test_every_commanded_flatten_is_a_named_non_strike():
    """DRIFT PIN: the runner names one class of commanded flattens; the cap's non-strike
    set must contain all of them (an operator's or a kill switch's flatten is not the
    tape's verdict on the entry)."""
    urgent = _urgent_flatten_set()
    assert {"kill_switch_flatten", "operator_flatten"} <= urgent
    for reason in urgent:
        assert cap_non_strike_exit_reason(reason) is True, reason


# ── LBGJ 22135, the live receipt ─────────────────────────────────────────────


def test_lbgj_22135_now_advances_the_cap():
    r = LBGJ_22135
    assert _old_rule(r["exit_reason"]) is False          # 09:45:43Z: skipped
    assert r["return_bps"] < 0
    counts = _cap_counts(r["exit_reason"], red=True)
    assert counts is True
    assert reentry_ramp_strike_class(r["exit_reason"]) == "exit_verdict"
    n = stopout_cycles_after_recycle(
        prev_stopout_cycles=r["stopout_cycles"], recycle_was_stopout=counts,
        recycle_holds_streak=not counts,
    )
    assert n == 1   # the 10:18:21 same-day seed would now carry seed_stopout_cycles 1


# ── the runner seam, executed (DB-backed EXITED -> WATCHING recycle) ─────────


def _strike_events(db, sid):
    return [(t, dict(p)) for t, p in _events(db, sid)
            if t in ("stopout_cap_counts_bailout", "stopout_cap_skipped_non_stop_class")]


def test_runner_counts_a_red_tape_verdict_and_names_its_class(monkeypatch, db):
    """LBGJ 22135 through the SHIPPED recycle: stopout_cycles 0 -> 1 and the counted
    receipt carries strike_class='exit_verdict' (event name kept for the ledger)."""
    sess = _seed(db, symbol="T23V-USD", name="t23_verdict", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": LBGJ_22135["exit_reason"],
        "last_exit_return_bps": LBGJ_22135["return_bps"],
        "g4_prior_trade": {"exit_reason": LBGJ_22135["exit_reason"]},
        "realized_pnl_usd": LBGJ_22135["realized_pnl_usd"],
        "stopout_cycles": LBGJ_22135["stopout_cycles"],
    })
    _tick(monkeypatch, db, sess)
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    assert _le(sess).get("stopout_cycles") == 1
    evs = _strike_events(db, sess.id)
    assert [t for t, _ in evs] == ["stopout_cap_counts_bailout"], evs
    payload = evs[0][1]
    assert payload["strike_class"] == "exit_verdict"
    assert payload["exit_reason"] == "tape_accel_rollover"
    assert payload["return_bps"] == pytest.approx(-358.4229390681007)
    assert payload["stopout_cycles"] == 0            # the value BEFORE the strike


def test_runner_skips_a_red_operator_flatten_and_holds_the_streak(monkeypatch, db):
    sess = _seed(db, symbol="T23O-USD", name="t23_opflat", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "operator_flatten",
        "last_exit_return_bps": -120.0,
        "g4_prior_trade": {"exit_reason": "operator_flatten"},
        "realized_pnl_usd": -25.0,
        "stopout_cycles": 2,
    })
    _tick(monkeypatch, db, sess)
    assert _le(sess).get("stopout_cycles") == 2     # held, not advanced, not cleared
    evs = _strike_events(db, sess.id)
    assert [t for t, _ in evs] == ["stopout_cap_skipped_non_stop_class"], evs
    assert evs[0][1]["non_strike_basis"] == "named_non_strike"


def test_runner_counts_a_stop_without_a_new_receipt(monkeypatch, db):
    """Stop-class accounting is unchanged: counted, and no counted-non-stop receipt."""
    sess = _seed(db, symbol="T23S-USD", name="t23_stop", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "tick_deadman_stop",
        "last_exit_return_bps": -90.0,
        "g4_prior_trade": {"exit_reason": "tick_deadman_stop"},
        "realized_pnl_usd": -12.0,
        "stopout_cycles": 1,
    })
    _tick(monkeypatch, db, sess)
    assert _le(sess).get("stopout_cycles") == 2
    assert _strike_events(db, sess.id) == []


def test_the_revert_knob_restores_stop_class_only_with_a_named_basis(monkeypatch, db):
    monkeypatch.setattr(settings, "chili_momentum_reentry_ramp_counts_every_loss", False)
    sess = _seed(db, symbol="T23R-USD", name="t23_revert", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "tape_accel_rollover",
        "last_exit_return_bps": -358.42,
        "g4_prior_trade": {"exit_reason": "tape_accel_rollover"},
        "realized_pnl_usd": -40.0,
        "stopout_cycles": 0,
    })
    _tick(monkeypatch, db, sess)
    assert _le(sess).get("stopout_cycles") == 0
    evs = _strike_events(db, sess.id)
    assert [t for t, _ in evs] == ["stopout_cap_skipped_non_stop_class"], evs
    assert evs[0][1]["non_strike_basis"] == "revert_stop_class_only"


# ── the wiring ───────────────────────────────────────────────────────────────


def test_the_runner_calls_the_one_predicate():
    i = _SRC.index('le["last_recycle_was_stopout"]')
    region = _SRC[max(0, i - 3200): i]
    assert "reentry_ramp_strike_class(_recycle_reason)" in region
    assert '"strike_class": _strike_class' in region
    assert '"non_strike_basis": _non_strike_basis' in region
    # the old two-predicate composition is gone from the cap
    assert "bailout_class_exit_reason(_recycle_reason)" not in region


# ── [23] review fixes ─────────────────────────────────────────────────────────


def _flatten_reason_literals() -> set[str]:
    """EVERY string literal the runner can hand to the commanded-flatten path as a
    reason: the ``_urgent`` set, every constant assigned to ``_requested_flatten_reason``
    (including both arms of an ``IfExp``), every ``flatten_reason="..."`` keyword and the
    default of ``_handle_kill_switch_mid_run``'s ``flatten_reason`` parameter."""
    tree = ast.parse(_SRC)
    found: set[str] = set(_urgent_flatten_set())

    def _consts(node) -> set[str]:
        """The VALUES an assignment can take: a constant, or both arms of an IfExp (the
        condition's own constants — e.g. the ``le.get("...")`` key — are not values)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return _consts(node.body) | _consts(node.orelse)
        return set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_requested_flatten_reason" for t in node.targets
        ):
            found |= _consts(node.value)
        if isinstance(node, ast.keyword) and node.arg == "flatten_reason" and isinstance(
            node.value, ast.Constant
        ):
            found.add(node.value.value)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_kill_switch_mid_run":
            for arg, default in zip(node.args.args[-len(node.args.defaults):], node.args.defaults):
                if arg.arg == "flatten_reason" and isinstance(default, ast.Constant):
                    found.add(default.value)
    return found


def test_the_fractional_remainder_day_close_is_a_commanded_flatten():
    """The operator-flatten branch renames ``operator_flatten`` to
    ``alpaca_fractional_remainder_day_close`` when the fractional marker is set
    (``_queue_full_close``). Before the fix it classified ``other_red`` => a strike,
    while the SAME close named ``operator_flatten`` was not."""
    r = "alpaca_fractional_remainder_day_close"
    assert cap_non_strike_exit_reason(r) is True
    assert reentry_ramp_strike_class(r) is None
    assert _cap_counts(r, red=True) is False
    assert reentry_ramp_strike_class(r + "_broker_zero_reconcile") is None


def test_every_flatten_reason_literal_is_a_named_non_strike():
    """DRIFT PIN ([23] review fix): the first pin read only the ``_urgent`` literal, which
    does not contain the fractional day close. This one reads every literal that can reach
    ``_handle_kill_switch_mid_run(flatten_reason=...)``."""
    literals = _flatten_reason_literals()
    assert {"alpaca_fractional_remainder_day_close", "operator_flatten", "eod_flatten",
            "overnight_pricebus_dark_flatten", "kill_switch_flatten"} <= literals, literals
    for reason in literals:
        assert cap_non_strike_exit_reason(reason) is True, reason


def test_the_retry_cap_emergency_is_deliberately_a_strike():
    """``exit_retry_cap_emergency`` is NOT a commanded flatten: it completes an exit the
    tape already decided (``source_exit_reason``) that could not fill. Red, it is a strike
    (``other_red``) — named here so nobody adds it to the non-strike set by analogy."""
    assert LR._EXIT_RETRY_CAP_EMERGENCY_REASON == "exit_retry_cap_emergency"
    assert reentry_ramp_strike_class(LR._EXIT_RETRY_CAP_EMERGENCY_REASON) == "other_red"


def _seed_leg(db, *, symbol, name, trade_cycles, le_extra, keyed):
    """Seed an EXITED session, then stamp leg keys that need the session id."""
    sess = _seed(db, symbol=symbol, name=name, state=STATE_LIVE_EXITED,
                 le_extra=dict(le_extra, trade_cycles=trade_cycles))
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get("momentum_live_execution") or {})
    for k, v in keyed(sess.id).items():
        if isinstance(v, dict) and isinstance(le.get(k), dict):
            le[k] = {**le[k], **v}
        else:
            le[k] = v
    snap["momentum_live_execution"] = le
    sess.risk_snapshot_json = snap
    db.commit()
    db.refresh(sess)
    return sess


def _cap_events(db, sid):
    names = ("stopout_cap_counts_bailout", "stopout_cap_skipped_non_stop_class",
             "stopout_cap_held_unpriced_exit", "stopout_cap_loss_basis_whole_trade",
             "g4_reentry_escalation_level_update")
    return [(t, dict(p)) for t, p in _events(db, sid) if t in names]


def test_a_green_scaled_trade_with_a_flat_runner_resets_the_streak(monkeypatch, db):
    """Live 09-10 session 21589: scale_out_limit +$22.96 on 781 shares, then the runner
    trail_stop at entry with pnl 0.0 => the final tranche's return is 0 bps (red by the
    ``<= 0`` rule) and the +$22.96 trade was a STRIKE. The whole-trade verdict of THIS
    leg's stash now decides: green => the streak RESETS, and the disagreement is a row."""
    sess = _seed_leg(db, symbol="T23W-USD", name="t23_whole", trade_cycles=1, le_extra={
        "last_exit_reason": "trail_stop",
        "last_exit_return_bps": 0.0,
        "g4_prior_trade": {"exit_reason": "trail_stop", "was_loss": False},
        "realized_pnl_usd": 22.96,
        "stopout_cycles": 2,
    }, keyed=lambda sid: {
        "last_exit_leg_key": f"{sid}:1",
        "g4_prior_trade": {"leg_key": f"{sid}:1"},
    })
    _tick(monkeypatch, db, sess)
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    assert _le(sess).get("stopout_cycles") == 0          # green reset, not a strike
    evs = _cap_events(db, sess.id)
    kinds = [t for t, _ in evs]
    assert "stopout_cap_loss_basis_whole_trade" in kinds, kinds
    basis = dict(evs[kinds.index("stopout_cap_loss_basis_whole_trade")][1])
    assert basis["final_tranche_return_bps"] == 0.0 and basis["whole_trade_was_loss"] is False
    assert "stopout_cap_counts_bailout" not in kinds and "stopout_cap_skipped_non_stop_class" not in kinds
    lvl = [p for t, p in evs if t == "g4_reentry_escalation_level_update"]
    assert lvl and lvl[0]["was_loss"] is False


def test_the_same_trade_unstamped_keeps_the_legacy_final_tranche_read(monkeypatch, db):
    """A stash written BEFORE the fix carries no leg key: the legacy read (final tranche)
    stands, named — the fix never guesses provenance."""
    sess = _seed(db, symbol="T23U-USD", name="t23_unstamped", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "trail_stop",
        "last_exit_return_bps": 0.0,
        "g4_prior_trade": {"exit_reason": "trail_stop", "was_loss": False},
        "realized_pnl_usd": 22.96,
        "stopout_cycles": 2,
    })
    _tick(monkeypatch, db, sess)
    assert _le(sess).get("stopout_cycles") == 3          # the pre-fix behaviour, unchanged


def test_an_unpriced_exit_does_not_recount_the_prior_leg(monkeypatch, db):
    """Leg 1 exited red ``tape_accel_rollover`` at -358 bps and was counted (cycles 1).
    Leg 2 is closed by an operator FLATTEN that resolves through the unpriced broker-zero
    branch, which writes neither ``last_exit_return_bps`` nor the stash. Before the fix the
    recycle read leg 1's values and counted a SECOND strike (cycles 2); now the streak and
    the level HOLD, with a receipt naming the stale values it refused to reuse."""
    sess = _seed_leg(db, symbol="T23P-USD", name="t23_unpriced", trade_cycles=2, le_extra={
        "last_exit_reason": "tape_accel_rollover",
        "last_exit_return_bps": -358.42,
        "g4_prior_trade": {"exit_reason": "tape_accel_rollover", "was_loss": True},
        "realized_pnl_usd": -40.0,
        "stopout_cycles": 1,
        "g4_reentry_escalation": 1,
    }, keyed=lambda sid: {
        "last_exit_leg_key": f"{sid}:1",               # written by LEG 1
        "g4_prior_trade": {"leg_key": f"{sid}:1"},
    })
    _tick(monkeypatch, db, sess)
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    le = _le(sess)
    assert le.get("stopout_cycles") == 1                 # held, not 2
    assert le.get("g4_reentry_escalation") == 1          # held, not advanced
    evs = _cap_events(db, sess.id)
    kinds = [t for t, _ in evs]
    assert kinds == ["stopout_cap_held_unpriced_exit"], kinds
    p = evs[0][1]
    assert p["stale_exit_leg_key"] == f"{sess.id}:1" and p["leg_key"] == f"{sess.id}:2"
    assert p["stale_return_bps"] == pytest.approx(-358.42)
    assert p["stale_stash_exit_reason"] == "tape_accel_rollover"


def test_a_priced_red_verdict_on_this_leg_is_counted_on_the_whole_trade_basis(monkeypatch, db):
    """The stamped, same-leg path: a red verdict counts, and the receipt names the basis."""
    sess = _seed_leg(db, symbol="T23K-USD", name="t23_keyed", trade_cycles=3, le_extra={
        "last_exit_reason": "tape_sellers_took_it",
        "last_exit_return_bps": -210.0,
        "g4_prior_trade": {"exit_reason": "tape_sellers_took_it", "was_loss": True},
        "realized_pnl_usd": -25.0,
        "stopout_cycles": 1,
    }, keyed=lambda sid: {
        "last_exit_leg_key": f"{sid}:3",
        "g4_prior_trade": {"leg_key": f"{sid}:3"},
    })
    _tick(monkeypatch, db, sess)
    assert _le(sess).get("stopout_cycles") == 2
    evs = [(t, p) for t, p in _cap_events(db, sess.id) if t == "stopout_cap_counts_bailout"]
    assert len(evs) == 1
    assert evs[0][1]["strike_class"] == "exit_verdict"
    assert evs[0][1]["loss_basis"] == "whole_trade"


def test_a_stash_seeded_from_another_session_does_not_name_this_exit(monkeypatch, db):
    """The same-day seed copies ANOTHER session's stash. If this leg's own exit then skips
    the stash writer, the other session's ``exit_reason`` must not classify this exit:
    the fresh ``last_exit_reason`` (this leg's) does."""
    sess = _seed_leg(db, symbol="T23X-USD", name="t23_seeded", trade_cycles=0, le_extra={
        "last_exit_reason": "operator_flatten",
        "last_exit_return_bps": -80.0,
        "g4_prior_trade": {"exit_reason": "stop", "was_loss": True, "leg_key": "999999:4"},
        "realized_pnl_usd": -12.0,
        "stopout_cycles": 1,
    }, keyed=lambda sid: {"last_exit_leg_key": f"{sid}:0"})
    _tick(monkeypatch, db, sess)
    assert _le(sess).get("stopout_cycles") == 1          # operator_flatten: held, not counted
    evs = [(t, p) for t, p in _cap_events(db, sess.id) if t == "stopout_cap_skipped_non_stop_class"]
    assert len(evs) == 1 and evs[0][1]["exit_reason"] == "operator_flatten"
    assert evs[0][1]["loss_basis"] == "final_tranche"


def test_the_exit_writer_stamps_the_leg_key():
    """``_complete_confirmed_live_exit`` is the only writer of the priced exit; it stamps
    the leg on both the summary field and the stash."""
    src = inspect.getsource(LR._complete_confirmed_live_exit)
    assert 'le["last_exit_leg_key"] = _exit_leg_key' in src
    assert '"leg_key": _exit_leg_key' in src
    assert 'int(le.get("trade_cycles") or 0)' in src
