"""GAP-B — TIGHT false-breakout-reversal / VWAP-reclaim entry (chase-guard parity).

Two of Ross's highest-conviction reclaim setups that the pullback-break gate doesn't
model: the FAILED-breakdown reversal ("bear trap": price knifes below an obvious
support / prior low, traps the shorts, then snaps back above it) and the VWAP-RECLAIM
("it lost VWAP, washed out, reclaimed VWAP — now I'm long again"). Both only earn the
trade when the consolidation is TIGHT — a coiled, low-range base — because a tight base
is what makes the reversal explosive and the stop small.

Eligibility (ALL required, in this order):
  * **TIGHT** — the recent range compression is below the ``theta_c`` PERCENTILE of the
    instrument's own recent compression history (self-relative; a calm name and an
    explosive name each measured against their OWN base, no fixed range magic number).
  * **flow_ok** — order-flow tape is present and supportive. FAIL-CLOSED: stale / missing
    tape => NOT ok (this is the entry side; a reclaim with no live buyers is a trap).
  * **vol_ok** — the reclaim bar carries a volume spike (a FLOOR, not a magic cutoff).

Then exactly ONE setup geometry fires per tick (mutually exclusive):
  * **false_break_reversal** — pierced below the trap level, then reclaimed it.
  * **vwap_reclaim** — lost VWAP, then reclaimed it (and is not the false-break case).

Chase-guard parity (the standing rule from memory ``project_momentum_chase_guard_parity``:
EVERY breakout entry trigger must carry all four guards, fail-closed where load-bearing):
  1. **tape** — REQUIRED, fail-CLOSED (no live tape => block).
  2. **extension** — block a chase that's already over-extended above the reclaim level.
  3. **backside** — block a below-VWAP / backside-of-the-move reclaim (front-side only).
  4. **structural stop** — a finite, tight structural stop must exist (the trap low /
     reclaimed VWAP) — no stop => no trade.

Flag-OFF (``enabled=False``) => the gate returns ``(False, "disabled", ...)`` and the
live runner falls through to its existing pullback-break trigger unchanged — byte-identical.

All functions are pure and side-effect-free for replay + unit testing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


def compression_percentile(compression_now: float, compression_history: list[float]) -> float | None:
    """Percentile rank (0..1) of the CURRENT compression within the name's own history.

    Lower = tighter than usual. Self-relative, so a calm large-cap and an explosive
    low-float are each measured against their OWN base — no fixed range threshold.
    Returns ``None`` when there isn't enough history to rank (caller fails open/closed
    per its policy). ``compression`` here is a range/spread measure: smaller = tighter.
    """
    hist = [float(h) for h in (compression_history or []) if h is not None and math.isfinite(float(h))]
    if len(hist) < 3:
        return None
    c = float(compression_now)
    below = sum(1 for h in hist if h < c)
    equal = sum(1 for h in hist if h == c)
    return (below + 0.5 * equal) / len(hist)


def is_tight(compression_now: float, compression_history: list[float], theta_c: float) -> bool:
    """TIGHT when the current compression sits BELOW the ``theta_c`` percentile of the
    name's own compression history. Insufficient history => NOT tight (fail-closed:
    we won't call a base tight without evidence)."""
    pct = compression_percentile(compression_now, compression_history)
    if pct is None:
        return False
    return pct < max(0.0, min(1.0, float(theta_c)))


@dataclass(frozen=True)
class ChaseGuards:
    """The four chase-guards, each a pass/fail with a reason. Parity contract: all four
    are ALWAYS evaluated for every entry trigger (no guard may be silently skipped)."""

    tape_ok: bool
    extension_ok: bool
    backside_ok: bool
    structural_stop_ok: bool
    reasons: dict[str, str] = field(default_factory=dict)

    def all_pass(self) -> bool:
        return self.tape_ok and self.extension_ok and self.backside_ok and self.structural_stop_ok

    def first_failure(self) -> str | None:
        if not self.tape_ok:
            return "tape"
        if not self.extension_ok:
            return "extension"
        if not self.backside_ok:
            return "backside"
        if not self.structural_stop_ok:
            return "structural_stop"
        return None


