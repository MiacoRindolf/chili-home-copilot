"""position-identity-phase-1 (2026-05-04) audit-query script.

Compares trading_positions snapshot against today's broker-reported
truth. Per docs/DESIGN/POSITION_IDENTITY.md § 8.1 exit criterion:
"after 1 week soak: zero discrepancies in the audit query for active
positions." Run on demand or as part of a soak-window cron.

Usage:
    docker compose exec -T scheduler-worker python /app/scripts/audit_position_layer_parity.py

Output: a summary dict + a non-zero exit code if any discrepancy found.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db import SessionLocal


# Float-equality tolerances (NOT magic-number thresholds; match
# existing convention in _try_emergency_repair_terminal_reject and
# elsewhere in the bracket layer).
_QTY_TOLERANCE = 1e-9
_PRICE_TOLERANCE = 1e-6


def _audit_live_positions() -> dict:
    """Walk trading_positions WHERE state='open' AND account_type='cash'.
    For each, fetch the broker's current snapshot and compare. Returns
    a dict summarising matches + discrepancies."""
    discrepancies: list[dict] = []
    matches = 0
    untested = 0

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id, user_id, broker_source, account_type, ticker, direction,
                   current_quantity, current_avg_price
            FROM trading_positions
            WHERE state='open'
              AND account_type='cash'
            ORDER BY id
        """)).fetchall()

        if not rows:
            return {
                "rows_audited": 0,
                "matches": 0,
                "discrepancies": [],
                "untested_due_to_no_broker_snapshot": 0,
                "ok": True,
            }

        # Pull broker snapshot once.
        try:
            from app.services.broker_service import (
                is_connected, get_positions, get_crypto_positions,
                dedupe_positions_by_ticker,
            )
        except Exception as e:
            return {
                "rows_audited": len(rows),
                "matches": 0,
                "discrepancies": [],
                "untested_due_to_no_broker_snapshot": len(rows),
                "ok": False,
                "error": f"broker_service import failed: {e}",
            }

        if not is_connected():
            return {
                "rows_audited": len(rows),
                "matches": 0,
                "discrepancies": [],
                "untested_due_to_no_broker_snapshot": len(rows),
                "ok": False,
                "error": "broker not connected; cannot audit live positions",
            }

        try:
            broker_positions = dedupe_positions_by_ticker(
                (get_positions() or []) + (get_crypto_positions() or [])
            )
        except Exception as e:
            return {
                "rows_audited": len(rows),
                "matches": 0,
                "discrepancies": [],
                "untested_due_to_no_broker_snapshot": len(rows),
                "ok": False,
                "error": f"broker fetch failed: {e}",
            }

        broker_by_key = {
            ((p.get("ticker") or "").upper().strip()): p
            for p in broker_positions
        }

        for row in rows:
            (pos_id, user_id, broker_source, account_type, ticker, direction,
             cur_qty, cur_avg) = row
            broker = broker_by_key.get((ticker or "").upper().strip())
            if broker is None:
                discrepancies.append({
                    "kind": "broker_missing",
                    "position_id": int(pos_id),
                    "ticker": ticker,
                    "local_qty": cur_qty,
                    "local_avg": cur_avg,
                    "broker_qty": None,
                    "broker_avg": None,
                })
                continue
            broker_qty = float(broker.get("quantity") or 0)
            broker_avg = float(broker.get("average_buy_price") or 0)
            qty_match = abs(float(cur_qty or 0) - broker_qty) <= _QTY_TOLERANCE
            avg_match = (
                cur_avg is None
                or broker_avg == 0
                or abs(float(cur_avg) - broker_avg) <= _PRICE_TOLERANCE
            )
            if qty_match and avg_match:
                matches += 1
                continue
            discrepancies.append({
                "kind": "qty_or_avg_mismatch",
                "position_id": int(pos_id),
                "ticker": ticker,
                "local_qty": cur_qty,
                "local_avg": cur_avg,
                "broker_qty": broker_qty,
                "broker_avg": broker_avg,
            })
        return {
            "rows_audited": len(rows),
            "matches": matches,
            "discrepancies": discrepancies,
            "untested_due_to_no_broker_snapshot": untested,
            "ok": len(discrepancies) == 0,
        }
    finally:
        db.close()


def _audit_paper_positions() -> dict:
    """Paper-mode positions are simulated; "broker truth" is the
    trading_paper_trades table itself. Compare position rows
    (account_type='paper') against trading_paper_trades open rows.
    """
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT p.id, p.user_id, p.ticker, p.direction,
                   p.current_quantity, p.current_avg_price
            FROM trading_positions p
            WHERE p.state='open' AND p.account_type='paper'
        """)).fetchall()
        # Cross-check: every (user, ticker, direction) position has an
        # open paper trade row.
        discrepancies = []
        for row in rows:
            pos_id, user_id, ticker, direction, _q, _a = row
            cnt = db.execute(text("""
                SELECT COUNT(*) FROM trading_paper_trades
                WHERE COALESCE(user_id, -1) = COALESCE(:uid, -1)
                  AND ticker = :tk
                  AND direction = :dir
                  AND status = 'open'
            """), {"uid": user_id, "tk": ticker, "dir": direction}).scalar() or 0
            if int(cnt) == 0:
                discrepancies.append({
                    "kind": "paper_position_without_open_paper_trade",
                    "position_id": int(pos_id),
                    "ticker": ticker,
                    "direction": direction,
                })
        return {
            "paper_rows_audited": len(rows),
            "paper_discrepancies": discrepancies,
            "ok": len(discrepancies) == 0,
        }
    finally:
        db.close()


def main() -> dict:
    live = _audit_live_positions()
    paper = _audit_paper_positions()
    summary = {"live": live, "paper": paper}
    print("[audit_position_layer_parity] summary:", summary)
    if (
        not live.get("ok", False)
        or not paper.get("ok", False)
    ):
        print("[audit_position_layer_parity] FAIL: discrepancies found")
        sys.exit(1)
    print("[audit_position_layer_parity] OK")
    return summary


if __name__ == "__main__":
    main()
