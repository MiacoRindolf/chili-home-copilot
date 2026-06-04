"""Equity-native pattern miner.

The equity (stock) side of the brain has been starved of *certified* alpha:
the dedicated ``crypto/pattern_miner`` mines fresh candidates FROM realized
crypto winners, but equity relied on the theory-first ``web_pattern_researcher``
(spawns theoretical setups and hopes). Result: ~7x more crypto candidates than
equity, and only a handful of equity patterns ever pass certification.

This module closes that gap with the SAME empirically-grounded strategy the
crypto miner uses, pointed at the equity book:

  **mine FROM realized equity winners.** When an equity trade closes
  profitable, its entry indicator snapshot is a known-good signature. Group
  winners by signature; for each signature with N+ wins, spawn candidate
  ``scan_patterns`` whose conditions mirror it. Candidates enter the normal
  ``candidate -> backtested -> challenged -> promoted`` lifecycle and certify
  (CPCV/DSR/PBO) before they are ever eligible to trade live capital.

Safety: this generates *research candidates only*. It places no orders, promotes
nothing, and is gated behind ``brain_equity_miner_enabled`` (default **False**).
Nothing it produces can touch live capital without passing certification AND an
operator promotion.

Design choices vs the crypto miner (per the project's no-magic-numbers rule):
  * Winner source is ``asset_kind='equity'`` (not 'crypto').
  * Timeframe is equity-appropriate (intraday vs daily swing), derived from each
    signature's observed hold duration rather than crypto's 5m/15m bias.
  * The candidate's seed exit policy is **adaptive**: ``max_bars`` is derived
    from how long the winning trades in the signature actually held; ``atr_mult``
    / ``target_r_multiple`` are inherited from the parent pattern's proven exit
    config when available, falling back to the system's established miner
    defaults only when there is no parent policy to reuse.

Reuses the crypto miner's asset-agnostic signature + condition helpers so the
two miners cannot drift in how they read a winning snapshot.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Reuse the asset-agnostic signature + condition builders so equity and crypto
# read a winning snapshot identically.
from .crypto.pattern_miner import (
    _build_conditions_from_signature,
    _fingerprint_signature,
)

logger = logging.getLogger(__name__)
LOG_PREFIX = "[equity_miner]"

# Bar duration per candidate timeframe, used to convert an observed hold (hours)
# into a bar count for the adaptive ``max_bars`` seed.
_TIMEFRAME_BAR_HOURS: dict[str, float] = {"1h": 1.0, "1d": 24.0}

# Established system miner exit defaults (same values the crypto miner seeds).
# Used ONLY as a last-resort fallback when a signature has no parent pattern
# whose proven exit policy we can inherit — not as a tuning knob.
_FALLBACK_ATR_MULT = 1.6
_FALLBACK_TARGET_R = 2.5


def _equity_timeframe_for_hold(hold_hours: float) -> str:
    """Equity winners are mostly multi-hour-to-multi-day swings. Intraday
    holds map to 1h bars; anything held a session or longer maps to 1d."""
    return "1h" if hold_hours < 4.0 else "1d"


def discover_equity_winners(
    sess: Session,
    *,
    lookback_days: int,
    include_paper: bool = True,
) -> list[dict[str, Any]]:
    """Pull recent profitable EQUITY winners + their entry signatures.

    Unlike crypto (which has abundant live winners carrying rich entry
    snapshots), the equity book trades little live and stores sparse snapshots
    on the trade row. The rich, consistent indicator vector lives on the
    *linked breakout alert* (``trading_breakout_alerts.indicator_snapshot``),
    so we source the signature from there.

    Raw material is widened to the paper-shadow book (``include_paper``) because
    that is the equity exploration ground: it generates the volume needed for a
    signature to repeat. Spawned candidates still pass real backtest/CPCV
    certification before they can ever trade live capital, so paper-sourced
    signatures are a safe seed, not a shortcut to live risk.
    """
    paper_union = (
        """
        UNION ALL
        SELECT p.id AS trade_id, p.ticker, p.scan_pattern_id, p.pnl,
               p.created_at AS entry_date, p.exit_date,
               ba.indicator_snapshot
        FROM trading_paper_trades p
        JOIN trading_breakout_alerts ba
          ON ba.id = NULLIF(p.signal_json->>'breakout_alert_id', '')::int
        WHERE p.status IN ('closed', 'expired')
          AND p.pnl IS NOT NULL AND p.pnl > 0
          AND p.exit_date IS NOT NULL AND p.created_at IS NOT NULL
          AND p.exit_date > NOW() - make_interval(days => :ld)
          AND UPPER(COALESCE(p.ticker, '')) NOT LIKE '%-USD'
          AND UPPER(COALESCE(p.ticker, '')) NOT LIKE '%-USDC'
        """
        if include_paper
        else ""
    )
    rows = sess.execute(
        text(
            f"""
            SELECT t.id AS trade_id, t.ticker, t.scan_pattern_id, t.pnl,
                   t.entry_date, t.exit_date,
                   ba.indicator_snapshot
            FROM trading_management_envelopes t
            JOIN trading_breakout_alerts ba ON ba.id = t.related_alert_id
            WHERE t.asset_kind = 'equity'
              AND t.status IN ('closed', 'expired')
              AND t.pnl IS NOT NULL AND t.pnl > 0
              AND t.entry_date IS NOT NULL AND t.exit_date IS NOT NULL
              AND t.exit_date > NOW() - make_interval(days => :ld)
            {paper_union}
            ORDER BY exit_date DESC
            """
        ),
        {"ld": int(lookback_days)},
    ).fetchall()

    winners: list[dict[str, Any]] = []
    for r in rows:
        snap_raw = r.indicator_snapshot
        if not snap_raw:
            continue
        try:
            snap = json.loads(snap_raw) if isinstance(snap_raw, str) else dict(snap_raw)
        except Exception:
            continue
        if not isinstance(snap, dict):
            continue
        # Equity snapshots nest flat indicators deeper than crypto:
        #   indicator_snapshot.breakout_alert.flat_indicators.{rsi_14, macd, ...}
        # Fall back through the crypto shape (breakout_alert flat) and the bare
        # top level so a single miner reads every snapshot variant we emit.
        entry: dict[str, Any] | None = None
        ba = snap.get("breakout_alert")
        if isinstance(ba, dict):
            if isinstance(ba.get("flat_indicators"), dict):
                entry = ba["flat_indicators"]
            else:
                entry = ba
        elif isinstance(snap.get("flat_indicators"), dict):
            entry = snap["flat_indicators"]
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
        winners.append(
            {
                "trade_id": int(r.trade_id),
                "ticker": str(r.ticker),
                "scan_pattern_id": int(r.scan_pattern_id) if r.scan_pattern_id else None,
                "pnl": float(r.pnl),
                "hold_hours": float(hold_hours),
                "entry_snapshot": entry,
                "fingerprint": fp,
            }
        )
    return winners


def _parent_exit_config(sess: Session, parent_pattern_id: int | None) -> dict[str, Any]:
    """Return the parent pattern's exit_config (proven policy) or {}."""
    if not parent_pattern_id:
        return {}
    row = sess.execute(
        text("SELECT exit_config FROM scan_patterns WHERE id = :pid LIMIT 1"),
        {"pid": int(parent_pattern_id)},
    ).fetchone()
    if not row or not row.exit_config:
        return {}
    cfg = row.exit_config
    try:
        return json.loads(cfg) if isinstance(cfg, str) else dict(cfg)
    except Exception:
        return {}


