"""Crypto-native pattern miner.

Generates new candidate scan_patterns specifically for the 24/7 fast-cycle
crypto markets. Separate from the general ``web_pattern_researcher``
(which is biased toward 1d swing setups for stocks) and the manual seeds
in mig 203 (a fixed set of 10 hand-coded patterns).

Strategy: **mine FROM realized crypto winners.** When a crypto trade
closes profitable, capture the indicator snapshot at entry — that's a
known-profitable signature. Spawn new candidate patterns whose
``rules_json.conditions`` mirror that signature, with small variations
(loosen/tighten thresholds, swap one indicator).

This sidesteps two biases of the general miner:
1. **Asset-class bias** — the general miner mostly emits 1d patterns
   designed for stocks. This one emits 5m/15m/1h crypto-suitable.
2. **Theory-first bias** — the general miner spawns variants of
   theoretical patterns (BB squeeze, Fib retracement, etc) and hopes
   they work. This one spawns variants of empirically-profitable
   trades — patterns that REAL crypto trades have already won on.

## Architecture

The miner runs as a brain-worker subtask (registered in
``scripts/brain_worker.py:_SUBTASKS``). Each invocation:

1. Read recent crypto-class trades that closed profitable
   (``status='closed' AND pnl > 0 AND asset_kind='crypto'``)
2. For each winner, extract the indicator snapshot at entry from
   ``trading_trades.indicator_snapshot`` (or the linked breakout_alert)
3. Group winners by similar signature (using a fingerprint hash)
4. For each winning signature with N+ trades:
   * Spawn 2-3 variant candidate patterns with small condition tweaks
   * Insert as ``lifecycle_stage='candidate'``,
     ``asset_class='crypto'``, ``timeframe`` matching the winner's
     observed hold duration (5m for hold<1h, 15m for hold<6h,
     1h for hold<24h, etc)

Variants are immediately added to the backtest queue and follow the
normal candidate → backtested → challenged → promoted lifecycle.

## Settings

* ``brain_crypto_miner_enabled`` (default True)
* ``brain_crypto_miner_min_winners_per_signature`` (default 3) — need
  at least N wins of a signature before spawning variants
* ``brain_crypto_miner_max_variants_per_run`` (default 10) — cap on
  how many candidates each invocation can create
* ``brain_crypto_miner_lookback_days`` (default 30)
"""
from __future__ import annotations

import json
import logging
import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[crypto_miner]"


# ── Signature fingerprinting ───────────────────────────────────────────


# Indicators we consider for the signature. Order matters: keys are
# sorted before hashing so different orderings produce the same hash.
_SIGNATURE_INDICATORS: tuple[str, ...] = (
    "rsi_14",
    "macd",
    "macd_signal",
    "ema_20",
    "ema_50",
    "ema_200",
    "bb_pct",
    "bb_width",
    "atr_14",
    "volume_ratio",
    "vwap_distance_pct",
    "adx",
)


def _bucket_value(key: str, value: float | None) -> str:
    """Bucket a continuous indicator value into a coarse range so similar
    trades fingerprint together (e.g. RSI 28 and RSI 32 both bucket to
    'lo' for the oversold case)."""
    if value is None:
        return "na"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "na"

    if key == "rsi_14":
        if v < 30:
            return "lo"
        if v < 50:
            return "mid_lo"
        if v < 70:
            return "mid_hi"
        return "hi"
    if key == "bb_pct":
        if v < 0.1:
            return "lo"
        if v < 0.5:
            return "mid_lo"
        if v < 0.9:
            return "mid_hi"
        return "hi"
    if key == "volume_ratio":
        if v < 1.0:
            return "below_avg"
        if v < 1.5:
            return "avg"
        if v < 2.5:
            return "elevated"
        return "spike"
    if key == "adx":
        if v < 15:
            return "weak"
        if v < 25:
            return "mod"
        if v < 40:
            return "strong"
        return "extreme"
    # Default: percentile-style bucketing
    return f"raw:{round(v, 2)}"


def _fingerprint_signature(snap: dict[str, Any]) -> str:
    """Compute a stable hash over the bucketed indicator values for a
    trade's entry snapshot. Different trades with similar indicator
    profiles hash to the same fingerprint."""
    buckets = {}
    for key in _SIGNATURE_INDICATORS:
        if key in snap:
            buckets[key] = _bucket_value(key, snap[key])
    if not buckets:
        return ""
    payload = json.dumps(buckets, sort_keys=True)
    return hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()


def _hold_to_timeframe(hold_hours: float) -> str:
    """Map an observed trade hold duration to a sensible candidate
    timeframe for the spawned pattern."""
    if hold_hours < 1.0:
        return "5m"
    if hold_hours < 6.0:
        return "15m"
    if hold_hours < 24.0:
        return "1h"
    if hold_hours < 72.0:
        return "4h"
    return "1d"


