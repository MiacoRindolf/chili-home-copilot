"""EXIT VERDICT G -- sell INTO the spike, the WHOLE position, on the tape's word (2026-09-10).

PURE MODULE: no I/O, no settings, no clock. Every function takes the tape rows it judges and
returns a dict the receipt carries verbatim. The live wrapper (`live_runner._exit_verdict_tick`)
does the bounded reads and the broker calls; this file is the part a test can run on a
synthetic tape and a replay can run byte-identically.

ANG DOKTRINA (operator, hindi negotiable): "the tick always answers"; "no magic numbers";
"no dark flags"; at, 2026-09-10 18:12Z, "hinayaan bumagsak kesa magbenta sa spike" -- ibenta
SA spike, hindi pagkatapos; 18:35Z, "bakit kalahati lang kung mataas ang accuracy? di ba dapat
LAHAT, tapos bili ulit kapag viable na ulit?" -- LAHAT sa trigger, ang re-entry ay sa entry path.

HISTORICAL MEASUREMENTS below used earlier geometry, print prices and sampling cadence.
They do not validate the current count contract, actual held cadence or executable PnL.
Count support below is half-window minima/buy VWAP, not a completed-pivot detector.

ANG SUKAT (historical script outputs; tingnan ang `_EXIT_VERDICT_DERIVATION`):
  * 35 opinion-exit legs / 7 d, walked from the ENTRY FILL: actual -697.87; F' (half on the
    since-high verdict D, runner under the tick deadman) -202.88; G (half at the first ROLLOVER
    of `signed_tape_accel` while the print is still above entry, else D) -2.32 -- G fired on
    the spike in 11/35 legs and was better in EVERY one; identical to F' on the other 24.
  * EVERY live Alpaca leg of 14 d (78 legs, 20 winners): actual -1,216.28; G-half -59.25;
    G-ALL +157.52 (winners +321.62 / losers -164.11); G-all + one tape-proven reclaim +271.45
    (54 extra round trips at print prices, NOT robust to small-cap spread => re-entry goes
    through the entry path, never a mechanical re-buy). Triggers: spike 28 / D 43 / deadman 7.
  * The runner (Amendment 3, 71 triggered legs): 83% make a NEW HIGH later, but the retrace
    before it is p50 1.03x the spike -- a swing low COMPLETES only after the bounce, so the
    "last completed swing low" right after a sale at the top is the PRE-spike low: late by
    construction. Half + breakeven runner beats sell-all by +$117 at print prices, but 55/71
    runners exit exactly at entry and small-cap slippage (~$3 each) eats it. Sell-all: fewer
    fills, no runner state; the 83% continuation is captured by RE-ENTRY when the tape
    re-proves (a separate PR on the re-entry ramp).

THE RULE (all anchored at the ENTRY FILL; recycle = new leg = new anchor):
  deadman  a print <= the tick deadman level => exit, checked per print, first. [65]: the
           level is set ONCE at the fill = max(resting stop, entry - the median depth of the
           symbol-day's COMPLETED pullback cycles); no pre-trigger ratchet (named fallback
           until completed-swing facts are wired). The old count-half low is receipt context.
  G        `signed_tape_accel` (the N-print window, the same feature D reads) crosses from > 0
           at the previous HELD evaluation to <= 0 now while the LAST PRINT > the entry fill
  D        over the prints SINCE the leg's high print (window = prints, min = the feature's
           own floor): accel < 0 AND buy_share_delta < 0 AND swing_low_now < swing_low_prev
  the EARLIER of G and D (in one tick: deadman, then G, then D) => the WHOLE position at the
  bid through the existing exit seam. ONE fill. No partial, no runner, no second sale.
  `exit_fraction` = 1.0 is a REPORTED binding value (`_EXIT_FRACTION_DERIVATION`), not a knob.

Row shape (the bounded readers in entry_gates return exactly this, oldest-first):

    (price, size, bid, ask, epoch_seconds, observed_at, id)

Only ``price``/``size``/``bid``/``ask``/``epoch`` reach the feature function (the same
``_signed_tape_features`` the entry confirmer reads); ``observed_at``/``id`` are the tuple
bound the reads resume from, so the frontier and the high print are stable across ticks.
"""

from __future__ import annotations

