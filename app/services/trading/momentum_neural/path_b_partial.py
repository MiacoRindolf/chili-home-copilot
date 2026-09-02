"""PATH B — purong (I/O-free) core para sa partial exit sa ilalim ng deadman stop.

KONTEKSTO. Sa Alpaca ang deadman stop ay nakaupo para sa BUONG qty, kaya
ubos ang ``qty_available`` at IMPOSIBLE ang partial sell — `live_partial_exit_filled`
= ZERO mula 2026-08-01, at ang CANF ngayong araw ay naglabas ng
`alpaca_scale_out_suppressed_for_deadman` (target 4.63, qty 355) kasama ang
`tranche_oco_skipped_extended_hours`. Ang PATH B ay: PATCH muna ang nakaupong
stop mula Q pababa sa R = Q - f, saka ibenta ang f.

ANG MODULE NA ITO AY HINDI PA NAKAKABIT. Walang import nito ang `live_runner`,
at ang `venue/alpaca_spot.py::replace_order_qty` ay wala pa ring production
caller — binabantayan iyon ng `tests/test_partial_exit_path_b_unwired.py`.
Ang buong disenyo, kasama ang mga dahilan kung bakit ipinagpaliban ang wiring,
ay nasa `docs/DESIGN/PARTIAL_EXIT_PATH_B.md`.

IKALAWANG PASADA (2026-09-02). Dalawang adversarial review ang bumasag sa
UNANG bersyon ng file na ito. Ang apat na pinakamahalagang pagbabago dito:

  1. `blocks_whole_exit` ay HINDI NA `is_in_flight`. Ang lumang bersyon ay
     humaharang ng whole exit sa `replace_stuck` / `containment_queued` /
     `restore_*` — samantalang sa mga phase na iyon ang whole exit MISMO ang
     lunas (L2: ang containment close ay dumadaan sa parehong chokepoint, kaya
     hinaharang ng phase ang tanging bagay na makakapag-alis ng phase).
     Ngayon: tatlong phase lamang ang humaharang, may HARD DEADLINE, at
     hindi kailanman humaharang laban sa operator/EOD/drawdown authority.
  2. `assess_protection` ay HINDI NA nagbibilang ng nakaupong PARTIAL LIMIT
     bilang proteksyon. Ang partial ay sell limit sa TAAS ng merkado — pataas
     na liquidity, hindi pababang takip. Ang lumang bersyon ay nagsasabing
     "covered" ang mismong estado na binabayaran ng PATH B habang 30% ng
     posisyon ay walang stop (S4).
  3. Ang `partial_stale_adopted` at `restore_rejected` ay HINDI NA terminal:
     may hubad na natitira ang dalawa, kaya may utang pa silang service step.
     TERMINAL ∩ NAKED_RISK == ∅ ngayon, at may test na nagpapatunay niyan.
  4. `plan_replacement_edge` ay humihingi na ng `predecessor_filled_size` at
     tumatanggi kapag hindi zero — ang `partially_filled` ay NASA
     `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES` (LR:9200-9207), kaya ang PATCH sa
     isang bahagyang napunang stop ay muling nag-aawtorisa ng share na naibenta na.

IKATLONG PASADA (2026-09-02). Isang pag-audit ng revision 2 laban sa sarili
nitong mga artifact ang nakakita ng natirang depekto na may EKSAKTONG hugis ng
S4: ang pag-aayos ay napunta sa `assess_protection` pero HINDI sa
`NAKED_RISK_PHASES`. Ang buong NORMAL na window ng PATH B
(`successor_certified` -> `partial_posted`) ay iniuulat ng naipadalang checker
bilang `naked_downside=106` habang sinasabi ng naipadalang phase set na hindi
ito hubad — at ang set na iyon ang tanging bagay na titingnan ng isang wiring
para malaman kung may utang itong remedyo. Tatlong pagbabago:

  5. `NAKED_RISK_PHASES` ay muling hinango mula sa naitamang checker: kasama na
     ang limang phase ng normal na window. Hindi na magkasalungat ang dalawang
     artifact, at may test na nag-uugnay sa kanila para hindi na sila
     mag-drift muli.
  6. Ang invariant ng R4 ay ABOT (`reachable_phases`) at hindi na KATABI. Ang
     tuwirang tseke ng revision 2 ay hindi kayang saklawin ang
     `successor_certified`, na ang restore ay dumadaan muna sa `post_deferred`.
  7. Ang tahimik na `continue` para sa `replace_stuck` / `containment_queued`
     sa test ng `consumed_by_exit` ay pinalitan ng PINANGALANANG
     `EXIT_CONSUMPTION_UNRECORDABLE_PHASES` na may sariling dahilan at sariling
     invariant. Ang naipadalang docstring at ang naipadalang test ay
     magkasalungat noon tungkol sa pagiging unibersal ng panuntunan.

BAKIT PURO. Ang mga bagay na kaya nating patunayan nang WALANG DB at WALANG
broker ay: (a) legal ba ang isang phase transition, (b) legal ba ang hati ng
qty, (c) protektado pa ba ang natitirang posisyon, (d) tama ba ang envelope na
ipapakita natin sa lineage matcher ng broker. Iyon ang laman dito. Ang claim
CAS at ang chokepoint ay may I/O at hindi mapapatunayan ng fake, kaya wala rito.

Walang import mula sa `live_runner` (o kahit anong may I/O) ANG BUONG FILE.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "PHASES",
    "TERMINAL_PHASES",
    "NAKED_RISK_PHASES",
    "REMEDY_PHASES",
    "WHOLE_EXIT_BLOCKING_PHASES",
    "SIBLING_LIVE_PHASES",
    "EXIT_CONSUMPTION_UNRECORDABLE_PHASES",
    "LEGAL_TRANSITIONS",
    "reachable_phases",
    "WHOLE_EXIT_BLOCK_CEILING_SECONDS",
    "MARKER_UNRESOLVED_CEILING_SECONDS",
    "PhaseError",
    "advance_phase",
    "is_in_flight",
    "is_terminal",
    "blocks_whole_exit",
    "requires_sibling_reconcile",
    "marker_ceiling_exceeded",
    "SplitPlan",
    "plan_replacement_edge",
    "marker_successor_envelope",
    "ProtectionVerdict",
    "assess_protection",
    "conservation_holds",
]


# --------------------------------------------------------------------------
# 1. Ang state machine
# --------------------------------------------------------------------------

#: Bawat legal na phase ng marker `deadman_qty_replacement`.
PHASES: Final[frozenset[str]] = frozenset({
    # unang edge: Q -> R
    "intent_frozen",
    "replace_submitted",
    "replace_indeterminate",
    "replace_rejected",
    "successor_certified",
    "post_deferred",
    # ang partial mismo
    "partial_posting",
    "partial_posted",
    "partial_indeterminate",
    "partial_filled",
    "partial_stale_adopted",
    "partial_rejected",
    "partial_rejected_final",
    # pangalawang edge: R -> broker_qty - open_partial (ibinabalik ang takip)
    "restore_intent_frozen",
    "restore_replace_submitted",
    "restore_indeterminate",
    "restore_certified",
    "restore_rejected",
    # ang huling remedyo kapag hindi na maibalik ang takip
    "flatten_queued",
    "flattened",
    # escape mula sa stuck na replace (BAGO ang certification lamang)
    "replace_stuck",
    "containment_queued",
    "containment_resolved",
    "replace_reverted",
    # iba pang terminal
    "abandoned",
    "consumed_by_exit",
})

#: Tapos na — walang service step ang tatakbo pa.
#:
#: INVARIANT (may test): TERMINAL ∩ NAKED_RISK == ∅. Ang isang phase na may
#: hubad na natitirang share ay HINDI tapos; may utang pa itong restore o
#: flatten. Ang unang bersyon ay naglagay ng `partial_stale_adopted` at
#: `restore_rejected` sa magkabilang set — kaya hindi maitala ang lunas na
#: mismong hinihingi ng R4.
TERMINAL_PHASES: Final[frozenset[str]] = frozenset({
    "replace_rejected",
    "partial_filled",
    "restore_certified",
    "flattened",
    "containment_resolved",
    "replace_reverted",
    "abandoned",
    "consumed_by_exit",
})

#: Mga phase kung saan may bahagi ng posisyon na WALANG PABABANG stop (R4).
#:
#: IKATLONG PASADA (2026-09-02). Ang set na ito ay ang PINAKAMALAKING natirang
#: depekto ng revision 2, at ito ay may EKSAKTONG hugis ng S4. Inayos ng
#: revision 2 ang `assess_protection` para tumigil sa pagbibilang ng nakaupong
#: partial LIMIT bilang proteksyon — pero HINDI iyon naipasa rito. Ang resulta
#: ay dalawang naipadalang artifact na MAGKASALUNGAT tungkol sa mismong window
#: na binabayaran ng PATH B::
#:
#:     assess_protection(broker_qty=355, stop_qty=249, open_partial_qty=106)
#:         -> status="naked_downside", naked_downside_qty=106
#:     "partial_posted" in NAKED_RISK_PHASES   -> False        (revision 2)
#:
#: Sinasabi na ng §4/D1 ng disenyo ang tama: "mula sa certification HANGGANG
#: maging terminal ang partial — ang BUONG buhay ng partial, ang NORMAL na
#: kaso — ang f na share ay walang PABABANG stop". Limang phase ang saklaw
#: niyon at wala ni isa sa kanila ang nakabandera noon:
#:
#:   * `successor_certified` at `post_deferred` — nakaupo ang R stop, hawak pa
#:     ang buong Q, at WALA PANG partial na nakapost: f na share na walang stop
#:     AT walang kahit anong nakaupong sell. Ito ang pinakahubad sa lahat.
#:   * `partial_posting` / `partial_posted` / `partial_indeterminate` — may
#:     nakaupong sell limit ang f, pero sa TAAS ng merkado. Hindi iyon takip.
#:
#: Ang isang wiring na naka-gate sa `phase in NAKED_RISK_PHASES` — na siyang
#: TANGING dahilan ng pag-iral ng set na ito — ay lalaktawan ang remedyo sa
#: buong normal na window at ipagpapalagay na walang panganib doon.
#:
#: HINDI kasama ang tatlong `WHOLE_EXIT_BLOCKING_PHASES`. Sa mga iyon ay malabo
#: ang MAY-ARI ng stop (Q pa ba o R na?), at ang remedyo ay hindi restore o
#: flatten kundi ang pagtatapos mismo ng in-flight na edge — o ang pag-expire ng
#: 30 s na ceiling. Iyon nga ang dahilan ng ceiling. Ang pagsama sa kanila rito
#: ay babasag din sa invariant na WHOLE_EXIT_BLOCKING ∩ NAKED_RISK == ∅ (L2).
NAKED_RISK_PHASES: Final[frozenset[str]] = frozenset({
    # ang NORMAL na window — naidagdag sa revision 3 (tingnan sa itaas)
    "successor_certified",
    "post_deferred",
    "partial_posting",
    "partial_posted",
    "partial_indeterminate",
    # ang mga failure branch — ito lamang ang nabanggit ng revision 2
    "partial_rejected",
    "partial_rejected_final",
    "partial_stale_adopted",
    "restore_intent_frozen",
    "restore_replace_submitted",
    "restore_indeterminate",
    "restore_rejected",
    "flatten_queued",
})

#: Ang mga phase kung saan AKTIBONG ibinabalik (o isinusuko) ang takip. Ang
#: invariant ng R4 ay: mula sa BAWAT naked na phase ay ABOT ang isa sa mga ito.
REMEDY_PHASES: Final[frozenset[str]] = frozenset({
    "restore_intent_frozen",
    "restore_replace_submitted",
    "restore_indeterminate",
    "restore_certified",
    "flatten_queued",
    "flattened",
})

#: Ang DALAWANG phase kung saan HINDI naitatala ang `consumed_by_exit`, at
#: kung bakit sinasadya iyon.
#:
#: Sa `replace_stuck` / `containment_queued` ay may nakaupong broker order na
#: HINDI natin alam kung kanino (iyon mismo ang kahulugan ng stuck). Kung
#: papayagan nating maging `consumed_by_exit` — na TERMINAL — ay titigil ang
#: pag-service habang may hindi kilalang order na nakaupo sa broker: iyon ang
#: hugis ng "paper zombie / ghost position". Ang containment lineage ay dapat
#: TUMAKBO PA RIN kahit na-flatten na ng operator ang posisyon, dahil ang order
#: ang natitirang problema, hindi ang posisyon.
#:
#: Kaya ang exception ay may sariling invariant (may test): ang bawat phase
#: dito ay dapat MANATILING may service step (hindi walang laman ang transition
#: set) AT may tahasang labasan ng operator patungo sa `abandoned`.
EXIT_CONSUMPTION_UNRECORDABLE_PHASES: Final[frozenset[str]] = frozenset({
    "replace_stuck",
    "containment_queued",
})

#: HINDI ito `is_in_flight`. Tatlong phase LAMANG ang tunay na malabo ang
#: MAY-ARI ng stop — habang nasa ere ang unang PATCH. Doon lang legal ang
#: paghadlang sa whole exit, dahil ang pag-freeze ng close handoff laban sa
#: isang `replaced` na predecessor ang mismong deadlock ng R2.
#:
#: Bakit WALA rito ang `replace_stuck` / `containment_queued` / `restore_*`:
#: sa mga phase na iyon ang whole exit ay ang LUNAS, hindi ang panganib. Ang
#: containment close (LR:9959) at ang `_queue_full_close` na hinihingi ng R4 ay
#: parehong dumadaan sa `_release_deadman_at_literal_submit`; kung haharangin
#: sila ng phase, walang makakapag-usad ng phase (L2).
#:
#: INVARIANT (may test): WHOLE_EXIT_BLOCKING ∩ NAKED_RISK == ∅.
WHOLE_EXIT_BLOCKING_PHASES: Final[frozenset[str]] = frozenset({
    "intent_frozen",
    "replace_submitted",
    "replace_indeterminate",
})

#: Mga phase kung saan MAAARING may BUHAY na partial sell order sa broker na
#: alam lamang ng claim (hindi pa ng `le`). Bago ang ANUMANG whole exit sa mga
#: phase na ito ay OBLIGADO munang itayo muli ang `le["scale_limit_*"]` mula sa
#: claim — kung hindi, ang `_cancel_scale_limit_and_clamp` ay tahimik na
#: pass-through noop (LR:18968-18970: `if not oid: return requested_qty`) at
#: ang whole exit ay magpapadala ng Q habang may f na nakaupo: Q + f na sell
#: authority laban sa Q share = short flip (S2).
SIBLING_LIVE_PHASES: Final[frozenset[str]] = frozenset({
    "partial_posting",
    "partial_posted",
    "partial_indeterminate",
})

#: Hangganan ng paghadlang sa whole exit. Isang owner-transport lease
#: (AOC:60 `_OWNER_TRANSPORT_LEASE_SECONDS` = 30 s). Lampas dito ay
#: DUMADAAN ang exit kahit malabo pa ang may-ari: mas mabuting isara nang
#: may kalabuan kaysa hindi kailanman makalabas.
WHOLE_EXIT_BLOCK_CEILING_SECONDS: Final[float] = 30.0

#: Kabuuang buhay ng isang hindi pa nareresolbang marker. Lampas dito ay
#: sapilitan ang `abandoned` at isang restore-o-flatten na desisyon; walang
#: marker ang puwedeng mag-wedge ng exit nang walang hanggan.
MARKER_UNRESOLVED_CEILING_SECONDS: Final[float] = 300.0

#: Ang buong legal na graph. Anumang wala rito ay bug — hindi tahimik na
#: pinapayagan (`advance_phase` ay nagre-raise).
#:
#: Tatlong panuntunan ang sinusunod ng talahanayan (lahat ay may test):
#:   * mula sa bawat NAKED_RISK phase ay ABOT ang isang REMEDY_PHASE — abot,
#:     hindi katabi (tingnan ang `reachable_phases`);
#:   * ang `consumed_by_exit` ay abot mula sa bawat phase kung saan hindi
#:     hinaharang ang whole exit — dahil doon nga puwedeng lamunin ng isang
#:     buong exit ang posisyon habang bukas pa ang marker (L5) — MALIBAN sa
#:     `EXIT_CONSUMPTION_UNRECORDABLE_PHASES`, na may sariling dahilan at
#:     sariling invariant;
#:   * walang non-terminal na phase na patay na kalsada.
LEGAL_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "intent_frozen": frozenset({
        "replace_submitted", "replace_indeterminate", "replace_rejected",
        "abandoned",
    }),
    "replace_submitted": frozenset({
        "successor_certified", "replace_stuck", "abandoned",
    }),
    "replace_indeterminate": frozenset({
        "successor_certified", "replace_stuck", "abandoned",
    }),
    "replace_rejected": frozenset(),
    # WALANG `successor_certified -> replace_stuck`. Pagkatapos ng
    # certification ay ALAM na nakaupo ang R stop; ang isang lumilipas na
    # read miss ay hindi puwedeng mag-escalate patungo sa isang containment
    # na nagfa-flatten ng protektadong runner (S5). Ang tanging labasan ay
    # `post_deferred` — retry, restore, o operator review.
    "successor_certified": frozenset({
        "partial_posting", "post_deferred", "consumed_by_exit", "abandoned",
    }),
    "post_deferred": frozenset({
        "partial_posting", "restore_intent_frozen", "consumed_by_exit",
        "abandoned",
    }),
    "partial_posting": frozenset({
        "partial_posted", "partial_indeterminate", "partial_rejected",
        "consumed_by_exit",
    }),
    "partial_posted": frozenset({
        "partial_filled", "partial_stale_adopted", "consumed_by_exit",
    }),
    "partial_indeterminate": frozenset({
        "partial_posted", "partial_rejected_final", "consumed_by_exit",
    }),
    "partial_rejected": frozenset({
        "partial_posting", "partial_rejected_final", "restore_intent_frozen",
        "consumed_by_exit",
    }),
    "partial_rejected_final": frozenset({
        "restore_intent_frozen", "flatten_queued", "consumed_by_exit",
    }),
    # NAKED: ang k < f na stale cancel ay nag-iiwan ng f - k na walang stop.
    # Kaya may restore edge itong utang — hindi ito terminal.
    "partial_stale_adopted": frozenset({
        "restore_intent_frozen", "flatten_queued", "consumed_by_exit",
    }),
    "partial_filled": frozenset(),
    "restore_intent_frozen": frozenset({
        "restore_replace_submitted", "restore_indeterminate",
        "restore_rejected", "flatten_queued", "consumed_by_exit",
    }),
    "restore_replace_submitted": frozenset({
        "restore_certified", "restore_rejected", "flatten_queued",
        "consumed_by_exit",
    }),
    "restore_indeterminate": frozenset({
        "restore_certified", "restore_rejected", "flatten_queued",
        "consumed_by_exit",
    }),
    "restore_certified": frozenset(),
    # NAKED: tinanggihan ang restore. Isang retry, o flatten. Hindi tapos.
    "restore_rejected": frozenset({
        "restore_intent_frozen", "flatten_queued", "consumed_by_exit",
    }),
    "flatten_queued": frozenset({"flattened", "consumed_by_exit"}),
    "flattened": frozenset(),
    "replace_stuck": frozenset({
        "containment_queued", "replace_reverted", "abandoned",
    }),
    "containment_queued": frozenset({
        "containment_resolved", "replace_reverted", "abandoned",
    }),
    "containment_resolved": frozenset(),
    "replace_reverted": frozenset(),
    "abandoned": frozenset(),
    "consumed_by_exit": frozenset(),
}


class PhaseError(ValueError):
    """Ilegal na phase o transition — palaging ere, hindi kailanman tahimik."""


def _require_phase(phase: str, *, label: str) -> str:
    text = str(phase or "").strip()
    if text not in PHASES:
        raise PhaseError(f"{label}_unknown_phase:{text!r}")
    return text


def advance_phase(current: str, target: str) -> str:
    """Isulong ang marker; nagre-raise kapag ilegal ang hakbang.

    ITO ANG NAG-IISANG sanctioned na writer ng `phase`. Walang "bagong edge"
    na daan-likod: iisa lamang ang `phase` field ng marker, kaya ang anumang
    bagong edge ay kailangan pa ring dumaan dito. (Ang unang bersyon ng
    docstring na ito ay nag-alok ng ganoong bypass; iyon ang paraan para
    malampasan ng isang wiring PR ang tanging validator na ipinapadala natin.)
    """
    src = _require_phase(current, label="current")
    dst = _require_phase(target, label="target")
    allowed = LEGAL_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        raise PhaseError(f"illegal_transition:{src}->{dst}")
    return dst


def reachable_phases(start: str) -> frozenset[str]:
    """Bawat phase na ABOT mula sa `start` sa pamamagitan ng legal na hakbang.

    Kailangan ito para maging TOTOO ang invariant ng R4. Ang revision 2 ay
    tumitingin lamang sa TUWIRANG target ("may kasama bang remedyo ang
    transition set nito?"). Mahina iyon: ang `successor_certified` ay hubad
    (f na share, walang stop) pero ang restore nito ay dumadaan muna sa
    `post_deferred`, kaya papasa ang tuwirang tseke sa maling dahilan — o
    babagsak sa tamang graph. Ang tamang tanong ay ABOT ba, hindi KATABI ba.

    Hindi kasama ang `start` mismo maliban kung may cycle pabalik dito.
    """
    src = _require_phase(start, label="phase")
    seen: set[str] = set()
    stack = list(LEGAL_TRANSITIONS.get(src, frozenset()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(LEGAL_TRANSITIONS.get(node, frozenset()))
    return frozenset(seen)


def is_terminal(phase: str) -> bool:
    return _require_phase(phase, label="phase") in TERMINAL_PHASES


def is_in_flight(phase: str) -> bool:
    """May service step pa bang utang ang marker na ito? (= hindi pa terminal.)

    HIWALAY ito sa `blocks_whole_exit`. Halos lahat ng in-flight na phase ay
    HINDI humaharang ng exit; ang paghaluin ang dalawa ang eksaktong bug na
    nag-deadlock sa unang bersyon.
    """
    return _require_phase(phase, label="phase") not in TERMINAL_PHASES


def blocks_whole_exit(
    phase: str,
    *,
    age_seconds: float | None,
    ceiling_seconds: float = WHOLE_EXIT_BLOCK_CEILING_SECONDS,
    override_authority: bool = False,
) -> bool:
    """Dapat bang ipagpaliban ng chokepoint ang whole exit nitong pulse na ito?

    Tatlong hangganan, at lahat ay nakaayos para MAS MADALING makalabas kaysa
    ma-wedge:

    * `override_authority=True` (operator flatten, EOD, drawdown breaker,
      kill switch) — HINDI kailanman naipagpapaliban. Ang mga awtoridad na
      iyon ay hindi puwedeng nakabitin sa likod ng isang marker.
    * `phase` ay dapat nasa `WHOLE_EXIT_BLOCKING_PHASES` — tatlo lamang, at
      wala ni isa sa mga ito ang may hubad na natitira.
    * `age_seconds` ay dapat kilala AT nasa loob pa ng `ceiling_seconds`.
      Hindi kilala, hindi finite, negatibo, o lampas na sa hangganan =>
      HINDI humaharang. Ang paghadlang ay isang paghingi ng isang pulse,
      hindi isang lock.
    """
    src = _require_phase(phase, label="phase")
    if override_authority:
        return False
    if src not in WHOLE_EXIT_BLOCKING_PHASES:
        return False
    if age_seconds is None:
        return False
    try:
        age = float(age_seconds)
        ceiling = float(ceiling_seconds)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(age) and math.isfinite(ceiling)):
        return False
    if age < 0.0 or ceiling <= 0.0:
        return False
    return age <= ceiling


def requires_sibling_reconcile(phase: str) -> bool:
    """Kailangan bang itayo muli ang `le["scale_limit_*"]` mula sa claim BAGO
    tumakbo ang `_cancel_scale_limit_and_clamp`?

    Oo sa bawat phase kung saan maaaring may buhay na partial sell na alam
    lamang ng claim. Kung hindi ito susundin ay pass-through noop ang clamp at
    magkakaroon ng Q + f na sell authority laban sa Q share (S2).
    """
    return _require_phase(phase, label="phase") in SIBLING_LIVE_PHASES


def marker_ceiling_exceeded(
    unresolved_age_seconds: float | None,
    *,
    ceiling_seconds: float = MARKER_UNRESOLVED_CEILING_SECONDS,
) -> bool:
    """Lumagpas na ba ang marker sa kabuuang wall-clock na hangganan nito?

    Hindi kilalang edad => False (hindi tayo nag-a-abandon sa haka-haka).
    True => sapilitan ang `abandoned` at isang restore-o-flatten na desisyon.
    """
    if unresolved_age_seconds is None:
        return False
    try:
        age = float(unresolved_age_seconds)
        ceiling = float(ceiling_seconds)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(age) and math.isfinite(ceiling)):
        return False
    return age > ceiling


# --------------------------------------------------------------------------
# 2. Ang hati ng qty at ang envelope ng successor
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SplitPlan:
    """Ang hati Q -> (f ibebenta, R iiwang naka-stop)."""

    total_qty: float
    partial_qty: float
    successor_qty: float
    ok: bool
    reason: str = ""


def plan_replacement_edge(
    *,
    total_qty: float,
    partial_qty: float,
    predecessor_filled_size: float,
    whole_shares: bool = True,
) -> SplitPlan:
    """R = Q - f, na may mga tseke na kailangan BAGO gumalaw ang broker.

    Tinatanggihan: hindi finite, Q <= 0, f <= 0, f >= Q (walang runner —
    iyon ay whole exit, hindi partial), para sa equity ang hindi buong share
    sa alinman sa dalawang binti, at — mahalaga — ang PREDECESSOR NA MAY
    FILL. Ang `partially_filled` ay NASA `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES`
    (LR:9200-9207), kaya "buhay" pa rin ang tingin dito ng maintenance; ang
    PATCH sa isang stop na pumutok na nang bahagya ay muling nag-aawtorisa ng
    share na NAIBENTA NA. Walang parameter para rito ang unang bersyon, kaya
    walang wiring ang makakatsek nito sa tamang lugar.
    """
    q = float(total_qty)
    f = float(partial_qty)
    bad = SplitPlan(total_qty=q, partial_qty=f, successor_qty=0.0, ok=False)
    try:
        pf = float(predecessor_filled_size)
    except (TypeError, ValueError):
        return SplitPlan(**{**bad.__dict__, "reason": "predecessor_fill_unreadable"})
    if not (math.isfinite(q) and math.isfinite(f) and math.isfinite(pf)):
        return SplitPlan(**{**bad.__dict__, "reason": "non_finite_quantity"})
    if pf < 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "predecessor_fill_negative"})
    if pf > 1e-9:
        return SplitPlan(
            **{**bad.__dict__, "reason": "predecessor_partially_filled"}
        )
    if q <= 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "non_positive_total"})
    if f <= 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "non_positive_partial"})
    if f >= q:
        return SplitPlan(**{**bad.__dict__, "reason": "partial_leaves_no_runner"})
    r = q - f
    if whole_shares:
        for leg, name in ((q, "total"), (f, "partial"), (r, "successor")):
            if abs(leg - round(leg)) > 1e-9:
                return SplitPlan(
                    **{**bad.__dict__, "reason": f"fractional_{name}_quantity"}
                )
        if round(r) < 1 or round(f) < 1:
            return SplitPlan(**{**bad.__dict__, "reason": "leg_below_one_share"})
    return SplitPlan(total_qty=q, partial_qty=f, successor_qty=r, ok=True)


def marker_successor_envelope(
    *,
    predecessor_order_request: dict[str, Any],
    successor_client_order_id: str,
    successor_qty: float,
) -> dict[str, Any] | None:
    """Ang envelope na dapat ipakita sa lineage matcher para sa isang edge na
    NAGPAPALIIT ng qty.

    ITO ANG S1. Ginagawa ngayon ng `_dispatch_alpaca_replaced_deadman_successor`
    ang inaasahang envelope sa pamamagitan ng pagkopya sa predecessor at
    pagpalit LAMANG ng cid (LR:10101-10104)::

        successor_request = {**predecessor_request, "client_order_id": cid}

    kaya nananatiling Q ang `base_size`. Pagkatapos ay hinihingi ng
    `_owner_transport_order_matches` (LR:9108-9130) na::

        abs(broker_qty - float(request["base_size"])) <= tol

    Ang successor na nakaupo para sa R = Q - f ay babagsak sa pagkakapantay na
    iyon nang eksaktong f — MAGPAKAILANMAN, bawat pulse
    (`replacement_deadman_successor_lineage_unproven`). Ibig sabihin: ang
    MATAGUMPAY na PATCH — ang mismong napatunayan ng live probe sa 200-254 ms —
    ay hindi kailanman maka-certify sa ilalim ng kasalukuyang code.

    Ang helper na ito ang tamang envelope: kapareho ng predecessor sa BAWAT
    field (side, product, tif, position_intent, extended_hours False,
    stop_price) maliban sa `client_order_id` at `base_size`. Ang wiring ay
    kailangang ipasa ito mula sa marker, hindi buuin mula sa predecessor.
    """
    if not isinstance(predecessor_order_request, dict):
        return None
    cid = str(successor_client_order_id or "").strip()
    if not cid:
        return None
    try:
        qty = float(successor_qty)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty) or qty <= 0.0:
        return None
    try:
        predecessor_qty = float(predecessor_order_request.get("base_size"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(predecessor_qty) or predecessor_qty <= 0.0:
        return None
    # Ang edge na ito ay PALAGING nagpapaliit. Ang pagpapalaki ay ibang usapin
    # (bagong risk), at ang pagkakapantay ay walang saysay na PATCH.
    if qty >= predecessor_qty - 1e-9:
        return None
    envelope = dict(predecessor_order_request)
    envelope["client_order_id"] = cid
    envelope["base_size"] = qty
    return envelope


# --------------------------------------------------------------------------
# 3. Ang invariant checker
# --------------------------------------------------------------------------

def _tol(*values: float) -> float:
    scale = max((abs(v) for v in values), default=0.0)
    return max(1e-6, scale * 1e-6)


@dataclass(frozen=True)
class ProtectionVerdict:
    """Ang sagot sa DALAWANG magkaibang tanong — hindi isa.

    Pinaghalo ng unang bersyon ang dalawa at ibinilang ang nakaupong partial
    LIMIT bilang proteksyon. Hindi iyon proteksyon: ang partial ay sell limit
    sa TAAS ng merkado (CANF: target 4.63). Sa isang gap-down ay pumuputok ang
    stop para sa R habang ang f ay bumabagsak kasama ng presyo laban sa isang
    limit na hindi na maaabot. Ang canonical na estado ng PATH B ay
    `covered_for_oversell` PERO `naked_downside = f` — at iyon ang numerong
    kailangang tanggapin ng operator nang nakasulat.
    """

    status: str                        # flat | covered | naked_downside | oversell_risk | invalid
    oversell_ok: bool                  # stop + open_partial <= broker_qty
    naked_downside_qty: float          # share na WALANG stop (kahit may partial sa taas)
    unhedged_qty_with_resting_sell: float  # bahagi ng naked na may nakaupong SELL sa taas
    ok: bool                           # covered o flat LAMANG

    @property
    def requires_restore_or_flatten(self) -> bool:
        return self.naked_downside_qty > 0.0 and self.oversell_ok


def assess_protection(
    *,
    broker_qty: float,
    stop_qty: float,
    open_partial_qty: float,
) -> ProtectionVerdict:
    """Ang isang tseke na DAPAT tawagin ng bawat wiring sa hinaharap.

    * **oversell / short flip** — `stop_qty + open_partial_qty` ay hindi
      kailanman dapat lumampas sa hawak sa broker. Ang paglampas ay dalawang
      sell authority sa parehong share; kapag pumutok pareho ay mababaliktad
      tayo pa-short.
    * **naked downside** (R4) — `broker_qty - stop_qty > 0` ay share na walang
      PABABANG takip. Ang nakaupong partial ay HINDI nagbabawas nito; ito ay
      iniuulat nang hiwalay bilang `unhedged_qty_with_resting_sell` para
      malaman ng operator kung ilan sa hubad ang may kahit anong nakaupong
      sell (pataas man).
    """
    b = float(broker_qty)
    s = float(stop_qty)
    p = float(open_partial_qty)
    invalid = ProtectionVerdict(
        status="invalid",
        oversell_ok=False,
        naked_downside_qty=0.0,
        unhedged_qty_with_resting_sell=0.0,
        ok=False,
    )
    if not (math.isfinite(b) and math.isfinite(s) and math.isfinite(p)):
        return invalid
    if s < 0.0 or p < 0.0 or b < 0.0:
        return invalid
    tol = _tol(b, s, p)
    oversell_ok = (s + p) <= b + tol
    naked = max(0.0, b - s)
    naked = 0.0 if naked <= tol else naked
    unhedged_with_sell = min(p, naked)
    if not oversell_ok:
        return ProtectionVerdict(
            status="oversell_risk",
            oversell_ok=False,
            naked_downside_qty=naked,
            unhedged_qty_with_resting_sell=unhedged_with_sell,
            ok=False,
        )
    if b <= tol:
        return ProtectionVerdict(
            status="flat",
            oversell_ok=True,
            naked_downside_qty=0.0,
            unhedged_qty_with_resting_sell=0.0,
            ok=True,
        )
    if naked > 0.0:
        return ProtectionVerdict(
            status="naked_downside",
            oversell_ok=True,
            naked_downside_qty=naked,
            unhedged_qty_with_resting_sell=unhedged_with_sell,
            ok=False,
        )
    return ProtectionVerdict(
        status="covered",
        oversell_ok=True,
        naked_downside_qty=0.0,
        unhedged_qty_with_resting_sell=0.0,
        ok=True,
    )


def conservation_holds(
    *,
    broker_qty: float,
    successor_qty: float,
    partial_qty: float,
    partial_cum_filled: float,
) -> bool:
    """Ang panuklas ng lineage para sa isang marker edge (kapalit ng LR:10202-10208).

    Ang orihinal na tseke ay `broker_qty == successor_requested`, na TAMA
    lamang bago pumutok ang sibling. Sa sandaling mapunan ang k share ng
    partial ay bumababa ang posisyon sa Q - k habang ang stop ay nananatiling
    R, kaya ang hubad na pagkakapantay ay mabibigo magpakailanman at
    mahaharangan ang dispatch (H2). Ang tamang konserbasyon ay:

        broker_qty == successor_qty + (partial_qty - partial_cum_filled)

    Bago ang POST: Q == R + f.  Pagkatapos ng k: Q - k == R + (f - k).
    Buong puno: Q - f == R.  Para sa restore edge (partial_qty = 0): Q == R2.

    ANG PANGALAWANG GATE. Ito rin ang tamang anyo para sa
    `abs(local_qty - requested_qty) <= tol` sa LR:10174-10190, na hindi
    nabanggit ng unang disenyo: doon ang `requested_qty` ay `base_size` ng
    PREDECESSOR (Q), kaya sa sandaling mapunan ang k ay
    `replacement_deadman_successor_quantity_generation_mismatch` na
    magpakailanman.
    """
    b = float(broker_qty)
    r = float(successor_qty)
    f = float(partial_qty)
    k = float(partial_cum_filled)
    if not all(math.isfinite(v) for v in (b, r, f, k)):
        return False
    if r < 0.0 or f < 0.0 or k < 0.0 or k > f + _tol(f):
        return False
    unsold = f - k
    return abs(b - (r + unsold)) <= _tol(b, r, unsold)
