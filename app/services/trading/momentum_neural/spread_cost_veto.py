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

DERATE-FIRST, VETO-LAST: if the spread is moderately wide vs the name's norm OR
eats a moderate fraction of R, the lane SIZES DOWN gracefully (mult in [floor,1.0])
— it does NOT reject the trade (an entry filter can't tell a winner from a loser
at fire-time; SIZE can). A HARD VETO (allow=False) fires ONLY at the extreme: an
EXTREME anomaly (>= p90 * anomaly_extreme_mult, i.e. far outside the name's own
distribution) AND the cost eats more than the documented max fraction of R. Both
the anomaly multiple and the max-fraction-of-R are ONE documented base each (no
flat bps magic; everything is name-relative + R-relative).

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
    now_utc: Optional[datetime] = None,
) -> tuple[bool, float, str, dict[str, Any]]:
    """Adaptive spread-cost gate. Returns ``(allow, derate_mult, reason, meta)``:

      * ``allow`` (bool): False = HARD VETO (extreme anomaly AND cost eats too much
        of R). The caller skips the entry. True for every non-extreme case.
      * ``derate_mult`` (float in [floor, 1.0]): the risk-budget size multiplier to
        compose into ``max_loss_usd`` before ``compute_risk_first_quantity``. 1.0 =
        no effect (byte-identical). <1.0 = graceful size-down.
      * ``reason`` (str), ``meta`` (dict): audit trail.

    ADAPTIVE references (no flat bps anywhere):
      * Anomaly score = current_spread / name_p50 (its own median). A name normally
        300bps reads anomaly=1.0 at 300bps — NOT toxic. >2x its own p50 OR above its
        own p75 = anomalously wide for IT.
      * Cost-of-R = round-trip spread cost as a fraction of the structural stop
        distance: (spread_bps/1e4 * entry_price) / stop_distance. The spread is paid
        once on the round trip; if it consumes most of the stop distance there is no
        edge left to the structural target.

    DERATE-FIRST: moderate anomaly OR moderate cost-of-R => mult in [floor,1.0],
    allow=True. VETO-LAST: allow=False ONLY when the spread is an EXTREME outlier vs
    the NAME'S OWN p90 (>= p90 * extreme_mult) AND the round-trip cost exceeds the
    documented max fraction of R. A wide-but-TYPICAL low-float spread with a good R
    PASSES at mult=1.0 (the no-0-fills guarantee).

    Fail-OPEN on unusable inputs / thin history / flag OFF =>
    ``(True, 1.0, ..., {})`` so this can NEVER newly block a fill it lacks data for.
    """
    if not flag_enabled:
        return _PASS

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
    max_frac_of_r = float(
        getattr(settings, "chili_momentum_spread_cost_max_fraction_of_r", 0.25) or 0.25
    )
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

    # ── HARD VETO (extreme only) ────────────────────────────────────────────────
    # Both conditions must hold: an EXTREME anomaly vs the name's OWN p90 distribution
    # AND the round-trip cost exceeds the documented max fraction of R. A name with no
    # distribution history (pct is None) can NEVER be vetoed here (extreme_anomaly is
    # False) -> we never hard-veto on thin history; at worst we cost-derate below.
    if extreme_anomaly and cost_too_high:
        meta["decision"] = "hard_veto"
        return (
            False,
            floor,
            "extreme_spread_anomaly_and_cost",
            meta,
        )

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

    mult = max(floor, min(1.0, mult))
    if mult >= 1.0:
        meta["decision"] = "pass"
        return True, 1.0, "pass", meta

    meta["decision"] = "derate"
    meta["derate_mult"] = round(mult, 4)
    return True, mult, "+".join(derate_reasons) or "derate", meta
