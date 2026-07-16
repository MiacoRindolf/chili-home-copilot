from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    ross_smallcap_profile_evidence,
)


def _symbol_from_snapshot(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("T") or row.get("symbol") or "").strip().upper()


def evaluate_live_eligible_rows(
    rows: Sequence[dict[str, Any]],
    *,
    snapshot_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    offenders: list[dict[str, Any]] = []
    snapshots = snapshot_by_symbol or {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        explain = row.get("explain_json") if isinstance(row.get("explain_json"), dict) else {}
        ok, reason, debug = ross_smallcap_profile_evidence(
            symbol,
            signal=explain,
            snapshot_row=snapshots.get(symbol),
            profile=EQUITY_ROSS_SMALLCAP,
        )
        if not ok:
            offenders.append(
                {
                    "id": row.get("id"),
                    "symbol": symbol,
                    "reason": reason,
                    "debug": debug,
                    "updated_at": row.get("updated_at"),
                }
            )
    return offenders


def _fetch_live_eligible_rows(db, *, max_rows: int) -> list[dict[str, Any]]:
    rows = db.execute(
        sa.text(
            """
            SELECT id, symbol, explain_json, updated_at
            FROM momentum_symbol_viability
            WHERE live_eligible
              AND (scope IN ('equity', 'symbol') OR scope IS NULL)
              AND symbol NOT LIKE '%-%'
            ORDER BY updated_at DESC NULLS LAST, id DESC
            LIMIT :max_rows
            """
        ),
        {"max_rows": int(max_rows)},
    ).mappings().all()
    return [dict(row) for row in rows]


def _snapshot_by_symbol() -> dict[str, dict[str, Any]]:
    from app.services.massive_client import get_full_market_snapshot

    snapshot = get_full_market_snapshot(max_age_seconds=EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds)
    out: dict[str, dict[str, Any]] = {}
    for row in snapshot or []:
        sym = _symbol_from_snapshot(row)
        if sym:
            out[sym] = row
    return out


def _demote_offenders(db, offenders: Sequence[dict[str, Any]]) -> int:
    ids: list[int] = []
    reason_by_id: dict[int, dict[str, Any]] = {}
    for offender in offenders:
        row_id = offender.get("id")
        if row_id is None:
            continue
        rid = int(row_id)
        ids.append(rid)
        reason_by_id[rid] = {
            "reason": offender.get("reason"),
            "debug": offender.get("debug") or {},
        }
    if not ids:
        return 0

    rows = db.execute(
        sa.text("SELECT id, explain_json FROM momentum_symbol_viability WHERE id = any(:ids)"),
        {"ids": ids},
    ).mappings().all()
    params: list[dict[str, Any]] = []
    for row in rows:
        rid = int(row["id"])
        explain = row.get("explain_json") if isinstance(row.get("explain_json"), dict) else {}
        explain = dict(explain)
        reason = reason_by_id.get(rid) or {}
        explain["ross_live_eligible_demoted"] = True
        explain["ross_live_eligible_demoted_reason"] = reason.get("reason")
        explain["ross_live_eligible_demoted_debug"] = reason.get("debug") or {}
        params.append({"id": rid, "explain_json": json.dumps(explain)})

    for start in range(0, len(params), 1000):
        chunk = params[start : start + 1000]
        db.execute(
            sa.text(
                """
                UPDATE momentum_symbol_viability
                SET live_eligible = false,
                    explain_json = CAST(:explain_json AS jsonb),
                    updated_at = now() at time zone 'utc'
                WHERE id = :id
                """
            ),
            chunk,
        )
    return len(params)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Ross live-eligible DB rows stay inside the Ross small-cap universe.")
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--demote", action="store_true")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        rows = _fetch_live_eligible_rows(db, max_rows=args.max_rows)
        offenders = evaluate_live_eligible_rows(rows, snapshot_by_symbol=_snapshot_by_symbol())
        if offenders and args.demote:
            demoted = _demote_offenders(db, offenders)
            db.commit()
            print(f"ross_live_eligible_demoted={demoted}")
        elif offenders:
            db.rollback()
        else:
            db.rollback()

        if not offenders:
            print("ross_live_eligible_hygiene_ok")
            return 0
        print(f"ross_live_eligible_hygiene_offenders={len(offenders)}")
        for offender in offenders[:50]:
            print(json.dumps(offender, default=str, sort_keys=True))
        return 0 if args.demote else 1
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