# ── Winner discovery ───────────────────────────────────────────────────


def discover_crypto_winners(
    sess: Session,
    *,
    lookback_days: int = 30,
) -> list[dict[str, Any]]:
    """Pull recent profitable crypto trades + their entry signatures.

    Each returned dict contains:
        trade_id, ticker, scan_pattern_id, pnl, hold_hours,
        entry_snapshot (dict), fingerprint (str)

    Trades without a usable indicator_snapshot are filtered out.
    """
    rows = sess.execute(text(
        """
        SELECT t.id AS trade_id, t.ticker, t.scan_pattern_id,
               t.pnl, t.entry_price, t.exit_price,
               t.entry_date, t.exit_date,
               t.indicator_snapshot
        FROM trading_trades t
        WHERE t.asset_kind = 'crypto'
          AND t.status = 'closed'
          AND t.pnl IS NOT NULL
          AND t.pnl > 0
          AND t.entry_date IS NOT NULL
          AND t.exit_date IS NOT NULL
          AND t.exit_date > NOW() - make_interval(days => :ld)
        ORDER BY t.exit_date DESC
        """
    ), {"ld": int(lookback_days)}).fetchall()

    winners: list[dict[str, Any]] = []
    for r in rows:
        # indicator_snapshot is stored as text(json). Parse defensively.
        snap_raw = r.indicator_snapshot
        if not snap_raw:
            continue
        try:
            snap = json.loads(snap_raw) if isinstance(snap_raw, str) else dict(snap_raw)
        except Exception:
            continue
        # The autotrader nests the alert snapshot one level deep:
        # indicator_snapshot = {"breakout_alert": {...flat indicators...}}
        if isinstance(snap.get("breakout_alert"), dict):
            entry = snap["breakout_alert"]
        else:
            entry = snap
        if not isinstance(entry, dict):
            continue

        try:
            hold_hours = (r.exit_date - r.entry_date).total_seconds() / 3600.0
        except Exception:
            hold_hours = 24.0

        fp = _fingerprint_signature(entry)
        if not fp:
            continue
        winners.append({
            "trade_id": int(r.trade_id),
            "ticker": str(r.ticker),
            "scan_pattern_id": int(r.scan_pattern_id) if r.scan_pattern_id else None,
            "pnl": float(r.pnl),
            "hold_hours": hold_hours,
            "entry_snapshot": entry,
            "fingerprint": fp,
        })
    return winners


# ── Variant spawning ───────────────────────────────────────────────────


