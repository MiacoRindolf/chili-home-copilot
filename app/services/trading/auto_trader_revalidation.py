"""Deterministic trade revalidation — native CHILI mechanics that replace the
per-candidate LLM viability call (``auto_trader_llm.run_revalidation_llm``).

The LLM revalidation prompt (``prompts/auto_trader_revalidation.txt``) restricts
the model to HARD, computable invalidations and *explicitly forbids* strategy
judgment — "catch hard invalidations, not second-guess the strategy", "do NOT
reject for weak momentum / general caution / wait-for-confirmation". Every one
of its ``viable=false`` criteria is a pure price/level comparison the autotrader
already has the inputs for, so this module reproduces them deterministically:
instant (no model call, no network), fully reproducible, and *without* the LLM's
fail-closed failure mode (``llm_unavailable`` / ``parse_failed`` currently BLOCK
live trades — i.e. model flakiness costs good entries).

Criteria parity with the prompt (long; mirrored for short):
- ``data_corrupt``       — any of entry/stop/target/current non-finite or <= 0
- ``incoherent_levels``  — not (stop < entry < target) for a long
- ``price_through_stop`` — current already at/through the stop
- ``target_already_met`` — current already at/through the target

The prompt's "catastrophic gap beyond the band" case needs no separate check
here: the rule gate enforces the entry slippage band UPSTREAM, so by this point
the current price is already within tolerance of entry (a >band gap was rejected
earlier). The remaining OHLCV "breakdown" signals are reflected in the fresh live
price, which these comparisons already test against the stop.
"""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

REVALIDATION_REASON_OK = "ok"
REVALIDATION_REASON_DATA_CORRUPT = "data_corrupt"
REVALIDATION_REASON_INCOHERENT = "incoherent_levels"
REVALIDATION_REASON_THROUGH_STOP = "price_through_stop"
REVALIDATION_REASON_TARGET_MET = "target_already_met"


def _finite_pos(*values: Any) -> bool:
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(f) or f <= 0.0:
            return False
    return True


def _is_long(direction: Any) -> bool:
    return str(direction if direction is not None else "long").strip().lower() != "short"


def deterministic_revalidation(
    alert: Any,
    *,
    current_price: float,
    settings_: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Return ``(viable, snapshot)`` replicating the LLM revalidation veto.

    Native deterministic mechanics — no model call, no network. Mirrors
    ``prompts/auto_trader_revalidation.txt`` exactly. ``snapshot`` carries the
    same shape the LLM path produced (``viable``/``confidence``/``reason``) plus
    the levels used, so audit/log consumers are unchanged.
    """
    entry = getattr(alert, "entry_price", None)
    stop = getattr(alert, "stop_loss", None)
    target = getattr(alert, "target_price", None)
    is_long = _is_long(getattr(alert, "direction", "long"))
    snap: dict[str, Any] = {
        "mode": "deterministic",
        "direction": "long" if is_long else "short",
        "current_price": current_price,
        "entry_price": entry,
        "stop_loss": stop,
        "target_price": target,
    }

    # 1. Data sanity — any non-finite / non-positive level is missing or corrupt.
    if not _finite_pos(entry, stop, target, current_price):
        snap.update(viable=False, confidence=0.0, reason=REVALIDATION_REASON_DATA_CORRUPT)
        return False, snap

    entry_f = float(entry)
    stop_f = float(stop)
    target_f = float(target)
    cur = float(current_price)

    # 2. Coherence — long: stop < entry < target; short: stop > entry > target.
    coherent = (stop_f < entry_f < target_f) if is_long else (stop_f > entry_f > target_f)
    if not coherent:
        snap.update(viable=False, confidence=0.0, reason=REVALIDATION_REASON_INCOHERENT)
        return False, snap

    # 3. Stop already hit — the position would stop out immediately on open.
    through_stop = (cur <= stop_f) if is_long else (cur >= stop_f)
    if through_stop:
        snap.update(viable=False, confidence=0.0, reason=REVALIDATION_REASON_THROUGH_STOP)
        return False, snap

    # 4. Target already met — no reward left to capture.
    target_met = (cur >= target_f) if is_long else (cur <= target_f)
    if target_met:
        snap.update(viable=False, confidence=0.0, reason=REVALIDATION_REASON_TARGET_MET)
        return False, snap

    # Viable. Audit-only confidence: how far the current price sits from the
    # nearer barrier, normalised by the stop->target span (1.0 centred, ->0 at a
    # barrier). Not used downstream — the LLM path's confidence wasn't either.
    span = (target_f - stop_f) if is_long else (stop_f - target_f)
    if span > 0:
        dist_stop = (cur - stop_f) if is_long else (stop_f - cur)
        dist_target = (target_f - cur) if is_long else (cur - target_f)
        confidence = max(0.0, min(1.0, 2.0 * min(dist_stop, dist_target) / span))
    else:
        confidence = 0.0
    snap.update(viable=True, confidence=round(confidence, 4), reason=REVALIDATION_REASON_OK)
    return True, snap
