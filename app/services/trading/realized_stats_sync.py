"""Sync ``ScanPattern.raw_realized_*`` from ``trading_trades``.

Background (2026-04-28): the audit showed many patterns with stored
``win_rate`` but ``trade_count = 0``. The EWMA-drop write paths in
:mod:`learning.py` only fire on the alert-feedback / closed-trade
update loops; patterns whose live trades came in via other code paths
(broker reconcile, manual close, etc.) never have their column synced.

2026-05-14 (f-canonical-outcome-layer Phase A): this writer used to
also write the legacy ``{trade_count, win_rate, avg_return_pct}``
columns and raced with the corrected writer in
:mod:`learning.update_pattern_stats_from_closed_trades`. Last-writer-
wins meant downstream gates could read the dumber raw numbers
(pattern 585 was the textbook case). The split: this module writes
**only** ``raw_realized_*``; ``learning.py`` writes corrected_* plus
the legacy columns. Readers go through
``pattern_stats_accessor.get_corrected_pattern_stats``.

Tunable::

    chili_realized_sync_enabled                = True
    chili_realized_sync_lookback_days          = 365   # all-time by default? 365 keeps it bounded
    chili_realized_sync_min_n                  = 1     # don\'t bother for patterns with no trades
    chili_canonical_outcome_divergence_info_pct = 0.20  # shadow-log INFO
    chili_canonical_outcome_divergence_warn_pct = 0.50  # shadow-log WARNING
"""
from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _shadow_log_divergence(
    pid: int,
    raw_wr: float | None,
    corrected_wr: float | None,
    info_pct: float,
    warn_pct: float,
) -> None:
    """Compare raw vs corrected win-rate and shadow-log at INFO / WARNING
    thresholds. No DB row, no metric -- Phase A is pure observation
    (per brief consult-gate decision)."""
    if raw_wr is None or corrected_wr is None:
        return
    try:
        cw = float(corrected_wr)
        rw = float(raw_wr)
    except (TypeError, ValueError):
        return
    if not (math.isfinite(cw) and math.isfinite(rw)):
        return
    denom = max(abs(cw), 1e-9)
    delta = abs(rw - cw) / denom
    if delta >= warn_pct:
        logger.warning(
            "[realized_sync] divergence pid=%s corrected=%.4f raw=%.4f delta=%.1f%%",
            pid, cw, rw, delta * 100.0,
        )
    elif delta >= info_pct:
        logger.info(
            "[realized_sync] divergence pid=%s corrected=%.4f raw=%.4f delta=%.1f%%",
            pid, cw, rw, delta * 100.0,
        )