def _build_conditions_from_signature(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a winning trade's entry snapshot into a conditions list
    that future scans can match. Uses bucket boundaries as op thresholds."""
    conds: list[dict[str, Any]] = []
    for key in _SIGNATURE_INDICATORS:
        if key not in snap:
            continue
        v = snap[key]
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        # Choose op + threshold based on the value's bucket. The goal:
        # capture the "shape" of the winning condition with one ineq.
        if key == "rsi_14":
            if fv < 30:
                conds.append({"indicator": key, "op": "<", "value": 30})
            elif fv > 70:
                conds.append({"indicator": key, "op": ">", "value": 70})
            elif fv > 50:
                conds.append({"indicator": key, "op": ">", "value": 50})
        elif key == "bb_pct":
            if fv < 0.1:
                conds.append({"indicator": key, "op": "<", "value": 0.1})
            elif fv > 0.9:
                conds.append({"indicator": key, "op": ">", "value": 0.9})
        elif key == "volume_ratio":
            if fv >= 2.0:
                conds.append({"indicator": key, "op": ">=", "value": 2.0})
            elif fv >= 1.5:
                conds.append({"indicator": key, "op": ">=", "value": 1.5})
        elif key == "adx":
            if fv > 25:
                conds.append({"indicator": key, "op": ">", "value": 25})
        # Other indicators: only include if a clear threshold pattern
        # emerges. Keep the list short to avoid over-fitting.
    return conds


def _spawn_variant_pattern(
    sess: Session,
    *,
    fingerprint: str,
    base_conditions: list[dict[str, Any]],
    timeframe: str,
    parent_pattern_id: int | None,
    n_winners: int,
) -> int | None:
    """Insert a new candidate pattern based on the winning signature.
    Returns the new pattern id or None if a pattern with the same name
    already exists (idempotent)."""
    # Build a stable name from the fingerprint so repeated runs don't
    # spawn duplicates.
    name = f"Crypto Auto Mined {fingerprint[:8]} ({timeframe})"
    existing = sess.execute(text(
        "SELECT id FROM scan_patterns WHERE name = :n LIMIT 1"
    ), {"n": name}).fetchone()
    if existing:
        return None

    if not base_conditions:
        return None

    # FIX 41 (architectural fix, 2026-04-29): we serialize to JSON-string
    # here so the SQL ``CAST(:rules AS jsonb)`` below can parse it cleanly
    # back into a JSONB *object* (not a JSONB string). This is the same
    # pattern mig 203 uses. The corruption that mig 204 repaired came from
    # an ORM-attribute assignment in ``hydrate_scan_pattern_rules_json`` —
    # not this SQL path. Keep the explicit CAST so the storage type is
    # always object regardless of how the bind layer adapts the value.
    rules_json = json.dumps({"conditions": base_conditions})
    description = (
        f"Auto-mined from {n_winners} profitable crypto trade(s) sharing "
        f"signature {fingerprint[:8]}. Emitted by crypto pattern_miner."
    )
    exit_cfg = json.dumps({
        "max_bars": 20,
        "atr_mult": 1.6,
        "use_bos": False,
        "target_r_multiple": 2.5,
    })

    row = sess.execute(text("""
        INSERT INTO scan_patterns (
            name, description, rules_json, exit_config,
            origin, asset_class, timeframe,
            confidence, win_rate, avg_return_pct,
            evidence_count, backtest_count, trade_count,
            score_boost, min_base_score, generation,
            parent_id,
            lifecycle_stage, promotion_status,
            ticker_scope, backtest_priority,
            active, created_at, updated_at
        ) VALUES (
            :name, :description, CAST(:rules AS jsonb), CAST(:exit_cfg AS jsonb),
            'crypto_miner_auto', 'crypto', :tf,
            0.5, NULL, NULL,
            :n_winners, 0, 0,
            0.0, 0.0, 1,
            :parent_id,
            'candidate', 'pending_oos',
            'universal', 50,
            true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        RETURNING id
    """), {
        "name": name,
        "description": description,
        "rules": rules_json,
        "exit_cfg": exit_cfg,
        "tf": timeframe,
        "parent_id": parent_pattern_id,
        "n_winners": n_winners,
    }).fetchone()
    sess.commit()
    return int(row.id) if row else None


# ── Public entry point ─────────────────────────────────────────────────


def run_crypto_pattern_miner(sess: Session) -> dict[str, Any]:
    """Single invocation of the miner. Returns a summary dict.

    Designed to be called from the brain-worker subtask registry.
    """
    from ....config import settings

    if not bool(getattr(settings, "brain_crypto_miner_enabled", True)):
        return {"skipped": True, "reason": "disabled"}

    lookback = int(getattr(settings, "brain_crypto_miner_lookback_days", 30))
    min_winners = int(getattr(settings, "brain_crypto_miner_min_winners_per_signature", 3))
    max_variants = int(getattr(settings, "brain_crypto_miner_max_variants_per_run", 10))

    winners = discover_crypto_winners(sess, lookback_days=lookback)
    if not winners:
        return {"winners_found": 0, "variants_spawned": 0}

    # Group by fingerprint
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for w in winners:
        by_fingerprint.setdefault(w["fingerprint"], []).append(w)

    # Sort signatures by (n_winners desc, total_pnl desc) so we spawn
    # variants for the best signatures first under the cap.
    def _sig_score(group: list[dict[str, Any]]) -> tuple[int, float]:
        return (len(group), sum(w["pnl"] for w in group))

    sorted_groups = sorted(by_fingerprint.values(), key=_sig_score, reverse=True)

    spawned = 0
    skipped_existing = 0
    for group in sorted_groups:
        if spawned >= max_variants:
            break
        if len(group) < min_winners:
            continue

        # Use the most-recent winner as the canonical exemplar
        exemplar = max(group, key=lambda w: w["trade_id"])
        snap = exemplar["entry_snapshot"]
        fp = exemplar["fingerprint"]
        timeframe = _hold_to_timeframe(exemplar["hold_hours"])
        parent_id = exemplar["scan_pattern_id"]

        conds = _build_conditions_from_signature(snap)
        if not conds:
            continue

        new_id = _spawn_variant_pattern(
            sess,
            fingerprint=fp,
            base_conditions=conds,
            timeframe=timeframe,
            parent_pattern_id=parent_id,
            n_winners=len(group),
        )
        if new_id is None:
            skipped_existing += 1
            continue
        spawned += 1
        logger.info(
            "%s spawned candidate id=%s fp=%s tf=%s n_winners=%d parent=%s",
            LOG_PREFIX, new_id, fp[:12], timeframe, len(group), parent_id,
        )

    return {
        "winners_found": len(winners),
        "unique_signatures": len(by_fingerprint),
        "variants_spawned": spawned,
        "skipped_existing": skipped_existing,
    }
