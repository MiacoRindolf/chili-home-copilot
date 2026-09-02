"""Purong unit test ng PATH B core: phase graph, hati ng qty, invariant checker.

WALANG DB, WALANG broker, WALANG live_runner import — sinasadya. Ang module
na sinusuri ay `path_b_partial`, na HINDI PA nakakabit (tingnan ang
`docs/DESIGN/PARTIAL_EXIT_PATH_B.md` §4 kung bakit ipinagpaliban ang wiring).

Ang bawat isa sa tatlong bahagi ay tumutugma sa isang refutation:
  * phase graph      -> R2 (chokepoint block habang may PATCH sa ere)
  * plan_replacement -> oversell / walang runner
  * assess_protection-> R4 (naked remainder) at ang short-flip guard

Runnable: pytest tests/test_partial_exit_path_b_helpers.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import path_b_partial as pb


# --------------------------------------------------------------------------
# 1. Phase graph
# --------------------------------------------------------------------------

def test_every_transition_target_is_a_known_phase():
    """Walang typo sa graph: bawat source at target ay nasa PHASES."""
    assert set(pb.LEGAL_TRANSITIONS) == set(pb.PHASES)
    for src, targets in pb.LEGAL_TRANSITIONS.items():
        for dst in targets:
            assert dst in pb.PHASES, f"{src}->{dst}"


def test_terminal_phases_have_no_outgoing_edges():
    for phase in pb.TERMINAL_PHASES:
        assert pb.LEGAL_TRANSITIONS[phase] == frozenset(), phase


def test_non_terminal_phases_all_have_an_exit():
    """Walang phase na wala nang patutunguhan — iyon ay isang deadlock (R5)."""
    for phase in pb.PHASES - pb.TERMINAL_PHASES:
        assert pb.LEGAL_TRANSITIONS[phase], phase


def test_every_phase_can_reach_a_terminal_phase():
    """Ang buong graph ay bumabagsak sa isang terminal — walang bilog na bitag."""
    reaches: set[str] = set(pb.TERMINAL_PHASES)
    changed = True
    while changed:
        changed = False
        for phase, targets in pb.LEGAL_TRANSITIONS.items():
            if phase not in reaches and targets & reaches:
                reaches.add(phase)
                changed = True
    assert reaches == set(pb.PHASES), sorted(set(pb.PHASES) - reaches)


def test_in_flight_and_terminal_are_disjoint():
    assert not (pb.IN_FLIGHT_PHASES & pb.TERMINAL_PHASES)
    assert pb.IN_FLIGHT_PHASES <= pb.PHASES
    assert pb.NAKED_RISK_PHASES <= pb.PHASES


def test_happy_path_walks_end_to_end():
    phase = "intent_frozen"
    for nxt in (
        "replace_submitted",
        "successor_certified",
        "partial_posting",
        "partial_posted",
        "partial_filled",
    ):
        phase = pb.advance_phase(phase, nxt)
    assert pb.is_terminal(phase)
    assert not pb.blocks_whole_exit(phase)


def test_certified_and_partial_phases_do_not_block_the_whole_exit():
    """Certified na = ang SUCCESSOR na ang may-ari, kaya normal ang whole exit."""
    for phase in (
        "successor_certified", "partial_posting", "partial_posted",
        "partial_indeterminate", "partial_filled",
    ):
        assert not pb.blocks_whole_exit(phase), phase


def test_patch_in_flight_blocks_the_whole_exit():
    """R2: habang nasa ere ang PATCH ay hindi puwedeng mag-freeze ng close
    handoff laban sa isang `replaced` na predecessor."""
    for phase in ("intent_frozen", "replace_submitted", "replace_indeterminate"):
        assert pb.blocks_whole_exit(phase), phase


def test_stuck_replace_escapes_to_containment_not_to_nothing():
    """R5: may labasan ang stuck na replace, at hindi ito operator event lamang."""
    assert pb.advance_phase("replace_stuck", "containment_queued")
    assert pb.advance_phase("replace_stuck", "replace_reverted")
    assert pb.advance_phase("containment_queued", "containment_resolved")


def test_rejected_partial_must_restore_before_it_can_end():
    """R4: ang `partial_rejected_final` ay may IISANG labasan — ang restore edge."""
    assert pb.LEGAL_TRANSITIONS["partial_rejected_final"] == frozenset(
        {"restore_intent_frozen"}
    )
    assert "partial_rejected_final" in pb.NAKED_RISK_PHASES


def test_illegal_transitions_raise():
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("intent_frozen", "partial_posted")   # laktaw sa PATCH
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("partial_filled", "partial_posting")  # terminal na
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("successor_certified", "intent_frozen")


def test_unknown_phase_names_raise_rather_than_pass_silently():
    for bad in ("", "  ", "posted", None):
        with pytest.raises(pb.PhaseError):
            pb.advance_phase(bad, "replace_submitted")
        with pytest.raises(pb.PhaseError):
            pb.is_terminal(bad)


# --------------------------------------------------------------------------
# 2. Hati ng qty
# --------------------------------------------------------------------------

def test_canf_shaped_split():
    """Ang tunay na hugis ngayong araw: 355 share, 30% -> 106 / 249."""
    plan = pb.plan_replacement_edge(total_qty=355, partial_qty=106)
    assert plan.ok
    assert plan.successor_qty == 249
    assert plan.partial_qty + plan.successor_qty == plan.total_qty


@pytest.mark.parametrize(
    "total,partial,reason",
    [
        (355, 355, "partial_leaves_no_runner"),
        (355, 400, "partial_leaves_no_runner"),
        (355, 0, "non_positive_partial"),
        (355, -5, "non_positive_partial"),
        (0, 10, "non_positive_total"),
        (float("nan"), 10, "non_finite_quantity"),
        (355, float("inf"), "non_finite_quantity"),
        (355, 106.5, "fractional_partial_quantity"),
        (2, 1.0, None),          # pinakamaliit na legal na hati
    ],
)
def test_split_rejections(total, partial, reason):
    plan = pb.plan_replacement_edge(total_qty=total, partial_qty=partial)
    if reason is None:
        assert plan.ok
    else:
        assert not plan.ok
        assert plan.reason == reason
        assert plan.successor_qty == 0.0


def test_split_never_returns_a_successor_larger_than_the_position():
    for q in (1, 2, 3, 10, 100, 355, 1000):
        for f in range(1, int(q)):
            plan = pb.plan_replacement_edge(total_qty=q, partial_qty=f)
            if plan.ok:
                assert 0 < plan.successor_qty < q
                assert plan.successor_qty + plan.partial_qty == q


# --------------------------------------------------------------------------
# 3. Invariant checker
# --------------------------------------------------------------------------

def test_full_stop_no_partial_is_covered():
    v = pb.assess_protection(broker_qty=355, stop_qty=355, open_partial_qty=0)
    assert v.status == "covered" and v.ok and v.naked_qty == 0.0


def test_the_intended_steady_state_is_covered():
    """Matapos ang PATCH at ang POST: stop 249 + open sell 106 == 355."""
    v = pb.assess_protection(broker_qty=355, stop_qty=249, open_partial_qty=106)
    assert v.status == "covered" and v.ok


def test_after_the_partial_fills_the_runner_is_still_covered():
    v = pb.assess_protection(broker_qty=249, stop_qty=249, open_partial_qty=0)
    assert v.status == "covered" and v.ok


def test_stale_cancel_with_k_below_f_is_a_naked_remainder():
    """R4: k=40 sa 106 ang napunan, kinansela ang natira -> 66 share na hubad."""
    v = pb.assess_protection(broker_qty=315, stop_qty=249, open_partial_qty=0)
    assert v.status == "naked_remainder"
    assert v.naked_qty == pytest.approx(66.0)
    assert not v.ok and v.requires_restore_or_flatten


def test_post_rejected_after_certification_is_a_naked_remainder():
    """R4: nag-PATCH tayo pababa sa 249 pero hindi na-post ang 106."""
    v = pb.assess_protection(broker_qty=355, stop_qty=249, open_partial_qty=0)
    assert v.status == "naked_remainder"
    assert v.naked_qty == pytest.approx(106.0)


def test_no_stop_at_all_is_naked_not_covered():
    v = pb.assess_protection(broker_qty=355, stop_qty=0, open_partial_qty=0)
    assert v.status == "naked_remainder" and v.naked_qty == pytest.approx(355.0)


def test_double_sell_authority_is_oversell_risk():
    """Kung nanatili ang lumang 355 na stop habang nakaupo ang 106 na partial
    ay may 461 share ng sell authority sa 355 — pagputok ng pareho ay short."""
    v = pb.assess_protection(broker_qty=355, stop_qty=355, open_partial_qty=106)
    assert v.status == "oversell_risk" and not v.ok


def test_flat_position_with_a_resting_sell_is_oversell_not_flat():
    v = pb.assess_protection(broker_qty=0, stop_qty=0, open_partial_qty=106)
    assert v.status == "oversell_risk" and not v.ok


def test_flat_position_is_flat():
    v = pb.assess_protection(broker_qty=0, stop_qty=0, open_partial_qty=0)
    assert v.status == "flat" and v.ok


@pytest.mark.parametrize(
    "b,s,p",
    [(float("nan"), 1, 0), (1, float("inf"), 0), (-1, 0, 0), (1, -1, 0), (1, 0, -1)],
)
def test_garbage_inputs_fail_closed(b, s, p):
    v = pb.assess_protection(broker_qty=b, stop_qty=s, open_partial_qty=p)
    assert not v.ok


# --------------------------------------------------------------------------
# 4. Konserbasyon ng lineage
# --------------------------------------------------------------------------

def test_conservation_before_the_partial_posts():
    assert pb.conservation_holds(
        broker_qty=355, successor_qty=249, partial_qty=106, partial_cum_filled=0
    )


def test_conservation_survives_a_partially_filled_sibling():
    """H2: ang hubad na `broker_qty == successor_requested` ay mabibigo rito
    magpakailanman at haharangan ang dispatch."""
    assert pb.conservation_holds(
        broker_qty=315, successor_qty=249, partial_qty=106, partial_cum_filled=40
    )
    assert not (315 == 249)  # ang lumang panuntunan, para malinaw


def test_conservation_after_the_partial_fills_whole():
    assert pb.conservation_holds(
        broker_qty=249, successor_qty=249, partial_qty=106, partial_cum_filled=106
    )


def test_conservation_for_a_restore_edge_has_no_partial_leg():
    assert pb.conservation_holds(
        broker_qty=315, successor_qty=315, partial_qty=0, partial_cum_filled=0
    )


@pytest.mark.parametrize(
    "b,r,f,k",
    [
        (355, 249, 106, 200),      # k > f
        (355, 200, 106, 0),        # nawawalang share
        (355, 300, 106, 0),        # sobrang stop
        (float("nan"), 249, 106, 0),
        (355, -249, 106, 0),
    ],
)
def test_conservation_rejects_impossible_states(b, r, f, k):
    assert not pb.conservation_holds(
        broker_qty=b, successor_qty=r, partial_qty=f, partial_cum_filled=k
    )
