"""Read-only and narrowly-scoped repair helpers for position identity.

This is the operational guardrail for the position/envelope split: surface
open-position mismatches as data, clear stale envelope pointers, and only bind
the one safe case where an open position has exactly one matching open
management envelope.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PositionIntegrityReport:
    open_positions_without_open_trade: list[dict[str, Any]]
    open_trades_without_open_position: list[dict[str, Any]]
    open_positions_missing_current_envelope: list[dict[str, Any]]
    current_envelope_mismatches: list[dict[str, Any]]
    repairable_current_envelope_links: list[dict[str, Any]]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "open_positions_without_open_trade": len(
                self.open_positions_without_open_trade
            ),
            "open_trades_without_open_position": len(
                self.open_trades_without_open_position
            ),
            "open_positions_missing_current_envelope": len(
                self.open_positions_missing_current_envelope
            ),
            "current_envelope_mismatches": len(self.current_envelope_mismatches),
            "repairable_current_envelope_links": len(
                self.repairable_current_envelope_links
            ),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "open_positions_without_open_trade": self.open_positions_without_open_trade,
            "open_trades_without_open_position": self.open_trades_without_open_position,
            "open_positions_missing_current_envelope": self.open_positions_missing_current_envelope,
            "current_envelope_mismatches": self.current_envelope_mismatches,
            "repairable_current_envelope_links": self.repairable_current_envelope_links,
        }


def _rows(db: Session, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    try:
        return [dict(r) for r in result.mappings().all()]
    except AttributeError:
        return [dict(r._mapping) for r in result.fetchall()]


def audit_position_identity(db: Session, *, limit: int = 100) -> PositionIntegrityReport:
    """Return the current position/envelope invariant breaks.

    The report intentionally does not mutate. It is safe for dashboards,
    health checks, and pre-live readiness checks.
    """
    lim = max(1, int(limit or 100))

    open_positions_without_open_trade = _rows(db, """
        SELECT p.id AS position_id, p.user_id, p.broker_source, p.account_type,
               p.ticker, p.direction, p.current_quantity, p.current_avg_price,
               p.current_envelope_id, p.last_observed_at
        FROM trading_positions p
        WHERE p.state = 'open'
          AND NOT EXISTS (
              SELECT 1
              FROM trading_trades t
              WHERE t.status = 'open'
                AND t.user_id = p.user_id
                AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
                AND t.ticker = p.ticker
                AND LOWER(COALESCE(t.direction, 'long')) = p.direction
          )
        ORDER BY p.last_observed_at DESC NULLS LAST, p.id DESC
        LIMIT :limit
    """, {"limit": lim})

    open_trades_without_open_position = _rows(db, """
        SELECT t.id AS trade_id, t.user_id, LOWER(COALESCE(t.broker_source, '')) AS broker_source,
               t.ticker, LOWER(COALESCE(t.direction, 'long')) AS direction,
               t.quantity, t.entry_price, t.entry_date
        FROM trading_trades t
        WHERE t.status = 'open'
          AND NOT EXISTS (
              SELECT 1
              FROM trading_positions p
              WHERE p.state = 'open'
                AND p.user_id = t.user_id
                AND p.broker_source = LOWER(COALESCE(t.broker_source, ''))
                AND p.ticker = t.ticker
                AND p.direction = LOWER(COALESCE(t.direction, 'long'))
          )
        ORDER BY t.entry_date DESC NULLS LAST, t.id DESC
        LIMIT :limit
    """, {"limit": lim})

    open_positions_missing_current_envelope = _rows(db, """
        SELECT p.id AS position_id, p.user_id, p.broker_source, p.account_type,
               p.ticker, p.direction, p.current_quantity, p.current_avg_price,
               p.current_envelope_id, p.last_observed_at
        FROM trading_positions p
        WHERE p.state = 'open'
          AND p.current_envelope_id IS NULL
        ORDER BY p.last_observed_at DESC NULLS LAST, p.id DESC
        LIMIT :limit
    """, {"limit": lim})

    current_envelope_mismatches = _rows(db, """
        SELECT p.id AS position_id, p.user_id AS position_user_id,
               p.broker_source AS position_broker_source, p.ticker AS position_ticker,
               p.direction AS position_direction, p.current_envelope_id,
               t.id AS trade_id, t.user_id AS trade_user_id,
               LOWER(COALESCE(t.broker_source, '')) AS trade_broker_source,
               t.ticker AS trade_ticker, LOWER(COALESCE(t.direction, 'long')) AS trade_direction,
               t.status AS trade_status
        FROM trading_positions p
        LEFT JOIN trading_trades t ON t.id = p.current_envelope_id
        WHERE p.current_envelope_id IS NOT NULL
          AND (
              t.id IS NULL
              OR t.status <> 'open'
              OR t.user_id <> p.user_id
              OR LOWER(COALESCE(t.broker_source, '')) <> p.broker_source
              OR t.ticker <> p.ticker
              OR LOWER(COALESCE(t.direction, 'long')) <> p.direction
          )
        ORDER BY p.id DESC
        LIMIT :limit
    """, {"limit": lim})

    repairable_current_envelope_links = _rows(db, """
        WITH matches AS (
            SELECT p.id AS position_id,
                   MIN(t.id) AS trade_id,
                   COUNT(*) AS open_trade_count
            FROM trading_positions p
            JOIN trading_trades t
              ON t.status = 'open'
             AND t.user_id = p.user_id
             AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
             AND t.ticker = p.ticker
             AND LOWER(COALESCE(t.direction, 'long')) = p.direction
            WHERE p.state = 'open'
              AND p.current_envelope_id IS NULL
            GROUP BY p.id
        )
        SELECT position_id, trade_id, open_trade_count
        FROM matches
        WHERE open_trade_count = 1
        ORDER BY position_id DESC
        LIMIT :limit
    """, {"limit": lim})

    return PositionIntegrityReport(
        open_positions_without_open_trade=open_positions_without_open_trade,
        open_trades_without_open_position=open_trades_without_open_position,
        open_positions_missing_current_envelope=open_positions_missing_current_envelope,
        current_envelope_mismatches=current_envelope_mismatches,
        repairable_current_envelope_links=repairable_current_envelope_links,
    )


def repair_current_envelope_links(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    """Repair ``trading_positions.current_envelope_id`` safely.

    This does not create/close trades or positions. It clears pointers that no
    longer resolve to the matching open trade, then fills the pointer when an
    open position has exactly one matching open trade by natural key. Ambiguous
    or missing matches stay untouched for human/ops review.
    """
    stale_candidates = _rows(db, """
        SELECT p.id AS position_id, p.current_envelope_id
        FROM trading_positions p
        LEFT JOIN trading_trades t ON t.id = p.current_envelope_id
        WHERE p.current_envelope_id IS NOT NULL
          AND (
              t.id IS NULL
              OR t.status <> 'open'
              OR t.user_id <> p.user_id
              OR LOWER(COALESCE(t.broker_source, '')) <> p.broker_source
              OR t.ticker <> p.ticker
              OR LOWER(COALESCE(t.direction, 'long')) <> p.direction
          )
        ORDER BY p.id
    """)

    candidates = _rows(db, """
        WITH matches AS (
            SELECT p.id AS position_id,
                   MIN(t.id) AS trade_id,
                   COUNT(*) AS open_trade_count
            FROM trading_positions p
            JOIN trading_trades t
              ON t.status = 'open'
             AND t.user_id = p.user_id
             AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
             AND t.ticker = p.ticker
             AND LOWER(COALESCE(t.direction, 'long')) = p.direction
            WHERE p.state = 'open'
              AND p.current_envelope_id IS NULL
            GROUP BY p.id
        )
        SELECT position_id, trade_id
        FROM matches
        WHERE open_trade_count = 1
        ORDER BY position_id
    """)

    if dry_run:
        return {
            "dry_run": dry_run,
            "eligible": len(candidates),
            "updated": 0,
            "stale": len(stale_candidates),
            "cleared": 0,
            "candidates": candidates,
            "stale_candidates": stale_candidates,
        }

    clear_result = db.execute(text("""
        WITH bad AS (
            SELECT p.id AS position_id
            FROM trading_positions p
            LEFT JOIN trading_trades t ON t.id = p.current_envelope_id
            WHERE p.current_envelope_id IS NOT NULL
              AND (
                  t.id IS NULL
                  OR t.status <> 'open'
                  OR t.user_id <> p.user_id
                  OR LOWER(COALESCE(t.broker_source, '')) <> p.broker_source
                  OR t.ticker <> p.ticker
                  OR LOWER(COALESCE(t.direction, 'long')) <> p.direction
              )
        )
        UPDATE trading_positions p
           SET current_envelope_id = NULL,
               updated_at = NOW()
          FROM bad
         WHERE p.id = bad.position_id
    """))

    result = db.execute(text("""
        WITH matches AS (
            SELECT p.id AS position_id,
                   MIN(t.id) AS trade_id,
                   COUNT(*) AS open_trade_count
            FROM trading_positions p
            JOIN trading_trades t
              ON t.status = 'open'
             AND t.user_id = p.user_id
             AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
             AND t.ticker = p.ticker
             AND LOWER(COALESCE(t.direction, 'long')) = p.direction
            WHERE p.state = 'open'
              AND p.current_envelope_id IS NULL
            GROUP BY p.id
        )
        UPDATE trading_positions p
           SET current_envelope_id = matches.trade_id,
               updated_at = NOW()
          FROM matches
         WHERE p.id = matches.position_id
           AND matches.open_trade_count = 1
           AND p.current_envelope_id IS NULL
    """))
    updated = int(result.rowcount or 0)
    return {
        "dry_run": False,
        "eligible": len(candidates),
        "updated": updated,
        "stale": len(stale_candidates),
        "cleared": int(clear_result.rowcount or 0),
        "candidates": candidates,
        "stale_candidates": stale_candidates,
    }


__all__ = [
    "PositionIntegrityReport",
    "audit_position_identity",
    "repair_current_envelope_links",
]
