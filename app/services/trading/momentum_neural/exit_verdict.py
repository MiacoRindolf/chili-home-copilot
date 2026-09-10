"""EXIT VERDICT F -- the tape answers the exit, in PRINTS since the leg's own high (2026-09-10).

PURE MODULE: no I/O, no settings, no clock. Every function takes the tape rows it judges and
returns a dict the receipt carries verbatim. The live wrapper (`live_runner._exit_verdict_tick`)
does the bounded reads and the broker calls; this file is the part a test can run on a
synthetic tape and a replay can run byte-identically.

ANG DOKTRINA (operator, hindi negotiable): "the tick always answers"; "chart = boundary (sell
all), tape = moment (sell part), reclaim = come back"; "no magic numbers"; "no dark flags".

ANG SUKAT. Bawat opinion exit sa 7 araw ay nagpasya sa QUOTE, BAR, WALL CLOCK o scanner score;
16 sa 34 ay pumutok sa n = 0 prints mula sa sariling high ng leg -- nasa high pa ang pangalan
nang umalis tayo. Ang verdict D (prints since the leg's high) ang pinakamahusay na iisang rule;
ang F (bahagi sa D, runner sa ilalim ng tick deadman na nagra-ratchet sa bawat bagong high
print, D2 pagkatapos ng bagong high) ang pinakamahusay sa lahat. Mga numero: tingnan ang
`_EXIT_VERDICT_DERIVATION` sa ibaba -- bawat isa ay galing sa tape, hindi sa hula.

Row shape (the three bounded readers in entry_gates return exactly this, oldest-first):

    (price, size, bid, ask, epoch_seconds, observed_at, id)

Only ``price``/``size``/``bid``/``ask``/``epoch`` reach the feature function (the same
``_signed_tape_features`` the entry confirmer reads); ``observed_at``/``id`` are the tuple
bound the reads resume from, so the high print is identified stably across ticks.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from .entry_gates import _signed_tape_features
from .paper_execution import scale_out_quantity

# ── the two floors, both reported (no new number: the feature's own) ────────────
#: `_signed_tape_features` returns None below this many parsed prints (entry_gates: `if n < 3`).
FEATURE_FLOOR_PRINTS = 3
#: The count-halves fields (`swing_low_prev/now`, `buy_share_delta`) need >= 4 parsed prints
#: (entry_gates: `if len(_px_seq) >= 4`); below it the verdict cannot read a lower low. This is
#: the BINDING floor: measured 0 evaluations bound at n == 3 over the 35-leg re-run.
BINDING_FLOOR_PRINTS = 4
#: Doctrine fallback for the partial fraction ("sell part") when no distribution can be read.
SELL_FRACTION_FALLBACK = 0.5

#: ONE harness of record for every number below: scratchpad/acceptance_exit_verdict_f_0910.py
#: (2026-09-10; the SHIPPED exit_verdict.py on the recorded tape, 35 opinion-exit legs since
#: 09-03, the verdict at every 3.19-s tick = the measured p50 HELD spacing, the deadman walked
#: per print, exits priced at the NBBO bid). The designer's in-memory STEP=100 print-priced
#: re-run (D -232.62 / F(0.5) -152.79 / F(11/31) -129.60) is SUPERSEDED and cited nowhere else.
_EXIT_VERDICT_DERIVATION = (
    "7-day counterfactual 2026-09-10, 35 opinion-exit legs since 09-03, tick-by-tick harness "
    "of record (3.19-s ticks, deadman per print, NBBO-bid priced, N=255): actual -697.87, "
    "D(bid) -502.18, D(print) -380.82, F(11/31) -485.50, F(0.5) -489.25, F(0.75, shipped) "
    "-495.7 by linearity (q*D + (1-q)*R, R = -476.32); brief's 34-leg print-priced scripts: "
    "actual -690.79, D -297.77, F -209; 16/34 exits at n=0 prints since high; tick deadman "
    "-304.93 vs ATR -468.88 (34 legs, print-priced)"
)
_TICK_DEADMAN_BASE_DERIVATION = (
    "tick_deadman_vs_atr_deadman.py:75-83 / f_partial_plus_tick_deadman.py:40-47 "
    "(2026-09-10): the runner's floor is the first of swing_low_prev, swing_low_now, "
    "buy_support_px strictly below the entry, read from the N most recent prints at the "
    "entry fill (N = chili_momentum_g4_reentry_tape_window_prints = 255), delivery-bounded "
    "by the tick (not by the fill instant); tick-by-tick harness of record: a print base on "
    "35/35 legs at N=255 (swing_low_prev 33, swing_low_now 2, resting_stop 0 -- the named "
    "fallback, never exercised there); ratchets per runner p50 1 / p90 1 / max 3; "
    "TICK-structure deadman -304.93 vs ATR/structural -468.88 on the same 34 legs with the "
    "same D verdict on top (print-priced scripts)"
)
_SELL_FRACTION_DERIVATION = (
    "1 - runner_beats_partial_share, share = P(runner leg ended above the partial price, "
    "both at the NBBO bid) = 8/32 over the 32 runner legs of the 35 opinion-exit legs since "
    "09-03 (tick-by-tick harness of record, verdict at every 3.19-s tick, deadman per print, "
    "N=255, 2026-09-10; scratchpad/acceptance_exit_verdict_f_0910.py) => 24/32 = 0.75. The "
    "in-memory STEP=100 print-priced re-run had said 20/31 => 11/31; the tick-by-tick table "
    "wins. EVALUATED at the shipped value: P&L is linear in q (F(q) = q*D + (1-q)*R with "
    "D = -502.18, R = -476.32), so F(0.75) = -495.7 vs F(0.5) -489.25 vs F(11/31) -485.50 -- "
    "the shipped value is the WORST of the three by $6-$10 (inside noise on 35 legs; the "
    "linear form has no interior optimum, q -> 0 = -476.32) while the hit rate says q > 0.5 "
    "(95% Wilson CI of the share [0.13, 0.42] excludes 0.5). The fraction is the verdict's "
    "measured hit rate, not a P&L optimum; the operator decides between the two rules. "
    "Named fallback 0.5 (doctrine: sell part)."
)

# ── the per-leg phase machine (le["exit_verdict"]["phase"]) ─────────────────────
#: Live FSM state NEVER changes for a partial: the phase lives beside it, in the leg dict.
PHASES = (
    "armed",
    "partial_shrink_pending",
    "partial_sell_pending",
    "runner",
    "runner_exit_pending",
    "exited",
)
#: Phases with a broker action outstanding (an order in flight or a shrink in progress).
PENDING_PHASES = frozenset({
    "partial_shrink_pending",
    "partial_sell_pending",
    "runner_exit_pending",
})
TERMINAL_PHASES = frozenset({"exited"})
#: The chandelier / first-target machinery must not run while the verdict machine holds the
#: leg (spec I5). Armed is in the trail-bypass set (a quote/ATR opinion F did not measure).
TRAIL_BYPASS_PHASES = frozenset({
    "armed",
    "partial_shrink_pending",
    "partial_sell_pending",
    "runner",
    "runner_exit_pending",
})
FIRST_TARGET_BYPASS_PHASES = frozenset({
    "partial_shrink_pending",
    "partial_sell_pending",
    "runner",
    "runner_exit_pending",
})
#: (from, to). ``None`` = absent (no marker yet). Any pair not listed raises.
_ALLOWED: frozenset[tuple[str | None, str]] = frozenset({
    (None, "armed"),
    ("armed", "armed"),                         # second distinct reason (idempotent per reason)
    ("armed", "partial_shrink_pending"),        # D fired, can_split
    ("armed", "exited"),                        # cannot split => whole (= D)
    ("partial_shrink_pending", "partial_sell_pending"),
    ("partial_shrink_pending", "armed"),        # shrink refused past the cap, Q stop intact
    ("partial_shrink_pending", "exited"),       # protection unavailable => full close
    ("partial_sell_pending", "runner"),         # f filled
    ("partial_sell_pending", "armed"),          # f order terminal with zero fill
    ("partial_sell_pending", "exited"),         # zero-fill after the cap => whole
    ("runner", "runner"),                       # ratchet
    ("runner", "runner_exit_pending"),
    ("runner", "exited"),                       # resting deadman / EOD / operator / bailout
    ("runner_exit_pending", "exited"),
})


def assert_verdict_transition(frm: str | None, to: str) -> None:
    """Raise ``ValueError`` on any edge the phase table does not list.

    ``any -> exited`` is allowed from every non-terminal phase (bailout, kill switch,
    cancel); ``exited`` itself is terminal. Recycle clears the marker outside the table.
    """
    if to not in PHASES:
        raise ValueError(f"exit_verdict: unknown phase {to!r}")
    if frm is not None and frm not in PHASES:
        raise ValueError(f"exit_verdict: unknown phase {frm!r}")
    if frm in TERMINAL_PHASES:
        raise ValueError(f"exit_verdict: {frm!r} is terminal")
    if to == "exited" and frm is not None:
        return
    if (frm, to) not in _ALLOWED:
        raise ValueError(f"exit_verdict: transition {frm!r} -> {to!r} not allowed")


# ── the leg high, FIRST occurrence at the max (the script's tie rule) ───────────

def _px(row: Sequence[Any]) -> float | None:
    try:
        v = float(row[0])
    except (TypeError, ValueError, IndexError):
        return None
    return v if v > 0 else None


def _at(row: Sequence[Any]) -> Any:
    return row[5] if len(row) > 5 else None


def _id(row: Sequence[Any]) -> Any:
    return row[6] if len(row) > 6 else None


def leg_high_print(rows: Sequence[Sequence[Any]]) -> dict[str, Any] | None:
    """The highest print of ``rows`` (oldest-first), FIRST occurrence on a tied max.

    ``since_high_exit_verdict_on_bailouts.py:36-43``: ``ORDER BY price DESC, observed_at ASC,
    id ASC LIMIT 1`` -- the earliest print at the max, so the since-high window is the WHOLE
    stall, never a shorter one that starts at a later re-test of the same price. This is NOT
    the feature dict's ``prints_since_high`` (newest occurrence, entry_gates: ``_px_seq[::-1]``).
    """
    best: dict[str, Any] | None = None
    for idx, row in enumerate(rows):
        p = _px(row)
        if p is None:
            continue
        if best is None or p > best["price"]:
            best = {"price": p, "observed_at": _at(row), "id": _id(row), "index": idx,
                    "tie_rule": "first_occurrence"}
    return best


# ── the verdict D over the prints SINCE the high ────────────────────────────────

def since_high_verdict(
    rows: Sequence[Sequence[Any]],
    *,
    window_s: float,
    tick_rate_floor_pctile: float,
) -> dict[str, Any]:
    """Verdict D on the prints AFTER the leg's high print (the high itself excluded).

        sellers_took_it = signed_tape_accel < 0      (back half weaker than the front)
                          and buy_share_delta < 0    (aggressor-buy share fell front -> back)
                          and swing_low_now < swing_low_prev   (a LOWER LOW in prints)

    No threshold: the comparisons are against zero and against the tape's own prior swing
    low. ``binding`` names what decided: the floor that held the verdict back, the single
    condition that failed, or ``all_three``. Every field the receipt needs travels in the dict.
    """
    n = len(rows)
    out: dict[str, Any] = {
        "fired": False,
        "binding": None,
        "n_since_high": int(n),
        "signed_tape_accel": None,
        "buy_share_delta": None,
        "swing_low_now": None,
        "swing_low_prev": None,
        "gap_restricted": None,
        "n_ticks": None,
        "window_s": float(window_s),
        "floor_prints": BINDING_FLOOR_PRINTS,
        "feature_floor": FEATURE_FLOOR_PRINTS,
    }
    if n < FEATURE_FLOOR_PRINTS:
        out["binding"] = "feature_floor_n_lt_3"
        return out
    feats = _signed_tape_features(
        rows, window_s=float(window_s), tick_rate_floor_pctile=float(tick_rate_floor_pctile)
    )
    if not isinstance(feats, dict):
        out["binding"] = "feature_none"
        return out
    acc = feats.get("signed_tape_accel")
    bsd = feats.get("buy_share_delta")
    sln = feats.get("swing_low_now")
    slp = feats.get("swing_low_prev")
    out.update({
        "signed_tape_accel": acc,
        "buy_share_delta": bsd,
        "swing_low_now": sln,
        "swing_low_prev": slp,
        "gap_restricted": feats.get("gap_restricted"),
        "n_ticks": feats.get("n_ticks"),
    })
    if None in (acc, bsd, sln, slp):
        n_ticks = feats.get("n_ticks")
        try:
            few = int(n_ticks) < BINDING_FLOOR_PRINTS
        except (TypeError, ValueError):
            few = n < BINDING_FLOOR_PRINTS
        out["binding"] = "count_halves_floor_n_lt_4" if few else "feature_field_missing"
        return out
    failed: list[str] = []
    if not (float(acc) < 0.0):
        failed.append("accel_not_negative")
    if not (float(bsd) < 0.0):
        failed.append("buy_share_delta_not_negative")
    if not (float(sln) < float(slp)):
        failed.append("no_lower_low")
    if failed:
        out["binding"] = failed[0]
        out["failed"] = failed
        return out
    out["fired"] = True
    out["binding"] = "all_three"
    return out


# ── the tick deadman: base at the entry fill, ratchet on every new high print ───

_BASE_KEYS = ("swing_low_prev", "swing_low_now", "buy_support_px")


def tick_deadman_base(
    feats: dict[str, Any] | None,
    *,
    entry_px: float,
    resting_stop: float | None,
) -> tuple[float | None, str]:
    """The runner's floor at the partial: the first of ``swing_low_prev``, ``swing_low_now``,
    ``buy_support_px`` strictly BELOW the entry (read from the N prints at the entry fill);
    fallback the resting broker stop (source ``resting_stop``). `_TICK_DEADMAN_BASE_DERIVATION`.
    """
    try:
        entry = float(entry_px)
    except (TypeError, ValueError):
        entry = None
    if isinstance(feats, dict) and entry is not None:
        for key in _BASE_KEYS:
            v = feats.get(key)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0.0 and fv < entry:
                return fv, key
    if resting_stop is None:
        return None, "none"
    try:
        return float(resting_stop), "resting_stop"
    except (TypeError, ValueError):
        return None, "none"


def tick_deadman_ratchet(level: float | None, cand: Any) -> tuple[float | None, bool]:
    """``level = max(level, cand)`` -- the tick deadman never lowers. Returns (level, moved)."""
    if cand is None:
        return level, False
    try:
        c = float(cand)
    except (TypeError, ValueError):
        return level, False
    if c <= 0.0:
        return level, False
    if level is None or c > float(level):
        return c, True
    return level, False


def walk_runner_prints(
    batch: Sequence[Sequence[Any]],
    *,
    level: float | None,
    runner_high: float | None,
    ratchet_feats: Callable[[Any], dict[str, Any] | None],
) -> dict[str, Any]:
    """Walk the inter-tick batch IN ORDER, per print (f_partial_plus_tick_deadman.py:70-83):

        (a) print <= level          => the tick deadman fires; stop the walk
        (b) print > runner_high     => new high: runner_high = print, saw_new_high = True,
                                       level = max(level, swing_low_prev at that print)

    The deadman wins inside a batch; the second verdict (D2) is judged by the caller AFTER
    the walk, only when ``saw_new_high``. ``ratchet_feats(observed_at)`` is injected (the
    caller's bounded as-of read); this module stays pure.
    """
    lvl = level
    hi = runner_high
    saw_new_high = False
    ratchets: list[dict[str, Any]] = []
    exit_print: dict[str, Any] | None = None
    scanned = 0
    for row in batch:
        p = _px(row)
        if p is None:
            continue
        scanned += 1
        if lvl is not None and p <= float(lvl):
            exit_print = {"price": p, "observed_at": _at(row), "id": _id(row)}
            break
        if hi is None or p > float(hi):
            hi = p
            saw_new_high = True
            feats = None
            try:
                feats = ratchet_feats(_at(row))
            except Exception:
                feats = None
            cand = feats.get("swing_low_prev") if isinstance(feats, dict) else None
            old = lvl
            lvl, moved = tick_deadman_ratchet(lvl, cand)
            if moved:
                ratchets.append({
                    "old_level": old,
                    "new_level": lvl,
                    "new_high_print": {"price": p, "observed_at": _at(row), "id": _id(row)},
                })
    return {
        "exit_print": exit_print,
        "level": lvl,
        "runner_high": hi,
        "saw_new_high": saw_new_high,
        "ratchets": ratchets,
        "prints_scanned": scanned,
    }


# ── the partial split (venue-valid; cannot split => whole = D) ──────────────────

def partial_split(
    *,
    current_qty: float,
    original_qty: float,
    fraction: float,
    base_increment: float | None,
    base_min_size: float | None,
) -> tuple[float, float, bool]:
    """``(f, R, can_split)`` = ``scale_out_quantity`` (paper_execution.py) applied to the
    CURRENT position with ``fraction`` of it -- what F measured. ``can_split`` False ⇒ the
    caller sells whole (= verdict D, the second-best measured rule)."""
    return scale_out_quantity(
        current_qty=current_qty,
        original_qty=original_qty,
        fraction=fraction,
        base_increment=base_increment,
        base_min_size=base_min_size,
    )


def verdict_receipt(v: dict[str, Any] | None) -> dict[str, Any]:
    """The verdict dict as every verdict receipt carries it (stable key set)."""
    v = v if isinstance(v, dict) else {}
    return {
        "fired": bool(v.get("fired")),
        "binding": v.get("binding"),
        "n_since_high": v.get("n_since_high"),
        "signed_tape_accel": v.get("signed_tape_accel"),
        "buy_share_delta": v.get("buy_share_delta"),
        "swing_low_now": v.get("swing_low_now"),
        "swing_low_prev": v.get("swing_low_prev"),
        "gap_restricted": v.get("gap_restricted"),
        "n_ticks": v.get("n_ticks"),
        "window_s": v.get("window_s"),
        "floor_prints": BINDING_FLOOR_PRINTS,
        "feature_floor": FEATURE_FLOOR_PRINTS,
    }
