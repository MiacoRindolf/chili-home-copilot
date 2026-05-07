"""position-identity-phase-1 (2026-05-04) backfill script.

Per docs/DESIGN/POSITION_IDENTITY.md § 8.1: walk trading_trades AND
trading_paper_trades, seed trading_positions rows + initial events.
Idempotent -- re-runs use ON CONFLICT DO NOTHING for positions and a
WHERE NOT EXISTS guard for events. Safe to run multiple times during
the Phase 1 soak window.

Usage:
    docker compose exec -T scheduler-worker python /app/scripts/backfill_position_rows.py
"""
from __future__ import annotations

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db import SessionLocal


def _backfill_open_from_trades(db) -> tuple[int, int]:
    """Insert trading_positions rows for every open trade with a
    distinct natural key. Write a synthetic 'opened' event per new
    position. Returns (positions_created, events_written)."""
    positions_created = 0
    events_written = 0

    rows = db.execute(text("""
        SELECT DISTINCT
            user_id,
            broker_source,
            COALESCE(broker_source, 'manual') AS bs_clean,
            ticker,
            direction,
            asset_kind
        FROM trading_trades
        WHERE status='open'
          AND broker_source IS NOT NULL
          AND ticker IS NOT NULL
          AND direction IS NOT NULL
    """)).fetchall()

    now = datetime.utcnow()
    for row in rows:
        user_id, broker_source, _bs_clean, ticker, direction, asset_kind = row
        if not broker_source:
            continue
        # account_type='cash' for live broker observations in Phase 1.
        ins = db.execute(text("""
            INSERT INTO trading_positions (
                user_id, broker_source, account_type, ticker, direction,
                asset_kind, state,
                last_observed_at, last_state_transition_at
            ) VALUES (
                :uid, :bs, 'cash', :tk, :dir,
                :ak, 'open',
                :now, :now
            )
            ON CONFLICT ON CONSTRAINT uq_trading_positions_natural_key DO NOTHING
            RETURNING id
        """), {
            "uid": user_id, "bs": broker_source, "tk": ticker,
            "dir": direction, "ak": asset_kind, "now": now,
        }).first()
        if ins is None:
            continue  # already existed
        pos_id = int(ins[0])
        positions_created += 1
        db.execute(text("""
            INSERT INTO trading_position_events (
                position_id, event_type, transition_reason,
                observed_at
            ) VALUES (
                :pid, 'opened', 'backfill_initial', :now
            )
        """), {"pid": pos_id, "now": now})
        events_written += 1
    db.commit()
    return positions_created, events_written


def _backfill_open_from_paper_trades(db) -> tuple[int, int]:
    """Same shape as _backfill_open_from_trades but for paper trades.
    Paper rows get account_type='paper' on the position.
    """
    positions_created = 0
    events_written = 0

    rows = db.execute(text("""
        SELECT DISTINCT user_id, ticker, direction
        FROM trading_paper_trades
        WHERE status='open'
          AND ticker IS NOT NULL
          AND direction IS NOT NULL
    """)).fetchall()

    now = datetime.utcnow()
    for row in rows:
        user_id, ticker, direction = row
        # Paper positions: broker_source='paper' (no real broker), the
        # account_type='paper' in the natural key disambiguates.
        ins = db.execute(text("""
            INSERT INTO trading_positions (
                user_id, broker_source, account_type, ticker, direction,
                state,
                last_observed_at, last_state_transition_at
            ) VALUES (
                :uid, 'paper', 'paper', :tk, :dir,
                'open',
                :now, :now
            )
            ON CONFLICT ON CONSTRAINT uq_trading_positions_natural_key DO NOTHING
            RETURNING id
        """), {"uid": user_id, "tk": ticker, "dir": direction, "now": now}).first()
        if ins is None:
            continue
        pos_id = int(ins[0])
        positions_created += 1
        db.execute(text("""
            INSERT INTO trading_position_events (
                position_id, event_type, transition_reason,
                observed_at
            ) VALUES (
                :pid, 'opened', 'backfill_initial_paper', :now
            )
        """), {"pid": pos_id, "now": now})
        events_written += 1
    db.commit()
    return positions_created, events_written


def main() -> dict:
    db = SessionLocal()
    try:
        live_pos, live_evt = _backfill_open_from_trades(db)
        paper_pos, paper_evt = _backfill_open_from_paper_trades(db)
        # Total counts for reporting.
        total_positions = db.execute(text(
            "SELECT COUNT(*) FROM trading_positions"
        )).scalar() or 0
        total_paper = db.execute(text(
            "SELECT COUNT(*) FROM trading_positions WHERE account_type='paper'"
        )).scalar() or 0
        total_events = db.execute(text(
            "SELECT COUNT(*) FROM trading_position_events"
        )).scalar() or 0
        summary = {
            "live_positions_created_this_run": live_pos,
            "live_events_written_this_run": live_evt,
            "paper_positions_created_this_run": paper_pos,
            "paper_events_written_this_run": paper_evt,
            "total_positions_in_db": int(total_positions),
            "total_paper_positions_in_db": int(total_paper),
            "total_events_in_db": int(total_events),
        }
        print("[backfill_position_rows] summary:", summary)
        return summary
    finally:
        # FIX 46 pattern (rollback before close).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
