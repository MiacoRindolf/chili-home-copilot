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
Ang buong disenyo, kasama ang APAT NA DAHILAN kung bakit ipinagpaliban ang
wiring, ay nasa `docs/DESIGN/PARTIAL_EXIT_PATH_B.md`.

BAKIT PURO. Ang tatlong bagay na kaya nating patunayan nang WALANG DB at
WALANG broker ay: (a) legal ba ang isang phase transition, (b) legal ba ang
hati ng qty, (c) protektado pa ba ang natitirang posisyon. Ang mga iyon ang
laman dito. Ang lahat ng iba pa sa disenyo — ang claim CAS, ang lineage, ang
chokepoint — ay may I/O at hindi mapapatunayan ng fake, kaya wala rito.

Walang import mula sa `live_runner` (o kahit anong may I/O) ANG BUONG FILE:
iyon ang nagpapanatiling puro at mabilis i-test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

__all__ = [
    "PHASES",
    "IN_FLIGHT_PHASES",
    "TERMINAL_PHASES",
    "NAKED_RISK_PHASES",
    "LEGAL_TRANSITIONS",
    "PhaseError",
    "advance_phase",
    "is_in_flight",
    "is_terminal",
    "blocks_whole_exit",
    "SplitPlan",
    "plan_replacement_edge",
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
    # escape mula sa stuck na replace
    "replace_stuck",
    "containment_queued",
    "containment_resolved",
    "replace_reverted",
    # iba pang terminal
    "abandoned",
    "consumed_by_exit",
})

#: Tapos na — walang service step ang tatakbo pa, at bumababa ang whole-exit block.
TERMINAL_PHASES: Final[frozenset[str]] = frozenset({
    "replace_rejected",
    "partial_filled",
    "partial_stale_adopted",
    "restore_certified",
    "restore_rejected",
    "containment_resolved",
    "replace_reverted",
    "abandoned",
    "consumed_by_exit",
})

#: May PATCH na nasa ere: HINDI natin alam kung sino ang may-ari ng stop, kaya
#: hinaharangan ng chokepoint ang whole exit nang ISANG PULSE bawat isa (R2 —
#: kung hindi, mag-fri-freeze ang close handoff laban sa isang `replaced` na
#: predecessor at mamamatay ang LAHAT ng exit path).
IN_FLIGHT_PHASES: Final[frozenset[str]] = frozenset({
    "intent_frozen",
    "replace_submitted",
    "replace_indeterminate",
    "replace_stuck",
    "containment_queued",
    "restore_intent_frozen",
    "restore_replace_submitted",
    "restore_indeterminate",
})

#: Mga phase kung saan MAAARING may bahagi ng posisyon na walang nakaupong stop
#: (R4). Sa mga ito ay obligado ang restore edge o ang full close sa loob ng
#: isang lease window — tingnan ang `assess_protection`.
NAKED_RISK_PHASES: Final[frozenset[str]] = frozenset({
    "partial_rejected",
    "partial_rejected_final",
    "partial_stale_adopted",
    "restore_intent_frozen",
    "restore_replace_submitted",
    "restore_indeterminate",
    "restore_rejected",
})