import math
import hashlib
import json
import statistics
from typing import Any, Sequence

from .entry_gates import _signed_tape_features, _TAPE_GAP_DISCONTINUITY_P90_MULT
from .tape_selection import RECORDED_TAPE_SELECTION

# Existing calibrations, reused by the count exit contract; no new setting.
COUNT_FEATURE_CONTRACT = "count_v1"
COUNT_EXIT_SCHEMA = "held_exit_count_v1"
MEASURED_PRINT_AGE_S = 14.69
MEASURED_WINDOW_PRINTS = 255
PRINT_AGE_CALIBRATION = "2026-09-10_8_names_96360_gaps_p99_14.693s_independent_of_tested_window"
GAP_CALIBRATION = "2026-09-10_48_symbol_hours_max_p99_over_nonzero_p90_7.8154_unlabeled_tail_scale"
WINDOW_CALIBRATION = "108_historical_entry_exit_fill_instants_legacy15s_count_p50_255_not_arm_or_exit_optimality"


def count_exit_contract(*, window_prints, print_age_bound_s, gap_mult, tick_rate_floor_pctile):
    """Validate the existing settings and name measured fallbacks (no I/O/clock).

    The fingerprint contains effective semantics, not the tested sample's gaps.
    A new/config-changed geometry cannot compare G with an older feature value.
    """
    sources = {}

    def finite(name, value, default, minimum, maximum=None, integer=False):
        try:
            f = float(value)
            valid = (not isinstance(value, bool) and math.isfinite(f) and f >= minimum
                     and (maximum is None or f <= maximum) and (not integer or f.is_integer()))
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            f = default
        sources[name] = "configured_existing_setting" if valid else "invalid_or_missing_existing_setting_measured_fallback"
        return int(f) if integer else float(f)

    n = finite("window_prints", window_prints, MEASURED_WINDOW_PRINTS, 4, integer=True)
    age = finite("print_age_bound_s", print_age_bound_s, MEASURED_PRINT_AGE_S, .5, 600.)
    mult = finite("gap_mult", gap_mult, _TAPE_GAP_DISCONTINUITY_P90_MULT, 1., 100.)
    floor = finite("tick_rate_floor_pctile", tick_rate_floor_pctile, 0., 0., 1.)
    if sources["tick_rate_floor_pctile"].endswith("measured_fallback"):
        sources["tick_rate_floor_pctile"] = "invalid_or_missing_existing_setting_existing_default"
    semantic = {"schema": COUNT_EXIT_SCHEMA, "feature_contract": COUNT_FEATURE_CONTRACT,
                "selection_contract": RECORDED_TAPE_SELECTION, "split": "count", "window_s": None,
                "window_prints": n, "gap_mult": mult, "print_age_bound_s": age,
                "tick_rate_floor_pctile": floor,
                "G_units": "back_minus_front_positive_aggressor_buy_shares",
                "half_rule": "front=n_ticks//2_back=remainder_after_parse_and_gap_trim",
                "D_window": "all_eligible_prints_strictly_after_first_leg_high",
                "support_semantics": "count_half_minima_and_buy_vwap_not_completed_pivots"}
    fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**semantic, "contract_id": fingerprint, "sources": sources,
            "calibration": {"window_prints": WINDOW_CALIBRATION, "gap_mult": GAP_CALIBRATION,
                            "print_age_bound_s": PRINT_AGE_CALIBRATION},
            "gap_rule": "nonzero_window_gap_p90_times_gap_mult",
            "no_cadence_gap_fallback": "independent_print_age_bound",
            "freshness_rule": "independent_print_age_bound_never_raised_by_window",
            "optimality_or_execution_pnl_claim": False}


