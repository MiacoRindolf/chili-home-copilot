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

IKAAPAT NA PASADA (2026-09-02). Isang ikatlong adversarial review ang bumasag
sa revision 3. Ang mga depekto nito ay nasa mga EDGE at sa mga SIGNATURE — hindi
na sa mga set, na inayos ng revision 3. Walo pang pagbabago:

   8. TINANGGAL ang `successor_certified -> abandoned` at
      `post_deferred -> abandoned`. Ang `abandoned` ay TERMINAL na walang
      labasan, kaya ang dalawang edge na iyon ay S4 muli, isang hop palabas:
      isang hubad na marker na dumaan doon ay hindi na kailanman maka-record ng
      restore o flatten, at `is_in_flight` ay False kaya walang service step ang
      tatakbo pa. Ang lumang invariant ("may ABOT na remedyo") ay pumapasa nang
      walang saysay dahil sapat na sa kanya na may ISANG landas na may remedyo.
      Ngayon: `NAKED_RESOLVING_TERMINALS` at ang invariant ay BAWAT abot na
      terminal, hindi isa lamang.
   9. Ang 300 s na ceiling ay MAISASAGAWA na. Ang `abandoned` ay walang papasok
      na edge mula sa 11 sa 13 na naked phase, kaya ang "sapilitan ang
      `abandoned`" ay nagre-raise ng `PhaseError` sa loob mismo ng service step
      (D4/D3: "isang tunay na patay na stop na iniulat bilang protected"). Ang
      `abandoned` ay para sa PRE-certification na lineage lamang — doon ay buo
      pa ang Q stop at walang hubad. Para sa hubad ay `flatten_queued` ang
      sapilitan. `marker_ceiling_forced_target()` ang nagsasabi kung alin, at
      may test na nagpapatunay na TUMATANGGAP ang `advance_phase` sa bawat isa.
  10. IDINAGDAG ang `flatten_queued` mula sa BUONG normal na window. Sinasabi ng
      §3.2 ng disenyo na "any NAKED phase -> flatten_queued", pero wala ni isa
      sa limang phase na idinagdag ng revision 3 sa `NAKED_RISK_PHASES` ang may
      ganoong edge. Ang tanging daan palabas ay ang PAG-RECORD MUNA NG
      RESULTANG PANG-BROKER NA HINDI PA NANGYAYARI (`partial_stale_adopted`,
      `partial_rejected_final`) — iyon ay pagpapapeke sa ledger para lang
      makalabas.
  11. Ang utang na sibling-reconcile ay HINANGO na (`SIBLING_RECONCILE_PHASES`),
      hindi na `SIBLING_LIVE_PHASES` mismo. Ang `partial_indeterminate ->
      partial_rejected_final -> flatten_queued` ay dalawang legal na hakbang
      palabas ng set kung saan HINDI ALAM ang liveness ng sibling, at sa dulo ay
      isang BUONG flatten na hindi humihingi ng reconcile: short flip (S2) sa
      pamamagitan ng paglalakad palabas ng set na ginawa para pigilan iyon. May
      `exit_consumption_precondition()` na ngayon para may lugar ang wiring na
      patunayang naibayad ang utang bago maitala ang TERMINAL na
      `consumed_by_exit`.
  12. Ang `marker_successor_envelope` ay may `edge_kind` na. Ang R4 restore edge
      at ang §3.4c pyramid re-cover ay PALAGING LUMALAKI (Q - k > Q - f), pero
      tumatanggi ang revision 3 sa bawat lumalaking edge — kaya ang TANGING
      remedyo sa hubad ay hindi kailanman ma-lineage-certify at mabubulok sa
      `replacement_deadman_successor_lineage_unproven` hanggang mag-expire ang
      lease, saka flatten. "Palaging i-flatten ang runner" ang naging remedyo.
  13. Ang `assess_protection` ay may `requires_remedy` na — ang NAG-IISANG
      predicate na dapat i-gate ng wiring. Ang `requires_restore_or_flatten` ay
      nagbabalik ng False kapag `oversell_risk`, kahit may hubad na share, dahil
      `and self.oversell_ok` ang huling clause nito. May `open_partial_qty` din
      itong tinatanggap na PRE-NETTED, samantalang `f` at `k` ang itinatago ng
      marker: may `open_partial_qty_from_marker()` na ngayon.
  14. Walang pampublikong helper dito ang NAGRE-RAISE na sa hindi mabasang
      input. Ang `assess_protection(broker_qty=None, ...)` ay nagre-raise ng
      TypeError BAGO pa marating ang sarili nitong `invalid` na hatol —
      samantalang `broker_recon_status` NULL / `history_unavailable` ay tunay na
      estado sa repo na ito. Ganoon din ang `conservation_holds` at ang dalawang
      unang argumento ng `plan_replacement_edge`.
  15. `MappingProxyType` ang `LEGAL_TRANSITIONS`. Ang `Final` ay hindi
      ipinatutupad (walang mypy — CLAUDE.md), kaya ang graph na tinatanong ng
      NAG-IISANG validator natin ay naisusulat ng kahit anong nag-i-import.

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
from types import MappingProxyType
from typing import Any, Final, Mapping