def evaluate_chase_guards(
    *,
    tape_present: bool,
    tape_supportive: bool,
    price: float,
    reclaim_level: float,
    extension_atr_mult: float,
    atr: float | None,
    vwap: float | None,
    structural_stop: float | None,
) -> ChaseGuards:
    """Evaluate all four chase-guards. tape is REQUIRED + fail-CLOSED.

    * tape: present AND supportive (missing/stale tape => fail-closed block).
    * extension: price must not be more than ``extension_atr_mult`` ATR above the reclaim
      level (don't chase a move that already ran). Unknown ATR => fail-OPEN (can't judge
      extension without a yardstick, and the structural stop still bounds risk).
    * backside: front-side only — price must be at/above VWAP (a below-VWAP reclaim is
      the backside of the move). Unknown VWAP => fail-OPEN.
    * structural_stop: a finite, positive stop below price must exist (the trap low /
      reclaimed VWAP). No stop => block.
    """
    reasons: dict[str, str] = {}

    tape_ok = bool(tape_present) and bool(tape_supportive)
    reasons["tape"] = "ok" if tape_ok else ("stale_or_missing" if not tape_present else "unsupportive")

    if atr is not None and float(atr) > 0 and reclaim_level > 0:
        ceiling = float(reclaim_level) + float(extension_atr_mult) * float(atr)
        extension_ok = float(price) <= ceiling
        reasons["extension"] = "ok" if extension_ok else "over_extended"
    else:
        extension_ok = True
        reasons["extension"] = "no_atr_fail_open"

    if vwap is not None and float(vwap) > 0:
        backside_ok = float(price) >= float(vwap)
        reasons["backside"] = "ok" if backside_ok else "below_vwap_backside"
    else:
        backside_ok = True
        reasons["backside"] = "no_vwap_fail_open"

    if structural_stop is not None and float(structural_stop) > 0 and float(structural_stop) < float(price):
        structural_stop_ok = True
        reasons["structural_stop"] = "ok"
    else:
        structural_stop_ok = False
        reasons["structural_stop"] = "no_finite_stop_below_price"

    return ChaseGuards(
        tape_ok=tape_ok,
        extension_ok=extension_ok,
        backside_ok=backside_ok,
        structural_stop_ok=structural_stop_ok,
        reasons=reasons,
    )


def detect_false_break_reversal(
    *,
    trap_level: float,
    bar_low: float,
    bar_close: float,
    reclaim_tol: float = 0.0,
) -> bool:
    """Failed-breakdown geometry: the bar PIERCED below the trap level (``bar_low <
    trap_level``) but CLOSED back above it (``bar_close >= trap_level * (1 - tol)``) —
    the shorts are trapped. Requires a real pierce, not just a close above support."""
    if trap_level <= 0:
        return False
    pierced = float(bar_low) < float(trap_level)
    reclaimed = float(bar_close) >= float(trap_level) * (1.0 - max(0.0, float(reclaim_tol)))
    return pierced and reclaimed


def detect_vwap_reclaim(
    *,
    vwap: float | None,
    prev_close: float,
    bar_close: float,
    reclaim_tol: float = 0.0,
) -> bool:
    """VWAP-reclaim geometry: the PRIOR close was below VWAP (it had lost VWAP) and the
    current bar CLOSED back at/above VWAP. Unknown VWAP => not a reclaim."""
    if vwap is None or float(vwap) <= 0:
        return False
    lost = float(prev_close) < float(vwap)
    reclaimed = float(bar_close) >= float(vwap) * (1.0 - max(0.0, float(reclaim_tol)))
    return lost and reclaimed


