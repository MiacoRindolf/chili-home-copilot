"""Unit tests para sa purong PATH B core.

WALANG DB, WALANG broker, WALANG import ng `live_runner` — ito ay nasa
`tests/test_partial_exit_path_b_lineage_seam.py` at
`tests/test_partial_exit_path_b_unwired.py`.

Ang mga test dito ay hindi lamang "gumagana ba ang function". Ang mga
INVARIANT ang binabantayan — ang mga bagay na kapag nabali ay tahimik na
nagiging ligtas ang isang estadong hindi ligtas. Iyon mismo ang aral ng
#1283 (berdeng helper test, seam na hindi kailanman gumana) at ng unang
bersyon ng file na ito, kung saan ang isang test ay IGINIIT na "covered" ang
estado kung saan 30% ng posisyon ay walang stop.

Runnable: pytest tests/test_partial_exit_path_b_helpers.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import path_b_partial as pb


# ==========================================================================
# 1. Ang graph — mga invariant, hindi mga halimbawa
# ==========================================================================

def test_every_phase_has_a_transition_row_and_every_target_is_a_phase():
    assert set(pb.LEGAL_TRANSITIONS) == set(pb.PHASES)
    for src, targets in pb.LEGAL_TRANSITIONS.items():
        for dst in targets:
            assert dst in pb.PHASES, f"{src}->{dst} ay hindi phase"


def test_the_graph_the_only_validator_consults_cannot_be_rewritten():
    """`Final` ay pahiwatig sa type checker, at walang type checker ang repo na
    ito (CLAUDE.md: "No ruff/black/mypy configured"). Ang `advance_phase` ay
    ipinapakilala bilang NAG-IISANG sanctioned na writer na walang daan-likod,
    pero ang graph na kinokonsulta niya ay naisusulat ng kahit anong
    nag-i-import:

        LEGAL_TRANSITIONS["partial_filled"] = frozenset({"partial_posting"})

    at legal na ang muling pag-POST ng f laban sa isang posisyong nabawasan na
    ng f — double fire, iniulat ng state machine bilang tamang hakbang.
    """
    with pytest.raises(TypeError):
        pb.LEGAL_TRANSITIONS["partial_filled"] = frozenset({"partial_posting"})  # type: ignore[index]
    assert pb.LEGAL_TRANSITIONS["partial_filled"] == frozenset()
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("partial_filled", "partial_posting")


def test_terminal_and_naked_risk_are_disjoint():
    """ANG INVARIANT NG R4. Ang isang phase na may share na walang nakaupong
    stop ay HINDI puwedeng terminal — may utang pa itong restore o flatten.

    Sa unang bersyon ay parehong terminal AT naked ang `partial_stale_adopted`
    at `restore_rejected`, at walang laman ang kanilang transition set — kaya
    ang mismong lunas na hinihingi ng R4 ay hindi maitatala. Ang wiring na
    magtitiwala sa `is_terminal` ay titigil sa pag-service habang hubad ang
    f - k na share.
    """
    assert pb.TERMINAL_PHASES & pb.NAKED_RISK_PHASES == frozenset()


def test_every_naked_risk_phase_can_reach_a_restore_or_a_flatten():
    """ANG INVARIANT NG R4, bilang ABOT at hindi KATABI.

    Ang revision 2 ay tumingin lamang sa TUWIRANG target. Mahina iyon: ang
    `successor_certified` ay hubad (f na share, walang stop, wala pang partial)
    pero ang restore nito ay dumadaan muna sa `post_deferred`, kaya ang
    tuwirang tseke ay babagsak sa isang graph na TAMA naman. Ang tamang tanong
    ay kung ABOT ba ang remedyo.
    """
    for phase in sorted(pb.NAKED_RISK_PHASES):
        targets = pb.LEGAL_TRANSITIONS[phase]
        assert targets, f"{phase} ay naked pero walang labasan"
        reach = pb.reachable_phases(phase)
        assert reach & pb.REMEDY_PHASES, (
            f"{phase} ay naked pero walang ABOT na restore o flatten: "
            f"abot={sorted(reach)}"
        )


def test_every_terminal_reachable_from_a_naked_phase_actually_resolves_it():
    """ANG INVARIANT NG R4, PINALAKAS (ikaapat na pasada).

    Ang naunang test ay pumapasa habang may LEGAL na landas patungo sa isang
    terminal na walang remedyo — sapat na sa kanya na may ISANG landas na may
    remedyo. Iyon mismo ang naipadala ng revision 3::

        advance_phase("successor_certified", "abandoned")   -> TINANGGAP
        LEGAL_TRANSITIONS["abandoned"] == frozenset()
        reachable_phases("abandoned") & REMEDY_PHASES == set()

    106 na share na walang pababang stop, `is_in_flight` ay False kaya walang
    service step ang tatakbo pa, at walang artifact na nagsasabing may utang.
    S4 iyon, isang hop palabas. Ang tamang tanong ay hindi "may isa bang
    landas na maayos" kundi "MAAYOS BA ANG BAWAT DULO".
    """
    for phase in sorted(pb.NAKED_RISK_PHASES):
        stray = pb.reachable_terminals(phase) - pb.NAKED_RESOLVING_TERMINALS
        assert not stray, (
            f"{phase} ay hubad pero maaaring magtapos sa {sorted(stray)} — "
            f"terminal na hindi nagreresolba ng hubad na natitira"
        )


def test_abandoned_is_unreachable_from_every_naked_phase():
    """Ang partikular na hugis ng nasa itaas, pinangalanan.

    Ang `abandoned` ay para sa PRE-certification na lineage lamang: doon ay buo
    pa ang Q stop at walang hubad. Mula sa isang hubad na phase ito ay
    pag-abandona sa POSISYON, hindi sa lineage.
    """
    for phase in sorted(pb.NAKED_RISK_PHASES):
        assert "abandoned" not in pb.reachable_phases(phase), phase
    # at nananatili itong abot mula sa lineage na dapat magkaroon niyon:
    for phase in ("intent_frozen", "replace_submitted", "replace_indeterminate",
                  "replace_stuck", "containment_queued"):
        assert "abandoned" in pb.LEGAL_TRANSITIONS[phase], phase


def test_every_naked_phase_has_a_direct_flatten_edge():
    """§3.2 ng disenyo: "any NAKED phase -> flatten_queued -> flattened(T)".

    Wala ni isa sa limang phase na idinagdag ng revision 3 sa
    `NAKED_RISK_PHASES` ang may ganoong edge. Ang tanging daan palabas ng normal
    na window patungo sa flatten ay dumaraan sa `partial_stale_adopted` (na
    naggigiit ng isang stale cancel na umampon ng k < f) o sa
    `partial_rejected_final` (na naggigiit ng isang panghuling rejection) —
    dalawang resultang pang-broker na HINDI PA NANGYAYARI. Ang isang breaker
    flatten sa `partial_posted` ay walang maitatalang totoo.
    """
    for phase in sorted(pb.NAKED_RISK_PHASES):
        if phase == "flatten_queued":
            continue
        assert "flatten_queued" in pb.LEGAL_TRANSITIONS[phase], (
            f"{phase} ay hubad pero hindi maitatala ang huling remedyo nang "
            f"hindi muna nag-iimbento ng resultang pang-broker"
        )


def test_the_normal_path_b_window_is_flagged_naked_by_both_artifacts():
    """ANG DEPEKTO NG REVISION 2, na may eksaktong hugis ng S4.

    Inayos ng revision 2 ang `assess_protection` para tumigil sa pagbibilang ng
    nakaupong upside limit bilang proteksyon — pero hindi iyon naipasa sa
    `NAKED_RISK_PHASES`. Dalawang naipadalang artifact ang nagkasalungat
    tungkol sa MISMONG window na binabayaran ng PATH B: sinasabi ng checker na
    `naked_downside=106`, sinasabi ng phase set na hindi ito hubad. Ang phase
    set ang tinitingnan ng isang wiring para malaman kung may utang itong
    remedyo, kaya lalaktawan nito ang buong normal na window.

    Ang test na ito ang nag-uugnay sa dalawa: kung sasabihin ng checker na
    hubad ang isang estado, dapat nakabandera rin ang phase nito.
    """
    window = {
        # phase                    broker,  stop, bukas na partial
        "successor_certified":   (355.0, 249.0,   0.0),
        "post_deferred":         (355.0, 249.0,   0.0),
        "partial_posting":       (355.0, 249.0, 106.0),
        "partial_posted":        (355.0, 249.0, 106.0),
        "partial_indeterminate": (355.0, 249.0, 106.0),
    }
    for phase, (broker, stop, partial) in sorted(window.items()):
        verdict = pb.assess_protection(
            broker_qty=broker, stop_qty=stop, open_partial_qty=partial
        )
        assert verdict.status == "naked_downside", phase
        assert verdict.naked_downside_qty == pytest.approx(106.0), phase
        assert phase in pb.NAKED_RISK_PHASES, (
            f"{phase}: sinasabi ng checker na {verdict.naked_downside_qty} na "
            f"share ang walang pababang stop, pero hindi ito nasa "
            f"NAKED_RISK_PHASES — iyon ang set na titingnan ng wiring"
        )


def test_partial_filled_is_the_first_phase_that_is_covered_again():
    """Ang hangganan sa kabilang dulo. Matapos mapunan ang f ay Q - f = R ang
    hawak at R ang stop, kaya ang `partial_filled` ay TAPOS at HINDI hubad."""
    assert pb.assess_protection(
        broker_qty=249.0, stop_qty=249.0, open_partial_qty=0.0
    ).ok is True
    assert "partial_filled" not in pb.NAKED_RISK_PHASES
    assert "partial_filled" in pb.TERMINAL_PHASES


def test_whole_exit_blocking_and_naked_risk_are_disjoint():
    """WALANG phase ang puwedeng sabay na (a) mag-ulat ng hubad na natitira at
    (b) humarang sa flatten na sumasakop niyon. Iyon ang L2 deadlock."""
    assert pb.WHOLE_EXIT_BLOCKING_PHASES & pb.NAKED_RISK_PHASES == frozenset()


def test_whole_exit_blocking_phases_are_only_the_owner_ambiguous_three():
    assert pb.WHOLE_EXIT_BLOCKING_PHASES == frozenset({
        "intent_frozen", "replace_submitted", "replace_indeterminate",
    })


def test_terminal_phases_have_no_outgoing_transitions():
    for phase in sorted(pb.TERMINAL_PHASES):
        assert pb.LEGAL_TRANSITIONS[phase] == frozenset()


def test_consumed_by_exit_is_reachable_from_every_unblocked_open_phase():
    """Kapag hindi hinaharang ang whole exit, MAAARING lamunin ng exit ang
    posisyon habang bukas pa ang marker — kaya kailangang maitala iyon.

    Sa unang bersyon ay `successor_certified` lamang ang may landas patungo
    sa `consumed_by_exit`, kaya ang pinakamalamang na tunay na karera —
    isang buong exit habang nakaupo ang f na limit — ay nag-iiwan ng
    marker na stranded at bukas (L5).
    """
    for phase in sorted(pb.PHASES):
        if phase in pb.TERMINAL_PHASES or phase in pb.WHOLE_EXIT_BLOCKING_PHASES:
            continue
        if phase in pb.EXIT_CONSUMPTION_UNRECORDABLE_PHASES:
            continue
        assert "consumed_by_exit" in pb.LEGAL_TRANSITIONS[phase], phase


def test_the_two_exit_consumption_exceptions_are_named_and_still_serviced():
    """Ang exception sa nauunang panuntunan ay dapat PINANGALANAN, hindi isang
    tahimik na `continue` sa isang test.

    Ang naipadalang test ng revision 2 ay naglaktaw ng `replace_stuck` at
    `containment_queued` sa loob ng test body, samantalang iginigiit ng
    docstring ng module at ng §3.2 ng disenyo na UNIBERSAL ang panuntunan. Ang
    isang panuntunang may hindi naitalang exception ay hindi panuntunan.

    Ang dahilan ng exception ay pangkaligtasan: sa dalawang phase na iyon ay
    may nakaupong broker order na hindi natin alam kung kanino. Ang
    `consumed_by_exit` ay TERMINAL, kaya ang pagpayag nito ay titigil sa
    pag-service habang may hindi kilalang order sa broker — ang hugis ng
    ghost/zombie na order. Kaya may sariling invariant ang exception: ang mga
    phase na ito ay dapat manatiling may service step, at may tahasang labasan
    ng operator.
    """
    assert pb.EXIT_CONSUMPTION_UNRECORDABLE_PHASES == frozenset({
        "replace_stuck", "containment_queued",
    })
    for phase in sorted(pb.EXIT_CONSUMPTION_UNRECORDABLE_PHASES):
        assert phase not in pb.TERMINAL_PHASES, phase
        assert pb.LEGAL_TRANSITIONS[phase], f"{phase} ay patay na kalsada"
        assert "abandoned" in pb.LEGAL_TRANSITIONS[phase], phase
        # at hindi sila hubad, kaya walang remedyo silang inuutang
        assert phase not in pb.NAKED_RISK_PHASES, phase


def test_no_open_phase_is_a_dead_end():
    """Walang non-terminal na phase ang puwedeng walang labasan — iyon ay
    tahimik na wedge, at ang wedge ang buong aral ng L2."""
    for phase in sorted(pb.PHASES):
        if phase in pb.TERMINAL_PHASES:
            continue
        assert pb.LEGAL_TRANSITIONS[phase], f"{phase} ay bukas pero walang labasan"


def test_every_open_phase_can_reach_some_terminal():
    """At bawat bukas na phase ay may abot na katapusan; walang cycle na
    walang labasan."""
    for phase in sorted(pb.PHASES):
        if phase in pb.TERMINAL_PHASES:
            continue
        assert pb.reachable_phases(phase) & pb.TERMINAL_PHASES, phase


def test_certified_cannot_escalate_to_replace_stuck():
    """S5. Pagkatapos ng certification ay ALAM na nakaupo ang R stop. Ang
    lumilipas na read miss (o isang legal na pyramid add) ay hindi puwedeng
    mag-escalate patungo sa containment na nagfa-flatten ng runner."""
    assert "replace_stuck" not in pb.LEGAL_TRANSITIONS["successor_certified"]
    assert "post_deferred" in pb.LEGAL_TRANSITIONS["successor_certified"]
    assert pb.LEGAL_TRANSITIONS["post_deferred"] >= frozenset({
        "partial_posting", "restore_intent_frozen",
    })


def test_stuck_replace_has_an_operator_abandon_route():
    assert "abandoned" in pb.LEGAL_TRANSITIONS["replace_stuck"]


def test_advance_phase_accepts_the_happy_path_and_raises_on_anything_else():
    assert pb.advance_phase("intent_frozen", "replace_submitted") == "replace_submitted"
    assert pb.advance_phase("replace_submitted", "successor_certified") == "successor_certified"
    assert pb.advance_phase("successor_certified", "partial_posting") == "partial_posting"
    assert pb.advance_phase("partial_posting", "partial_posted") == "partial_posted"
    assert pb.advance_phase("partial_posted", "partial_filled") == "partial_filled"
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("partial_filled", "partial_posting")
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("intent_frozen", "partial_posted")
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("intent_frozen", "not_a_phase")
    with pytest.raises(pb.PhaseError):
        pb.advance_phase("", "intent_frozen")


def test_the_r4_restore_is_reachable_from_both_naked_terminals_of_v1():
    """Ang dalawang phase na ipinagbawal ng unang bersyon ay dumadaloy na."""
    assert pb.advance_phase("partial_stale_adopted", "restore_intent_frozen")
    assert pb.advance_phase("restore_rejected", "restore_intent_frozen")
    assert pb.advance_phase("restore_rejected", "flatten_queued")
    assert pb.advance_phase("flatten_queued", "flattened")


def test_is_in_flight_is_simply_not_terminal_and_is_not_the_block():
    for phase in sorted(pb.PHASES):
        assert pb.is_in_flight(phase) is (phase not in pb.TERMINAL_PHASES)
    # at hindi ito ang predicate ng chokepoint:
    assert pb.is_in_flight("replace_stuck") is True
    assert pb.blocks_whole_exit("replace_stuck", age_seconds=1.0) is False


# ==========================================================================
# 2. blocks_whole_exit — may orasan, at may override
# ==========================================================================

def test_the_three_blocking_phases_block_inside_the_ceiling():
    for phase in sorted(pb.WHOLE_EXIT_BLOCKING_PHASES):
        assert pb.blocks_whole_exit(phase, age_seconds=0.0) is True
        assert pb.blocks_whole_exit(phase, age_seconds=29.0) is True


def test_the_block_expires_at_the_ceiling():
    """L2/S7: ang harang ay paghingi ng isang pulse, hindi isang lock."""
    assert pb.blocks_whole_exit("replace_submitted", age_seconds=31.0) is False
    assert pb.blocks_whole_exit(
        "replace_submitted", age_seconds=5.0, ceiling_seconds=1.0
    ) is False


def test_operator_and_breaker_authority_are_never_deferred():
    for phase in sorted(pb.WHOLE_EXIT_BLOCKING_PHASES):
        assert pb.blocks_whole_exit(
            phase, age_seconds=0.0, override_authority=True
        ) is False


def test_an_unknown_or_insane_age_never_wedges_an_exit():
    assert pb.blocks_whole_exit("replace_submitted", age_seconds=None) is False
    assert pb.blocks_whole_exit("replace_submitted", age_seconds=float("nan")) is False
    assert pb.blocks_whole_exit("replace_submitted", age_seconds=-1.0) is False
    assert pb.blocks_whole_exit("replace_submitted", age_seconds="x") is False


def test_the_resolution_phases_never_block_the_exit_that_resolves_them():
    """L2. Ang containment close at ang `_queue_full_close` ng R4 ay parehong
    dumadaan sa `_release_deadman_at_literal_submit`. Kung haharangin sila ng
    phase ay walang makakapag-usad ng phase."""
    for phase in (
        "replace_stuck",
        "containment_queued",
        "restore_intent_frozen",
        "restore_replace_submitted",
        "restore_indeterminate",
        "restore_rejected",
        "partial_stale_adopted",
        "flatten_queued",
    ):
        assert pb.blocks_whole_exit(phase, age_seconds=0.0) is False


def test_marker_ceiling():
    assert pb.marker_ceiling_exceeded(None) is False
    assert pb.marker_ceiling_exceeded(10.0) is False
    assert pb.marker_ceiling_exceeded(301.0) is True
    assert pb.marker_ceiling_exceeded(2.0, ceiling_seconds=1.0) is True


def test_the_ceiling_can_actually_be_executed_from_every_open_phase():
    """ANG ANTI-WEDGE NA CEILING AY DAPAT MAISASAGAWA (ikaapat na pasada).

    Ang revision 3 ay nag-utos ng "sapilitan ang `abandoned`" sa tatlong lugar,
    pero ang `abandoned` ay walang papasok na edge mula sa 11 sa 13 na naked
    phase — kasama ang `partial_posted`, ang mismong phase na gumagawa ng
    mahahabang marker (ang limit na nakaupo lang at hindi kailanman na-trade:
    ang kaso ng CANF). Ang wiring na susunod sa naipadalang utos ay
    makakakuha ng `PhaseError` sa LOOB ng service step, at ayon sa D4/D3 ang
    exception doon ay "isang tunay na patay na stop na iniulat bilang
    protected". Ang test na ito ang nag-uugnay sa utos at sa graph.
    """
    for phase in sorted(pb.PHASES):
        if phase in pb.TERMINAL_PHASES:
            assert pb.marker_ceiling_forced_target(phase) is None, phase
            continue
        target = pb.marker_ceiling_forced_target(phase)
        if target is None:
            # nasa remedyo na mismo — walang isusulong
            assert phase == "flatten_queued", phase
            continue
        # HINDI pangako: dapat tanggapin ito ng nag-iisang sanctioned na writer.
        assert pb.advance_phase(phase, target) == target


def test_the_ceiling_target_never_abandons_a_naked_position():
    """Dalawang magkaibang bagay ang tinatawag na "abandon", at ang paghalo sa
    kanila ang nag-recreate ng S4."""
    for phase in sorted(pb.NAKED_RISK_PHASES):
        target = pb.marker_ceiling_forced_target(phase)
        assert target in (None, "flatten_queued"), (phase, target)
    for phase in sorted(pb.WHOLE_EXIT_BLOCKING_PHASES | pb.EXIT_CONSUMPTION_UNRECORDABLE_PHASES):
        assert pb.marker_ceiling_forced_target(phase) == "abandoned", phase


# ==========================================================================
# 3. Ang sibling-reconcile seam (S2)
# ==========================================================================

def test_the_phases_with_a_live_sibling_demand_a_reconcile_before_the_clamp():
    """S2. Sa `partial_posting` / `partial_indeterminate` ang cid ng sibling ay
    alam LAMANG ng claim. Ang `_cancel_scale_limit_and_clamp` ay
    `if not oid: return requested_qty` (LR:18968-18970) — tahimik na
    pass-through. Kung hindi muna itatayo ang `le` mula sa claim, ang whole
    exit ay magpapadala ng Q habang may f na nakaupo: short flip."""
    assert pb.requires_sibling_reconcile("partial_posting") is True
    assert pb.requires_sibling_reconcile("partial_posted") is True
    assert pb.requires_sibling_reconcile("partial_indeterminate") is True
    assert pb.requires_sibling_reconcile("intent_frozen") is False
    assert pb.requires_sibling_reconcile("partial_filled") is False


def test_no_sibling_live_phase_is_blocked_so_the_reconcile_is_mandatory():
    """Kinuha natin ang ALTERNATIBO ng amendment 5: hindi hinaharang ang exit
    sa mga phase na ito, kaya ang reconcile sa ulo ng chokepoint ang NAG-IISANG
    bagay na pumipigil sa oversell. Ang test na ito ang nag-uugnay sa dalawa:
    kapag may nagdagdag ng harang o nag-alis ng reconcile ay babagsak ito."""
    for phase in sorted(pb.SIBLING_LIVE_PHASES):
        assert pb.blocks_whole_exit(phase, age_seconds=0.0) is False
        assert pb.requires_sibling_reconcile(phase) is True


def test_the_reconcile_debt_survives_two_legal_steps_out_of_the_live_set():
    """S2, SA PAMAMAGITAN NG PAGLALAKAD PALABAS NG SET (ikaapat na pasada).

    Ang `SIBLING_LIVE_PHASES` ay per-phase, hindi per-path, at ang utang ay
    natatapos sa paglabas nito. Dalawang legal na hakbang::

        partial_indeterminate   (liveness HINDI ALAM — iyon ang kahulugan)
          -> partial_rejected_final    (hindi na live-set; walang utang)
          -> flatten_queued            (BUONG flatten; walang utang)

    Ang POST ay nag-timeout pero dumapo sa broker; ang `le` ay wala pa ring
    `scale_limit_order_id`, kaya ang `_cancel_scale_limit_and_clamp` ay noop
    (LR:18968-18970) at ang flatten ay magpapadala ng buong Q habang nakaupo ang
    106: short flip — sa landas na inaakala ng disenyo na ligtas.
    """
    for phase in ("partial_rejected_final", "flatten_queued",
                  "restore_intent_frozen", "restore_replace_submitted",
                  "restore_indeterminate", "restore_rejected"):
        assert pb.requires_sibling_reconcile(phase) is True, phase


def test_the_reconcile_debt_stops_only_at_a_phase_that_proves_the_sibling_dead():
    """At hindi ito walang katapusan: may tatlong phase na TUNAY na
    nagpapatunay na wala nang nakaupo, at doon ito humihinto."""
    assert pb.SIBLING_RESOLVED_PHASES == frozenset({
        "partial_filled", "partial_rejected", "partial_stale_adopted",
    })
    for phase in sorted(pb.SIBLING_RESOLVED_PHASES):
        assert pb.requires_sibling_reconcile(phase) is False, phase
    # ...at ang mga phase bago pa mailagay ang partial ay malinis din.
    for phase in ("intent_frozen", "replace_submitted", "successor_certified",
                  "post_deferred", "replace_stuck", "containment_queued"):
        assert pb.requires_sibling_reconcile(phase) is False, phase


def test_the_derived_reconcile_set_is_pinned_so_a_graph_edit_cannot_shift_it():
    """Hinango ang set mula sa graph para hindi ito mag-drift — at naka-pin
    dito para ang isang pagbabago sa graph ay lumitaw bilang bumagsak na test at
    hindi bilang tahimik na paglipat ng kahulugan."""
    assert pb.SIBLING_RECONCILE_PHASES == frozenset({
        "partial_posting", "partial_posted", "partial_indeterminate",
        "partial_rejected_final", "flatten_queued",
        "restore_intent_frozen", "restore_replace_submitted",
        "restore_indeterminate", "restore_rejected",
    })
    assert pb.SIBLING_LIVE_PHASES <= pb.SIBLING_RECONCILE_PHASES
    # walang terminal: doon ay walang service step na maiuutang
    assert pb.SIBLING_RECONCILE_PHASES & pb.TERMINAL_PHASES == frozenset()


def test_consumed_by_exit_cannot_be_recorded_while_the_reconcile_is_unpaid():
    """Ang `consumed_by_exit` ay TERMINAL, kaya ang pagsulat nito habang may
    hindi naresolbang sibling ay hindi paglilinis ng utang kundi PAGTATAPON
    nito: `is_in_flight` ay False, at ang naulilang 106-share na limit ay hindi
    na mase-service ng kahit anong pass.

    Hindi kayang tanungin ito ng `advance_phase` — walang argumento ang isang
    graph edge — kaya ito ang lugar kung saan pinapatunayan ng wiring na
    naibayad ang utang.
    """
    for phase in sorted(pb.SIBLING_RECONCILE_PHASES):
        assert pb.exit_consumption_precondition(
            phase, sibling_reconciled=False
        ) is False, phase
        assert pb.exit_consumption_precondition(
            phase, sibling_reconciled=True
        ) is True, phase


def test_the_exit_consumption_precondition_still_refuses_the_two_exceptions():
    for phase in sorted(pb.EXIT_CONSUMPTION_UNRECORDABLE_PHASES):
        assert pb.exit_consumption_precondition(
            phase, sibling_reconciled=True
        ) is False, phase
    # at pumapayag ito kung saan walang sibling na maiuutang
    assert pb.exit_consumption_precondition(
        "successor_certified", sibling_reconciled=False
    ) is True
    # at hindi sa terminal
    assert pb.exit_consumption_precondition(
        "partial_filled", sibling_reconciled=True
    ) is False


# ==========================================================================
# 4. Ang hati
# ==========================================================================

def test_the_canf_split():
    plan = pb.plan_replacement_edge(
        total_qty=355.0, partial_qty=106.0, predecessor_filled_size=0.0
    )
    assert plan.ok is True
    assert plan.successor_qty == 249.0


def test_a_partially_filled_predecessor_is_refused():
    """Amendment 10. Ang `partially_filled` ay nasa
    `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES`, kaya "buhay" ang tingin dito ng
    maintenance; ang PATCH doon ay muling nag-aawtorisa ng naibentang share."""
    plan = pb.plan_replacement_edge(
        total_qty=355.0, partial_qty=106.0, predecessor_filled_size=40.0
    )
    assert plan.ok is False
    assert plan.reason == "predecessor_partially_filled"


@pytest.mark.parametrize("total,partial,reason", [
    (0.0, 10.0, "non_positive_total"),
    (100.0, 0.0, "non_positive_partial"),
    (100.0, -5.0, "non_positive_partial"),
    (100.0, 100.0, "partial_leaves_no_runner"),
    (100.0, 150.0, "partial_leaves_no_runner"),
    (float("nan"), 10.0, "non_finite_quantity"),
    (100.5, 10.0, "fractional_total_quantity"),
    (100.0, 10.5, "fractional_partial_quantity"),
])
def test_the_split_refuses_bad_geometry(total, partial, reason):
    plan = pb.plan_replacement_edge(
        total_qty=total, partial_qty=partial, predecessor_filled_size=0.0
    )
    assert plan.ok is False
    assert plan.reason == reason


def test_fractional_legs_are_allowed_when_whole_shares_is_off():
    plan = pb.plan_replacement_edge(
        total_qty=1.5, partial_qty=0.5, predecessor_filled_size=0.0,
        whole_shares=False,
    )
    assert plan.ok is True
    assert plan.successor_qty == 1.0


def test_the_split_refuses_a_second_partial_against_resting_sell_authority():
    """DOUBLE FIRE (ikaapat na pasada). Ang planner ay tumitingin lamang sa fill
    ng PREDECESSOR at hindi sa kung ano pang SELL ang nakaupo.

    Isang unang partial na f0 = 50 ang nakaupo at hindi pa napupunan; pumutok
    ang pangalawang scale-out. Ang predecessor stop ay tunay ngang walang fill,
    kaya bawat tseke ng revision 3 ay pumapasa at `ok=True` ang isinasagot nito
    para sa R = 249 — pero ang resulta ay 50 + 106 = 156 na nakaupong sell
    KASAMA ang 249 na stop laban sa 355 na share. Ang `assess_protection`
    lamang ang nakakakita niyon, at walang nag-uugnay sa planner dito.
    """
    plan = pb.plan_replacement_edge(
        total_qty=355.0, partial_qty=106.0,
        predecessor_filled_size=0.0, open_partial_qty=50.0,
    )
    assert plan.ok is False
    assert plan.reason == "oversell_after_split"
    # at ang checker ay sumasang-ayon sa estadong iyon:
    assert pb.assess_protection(
        broker_qty=355.0, stop_qty=249.0, open_partial_qty=156.0
    ).status == "oversell_risk"


@pytest.mark.parametrize("kwargs,reason", [
    ({"total_qty": None, "partial_qty": 106.0, "predecessor_filled_size": 0.0},
     "quantity_unreadable"),
    ({"total_qty": 355.0, "partial_qty": "x", "predecessor_filled_size": 0.0},
     "quantity_unreadable"),
    ({"total_qty": 355.0, "partial_qty": 106.0, "predecessor_filled_size": None},
     "predecessor_fill_unreadable"),
    ({"total_qty": 355.0, "partial_qty": 106.0, "predecessor_filled_size": 0.0,
      "open_partial_qty": None}, "open_partial_unreadable"),
])
def test_the_split_refuses_unreadable_input_instead_of_raising(kwargs, reason):
    """Dalawa sa tatlong quantity na parameter ay na-coerce sa LABAS ng
    try/except ng revision 3, kaya `plan_replacement_edge(total_qty=None, ...)`
    ay nagre-raise habang ang `predecessor_filled_size=None` ay malinis na
    tumatanggi. Ang function mismo ang nagpapakita na ang hindi mabasang input
    ay isang DAHILAN, hindi isang exception."""
    plan = pb.plan_replacement_edge(**kwargs)
    assert plan.ok is False
    assert plan.reason == reason


# ==========================================================================
# 4b. Ang restore edge (edge 2) — R0-10 sa DALAWANG edge
# ==========================================================================

def test_the_restore_edge_plans_every_geometry_the_design_names():
    # (a) `partial_rejected_final`: k = 0, walang bukas na partial, buo ang Q
    assert pb.plan_restore_edge(
        broker_qty=355.0, open_partial_qty=0.0,
        predecessor_qty=249.0, predecessor_filled_size=0.0,
    ).successor_qty == 355.0
    # (b) `partial_stale_adopted`: k = 40 ang naampon, kaya 315 ang hawak
    assert pb.plan_restore_edge(
        broker_qty=315.0, open_partial_qty=0.0,
        predecessor_qty=249.0, predecessor_filled_size=0.0,
    ).successor_qty == 315.0
    # (c) §3.4c pyramid add a = 50 habang nakaupo pa ang f = 106
    assert pb.plan_restore_edge(
        broker_qty=405.0, open_partial_qty=106.0,
        predecessor_qty=249.0, predecessor_filled_size=0.0,
    ).successor_qty == 299.0


def test_the_restore_edge_enforces_r0_10_which_edge_1_cannot_reach():
    """R0-10 ("ang isang `partially_filled` na predecessor ay hindi kailanman
    hinahati") ay walang kondisyon sa §3.4b, pero ang tanging nagpapatupad niyon
    ay ang `plan_replacement_edge` — na HINDI KAYANG tawagin sa restore edge:
    ang sizing doon ay may f = 0, na `non_positive_partial` agad.

    Konkreto: pumutok ang R = 249 na stop at napunan nang j = 80. Ang
    `partially_filled` ay "buhay" pa rin sa lifecycle gate (LR:9200-9207), kaya
    ang restore ay magpi-PATCH ng MISMONG order na iyon pataas — muling
    inaawtorisa ang 80 na naibenta na.
    """
    assert pb.plan_replacement_edge(
        total_qty=355.0, partial_qty=0.0, predecessor_filled_size=0.0
    ).reason == "non_positive_partial"
    plan = pb.plan_restore_edge(
        broker_qty=355.0, open_partial_qty=0.0,
        predecessor_qty=249.0, predecessor_filled_size=80.0,
    )
    assert plan.ok is False
    assert plan.reason == "predecessor_partially_filled"


@pytest.mark.parametrize("kwargs,reason", [
    # ang takip ay sumasaklaw na sa buong hindi-partial na natitira
    ({"broker_qty": 315.0, "open_partial_qty": 66.0, "predecessor_qty": 249.0,
      "predecessor_filled_size": 0.0}, "restore_is_not_growing"),
    ({"broker_qty": 106.0, "open_partial_qty": 106.0, "predecessor_qty": 0.0,
      "predecessor_filled_size": 0.0}, "restore_leaves_nothing"),
    ({"broker_qty": None, "open_partial_qty": 0.0, "predecessor_qty": 249.0,
      "predecessor_filled_size": 0.0}, "quantity_unreadable"),
    ({"broker_qty": 0.0, "open_partial_qty": 0.0, "predecessor_qty": 249.0,
      "predecessor_filled_size": 0.0}, "non_positive_broker_qty"),
])
def test_the_restore_edge_refuses_bad_geometry(kwargs, reason):
    plan = pb.plan_restore_edge(**kwargs)
    assert plan.ok is False
    assert plan.reason == reason


# ==========================================================================
# 5. Ang envelope ng successor (S1)
# ==========================================================================

_PREDECESSOR_REQUEST = {
    "product_id": "CANF",
    "side": "sell",
    "order_type": "stop",
    "base_size": 355.0,
    "stop_price": 3.91,
    "time_in_force": "gtc",
    "position_intent": "sell_to_close",
    "extended_hours": False,
    "client_order_id": "chili-deadman-canf-gen7",
}


def test_the_marker_envelope_keeps_every_field_but_cid_and_size():
    env = pb.marker_successor_envelope(
        predecessor_order_request=_PREDECESSOR_REQUEST,
        successor_client_order_id="chili-deadman-canf-gen8",
        successor_qty=249.0,
    )
    assert env is not None
    assert env["base_size"] == 249.0
    assert env["client_order_id"] == "chili-deadman-canf-gen8"
    for key in (
        "product_id", "side", "order_type", "stop_price",
        "time_in_force", "position_intent", "extended_hours",
    ):
        assert env[key] == _PREDECESSOR_REQUEST[key]
    # hindi nababago ang input
    assert _PREDECESSOR_REQUEST["base_size"] == 355.0


@pytest.mark.parametrize("kwargs", [
    {"successor_client_order_id": "", "successor_qty": 249.0},
    {"successor_client_order_id": "x", "successor_qty": 0.0},
    {"successor_client_order_id": "x", "successor_qty": -1.0},
    {"successor_client_order_id": "x", "successor_qty": 355.0},   # walang liit
    {"successor_client_order_id": "x", "successor_qty": 400.0},   # lumalaki
    {"successor_client_order_id": "x", "successor_qty": float("inf")},
    {"successor_client_order_id": "x", "successor_qty": None},
    {"successor_client_order_id": "x", "successor_qty": 249.0,
     "edge_kind": "wat"},
])
def test_the_marker_envelope_refuses_non_shrinking_or_invalid_edges(kwargs):
    assert pb.marker_successor_envelope(
        predecessor_order_request=_PREDECESSOR_REQUEST, **kwargs
    ) is None


def test_the_restore_edge_can_be_lineage_certified_because_it_grows():
    """ANG REMEDYO NG R4 AY KAILANGANG MA-CERTIFY (ikaapat na pasada).

    Ang envelope ng revision 3 ay tumatanggi sa BAWAT hindi-lumiliit na edge
    (`if qty >= predecessor_qty - 1e-9: return None`), samantalang ang tanging
    dalawang remedyo sa isang hubad na natitira ay PAREHONG lumalaki: ang R4
    restore (`broker_qty - open_partial_qty`, na Q laban sa isang predecessor na
    R = Q - f) at ang §3.4c pyramid re-cover. Walang envelope para sa kanila,
    kaya babalik ang wiring sa predecessor-copy at babagsak ang
    `_owner_transport_order_matches` sa `|355 - 249| > tol` sa bawat pulse:
    `replacement_deadman_successor_lineage_unproven` magpakailanman. Ang marker
    ay uupo sa `restore_replace_submitted` — HUBAD — hanggang mag-expire ang
    lease, saka `flatten_queued`. "Palaging i-flatten ang runner" ang naging
    remedyo — ang mismong resultang sinasabi ng §3.9 na binura.
    """
    env = pb.marker_successor_envelope(
        predecessor_order_request={**_PREDECESSOR_REQUEST, "base_size": 249.0},
        successor_client_order_id="chili-deadman-canf-gen9",
        successor_qty=355.0,
        edge_kind="restore",
    )
    assert env is not None
    assert env["base_size"] == 355.0
    assert env["stop_price"] == _PREDECESSOR_REQUEST["stop_price"]


def test_edge_kind_is_a_naming_not_a_loosening():
    """Ang bawat uri ay tumatanggi sa MALING DIREKSYON at sa pagkakapantay.
    Kung hindi, ang parameter ay isang blangkong tseke sa halip na pangalan."""
    pred = {**_PREDECESSOR_REQUEST, "base_size": 249.0}
    # ang restore ay hindi tumatanggap ng pagliit
    assert pb.marker_successor_envelope(
        predecessor_order_request=pred, successor_client_order_id="x",
        successor_qty=100.0, edge_kind="restore",
    ) is None
    # at hindi ng pagkakapantay
    assert pb.marker_successor_envelope(
        predecessor_order_request=pred, successor_client_order_id="x",
        successor_qty=249.0, edge_kind="restore",
    ) is None
    # ang shrink ay hindi tumatanggap ng paglaki (ang default pa rin)
    assert pb.marker_successor_envelope(
        predecessor_order_request=pred, successor_client_order_id="x",
        successor_qty=355.0,
    ) is None


def test_the_marker_envelope_refuses_a_request_without_a_size():
    assert pb.marker_successor_envelope(
        predecessor_order_request={"product_id": "CANF"},
        successor_client_order_id="x",
        successor_qty=249.0,
    ) is None
    assert pb.marker_successor_envelope(
        predecessor_order_request=None,  # type: ignore[arg-type]
        successor_client_order_id="x",
        successor_qty=249.0,
    ) is None


# ==========================================================================
# 6. assess_protection — DALAWANG tanong, hindi isa
# ==========================================================================

def test_the_intended_steady_state_is_covered_for_oversell_but_naked_downside():
    """ITO ANG PINAKAMAHALAGANG TEST SA FILE.

    Ang canonical na window ng PATH B — broker 355, stop 249, partial 106 —
    ay iginiit ng UNANG bersyon bilang `covered`, `ok=True`. MALI iyon: ang
    106 ay may sell LIMIT sa 4.63, sa TAAS ng merkado. Sa isang gap-down ay
    pumuputok ang 249 na stop habang ang 106 ay bumabagsak laban sa isang
    limit na hindi na maaabot. Ang bilang na kailangang tanggapin ng operator
    nang nakasulat ay ito: 106 na share ang walang pababang stop sa BUONG
    buhay ng partial — hindi lamang sa mga failure branch.
    """
    v = pb.assess_protection(broker_qty=355.0, stop_qty=249.0, open_partial_qty=106.0)
    assert v.oversell_ok is True
    assert v.status == "naked_downside"
    assert v.naked_downside_qty == pytest.approx(106.0)
    assert v.unhedged_qty_with_resting_sell == pytest.approx(106.0)
    assert v.ok is False
    assert v.requires_restore_or_flatten is True


def test_a_whole_position_stop_is_the_only_covered_state():
    v = pb.assess_protection(broker_qty=355.0, stop_qty=355.0, open_partial_qty=0.0)
    assert v.status == "covered"
    assert v.ok is True
    assert v.naked_downside_qty == 0.0
    assert v.requires_restore_or_flatten is False


def test_the_post_partial_steady_state_is_covered_again():
    """Matapos mapunan ang f, ang R stop ay sumasakop sa BUONG natitira."""
    v = pb.assess_protection(broker_qty=249.0, stop_qty=249.0, open_partial_qty=0.0)
    assert v.status == "covered"
    assert v.ok is True


def test_the_r4_failure_branch_is_naked_with_no_resting_sell_at_all():
    """Stale cancel na may k < f: 355 - 40 = 315 na hawak, 249 na stop,
    walang nakaupong partial. 66 na share na walang ANUMANG sell."""
    v = pb.assess_protection(broker_qty=315.0, stop_qty=249.0, open_partial_qty=0.0)
    assert v.status == "naked_downside"
    assert v.naked_downside_qty == pytest.approx(66.0)
    assert v.unhedged_qty_with_resting_sell == 0.0
    assert v.requires_restore_or_flatten is True


def test_oversell_is_caught_even_when_the_downside_looks_fine():
    """Ang lumang stop (355) na hindi napalitan kasama ng bagong partial (106)
    laban sa 355 na share = 461 na sell authority."""
    v = pb.assess_protection(broker_qty=355.0, stop_qty=355.0, open_partial_qty=106.0)
    assert v.oversell_ok is False
    assert v.status == "oversell_risk"
    assert v.ok is False
    # hindi ito "restore" na problema — flatten ang tanging tamang sagot
    assert v.requires_restore_or_flatten is False


def test_flat_is_flat_only_when_nothing_rests():
    assert pb.assess_protection(
        broker_qty=0.0, stop_qty=0.0, open_partial_qty=0.0
    ).status == "flat"
    assert pb.assess_protection(
        broker_qty=0.0, stop_qty=0.0, open_partial_qty=106.0
    ).status == "oversell_risk"


@pytest.mark.parametrize("kwargs", [
    {"broker_qty": float("nan"), "stop_qty": 1.0, "open_partial_qty": 0.0},
    {"broker_qty": 1.0, "stop_qty": float("inf"), "open_partial_qty": 0.0},
    {"broker_qty": -1.0, "stop_qty": 0.0, "open_partial_qty": 0.0},
    {"broker_qty": 1.0, "stop_qty": -1.0, "open_partial_qty": 0.0},
    # IKAAPAT NA PASADA: ang tatlong ito ay NAGRE-RAISE noon, bago pa marating
    # ang sariling `invalid` na hatol ng function. Ang `broker_qty=None` ay
    # tunay na estado dito (`broker_recon_status` NULL / `history_unavailable`,
    # ang 09-02 na loss-guard landmine), at ang exception ay tumatakas sa
    # naked-risk gate sa halip na maging isang hatol na kayang basahin.
    {"broker_qty": None, "stop_qty": 249.0, "open_partial_qty": 106.0},
    {"broker_qty": "x", "stop_qty": 249.0, "open_partial_qty": 106.0},
    {"broker_qty": 355.0, "stop_qty": None, "open_partial_qty": 106.0},
    {"broker_qty": 355.0, "stop_qty": 249.0, "open_partial_qty": None},
])
def test_nonsense_inputs_are_invalid_never_ok(kwargs):
    v = pb.assess_protection(**kwargs)
    assert v.status == "invalid"
    assert v.ok is False
    # at ang hindi mabasa ay UTANG, hindi malinis:
    assert v.requires_remedy is True


def test_the_helpers_on_the_protection_path_never_raise():
    """Ang patakaran ng `blocks_whole_exit` at ng `marker_ceiling_exceeded`,
    ipinapatupad na sa buong pampublikong ibabaw."""
    assert pb.blocks_whole_exit("replace_submitted", age_seconds="x") is False
    assert pb.marker_ceiling_exceeded("x") is False
    assert pb.conservation_holds(
        broker_qty=None, successor_qty=249.0,
        partial_qty=106.0, partial_cum_filled=0.0,
    ) is False
    assert pb.open_partial_qty_from_marker(
        partial_quantity=None, partial_cum_filled=0.0
    ) is None


def test_requires_remedy_is_true_whenever_anything_is_owed():
    """ANG NAG-IISANG PREDICATE NA DAPAT I-GATE NG WIRING.

    Ang `requires_restore_or_flatten` ay nagtatapos sa `and self.oversell_ok`,
    kaya nagbabalik ito ng **False** sa isang estado na may 66 na hubad na share
    basta't may oversell ding nangyayari — ang PINAKAMASAMA sa dalawa. Ang
    pangalan ay nangangako ng "restore O flatten" at flatten ang sagot doon.
    """
    over = pb.assess_protection(
        broker_qty=315.0, stop_qty=249.0, open_partial_qty=106.0
    )
    assert over.status == "oversell_risk"
    assert over.naked_downside_qty == pytest.approx(66.0)
    assert over.requires_restore_or_flatten is False   # nakasulat na kahulugan
    assert over.requires_flatten_now is True
    assert over.requires_remedy is True                # ...pero may utang
    # at ang tanging estado na walang utang ay ang tunay na ligtas:
    for kw in (
        {"broker_qty": 355.0, "stop_qty": 355.0, "open_partial_qty": 0.0},
        {"broker_qty": 0.0, "stop_qty": 0.0, "open_partial_qty": 0.0},
    ):
        assert pb.assess_protection(**kw).requires_remedy is False


def test_the_marker_schema_needs_netting_before_the_checker_can_be_trusted():
    """§3.1 ay nagtatago ng `partial_quantity: f` at `partial_cum_filled: k` —
    walang open/leaves. Ang pinakamalapit na field ay `f`, at ang pagpapakain
    niyon matapos mapunan ang k ay nagbubunga ng TAHIMIK na maling sagot sa
    magkabilang panig: isang huwad na short-flip na alarma, AT
    `requires_restore_or_flatten = False` habang 66 ang hubad.

    Ito ang kaparehong depekto ng sobrang pagbabawas na inilalarawan ng §3.7/5
    para sa `le["scale_limit_qty"]`, naulit sa schema ng marker.
    """
    f, k = 106.0, 40.0
    open_qty = pb.open_partial_qty_from_marker(
        partial_quantity=f, partial_cum_filled=k
    )
    assert open_qty == pytest.approx(66.0)

    wrong = pb.assess_protection(broker_qty=315.0, stop_qty=249.0, open_partial_qty=f)
    right = pb.assess_protection(
        broker_qty=315.0, stop_qty=249.0, open_partial_qty=open_qty
    )
    assert wrong.status == "oversell_risk"
    assert wrong.requires_restore_or_flatten is False
    assert right.status == "naked_downside"
    assert right.naked_downside_qty == pytest.approx(66.0)
    assert right.requires_restore_or_flatten is True
    # at ang konserbasyon ay humahawak sa parehong estado, mula sa f at k:
    assert pb.conservation_holds(
        broker_qty=315.0, successor_qty=249.0,
        partial_qty=f, partial_cum_filled=k,
    ) is True


def test_open_partial_qty_from_marker_refuses_impossible_bookkeeping():
    assert pb.open_partial_qty_from_marker(
        partial_quantity=106.0, partial_cum_filled=200.0
    ) is None
    assert pb.open_partial_qty_from_marker(
        partial_quantity=-1.0, partial_cum_filled=0.0
    ) is None
    assert pb.open_partial_qty_from_marker(
        partial_quantity=106.0, partial_cum_filled=106.0
    ) == 0.0


# ==========================================================================
# 7. conservation_holds
# ==========================================================================

def test_conservation_across_the_whole_partial_lifecycle():
    # bago ang POST: Q == R + f
    assert pb.conservation_holds(
        broker_qty=355.0, successor_qty=249.0,
        partial_qty=106.0, partial_cum_filled=0.0,
    ) is True
    # pagkatapos ng k = 40
    assert pb.conservation_holds(
        broker_qty=315.0, successor_qty=249.0,
        partial_qty=106.0, partial_cum_filled=40.0,
    ) is True
    # buong puno
    assert pb.conservation_holds(
        broker_qty=249.0, successor_qty=249.0,
        partial_qty=106.0, partial_cum_filled=106.0,
    ) is True
    # restore edge (walang partial)
    assert pb.conservation_holds(
        broker_qty=315.0, successor_qty=315.0,
        partial_qty=0.0, partial_cum_filled=0.0,
    ) is True


def test_conservation_catches_the_naked_gap():
    """k = 40 na napunan pero R pa rin ang stop at hindi pa naibabalik:
    315 hawak, 249 stop, 66 na bukas na partial ang natitira — kapag
    kinansela ang partial, ang konserbasyon ay dapat sumabog."""
    assert pb.conservation_holds(
        broker_qty=315.0, successor_qty=249.0,
        partial_qty=106.0, partial_cum_filled=106.0,
    ) is False


@pytest.mark.parametrize("kwargs", [
    {"broker_qty": float("nan"), "successor_qty": 1.0, "partial_qty": 1.0,
     "partial_cum_filled": 0.0},
    {"broker_qty": 2.0, "successor_qty": -1.0, "partial_qty": 1.0,
     "partial_cum_filled": 0.0},
    {"broker_qty": 2.0, "successor_qty": 1.0, "partial_qty": 1.0,
     "partial_cum_filled": 5.0},
])
def test_conservation_refuses_nonsense(kwargs):
    assert pb.conservation_holds(**kwargs) is False