__all__ = [
    "PHASES",
    "TERMINAL_PHASES",
    "NAKED_RISK_PHASES",
    "NAKED_RESOLVING_TERMINALS",
    "REMEDY_PHASES",
    "WHOLE_EXIT_BLOCKING_PHASES",
    "SIBLING_LIVE_PHASES",
    "SIBLING_RESOLVED_PHASES",
    "SIBLING_RECONCILE_PHASES",
    "EXIT_CONSUMPTION_UNRECORDABLE_PHASES",
    "LEGAL_TRANSITIONS",
    "reachable_phases",
    "reachable_terminals",
    "WHOLE_EXIT_BLOCK_CEILING_SECONDS",
    "MARKER_UNRESOLVED_CEILING_SECONDS",
    "PhaseError",
    "advance_phase",
    "is_in_flight",
    "is_terminal",
    "blocks_whole_exit",
    "requires_sibling_reconcile",
    "exit_consumption_precondition",
    "marker_ceiling_exceeded",
    "marker_ceiling_forced_target",
    "SplitPlan",
    "plan_replacement_edge",
    "plan_restore_edge",
    "marker_successor_envelope",
    "ProtectionVerdict",
    "assess_protection",
    "open_partial_qty_from_marker",
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

#: Ang TANGING mga terminal kung saan puwedeng magtapos ang isang HUBAD na
#: marker — dahil sa bawat isa sa kanila ay naresolba na ang hubad na natitira.
#:
#: IKAAPAT NA PASADA. Ito ang pinalitan ng mahinang invariant ng revision 3.
#: Ang tanong noon ay "may ISANG abot na remedyo ba?", at oo — pero ang
#: `successor_certified -> abandoned` ay legal DIN, at ang `abandoned` ay
#: TERMINAL na may `LEGAL_TRANSITIONS["abandoned"] == frozenset()`. Ang isang
#: marker na dumaan doon ay may 106 na share na walang pababang stop,
#: `is_in_flight` ay False kaya walang service step ang tatakbo pa, at ang
#: pangakong restore-o-flatten ay HINDI NA MAITATALA kailanman. Iyon ay S4 na
#: muli, isang hop palabas. Ang tamang tanong ay: BAWAT abot na terminal ba ay
#: isa sa mga ito?
#:
#:   * `flattened` / `restore_certified` — tahasang naibalik o isinuko ang takip.
#:   * `partial_filled` — napuno ang f, kaya Q - f = R ang hawak at R ang stop.
#:   * `consumed_by_exit` — nilamon ng buong exit ang posisyon; wala nang share
#:     na mahuhubaran. (Hiwalay itong may utang na sibling reconcile —
#:     `exit_consumption_precondition` — pero hindi na iyon usapin ng R4.)
#:
#: WALA rito ang `abandoned`, `replace_rejected`, `containment_resolved` at
#: `replace_reverted`: sa apat na iyon ay buo pa ang Q stop (pre-certification)
#: o hindi natin alam ang estado — at wala ni isa sa kanila ang nagsasabing
#: naresolba ang hubad na natitira.
NAKED_RESOLVING_TERMINALS: Final[frozenset[str]] = frozenset({
    "flattened",
    "restore_certified",
    "partial_filled",
    "consumed_by_exit",
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
#:
#: BAKIT HINDI KASALUNGAT ANG LABASANG IYON (ikaapat na pasada). Ang `abandoned`
#: ay TERMINAL din, kaya sa unang tingin ay hinaharangan natin ang isang
#: terminal at saka nag-aalok ng iba. Ang pagkakaiba ay hindi ang terminality
#: kundi ang KAALAMAN: ang `consumed_by_exit` ay naitatala ng makina batay sa
#: PRESENSYA ng isang close handoff — awtomatiko, tahimik, at habang wala pa
#: ring nakakaalam kung kanino ang nakaupong order. Ang `abandoned` mula rito ay
#: isang TAHASANG desisyon ng operator na tumitingin sa mismong order na iyon.
#: Ang una ay pagkalimot; ang pangalawa ay pagpapasya. Ang isang wiring ay hindi
#: kailanman dapat magsulat ng `abandoned` sa dalawang phase na ito nang
#: mag-isa — ito ay para sa operator lamang.
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

#: Ang mga phase kung saan NAPATUNAYAN nang hindi na nakaupo ang partial. Ito
#: ang PUMUPUTOL sa paglaganap ng utang na reconcile sa graph.
#:
#:   * `partial_filled` — napuno nang buo; wala nang natitira.
#:   * `partial_rejected` — tinanggihan ang POST; wala kailanman nakaupo.
#:   * `partial_stale_adopted` — bumalik ang cancel bilang stale at inampon
#:     natin ang k; ang order ay KANSELADO.
#:
#: WALA rito ang `partial_rejected_final`: abot iyon mula sa `partial_rejected`
#: (patay ang sibling) AT mula sa `partial_indeterminate` (HINDI ALAM). Iisa
#: lang ang `phase` field, kaya hindi mapaghihiwalay ang dalawang pinagmulan —
#: at ang konserbatibong sagot sa short flip ay ang mahal na sagot.
SIBLING_RESOLVED_PHASES: Final[frozenset[str]] = frozenset({
    "partial_filled",
    "partial_rejected",
    "partial_stale_adopted",
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
#: Apat na panuntunan ang sinusunod ng talahanayan (lahat ay may test):
#:   * mula sa bawat NAKED_RISK phase ay BAWAT ABOT na terminal ay nasa
#:     `NAKED_RESOLVING_TERMINALS` — bawat isa, hindi isa lamang (tingnan ang
#:     `reachable_terminals` at ang paliwanag sa itaas ng set na iyon);
#:   * ang BAWAT NAKED_RISK phase ay may TUWIRANG `flatten_queued` na edge, kaya
#:     ang huling remedyo ay hindi kailanman nangangailangan munang mag-record
#:     ng resultang pang-broker na hindi pa nangyayari;
#:   * ang `consumed_by_exit` ay abot mula sa bawat phase kung saan hindi
#:     hinaharang ang whole exit — dahil doon nga puwedeng lamunin ng isang
#:     buong exit ang posisyon habang bukas pa ang marker (L5) — MALIBAN sa
#:     `EXIT_CONSUMPTION_UNRECORDABLE_PHASES`, na may sariling dahilan at
#:     sariling invariant;
#:   * walang non-terminal na phase na patay na kalsada.
#:
#: `MappingProxyType`, hindi `dict` (ikaapat na pasada). Ang `Final` ay isang
#: pahiwatig sa type checker, at walang type checker ang repo na ito
#: (CLAUDE.md: "No ruff/black/mypy configured"). Ang `advance_phase` ay
#: ipinapakilala bilang NAG-IISANG sanctioned na writer, pero ang graph na
#: kinokonsulta niya ay naisusulat noon ng kahit anong nag-i-import:
#: `LEGAL_TRANSITIONS["partial_filled"] = frozenset({"partial_posting"})` at
#: nagiging legal na ang muling pag-POST laban sa posisyong nabawasan na —
#: double fire, na iniulat ng state machine bilang tama. Ang mga halaga ay
#: frozenset na, kaya ang mapping lamang ang kulang.
_LEGAL_TRANSITIONS_TABLE: Final[dict[str, frozenset[str]]] = {
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
    #
    # WALA na ring `-> abandoned` ang lima sa ibaba (ikaapat na pasada). Hubad
    # silang lahat, at ang `abandoned` ay terminal na walang remedyo: ang edge
    # na iyon ay S4 muli. Ang sapilitang labasan ng 300 s na ceiling para sa
    # isang hubad na marker ay `flatten_queued`, hindi `abandoned` — tingnan ang
    # `marker_ceiling_forced_target`.
    "successor_certified": frozenset({
        "partial_posting", "post_deferred", "flatten_queued",
        "consumed_by_exit",
    }),
    "post_deferred": frozenset({
        "partial_posting", "restore_intent_frozen", "flatten_queued",
        "consumed_by_exit",
    }),
    "partial_posting": frozenset({
        "partial_posted", "partial_indeterminate", "partial_rejected",
        "flatten_queued", "consumed_by_exit",
    }),
    "partial_posted": frozenset({
        "partial_filled", "partial_stale_adopted", "flatten_queued",
        "consumed_by_exit",
    }),
    "partial_indeterminate": frozenset({
        "partial_posted", "partial_rejected_final", "flatten_queued",
        "consumed_by_exit",
    }),
    "partial_rejected": frozenset({
        "partial_posting", "partial_rejected_final", "restore_intent_frozen",
        "flatten_queued", "consumed_by_exit",
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

#: Ang pampublikong, HINDI NAISUSULAT na tanawin ng graph.
LEGAL_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    _LEGAL_TRANSITIONS_TABLE
)


def _derive_sibling_reconcile_phases() -> frozenset[str]:
    """Ang buong sakop ng utang na sibling-reconcile, HINANGO sa graph.

    Ang revision 3 ay naglagay ng utang sa `SIBLING_LIVE_PHASES` lamang. Pero
    ang utang ay HINDI natatapos sa paglabas ng set na iyon: ang
    `partial_indeterminate -> partial_rejected_final -> flatten_queued` ay
    dalawang legal na hakbang mula sa "hindi alam kung may nakaupo" patungo sa
    isang BUONG flatten na walang hinihinging reconcile. Ang `le` ay wala pa
    ring `scale_limit_order_id`, kaya ang `_cancel_scale_limit_and_clamp` ay
    noop (LR:18968-18970) at ang flatten ay magpapadala ng buong Q habang
    nakaupo ang f: short flip (S2), sa pamamagitan ng paglalakad palabas ng set
    na eksaktong ginawa para pigilan iyon.

    Ang paghango: magsimula sa `SIBLING_LIVE_PHASES`, sumunod sa bawat legal na
    edge, at HUMINTO sa `SIBLING_RESOLVED_PHASES` — doon lamang napatutunayang
    hindi na nakaupo ang partial. Hindi kasama ang terminal: walang service step
    doon, kaya walang hakbang na maiuutang.
    """
    seen: set[str] = set(SIBLING_LIVE_PHASES)
    stack = list(SIBLING_LIVE_PHASES)
    while stack:
        node = stack.pop()
        for nxt in LEGAL_TRANSITIONS.get(node, frozenset()):
            if nxt in seen or nxt in SIBLING_RESOLVED_PHASES:
                continue
            seen.add(nxt)
            stack.append(nxt)
    return frozenset(seen - TERMINAL_PHASES)


#: Bawat NON-TERMINAL na phase kung saan MAAARING may buhay pang partial sell
#: sa broker. Ito ang tanong ng `requires_sibling_reconcile`.
SIBLING_RECONCILE_PHASES: Final[frozenset[str]] = _derive_sibling_reconcile_phases()


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


def reachable_terminals(start: str) -> frozenset[str]:
    """Bawat TERMINAL na maaaring kalagyan ng marker na ito sa dulo.

    Ito ang tamang tanong para sa R4 (ikaapat na pasada). Ang "may ABOT ba
    akong remedyo" ay pumapasa habang may KATABING landas patungo sa isang
    terminal na walang remedyo — at ang wiring ay maaaring tumahak doon nang
    lubos na legal. Ang tanong ay: SAAN AKO PUWEDENG MAPUNTA SA DULO, at
    tanggap ba ang bawat isa sa mga iyon?
    """
    return reachable_phases(start) & TERMINAL_PHASES


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

    IKAAPAT NA PASADA. Ang tinatanong dito ay ang HINANGONG
    `SIBLING_RECONCILE_PHASES`, hindi na ang `SIBLING_LIVE_PHASES`. Ang utang ay
    nananatili hanggang may phase na TUNAY na nagpapatunay na patay na ang
    sibling (`SIBLING_RESOLVED_PHASES`); ang paglabas lamang ng tatlong live na
    phase ay hindi patunay. Konserbatibo ito nang sinasadya: ang isang
    reconcile na walang mahanap ay isang mumurahing no-op, ang isang nilaktawan
    ay isang short flip.
    """
    return _require_phase(phase, label="phase") in SIBLING_RECONCILE_PHASES


def exit_consumption_precondition(
    phase: str,
    *,
    sibling_reconciled: bool,
) -> bool:
    """Puwede na bang itala ng wiring ang `consumed_by_exit` mula sa `phase`?

    Ang `consumed_by_exit` ay TERMINAL: matapos itong maisulat ay wala nang
    service step na tatakbo pa. Kaya ang pagsulat nito habang may hindi
    naresolbang utang ay hindi paglilinis kundi pagtatapon — ang utang ay hindi
    nababayaran, ito ay NAWAWALA. Dalawang kondisyon:

    * ang `phase` ay hindi nasa `EXIT_CONSUMPTION_UNRECORDABLE_PHASES` (may
      nakaupong order na hindi natin alam kung kanino: ghost/zombie); at
    * kung `requires_sibling_reconcile(phase)`, dapat NAKATAKBO NA ang reconcile
      sa ulo ng chokepoint at ipinapasa rito ang resulta nito.

    Ang `advance_phase` ay hindi kayang tanungin ito — walang argumento ang
    isang graph edge — kaya ito ang lugar kung saan pinapatunayan ng wiring na
    naibayad ang utang bago ito maging terminal.
    """
    src = _require_phase(phase, label="phase")
    if "consumed_by_exit" not in LEGAL_TRANSITIONS.get(src, frozenset()):
        return False
    if src in EXIT_CONSUMPTION_UNRECORDABLE_PHASES:
        return False
    if src in SIBLING_RECONCILE_PHASES and not bool(sibling_reconciled):
        return False
    return True


def marker_ceiling_exceeded(
    unresolved_age_seconds: float | None,
    *,
    ceiling_seconds: float = MARKER_UNRESOLVED_CEILING_SECONDS,
) -> bool:
    """Lumagpas na ba ang marker sa kabuuang wall-clock na hangganan nito?

    Hindi kilalang edad => False (hindi tayo nag-a-abandon sa haka-haka).
    True => sapilitan ang phase na ibinabalik ng `marker_ceiling_forced_target`.
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


def marker_ceiling_forced_target(phase: str) -> str | None:
    """Saan sapilitang isusulong ang marker kapag lumagpas ito sa 300 s?

    IKAAPAT NA PASADA. Ang revision 3 ay nag-utos ng "sapilitan ang `abandoned`"
    sa tatlong lugar (module, disenyo §3.6), pero ang `abandoned` ay walang
    papasok na edge mula sa 11 sa 13 na naked phase. Ang isang wiring na
    susunod sa naipadalang utos ay makakakuha ng `PhaseError` sa LOOB ng service
    step — at ayon sa D4/D3 ang exception doon ay nagbubunga ng "isang tunay na
    patay na stop na iniulat bilang protected: True". Ang anti-wedge na ceiling
    ay hindi maisasagawa nang eksakto sa kasong gumagawa ng mahahabang marker:
    ang limit na nakaupo lang at hindi kailanman na-trade (ang kaso ng CANF).

    Ang lunas ay ang pagkilala na DALAWANG magkaibang bagay ang tinatawag na
    "abandon":

    * PRE-certification / containment lineage (`intent_frozen`,
      `replace_submitted`, `replace_indeterminate`, `replace_stuck`,
      `containment_queued`) — buo pa ang Q stop, walang hubad na share. Doon ang
      `abandoned` ang tamang wakas: isinusuko natin ang lineage, hindi ang takip.
    * anumang HUBAD na phase — may share na walang stop. Doon ang `abandoned` ay
      pag-abandona sa POSISYON. Ang sapilitang labasan ay `flatten_queued`, na
      siya ring sinasabi ng §3.2 ("any NAKED phase -> flatten_queued") at ngayon
      ay may tuwirang edge mula sa bawat isa sa kanila.

    Nagbabalik ng `None` kapag TERMINAL na ang phase (wala nang isusulong) o
    kapag nasa `flatten_queued` na ito (nasa remedyo na mismo).
    """
    src = _require_phase(phase, label="phase")
    if src in TERMINAL_PHASES or src == "flatten_queued":
        return None
    target = "flatten_queued" if src in NAKED_RISK_PHASES else "abandoned"
    # Hindi ito puwedeng maging pangako lamang: kung hindi ito tinatanggap ng
    # graph ay ang mismong depekto ito na inaayos ng helper na ito.
    advance_phase(src, target)
    return target


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


def _maybe_float(value: Any) -> float | None:
    """Coercion na TUMATANGGI, hindi nagre-raise.

    IKAAPAT NA PASADA. Ang bawat pampublikong helper dito ay tinatawag sa
    landas ng proteksyon, at ang `broker_recon_status` NULL /
    `history_unavailable` ay hindi haka-haka sa repo na ito — iyon ang 09-02 na
    loss-guard landmine. Ang isang hubad na `float(None)` sa gitna ng gate na
    iyon ay isang exception na tumatakas, hindi isang hatol na kayang basahin ng
    tumatawag. Ito ang patakaran na sinusunod na ng `blocks_whole_exit` at ng
    `marker_ceiling_exceeded`.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def plan_replacement_edge(
    *,
    total_qty: float,
    partial_qty: float,
    predecessor_filled_size: float,
    open_partial_qty: float = 0.0,
    whole_shares: bool = True,
) -> SplitPlan:
    """R = Q - f, na may mga tseke na kailangan BAGO gumalaw ang broker.

    Tinatanggihan: hindi mabasa o hindi finite, Q <= 0, f <= 0, f >= Q (walang
    runner — iyon ay whole exit, hindi partial), para sa equity ang hindi buong
    share sa alinman sa dalawang binti, ang PREDECESSOR NA MAY FILL, at ang
    NAKAUPO NANG SELL AUTHORITY.

    * **Predecessor na may fill** (R0-10). Ang `partially_filled` ay NASA
      `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES` (LR:9200-9207), kaya "buhay" pa rin
      ang tingin dito ng maintenance; ang PATCH sa isang stop na pumutok na nang
      bahagya ay muling nag-aawtorisa ng share na NAIBENTA NA.
    * **`open_partial_qty`** (ikaapat na pasada). Ang tseke sa itaas ay tumitingin
      lamang sa fill ng PREDECESSOR at hindi sa kung ano pang SELL ang nakaupo.
      Kapag may naunang PATH B partial (o isang legacy `scale_limit`) na nakaupo
      pa, ang `R + f + open` ay maaaring lumampas sa Q: dalawang sell authority
      sa parehong share, at oversell kapag pumutok pareho. Ang `assess_protection`
      lamang ang kayang makakita niyon noon, at walang nag-uugnay sa planner
      dito — kaya ang planner ay nagsasabing `ok=True` sa isang edge na alam ng
      sariling module na mapanganib.
    """
    q = _maybe_float(total_qty)
    f = _maybe_float(partial_qty)
    pf = _maybe_float(predecessor_filled_size)
    op = _maybe_float(open_partial_qty)
    bad = SplitPlan(
        total_qty=q if q is not None else 0.0,
        partial_qty=f if f is not None else 0.0,
        successor_qty=0.0,
        ok=False,
    )
    if q is None or f is None:
        return SplitPlan(**{**bad.__dict__, "reason": "quantity_unreadable"})
    if pf is None:
        return SplitPlan(**{**bad.__dict__, "reason": "predecessor_fill_unreadable"})
    if op is None:
        return SplitPlan(**{**bad.__dict__, "reason": "open_partial_unreadable"})
    if not all(math.isfinite(v) for v in (q, f, pf, op)):
        return SplitPlan(**{**bad.__dict__, "reason": "non_finite_quantity"})
    if pf < 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "predecessor_fill_negative"})
    if pf > 1e-9:
        return SplitPlan(
            **{**bad.__dict__, "reason": "predecessor_partially_filled"}
        )
    if op < 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "open_partial_negative"})
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
    if (r + f + op) > q + _tol(q, r, f, op):
        return SplitPlan(**{**bad.__dict__, "reason": "oversell_after_split"})
    return SplitPlan(total_qty=q, partial_qty=f, successor_qty=r, ok=True)


def plan_restore_edge(
    *,
    broker_qty: float,
    open_partial_qty: float,
    predecessor_qty: float,
    predecessor_filled_size: float,
    whole_shares: bool = True,
) -> SplitPlan:
    """Ang IKALAWANG edge: ibalik ang takip sa `broker_qty - open_partial_qty`.

    IKAAPAT NA PASADA. Wala itong katumbas noon, at iyon ang butas. Sinasabi ng
    §3.4b na "ang isang `partially_filled` na predecessor ay hindi kailanman
    hinahati" — walang kondisyon — pero ang tanging helper na nagpapatupad niyon
    ay ang `plan_replacement_edge`, at ang restore edge ay HINDI KAYANG tumawag
    doon: ang sizing nito ay may `f = 0`, na `non_positive_partial` agad. Kaya
    ang panuntunan ay walang nagpapatupad sa mismong landas ng remedyo.

    Ang konkretong pinsala: nasa `partial_rejected_final` ang marker, saka
    pumutok ang R = 249 na stop at bahagyang napunan nang j = 80. Ang
    `partially_filled` ay "buhay" pa rin sa mata ng lifecycle gate, kaya ang
    restore ay magpi-PATCH ng MISMONG order na iyon pataas mula 249 patungong
    `broker_qty` — muling inaawtorisa ang 80 na naibenta na. Iyon ang kaparehong
    uri ng short flip na isinara ng R0-10, sa landas kung saan hindi ito
    binabantayan.

    Ang edge na ito ay LUMALAKI ayon sa kahulugan (ibinabalik ang takip), kaya
    ang `marker_successor_envelope` ay kailangang tawagin nang may
    `edge_kind="restore"`.
    """
    b = _maybe_float(broker_qty)
    op = _maybe_float(open_partial_qty)
    pq = _maybe_float(predecessor_qty)
    pf = _maybe_float(predecessor_filled_size)
    bad = SplitPlan(
        total_qty=b if b is not None else 0.0,
        partial_qty=0.0,
        successor_qty=0.0,
        ok=False,
    )
    if b is None or op is None or pq is None:
        return SplitPlan(**{**bad.__dict__, "reason": "quantity_unreadable"})
    if pf is None:
        return SplitPlan(**{**bad.__dict__, "reason": "predecessor_fill_unreadable"})
    if not all(math.isfinite(v) for v in (b, op, pq, pf)):
        return SplitPlan(**{**bad.__dict__, "reason": "non_finite_quantity"})
    if pf < 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "predecessor_fill_negative"})
    # R0-10, ngayon ay sa DALAWANG edge at hindi na sa isa lamang.
    if pf > 1e-9:
        return SplitPlan(
            **{**bad.__dict__, "reason": "predecessor_partially_filled"}
        )
    if op < 0.0 or pq < 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "negative_quantity"})
    if b <= 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "non_positive_broker_qty"})
    r2 = b - op
    if r2 <= 0.0:
        return SplitPlan(**{**bad.__dict__, "reason": "restore_leaves_nothing"})
    if whole_shares:
        if abs(r2 - round(r2)) > 1e-9:
            return SplitPlan(
                **{**bad.__dict__, "reason": "fractional_successor_quantity"}
            )
        if round(r2) < 1:
            return SplitPlan(**{**bad.__dict__, "reason": "leg_below_one_share"})
    # Ang restore ay pagpapalaki. Ang pagpapaliit o pagkakapantay ay ibang edge
    # (o isang walang saysay na PATCH), at ang envelope ay tatanggi rin doon.
    if r2 <= pq + _tol(r2, pq):
        return SplitPlan(**{**bad.__dict__, "reason": "restore_is_not_growing"})
    return SplitPlan(total_qty=b, partial_qty=0.0, successor_qty=r2, ok=True)


def marker_successor_envelope(
    *,
    predecessor_order_request: dict[str, Any],
    successor_client_order_id: str,
    successor_qty: float,
    edge_kind: str = "shrink",
) -> dict[str, Any] | None:
    """Ang envelope na dapat ipakita sa lineage matcher para sa isang marker edge.

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

    DALAWANG URI NG EDGE (ikaapat na pasada). Ang revision 3 ay tumatanggi sa
    BAWAT edge na hindi nagpapaliit — kasama ang tanging dalawang remedyo sa
    isang hubad na natitira, na PAREHONG lumalaki:

    * ang R4 restore edge — `successor_qty = broker_qty - open_partial_qty`
      laban sa isang predecessor na nakaupo para sa R = Q - f. Mula sa
      `partial_rejected_final` (k = 0, walang bukas na partial) iyon ay Q laban
      sa R: LUMALAKI nang eksaktong f;
    * ang §3.4c pyramid re-cover — `Q + a - f > Q - f`.

    Kapag walang envelope para sa kanila ay babalik ang wiring sa hugis na
    predecessor-copy (`base_size` = R), at ang `_owner_transport_order_matches`
    ay babagsak sa `|Q - R| > tol` sa bawat pulse:
    `replacement_deadman_successor_lineage_unproven` magpakailanman. Ang
    marker ay uupo sa `restore_replace_submitted` — HUBAD — hanggang mag-expire
    ang lease, saka `flatten_queued`. Ang remedyo ng R4 ay tahimik na
    nagiging "palaging i-flatten ang runner", ang mismong resulta na sinasabi ng
    §3.9 na binura sa bawat post-certification na landas.

    `edge_kind="shrink"` (edge 1: Q -> R) o `"restore"` (edge 2: R -> Q - open).
    Ang pagkakapantay ay tinatanggihan sa PAREHO — iyon ay walang saysay na
    PATCH — at ang maling direksyon ay tinatanggihan sa pareho, kaya ang
    parameter ay hindi isang pagpapaluwag kundi isang pagpapangalan.
    """
    if not isinstance(predecessor_order_request, dict):
        return None
    kind = str(edge_kind or "").strip()
    if kind not in ("shrink", "restore"):
        return None
    cid = str(successor_client_order_id or "").strip()
    if not cid:
        return None
    qty = _maybe_float(successor_qty)
    if qty is None or not math.isfinite(qty) or qty <= 0.0:
        return None
    predecessor_qty = _maybe_float(predecessor_order_request.get("base_size"))
    if (
        predecessor_qty is None
        or not math.isfinite(predecessor_qty)
        or predecessor_qty <= 0.0
    ):
        return None
    tol = _tol(qty, predecessor_qty)
    if abs(qty - predecessor_qty) <= tol:
        return None
    if kind == "shrink" and qty > predecessor_qty:
        return None
    if kind == "restore" and qty < predecessor_qty:
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
        """May hubad na natitira AT may ganang mag-restore (walang oversell)."""
        return self.naked_downside_qty > 0.0 and self.oversell_ok

    @property
    def requires_flatten_now(self) -> bool:
        """Oversell: dalawang sell authority sa parehong share. Ang restore ay
        magpapalala; flatten lamang ang tamang sagot."""
        return self.status == "oversell_risk"

    @property
    def requires_remedy(self) -> bool:
        """ANG NAG-IISANG predicate na dapat i-gate ng isang wiring.

        IKAAPAT NA PASADA. Ang `requires_restore_or_flatten` ay nagtatapos sa
        `and self.oversell_ok`, kaya nagbabalik ito ng **False** sa isang estado
        na may 66 na hubad na share basta't may oversell ding nangyayari — ang
        mismong estado na PINAKAMASAMA sa dalawa. Ang pangalan ay nangangako ng
        "restore O flatten" at ang flatten ang sagot doon, kaya ang False ay
        mali kahit sa sarili nitong pangalan. Hindi ito napansin dahil ang bawat
        test ng `assess_protection` ay may k = 0.

        Hindi pinapalitan ang lumang property (may nakasulat na kahulugan ito:
        "puwede pa bang mag-restore?"). Ito ang idinagdag: "may utang ba akong
        ANUMANG remedyo?" — at `invalid` ay bilang utang, hindi bilang malinis.
        """
        return not self.ok


def open_partial_qty_from_marker(
    *,
    partial_quantity: float,
    partial_cum_filled: float,
) -> float | None:
    """Ang `open_partial_qty` mula sa DALAWANG field na tunay na itinatago ng
    marker (§3.1). Nagbabalik ng `None` kapag hindi mabasa.

    IKAAPAT NA PASADA. Ang `assess_protection` ay tumatanggap ng iisang
    PRE-NETTED na `p`, pero ang schema ng marker ay may `partial_quantity: f` at
    `partial_cum_filled: k` — walang open/leaves. Ang pinakamalapit na field ay
    `f`, at ang pagpapakain niyon matapos mapunan ang k ay nagbubunga ng
    TAHIMIK na maling sagot sa magkabilang panig::

        f = 106, k = 40, broker = 315, stop = 249
        assess_protection(315, 249, open_partial_qty=106)
            -> oversell_risk (MALI: 249 + 66 = 315, walang oversell)
            -> requires_restore_or_flatten = False (MALI: 66 ang hubad)
        assess_protection(315, 249, open_partial_qty=66)
            -> naked_downside, requires_restore_or_flatten = True (TAMA)

    Ito ang kaparehong depekto ng sobrang pagbabawas na inilalarawan ng §3.7/5
    para sa `le["scale_limit_qty"]`, na naulit sa schema ng marker at sa
    signature ng naipadalang checker. Ang `conservation_holds` ay tumatanggap ng
    `f` at `k` nang hiwalay at tama; ito ang katumbas nito para sa checker.
    """
    f = _maybe_float(partial_quantity)
    k = _maybe_float(partial_cum_filled)
    if f is None or k is None:
        return None
    if not (math.isfinite(f) and math.isfinite(k)):
        return None
    if f < 0.0 or k < 0.0 or k > f + _tol(f):
        return None
    return max(0.0, f - k)


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

    Ang `open_partial_qty` ay ang BUKAS (`f - k`) na bilang, hindi ang orihinal
    na `f` — tingnan ang `open_partial_qty_from_marker`.

    Hindi kailanman nagre-raise: ang hindi mabasang input ay `status="invalid"`,
    `ok=False`, `requires_remedy=True`. Ang `broker_qty=None` ay tunay na estado
    dito (`broker_recon_status` NULL / `history_unavailable`), at ang isang
    TypeError sa loob ng naked-risk gate ay isang gate na hindi tumakbo.
    """
    invalid = ProtectionVerdict(
        status="invalid",
        oversell_ok=False,
        naked_downside_qty=0.0,
        unhedged_qty_with_resting_sell=0.0,
        ok=False,
    )
    b = _maybe_float(broker_qty)
    s = _maybe_float(stop_qty)
    p = _maybe_float(open_partial_qty)
    if b is None or s is None or p is None:
        return invalid
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
    vals = [
        _maybe_float(v)
        for v in (broker_qty, successor_qty, partial_qty, partial_cum_filled)
    ]
    if any(v is None for v in vals):
        return False
    b, r, f, k = vals  # type: ignore[misc]
    if not all(math.isfinite(v) for v in (b, r, f, k)):
        return False
    if r < 0.0 or f < 0.0 or k < 0.0 or k > f + _tol(f):
        return False
    unsold = f - k
    return abs(b - (r + unsold)) <= _tol(b, r, unsold)
