"""Auto-tune ``ScanPattern.scope_tickers`` from realized per-ticker stats.

Background (2026-04-28 wider-ticker analysis): some demoted patterns
have a positive edge on a SUBSET of tickers but lose in aggregate
because they're firing on tickers where the edge doesn't hold. Pattern
1052 was the canonical case — net +$8 across 26 trades, but +$36
concentrated on ACMR/INFQ and -$10 spread across AGL/ADV/ETH-USD.

The brain learns ticker dependency itself rather than us banning
tickers manually. Once a pattern has enough realized history per
ticker, the autotuner:

* groups closed trades by (scan_pattern_id, ticker)
* keeps only tickers with n >= ``min_trades_per_ticker``
* classifies each as **edge** (total_pnl > 0) or **bleed** (<= 0)
* if the pattern has BOTH edge AND bleed tickers, it sets
  ``ticker_scope = 'explicit_list'`` and ``scope_tickers`` to the
  edge tickers only

Idempotent. Safe to run repeatedly. Records an audit row in
``learning_events`` so we can roll forward / back without losing
provenance.

Tunable via :class:`~app.config.Settings`::

    chili_ticker_autotune_enabled         = True
    chili_ticker_autotune_min_total_trades = 5    # pattern-level
    chili_ticker_autotune_min_trades_per_ticker = 2
    chili_ticker_autotune_lookback_days    = 90
    chili_ticker_autotune_dry_run          = False
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutotuneAction:
    pattern_id: int
    pattern_name: str
    edge_tickers: tuple[str, ...]
    bleed_tickers: tuple[str, ...]
    net_pnl: float
    decision: str  # 'narrow_to_explicit' | 'keep_universal' | 'no_action'

    def to_payload(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "pattern_name": self.pattern_name,
            "edge_tickers": list(self.edge_tickers),
            "bleed_tickers": list(self.bleed_tickers),
            "net_pnl": float(self.net_pnl),
            "decision": self.decision,
        }


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _query_per_ticker_stats(
    sess: Session, *, pattern_ids: list[int] | None, lookback_days: int, min_trades_per_ticker: int
) -> list[dict[str, Any]]:
    """Return per-(pattern, ticker) realized stats for closed trades."""
    sql = """
        SELECT scan_pattern_id, ticker,
               count(*) AS n,
               sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               sum(coalesce(pnl, 0)) AS total_pnl,
               avg(coalesce(pnl, 0)) AS avg_pnl
        FROM trading_trades
        WHERE status = 'closed'
          AND scan_pattern_id IS NOT NULL
          AND ticker IS NOT NULL
          AND exit_date > NOW() - make_interval(days => :lookback_days)
        {pattern_filter}
        GROUP BY scan_pattern_id, ticker
        HAVING count(*) >= :min_per_ticker
    """
    params: dict[str, Any] = {
        "lookback_days": int(lookback_days),
        "min_per_ticker": int(min_trades_per_ticker),
    }
    if pattern_ids:
        sql = sql.format(pattern_filter="AND scan_pattern_id = ANY(:ids)")
        params["ids"] = pattern_ids
    else:
        sql = sql.format(pattern_filter="")
    rows = sess.execute(text(sql), params).fetchall()
    return [
        {
            "scan_pattern_id": int(r.scan_pattern_id),
            "ticker": str(r.ticker),
            "n": int(r.n),
            "wins": int(r.wins or 0),
            "total_pnl": float(r.total_pnl or 0),
            "avg_pnl": float(r.avg_pnl or 0),
        }
        for r in rows
    ]


def _decide_action(
    pattern_id: int, pattern_name: str, ticker_rows: list[dict[str, Any]],
) -> AutotuneAction:
    edge: list[tuple[str, float]] = []
    bleed: list[tuple[str, float]] = []
    net_pnl = 0.0
    for r in ticker_rows:
        net_pnl += r["total_pnl"]
        if r["total_pnl"] > 0:
            edge.append((r["ticker"], r["total_pnl"]))
        else:
            bleed.append((r["ticker"], r["total_pnl"]))
    edge.sort(key=lambda x: -x[1])
    bleed.sort(key=lambda x: x[1])

    edge_t = tuple(t for t, _ in edge)
    bleed_t = tuple(t for t, _ in bleed)

    # Decision rules:
    # - both edge AND bleed -> narrow scope to edge tickers only
    # - only edge (no bleed) -> keep universal (room to find more good tickers)
    # - only bleed -> let EV gate handle it (don't shadow with autotune)
    # - neither -> no action
    if edge and bleed:
        decision = "narrow_to_explicit"
    elif edge and not bleed:
        decision = "keep_universal"
    elif bleed and not edge:
        decision = "no_action"  # EV gate's territory
    else:
        decision = "no_action"

    return AutotuneAction(
        pattern_id=pattern_id,
        pattern_name=pattern_name,
        edge_tickers=edge_t,
        bleed_tickers=bleed_t,
        net_pnl=net_pnl,
        decision=decision,
    )


def _apply_action(sess: Session, action: AutotuneAction, *, dry_run: bool) -> bool:
    """Apply the narrow-to-explicit decision to the ScanPattern row."""
    if action.decision != "narrow_to_explicit":
        return False
    if dry_run:
        logger.info(
            "[ticker_autotune] DRY_RUN would narrow pattern_id=%s name=%s scope_tickers=%s (bleed_was=%s)",
            action.pattern_id, action.pattern_name, list(action.edge_tickers), list(action.bleed_tickers),
        )
        return True

    new_scope_tickers = json.dumps(list(action.edge_tickers))
    sess.execute(text(
        """
        UPDATE scan_patterns
        SET ticker_scope = 'explicit_list',
            scope_tickers = :scope_tickers,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :pid
        """
    ), {"pid": action.pattern_id, "scope_tickers": new_scope_tickers})

    # Audit row in trading_learning_events. Best-effort: don't fail the whole
    # autotune if the events table schema is unexpected.
    try:
        sess.execute(text(
            """
            INSERT INTO trading_learning_events (event_type, description, created_at)
            VALUES ('ticker_autotune', :desc, CURRENT_TIMESTAMP)
            """
        ), {
            "desc": (
                f"pattern_id={action.pattern_id} narrowed to {list(action.edge_tickers)}; "
                f"bleed_tickers_dropped={list(action.bleed_tickers)}; net_pnl={action.net_pnl:.2f}"
            )
        })
    except Exception as e:
        logger.debug("[ticker_autotune] learning_events write skipped: %s", e)

    sess.commit()
    logger.info(
        "[ticker_autotune] APPLIED pattern_id=%s name=%s scope_tickers=%s (dropped=%s, net_pnl=%.2f)",
        action.pattern_id, action.pattern_name, list(action.edge_tickers),
        list(action.bleed_tickers), action.net_pnl,
    )
    return True


def run_autotune(
    sess: Session, *, pattern_ids: list[int] | None = None, dry_run: bool | None = None,
) -> list[AutotuneAction]:
    """Main entry point. Iterate eligible patterns and apply scope narrowing.

    ``pattern_ids=None`` runs across all patterns. Use a list for surgical
    rescue ops (e.g. just pattern 1052).
    """
    if not bool(_settings_get("chili_ticker_autotune_enabled", True)):
        logger.info("[ticker_autotune] disabled via chili_ticker_autotune_enabled")
        return []
    if dry_run is None:
        dry_run = bool(_settings_get("chili_ticker_autotune_dry_run", False))

    min_total = int(_settings_get("chili_ticker_autotune_min_total_trades", 5))
    min_per_ticker = int(_settings_get("chili_ticker_autotune_min_trades_per_ticker", 2))
    lookback = int(_settings_get("chili_ticker_autotune_lookback_days", 90))

    rows = _query_per_ticker_stats(
        sess, pattern_ids=pattern_ids,
        lookback_days=lookback, min_trades_per_ticker=min_per_ticker,
    )
    if not rows:
        return []

    # Group rows by pattern.
    by_pattern: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_pattern.setdefault(r["scan_pattern_id"], []).append(r)

    # Pull pattern names + filter to patterns with enough total realized history.
    pattern_meta = {
        int(r.id): {
            "name": r.name or f"pattern_{r.id}",
            "ticker_scope": r.ticker_scope,
            "trade_count": int(r.trade_count or 0),
        }
        for r in sess.execute(text(
            "SELECT id, name, ticker_scope, trade_count FROM scan_patterns WHERE id = ANY(:ids)"
        ), {"ids": list(by_pattern.keys())}).fetchall()
    }

    actions: list[AutotuneAction] = []
    for pid, ticker_rows in by_pattern.items():
        meta = pattern_meta.get(pid)
        if not meta:
            continue
        # Sample-size gate at the pattern level. Both checks: realized
        # trade_count column AND the sum across per-ticker rows we just
        # pulled (in case trade_count column isn't synced).
        rows_n_total = sum(r["n"] for r in ticker_rows)
        if meta["trade_count"] < min_total and rows_n_total < min_total:
            continue
        # Don't try to narrow patterns that are already on an explicit list —
        # those were either set by an operator or a prior autotune run, and
        # narrowing further could be too aggressive without an "expand" rule.
        if (meta["ticker_scope"] or "universal") != "universal":
            continue

        action = _decide_action(pid, meta["name"], ticker_rows)
        actions.append(action)
        _apply_action(sess, action, dry_run=dry_run)

    return actions