def _adaptive_exit_config(
    group: list[dict[str, Any]],
    *,
    timeframe: str,
    parent_exit_config: dict[str, Any],
) -> dict[str, Any]:
    """Seed the candidate's exit policy from observed evidence.

    ``max_bars`` is derived from how long the winning trades actually held (75th
    percentile hold / bar duration), so the backtester gives the pattern enough
    room to repeat what already worked. ``atr_mult`` / ``target_r_multiple`` are
    inherited from the parent pattern's proven exit policy when present, else the
    established system fallback. No new tuning constants are introduced.
    """
    bar_hours = _TIMEFRAME_BAR_HOURS.get(timeframe, 24.0)
    holds = sorted(float(w["hold_hours"]) for w in group if w.get("hold_hours") is not None)
    if holds:
        idx = min(len(holds) - 1, int(math.ceil(0.75 * len(holds)) - 1))
        p75_hold = holds[max(0, idx)]
    else:
        p75_hold = bar_hours
    max_bars = max(3, int(math.ceil(p75_hold / bar_hours)))

    atr_mult = parent_exit_config.get("atr_mult", _FALLBACK_ATR_MULT)
    target_r = parent_exit_config.get("target_r_multiple", _FALLBACK_TARGET_R)
    use_bos = bool(parent_exit_config.get("use_bos", False))
    return {
        "max_bars": int(max_bars),
        "atr_mult": float(atr_mult),
        "use_bos": use_bos,
        "target_r_multiple": float(target_r),
        "exit_seed_source": "equity_miner_adaptive",
    }