def sync_realized_stats(sess: Session, *, dry_run: bool = False) -> dict[str, int]:
    """Recompute ``raw_realized_{trade_count, win_rate, avg_return_pct}``
    from ``trading_trades``. Returns counts of patterns updated /
    skipped.

    Legacy ``{trade_count, win_rate, avg_return_pct}`` are NEVER written
    by this function -- since 2026-05-14 they are owned exclusively by
    :func:`learning.update_pattern_stats_from_closed_trades` (the
    corrected writer).
    """
    if not bool(_settings_get("chili_realized_sync_enabled", True)):
        logger.info("[realized_sync] disabled via chili_realized_sync_enabled")
        return {"updated": 0, "skipped": 0, "no_trades": 0}

    lookback = int(_settings_get("chili_realized_sync_lookback_days", 365))
    min_n = max(1, int(_settings_get("chili_realized_sync_min_n", 1)))
    info_pct = float(_settings_get("chili_canonical_outcome_divergence_info_pct", 0.20))
    warn_pct = float(_settings_get("chili_canonical_outcome_divergence_warn_pct", 0.50))

    # Realized stats per pattern from trading_trades. Mean-of-trade-returns
    # IS the EV. We compute pct return from entry/exit prices (matches what
    # learning.py does for the EWMA-replacement path).
    #
    # f-evaluation-function-fix Tier A #2 (2026-05-18): also refresh
    # avg_winner_pct / avg_loser_pct / payoff_ratio so the
    # ``_matches_thin_evidence_criteria`` payoff-ratio protection sees
    # current values. Uses ``pnl / (entry_price * quantity)`` (notional-
    # normalized) to match mig 246's backfill convention. Note that
    # avg_ret_pct (above) uses entry-to-exit price return scaled to
    # percent (×100); the payoff columns use the notional-fractional form
    # (no ×100, no quantity cancellation). Both are correct measurements
    # of different things — preserve the existing avg_ret_pct shape for
    # backwards compat.
    rows = sess.execute(text("""
        SELECT scan_pattern_id,
               count(*) AS n,
               sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               avg(
                 CASE
                   WHEN entry_price IS NOT NULL AND entry_price > 0
                        AND exit_price IS NOT NULL
                   THEN ((exit_price - entry_price) / entry_price) * 100.0
                   ELSE NULL
                 END
               ) AS avg_ret_pct,
               avg(
                 CASE
                   WHEN pnl > 0 AND entry_price > 0 AND quantity > 0
                   THEN pnl / (entry_price * quantity)
                 END
               ) AS avg_winner_pct,
               avg(
                 CASE
                   WHEN pnl < 0 AND entry_price > 0 AND quantity > 0
                   THEN pnl / (entry_price * quantity)
                 END
               ) AS avg_loser_pct,
               count(*) FILTER (
                 WHERE pnl IS NOT NULL
                   AND entry_price > 0
                   AND quantity > 0
               ) AS payoff_n
        FROM trading_trades
        WHERE status = 'closed'
          AND scan_pattern_id IS NOT NULL
          AND exit_date > NOW() - make_interval(days => :lookback)
        GROUP BY scan_pattern_id
        HAVING count(*) >= :min_n
    """), {"lookback": lookback, "min_n": min_n}).fetchall()

    updated = 0
    skipped = 0
    for r in rows:
        pid = int(r.scan_pattern_id)
        n = int(r.n)
        wins = int(r.wins or 0)
        wr = (wins / n) if n > 0 else None
        avg_ret = float(r.avg_ret_pct) if r.avg_ret_pct is not None else None

        # f-evaluation-function-fix Tier A #2 (2026-05-18): payoff-ratio
        # quartet. avg_loser_pct is negative; payoff_ratio uses ABS().
        avg_winner_pct = float(r.avg_winner_pct) if r.avg_winner_pct is not None else None
        avg_loser_pct = float(r.avg_loser_pct) if r.avg_loser_pct is not None else None
        payoff_n_val = int(r.payoff_n or 0)
        payoff_ratio_val: float | None = None
        if (
            avg_winner_pct is not None
            and avg_loser_pct is not None
            and avg_loser_pct < 0
            and math.isfinite(avg_winner_pct)
            and math.isfinite(avg_loser_pct)
        ):
            payoff_ratio_val = avg_winner_pct / abs(avg_loser_pct)
            if not math.isfinite(payoff_ratio_val):
                payoff_ratio_val = None

        # NaN/range safety. Migration 241 mirrored the legacy
        # CHECK(win_rate ∈ [0,1]) onto raw_realized_win_rate; respect it.
        if wr is not None and (not math.isfinite(wr) or wr < 0.0 or wr > 1.0):
            logger.warning(
                "[realized_sync] skipping pattern_id=%s — computed wr=%s out of range", pid, wr,
            )
            skipped += 1
            continue
        if avg_ret is not None and not math.isfinite(avg_ret):
            avg_ret = None

        if dry_run:
            logger.info(
                "[realized_sync] DRY pattern_id=%s n=%s wr=%.4f avg_ret_pct=%s "
                "payoff=%s payoff_n=%d",
                pid, n, wr or 0.0,
                f"{avg_ret:.2f}" if avg_ret is not None else "None",
                f"{payoff_ratio_val:.3f}" if payoff_ratio_val is not None else "None",
                payoff_n_val,
            )
            updated += 1
            continue

        # Read the current corrected_win_rate (if any) so we can shadow-
        # log raw vs corrected divergence after the UPDATE. Sticking with
        # corrected_win_rate (not legacy win_rate) because divergence is
        # interesting only against the authoritative value.
        existing = sess.execute(
            text("SELECT corrected_win_rate FROM scan_patterns WHERE id = :pid"),
            {"pid": pid},
        ).first()
        corrected_wr = existing[0] if existing is not None else None

        sess.execute(text("""
            UPDATE scan_patterns
            SET raw_realized_trade_count = :n,
                raw_realized_win_rate = :wr,
                raw_realized_avg_return_pct = :ret,
                raw_realized_stats_updated_at = CURRENT_TIMESTAMP,
                avg_winner_pct = :avg_winner,
                avg_loser_pct = :avg_loser,
                payoff_ratio = :payoff_ratio,
                payoff_ratio_n = :payoff_n,
                payoff_ratio_updated_at = CURRENT_TIMESTAMP
            WHERE id = :pid
        """), {
            "pid": pid, "n": n, "wr": wr, "ret": avg_ret,
            "avg_winner": avg_winner_pct,
            "avg_loser": avg_loser_pct,
            "payoff_ratio": payoff_ratio_val,
            "payoff_n": payoff_n_val,
        })
        updated += 1

        _shadow_log_divergence(pid, wr, corrected_wr, info_pct, warn_pct)

    if not dry_run:
        sess.commit()

    no_trades = sess.execute(text("""
        SELECT count(*) FROM scan_patterns
        WHERE NOT EXISTS (
            SELECT 1 FROM trading_trades
            WHERE scan_pattern_id = scan_patterns.id AND status = 'closed'
        )
    """)).scalar() or 0

    logger.info(
        "[realized_sync] complete: updated=%s skipped=%s patterns_with_no_closed_trades=%s",
        updated, skipped, no_trades,
    )
    return {"updated": updated, "skipped": skipped, "no_trades": int(no_trades)}
