"""Position-identity integrity guardrails."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.services.trading.position_integrity import (
    PositionIntegrityReport,
    close_orphaned_position_identities,
)


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def test_position_integrity_report_counts():
    report = PositionIntegrityReport(
        open_positions_without_open_trade=[{"position_id": 1}],
        open_trades_without_open_position=[{"trade_id": 2}],
        open_positions_missing_current_envelope=[{"position_id": 3}],
        current_envelope_mismatches=[],
        repairable_current_envelope_links=[{"position_id": 3, "trade_id": 4}],
    )

    assert report.counts == {
        "open_positions_without_open_trade": 1,
        "open_trades_without_open_position": 1,
        "open_positions_missing_current_envelope": 1,
        "current_envelope_mismatches": 0,
        "repairable_current_envelope_links": 1,
    }


def test_position_integrity_repairs_only_exact_one_to_one_links():
    src = _read("app/services/trading/position_integrity.py")

    assert "open_trade_count = 1" in src
    assert "current_envelope_id IS NULL" in src
    assert "current_envelope_id = NULL" in src
    assert "UPDATE trading_positions" in src
    assert "does not create/close trades or positions" in src


def test_close_orphaned_position_identities_closes_only_dead_envelopes(db):
    orphan_trade_id = db.execute(text("""
        INSERT INTO trading_trades (
            ticker, status, broker_source, direction, quantity, entry_price,
            entry_date
        ) VALUES (
            'PINT-ORPHAN-USD', 'closed', 'coinbase', 'long', 4, 2.50,
            NOW()
        )
        RETURNING id
    """)).scalar_one()
    repairable_closed_trade_id = db.execute(text("""
        INSERT INTO trading_trades (
            ticker, status, broker_source, direction, quantity, entry_price,
            entry_date
        ) VALUES (
            'PINT-REPAIR-USD', 'closed', 'coinbase', 'long', 4, 2.50,
            NOW()
        )
        RETURNING id
    """)).scalar_one()
    db.execute(text("""
        INSERT INTO trading_trades (
            ticker, status, broker_source, direction, quantity, entry_price,
            entry_date
        ) VALUES (
            'PINT-REPAIR-USD', 'open', 'coinbase', 'long', 4, 2.50,
            NOW()
        )
    """))

    orphan_position_id = db.execute(text("""
        INSERT INTO trading_positions (
            user_id, broker_source, account_type, ticker, direction,
            asset_kind, current_quantity, current_avg_price, state,
            current_envelope_id, last_observed_at
        ) VALUES (
            NULL, 'coinbase', 'spot', 'PINT-ORPHAN-USD', 'long',
            'crypto', 4, 2.50, 'open', :trade_id, NOW()
        )
        RETURNING id
    """), {"trade_id": int(orphan_trade_id)}).scalar_one()
    repairable_position_id = db.execute(text("""
        INSERT INTO trading_positions (
            user_id, broker_source, account_type, ticker, direction,
            asset_kind, current_quantity, current_avg_price, state,
            current_envelope_id, last_observed_at
        ) VALUES (
            NULL, 'coinbase', 'spot', 'PINT-REPAIR-USD', 'long',
            'crypto', 4, 2.50, 'open', :trade_id, NOW()
        )
        RETURNING id
    """), {"trade_id": int(repairable_closed_trade_id)}).scalar_one()

    preview = close_orphaned_position_identities(
        db,
        broker_source="coinbase",
        dry_run=True,
    )

    preview_ids = {int(row["position_id"]) for row in preview["candidates"]}
    assert int(orphan_position_id) in preview_ids
    assert int(repairable_position_id) not in preview_ids

    result = close_orphaned_position_identities(
        db,
        broker_source="coinbase",
        dry_run=False,
    )

    assert result["closed"] >= 1
    orphan_state = db.execute(text("""
        SELECT state, current_quantity
        FROM trading_positions
        WHERE id = :position_id
    """), {"position_id": int(orphan_position_id)}).one()
    assert orphan_state[0] == "closed"
    assert float(orphan_state[1]) == 0.0

    repairable_state = db.execute(text("""
        SELECT state, current_quantity
        FROM trading_positions
        WHERE id = :position_id
    """), {"position_id": int(repairable_position_id)}).one()
    assert repairable_state[0] == "open"
    assert float(repairable_state[1]) == 4.0

    event_reason = db.execute(text("""
        SELECT transition_reason
        FROM trading_position_events
        WHERE position_id = :position_id
        ORDER BY id DESC
        LIMIT 1
    """), {"position_id": int(orphan_position_id)}).scalar_one()
    assert event_reason == "position_identity_orphaned_closed_envelope"
