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

from .management_envelopes import (
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)

PHASE5K_POSITION_INTEGRITY_ENV = "CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES"
_POSITION_INTEGRITY_COMPAT_RELATION = LEGACY_TRADES_COMPAT_RELATION
_POSITION_INTEGRITY_ENVELOPE_RELATION = MANAGEMENT_ENVELOPES_RELATION


def _envelope_account_type_sql(alias: str) -> str:
    return (
        f"CASE WHEN LOWER(COALESCE({alias}.broker_source, '')) = 'coinbase' "
        "THEN 'spot' ELSE 'cash' END"
    )


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _position_integrity_source_relation(
    use_envelopes: bool | None = None,
    *,
    settings_: Any | None = None,
) -> str:
    if use_envelopes is None:
        if settings_ is None:
            try:
                from ...config import settings as settings_
            except Exception:
                settings_ = None
        use_envelopes = _truthy_flag(
            getattr(settings_, "chili_phase5k_position_integrity_use_envelopes", False)
        )
    if use_envelopes:
        return _POSITION_INTEGRITY_ENVELOPE_RELATION
    return _POSITION_INTEGRITY_COMPAT_RELATION


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


def audit_position_identity(
    db: Session,
    *,
    limit: int = 100,
    use_envelopes: bool | None = None,
    settings_obj: Any | None = None,
) -> PositionIntegrityReport:
    """Return the current position/envelope invariant breaks.

    The report intentionally does not mutate. It is safe for dashboards,
    health checks, and pre-live readiness checks.
    """
    lim = max(1, int(limit or 100))
    relation = _position_integrity_source_relation(
        use_envelopes,
        settings_=settings_obj,
    )
    t_account_type = _envelope_account_type_sql("t")

    open_positions_without_open_trade = _rows(db, f"""
        SELECT p.id AS position_id, p.user_id, p.broker_source, p.account_type,
               p.ticker, p.direction, p.current_quantity, p.current_avg_price,
               p.current_envelope_id, p.last_observed_at
        FROM trading_positions p
        WHERE p.state = 'open'
          AND NOT EXISTS (
              SELECT 1
              FROM {relation} t
              WHERE t.status = 'open'
                AND t.user_id = p.user_id
                AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
                AND p.account_type = {t_account_type}
                AND t.ticker = p.ticker
                AND LOWER(COALESCE(t.direction, 'long')) = p.direction
          )
        ORDER BY p.last_observed_at DESC NULLS LAST, p.id DESC
        LIMIT :limit
    """, {"limit": lim})

    open_trades_without_open_position = _rows(db, f"""
        SELECT t.id AS trade_id, t.user_id, LOWER(COALESCE(t.broker_source, '')) AS broker_source,
               t.ticker, LOWER(COALESCE(t.direction, 'long')) AS direction,
               t.quantity, t.entry_price, t.entry_date
        FROM {relation} t
        WHERE t.status = 'open'
          AND NOT EXISTS (
              SELECT 1
              FROM trading_positions p
              WHERE p.state = 'open'
                AND p.user_id = t.user_id
                AND p.broker_source = LOWER(COALESCE(t.broker_source, ''))
                AND p.account_type = {t_account_type}
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

    current_envelope_mismatches = _rows(db, f"""
        SELECT p.id AS position_id, p.user_id AS position_user_id,
               p.broker_source AS position_broker_source, p.ticker AS position_ticker,
               p.direction AS position_direction, p.current_envelope_id,
               t.id AS trade_id, t.user_id AS trade_user_id,
               LOWER(COALESCE(t.broker_source, '')) AS trade_broker_source,
               {t_account_type} AS trade_account_type,
               t.ticker AS trade_ticker, LOWER(COALESCE(t.direction, 'long')) AS trade_direction,
               t.status AS trade_status
        FROM trading_positions p
        LEFT JOIN {relation} t ON t.id = p.current_envelope_id
        WHERE p.state = 'open'
          AND p.current_envelope_id IS NOT NULL
          AND (
              t.id IS NULL
              OR t.status <> 'open'
              OR t.user_id <> p.user_id
              OR LOWER(COALESCE(t.broker_source, '')) <> p.broker_source
              OR {t_account_type} <> p.account_type
              OR t.ticker <> p.ticker
              OR LOWER(COALESCE(t.direction, 'long')) <> p.direction
          )
        ORDER BY p.id DESC
        LIMIT :limit
    """, {"limit": lim})

    repairable_current_envelope_links = _rows(db, f"""
        WITH matches AS (
            SELECT p.id AS position_id,
                   MIN(t.id) AS trade_id,
                   COUNT(*) AS open_trade_count
            FROM trading_positions p
            JOIN {relation} t
              ON t.status = 'open'
             AND t.user_id = p.user_id
             AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
             AND p.account_type = {t_account_type}
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


def repair_current_envelope_links(
    db: Session,
    *,
    dry_run: bool = True,
    use_envelopes: bool | None = None,
    settings_obj: Any | None = None,
) -> dict[str, Any]:
    """Repair ``trading_positions.current_envelope_id`` safely.

    This does not create/close trades or positions. It clears pointers that no
    longer resolve to the matching open trade, then fills the pointer when an
    open position has exactly one matching open trade by natural key. Ambiguous
    or missing matches stay untouched for human/ops review.
    """
    relation = _position_integrity_source_relation(
        use_envelopes,
        settings_=settings_obj,
    )
    t_account_type = _envelope_account_type_sql("t")
    stale_candidates = _rows(db, f"""
        SELECT p.id AS position_id, p.current_envelope_id
        FROM trading_positions p
        LEFT JOIN {relation} t ON t.id = p.current_envelope_id
        WHERE p.state = 'open'
          AND p.current_envelope_id IS NOT NULL
          AND (
              t.id IS NULL
              OR t.status <> 'open'
              OR t.user_id <> p.user_id
              OR LOWER(COALESCE(t.broker_source, '')) <> p.broker_source
              OR {t_account_type} <> p.account_type
              OR t.ticker <> p.ticker
              OR LOWER(COALESCE(t.direction, 'long')) <> p.direction
          )
        ORDER BY p.id
    """)

    candidates = _rows(db, f"""
        WITH matches AS (
            SELECT p.id AS position_id,
                   MIN(t.id) AS trade_id,
                   COUNT(*) AS open_trade_count
            FROM trading_positions p
            JOIN {relation} t
              ON t.status = 'open'
             AND t.user_id = p.user_id
             AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
             AND p.account_type = {t_account_type}
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

    clear_result = db.execute(text(f"""
        WITH bad AS (
            SELECT p.id AS position_id
            FROM trading_positions p
            LEFT JOIN {relation} t ON t.id = p.current_envelope_id
            WHERE p.state = 'open'
              AND p.current_envelope_id IS NOT NULL
              AND (
                  t.id IS NULL
                  OR t.status <> 'open'
                  OR t.user_id <> p.user_id
                  OR LOWER(COALESCE(t.broker_source, '')) <> p.broker_source
                  OR {t_account_type} <> p.account_type
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

    result = db.execute(text(f"""
        WITH matches AS (
            SELECT p.id AS position_id,
                   MIN(t.id) AS trade_id,
                   COUNT(*) AS open_trade_count
            FROM trading_positions p
            JOIN {relation} t
              ON t.status = 'open'
             AND t.user_id = p.user_id
             AND LOWER(COALESCE(t.broker_source, '')) = p.broker_source
             AND p.account_type = {t_account_type}
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


def close_orphaned_position_identities(
    db: Session,
    *,
    broker_source: str | None = None,
    dry_run: bool = True,
    limit: int = 500,
    use_envelopes: bool | None = None,
    settings_obj: Any | None = None,
) -> dict[str, Any]:
    """Close local position sidecars whose management envelope is gone.

    This is deliberately narrower than broker stale-close logic. It never
    creates/closes ``Trade`` rows and never synthesizes PnL. It only flips a
    ``trading_positions`` row to closed when its ``current_envelope_id`` points
    at a missing or already-closed trade and there is no matching open trade
    that could safely own the position instead.
    """
    lim = max(1, int(limit or 500))
    broker_clause = ""
    params: dict[str, Any] = {"limit": lim}
    if broker_source:
        broker_clause = "AND p.broker_source = :broker_source"
        params["broker_source"] = broker_source.lower()
    relation = _position_integrity_source_relation(
        use_envelopes,
        settings_=settings_obj,
    )
    open_t_account_type = _envelope_account_type_sql("open_t")

    candidates = _rows(db, f"""
        SELECT p.id AS position_id, p.user_id, p.broker_source, p.account_type,
               p.ticker, p.direction, p.current_quantity, p.current_avg_price,
               p.current_envelope_id, p.last_observed_at,
               t.status AS envelope_status
        FROM trading_positions p
        LEFT JOIN {relation} t ON t.id = p.current_envelope_id
        WHERE p.state = 'open'
          AND p.current_envelope_id IS NOT NULL
          {broker_clause}
          AND (t.id IS NULL OR t.status <> 'open')
          AND NOT EXISTS (
              SELECT 1
              FROM {relation} open_t
              WHERE open_t.status = 'open'
                AND open_t.user_id IS NOT DISTINCT FROM p.user_id
                AND LOWER(COALESCE(open_t.broker_source, '')) = p.broker_source
                AND p.account_type = {open_t_account_type}
                AND open_t.ticker = p.ticker
                AND LOWER(COALESCE(open_t.direction, 'long')) = p.direction
          )
        ORDER BY p.last_observed_at DESC NULLS LAST, p.id DESC
        LIMIT :limit
    """, params)

    if dry_run:
        return {
            "dry_run": True,
            "eligible": len(candidates),
            "closed": 0,
            "candidates": candidates,
        }

    closed = 0
    for row in candidates:
        result = db.execute(text("""
            UPDATE trading_positions
               SET state = 'closed',
                   current_quantity = 0,
                   last_state_transition_at = NOW(),
                   updated_at = NOW()
             WHERE id = :position_id
               AND state = 'open'
        """), {"position_id": int(row["position_id"])})
        if int(result.rowcount or 0) <= 0:
            continue
        db.execute(text("""
            INSERT INTO trading_position_events (
                position_id, event_type, transition_reason, quantity,
                envelope_id, observed_at
            ) VALUES (
                :position_id, 'closed', 'position_identity_orphaned_closed_envelope',
                0, :envelope_id, NOW()
            )
        """), {
            "position_id": int(row["position_id"]),
            "envelope_id": row.get("current_envelope_id"),
        })
        closed += 1

    return {
        "dry_run": False,
        "eligible": len(candidates),
        "closed": closed,
        "candidates": candidates,
    }


__all__ = [
    "PositionIntegrityReport",
    "audit_position_identity",
    "close_orphaned_position_identities",
    "repair_current_envelope_links",
]
