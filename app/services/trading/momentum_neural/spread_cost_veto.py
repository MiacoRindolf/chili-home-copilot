"""Adaptive spread-cost entry veto/derate (momentum_neural).

THE TRAP this avoids (project_momentum_zero_fills_root_cause): Ross low-float
movers INHERENTLY trade wide spreads (PAVS live spread 317bps, not a bug — it is
the market for a +200% low-float runner). A FLAT bps spread veto re-creates the
documented 0-fills over-restriction: it rejects every explosive name the lane is
supposed to trade. So this gate is NEVER flat. It judges the live spread RELATIVE
to two adaptive references:

  (a) THE NAME'S OWN RECENT TYPICAL SPREAD — the rolling p50/p75/p90 of its own
      ``momentum_nbbo_spread_tape`` history. A 300bps spread is FINE if the name
      normally trades 300bps; it is TOXIC only when it is anomalously wide vs the
      name's OWN norm (a thinning book / a stale quote / a halt-resume vacuum).

  (b) THE TRADE'S EXPECTED REWARD — the round-trip spread cost as a FRACTION of
      the structural risk R (= stop_distance). A wide spread that eats <=25% of
      the stop distance still leaves a tradeable edge; one that eats most of R
      means you start the trade already deep in the hole with no room to your stop.

DERATE-ONLY, GLOBALLY (2026-06-27): this gate NEVER returns ``allow=False`` for ANY
entry. If the spread is moderately wide vs the name's norm OR eats a moderate fraction
of R, the lane SIZES DOWN gracefully (mult in [floor,1.0]); at the EXTREME (an EXTREME
anomaly >= p90 * anomaly_extreme_mult AND the cost eats more than the documented max
fraction of R) it DERATES TO THE FLOOR (mult=floor, allow=True) — it does NOT reject
the trade. An entry filter can't tell a winner from a loser at fire-time; SIZE can, and
a toxic spread always sizes DOWN rather than blocking. This is ROBUST to ANY trigger
reason: because every entry takes the same derate path, there is no substring
under-coverage that could let a toxic spread slip through unguarded. Both the anomaly
multiple and the max-fraction-of-R are ONE documented base each (no flat bps magic;
everything is name-relative + R-relative).

RECLAIM DERATES-LESS TILT (2026-06-27): a low-float dip/VWAP-reclaim fires PRECISELY at
the reclaim — the moment the book is thinnest and the spread is INHERENTLY at its widest
(the flush vacuum / the snap off the low). The other entry gates already carve this out
(``_deep_reclaim`` etc.). The spread-cost gate honours the same intent: when the
entry-trigger reason/pattern is a RECLAIM family (dip_buy / vwap_reclaim / flush_dip /
deep_reclaim / wick_reclaim / bounce / curl reclaims), it judges cost against a
MORE-PERMISSIVE max-fraction-of-R base (``chili_momentum_spread_cost_reclaim_max_fraction_of_r``,
ONE documented base, default ~0.35 vs the non-reclaim ~0.25). Net: at the SAME extreme
spread a reclaim DERATES LESS than a non-reclaim. This is now a derates-less tilt, NOT a
hard-veto exemption (since NOTHING hard-vetoes anymore). The name-relative p50/p75/p90
anomaly logic (the derate MAGNITUDE) is unchanged.

Flag: ``chili_momentum_adaptive_spread_cost_veto_enabled`` (default False). When
OFF, ``adaptive_spread_cost_veto_derate`` returns the byte-identical pass-through
``(True, 1.0, "flag_off", {})`` and the caller's sizing path is unchanged.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings

logger = logging.getLogger(__name__)

# Pass-through used when the flag is OFF or inputs are unusable. allow=True,
# derate_mult=1.0 => the caller's risk budget is multiplied by 1.0 (byte-identical).
_PASS = (True, 1.0, "flag_off", {})

# RECLAIM FAMILY: substrings that identify a dip/VWAP-reclaim-style entry-trigger
# reason/pattern. These entries fire at the reclaim — the widest-spread / thinnest-book
# moment — so the gate must DERATE-ONLY (never hard-veto) and use a more-permissive
# max-fraction-of-R base. Matched as case-insensitive substrings of the normalized
# trigger reason so trigger-reason variants (``deep_reclaim_ok`` / ``flush_dip_buy`` /
# ``vwap_reclaim`` / ``..._tick_ok``) are all covered without an exhaustive enum.
_RECLAIM_TRIGGER_SUBSTRINGS = (
    "reclaim",       # vwap_reclaim, wick_reclaim, deep_reclaim, deep_reclaim_tick_ok, ...
    "dip",           # flush_dip_buy, deep_reclaim_dipbuy_ok, ask_thins_dip, halt_resume_dip_ok
    "flush",         # flush_dip / flush-style fast reversals
    "curl",          # curl-back reclaims
    "bounce",        # bounce reclaims
    "sub_vwap_trap", # below-VWAP trap reclaim
)


def _is_reclaim_family(entry_trigger_reason: Any) -> bool:
    """True iff ``entry_trigger_reason`` names a reclaim/dip-family entry trigger.

    Pure / fail-CLOSED to False: an unknown / empty / non-string reason is NOT a
    reclaim, so the non-reclaim (hard-veto-capable) path applies — the carve-out only
    LOOSENS when we positively recognize a reclaim, never by default."""
    try:
        r = str(entry_trigger_reason or "").strip().lower()
    except Exception:
        return False
    if not r:
        return False
    return any(tok in r for tok in _RECLAIM_TRIGGER_SUBSTRINGS)


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if (f == f and math.isfinite(f)) else None
    except (TypeError, ValueError):
        return None


def name_spread_percentiles(
    db: Session,
    symbol: str,
    *,
    lookback_days: float,
    now_utc: Optional[datetime] = None,
    min_samples: int = 8,
) -> Optional[dict[str, float]]:
    """The name's OWN recent spread distribution (p50/p75/p90 bps) over the last
    ``lookback_days`` of its ``momentum_nbbo_spread_tape`` history.

    This is the ADAPTIVE baseline — "what is a typical spread FOR THIS NAME" — so a
    chronically-wide low-float name is judged against its own norm, never a flat bar.
    Returns None when there are fewer than ``min_samples`` rows (the distribution is
    not yet meaningful -> caller fails OPEN, never over-restricts on thin history).
    Pure read; never raises (best-effort, returns None on any failure)."""
    sym = str(symbol or "").strip().upper()
    if not sym or lookback_days <= 0:
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    since = now_utc.replace(tzinfo=None) - timedelta(days=float(lookback_days))
    try:
        row = db.execute(
            text(
                "SELECT "
                "  percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps), "
                "  percentile_cont(0.75) WITHIN GROUP (ORDER BY spread_bps), "
                "  percentile_cont(0.90) WITHIN GROUP (ORDER BY spread_bps), "
                "  count(*) "
                "FROM momentum_nbbo_spread_tape "
                "WHERE symbol = :s AND spread_bps IS NOT NULL AND spread_bps > 0 "
                "  AND observed_at >= :since"
            ),
            {"s": sym, "since": since},
        ).fetchone()
    except Exception as exc:  # pragma: no cover - DB/network
        logger.debug("[spread_cost_veto] percentile read failed for %s: %s", sym, exc)
        return None
    if not row or row[0] is None:
        return None
    n = int(row[3] or 0)
    if n < int(min_samples):
        return None
    p50 = _f(row[0])
    p75 = _f(row[1])
    p90 = _f(row[2])
    if p50 is None or p50 <= 0:
        return None
    return {
        "p50": p50,
        "p75": p75 if (p75 is not None and p75 > 0) else p50,
        "p90": p90 if (p90 is not None and p90 > 0) else (p75 or p50),
        "n": float(n),
    }


def adaptive_spread_cost_veto_derate(
    *,
    symbol: str,
    entry_price: float,
    current_spread_bps: float | None,
    stop_distance: float,
    db: Session,
    flag_enabled: bool,
    entry_trigger_reason: Any = None,
    now_utc: Optional[datetime] = None,
) -> tuple[bool, float, str, dict[str, Any]]:
    """Adaptive spread-cost gate. Returns ``(allow, derate_mult, reason, meta)``:

      * ``allow`` (bool): ALWAYS True — this gate is DERATE-ONLY and NEVER blocks an
        entry. (Kept in the signature for the caller's stable contract.)
      * ``derate_mult`` (float in [floor, 1.0]): the risk-budget size multiplier to
        compose into ``max_loss_usd`` before ``compute_risk_first_quantity``. 1.0 =
        no effect (byte-identical). <1.0 = graceful size-down; floor = the extreme
        toxic-spread case (was the old hard veto, now floored).
      * ``reason`` (str), ``meta`` (dict): audit trail.

    ADAPTIVE references (no flat bps anywhere):
      * Anomaly score = current_spread / name_p50 (its own median). A name normally
        300bps reads anomaly=1.0 at 300bps — NOT toxic. >2x its own p50 OR above its
        own p75 = anomalously wide for IT.
      * Cost-of-R = round-trip spread cost as a fraction of the structural stop
        distance: (spread_bps/1e4 * entry_price) / stop_distance. The spread is paid
        once on the round trip; if it consumes most of the stop distance there is no
        edge left to the structural target.

    DERATE-ONLY: moderate anomaly OR moderate cost-of-R => mult in [floor,1.0]. At the
    EXTREME (an EXTREME outlier vs the NAME'S OWN p90 >= p90 * extreme_mult AND the
    round-trip cost exceeds the documented max fraction of R) => mult=floor, allow=True
    (NEVER allow=False). A wide-but-TYPICAL low-float spread with a good R PASSES at
    mult=1.0 (the no-0-fills guarantee). A toxic spread always SIZES DOWN, never blocks
    — robust to ANY trigger reason (no substring under-coverage can block an entry).

    RECLAIM DERATES-LESS TILT: when ``entry_trigger_reason`` names a reclaim/dip family
    (``_is_reclaim_family``), the cost is judged against the more-permissive
    ``chili_momentum_spread_cost_reclaim_max_fraction_of_r`` base (a reclaim inherently
    fires at the widest-spread moment), so at the SAME extreme spread a reclaim derates
    LESS than a non-reclaim. The name-relative anomaly logic for the derate MAGNITUDE is
    identical. This is a derates-less tilt, not a hard-veto exemption (nothing vetoes).

    Fail-OPEN on unusable inputs / thin history / flag OFF =>
    ``(True, 1.0, ..., {})`` so this can NEVER newly block a fill it lacks data for.
    """
    if not flag_enabled:
        return _PASS

    # RECLAIM DERATES-LESS tilt: positively recognized reclaim/dip family => permissive
    # R base (derates LESS at the same extreme spread). Unknown/None => non-reclaim
    # (standard R base). Nothing hard-vetoes either way (derate-only globally).
    is_reclaim = _is_reclaim_family(entry_trigger_reason)

    e = _f(entry_price)
    sb = _f(current_spread_bps)
    sd = _f(stop_distance)
    # Fail-OPEN on any unusable basis: we never veto/derate on bad data.
    if e is None or e <= 0:
        return True, 1.0, "no_entry_price", {}
    if sb is None or sb <= 0:
        return True, 1.0, "no_spread", {}
    if sd is None or sd <= 0:
        return True, 1.0, "no_stop_distance", {}

    # ── ONE documented base each (adaptive knobs, no flat bps) ──────────────────
    # Max fraction of the structural risk R the round-trip spread may consume.
    # RECLAIM CARVE-OUT: a reclaim/dip entry fires at the widest-spread moment, so it
    # uses the MORE-PERMISSIVE reclaim base (ONE documented setting); non-reclaim uses
    # the standard base (UNCHANGED). Falls back to the standard base if the reclaim
    # setting is missing/invalid -> the carve-out can never make the gate STRICTER.
    _std_max_frac_of_r = float(
        getattr(settings, "chili_momentum_spread_cost_max_fraction_of_r", 0.25) or 0.25
    )
    if is_reclaim:
        _reclaim_max_frac_of_r = float(
            getattr(settings, "chili_momentum_spread_cost_reclaim_max_fraction_of_r", 0.35)
            or 0.35
        )
        # Never let the reclaim base be STRICTER than the standard base (a misconfig
        # must not turn the permissive carve-out into a tighter gate for reclaims).
        max_frac_of_r = max(_std_max_frac_of_r, _reclaim_max_frac_of_r)
    else:
        max_frac_of_r = _std_max_frac_of_r
    # Anomaly multiple vs the name's OWN median (p50) that flags "wide for IT".
    anomaly_mult = float(
        getattr(settings, "chili_momentum_spread_anomaly_p50_mult", 2.0) or 2.0
    )
    # Extra multiple beyond the name's OWN p90 that defines an EXTREME outlier.
    extreme_mult = float(
        getattr(settings, "chili_momentum_spread_anomaly_extreme_p90_mult", 1.5) or 1.5
    )
    # Graceful derate floor (never zeroes the size -> preserves the explosive tail).
    floor = float(
        getattr(settings, "chili_momentum_spread_cost_derate_floor", 0.5) or 0.5
    )
    if not (0.0 < floor <= 1.0):
        floor = 0.5
    lookback_days = float(
        getattr(settings, "chili_momentum_spread_norm_lookback_days", 20.0) or 20.0
    )

    # ── (b) Cost-of-R: round-trip spread cost as a fraction of the stop distance ──
    # spread_dollars = (spread_bps / 1e4) * entry_price ; cost_of_r = spread$ / R.
    spread_dollars = (sb / 10_000.0) * e
    cost_of_r = spread_dollars / sd if sd > 0 else float("inf")

    # ── (a) Name-relative anomaly: spread vs the name's OWN rolling distribution ──
    pct = name_spread_percentiles(db, symbol, lookback_days=lookback_days, now_utc=now_utc)
    meta: dict[str, Any] = {
        "symbol": str(symbol or "").upper(),
        "spread_bps": round(sb, 1),
        "cost_of_r": round(cost_of_r, 4),
        "max_frac_of_r": max_frac_of_r,
        "is_reclaim": bool(is_reclaim),
        "entry_trigger_reason": (
            str(entry_trigger_reason) if entry_trigger_reason is not None else None
        ),
    }

    anomaly_ratio: Optional[float] = None
    above_p75 = False
    extreme_anomaly = False
    if pct is not None:
        p50 = pct["p50"]
        p75 = pct["p75"]
        p90 = pct["p90"]
        anomaly_ratio = sb / p50 if p50 > 0 else None
        above_p75 = sb > p75
        extreme_anomaly = sb >= (p90 * extreme_mult)
        meta.update({
            "name_p50_bps": round(p50, 1),
            "name_p75_bps": round(p75, 1),
            "name_p90_bps": round(p90, 1),
            "name_samples": int(pct["n"]),
            "anomaly_ratio": round(anomaly_ratio, 3) if anomaly_ratio is not None else None,
        })
    else:
        meta["name_dist"] = "insufficient_history"

    cost_too_high = cost_of_r > max_frac_of_r

    # ── DERATE-ONLY GLOBALLY (no hard veto for ANY entry) ───────────────────────
    # The extreme case (an EXTREME anomaly vs the name's OWN p90 distribution AND the
    # round-trip cost exceeds the documented max fraction of R) NO LONGER blocks the
    # entry. It DERATES TO THE FLOOR instead (allow=True, mult=floor): a toxic spread
    # always SIZES DOWN, it never rejects the trade. This is robust to ANY trigger
    # reason — there is no substring under-coverage that could let a toxic spread slip
    # through un-derated, because every entry takes the same derate path below.
    #
    # The reclaim carve-out survives as a DERATES-LESS tilt (no longer a hard-veto
    # exemption): a reclaim judges cost against the more-permissive R base, so at the
    # SAME extreme spread a reclaim derates LESS than a non-reclaim. The name-relative
    # p50/p75/p90 anomaly logic still sets the derate MAGNITUDE.
    extreme_cost_floor = extreme_anomaly and cost_too_high
    if extreme_cost_floor:
        # Extreme toxic spread: floored size-down, never a block. Record the basis for
        # the audit trail; both reclaim and non-reclaim land here (derate-only).
        meta["extreme_floor"] = True
        if is_reclaim:
            meta["reclaim_veto_carveout"] = True

    # ── GRACEFUL DERATE (size down, never reject) ───────────────────────────────
    # Two independent size-down pressures, take the SMALLER multiplier (most cautious).
    # CRITICAL no-0-fills property: a TIGHT or wide-but-TYPICAL spread with a healthy R
    # must PASS at mult=1.0 (never derate a normal Ross trade). So neither pressure
    # engages until the spread is NEARING a problem — both have a dead-band:
    #   1. cost-of-R: ONLY engages once the round-trip cost climbs into the upper band
    #      (>= engage_frac x max_frac_of_r); below that the cost is benign -> mult 1.0.
    #      It then scales linearly to the floor as the cost reaches max_frac_of_r.
    #   2. anomaly: ONLY when the spread is anomalously wide FOR THE NAME (>= anomaly_mult
    #      x its own p50, OR above its own p75); scales toward floor past that threshold.
    # A spread that is typical for the name AND cheap relative to R hits NEITHER -> 1.0.
    engage_frac = float(
        getattr(settings, "chili_momentum_spread_cost_derate_engage_frac", 0.5) or 0.5
    )
    if not (0.0 <= engage_frac < 1.0):
        engage_frac = 0.5
    mult = 1.0
    derate_reasons: list[str] = []

    # 1. Cost-of-R derate: a DEAD-BAND below engage_frac*cap (benign cost -> no derate);
    # then linear from 1.0 at the engage point down to floor at the cap.
    if max_frac_of_r > 0:
        engage_cost = engage_frac * max_frac_of_r
        if cost_of_r > engage_cost:
            span = max(1e-9, max_frac_of_r - engage_cost)
            # frac in (0,1]: 0 at the engage point, 1.0 at (and beyond) the cap.
            cost_frac = min(1.0, (cost_of_r - engage_cost) / span)
            cost_mult = max(floor, 1.0 - cost_frac * (1.0 - floor))
            if cost_mult < mult:
                mult = cost_mult
            if cost_too_high:
                derate_reasons.append("cost_of_r_high")
            else:
                derate_reasons.append("cost_of_r")

    # 2. Anomaly derate (only when we HAVE the name's distribution).
    if anomaly_ratio is not None and (above_p75 or anomaly_ratio >= anomaly_mult):
        # How far past the name's own p50, normalized by the anomaly_mult threshold:
        # at anomaly_ratio==anomaly_mult -> 0 extra; scales toward floor beyond it.
        over = max(0.0, anomaly_ratio - 1.0) / max(1e-9, (anomaly_mult - 1.0))
        anom_mult = max(floor, 1.0 - min(1.0, over) * (1.0 - floor))
        if anom_mult < mult:
            mult = anom_mult
        derate_reasons.append("anomaly_wide_for_name")

    # The extreme toxic case (was the old hard veto) now floors the size deterministically
    # — allow=True, mult=floor — regardless of the linear math above. This guarantees a
    # toxic spread always sizes DOWN to the floor for EVERY entry reason, never blocks.
    if extreme_cost_floor:
        mult = floor
        if "extreme_spread_floored" not in derate_reasons:
            derate_reasons.append("extreme_spread_floored")

    mult = max(floor, min(1.0, mult))
    if mult >= 1.0:
        meta["decision"] = "pass"
        return True, 1.0, "pass", meta

    meta["decision"] = "derate"
    meta["derate_mult"] = round(mult, 4)
    return True, mult, "+".join(derate_reasons) or "derate", meta