def _spawn_equity_variant_pattern(
    sess: Session,
    *,
    fingerprint: str,
    base_conditions: list[dict[str, Any]],
    timeframe: str,
    exit_config: dict[str, Any],
    parent_pattern_id: int | None,
    n_winners: int,
) -> int | None:
    """Insert a new equity candidate from the winning signature. Idempotent on
    name; returns the new pattern id or None if it already exists."""
    name = f"Equity Auto Mined {fingerprint[:8]} ({timeframe})"
    existing = sess.execute(
        text("SELECT id FROM scan_patterns WHERE name = :n LIMIT 1"), {"n": name}
    ).fetchone()
    if existing:
        return None
    if not base_conditions:
        return None

    rules_json = json.dumps({"conditions": base_conditions})
    exit_cfg = json.dumps(exit_config)
    description = (
        f"Auto-mined from {n_winners} profitable equity trade(s) sharing "
        f"signature {fingerprint[:8]}. Emitted by equity pattern_miner."
    )
    row = sess.execute(
        text(
            """
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
                'equity_miner_auto', 'equity', :tf,
                0.5, NULL, NULL,
                :n_winners, 0, 0,
                0.0, 0.0, 1,
                :parent_id,
                'candidate', 'pending_oos',
                'universal', 50,
                true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            RETURNING id
            """
        ),
        {
            "name": name,
            "description": description,
            "rules": rules_json,
            "exit_cfg": exit_cfg,
            "tf": timeframe,
            "parent_id": parent_pattern_id,
            "n_winners": n_winners,
        },
    ).fetchone()
    sess.commit()
    return int(row.id) if row else None


def run_equity_pattern_miner(sess: Session) -> dict[str, Any]:
    """Single invocation of the equity miner. Returns a summary dict.

    Designed to be called from the brain-worker subtask registry. Disabled by
    default; enable with ``brain_equity_miner_enabled=true``.
    """
    from ...config import settings

    if not bool(getattr(settings, "brain_equity_miner_enabled", False)):
        return {"skipped": True, "reason": "disabled"}

    # Equity trades are sparser than crypto, so default to a longer lookback.
    lookback = int(getattr(settings, "brain_equity_miner_lookback_days", 90))
    min_winners = int(getattr(settings, "brain_equity_miner_min_winners_per_signature", 3))
    max_variants = int(getattr(settings, "brain_equity_miner_max_variants_per_run", 10))

    winners = discover_equity_winners(sess, lookback_days=lookback)
    if not winners:
        return {"winners_found": 0, "variants_spawned": 0}

    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for w in winners:
        by_fingerprint.setdefault(w["fingerprint"], []).append(w)

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
        exemplar = max(group, key=lambda w: w["trade_id"])
        snap = exemplar["entry_snapshot"]
        fp = exemplar["fingerprint"]
        timeframe = _equity_timeframe_for_hold(exemplar["hold_hours"])
        parent_id = exemplar["scan_pattern_id"]

        conds = _build_conditions_from_signature(snap)
        if not conds:
            continue

        exit_config = _adaptive_exit_config(
            group,
            timeframe=timeframe,
            parent_exit_config=_parent_exit_config(sess, parent_id),
        )
        new_id = _spawn_equity_variant_pattern(
            sess,
            fingerprint=fp,
            base_conditions=conds,
            timeframe=timeframe,
            exit_config=exit_config,
            parent_pattern_id=parent_id,
            n_winners=len(group),
        )
        if new_id is None:
            skipped_existing += 1
            continue
        spawned += 1
        logger.info(
            "%s spawned equity candidate id=%s fp=%s tf=%s n_winners=%d parent=%s",
            LOG_PREFIX,
            new_id,
            fp[:12],
            timeframe,
            len(group),
            parent_id,
        )

    return {
        "winners_found": len(winners),
        "unique_signatures": len(by_fingerprint),
        "variants_spawned": spawned,
        "skipped_existing": skipped_existing,
    }