def count_feature_receipt(features):
    """Bounded geometry/age receipt, no inline membership or pivot claim."""
    f = features if isinstance(features, dict) else {}
    n = f.get("n_ticks")
    halves = {"front": n // 2, "back": n - n // 2} if isinstance(n, int) and f.get("split") == "count" else None
    return {**{k: f.get(k) for k in (
        "feature_contract", "window_kind", "window_prints", "window_s", "split", "span_s", "n_ticks",
        "gap_restricted", "gap_trim_s", "gap_trim_basis", "gap_trim_window_p90_s", "gap_trim_mult",
        "print_age_s", "print_age_bound_s", "print_stale", "tick_rate_basis",
        "tick_rate_floor_pctile", "available_by", "observed_through")},
        "half_print_counts": halves, "completed_pivot_claim": False}

# ── the two floors, both reported (no new number: the feature's own) ────────────
#: `_signed_tape_features` returns None below this many parsed prints (entry_gates: `if n < 3`).
FEATURE_FLOOR_PRINTS = 3
#: The count-halves fields (`swing_low_prev/now`, `buy_share_delta`) need >= 4 parsed prints
#: (entry_gates: `if len(_px_seq) >= 4`); below it the verdict cannot read a lower low. This is
#: the BINDING floor: measured 0 evaluations bound at n == 3 over the 35-leg re-run.
BINDING_FLOOR_PRINTS = 4
#: The WHOLE position leaves at the trigger. A reported binding value, not a knob (Amendment 2).
EXIT_FRACTION = 1.0

#: The harnesses of record (2026-09-10; scratchpad g_sell_into_spike_accel_rollover.py,
#: g2_monotone_swing_low_ratchet.py, h_sell_all_vs_half_all_legs.py, i_post_spike_structure.py;
#: every walk from the ENTRY FILL, print-priced, the 60-min horizon a measurement bound only).
_EXIT_VERDICT_DERIVATION = (
    "HISTORICAL print-priced script outputs using legacy geometry, not validation of "
    "held_exit_count_v1, actual held cadence or executable PnL. "
    "sell into the spike, ALL, 2026-09-10: 35 opinion-exit legs / 7 d walked from the entry "
    "fill -- actual -697.87, F' (half on the since-high verdict D, runner under the tick "
    "deadman) -202.88, G (half at the first accel rollover while the print > entry, else D) "
    "-2.32; G fired on the spike in 11/35 legs and was better in EVERY one, identical to F' on "
    "the other 24 (g_sell_into_spike_accel_rollover.py). ALL 78 live Alpaca legs / 14 d, "
    "winners included: actual -1,216.28, G-half -59.25, G-ALL +157.52 (20 winners +321.62 / 58 "
    "losers -164.11), G-all + one tape-proven reclaim +271.45 across 54 extra round trips at "
    "print prices (not robust to ~0.5% small-cap spread => re-entry via the entry path); "
    "triggers spike 28 / D 43 / deadman 7 (h_sell_all_vs_half_all_legs.py). Earlier 34-leg "
    "print-priced scripts: 16/34 opinion exits fired at n=0 prints since the leg's high; tick "
    "deadman -304.93 vs ATR deadman -468.88 on the same legs."
)
_EXIT_FRACTION_DERIVATION = (
    "exit_fraction = 1.0: measured 14d (78 live Alpaca legs, winners included, "
    "h_sell_all_vs_half_all_legs.py 2026-09-10): all +157.52 vs half -59.25 vs actual "
    "-1,216.28; sell-all beats half by +217 on the same legs with FEWER fills, the runner loses "
    "money even with the winners in. Amendment 3 (i_post_spike_structure.py, 71 triggered "
    "legs): half + breakeven runner +459.17 vs G-all +341.75 at print prices, but 55/71 "
    "runners exit exactly at entry and ~0.4% small-cap slippage (~$3 each, ~-155) puts the "
    "edge inside the noise. Re-measure when a multi-hour runner day exists in the sample "
    "(none in these 14 days; the runner's case is unmeasured, not refuted). Not a knob."
)
_ACCEL_ROLLOVER_DERIVATION = (
    "Current held_exit_count_v1 uses raw back-minus-front positive aggressor-buy shares "
    "over count halves; this is not physical price acceleration or net aggressor flow. "
    "The following historical legacy-geometry outputs are not current-policy validation. "
    "G trigger (g_sell_into_spike_accel_rollover.py 2026-09-10): signed_tape_accel over the "
    "N most recent prints (the same print-indexed feature the verdict reads; N = "
    "chili_momentum_g4_reentry_tape_window_prints) crosses from > 0 at the previous HELD "
    "evaluation to <= 0 now while the last print > the entry fill. Evaluated on EVERY held tick (the script's 25-print "
    "step is the measurement's resolution, not a rule). Fired on the spike in 11/35 legs "
    "(WYHG 09-08 09:07 -65 -> +9, 09:09 -46 -> +42, MOBX -29 -> -9, TNON x4 +2-3, FTFT +3), "
    "28/78 on the 14-d sample; the script read N=458 (the p50 of the 15-s window of that day) "
    "-- the shipped N is the named setting (p50 at 108 decision instants = 255)."
)
_TICK_DEADMAN_DERIVATION = (
    "[65] 2026-09-11. BASE at the entry fill = max(resting_stop, entry - median(hi - pb_low)) "
    "over the COMPLETED cycles of the symbol-day tape ledger (tape_cycles.PullbackCycleScanner "
    "in le['tape_cycle_state'], pullback_frac 0.50 = CYCLE_PULLBACK_FRAC_BASE): the median depth "
    "of the day's pullbacks the tape CONTINUED from (a cycle completes only when a print takes "
    "out its high after the retrace). resting_stop = position.stop_price, the software bid-stop "
    "(the broker deadman rests a buffer below it and is inert in premarket). Named fallback "
    "resting_stop when the ledger is missing / not caught up / has no completed cycle / the "
    "candidate is not inside (0, entry). NO pre-trigger ratchet: the rolling count-half minimum "
    "is not a completed swing low and raising the floor to it was measured to cost money; the "
    "named fallback holds until completed-swing facts (#1408) are wired. The count-half keys "
    "(swing_low_prev / swing_low_now / buy_support_px over N=255 prints at the fill, the old "
    "base) are receipt context only. MEASURED (scripts/deadman_base_replay_65.py, the scout's t65 tape cache; the "
    "shipped count_v1 features and the shipped scanner; the floor checked on EVERY print, the "
    "software bid-stop at position.stop_price with its 1-s confirm, the broker stop RTH-only, "
    "the C4 viability lifts at their actual event times; G every 25 prints for the first 400 "
    "then 100, D every 100; 60-min horizon; priced at the print / the bid / the bid 15.3 s "
    "later): the old count-half base sat p50 0.23 R (today, n=18) / 0.31 R (14 d, n=79) below "
    "entry (R = entry - position stop) and 43/56 of its 14-d floor exits printed back above "
    "entry within 5 min. 14 d, 81 legs / 34 symbol-days: old base + ratchet -429.64 / -662.00 / "
    "-751.55 -> this base +79.57 / -324.45 / -447.77; paired +509 [+217, +856] print, +338 "
    "[+102, +647] bid, +304 [-41, +767] bid+15.3s (90% cluster bootstrap on symbol-days). "
    "2026-09-11, 22 legs / 5 symbol-days (14 TNON): +197.92 / +151.59 / +18.16 -> +298.07 / "
    "+193.46 / +254.08; paired +100 [-21, +320], +42 [-55, +185], +236 [+10, +470]. The ratchet "
    "on this base costs +361 [+83, +762] print over 14 d. Moving the ledger +/-15 s around the "
    "fill changes the base on 1 of 103 legs."
)
#: [65] The pre-trigger ratchet is OFF by measurement, not by a switch: this is the named
#: fallback every receipt carries until completed-swing facts (#1408) give a completed low.
TICK_DEADMAN_RATCHET_FALLBACK = "no_pre_trigger_ratchet_until_completed_swing_facts_wired"

# ── the per-leg phase machine (le["exit_verdict"]["phase"]) ─────────────────────
#: Live FSM state NEVER changes for the verdict: the phase lives beside it, in the leg dict.
#: armed        = held, the tape judges every tick (deadman walk, G, D)
#: exit_pending = the WHOLE exit is decided and handed to the exit seam (never a second one)
#: exited       = the fill landed (`_complete_confirmed_live_exit`); terminal
PHASES = ("armed", "exit_pending", "exited")
TERMINAL_PHASES = frozenset({"exited"})
#: The chandelier / quote-flow stop-movers must not lift the bid-stop while the verdict holds
#: the leg: the TICK deadman is the only software stop authority (review of #1385, major).
TRAIL_BYPASS_PHASES = frozenset({"armed", "exit_pending"})
#: A decided exit always owns pending work. The live caller additionally excludes
#: every supported tape-owned leg from fixed-target production, even on unreadable tape.
FIRST_TARGET_BYPASS_PHASES = frozenset({"exit_pending"})
#: (from, to). ``None`` = absent (no marker yet). Any pair not listed raises.
_ALLOWED: frozenset[tuple[str | None, str]] = frozenset({
    (None, "armed"),
    ("armed", "armed"),                 # every held tick
    ("armed", "exit_pending"),          # the trigger (deadman / G / D)
    ("armed", "exited"),                # another exit (bailout, cap, EOD, operator) ended the leg
    ("exit_pending", "exited"),         # the whole fill landed
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


# ── row helpers ─────────────────────────────────────────────────────────────────

def _px(row: Sequence[Any]) -> float | None:
    try:
        v = float(row[0])
    except (TypeError, ValueError, IndexError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _at(row: Sequence[Any]) -> Any:
    return row[5] if len(row) > 5 else None


def _id(row: Sequence[Any]) -> Any:
    return row[6] if len(row) > 6 else None


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


# ── the leg high, FIRST occurrence at the max (the script's tie rule) ───────────

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
    tick_rate_floor_pctile: float = 0.,
    feature_contract: str = COUNT_FEATURE_CONTRACT,
    print_age_bound_s: float = MEASURED_PRINT_AGE_S,
    gap_mult: float = _TAPE_GAP_DISCONTINUITY_P90_MULT,
    as_of_ts: float | None = None,
    window_s: float | None = None,
    count_parameter_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verdict D on the prints AFTER the leg's high print (the high itself excluded).

        sellers_took_it = signed_tape_accel < 0      (back half weaker than the front)
                          and buy_share_delta < 0    (aggressor-buy share fell front -> back)
                          and swing_low_now < swing_low_prev   (a LOWER LOW in prints)

    Count halves compare raw positive aggressor-buy volume, buy share and half minima.
    The minima are not completed swing/pivot confirmations. The count contract never
    uses ``window_s`` for midpoint, trim, rate fallback or freshness. The only time-split
    path is explicit ``legacy_time_split`` with ``window_s`` for research reproduction.
    ``binding`` names what decided: the floor that held the verdict back, the single
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
        "feature_contract": feature_contract,
        "window_kind": "since_high_prints", "window_prints": None,
        "window_s": None, "split": "count", "completed_pivot_claim": False,
        "floor_prints": BINDING_FLOOR_PRINTS,
        "feature_floor": FEATURE_FLOOR_PRINTS,
    }
    legacy = feature_contract == "legacy_time_split"
    if feature_contract not in {COUNT_FEATURE_CONTRACT, "legacy_time_split"}:
        out["binding"] = "invalid_feature_contract"
        return out
    if legacy:
        w = _f(window_s)
        if w is None or w <= 0:
            out["binding"] = "legacy_reproduction_requires_positive_window_s"
            return out
        out.update(window_s=w, split="time", legacy_reproduction_only=True)
        feature_kwargs = {"window_s": w, "tick_rate_floor_pctile": tick_rate_floor_pctile}
    else:
        cfg = count_exit_contract(window_prints=max(BINDING_FLOOR_PRINTS, n),
                                  print_age_bound_s=print_age_bound_s, gap_mult=gap_mult,
                                  tick_rate_floor_pctile=tick_rate_floor_pctile)
        direct_sources = {k: ("validated_explicit_or_default_function_argument"
                             if v == "configured_existing_setting" else v.replace("existing_setting", "function_argument"))
                          for k, v in cfg["sources"].items() if k != "window_prints"}
        if count_parameter_sources is not None:
            direct_sources = {k: count_parameter_sources.get(k, direct_sources[k]) for k in direct_sources}
        out.update(print_age_bound_s=cfg["print_age_bound_s"],
                   count_parameter_sources=direct_sources,
                   calibration={k: v for k, v in cfg["calibration"].items() if k != "window_prints"})
        feature_kwargs = {"window_s": None, "split": "count", "window_mode": "prints",
                          "gap_trim_s": cfg["print_age_bound_s"], "gap_discontinuity_mult": cfg["gap_mult"],
                          "as_of_ts": as_of_ts, "tick_rate_floor_pctile": cfg["tick_rate_floor_pctile"]}
    if n < FEATURE_FLOOR_PRINTS:
        out["binding"] = "feature_floor_n_lt_3"
        return out
    feats = _signed_tape_features(rows, **feature_kwargs)
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
    out.update({k: v for k, v in count_feature_receipt(feats).items()
                if k not in {"feature_contract", "window_kind", "window_prints", "window_s"}})
    if not legacy:
        out["print_age_bound_s"] = cfg["print_age_bound_s"]
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


# ── the G trigger: the acceleration rolls over while the print is above entry ───

def accel_rollover(
    *,
    accel_prev: Any,
    accel_now: Any,
    last_print: Any,
    entry_px: Any,
) -> dict[str, Any]:
    """G (`_ACCEL_ROLLOVER_DERIVATION`): ``accel_prev > 0 and accel_now <= 0 and
    last_print > entry_px``. ``accel_prev`` is the feature at the PREVIOUS held evaluation
    (the caller keeps it; a withheld tick does not advance it). ``binding`` names the single
    condition that held the trigger back, or ``rollover_above_entry`` when it fired."""
    prev = _f(accel_prev)
    now = _f(accel_now)
    lp = _f(last_print)
    entry = _f(entry_px)
    out: dict[str, Any] = {
        "fired": False,
        "binding": None,
        "accel_prev": prev,
        "accel_now": now,
        "last_print": lp,
        "entry_px": entry,
    }
    if now is None:
        out["binding"] = "accel_missing"
    elif prev is None:
        out["binding"] = "no_previous_evaluation"
    elif not (prev > 0.0):
        out["binding"] = "prev_not_positive"
    elif not (now <= 0.0):
        out["binding"] = "now_still_positive"
    elif lp is None or entry is None:
        out["binding"] = "no_print"
    elif not (lp > entry):
        out["binding"] = "print_not_above_entry"
    else:
        out["fired"] = True
        out["binding"] = "rollover_above_entry"
    return out


# ── the tick deadman: base at the entry fill ([65]: the tape's continued-pullback depth) ──

_BASE_KEYS = ("swing_low_prev", "swing_low_now", "buy_support_px")


def continued_pullback_depths(cycle_state: Any) -> list[float]:
    """``hi - pb_low`` of every COMPLETED cycle in the symbol-day tape ledger, oldest-first.

    ``cycle_state`` is ``tape_cycles.PullbackCycleScanner.to_dict()`` (``le["tape_cycle_state"]``).
    A cycle is written to ``cycles`` only when a print takes out its high AFTER a retrace of
    >= ``pullback_frac`` x its own amplitude, so every depth here is a pullback the tape
    CONTINUED from. Unreadable or non-positive rows are skipped, never guessed.
    """
    if not isinstance(cycle_state, dict):
        return []
    rows = cycle_state.get("cycles")
    if not isinstance(rows, list):
        return []
    out: list[float] = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        hi = _f(c.get("hi"))
        lo = _f(c.get("pb_low"))
        if hi is None or lo is None or not (hi > lo > 0.0):
            continue
        out.append(hi - lo)
    return out


def tick_deadman_cont_base(
    cycle_state: Any,
    *,
    entry_px: Any,
    resting_stop: Any,
    expected_day: str | None = None,
) -> dict[str, Any]:
    """[65] The tick deadman floor at the fill (`_TICK_DEADMAN_DERIVATION`):

        level = max(resting_stop, entry - median(hi - pb_low of the ledger's completed cycles))

    The median continued-pullback depth of the symbol-day is the tape's own measure of a
    pullback that is NOT a breakdown; the old count-half low sat at 0.17x-0.45x of it. The
    resting stop (``position.stop_price``) is the floor under it: a deadman below the software
    bid-stop could never fire first.

    Named fallback ``resting_stop`` (``fallback_reason`` says why) when the ledger is missing,
    belongs to another session day than ``expected_day`` (the caller's clock; this module has
    none), is not caught up (a partial ledger describes an earlier part of the day), has no
    completed cycle, or the candidate is not inside ``(0, entry)``. Returns the receipt dict: ``level``,
    ``level_source``, ``binding`` (the value that decided), ``cont_candidate``,
    ``cont_depth_p50``, ``n_cycles``, ``depths``, ``resting_stop``, ``risk_R`` (entry - resting
    stop), ``distance_R`` ((entry - level) / risk_R) and the ledger's own provenance.
    """
    entry = _f(entry_px)
    if entry is not None and entry <= 0.0:
        entry = None
    rest = _f(resting_stop)
    if rest is not None and rest <= 0.0:
        rest = None
    st = cycle_state if isinstance(cycle_state, dict) else None
    feed = st.get("feed") if st is not None and isinstance(st.get("feed"), dict) else {}
    depths = continued_pullback_depths(st)
    out: dict[str, Any] = {
        "level": None,
        "level_source": "none",
        "binding": None,
        "fallback_reason": None,
        "cont_candidate": None,
        "cont_depth_p50": None,
        "n_cycles": len(depths),
        "depths": [round(d, 6) for d in depths],
        "resting_stop": rest,
        "entry_px": entry,
        "risk_R": (entry - rest) if (entry is not None and rest is not None and entry > rest) else None,
        "risk_R_basis": "entry_minus_position_stop",
        "distance_R": None,
        "statistic": "median_of_completed_cycle_depths",
        "ledger": (
            {
                "day": st.get("day"),
                "through": st.get("last_observed_at"),
                "n_prints": st.get("n_prints"),
                "n_cycles_total": st.get("n_cycles"),
                "pullback_frac": st.get("pullback_frac"),
                "caught_up": feed.get("caught_up") if feed else None,
                "expected_day": expected_day,
            }
            if st is not None
            else None
        ),
        "completed_pivot_claim": False,
    }
    reason: str | None = None
    if st is None:
        reason = "no_tape_cycle_state"
    elif expected_day is not None and str(st.get("day") or "") != str(expected_day):
        reason = "tape_cycle_ledger_other_day"
    elif feed.get("caught_up") is not True:
        reason = "tape_cycle_ledger_not_caught_up"
    elif entry is None:
        reason = "entry_unreadable"
    elif not depths:
        reason = "no_completed_cycles"
    else:
        p50 = statistics.median(depths)
        cand = entry - p50
        out["cont_depth_p50"] = p50
        out["cont_candidate"] = cand
        if not (0.0 < cand < entry):
            reason = "cont_candidate_not_below_entry"
        elif rest is not None and rest >= cand:
            out.update(level=rest, level_source="resting_stop",
                       binding="resting_stop_at_or_above_cont_depth_p50")
        else:
            out.update(level=cand, level_source="cont_depth_p50", binding="cont_depth_p50")
    if reason is not None:
        out["fallback_reason"] = reason
        if rest is not None:
            out.update(level=rest, level_source="resting_stop", binding="named_fallback_resting_stop")
        else:
            out.update(binding="named_fallback_none")
    lvl = out["level"]
    if lvl is not None and out["risk_R"]:
        out["distance_R"] = (entry - lvl) / out["risk_R"]
    return out


def tick_deadman_base(
    feats: dict[str, Any] | None,
    *,
    entry_px: float,
    resting_stop: float | None,
) -> tuple[float | None, str]:
    """The OLD floor at the fill, kept as RECEIPT CONTEXT only since [65]: the first of
    ``swing_low_prev``, ``swing_low_now``, ``buy_support_px`` strictly BELOW the entry (read
    from the N prints at the entry fill); fallback the resting stop (source ``resting_stop``).
    Measured inside the tape's own noise (p50 0.23 R / 0.31 R below entry) -- the binding
    floor is `tick_deadman_cont_base`.
    """
    try:
        entry = float(entry_px)
    except (TypeError, ValueError):
        entry = None
    if isinstance(feats, dict) and entry is not None:
        for key in _BASE_KEYS:
            fv = _f(feats.get(key))
            if fv is None:
                continue
            if fv > 0.0 and fv < entry:
                return fv, key
    if resting_stop is None:
        return None, "none"
    try:
        return float(resting_stop), "resting_stop"
    except (TypeError, ValueError):
        return None, "none"


def swing_low_candidate(feats: dict[str, Any] | None) -> tuple[float | None, str | None]:
    """The rolling count-half candidate at a held tick: the FIRST non-null of the three keys
    (``g2_monotone_swing_low_ratchet._tick_stop_at``), or ``(None, None)``. Since [65] it is
    SHADOW only (the held-evaluation audit records it; it never moves the floor)."""
    if not isinstance(feats, dict):
        return None, None
    for key in _BASE_KEYS:
        fv = _f(feats.get(key))
        if fv is not None and fv > 0.0:
            return fv, key
    return None, None


def tick_deadman_ratchet(
    level: float | None,
    cand: Any,
    *,
    last_print: Any = None,
) -> tuple[float | None, bool]:
    """Raise only with a known finite print strictly above the candidate.

    A missing print cannot prove the proposed level is below the market. The
    existing floor remains until usable tape supports a monotone rise.
    Returns ``(level, moved)``. Since [65] the live tick does not call it (the rolling
    count-half candidate is not a completed low); it is the monotone primitive the
    completed-swing ratchet will use (`TICK_DEADMAN_RATCHET_FALLBACK` until then).
    """
    c = _f(cand)
    if c is None or c <= 0.0:
        return level, False
    lp = _f(last_print)
    if lp is None or not (c < lp):
        return level, False
    if level is None or c > float(level):
        return c, True
    return level, False


def walk_held_prints(
    batch: Sequence[Sequence[Any]],
    *,
    level: float | None,
    leg_high: dict[str, Any] | None,
    prints_since_high: int = 0,
) -> dict[str, Any]:
    """Walk the inter-tick batch IN ORDER, EVERY print (the tick always answers):

        (a) print <= level          => the tick deadman fires; the walk stops AT that print
        (b) print > leg_high        => the leg high moves (FIRST occurrence: strictly greater)
                                       and the since-high count restarts at 0

    Returns the crossing print (or None), the leg high, the last print seen, how many prints
    were walked, the running count of prints since the high and the FRONTIER = the tuple
    ``(observed_at, id)`` of the LAST WALKED print. The caller resumes the next read strictly
    after that tuple, so the frontier can never advance past a print this walk did not
    evaluate (review of #1385, major): an unreadable batch leaves it where it was; a stopped
    walk leaves it at the crossing print.
    """
    lvl = _f(level)
    hi = dict(leg_high) if isinstance(leg_high, dict) else None
    exit_print: dict[str, Any] | None = None
    walked = 0
    since_high = int(prints_since_high or 0)
    last_print: float | None = None
    last_at: Any = None
    frontier: tuple[Any, Any] | None = None
    for row in batch:
        p = _px(row)
        if p is None:
            continue
        walked += 1
        last_print = p
        last_at = _at(row)
        frontier = (_at(row), _id(row))
        if lvl is not None and p <= lvl:
            exit_print = {"price": p, "observed_at": _at(row), "id": _id(row)}
            if hi is not None:
                since_high += 1
            break
        if hi is None or p > float(hi["price"]):
            hi = {"price": p, "observed_at": _at(row), "id": _id(row), "tie_rule": "first_occurrence"}
            since_high = 0
        else:
            since_high += 1
    return {
        "exit_print": exit_print,
        "leg_high": hi,
        "last_print": last_print,
        "last_print_at": last_at,
        "prints_walked": walked,
        "prints_since_high": since_high,
        "frontier": frontier,
    }


# ── receipts ────────────────────────────────────────────────────────────────────

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
        **{k: v.get(k) for k in ("feature_contract", "window_kind", "window_prints", "split",
             "span_s", "half_print_counts", "gap_trim_s", "gap_trim_basis", "gap_trim_window_p90_s",
             "gap_trim_mult", "print_age_s", "print_age_bound_s", "print_stale", "tick_rate_basis",
             "count_parameter_sources", "calibration", "completed_pivot_claim", "withheld")},
    }


def rollover_receipt(g: dict[str, Any] | None) -> dict[str, Any]:
    """The G dict as every receipt carries it (stable key set)."""
    g = g if isinstance(g, dict) else {}
    return {
        "fired": bool(g.get("fired")),
        "binding": g.get("binding"),
        "accel_prev": g.get("accel_prev"),
        "accel_now": g.get("accel_now"),
        "last_print": g.get("last_print"),
        "entry_px": g.get("entry_px"),
        **{k: g.get(k) for k in ("feature_contract_id", "previous_feature_contract_id",
                                "withheld", "condition_fired_before_freshness")},
    }