@dataclass(frozen=True)
class TightEntryDecision:
    ok: bool
    reason: str
    setup: str | None              # "false_break_reversal" | "vwap_reclaim" | None
    guards: ChaseGuards | None
    debug: dict[str, Any] = field(default_factory=dict)


def evaluate_tight_false_break_entry(
    *,
    enabled: bool,
    compression_now: float,
    compression_history: list[float],
    theta_c: float,
    tape_present: bool,
    tape_supportive: bool,
    vol_ratio: float,
    vol_spike_floor: float,
    price: float,
    vwap: float | None,
    prev_close: float,
    bar_low: float,
    bar_close: float,
    trap_level: float,
    atr: float | None,
    extension_atr_mult: float,
    structural_stop: float | None,
    reclaim_tol: float = 0.0,
) -> TightEntryDecision:
    """The full TIGHT false-break / VWAP-reclaim entry gate with chase-guard parity.

    Returns ``(ok, reason, setup, guards, debug)``. ``enabled=False`` => disabled (the
    live runner falls through to its existing trigger — byte-identical). Eligibility
    (TIGHT -> flow_ok -> vol_ok) is checked before geometry; exactly ONE geometry may
    fire per tick (mutually exclusive: false-break takes precedence over vwap-reclaim);
    then all four chase-guards must pass.
    """
    debug: dict[str, Any] = {}

    if not enabled:
        return TightEntryDecision(ok=False, reason="disabled", setup=None, guards=None, debug=debug)

    # 1. TIGHT eligibility.
    pct = compression_percentile(compression_now, compression_history)
    debug["compression_pct"] = None if pct is None else round(pct, 4)
    if not is_tight(compression_now, compression_history, theta_c):
        return TightEntryDecision(ok=False, reason="not_tight", setup=None, guards=None, debug=debug)

    # 2. flow_ok — FAIL-CLOSED on stale/missing tape (entry side).
    if not bool(tape_present):
        return TightEntryDecision(ok=False, reason="flow_stale_fail_closed", setup=None, guards=None, debug=debug)
    if not bool(tape_supportive):
        return TightEntryDecision(ok=False, reason="flow_not_ok", setup=None, guards=None, debug=debug)

    # 3. vol_ok — reclaim bar volume spike (a FLOOR).
    debug["vol_ratio"] = round(float(vol_ratio), 3)
    if float(vol_ratio) < float(vol_spike_floor):
        return TightEntryDecision(ok=False, reason="vol_below_floor", setup=None, guards=None, debug=debug)

    # 4. Geometry — mutually exclusive, false-break takes precedence.
    is_false_break = detect_false_break_reversal(
        trap_level=trap_level, bar_low=bar_low, bar_close=bar_close, reclaim_tol=reclaim_tol
    )
    is_vwap_reclaim = detect_vwap_reclaim(
        vwap=vwap, prev_close=prev_close, bar_close=bar_close, reclaim_tol=reclaim_tol
    )
    if is_false_break:
        setup = "false_break_reversal"
        reclaim_level = float(trap_level)
    elif is_vwap_reclaim:
        setup = "vwap_reclaim"
        reclaim_level = float(vwap) if vwap is not None else float(bar_close)
    else:
        return TightEntryDecision(ok=False, reason="no_reversal_geometry", setup=None, guards=None, debug=debug)
    debug["setup"] = setup

    # 5. Chase-guard parity — all four always evaluated.
    guards = evaluate_chase_guards(
        tape_present=tape_present,
        tape_supportive=tape_supportive,
        price=price,
        reclaim_level=reclaim_level,
        extension_atr_mult=extension_atr_mult,
        atr=atr,
        vwap=vwap,
        structural_stop=structural_stop,
    )
    debug["guard_reasons"] = guards.reasons
    if not guards.all_pass():
        return TightEntryDecision(
            ok=False, reason=f"guard_block_{guards.first_failure()}", setup=setup, guards=guards, debug=debug
        )

    return TightEntryDecision(ok=True, reason="tight_entry_ok", setup=setup, guards=guards, debug=debug)