#: Ang buong legal na graph. Anumang wala rito ay bug — hindi tahimik na
#: pinapayagan (`advance_phase` ay nagre-raise).
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
    "successor_certified": frozenset({
        "partial_posting", "consumed_by_exit", "replace_stuck",
    }),
    "partial_posting": frozenset({
        "partial_posted", "partial_indeterminate", "partial_rejected",
    }),
    "partial_posted": frozenset({
        "partial_filled", "partial_stale_adopted",
    }),
    "partial_indeterminate": frozenset({
        "partial_posted", "partial_rejected_final",
    }),
    "partial_rejected": frozenset({
        "partial_posting", "partial_rejected_final",
    }),
    "partial_rejected_final": frozenset({"restore_intent_frozen"}),
    "partial_stale_adopted": frozenset(),
    "partial_filled": frozenset(),
    "restore_intent_frozen": frozenset({
        "restore_replace_submitted", "restore_indeterminate", "restore_rejected",
    }),
    "restore_replace_submitted": frozenset({
        "restore_certified", "restore_rejected", "replace_stuck",
    }),
    "restore_indeterminate": frozenset({
        "restore_certified", "restore_rejected", "replace_stuck",
    }),
    "restore_certified": frozenset(),
    "restore_rejected": frozenset(),
    "replace_stuck": frozenset({"containment_queued", "replace_reverted"}),
    "containment_queued": frozenset({"containment_resolved"}),
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

    Ang `partial_stale_adopted` ay terminal PARA SA PARTIAL, pero ang
    naked na natitira ay hinahabol ng hiwalay na restore edge — kaya ang
    caller ang naglalagay ng `restore_intent_frozen` bilang BAGONG edge,
    hindi bilang transition mula sa terminal na phase na ito.
    """
    src = _require_phase(current, label="current")
    dst = _require_phase(target, label="target")
    allowed = LEGAL_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        raise PhaseError(f"illegal_transition:{src}->{dst}")
    return dst


def is_terminal(phase: str) -> bool:
    return _require_phase(phase, label="phase") in TERMINAL_PHASES


def is_in_flight(phase: str) -> bool:
    return _require_phase(phase, label="phase") in IN_FLIGHT_PHASES


def blocks_whole_exit(phase: str) -> bool:
    """Dapat bang harangin ng chokepoint ang whole exit nitong pulse na ito?

    Oo lamang habang may PATCH sa ere. Kapag `successor_certified` o
    `partial_*` ay ang SUCCESSOR na ang may-ari, kaya normal ang daloy ng
    whole exit (nililinis muna ng `_cancel_scale_limit_and_clamp` ang sibling,
    gaya ngayon).
    """
    return is_in_flight(phase)


# --------------------------------------------------------------------------
# 2. Ang hati ng qty
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
    whole_shares: bool = True,
) -> SplitPlan:
    """R = Q - f, na may mga tseke na kailangan BAGO gumalaw ang broker.

    Tinatanggihan: hindi finite, Q <= 0, f <= 0, f >= Q (walang runner —
    iyon ay whole exit, hindi partial), at para sa equity ang hindi buong
    share sa alinman sa dalawang binti (ang Alpaca ay tumatanggap lamang ng
    buong share para sa isang resting sell).
    """
    q = float(total_qty)
    f = float(partial_qty)
    bad = SplitPlan(total_qty=q, partial_qty=f, successor_qty=0.0, ok=False)
    if not (math.isfinite(q) and math.isfinite(f)):
        return SplitPlan(**{**bad.__dict__, "reason": "non_finite_quantity"})
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


# --------------------------------------------------------------------------
# 3. Ang invariant checker
# --------------------------------------------------------------------------

def _tol(*values: float) -> float:
    scale = max((abs(v) for v in values), default=0.0)
    return max(1e-6, scale * 1e-6)


@dataclass(frozen=True)
class ProtectionVerdict:
    """Ang sagot sa: takip ba ang posisyon ngayong pulse na ito?"""

    status: str          # covered | flat | naked_remainder | oversell_risk
    naked_qty: float     # share na WALANG nakaupong stop at walang open sell
    ok: bool             # True lamang kapag covered o flat

    @property
    def requires_restore_or_flatten(self) -> bool:
        return self.status == "naked_remainder"


def assess_protection(
    *,
    broker_qty: float,
    stop_qty: float,
    open_partial_qty: float,
) -> ProtectionVerdict:
    """Ang isang tseke na DAPAT tawagin ng bawat wiring sa hinaharap.

    Dalawang bagay ang binabantayan:

    * **oversell / short flip** — `stop_qty + open_partial_qty` ay hindi
      kailanman dapat lumampas sa hawak sa broker. Ang paglampas ay nangangahulugang
      may dalawang sell authority sa parehong share; kapag pumutok pareho ay
      mababaliktad tayo pa-short.
    * **naked remainder** (R4) — `broker_qty - open_partial_qty - stop_qty > 0`
      ay share na walang anumang nakaupong proteksyon. Ang tanging tinatanggap
      na sagot ay isang restore edge o isang full close sa loob ng isang lease
      window; ang pananatili rito ay ang mismong bagay na ipinagbabawal ng
      deadman.

    Ang `flat` ay hiwalay sa `covered` dahil ang zero na posisyon na may
    natitirang open sell ay `oversell_risk`, hindi `flat`.
    """
    b = float(broker_qty)
    s = float(stop_qty)
    p = float(open_partial_qty)
    if not (math.isfinite(b) and math.isfinite(s) and math.isfinite(p)):
        return ProtectionVerdict(status="oversell_risk", naked_qty=0.0, ok=False)
    if s < 0.0 or p < 0.0 or b < 0.0:
        return ProtectionVerdict(status="oversell_risk", naked_qty=0.0, ok=False)
    tol = _tol(b, s, p)
    if s + p > b + tol:
        return ProtectionVerdict(
            status="oversell_risk", naked_qty=0.0, ok=False,
        )
    if b <= tol:
        return ProtectionVerdict(status="flat", naked_qty=0.0, ok=True)
    naked = b - p - s
    if naked > tol:
        return ProtectionVerdict(status="naked_remainder", naked_qty=naked, ok=False)
    return ProtectionVerdict(status="covered", naked_qty=0.0, ok=True)


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
